"""
V2 Recovery Simulation — Telemetry-Driven Distributed Systems Visualization
scenes/v2_recovery.py

Renders the recovery_engine.py simulation as a Manim CE scene.
Run: manim -pqh scenes/v2_recovery.py V2_RecoveryScene

Architecture:
  1. Loads config from configs/recovery_test.json
  2. Runs RecoveryEngine to produce metrics_history (SimulationMetrics per tick)
  3. Animates network topology + live telemetry overlays driven by metrics
  4. Plots the recovery curve using always_redraw

Visual philosophy: austere, engineering-first, telemetry-primary.
No cinematic flourishes. Every visual element communicates system state.
"""

from manim import *
import os, sys

# ── Resolve project root ────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from simulations.recovery_engine import (
    RecoveryEngine, load_config
)

# ── Color palette ────────────────────────────────────────────────────────────
BG       = "#0a0a0f"
STABLE   = "#00d4ff"     # cyan
DEGRADED = "#ff9500"     # amber
OVERLOAD = "#ef4444"     # red
FRAG     = "#991b1b"     # deep red
RECOVER  = "#22c55e"     # green
GRID     = "#1a2a3a"
TEXT_DIM = "#94a3b8"
METRIC_OK = "#00d4ff"
METRIC_WN = "#ff9500"
METRIC_ER = "#ef4444"
METRIC_GR = "#22c55e"


class V2_RecoveryScene(Scene):
    """
    Telemetry-driven recovery simulation scene.
    Runs RecoveryEngine → iterates metrics_history → drives ValueTrackers.
    """

    def construct(self):
        # ── 1. Load config + run simulation ──────────────────────────────────
        config_path = os.path.join(PROJECT_ROOT, "configs", "recovery_test.json")
        cfg = load_config(config_path)
        self.cfg = cfg
        engine = RecoveryEngine(cfg)
        self.engine = engine
        metrics_history = engine.run()  # list of SimulationMetrics

        total_ticks = len(metrics_history)
        scene_duration = cfg.get("visualization", {}).get("scene_duration_seconds", 45)

        # ── 2. Build network topology mobjects ────────────────────────────────
        node_objs, edge_lines = self._build_network(engine)
        edge_key_lookup = {(min(a, b), max(a, b)): line for (a, b), line in edge_lines.items()}

        # ── 3. Telemetry overlay panel (top-right) ───────────────────────────
        metric_trackers = self._build_metric_overlay()

        # ── 4. Recovery curve graph (bottom half) ─────────────────────────────
        graph, graph_line = self._build_recovery_curve()

        # ── 5. Phase label (top-left) ────────────────────────────────────────
        phase_label = self._build_phase_label()

        # ── Compose scene ────────────────────────────────────────────────────
        self.add(graph, graph_line, phase_label)
        for line in edge_lines.values():
            self.add(line)
        for n in node_objs.values():
            self.add(n)
        self.add(metric_trackers["panel"])

        # ── 6. Animate through metrics_history ───────────────────────────────
        # steps_per_tick controls animation speed (scaled to scene duration)
        steps_per_tick = max(1, total_ticks // scene_duration)

        for step_idx in range(0, total_ticks, steps_per_tick):
            snap = metrics_history[step_idx]

            # Phase label
            phase = self._phase_from_snap(snap, step_idx, total_ticks)
            self._update_phase_label(phase_label, phase)

            # Node colors
            for nid, node in engine.nodes.items():
                if nid in node_objs:
                    self._update_node(node_objs[nid], node)

            # Edge heat
            for (a, b), line in edge_lines.items():
                node_a = engine.nodes.get(a)
                if node_a:
                    q_pressure = node_a.queue_depth / max(1, node_a.base_capacity)
                    e_heat = getattr(node_a, 'edge_heat', 0.0)
                    heat = max(q_pressure, e_heat)
                    if heat > 0.7:
                        line.set_stroke(OVERLOAD, opacity=0.9)
                    elif heat > 0.4:
                        line.set_stroke(DEGRADED, opacity=0.6)
                    else:
                        line.set_stroke(STABLE, opacity=0.25)

            # Update telemetry trackers
            p95 = float(getattr(snap, 'p95_latency', 0))
            q   = int(getattr(snap, 'queue_depth', 0))
            ret = int(getattr(snap, 'retry_count', 0))
            frag= int(getattr(snap, 'fragmented_nodes', 0))
            stab= float(getattr(snap, 'stability_score', 1.0))

            self._update_metric_trackers(metric_trackers, p95, q, ret, frag, stab)

            # Recovery curve — extend the VGroup line point-by-point
            x_norm = step_idx / max(1, total_ticks - 1)
            y_instability = 1.0 - stab
            x_max = cfg["visualization"]["x_max"] if "x_max" in cfg.get("visualization", {}) else 1.0
            y_max = 1.0
            new_pt = graph.c2p(x_norm * x_max, y_instability * y_max, 0)
            if len(graph_line) > 0:
                last_pt = graph_line[-1].get_end()
                seg = Line(last_pt, new_pt, stroke_color=STABLE, stroke_opacity=0.9, stroke_width=2.0)
            else:
                seg = Line(graph.c2p(0, 0, 0), new_pt, stroke_color=STABLE, stroke_opacity=0.9, stroke_width=2.0)
            graph_line.add(seg)

            self.wait(1.0 / steps_per_tick)

    # ─────────────────────────────────────────────────────────────────────────
    # Network
    # ─────────────────────────────────────────────────────────────────────────

    def _build_network(self, engine: RecoveryEngine) -> tuple:
        """Build Mobjects for nodes (circles) and edges (Lines)."""
        node_objs = {}
        edge_lines = {}

        for nid, node in engine.nodes.items():
            n = self._make_node_circle(nid, node)
            node_objs[nid] = n

        # Edges from topology adjacency dict
        for nid, neighbors in engine.topology.items():
            for nb in neighbors:
                if nb > nid:  # each edge once
                    p_a = np.array([node.x, node.y, 0])
                    p_b = np.array([engine.nodes[nb].x, engine.nodes[nb].y, 0])
                    line = Line(p_a, p_b,
                                stroke_color=STABLE, stroke_opacity=0.25, stroke_width=1.0)
                    edge_lines[(nid, nb)] = line

        return node_objs, edge_lines

    def _make_node_circle(self, nid: int, node) -> VGroup:
        """Node: outer ring + core dot + label."""
        outer = Circle(radius=0.22, fill_opacity=0, stroke_opacity=0)
        core  = Circle(radius=0.14, fill_color=STABLE, fill_opacity=0.85,
                      stroke_color=STABLE, stroke_opacity=0.4)
        label = Text(f"N{nid:02d}", font="Ubuntu Mono", font_size=7,
                     fill_color=WHITE, fill_opacity=0.6)
        group = VGroup(outer, core, label)
        group.move_to(np.array([node.x, node.y, 0]))
        return group

    def _update_node(self, mobj: VGroup, node) -> None:
        """Sync circle color to NodeState."""
        outer, core, label = mobj
        state = getattr(node, 'state', 'STABLE').upper()

        color_map = {
            'FRAGMENTED':  (FRAG,     0.5, 0.0),
            'FAILED':      (FRAG,     0.5, 0.0),
            'OVERLOADED':  (OVERLOAD, 0.9, 0.6),
            'DEGRADED':    (DEGRADED, 0.8, 0.3),
            'RECOVERING':  (RECOVER,  0.7, 0.2),
            'STABLE':      (STABLE,   0.85, 0.0),
        }
        color, op, outer_op = color_map.get(state, (STABLE, 0.85, 0.0))
        core.set_fill(color, opacity=op)
        core.set_stroke(color, opacity=op * 0.5)
        outer.set_stroke(color, opacity=outer_op)
        label.set_fill(color=WHITE, opacity=0.6)

    # ─────────────────────────────────────────────────────────────────────────
    # Telemetry overlay
    # ─────────────────────────────────────────────────────────────────────────

    def _build_metric_overlay(self) -> dict:
        panel = VGroup()
        panel.to_corner(UR, buff=0.4)

        header = Text("LIVE TELEMETRY", font="Ubuntu Mono", font_size=10,
                      fill_color=TEXT_DIM, weight=BOLD)
        header.to_corner(UR)

        p95_t   = ValueTracker(0.0)
        queue_t = ValueTracker(0)
        retry_t = ValueTracker(0)
        frag_t  = ValueTracker(0)
        stab_t  = ValueTracker(1.0)

        def make_label(tracker, fmt_fn, color):
            tr = tracker  # local binding breaks late closure
            return always_redraw(lambda: Text(fmt_fn(tr.get_value()),
                               font="Ubuntu Mono", font_size=9, fill_color=color))

        p95_l   = make_label(p95_t,   lambda v: f"p95: {v:.0f}ms",  METRIC_OK)
        queue_l = make_label(queue_t, lambda v: f"queue: {int(v)}", METRIC_OK)
        retry_l = make_label(retry_t, lambda v: f"retries: {int(v)}", "#ff9500")
        frag_l  = make_label(frag_t,  lambda v: f"frag: {int(v)} nodes", "#ef4444")
        stab_l  = make_label(stab_t,   lambda v: f"stability: {v:.2f}", "#22c55e")

        for lbl in [p95_l, queue_l, retry_l, frag_l, stab_l]:
            panel.add(lbl)

        panel.add(header)
        panel.move_to(panel.get_center())

        return {
            "panel": panel,
            "p95_t": p95_t, "queue_t": queue_t,
            "retry_t": retry_t, "frag_t": frag_t, "stab_t": stab_t,
            "p95_l": p95_l, "queue_l": queue_l,
            "retry_l": retry_l, "frag_l": frag_l, "stab_l": stab_l,
        }

    def _update_metric_trackers(self, trackers, p95, queue, retries, frag, stab):
        trackers["p95_t"].set_value(p95)
        trackers["queue_t"].set_value(queue)
        trackers["retry_t"].set_value(retries)
        trackers["frag_t"].set_value(frag)
        trackers["stab_t"].set_value(stab)

    # ─────────────────────────────────────────────────────────────────────────
    # Recovery curve
    # ─────────────────────────────────────────────────────────────────────────

    def _build_recovery_curve(self) -> tuple:
        """Axes: X = time (0..1 normalized), Y = instability (0..1)."""
        graph = Axes(
            x_range=[0, 1, 0.25],
            y_range=[0, 1, 0.25],
            x_length=7, y_length=3,
            tips=False,
            axis_config={"stroke_color": GRID, "stroke_opacity": 0.5},
        )
        graph.to_corner(DL, buff=0.5)

        x_label = Text("time →", font="Ubuntu Mono", font_size=8, fill_color=TEXT_DIM)
        y_label = Text("instability →", font="Ubuntu Mono", font_size=8, fill_color=TEXT_DIM)
        x_label.next_to(graph, DOWN, buff=0.1)
        y_label.rotate(90).next_to(graph, LEFT, buff=0.15)

        self.add(graph, x_label, y_label)

        # Build graph line as a VGroup that we extend point-by-point
        graph_line = VGroup()
        graph_line.set_stroke(STABLE, opacity=0.9, width=2.0)
        self.add(graph_line)
        return graph, graph_line

    # ─────────────────────────────────────────────────────────────────────────
    # Phase label
    # ─────────────────────────────────────────────────────────────────────────

    def _build_phase_label(self) -> Text:
        label = Text("STABLE", font="Ubuntu Mono", font_size=12,
                     fill_color=STABLE, weight=BOLD)
        label.to_corner(UL, buff=0.4)
        return label

    def _phase_from_snap(self, snap, step_idx, total_ticks) -> str:
        """Derive phase string from simulation metrics + tick position."""
        frag = int(getattr(snap, 'fragmented_nodes', 0))
        stab = float(getattr(snap, 'stability_score', 1.0))
        p95  = float(getattr(snap, 'p95_latency', 20))

        progress = step_idx / max(1, total_ticks)
        recovery_tick = self._recovery_tick()

        if step_idx < 60:
            return "STABLE"
        elif step_idx < 120:
            return "INJECTION"
        elif step_idx < recovery_tick:
            return "CASCADE" if frag > 0 else "FAILURE"
        elif step_idx < recovery_tick + 80:
            return "RECOVERY"
        else:
            outcome = getattr(snap, 'recovery_outcome', 'in_progress')
            return "FULL_RECOVERY" if outcome == "full_recovery" else "SECONDARY_COLLAPSE"

    def _recovery_tick(self) -> int:
        return self.engine.cfg["failure_injection"]["recovery_tick"]

    def _update_phase_label(self, label: Text, phase: str) -> None:
        color_map = {
            "STABLE": STABLE,
            "INJECTION": DEGRADED,
            "CASCADE": OVERLOAD,
            "FAILURE": OVERLOAD,
            "RECOVERY": RECOVER,
            "FULL_RECOVERY": RECOVER,
            "SECONDARY_COLLAPSE": FRAG,
        }
        label.become(Text(phase, font="Ubuntu Mono", font_size=12,
                           fill_color=color_map.get(phase, TEXT_DIM), weight=BOLD))
        label.to_corner(UL, buff=0.4)

    def _update_phase_colors(self, label, phase):
        pass  # handled inline above