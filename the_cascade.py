"""
THE CASCADE — Logistics AI Coordination Failure Visualization
PHASE 1: Proof Render (1080p / 24fps / Stable)

Narrative arc:
  SCENE 1 — Stable coordination network (6s)
  SCENE 2 — Latency spike at node_07 (5s)
  SCENE 3 — Reroute explosion across agents (5s)
  SCENE 4 — Network fragmentation cascade (6s)
  SCENE 5 — Final message reveal (5s)

Optimization targets:
  - 12 nodes, 25 agents, 24 edges
  - No bloom, no glow, no motion blur
  - Static background plane
  - Precomputed paths
  - Separate scene validation before merge
"""

from manim import *
import random, math

# ── Palette (dark cinematic) ────────────────────────────────────────────────
BG        = "#0a0a0f"
CYAN      = "#00d4ff"
DIM_CYAN  = "#1a3a4a"
AMBER     = "#ff9500"
RED       = "#ef4444"
RED_DEEP  = "#991b1b"
WHITE     = "#f8fafc"
GRAY      = "#4a5568"
GRAY_DIM  = "#1e2530"


def lmap(value, min_src, max_src, min_dst, max_dst):
    """Linear map from source range to destination range."""
    return min_dst + (max_dst - min_dst) * (value - min_src) / (max_src - min_src)


class Node(VGroup):
    """Single network node with pulse capability."""

    def __init__(self, index, position, radius=0.18, **kwargs):
        super().__init__(**kwargs)
        self.index = index
        self.pos = position
        self.radius = radius
        self.failed = False
        self.latency_state = "normal"  # normal | spike | failed

        # Outer glow ring (low opacity for proof render)
        outer = Circle(radius * 1.6, fill_color=CYAN, fill_opacity=0.0,
                       stroke_color=CYAN, stroke_opacity=0.0)
        # Core dot
        core = Circle(radius, fill_color=CYAN, fill_opacity=0.9,
                      stroke_color=CYAN, stroke_opacity=0.5)
        # Label
        label = Text(f"N{index:02d}", font="Ubuntu Mono", font_size=8,
                     fill_color=WHITE, fill_opacity=0.7)

        self.outer = outer
        self.core = core
        self.label = label
        self.label.move_to(position + [0, -(radius + 0.12), 0])

        self.add(outer, core, label)

    def set_state(self, state):
        """Update visual state: normal | spike | failed."""
        self.latency_state = state
        if state == "normal":
            self.core.set_fill(CYAN, opacity=0.9)
            self.core.set_stroke(CYAN, opacity=0.5)
            self.outer.set_fill(CYAN, opacity=0.0)
            self.outer.set_stroke(CYAN, opacity=0.0)
            self.label.set_fill_opacity(0.7)
            self.failed = False
        elif state == "spike":
            self.core.set_fill(AMBER, opacity=1.0)
            self.core.set_stroke(AMBER, opacity=0.8)
            self.outer.set_fill(AMBER, opacity=0.0)
            self.outer.set_stroke(AMBER, opacity=0.6)
            self.failed = False
        elif state == "failed":
            self.core.set_fill(GRAY_DIM, opacity=0.6)
            self.core.set_stroke(GRAY, opacity=0.3)
            self.outer.set_fill(GRAY_DIM, opacity=0.0)
            self.outer.set_stroke(GRAY_DIM, opacity=0.0)
            self.label.set_fill_opacity(0.25)
            self.failed = True

    def pulse(self):
        """Single pulse animation for spike event."""
        self.outer.set_stroke(AMBER, opacity=0.9)
        self.play(
            self.outer.animate.set_fill(AMBER, opacity=0.15).scale(1.5),
            run_time=0.4, rate_func=rush_into
        )
        self.play(
            self.outer.animate.set_fill(AMBER, opacity=0.0).scale(1.0),
            run_time=0.4, rate_func=slow_into
        )


class Agent:
    """Agent dot that traverses the network edge-by-edge."""

    def __init__(self, start_node, color=CYAN, radius=0.045):
        self.color = color
        self.radius = radius
        self.dot = Dot(radius=radius, fill_color=color, fill_opacity=0.85,
                       stroke_color=color, stroke_opacity=0.4)
        self.current_node = start_node
        self.target_node = None
        self.progress = 0.0  # 0 to 1 along edge
        self.speed = random.uniform(0.6, 1.2)
        self.failed = False
        self.stuck_timer = 0

    def move_to(self, node, network):
        """Set target node and calculate path."""
        self.current_node = node
        path = network.find_path(node, self.target_node) if self.target_node else []
        if path:
            self.path = path
            self.path_index = 0
            self.progress = 0.0


class Network:
    """Force-directed-ish network graph. Precomputes edge centers."""

    def __init__(self, node_positions, edges, congestion_levels=None):
        self.nodes = {}
        self.edges = {}  # (i,j) sorted tuple -> Line
        self.adjacency = {i: [] for i in node_positions}
        self.congestion = congestion_levels or {}
        self.edge_heat = {}  # (i,j) -> heat 0-1

        for idx, pos in node_positions.items():
            self.nodes[idx] = pos

        for i, j in edges:
            key = (min(i, j), max(i, j))
            p1 = node_positions[i]
            p2 = node_positions[j]
            self.adjacency[i].append((j, key))
            self.adjacency[j].append((i, key))
            self.edge_heat[key] = 0.0

        # Precompute edge center points for agent placement
        self.edge_centers = {}
        for i, j in edges:
            key = (min(i, j), max(i, j))
            p1 = node_positions[i]
            p2 = node_positions[j]
            self.edge_centers[key] = (p1 + p2) / 2

    def find_path(self, from_node, to_node):
        """BFS path finding. Returns list of node indices."""
        if from_node == to_node:
            return [from_node]
        queue = [(from_node, [from_node])]
        visited = {from_node}
        while queue:
            node, path = queue.pop(0)
            for neighbor, _ in self.adjacency[node]:
                if neighbor == to_node:
                    return path + [to_node]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))
        return [from_node]

    def get_edge_heat_color(self, key):
        """Return color based on congestion heat."""
        h = self.edge_heat.get(key, 0)
        if h < 0.3:
            return DIM_CYAN
        elif h < 0.6:
            return AMBER
        elif h < 0.85:
            return RED
        else:
            return RED_DEEP

    def get_agent_position(self, from_node, to_node, progress):
        """Interpolate position along edge."""
        p1 = self.nodes[from_node]
        p2 = self.nodes[to_node]
        return p1 + (p2 - p1) * progress


# ─────────────────────────────────────────────────────────────────────────────
# SCENE 1: STABLE COORDINATION NETWORK
# ─────────────────────────────────────────────────────────────────────────────
class Scene1_StableNetwork(Scene):
    """Wide establishing shot. Network pulses calmly. Agents flow along edges."""

    def construct(self):
        self.camera.background_color = BG

        # ── Node layout (12 nodes, somewhat distributed) ──────────────────
        node_positions = {
            0:  np.array([-4.0,  2.0, 0]),
            1:  np.array([-1.8,  3.2, 0]),
            2:  np.array([ 0.5,  2.8, 0]),
            3:  np.array([ 3.2,  2.0, 0]),
            4:  np.array([ 4.5,  0.5, 0]),
            5:  np.array([ 3.5, -1.8, 0]),
            6:  np.array([ 1.0, -2.5, 0]),
            7:  np.array([-1.5, -2.2, 0]),   # <-- failure node
            8:  np.array([-3.5, -1.0, 0]),
            9:  np.array([-4.5,  0.5, 0]),
            10: np.array([ 0.2,  0.0, 0]),
            11: np.array([ 2.0,  0.8, 0]),
        }

        edges = [
            (0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,9),(9,0),
            (0,10),(1,10),(2,10),(3,11),(4,11),(5,11),(6,10),(7,10),(8,10),(9,10),
            (10,11),(2,11),(3,5),(1,8)
        ]

        network = Network(node_positions, edges)
        nodes_group = VGroup()
        node_objs = {}

        for idx, pos in node_positions.items():
            n = Node(idx, pos)
            n.set_state("normal")
            node_objs[idx] = n
            nodes_group.add(n)

        # ── Edges (static lines for proof render) ─────────────────────────
        edges_group = VGroup()
        edge_lines = {}
        for i, j in edges:
            key = (min(i, j), max(i, j))
            p1 = node_positions[i]
            p2 = node_positions[j]
            line = Line(p1, p2, stroke_color=DIM_CYAN, stroke_opacity=0.6, stroke_width=1.2)
            edge_lines[key] = line
            edges_group.add(line)

        # ── Ambient glow behind edges (skip for proof — too expensive) ────
        # edge_glow = VGroup(*[
        #     Line(p1, p2, stroke_color=CYAN, stroke_opacity=0.08, stroke_width=4)
        #     for i, j in edges for p1, p2 in [(node_positions[i], node_positions[j])]
        # ])

        # ── Status bar (top) ───────────────────────────────────────────────
        status = Text("COORDINATION NETWORK // STABLE", font="Ubuntu Mono",
                      font_size=11, fill_color=CYAN, fill_opacity=0.6)
        status.to_edge(UP).shift(DOWN * 0.25)
        status_line = Line(LEFT * 7, RIGHT * 7, stroke_color=DIM_CYAN, stroke_opacity=0.3)
        status_line.next_to(status, DOWN, buff=0.08)
        self.add(status, status_line)

        # ── Agents (25 dots, staggered starts) ────────────────────────────
        NUM_AGENTS = 25
        agents = []
        agent_dots = VGroup()

        for i in range(NUM_AGENTS):
            start_node = random.choice(list(node_positions.keys()))
            agent = Agent(start_node)
            agent.dot.move_to(node_positions[start_node])
            agents.append(agent)
            agent_dots.add(agent.dot)

        # ── Compose scene ──────────────────────────────────────────────────
        self.add(edges_group, nodes_group, agent_dots)

        # ── Slow camera drift (proof: minimal) ─────────────────────────────
        # No camera animation in proof render — static frame

        # ── Agent movement updater (simplified: random hop) ───────────────
        def update_agents(mob, dt):
            for agent in agents:
                if agent.failed:
                    continue
                if agent.target_node is None:
                    # Pick random connected neighbor
                    neighbors = [n for n, _ in network.adjacency[agent.current_node]]
                    if neighbors:
                        agent.target_node = random.choice(neighbors)
                        agent.progress = 0.0
                        agent.speed = random.uniform(0.4, 0.8)
                else:
                    # Move along edge
                    agent.progress += dt * agent.speed
                    if agent.progress >= 1.0:
                        # Reached target
                        agent.current_node = agent.target_node
                        agent.target_node = None
                        agent.progress = 0.0
                    else:
                        # Update dot position
                        p = network.get_agent_position(
                            agent.current_node, agent.target_node, agent.progress
                        )
                        agent.dot.move_to(p)

        agent_updater = Mobject().add_updater(update_agents)
        self.add(agent_updater)

        # ── Gentle node pulse (proof: just slow opacity cycle) ────────────
        def pulse_node(mob, dt):
            for idx, node in node_objs.items():
                if node.latency_state == "normal" and not node.failed:
                    t = (self.time * 0.5 + idx * 0.4) % (2 * math.pi)
                    op = 0.85 + 0.15 * math.sin(t)
                    node.core.set_fill(CYAN, opacity=op)

        # Subtle updater for gentle pulse — proof render safe
        def gentle_pulse(mob, dt):
            pass  # Skip in proof render to save CPU

        self.wait(6)


# ─────────────────────────────────────────────────────────────────────────────
# SCENE 2: LATENCY SPIKE AT NODE_07
# ─────────────────────────────────────────────────────────────────────────────
class Scene2_LatencySpike(Scene):
    """Node_07 turns amber. Concentric pulse rings. System text alert."""

    def construct(self):
        self.camera.background_color = BG

        # ── Network recreation (simple static version for proof) ───────────
        node_positions = {
            0:  np.array([-4.0,  2.0, 0]),
            1:  np.array([-1.8,  3.2, 0]),
            2:  np.array([ 0.5,  2.8, 0]),
            3:  np.array([ 3.2,  2.0, 0]),
            4:  np.array([ 4.5,  0.5, 0]),
            5:  np.array([ 3.5, -1.8, 0]),
            6:  np.array([ 1.0, -2.5, 0]),
            7:  np.array([-1.5, -2.2, 0]),   # FAILURE NODE
            8:  np.array([-3.5, -1.0, 0]),
            9:  np.array([-4.5,  0.5, 0]),
            10: np.array([ 0.2,  0.0, 0]),
            11: np.array([ 2.0,  0.8, 0]),
        }

        edges = [
            (0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,9),(9,0),
            (0,10),(1,10),(2,10),(3,11),(4,11),(5,11),(6,10),(7,10),(8,10),(9,10),
            (10,11),(2,11),(3,5),(1,8)
        ]

        # Build edges
        edges_group = VGroup()
        for i, j in edges:
            p1 = node_positions[i]
            p2 = node_positions[j]
            line = Line(p1, p2, stroke_color=DIM_CYAN, stroke_opacity=0.6, stroke_width=1.2)
            edges_group.add(line)

        # Build nodes
        nodes_group = VGroup()
        node_objs = {}
        for idx, pos in node_positions.items():
            n = Node(idx, pos)
            n.set_state("normal")
            node_objs[idx] = n
            nodes_group.add(n)

        self.add(edges_group, nodes_group)

        # ── Status bar ─────────────────────────────────────────────────────
        status = Text("NODE_07 // LATENCY SPIKE // +340ms",
                      font="Ubuntu Mono", font_size=11, fill_color=AMBER, fill_opacity=0.9)
        status.to_edge(UP).shift(DOWN * 0.25)
        status_line = Line(LEFT * 7, RIGHT * 7, stroke_color=AMBER, stroke_opacity=0.4)
        status_line.next_to(status, DOWN, buff=0.08)
        self.add(status, status_line)

        # ── Spike animation: node_07 turns amber + pulse ──────────────────
        node7 = node_objs[7]
        node7.set_state("spike")

        # Pulse rings (proof: simple expanding circles)
        failure_pos = node_positions[7]
        rings = VGroup()
        for r_idx in range(3):
            ring = Circle(radius=0.25 + r_idx * 0.4,
                          fill_opacity=0, stroke_color=AMBER, stroke_opacity=0.0)
            ring.move_to(failure_pos)
            rings.add(ring)

        self.add(rings)

        # Staggered ring expansion
        for ring in rings:
            self.play(
                ring.animate.set_stroke(AMBER, opacity=0.7).scale(4.0),
                run_time=1.2, rate_func=slow_into
            )
            self.play(ring.animate.set_stroke(AMBER, opacity=0.0), run_time=0.3)

        # Agents slow down near node 7
        agents = []
        agent_dots = VGroup()
        NUM_AGENTS = 25
        network = Network(node_positions, edges)

        for i in range(NUM_AGENTS):
            start_node = random.choice(list(node_positions.keys()))
            agent = Agent(start_node, color=CYAN)
            agent.dot.move_to(node_positions[start_node])
            # Give some agents the reroute behavior
            if i % 4 == 0:
                agent.target_node = 7  # reroute toward failure
            agents.append(agent)
            agent_dots.add(agent.dot)

        def update_spike_agents(mob, dt):
            for agent in agents:
                if agent.failed:
                    continue
                # Slow down if heading toward node 7
                speed_mult = 0.3 if agent.target_node == 7 else 1.0
                if agent.target_node is None:
                    neighbors = [n for n, _ in network.adjacency[agent.current_node]]
                    if neighbors:
                        agent.target_node = random.choice(neighbors)
                        agent.progress = 0.0
                        agent.speed = random.uniform(0.3, 0.6)
                else:
                    agent.progress += dt * agent.speed * speed_mult
                    if agent.progress >= 1.0:
                        agent.current_node = agent.target_node
                        agent.target_node = None
                        agent.progress = 0.0
                        # Skip node 7 after hitting it once
                        if agent.current_node == 7:
                            agent.failed = True
                            agent.dot.set_fill(GRAY, opacity=0.3)
                    else:
                        p = network.get_agent_position(
                            agent.current_node, agent.target_node, agent.progress
                        )
                        agent.dot.move_to(p)

        updater = Mobject().add_updater(update_spike_agents)
        self.add(updater)

        # Amber edge coloring for edges connected to node 7
        connected_edges = [(7, n) for n, _ in network.adjacency[7]]
        for i, j in edges:
            key = (min(i, j), max(i, j))
            if key in [(min(7,n), max(7,n)) for n in [0,6,8,10]]:
                for line in edges_group:
                    if hasattr(line, 'start') and hasattr(line, 'end'):
                        pass  # skip complex check in proof
                # Simple: just highlight edges from node7
                idx_list = [idx for idx, ln in enumerate(edges_group) if False]  # placeholder
        self.add(agent_dots)
        self.wait(5)


# ─────────────────────────────────────────────────────────────────────────────
# SCENE 3: REROUTE EXPLOSION
# ─────────────────────────────────────────────────────────────────────────────
class Scene3_RerouteExplosion(Scene):
    """8 nodes receive simultaneous reroute. Agents speed up chaotically.
    Edge congestion turns orange → red."""

    def construct(self):
        self.camera.background_color = BG

        node_positions = {
            0:  np.array([-4.0,  2.0, 0]),
            1:  np.array([-1.8,  3.2, 0]),
            2:  np.array([ 0.5,  2.8, 0]),
            3:  np.array([ 3.2,  2.0, 0]),
            4:  np.array([ 4.5,  0.5, 0]),
            5:  np.array([ 3.5, -1.8, 0]),
            6:  np.array([ 1.0, -2.5, 0]),
            7:  np.array([-1.5, -2.2, 0]),
            8:  np.array([-3.5, -1.0, 0]),
            9:  np.array([-4.5,  0.5, 0]),
            10: np.array([ 0.2,  0.0, 0]),
            11: np.array([ 2.0,  0.8, 0]),
        }

        edges = [
            (0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,9),(9,0),
            (0,10),(1,10),(2,10),(3,11),(4,11),(5,11),(6,10),(7,10),(8,10),(9,10),
            (10,11),(2,11),(3,5),(1,8)
        ]

        network = Network(node_positions, edges)

        # Build edges with heat tracking
        edges_group = VGroup()
        edge_map = {}  # (i,j) -> line index in group
        for idx, (i, j) in enumerate(edges):
            key = (min(i, j), max(i, j))
            p1 = node_positions[i]
            p2 = node_positions[j]
            line = Line(p1, p2, stroke_color=DIM_CYAN, stroke_opacity=0.6, stroke_width=1.2)
            edges_group.add(line)
            edge_map[key] = idx

        # Nodes — node 7 is already failed (amber)
        nodes_group = VGroup()
        node_objs = {}
        for idx, pos in node_positions.items():
            n = Node(idx, pos)
            if idx == 7:
                n.set_state("spike")
            else:
                n.set_state("normal")
            node_objs[idx] = n
            nodes_group.add(n)

        # Status bar
        status = Text("REROUTE FLOOD // SIMULTANEOUS COORDINATION FAILURE",
                      font="Ubuntu Mono", font_size=11, fill_color=RED, fill_opacity=0.9)
        status.to_edge(UP).shift(DOWN * 0.25)
        status_line = Line(LEFT * 7, RIGHT * 7, stroke_color=RED, stroke_opacity=0.4)
        status_line.next_to(status, DOWN, buff=0.08)
        self.add(status, status_line)

        self.add(edges_group, nodes_group)

        # ── REROUTE TEXT BURST ─────────────────────────────────────────────
        reroute_label = Text("REROUTE", font="Ubuntu Mono", font_size=28,
                              fill_color=RED, fill_opacity=0.0, weight=BOLD)
        reroute_label.move_to(ORIGIN).shift(UP * 1.2)
        self.add(reroute_label)

        # Flash "REROUTE" text
        self.play(reroute_label.animate.set_fill(RED, opacity=1.0).scale(1.1),
                  run_time=0.3, rate_func=rush_into)
        self.wait(0.4)
        self.play(reroute_label.animate.set_fill(RED, opacity=0.0).scale(1.0),
                  run_time=0.5, rate_func=slow_into)

        # ── Agents — chaos reroute ─────────────────────────────────────────
        agents = []
        agent_dots = VGroup()
        NUM_AGENTS = 25

        for i in range(NUM_AGENTS):
            start_node = random.choice(list(node_positions.keys()))
            agent = Agent(start_node, color=CYAN)
            agent.dot.move_to(node_positions[start_node])
            # Random target (chaotic reroute)
            agent.target_node = random.choice(list(node_positions.keys()))
            agent.progress = 0.0
            agent.speed = random.uniform(1.5, 2.5)  # Fast!
            agents.append(agent)
            agent_dots.add(agent.dot)

        # Edge heat state
        edge_heats = {key: 0.0 for key in edge_map}

        def update_chaos_agents(mob, dt):
            # Increase heat on all edges (congestion)
            for key in edge_heats:
                edge_heats[key] = min(1.0, edge_heats[key] + dt * 0.4)

            # Update edge colors based on heat
            for key, heat in edge_heats.items():
                line_idx = edge_map.get(key)
                if line_idx is not None:
                    line = edges_group[line_idx]
                    if heat < 0.3:
                        line.set_stroke(DIM_CYAN, opacity=0.6)
                    elif heat < 0.6:
                        line.set_stroke(AMBER, opacity=0.8)
                    elif heat < 0.85:
                        line.set_stroke(RED, opacity=0.9)
                    else:
                        line.set_stroke(RED_DEEP, opacity=1.0)

            # Move agents
            for agent in agents:
                if agent.failed:
                    continue
                if agent.target_node is None:
                    neighbors = [n for n, _ in network.adjacency[agent.current_node]]
                    if neighbors:
                        agent.target_node = random.choice(neighbors)
                        agent.progress = 0.0
                        agent.speed = random.uniform(1.2, 2.8)
                else:
                    agent.progress += dt * agent.speed
                    if agent.progress >= 1.0:
                        agent.current_node = agent.target_node
                        agent.target_node = None
                        agent.progress = 0.0
                    else:
                        p = network.get_agent_position(
                            agent.current_node, agent.target_node, agent.progress
                        )
                        agent.dot.move_to(p)

        updater = Mobject().add_updater(update_chaos_agents)
        self.add(updater)
        self.add(agent_dots)
        self.wait(5)


# ─────────────────────────────────────────────────────────────────────────────
# SCENE 4: NETWORK FRAGMENTATION
# ─────────────────────────────────────────────────────────────────────────────
class Scene4_Fragmentation(Scene):
    """5 more nodes fail in sequence. Coordination collapses.
    Camera pulls back to show full fragmentation."""

    def construct(self):
        self.camera.background_color = BG

        node_positions = {
            0:  np.array([-4.0,  2.0, 0]),
            1:  np.array([-1.8,  3.2, 0]),
            2:  np.array([ 0.5,  2.8, 0]),
            3:  np.array([ 3.2,  2.0, 0]),
            4:  np.array([ 4.5,  0.5, 0]),
            5:  np.array([ 3.5, -1.8, 0]),
            6:  np.array([ 1.0, -2.5, 0]),
            7:  np.array([-1.5, -2.2, 0]),  # already failed
            8:  np.array([-3.5, -1.0, 0]),
            9:  np.array([-4.5,  0.5, 0]),
            10: np.array([ 0.2,  0.0, 0]),
            11: np.array([ 2.0,  0.8, 0]),
        }

        edges = [
            (0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,9),(9,0),
            (0,10),(1,10),(2,10),(3,11),(4,11),(5,11),(6,10),(7,10),(8,10),(9,10),
            (10,11),(2,11),(3,5),(1,8)
        ]

        # Status bar
        status = Text("COORDINATION LOSS", font="Ubuntu Mono", font_size=14,
                      fill_color=RED, fill_opacity=0.0, weight=BOLD)
        counter = Text("FAILED NODES: 1/12", font="Ubuntu Mono", font_size=11,
                       fill_color=GRAY, fill_opacity=0.7)
        status.to_edge(UP).shift(DOWN * 0.25)
        counter.next_to(status, DOWN, buff=0.05).align_to(status, LEFT)
        status_line = Line(LEFT * 7, RIGHT * 7, stroke_color=RED, stroke_opacity=0.2)
        status_line.next_to(status, DOWN, buff=0.08)
        self.add(status, counter, status_line)

        # Build network
        network = Network(node_positions, edges)

        edges_group = VGroup()
        edge_map = {}
        for idx, (i, j) in enumerate(edges):
            key = (min(i, j), max(i, j))
            p1 = node_positions[i]
            p2 = node_positions[j]
            line = Line(p1, p2, stroke_color=RED, stroke_opacity=0.15, stroke_width=1.0)
            edges_group.add(line)
            edge_map[key] = idx

        nodes_group = VGroup()
        node_objs = {}
        for idx, pos in node_positions.items():
            n = Node(idx, pos)
            if idx == 7:
                n.set_state("failed")
            else:
                n.set_state("normal")
            node_objs[idx] = n
            nodes_group.add(n)

        self.add(edges_group, nodes_group)

        # Fade in status
        self.play(status.animate.set_fill(RED, opacity=0.9), run_time=0.5)
        self.wait(0.8)

        # Sequential node failures (node 10, 6, 2, 8, 0 — every 0.9s)
        failure_order = [10, 6, 2, 8, 0]
        failed_count = 1

        for fail_node in failure_order:
            self.wait(0.8)
            failed_count += 1
            node = node_objs[fail_node]
            node.set_state("failed")

            # Update counter
            self.play(
                counter.animate.set_fill(RED, opacity=0.9).scale(1.05),
                run_time=0.2
            )
            new_counter = Text(f"FAILED NODES: {failed_count}/12",
                               font="Ubuntu Mono", font_size=11,
                               fill_color=RED, fill_opacity=0.9)
            new_counter.move_to(counter.get_center())
            self.remove(counter)
            self.add(new_counter)
            counter = new_counter

            # Dim connected edges
            for neighbor, key in network.adjacency[fail_node]:
                line_idx = edge_map.get(key)
                if line_idx is not None:
                    line = edges_group[line_idx]
                    self.play(line.animate.set_stroke(GRAY_DIM, opacity=0.1), run_time=0.3)

        self.wait(3)


# ─────────────────────────────────────────────────────────────────────────────
# SCENE 5: CASCADE COMPLETE — MESSAGE REVEAL
# ─────────────────────────────────────────────────────────────────────────────
class Scene5_CascadeComplete(Scene):
    """Final message: white text on black. Fade-in reveal."""

    def construct(self):
        self.camera.background_color = BG

        # Subtle grid (proof: minimal)
        grid_h = VGroup(*[
            Line(LEFT * 8, RIGHT * 8, stroke_color=CYAN, stroke_opacity=0.04)
            for y in np.arange(-4, 4.5, 0.8)
        ])
        grid_v = VGroup(*[
            Line(UP * 5, DOWN * 5, stroke_color=CYAN, stroke_opacity=0.04)
            for x in np.arange(-8, 8.5, 0.8)
        ])
        self.add(grid_h, grid_v)

        # Line 1
        line1 = Text("Most systems don't fail instantly.",
                     font="Ubuntu Mono", font_size=22,
                     fill_color=WHITE, fill_opacity=0.0, weight=BOLD)
        line1.move_to(ORIGIN).shift(UP * 0.5)

        # Line 2 (emphasized)
        line2 = Text("They fail gradually, then all at once.",
                     font="Ubuntu Mono", font_size=26,
                     fill_color=CYAN, fill_opacity=0.0, weight=BOLD)

        # Attribution
        attr = Text("— The Cascade — A Coordination Failure Story",
                   font="Ubuntu Mono", font_size=10,
                   fill_color=GRAY, fill_opacity=0.0)
        attr.next_to(line2, DOWN, buff=0.4)

        self.add(line1, line2, attr)

        # Animate reveal
        self.wait(0.5)
        self.play(line1.animate.set_fill(WHITE, opacity=0.85), run_time=1.5, rate_func=slow_into)
        self.wait(0.8)
        self.play(line2.animate.set_fill(CYAN, opacity=1.0), run_time=1.8, rate_func=slow_into)
        self.wait(0.5)
        self.play(attr.animate.set_fill(GRAY, opacity=0.5), run_time=1.0, rate_func=slow_into)
        self.wait(5.5)  # Extended hold for attribution legibility before fade


# ─────────────────────────────────────────────────────────────────────────────
# PROOF RENDER SCRIPT — Runs all 5 scenes sequentially
# ─────────────────────────────────────────────────────────────────────────────
"""
To render proof sequences individually:

  manim -pqh the_cascade.py Scene1_StableNetwork
  manim -pqh the_cascade.py Scene2_LatencySpike
  manim -pqh the_cascade.py Scene3_RerouteExplosion
  manim -pqh the_cascade.py Scene4_Fragmentation
  manim -pqh the_cascade.py Scene5_CascadeComplete

To render full quality (4K / cinematic), run after proof validation:

  manim -pqk the_cascade.py [SceneClass]

Flags:
  -p  preview (opens player after render)
  -q  quiet (less console noise)
  -h  high quality (1080p for proof)
  -k  4K for cinematic pass
"""
