"""
recovery_engine.py — The Cascade v2 Core Simulation Engine
=========================================================
Deterministic, config-driven distributed systems recovery simulation.
Produces telemetry CSVs that the visualization layer reads directly.

All state transitions derive from math, not from aesthetic judgment.
"""

from __future__ import annotations

import json, math, random, csv, os, statistics
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Configuration loader
# ─────────────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NodeState:
    node_id: int
    base_latency_ms: float
    base_capacity: float
    base_drain_rate: float

    latency_ms: float = 12.0
    queue_depth: int = 0
    drain_rate: float = 0.85
    failed: bool = False
    fragmented: bool = False
    in_retry_storm: bool = False
    retry_count: int = 0
    failed_requests: int = 0
    success_requests: int = 0
    edge_heat: float = 0.0

    # Position in topology (for visualization)
    x: float = 0.0
    y: float = 0.0

    def total_requests(self) -> int:
        return self.success_requests + self.failed_requests

    def failure_rate(self) -> float:
        total = self.total_requests()
        return self.failed_requests / total if total > 0 else 0.0

    def queue_pressure(self) -> float:
        return self.queue_depth / self.base_capacity

    def update_latency_from_storm(self, amplifier: float):
        if self.in_retry_storm:
            self.latency_ms = self.base_latency_ms * amplifier

    def step_drain(self):
        drained = int(self.queue_depth * self.drain_rate)
        self.queue_depth = max(0, self.queue_depth - drained)

    def enqueue(self, volume: int):
        self.queue_depth = min(
            self.queue_depth + volume,
            int(self.base_capacity * 4)
        )


@dataclass
class SimulationMetrics:
    tick: int
    time_ms: float
    p50_latency: float
    p95_latency: float
    queue_depth: int
    retry_count: int
    fragmented_nodes: int
    stability_score: float
    global_health_score: float
    edge_heat_avg: float
    edge_heat_peak: float
    active_nodes: int
    total_requests: int
    failed_requests: int
    recovery_outcome: str


# ─────────────────────────────────────────────────────────────────────────────
# Recovery Engine
# ─────────────────────────────────────────────────────────────────────────────

class RecoveryEngine:
    """
    Simulates a distributed mesh network under failure injection,
    with configurable recovery dynamics and retry storm amplification.

    All state changes are math-driven and deterministic per seed.
    Telemetry is emitted every tick — the visualization reads this file only.
    """

    def __init__(self, config: dict):
        self.cfg = config
        self.tick = 0
        self.time_ms = 0.0
        self.tick_interval = config["telemetry"]["tick_interval_ms"]
        self.total_ticks = config["telemetry"]["total_ticks"]

        self.nodes: Dict[int, NodeState] = {}
        self.edges: Dict[tuple, float] = {}  # (i,j) -> heat
        # Topology as adjacency dict (built from edge list)
        self.topology: Dict[int, List[int]] = {i: [] for i in range(self.cfg["topology"]["nodes"])}
        self._seed = config["topology"]["seed"]
        self._rng = random.Random(self._seed)

        self.recovery_outcome: Optional[str] = None
        self.phase: str = "STABLE"
        self._last_evidence = None
        self._phase_transitions: List = []   # filled by export_phase_transitions

        self._init_topology()
        self._init_traffic()
        self.metrics_history: List[SimulationMetrics] = []

    # ── Topology ─────────────────────────────────────────────────────────────

    def _init_topology(self):
        n = self.cfg["topology"]["nodes"]
        nd = self.cfg["node_defaults"]

        # Deterministic node positions on a ring
        for i in range(n):
            angle = 2 * math.pi * i / n
            node = NodeState(
                node_id=i,
                base_latency_ms=nd["base_latency_ms"],
                base_capacity=nd["base_capacity"],
                base_drain_rate=nd["base_drain_rate"],
                latency_ms=nd["base_latency_ms"],
                drain_rate=nd["base_drain_rate"],
                x=2.5 * math.cos(angle),
                y=2.5 * math.sin(angle),
            )
            self.nodes[i] = node

        # Mesh edges (ring + chords)
        edge_list = []
        for i in range(n):
            edge_list.append((i, (i + 1) % n))  # ring
        for i in range(n):
            edge_list.append((i, (i + 4) % n))  # chord
        edge_list = list(set(edge_list))  # dedupe

        for (i, j) in edge_list:
            self.edges[(min(i, j), max(i, j))] = 0.0
            self.topology[i].append(j)
            self.topology[j].append(i)

    # ── Traffic ───────────────────────────────────────────────────────────────

    def _init_traffic(self):
        self.traffic_rate = 15  # requests per tick per healthy node
        self.retry_volume = 0

    # ── Per-tick models ──────────────────────────────────────────────────────

    def _generate_latencies(self) -> List[float]:
        """Sample per-node latency: base ± jitter + failure inflation."""
        lats = []
        nd = self.cfg["node_defaults"]
        for node in self.nodes.values():
            jitter = self._rng.gauss(0, nd["base_latency_ms"] * 0.15)
            lats.append(max(1.0, node.latency_ms + jitter))
        return lats

    def _compute_percentiles(self, lats: List[float]) -> tuple:
        if not lats:
            return 0.0, 0.0
        sorted_lats = sorted(lats)
        n = len(sorted_lats)
        p50 = sorted_lats[int(n * 0.50)]
        p95 = sorted_lats[min(int(n * 0.95), n - 1)]
        return p50, p95

    def _decay_heat(self, factor: float):
        """Heat decay with optional retry storm penalty."""
        storm_penalty = self.cfg["recovery"]["retry_backoff_multiplier"] \
            if self.retry_volume > 0 else 1.0
        for key in self.edges:
            self.edges[key] *= factor * storm_penalty
            self.edges[key] = max(0.0, min(1.0, self.edges[key]))

    def _propagate_heat(self):
        """Heat spillover from hot nodes to connected edges."""
        for (i, j), heat in list(self.edges.items()):
            ni = self.nodes.get(i)
            nj = self.nodes.get(j)
            if ni and nj:
                node_heat = max(
                    ni.queue_pressure() + (0.3 if ni.in_retry_storm else 0.0),
                    nj.queue_pressure() + (0.3 if nj.in_retry_storm else 0.0)
                )
                self.edges[(i, j)] = max(heat, node_heat * 0.6)

    def _detect_retry_storms(self):
        """
        Retry storm: when retry volume exceeds capacity threshold.
        Storm amplifies latency, inflates queue, and spikes edge heat.
        """
        rm = self.cfg["retry_storm_model"]
        for node in self.nodes.values():
            if node.failed or node.fragmented:
                continue
            capacity = node.base_capacity * rm["retry_threshold_multiplier"]
            if self.retry_volume > capacity:
                if not node.in_retry_storm:
                    node.in_retry_storm = True
                    node.latency_ms = node.base_latency_ms * rm["storm_latency_amplifier"]
            else:
                node.in_retry_storm = False
                node.latency_ms = node.base_latency_ms

    def _inject_failure(self):
        """Phase 2: inject latency spike at target node."""
        fi = self.cfg["failure_injection"]
        target = self.nodes.get(fi["target_node_id"])
        if target and not target.failed:
            target.latency_ms = target.base_latency_ms * fi["latency_multiplier"]

    def _fragment_nodes(self):
        """Phase 3: nodes exceeding fragmentation threshold become inactive.
        Fragmentation can spread to topological neighbors of the failed node."""
        ft = self.cfg["failure_injection"]["fragmentation_threshold_ms"]
        sf = self.cfg["_fragmentation"]["spreading_factor"]
        min_frag = self.cfg["_fragmentation"]["min_nodes_frag"]

        # Primary fragmentation — direct threshold breach
        direct_frag = [n for n in self.nodes.values() if n.latency_ms > ft and not n.failed]
        for node in direct_frag:
            node.failed = True
            node.fragmented = True

        # If primary is too few, also frag neighbor nodes of the most-affected one
        target_id = self.cfg["failure_injection"]["target_node_id"]
        target = self.nodes[target_id]
        neighbors = [self.nodes[nid] for nid in self.topology.get(target_id, [])]
        for nb in neighbors:
            if not nb.failed and self._rng.random() < sf:
                nb.failed = True
                nb.fragmented = True
                nb.latency_ms = max(nb.latency_ms, ft * 2)

        # Enforce minimum fragmentation (catch edge case of low multiplier)
        if len([n for n in self.nodes.values() if n.fragmented]) < min_frag:
            for nid in [nid for nid in self.topology.get(target_id, [])[:min_frag]]:
                nd = self.nodes[nid]
                if not nd.fragmented:
                    nd.failed = True
                    nd.fragmented = True
                    nd.latency_ms = max(nd.latency_ms, ft * 2)

    def _activate_recovery(self):
        """Phase 4: load shedding + rate limiting + traffic decay."""
        rc = self.cfg["recovery"]
        # Load shedding: drop a fraction of queue
        if rc["load_shedding_active"]:
            for node in self.nodes.values():
                shed = int(node.queue_depth * rc["load_shed_fraction"])
                node.queue_depth = max(0, node.queue_depth - shed)

        # Rate limiting: cap new arrivals
        tokens = rc["rate_limit_tokens_per_tick"]

        # Traffic decay: exponential half-life
        half_life = rc["traffic_decay_half_life_ticks"]
        decay_factor = 0.5 ** (1.0 / half_life)
        self.traffic_rate *= decay_factor
        self.traffic_rate = max(2, self.traffic_rate)

    def _resolve_recovery(self) -> float:
        """
        Phase 5: determine recovery outcome using the 6-class resilience taxonomy.
        NOT guaranteed success — outcome depends on sustained contention.
        """
        from .resilience_taxonomy import (
            OutcomeClassifier, PhaseDetector,
            StabilitySignatures, ClassificationEvidence,
        )

        fragmented = sum(1 for n in self.nodes.values() if n.fragmented)
        total = len(self.nodes)
        recovered = sum(1 for n in self.nodes.values()
                        if not n.fragmented and not n.failed)

        # ── Retry-storm oscillation penalty ──────────────────────────────────
        # retry_volume is driven by retry storms during FRAGMENTATION phase.
        # When it exceeds capacity threshold, oscillations become sustained.
        rm = self.cfg["retry_storm_model"]
        osc_prob = rm.get("oscillation_probability", 0.0)
        storm_capacity = self.cfg["node_defaults"]["base_capacity"] \
                        * rm["retry_threshold_multiplier"]

        osc_penalty = 0.0
        if self.retry_volume > storm_capacity:
            # Sustained storm: apply oscillating penalty driven by retry volume
            # Higher retry volume → larger oscillation amplitude
            excess_ratio = min(1.0, (self.retry_volume - storm_capacity) / storm_capacity)
            osc_penalty = 0.15 + (osc_prob * 0.25 * excess_ratio)
        elif self.retry_volume > storm_capacity * 0.5:
            # Light storm: probabilistic small penalty
            if self._rng.random() < osc_prob:
                osc_penalty = 0.05 + osc_prob * 0.10

        base_stability = recovered / total
        stability = base_stability - osc_penalty
        stability = max(0.0, min(1.0, stability))

        # ── Build a minimal metrics history from current state ──────────────
        # We use the last N ticks of actual history for proper classification.
        history = self.metrics_history[-200:] if len(self.metrics_history) > 200 else self.metrics_history

        # Run the full taxonomy classifier on actual telemetry history
        classifier = OutcomeClassifier()
        outcome, evidence = classifier.classify(history, self.cfg)

        self.recovery_outcome = outcome
        self._last_evidence = evidence   # store for export
        return stability

    def _compute_global_health(self) -> float:
        """Composite health score across all pressure dimensions."""
        pressures = [n.queue_pressure() for n in self.nodes.values()]
        q_pressure = statistics.mean(pressures) if pressures else 0.0

        fragmented = sum(1 for n in self.nodes.values() if n.fragmented)
        f_pressure = fragmented / len(self.nodes) if self.nodes else 0.0

        lats = [n.latency_ms for n in self.nodes.values()]
        peak = max(lats) if lats else 1.0
        base = self.cfg["node_defaults"]["base_latency_ms"]
        l_pressure = min(1.0, (peak / (base * 4.0)))

        storm_pen = min(1.0, self.retry_volume /
                        (self.cfg["retry_storm_model"]["retry_threshold_multiplier"]
                         * self.cfg["node_defaults"]["base_capacity"]))

        health = 1.0 - (q_pressure * 0.3) - (f_pressure * 0.35) \
                 - (l_pressure * 0.25) - (storm_pen * 0.10)
        return max(0.0, min(1.0, health))

    # ── Main tick ─────────────────────────────────────────────────────────────

    def tick_step(self):
        """Advance one simulation tick. All state is math-derived."""
        self.tick += 1
        self.time_ms = self.tick * self.tick_interval

        # Phase detection
        fi = self.cfg["failure_injection"]
        recovery_tick = fi["recovery_tick"]
        frag_threshold = fi["fragmentation_threshold_ms"]

        if self.tick < fi["failure_tick"]:
            self.phase = "STABLE"
        elif self.tick < fi["failure_tick"] + 40:
            self.phase = "FAILURE_INJECTION"
        elif self.tick < recovery_tick:
            self.phase = "FRAGMENTATION"
        elif self.tick < recovery_tick + 80:
            self.phase = "RECOVERY_INITIATION"
        else:
            self.phase = "RECOVERY_OUTCOME"

        # ── Per-phase state updates ──
        if self.phase == "FAILURE_INJECTION":
            self._inject_failure()
        elif self.phase == "FRAGMENTATION":
            self._inject_failure()
            self._fragment_nodes()
            # Retry amplification
            self.retry_volume = sum(
                int(n.queue_depth * 0.4) for n in self.nodes.values() if n.fragmented
            )
            self._detect_retry_storms()
            # Traffic reroutes increase edge heat
            for node in self.nodes.values():
                if node.fragmented:
                    node.edge_heat = 1.0
            self._propagate_heat()
        elif self.phase == "RECOVERY_INITIATION":
            self._activate_recovery()
            self._decay_heat(0.88)
            self._propagate_heat()
            self.retry_volume = max(0, int(self.retry_volume * 0.7))
            self._detect_retry_storms()
            # Resolve fragmented nodes that have recovered
            for node in self.nodes.values():
                if node.fragmented and node.latency_ms < frag_threshold:
                    if self._rng.random() < self.cfg["recovery"]["recovery_rate"]:
                        node.fragmented = False
                        node.latency_ms = node.base_latency_ms
                        node.queue_depth = int(node.base_capacity * 0.5)
        elif self.phase == "RECOVERY_OUTCOME":
            self._activate_recovery()
            self._decay_heat(0.92)
            self._propagate_heat()
            self.retry_volume = max(0, int(self.retry_volume * 0.5))
            self._detect_retry_storms()

        # ── Traffic injection (healthy nodes only) ──
        rate = int(self.traffic_rate)
        for node in self.nodes.values():
            if node.fragmented or node.failed:
                continue
            arrivals = self._rng.randint(max(1, rate - 3), rate + 3)
            node.enqueue(arrivals)
            node.success_requests += arrivals
            node.step_drain()

        # ── Failed nodes generate retries ──
        for node in self.nodes.values():
            if node.fragmented:
                node.failed_requests += self._rng.randint(0, 3)
                if self._rng.random() < 0.05:
                    node.retry_count += 1
                    self.retry_volume += 4

        # ── Global state ──
        # Always call _resolve_recovery at end of run to guarantee outcome is set.
        # Under light-network conditions the sim may not reach RECOVERY_OUTCOME
        # phase, so we call it unconditionally on the final tick.
        if self.phase == "RECOVERY_OUTCOME" or self.tick >= self.total_ticks - 1:
            stability = self._resolve_recovery()
        else:
            fragmented = sum(1 for n in self.nodes.values() if n.fragmented)
            recovered = sum(1 for n in self.nodes.values()
                            if not n.fragmented and not n.failed)
            # Same oscillation penalty logic for non-RECOVERY_OUTCOME ticks
            rm = self.cfg["retry_storm_model"]
            osc_prob = rm.get("oscillation_probability", 0.0)
            storm_capacity = self.cfg["node_defaults"]["base_capacity"] \
                            * rm["retry_threshold_multiplier"]
            if self.retry_volume > storm_capacity:
                excess_ratio = min(1.0, (self.retry_volume - storm_capacity) / storm_capacity)
                osc_pen = 0.15 + (osc_prob * 0.25 * excess_ratio)
            elif self.retry_volume > storm_capacity * 0.5:
                osc_pen = (0.05 + osc_prob * 0.10) if self._rng.random() < osc_prob else 0.0
            else:
                osc_pen = 0.0
            stability = max(0.0, (recovered / len(self.nodes)) - osc_pen)

        # If outcome is still None (never reached RECOVERY_OUTCOME phase),
        # force-classify from current state using the taxonomy classifier.
        if self.recovery_outcome is None:
            from .resilience_taxonomy import OutcomeClassifier
            history = self.metrics_history
            classifier = OutcomeClassifier()
            outcome, evidence = classifier.classify(history, self.cfg)
            self.recovery_outcome = outcome
            self._last_evidence = evidence

        global_health = self._compute_global_health()

        lats = self._generate_latencies()
        p50, p95 = self._compute_percentiles(lats)

        edge_heats = list(self.edges.values())
        edge_avg = statistics.mean(edge_heats) if edge_heats else 0.0
        edge_peak = max(edge_heats) if edge_heats else 0.0

        fragmented_n = sum(1 for n in self.nodes.values() if n.fragmented)
        total_queue = sum(n.queue_depth for n in self.nodes.values())
        total_req = sum(n.total_requests() for n in self.nodes.values())
        failed_req = sum(n.failed_requests for n in self.nodes.values())

        # ── Record metrics ──
        m = SimulationMetrics(
            tick=self.tick,
            time_ms=self.time_ms,
            p50_latency=round(p50, 3),
            p95_latency=round(p95, 3),
            queue_depth=total_queue,
            retry_count=self.retry_volume,
            fragmented_nodes=fragmented_n,
            stability_score=round(stability, 4),
            global_health_score=round(global_health, 4),
            edge_heat_avg=round(edge_avg, 4),
            edge_heat_peak=round(edge_peak, 4),
            active_nodes=len(self.nodes) - fragmented_n,
            total_requests=total_req,
            failed_requests=failed_req,
            recovery_outcome=self.recovery_outcome or "unknown",
        )
        self.metrics_history.append(m)

    # ── Run simulation ───────────────────────────────────────────────────────

    def run(self) -> List[SimulationMetrics]:
        """Execute full simulation. Returns list of all tick metrics."""
        for _ in range(self.total_ticks):
            self.tick_step()
        return self.metrics_history

    # ── CSV export ────────────────────────────────────────────────────────────

    def export_telemetry_csv(self, path: str):
        """Export full tick-by-tick telemetry. Viz reads this file directly."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        cols = [
            "tick", "time_ms", "p50_latency", "p95_latency", "queue_depth",
            "retry_count", "fragmented_nodes", "stability_score",
            "global_health_score", "edge_heat_avg", "edge_heat_peak",
            "active_nodes", "total_requests", "failed_requests", "recovery_outcome"
        ]
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            for m in self.metrics_history:
                writer.writerow({
                    "tick": m.tick,
                    "time_ms": m.time_ms,
                    "p50_latency": m.p50_latency,
                    "p95_latency": m.p95_latency,
                    "queue_depth": m.queue_depth,
                    "retry_count": m.retry_count,
                    "fragmented_nodes": m.fragmented_nodes,
                    "stability_score": m.stability_score,
                    "global_health_score": m.global_health_score,
                    "edge_heat_avg": m.edge_heat_avg,
                    "edge_heat_peak": m.edge_heat_peak,
                    "active_nodes": m.active_nodes,
                    "total_requests": m.total_requests,
                    "failed_requests": m.failed_requests,
                    "recovery_outcome": m.recovery_outcome,
                })

    # ── JSON summary export ──────────────────────────────────────────────────

    def export_summary_json(self, path: str):
        """One-shot summary for experiment record with 6-class taxonomy."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        history = self.metrics_history
        if not history:
            return

        from .resilience_taxonomy import PhaseDetector

        peak_p95 = max(m.p95_latency for m in history)
        peak_queue = max(m.queue_depth for m in history)
        peak_retries = max(m.retry_count for m in history)
        final_health = history[-1].global_health_score
        final_stability = history[-1].stability_score

        # Find fragmentation duration
        frag_start = next(
            (m.tick for m in history if m.fragmented_nodes > 0), None
        )
        frag_end = None
        for m in reversed(history):
            if m.fragmented_nodes > 0:
                frag_end = m.tick
                break
        frag_duration = (frag_end - frag_start) * self.tick_interval \
            if frag_start and frag_end else 0

        # ── Phase transition detection ────────────────────────────────────
        detector = PhaseDetector(self.cfg)
        phases = detector.detect_transitions(history)

        # Find unique phase sequence (condensed transitions only)
        transition_events = detector.transitions
        phase_sequence = [t.to_phase for t in transition_events]
        if phases and phase_sequence:
            phase_sequence = [phases[0]] + phase_sequence
        elif phases:
            phase_sequence = [phases[0]]

        # Phase dwell times
        phase_dwell = {p: phases.count(p) for p in set(phases)} if phases else {}

        # ── Stability signature from evidence ──────────────────────────────
        evidence_data = {}
        sig_data = {}
        if self._last_evidence is not None:
            ev = self._last_evidence
            sig = ev.stability_signature
            evidence_data = {
                "primary_signal": ev.primary_signal,
                "secondary_signals": ev.secondary_signals,
                "violating_thresholds": ev.violating_thresholds,
            }
            sig_data = {
                "initial_stability": round(sig.initial_stability, 4),
                "final_stability": round(sig.final_stability, 4),
                "min_stability": round(sig.min_stability, 4),
                "stability_trend": round(sig.stability_trend, 4),
                "oscillation_count": sig.oscillation_count,
                "oscillation_amplitude": round(sig.oscillation_amplitude, 4),
                "fragmentation_peak": sig.fragmentation_peak,
                "fragmentation_duration_ticks": sig.fragmentation_duration_ticks,
                "fragmentation_persistence": round(sig.fragmentation_persistence, 4),
                "retry_peak": sig.retry_peak,
                "retry_storm_duration_ticks": sig.retry_storm_duration_ticks,
                "recovery_convergence_slope": round(sig.recovery_convergence_slope, 4),
                "collapse_velocity": round(sig.collapse_velocity, 4),
                "secondary_failure_tick": sig.secondary_failure_tick,
            }

        summary = {
            "simulation_name": self.cfg["simulation_name"],
            "config_file": "configs/recovery_test.json",
            "total_ticks": self.total_ticks,
            "total_walltime_ms": round(history[-1].time_ms, 1),
            "tick_interval_ms": self.tick_interval,
            "topology": self.cfg["topology"]["type"],
            "node_count": self.cfg["topology"]["nodes"],
            "failure_injection_tick": self.cfg["failure_injection"]["failure_tick"],
            "recovery_initiated_tick": self.cfg["failure_injection"]["recovery_tick"],
            # 6-class taxonomy outcome
            "recovery_outcome": history[-1].recovery_outcome,
            "outcome_severity": {
                "full_recovery": 0, "partial_recovery": 1,
                "oscillatory_instability": 2, "secondary_collapse": 3,
                "cascading_fragmentation": 4, "unrecoverable_partition": 5,
            }.get(history[-1].recovery_outcome, -1),
            "final_health_score": final_health,
            "final_stability_score": final_stability,
            "peak_p95_latency_ms": peak_p95,
            "peak_queue_depth": peak_queue,
            "peak_retry_count": peak_retries,
            "fragmentation_duration_ms": round(frag_duration, 1),
            "total_requests": history[-1].total_requests,
            "total_failed_requests": history[-1].failed_requests,
            "final_active_nodes": history[-1].active_nodes,
            "final_fragmented_nodes": history[-1].fragmented_nodes,
            "recovery_phases": {
                "stable_until_tick": self.cfg["failure_injection"]["failure_tick"],
                "failure_injection_ticks": "40",
                "fragmentation_ticks": str(
                    self.cfg["failure_injection"]["recovery_tick"]
                    - self.cfg["failure_injection"]["failure_tick"] - 40
                ),
                "recovery_initiation_ticks": "80",
                "outcome_ticks": str(
                    self.total_ticks - self.cfg["failure_injection"]["recovery_tick"] - 80
                )
            },
            # Phase transition analysis
            "phase_sequence": phase_sequence,
            "phase_dwell_ticks": {p: int(t) for p, t in phase_dwell.items()},
            "transition_count": len(transition_events),
            "transitions": [
                {
                    "from": t.from_phase,
                    "to": t.to_phase,
                    "tick": t.tick,
                    "trigger": t.trigger,
                }
                for t in transition_events
            ],
            # Stability signature
            "stability_signature": sig_data,
            "classification_evidence": evidence_data,
        }
        with open(path, "w") as f:
            json.dump(summary, f, indent=2)

        # Export phase transitions CSV
        transitions_csv_path = path.replace("recovery_summary.json", "phase_transitions.csv")
        detector.export_transitions_csv(phases, transitions_csv_path, history)

    # ── Latency distribution export ──────────────────────────────────────────

    def export_latency_distribution_csv(self, path: str):
        """Latency distribution snapshots at key phases."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        snapshots = [
            ("STABLE_t=100", 100),
            ("FAILURE_t=130", 130),
            ("FRAGMENTATION_t=200", 200),
            ("RECOVERY_t=320", 320),
            ("OUTCOME_t=580", 580),
        ]
        rows = []
        for label, tick in snapshots:
            m = next((x for x in self.metrics_history if x.tick == tick), None)
            if m:
                rows.append({
                    "phase_label": label,
                    "tick": m.tick,
                    "p50_latency": m.p50_latency,
                    "p95_latency": m.p95_latency,
                    "global_health_score": m.global_health_score,
                    "fragmented_nodes": m.fragmented_nodes,
                    "retry_count": m.retry_count,
                })
        if rows:
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=list(rows[0].keys())
                )
                writer.writeheader()
                writer.writerows(rows)

    # ── Retry storm log ──────────────────────────────────────────────────────

    def export_retry_storm_csv(self, path: str):
        """Rows where retry_count exceeded threshold."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        threshold = (
            self.cfg["node_defaults"]["base_capacity"]
            * self.cfg["retry_storm_model"]["retry_threshold_multiplier"]
        )
        storm_rows = [
            {
                "tick": m.tick,
                "time_ms": m.time_ms,
                "retry_count": m.retry_count,
                "p95_latency": m.p95_latency,
                "queue_depth": m.queue_depth,
                "fragmented_nodes": m.fragmented_nodes,
                "global_health_score": m.global_health_score,
            }
            for m in self.metrics_history
            if m.retry_count > threshold
        ]
        if storm_rows:
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=list(storm_rows[0].keys())
                )
                writer.writeheader()
                writer.writerows(storm_rows)

    # ── Contention decay export ───────────────────────────────────────────────

    def export_contention_decay_csv(self, path: str):
        """Edge heat avg over time — shows congestion clearing."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rows = [
            {
                "tick": m.tick,
                "time_ms": m.time_ms,
                "edge_heat_avg": m.edge_heat_avg,
                "edge_heat_peak": m.edge_heat_peak,
                "queue_depth": m.queue_depth,
                "global_health_score": m.global_health_score,
                "recovery_outcome": m.recovery_outcome,
            }
            for m in self.metrics_history
        ]
        if rows:
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=list(rows[0].keys())
                )
                writer.writeheader()
                writer.writerows(rows)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="The Cascade v2 — Recovery Dynamics Engine"
    )
    parser.add_argument(
        "--config", default="configs/recovery_test.json",
        help="Path to JSON configuration file"
    )
    parser.add_argument(
        "--output-dir", default="metrics",
        help="Directory for telemetry exports"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    os.makedirs(args.output_dir, exist_ok=True)

    engine = RecoveryEngine(cfg)
    print(f"[RecoveryEngine] Starting simulation: {cfg['simulation_name']}")
    print(f"[RecoveryEngine] Ticks: {cfg['telemetry']['total_ticks']}, "
          f"Interval: {cfg['telemetry']['tick_interval_ms']}ms, "
          f"Seed: {cfg['topology']['seed']}")

    metrics = engine.run()

    print(f"[RecoveryEngine] Simulation complete. Outcome: {metrics[-1].recovery_outcome}")

    # Export all telemetry files
    telemetry_path = f"{args.output_dir}/telemetry.csv"
    engine.export_telemetry_csv(telemetry_path)
    print(f"[RecoveryEngine] Telemetry CSV: {telemetry_path}")

    summary_path = f"{args.output_dir}/recovery_summary.json"
    engine.export_summary_json(summary_path)
    print(f"[RecoveryEngine] Summary JSON: {summary_path}")

    latdist_path = f"{args.output_dir}/latency_distribution.csv"
    engine.export_latency_distribution_csv(latdist_path)
    print(f"[RecoveryEngine] Latency distribution: {latdist_path}")

    retry_path = f"{args.output_dir}/retry_storms.csv"
    engine.export_retry_storm_csv(retry_path)
    print(f"[RecoveryEngine] Retry storm log: {retry_path}")

    contention_path = f"{args.output_dir}/contention_decay.csv"
    engine.export_contention_decay_csv(contention_path)
    print(f"[RecoveryEngine] Contention decay: {contention_path}")

    print("[RecoveryEngine] All exports complete.")


if __name__ == "__main__":
    main()
