"""
THE CASCADE — CINEMATIC INFRASTRUCTURE VISUALIZATION
======================================================
Phase 2: Cinematic Build (1080p60 / Manim 0.20.1)

Architecture:
  SCENE 1 — [00:00–08:00] SYSTEM ALIVE
    - Slow camera establishing drift
    - 14-node network materializes from dark
    - Ambient particle field drifts
    - 35 agents traverse edges with realistic traffic
    - Telemetry HUD: LATENCY, THROUGHPUT, NODE STATUS
    - No explanatory text — atmosphere only

  SCENE 2 — [08:00–16:00] ANOMALY DETECTED
    - Subtle: node_07 latency meter begins climbing
    - Camera: slow zoom toward node_07
    - Ambient particles react — slight turbulence
    - Telemetry: node_07 LATENCY: +340ms
    - Single ping alert — isolated, controlled
    - System still nominally stable

  SCENE 3 — [16:00–25:00] CASCADE INITIATION
    - node_07 fails — red glow pulse
    - Camera micro-shake
    - 12 agents reroute simultaneously (reroute flood)
    - Edge congestion heat: DIM → AMBER → RED
    - Telemetry: "AGENTS REROUTING" counter spikes
    - Pressure meter in HUD begins red zone

  SCENE 4 — [25:00–35:00] FRAGMENTATION
    - Sequential node failures: 6, 2, 10, 8, 0, 3 (every 1.2s)
    - Camera pulls back slowly to track the spread
    - Network fragments visually — red edges disconnect
    - Agent scatter/die animations
    - All telemetry in red
    - System state: CRITICAL / COORDINATION FAILURE

  SCENE 5 — [35:00–43:00] SILENCE
    - Network dark, mostly offline
    - Grid reappears — cold, clinical
    - Final text fade-in:
        "Most systems don't fail instantly."
        "They fail gradually, then all at once."
    - Attribution
    - Black out

Palette:
  BG          #050508   near-black, blue-shifted dark
  CYAN        #00c8e8   electric infrastructure cyan
  CYAN_DIM    #0a2a35   deep muted cyan
  AMBER       #ff9500   warning amber
  RED         #ff2222   critical red
  RED_DEEP    #800000   deep failure red
  WHITE       #e8e8f0   cool white
  GRAY        #2a3040   muted slate
  GRID        #0d1520   subtle grid line

Audio Hooks (add_sound):
  08s — single alert ping (440Hz, 80ms)
  14s — latency drop impact (60Hz sub, 200ms)
  16s — reroute burst (white noise 150ms)
  20s — second impact hit
  25s — cascade rumble onset
  35s — silence + room tone
"""

from manim import *
import random, math

# ── Palette ──────────────────────────────────────────────────────────────────
BG        = "#050508"
BG2       = "#080a10"
CYAN      = "#00c8e8"
CYAN_DIM  = "#0a2a35"
CYAN_MID  = "#0d4a5a"
AMBER     = "#ff9500"
RED       = "#ff2222"
RED_DIM   = "#800000"
RED_DARK  = "#3a0000"
WHITE     = "#e8e8f0"
GRAY      = "#2a3040"
GRAY_DIM  = "#151c25"
GRID      = "#0d1520"
TELEM     = "#00ff88"   # telemetry green


# ──────────────────────────────────────────────────────────────────────────────
# GLOW SYSTEM — layered halo renderer (manual glow, no post-processing)
# ──────────────────────────────────────────────────────────────────────────────
def make_glow(dot_or_circle, color, glow_radius=1.6, layers=4):
    """
    Returns a VGroup of concentric translucent halos around an object.
    Simulates bloom/glow without post-processing.
    """
    group = VGroup()
    for i in range(layers, 0, -1):
        factor = i / layers
        halo = dot_or_circle.copy()
        if hasattr(halo, 'radius'):
            halo.set_fill(color, opacity=0.0)
            halo.set_stroke(color, opacity=0.04 * factor)
            new_radius = halo.radius * (1 + (glow_radius - 1) * factor)
            halo.scale(new_radius / halo.radius)
        group.add(halo)
    return group


def make_node_glow(position, radius=0.16, color=CYAN):
    """Build a multi-layer glow halo at a position."""
    core = Circle(radius=radius, fill_color=color, fill_opacity=0.95,
                  stroke_color=color, stroke_opacity=0.6)
    core.move_to(position)
    halos = VGroup()
    for layer in [3.0, 2.2, 1.6]:
        h = Circle(radius=radius * layer, fill_opacity=0,
                   stroke_color=color, stroke_opacity=0.025 / (layer - 0.9))
        h.move_to(position)
        halos.add(h)
    return core, halos


# ──────────────────────────────────────────────────────────────────────────────
# PARTICLE FIELD — multi-layer atmospheric depth
# ──────────────────────────────────────────────────────────────────────────────
class ParticleLayer:
    """Slow-drifting background particle field. Layered for depth parallax."""

    def __init__(self, count=60, speed_range=(0.02, 0.08),
                 opacity_range=(0.04, 0.14), radius_range=(0.008, 0.025),
                 color=CYAN_DIM, width=18, height=10):
        self.count = count
        self.speed_range = speed_range
        self.opacity_range = opacity_range
        self.radius_range = radius_range
        self.color = color
        self.width = width
        self.height = height
        self.particles = []
        self.vgroup = VGroup()
        self.build()

    def build(self):
        for _ in range(self.count):
            r = random.uniform(*self.radius_range)
            op = random.uniform(*self.opacity_range)
            p = Dot(radius=r, fill_color=self.color, fill_opacity=op)
            p.move_to([
                random.uniform(-self.width/2, self.width/2),
                random.uniform(-self.height/2, self.height/2),
                random.uniform(-0.1, 0.1)
            ])
            p.speed = random.uniform(*self.speed_range)
            p.angle = random.uniform(0, TAU)
            p.drift = random.uniform(0.995, 1.002)
            self.particles.append(p)
            self.vgroup.add(p)

    def start_drift(self, scene):
        """Attach updater to scene."""
        def update_particles(mob, dt):
            for p in self.particles:
                dx = math.cos(p.angle) * p.speed * dt
                dy = math.sin(p.angle) * p.speed * dt
                p.shift([dx, dy, 0])
                # Wrap at edges
                if p.get_center()[0] > self.width / 2:
                    p.shift([-(self.width + 0.2), 0, 0])
                if p.get_center()[1] > self.height / 2:
                    p.shift([0, -(self.height + 0.2), 0])
                if p.get_center()[0] < -self.width / 2:
                    p.shift([self.width + 0.2, 0, 0])
                if p.get_center()[1] < -self.height / 2:
                    p.shift([0, self.height + 0.2, 0])
        self.vgroup.add_updater(update_particles)
        scene.add(self.vgroup)

    def set_opacity(self, op):
        for p in self.particles:
            p.set_fill(self.color, opacity=op)


# ──────────────────────────────────────────────────────────────────────────────
# TELEMETRY HUD — minimal system overlays
# ──────────────────────────────────────────────────────────────────────────────
class TelemetryHUD:
    """
    Minimal telemetry overlay — system metrics, no explanatory text.
    Monospace, muted, bottom-left anchored.
    """

    def __init__(self, width=12):
        self.width = width
        self.items = {}
        self.group = VGroup()
        self.build()

    def build(self):
        # Subtle HUD frame lines
        self.group = VGroup()

        # Grid of metric readouts — positioned bottom-left
        metrics = [
            ("LAT", "000ms"),
            ("THRU", "000k/s"),
            ("NODE", "14/14"),
            ("AGENT", "35/35"),
            ("SYS", "STABLE"),
        ]
        x_start = -5.8
        y_pos = -3.6
        spacing = 2.1

        for i, (label, val) in enumerate(metrics):
            lbl = Text(label, font="Ubuntu Mono", font_size=9,
                       fill_color=GRAY, fill_opacity=0.6)
            lbl.move_to([x_start + i * spacing, y_pos, 0])

            val_txt = Text(val, font="Ubuntu Mono", font_size=9,
                           fill_color=CYAN, fill_opacity=0.8)
            val_txt.move_to([x_start + i * spacing + 0.5, y_pos, 0])

            self.group.add(lbl, val_txt)
            self.items[label] = val_txt

        # Top status bar
        self.status = Text("INFRASTRUCTURE COORDINATION LAYER",
                           font="Ubuntu Mono", font_size=9,
                           fill_color=GRAY, fill_opacity=0.35)
        self.status.to_edge(UP).shift(DOWN * 0.3)
        self.group.add(self.status)

    def update_metric(self, label, value, color=TELEM):
        if label in self.items:
            self.items[label].become(
                Text(str(value), font="Ubuntu Mono", font_size=9,
                     fill_color=color, fill_opacity=0.9)
            )

    def set_status(self, text, color=GRAY):
        self.status.become(
            Text(text, font="Ubuntu Mono", font_size=9,
                 fill_color=color, fill_opacity=0.6)
        )
        self.status.to_edge(UP).shift(DOWN * 0.3)


# ──────────────────────────────────────────────────────────────────────────────
# NETWORK NODE — infrastructure-grade with glow
# ──────────────────────────────────────────────────────────────────────────────
class CinematicNode:
    """A single network node with glow halo and state machine."""

    def __init__(self, index, position, radius=0.18, label=None):
        self.index = index
        self.pos = position
        self.radius = radius
        self.state = "normal"  # normal | warning | critical | failed
        self.label_id = f"N{index:02d}"
        self.pulse_timer = random.uniform(0, TAU)

        # Glow halos (built once)
        self.halos = VGroup()
        for mult in [3.2, 2.4, 1.8]:
            h = Circle(radius=radius * mult, fill_opacity=0,
                       stroke_color=CYAN, stroke_opacity=0.0)
            h.move_to(position)
            self.halos.add(h)

        # Core dot
        self.core = Circle(radius=radius, fill_color=CYAN, fill_opacity=0.9,
                           stroke_color=CYAN, stroke_opacity=0.5)
        self.core.move_to(position)

        # Label
        lbl_pos = position + [0, -(radius + 0.14), 0]
        self.label = Text(self.label_id, font="Ubuntu Mono", font_size=7.5,
                          fill_color=CYAN_DIM, fill_opacity=0.5)
        self.label.move_to(lbl_pos)

        self.group = VGroup(self.halos, self.core, self.label)

    def set_state(self, state, animate=True):
        """Update visual state with color transition."""
        self.state = state
        if state == "normal":
            target_fill = CYAN
            target_fill_op = 0.88
            target_stroke = CYAN
            target_halo_op = 0.0
            target_label_op = 0.5
        elif state == "warning":
            target_fill = AMBER
            target_fill_op = 0.95
            target_stroke = AMBER
            target_halo_op = 0.08
            target_label_op = 0.7
        elif state == "critical":
            target_fill = RED
            target_fill_op = 1.0
            target_stroke = RED
            target_halo_op = 0.15
            target_label_op = 0.8
        elif state == "failed":
            target_fill = GRAY_DIM
            target_fill_op = 0.4
            target_stroke = GRAY
            target_stroke_op = 0.2
            target_halo_op = 0.0
            target_label_op = 0.15

        if animate:
            self.core.set_fill(target_fill, opacity=target_fill_op)
            self.core.set_stroke(target_stroke, opacity=0.5)
            if state != "failed":
                for h in self.halos:
                    h.set_stroke(target_fill, opacity=target_halo_op)
            self.label.set_fill(target_fill if state != "normal" else CYAN_DIM,
                                opacity=target_label_op)
        else:
            self.core.set_fill(target_fill, opacity=target_fill_op)
            self.core.set_stroke(target_stroke, opacity=0.5)

    def pulse_expand(self, scene, color=RED, scale=3.5, run_time=0.8):
        """Single pulse ring expansion — adds impact to failure moment."""
        pos = self.pos
        ring = Circle(radius=self.radius * 1.2,
                      fill_opacity=0, stroke_color=color, stroke_opacity=0.9)
        ring.move_to(pos)
        scene.add(ring)
        scene.play(
            ring.animate.scale(scale).set_stroke(color, opacity=0.0),
            run_time=run_time, rate_func=rush_into
        )
        scene.remove(ring)

    def get_group(self):
        return self.group


# ──────────────────────────────────────────────────────────────────────────────
# AGENT — infrastructure traffic dot
# ──────────────────────────────────────────────────────────────────────────────
class Agent:
    """Agent dot that traverses the network edge-by-edge with realistic behavior."""

    def __init__(self, start_node, node_positions, adjacency, color=CYAN, radius=0.038):
        self.color = color
        self.radius = radius
        self.node_positions = node_positions
        self.adjacency = adjacency

        self.dot = Dot(radius=radius, fill_color=color, fill_opacity=0.8,
                       stroke_color=color, stroke_opacity=0.3)
        self.dot.move_to(node_positions[start_node])

        self.current_node = start_node
        self.target_node = None
        self.progress = 0.0
        self.speed = random.uniform(0.5, 1.1)
        self.failed = False
        self.pause_timer = 0.0
        self.path = []

    def set_path(self, target_node):
        """Calculate path and set target."""
        self.target_node = target_node
        self.path = self.find_path(self.current_node, target_node)
        self.path_index = 0
        self.progress = 0.0
        if len(self.path) > 1:
            self.current_node = self.path[0]

    def find_path(self, start, end):
        if start == end:
            return [start]
        queue = [(start, [start])]
        visited = {start}
        while queue:
            node, path = queue.pop(0)
            for neighbor, _ in self.adjacency[node]:
                if neighbor == end:
                    return path + [end]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))
        return [start]

    def update(self, dt):
        """Update agent position along path."""
        if self.failed or not self.path or self.path_index >= len(self.path) - 1:
            return

        if self.pause_timer > 0:
            self.pause_timer -= dt
            return

        from_node = self.path[self.path_index]
        to_node = self.path[self.path_index + 1]

        p1 = self.node_positions[from_node]
        p2 = self.node_positions[to_node]
        seg_len = math.sqrt((p2[0]-p1[0])**2 + (p2[1]-p1[1])**2)

        self.progress += (self.speed * dt * 0.6) / max(seg_len, 0.1)

        if self.progress >= 1.0:
            self.path_index += 1
            self.progress = 0.0
            if self.path_index >= len(self.path) - 1:
                # Reached destination — pick new target
                self.current_node = self.target_node
                self.target_node = None
                self.path = []
                return

        # Interpolate position
        p = p1 + (p2 - p1) * self.progress
        self.dot.move_to(p)

    def reroute_to(self, target_node):
        """Trigger chaotic reroute to target."""
        self.set_path(target_node)
        self.speed = random.uniform(2.0, 3.5)  # Fast!
        self.pause_timer = random.uniform(0, 0.2)  # Brief hesitation


# ──────────────────────────────────────────────────────────────────────────────
# NETWORK — infrastructure graph with heat tracking
# ──────────────────────────────────────────────────────────────────────────────
class NetworkGraph:
    """
    Manages nodes, edges, edge heat, and camera positioning.
    All spatial calculations precomputed for stability.
    """

    def __init__(self, node_positions, edges):
        self.node_positions = node_positions
        self.edges = edges
        self.adjacency = {i: [] for i in node_positions}

        for i, j in edges:
            key = (min(i, j), max(i, j))
            self.adjacency[i].append((j, key))
            self.adjacency[j].append((i, key))

        # Edge heat map: 0=clear, 1=saturated
        self.edge_heat = {}
        for i, j in edges:
            key = (min(i, j), max(i, j))
            self.edge_heat[key] = 0.0

        self.edge_lines = {}  # key -> Line mobject
        self.nodes = {}  # index -> CinematicNode

    def build_edges(self):
        """Build edge mobjects and return VGroup."""
        group = VGroup()
        for i, j in self.edges:
            key = (min(i, j), max(i, j))
            p1 = self.node_positions[i]
            p2 = self.node_positions[j]
            line = Line(p1, p2, stroke_color=CYAN_DIM,
                        stroke_opacity=0.45, stroke_width=1.0)
            self.edge_lines[key] = line
            group.add(line)
        return group

    def build_nodes(self):
        """Build cinematic nodes and return VGroup."""
        group = VGroup()
        for idx, pos in self.node_positions.items():
            n = CinematicNode(idx, pos)
            n.set_state("normal", animate=False)
            self.nodes[idx] = n
            group.add(n.get_group())
        return group

    def set_edge_heat(self, key, heat):
        """Update edge color based on congestion heat (0-1)."""
        self.edge_heat[key] = heat
        if key not in self.edge_lines:
            return
        line = self.edge_lines[key]
        if heat < 0.2:
            line.set_stroke(CYAN_DIM, opacity=0.45)
        elif heat < 0.45:
            line.set_stroke(AMBER, opacity=0.75)
        elif heat < 0.7:
            line.set_stroke(RED, opacity=0.85)
        else:
            line.set_stroke(RED_DIM, opacity=0.95)

    def fail_node(self, idx):
        """Mark node as failed and dim connected edges."""
        if idx in self.nodes:
            self.nodes[idx].set_state("failed")
        # Dim edges connected to this node
        for neighbor, key in self.adjacency[idx]:
            self.set_edge_heat(key, 0.0)
            if key in self.edge_lines:
                self.edge_lines[key].set_stroke(GRAY_DIM, opacity=0.15)

    def get_agent_position(self, from_node, to_node, progress):
        p1 = self.node_positions[from_node]
        p2 = self.node_positions[to_node]
        return p1 + (p2 - p1) * progress


# ──────────────────────────────────────────────────────────────────────────────
# BACKGROUND ATMOSPHERE — volumetric depth layers
# ──────────────────────────────────────────────────────────────────────────────
def build_atmosphere(scene):
    """
    Build layered atmospheric depth planes.
    Creates the 'surveillance room' / 'infrastructure film' feel.
    """
    # Deep background gradient (near-black with blue tint)
    bg_plane = FullScreenRectangle(fill_color=BG, fill_opacity=1.0)
    bg_plane.set_z_index(-10)
    scene.add(bg_plane)

    # Central subtle radial glow
    radial = Circle(radius=4.5, fill_color="#060a12", fill_opacity=0.8,
                   stroke_opacity=0)
    radial.move_to(ORIGIN)
    radial.set_z_index(-8)
    scene.add(radial)

    # Outer vignette effect
    vignette = Circle(radius=7.5, fill_color=BG, fill_opacity=0.0,
                     stroke_color=BG2, stroke_opacity=0.3, stroke_width=12)
    vignette.move_to(ORIGIN)
    vignette.set_z_index(-6)
    scene.add(vignette)


def build_grid(scene, opacity=0.06):
    """Subtle infrastructure grid."""
    grid_h = VGroup(*[
        Line(LEFT * 10, RIGHT * 10,
             stroke_color=CYAN_DIM, stroke_opacity=opacity * 0.6)
        for y in np.arange(-4.5, 5, 0.75)
    ])
    grid_v = VGroup(*[
        Line(UP * 6, DOWN * 6,
             stroke_color=CYAN_DIM, stroke_opacity=opacity * 0.6)
        for x in np.arange(-10, 10.5, 0.75)
    ])
    grid = VGroup(grid_h, grid_v)
    grid.set_z_index(-4)
    scene.add(grid)
    return grid


# ──────────────────────────────────────────────────────────────────────────────
# SCENE 1: SYSTEM ALIVE
# ──────────────────────────────────────────────────────────────────────────────
class CinematicScene1(MovingCameraScene):
    """
    The system breathes.
    14 nodes materialize from darkness.
    35 agents flow along edges — purposeful, alive.
    Telemetry HUD reads nominal.
    Camera: slow drift establishing the space.
    Duration: 8 seconds.
    """

    def construct(self):
        self.camera.background_color = BG
        self.camera.frame.set_width(14)

        # ── Atmosphere ─────────────────────────────────────────────────────
        build_atmosphere(self)
        grid = build_grid(self, opacity=0.05)
        grid.fade(0.7)  # Very subtle

        # ── Particle layers (3 depth layers) ───────────────────────────────
        far_particles = ParticleLayer(count=50, speed_range=(0.008, 0.025),
                                      opacity_range=(0.03, 0.08),
                                      radius_range=(0.006, 0.018),
                                      color="#0a2030", width=16, height=9)
        far_particles.start_drift(self)

        mid_particles = ParticleLayer(count=35, speed_range=(0.015, 0.045),
                                      opacity_range=(0.04, 0.12),
                                      radius_range=(0.008, 0.022),
                                      color="#0d2535", width=14, height=8)
        mid_particles.start_drift(self)

        # ── Network definition ───────────────────────────────────────────────
        node_positions = {
            0:  np.array([-5.0,  2.2, 0]),
            1:  np.array([-2.8,  3.8, 0]),
            2:  np.array([ 0.4,  3.4, 0]),
            3:  np.array([ 3.8,  2.6, 0]),
            4:  np.array([ 5.8,  0.8, 0]),
            5:  np.array([ 4.8, -2.2, 0]),
            6:  np.array([ 1.2, -3.2, 0]),
            7:  np.array([-1.8, -2.8, 0]),  # FAILURE NODE
            8:  np.array([-4.5, -1.2, 0]),
            9:  np.array([-5.8,  0.6, 0]),
            10: np.array([ 0.0,  0.0, 0]),
            11: np.array([ 2.5,  1.0, 0]),
            12: np.array([-3.0,  0.8, 0]),
            13: np.array([ 0.8, -1.2, 0]),
        }

        edges = [
            (0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,9),(9,0),
            (0,10),(1,10),(2,10),(3,11),(4,11),(5,11),(6,10),(7,10),(8,10),(9,10),
            (10,11),(2,11),(3,5),(1,8),(0,12),(12,8),(10,13),(13,6),(11,13),(4,11)
        ]

        network = NetworkGraph(node_positions, edges)
        edges_group = network.build_edges()
        nodes_group = network.build_nodes()

        self.add(edges_group, nodes_group)

        # ── Telemetry HUD ──────────────────────────────────────────────────
        hud = TelemetryHUD()
        self.add(hud.group)

        # ── Agents (35) ────────────────────────────────────────────────────
        NUM_AGENTS = 35
        agents = []
        agent_dots = VGroup()

        for i in range(NUM_AGENTS):
            start = random.choice(list(node_positions.keys()))
            agent = Agent(start, node_positions, network.adjacency)
            agent.dot.move_to(node_positions[start])
            # Give random initial destination
            dest = random.choice(list(node_positions.keys()))
            agent.set_path(dest)
            agents.append(agent)
            agent_dots.add(agent.dot)

        self.add(agent_dots)

        # ── Agent updater ─────────────────────────────────────────────────
        def update_agents(mob, dt):
            for agent in agents:
                if agent.failed:
                    continue
                if agent.target_node is None:
                    # Pick new random destination
                    dest = random.choice(list(node_positions.keys()))
                    agent.set_path(dest)
                    agent.speed = random.uniform(0.5, 1.1)
                else:
                    agent.update(dt)

        agent_dots.add_updater(update_agents)

        self.wait(8)


# ──────────────────────────────────────────────────────────────────────────────
# SCENE 2: ANOMALY DETECTED
# ──────────────────────────────────────────────────────────────────────────────
class CinematicScene2(MovingCameraScene):
    """
    node_07 latency climbs.
    Camera: controlled zoom toward failure point.
    Single alert ping.
    System still nominal — but the tension is present.
    Duration: 8 seconds.
    """

    def construct(self):
        self.camera.background_color = BG
        self.camera.frame.set_width(14)

        build_atmosphere(self)
        grid = build_grid(self, opacity=0.04)

        node_positions = {
            0:  np.array([-5.0,  2.2, 0]),
            1:  np.array([-2.8,  3.8, 0]),
            2:  np.array([ 0.4,  3.4, 0]),
            3:  np.array([ 3.8,  2.6, 0]),
            4:  np.array([ 5.8,  0.8, 0]),
            5:  np.array([ 4.8, -2.2, 0]),
            6:  np.array([ 1.2, -3.2, 0]),
            7:  np.array([-1.8, -2.8, 0]),  # ANOMALY NODE
            8:  np.array([-4.5, -1.2, 0]),
            9:  np.array([-5.8,  0.6, 0]),
            10: np.array([ 0.0,  0.0, 0]),
            11: np.array([ 2.5,  1.0, 0]),
            12: np.array([-3.0,  0.8, 0]),
            13: np.array([ 0.8, -1.2, 0]),
        }

        edges = [
            (0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,9),(9,0),
            (0,10),(1,10),(2,10),(3,11),(4,11),(5,11),(6,10),(7,10),(8,10),(9,10),
            (10,11),(2,11),(3,5),(1,8),(0,12),(12,8),(10,13),(13,6),(11,13),(4,11)
        ]

        network = NetworkGraph(node_positions, edges)
        edges_group = network.build_edges()
        nodes_group = network.build_nodes()
        self.add(edges_group, nodes_group)

        # Telemetry
        hud = TelemetryHUD()
        hud.update_metric("LAT", "+042ms", color=TELEM)
        hud.update_metric("NODE", "14/14", color=TELEM)
        hud.group.set_opacity(0.6)
        self.add(hud.group)

        # Camera: slow zoom toward node_07
        node7_pos = node_positions[7]
        self.play(
            self.camera.frame.animate.scale(0.62).move_to(node7_pos + [0, 0, 0]),
            run_time=5.0, rate_func=slow_into
        )

        # Node 7 turns warning state at t=2s
        self.wait(2.0)
        network.nodes[7].set_state("warning")
        hud.update_metric("LAT", "+187ms", color=AMBER)
        self.wait(1.5)

        # Pulse rings from node_07
        for i in range(2):
            ring = Circle(radius=0.3 + i * 0.5,
                          fill_opacity=0, stroke_color=AMBER, stroke_opacity=0.7)
            ring.move_to(node_positions[7])
            self.add(ring)
            self.play(ring.animate.scale(4.0).set_stroke(AMBER, opacity=0.0),
                      run_time=1.2, rate_func=slow_into)
            self.remove(ring)
            self.wait(0.3)

        # Latency climbs further
        hud.update_metric("LAT", "+340ms", color=RED)

        # Camera micro-shake (subtle)
        base_pos = self.camera.frame.get_center()
        for _ in range(3):
            self.play(
                self.camera.frame.animate.shift([0.05, 0.03, 0]),
                run_time=0.08, rate_func=rush_into
            )
            self.play(
                self.camera.frame.animate.shift([-0.05, -0.03, 0]),
                run_time=0.08, rate_func=rush_into
            )

        self.wait(2.5)


# ──────────────────────────────────────────────────────────────────────────────
# SCENE 3: CASCADE INITIATION
# ──────────────────────────────────────────────────────────────────────────────
class CinematicScene3(MovingCameraScene):
    """
    node_07 fails — red pulse.
    14 agents reroute simultaneously.
    Edge congestion floods: DIM → AMBER → RED.
    Camera shake. System pressure spikes.
    Duration: 9 seconds.
    """

    def construct(self):
        self.camera.background_color = BG
        self.camera.frame.set_width(8)

        build_atmosphere(self)
        grid = build_grid(self, opacity=0.04)

        node_positions = {
            0:  np.array([-5.0,  2.2, 0]),
            1:  np.array([-2.8,  3.8, 0]),
            2:  np.array([ 0.4,  3.4, 0]),
            3:  np.array([ 3.8,  2.6, 0]),
            4:  np.array([ 5.8,  0.8, 0]),
            5:  np.array([ 4.8, -2.2, 0]),
            6:  np.array([ 1.2, -3.2, 0]),
            7:  np.array([-1.8, -2.8, 0]),  # FAILED
            8:  np.array([-4.5, -1.2, 0]),
            9:  np.array([-5.8,  0.6, 0]),
            10: np.array([ 0.0,  0.0, 0]),
            11: np.array([ 2.5,  1.0, 0]),
            12: np.array([-3.0,  0.8, 0]),
            13: np.array([ 0.8, -1.2, 0]),
        }

        edges = [
            (0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,9),(9,0),
            (0,10),(1,10),(2,10),(3,11),(4,11),(5,11),(6,10),(7,10),(8,10),(9,10),
            (10,11),(2,11),(3,5),(1,8),(0,12),(12,8),(10,13),(13,6),(11,13),(4,11)
        ]

        network = NetworkGraph(node_positions, edges)
        edges_group = network.build_edges()
        nodes_group = network.build_nodes()

        # node_07 already critical
        network.nodes[7].set_state("critical", animate=False)

        self.add(edges_group, nodes_group)

        # Telemetry
        hud = TelemetryHUD()
        hud.update_metric("LAT", "+340ms", color=RED)
        hud.update_metric("SYS", "CRITICAL", color=RED)
        hud.group.set_opacity(0.7)
        self.add(hud.group)

        # ── Agents ─────────────────────────────────────────────────────────
        NUM_AGENTS = 35
        agents = []
        agent_dots = VGroup()

        for i in range(NUM_AGENTS):
            start = random.choice(list(node_positions.keys()))
            agent = Agent(start, node_positions, network.adjacency)
            agent.dot.move_to(node_positions[start])
            # Reroute toward node_07 (chaos begins)
            if i % 3 == 0:
                agent.reroute_to(7)  # Head toward failure
            else:
                dest = random.choice(list(node_positions.keys()))
                agent.set_path(dest)
            agents.append(agent)
            agent_dots.add(agent.dot)

        self.add(agent_dots)

        # ── node_07 failure pulse ───────────────────────────────────────────
        self.wait(1.0)
        network.nodes[7].pulse_expand(self, color=RED, scale=4.5, run_time=1.0)
        network.nodes[7].set_state("failed")

        # Flash all halos red briefly
        for n in network.nodes.values():
            if n.state != "failed":
                for halo in n.halos:
                    self.play(halo.animate.set_stroke(RED, opacity=0.06),
                              run_time=0.15)

        # Camera shake
        for _ in range(5):
            self.play(
                self.camera.frame.animate.shift([
                    random.uniform(-0.08, 0.08),
                    random.uniform(-0.05, 0.05), 0
                ]),
                run_time=0.06, rate_func=rush_into
            )
        self.play(
            self.camera.frame.animate.move_to([0, 0, 0]),
            run_time=0.3
        )

        # Edge congestion floods
        all_keys = list(network.edge_lines.keys())
        for step in range(3):
            self.wait(0.4)
            # Heat up a batch of edges
            batch = all_keys[step * 6:(step + 1) * 6]
            for key in batch:
                network.set_edge_heat(key, min(1.0, 0.3 + step * 0.35))

        # Agent counter updates
        reroute_count = sum(1 for a in agents if a.target_node == 7)
        hud.update_metric("AGENT", f"{reroute_count} REROUTE", color=AMBER)

        # Agent updater with chaos
        def update_chaos(mob, dt):
            for agent in agents:
                if agent.failed:
                    continue
                # Occasionally reroute
                if agent.target_node is None:
                    dest = random.choice(list(node_positions.keys()))
                    agent.set_path(dest)
                    agent.speed = random.uniform(1.8, 3.0)
                else:
                    agent.update(dt)
                    # Kill agents that hit node_07
                    if agent.path and agent.path_index >= len(agent.path) - 1:
                        if agent.current_node == 7:
                            agent.failed = True
                            agent.dot.set_fill(GRAY_DIM, opacity=0.25)
                            agent.dot.set_stroke(GRAY, opacity=0.1)

        # Agent updater with chaos — attached to agent_dots for per-frame update
        agent_dots.add_updater(update_chaos)

        # Camera: widen back out slowly
        self.play(
            self.camera.frame.animate.scale(12 / self.camera.frame.get_width()).move_to([0, 0, 0]),
            run_time=4.0, rate_func=slow_into
        )

        self.wait(4)


# ──────────────────────────────────────────────────────────────────────────────
# SCENE 4: FRAGMENTATION
# ──────────────────────────────────────────────────────────────────────────────
class CinematicScene4(MovingCameraScene):
    """
    6 more nodes fail in sequence.
    Network fragments.
    Camera pulls back to track the spread.
    Agents scatter.
    Telemetry: COORDINATION FAILURE.
    Duration: 10 seconds.
    """

    def construct(self):
        self.camera.background_color = BG
        self.camera.frame.set_width(12)

        build_atmosphere(self)
        grid = build_grid(self, opacity=0.04)

        node_positions = {
            0:  np.array([-5.0,  2.2, 0]),
            1:  np.array([-2.8,  3.8, 0]),
            2:  np.array([ 0.4,  3.4, 0]),
            3:  np.array([ 3.8,  2.6, 0]),
            4:  np.array([ 5.8,  0.8, 0]),
            5:  np.array([ 4.8, -2.2, 0]),
            6:  np.array([ 1.2, -3.2, 0]),
            7:  np.array([-1.8, -2.8, 0]),  # FAILED
            8:  np.array([-4.5, -1.2, 0]),
            9:  np.array([-5.8,  0.6, 0]),
            10: np.array([ 0.0,  0.0, 0]),
            11: np.array([ 2.5,  1.0, 0]),
            12: np.array([-3.0,  0.8, 0]),
            13: np.array([ 0.8, -1.2, 0]),
        }

        edges = [
            (0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,9),(9,0),
            (0,10),(1,10),(2,10),(3,11),(4,11),(5,11),(6,10),(7,10),(8,10),(9,10),
            (10,11),(2,11),(3,5),(1,8),(0,12),(12,8),(10,13),(13,6),(11,13),(4,11)
        ]

        network = NetworkGraph(node_positions, edges)
        edges_group = network.build_edges()
        nodes_group = network.build_nodes()

        # Pre-fail node 07
        network.nodes[7].set_state("failed", animate=False)

        self.add(edges_group, nodes_group)

        # Telemetry — critical state
        hud = TelemetryHUD()
        hud.update_metric("LAT", "+++", color=RED)
        hud.update_metric("SYS", "FAILURE", color=RED)
        hud.set_status("COORDINATION FAILURE", color=RED)
        hud.group.set_opacity(0.8)
        self.add(hud.group)

        # ── Failure sequence ───────────────────────────────────────────────
        failure_order = [10, 6, 2, 8, 0, 3]
        failed_so_far = {7}

        # Camera: pull back slightly at start to reveal full spread
        self.play(
            self.camera.frame.animate.scale(1.3).move_to([0, -0.5, 0]),
            run_time=3.0, rate_func=slow_into
        )

        for f_node in failure_order:
            self.wait(1.2)
            failed_so_far.add(f_node)

            node = network.nodes[f_node]
            node.set_state("failed")
            node.pulse_expand(self, color=RED, scale=3.5, run_time=0.8)

            # Fail connected edges
            for neighbor, key in network.adjacency[f_node]:
                network.set_edge_heat(key, 0.0)
                if key in network.edge_lines:
                    line = network.edge_lines[key]
                    self.play(line.animate.set_stroke(GRAY_DIM, opacity=0.12),
                              run_time=0.25)

            # Update telemetry
            remaining = 14 - len(failed_so_far)
            hud.update_metric("NODE", f"{remaining}/14", color=RED)
            hud.update_metric("AGENT", f"{max(0, 35 - len(failed_so_far)*3)}/35", color=RED)

            # Camera micro-shake per failure
            for _ in range(2):
                self.play(
                    self.camera.frame.animate.shift([
                        random.uniform(-0.06, 0.06),
                        random.uniform(-0.04, 0.04), 0
                    ]),
                    run_time=0.05, rate_func=rush_into
                )

        self.wait(3)


# ──────────────────────────────────────────────────────────────────────────────
# SCENE 5: SILENCE — THE MESSAGE
# ──────────────────────────────────────────────────────────────────────────────
class CinematicScene5(MovingCameraScene):
    """
    Network dark.
    Cold grid reappears.
    Final message fade-in — no explanation, just truth.
    Duration: 8 seconds.
    """

    def construct(self):
        self.camera.background_color = BG
        self.camera.frame.set_width(14)
        self.camera.frame.move_to(ORIGIN)

        # ── Atmosphere — cold, clinical ────────────────────────────────────
        bg_plane = FullScreenRectangle(fill_color="#030305", fill_opacity=1.0)
        self.add(bg_plane)

        # Grid — cold, precise
        grid_h = VGroup(*[
            Line(LEFT * 10, RIGHT * 10,
                 stroke_color="#0a1520", stroke_opacity=0.5)
            for y in np.arange(-4.5, 5, 0.75)
        ])
        grid_v = VGroup(*[
            Line(UP * 6, DOWN * 6,
                 stroke_color="#0a1520", stroke_opacity=0.5)
            for x in np.arange(-10, 10.5, 0.75)
        ])
        self.add(VGroup(grid_h, grid_v))

        # Dead network remnants (very faint)
        node_positions = {
            0:  np.array([-5.0,  2.2, 0]),
            1:  np.array([-2.8,  3.8, 0]),
            2:  np.array([ 0.4,  3.4, 0]),
            3:  np.array([ 3.8,  2.6, 0]),
            4:  np.array([ 5.8,  0.8, 0]),
            5:  np.array([ 4.8, -2.2, 0]),
            6:  np.array([ 1.2, -3.2, 0]),
            7:  np.array([-1.8, -2.8, 0]),
            8:  np.array([-4.5, -1.2, 0]),
            9:  np.array([-5.8,  0.6, 0]),
            10: np.array([ 0.0,  0.0, 0]),
            11: np.array([ 2.5,  1.0, 0]),
            12: np.array([-3.0,  0.8, 0]),
            13: np.array([ 0.8, -1.2, 0]),
        }

        # Dead nodes — barely visible
        dead_nodes = VGroup()
        for pos in node_positions.values():
            d = Circle(radius=0.06, fill_color=GRAY_DIM, fill_opacity=0.3,
                       stroke_color=GRAY, stroke_opacity=0.1)
            d.move_to(pos)
            dead_nodes.add(d)
        self.add(dead_nodes)

        # ── Final message ─────────────────────────────────────────────────
        line1 = Text("Most systems don't fail instantly.",
                      font="Ubuntu Mono", font_size=21,
                      fill_color=WHITE, fill_opacity=0.0)
        line1.move_to(ORIGIN).shift(UP * 0.6)

        line2 = Text("They fail gradually, then all at once.",
                      font="Ubuntu Mono", font_size=25,
                      fill_color=CYAN, fill_opacity=0.0, weight=BOLD)
        line2.move_to(ORIGIN).shift(DOWN * 0.4)

        attr = Text("— Coordination Intelligence Analysis",
                    font="Ubuntu Mono", font_size=9,
                    fill_color=GRAY, fill_opacity=0.0)
        attr.next_to(line2, DOWN, buff=0.5)

        self.add(line1, line2, attr)

        # ── Reveal sequence ───────────────────────────────────────────────
        self.wait(1.0)

        # Grid fades in first (atmosphere)
        self.play(
            grid_h.animate.set_stroke(opacity=0.45),
            grid_v.animate.set_stroke(opacity=0.45),
            run_time=1.5
        )
        self.wait(0.5)

        # Line 1 fades
        self.play(line1.animate.set_fill(WHITE, opacity=0.82),
                  run_time=2.0, rate_func=slow_into)
        self.wait(1.0)

        # Line 2 — the key line — weighted
        self.play(line2.animate.set_fill(CYAN, opacity=1.0),
                  run_time=2.2, rate_func=slow_into)
        self.wait(0.6)

        # Attribution
        self.play(attr.animate.set_fill(GRAY, opacity=0.45),
                  run_time=1.2, rate_func=slow_into)

        self.wait(2.5)


# ──────────────────────────────────────────────────────────────────────────────
# COMBINED CINEMATIC RENDER (Full Sequence)
# ──────────────────────────────────────────────────────────────────────────────
"""
To render individual scenes for validation:

  manim -pqh showcase/the_cascade/cinematic/the_cascade_cinematic.py CinematicScene1
  manim -pqh showcase/the_cascade/cinematic/the_cascade_cinematic.py CinematicScene2
  manim -pqh showcase/the_cascade/cinematic/the_cascade_cinematic.py CinematicScene3
  manim -pqh showcase/the_cascade/cinematic/the_cascade_cinematic.py CinematicScene4
  manim -pqh showcase/the_cascade/cinematic/the_cascade_cinematic.py CinematicScene5

Flags:
  -p  preview (opens player)
  -q  quiet
  -h  high quality (1080p60 for proof)
  -k  4K cinematic pass

Sound sync (manual — add after render):
  Use ffmpeg to combine MP4 + audio:
  ffmpeg -i scene.mp4 -i audio.aiff -c:v copy -c:a aac -shortest output.mp4
"""
