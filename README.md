# The Cascade — Distributed Systems Recovery Dynamics

> **An experimental platform for studying congestion propagation, retry storms, and recovery behavior in distributed coordination systems.**

Built for systems engineers and researchers who want to observe, measure, and reproduce distributed coordination failures under controlled, deterministic conditions.

---

## What This Is

The Cascade is a **telemetry-driven simulation engine** for distributed systems recovery dynamics. It models a mesh network of nodes under failure injection, tracks latency propagation, retry amplification, fragmentation cascades, and recovery behavior — with full Prometheus/Grafana instrumentation and deterministic replay.

It is NOT a visualization tool. The Manim renders are a secondary output. The primary output is **observability data**: metrics, traces, replay metadata, and structured telemetry CSVs.

---

## Core Components

| Component | Location | Role |
|---|---|---|
| **Simulation Engine** | `simulations/recovery_engine.py` | Deterministic per-seed simulation of distributed mesh under failure |
| **Metrics Server** | `services/metrics_server.py` | Prometheus `/metrics` endpoint — exposes real-time telemetry |
| **Replay Manager** | `replays/replay_manager.py` | Deterministic replay system — save + re-run any experiment |
| **Topology Benchmarks** | `experiments/topology_benchmarks.py` | Compare mesh / ring / scale-free / hierarchical topologies |
| **OpenTelemetry Tracing** | `tracing/otel_instrumentation.py` | Distributed trace spans for the agent decision loop |
| **Prometheus Stack** | `monitoring/` | Prometheus scrape config, alerting rules, Grafana dashboards |
| **Docker Compose** | `docker-compose.yml` | Full observability stack: Prometheus + Grafana + Jaeger + Alertmanager |

---

## Quick Start

### 1 — Start the observability stack

```bash
docker-compose up -d
```

Stack at:
- **Grafana** — http://localhost:3000 (admin / cascade)
- **Prometheus** — http://localhost:9091
- **Jaeger** — http://localhost:16686
- **Metrics** — http://localhost:9090/metrics (direct Prometheus scrape target)

### 2 — Run the simulation with live metrics

```bash
# Engine + Prometheus metrics + 4K Manim render
python run_v2.py --config configs/recovery_test.json --enable-metrics

# Engine + Prometheus metrics only (fastest iteration)
python run_v2.py --config configs/recovery_test.json --engine-only --enable-metrics

# Engine only (no metrics server, no render)
python run_v2.py --config configs/recovery_test.json --engine-only
```

### 3 — Watch live telemetry in Grafana

Open http://localhost:3000 → **Recovery Dynamics** dashboard.

Real-time panels:
- p95 latency over time (1s scrape interval)
- Retry storm volume spikes
- Fragmented node count
- Stability score trend
- Edge congestion heat
- Global health score

---

## Architecture

```
simulation-engine (container :9090)
  │
  │  tick-by-tick via HTTP
  ▼
Prometheus (:9091)  ──────────────────────────── Grafana (:3000)
  │ scrape every 1s from :9090                  │ pre-provisioned dashboards
  │ alerts → Alertmanager (:9093)               │ Prometheus datasource
  │
Jaeger (:16686)  ← OTLP gRPC (4317)              │
  │ traces from engine + services                │
OTel Collector                                 │
  │                                             │
simulation-engine ←──────────────────────────────┘
```

### Pipeline (run_v2.py)

```
Config JSON
    │
    ▼
┌─────────────────────┐
│  PrometheusMetrics  │  ← Optional, background thread
│  Server (:9090)     │
└─────────────────────┘
    │
    │ tick_step() monkey-patch: push metrics every tick
    ▼
┌─────────────────────┐
│  RecoveryEngine    │  ← Deterministic, seeded
│  (tick loop)       │
└─────────────────────┘
    │
    ├── telemetry.csv          ← Manim reads this
    ├── recovery_summary.json
    ├── latency_distribution.csv
    ├── retry_storms.csv
    └── contention_decay.csv
    │
    ▼
┌─────────────────────┐
│  Manim Renderer     │  ← Secondary output
│  (scenes/v2_recovery.py)
└─────────────────────┘
```

---

## Simulation Model

The engine models a **12-node ring mesh** under a 4-phase failure/recovery cycle:

**Phase 1 — STABLE (tick 0–99):** Normal operation. All 12 nodes process requests at base capacity.

**Phase 2 — FAILURE_INJECTION (tick 100–139):** Latency spike injected at `Node_4` (500ms × 50x multiplier = 25,000ms effective latency). Neighboring nodes begin queue buildup.

**Phase 3 — FRAGMENTATION (tick 140–159):** Nodes exceeding `fragmentation_threshold_ms` (25,000ms) become `fragmented = True`. Latency propagates via topological neighbors. Retry storm volume = `sum(queue_depth × 0.4)` for each fragmented node.

**Phase 4 — RECOVERY_INITIATION (tick 160–239):** Load shedding (drop 30% of queue). Traffic exponential decay (half-life: 100 ticks). Fragmented nodes have 6% chance per tick to recover.

**Phase 5 — RECOVERY_OUTCOME (tick 240+):** Final stability resolved. Outcome classification:

| Outcome | Conditions |
|---|---|
| `full_recovery` | No fragmented nodes, stability > 0.9, retry volume < 5 |
| `partial_recovery` | Stability > 0.5, fragmented < 30% of total |
| `oscillation` | 0.3 < stability ≤ 0.6, retry volume > 15 |
| `secondary_collapse` | All other states |

### Configuration Parameters (configs/recovery_test.json)

| Parameter | Value | Effect |
|---|---|---|
| `failure_tick` | 100 | Tick when failure injection begins |
| `latency_multiplier` | 50x | Latency inflation at target node |
| `fragmentation_threshold_ms` | 25,000ms | Latency threshold for fragmentation |
| `recovery_tick` | 160 | Tick when recovery activation begins |
| `recovery_rate` | 0.06 | Per-tick probability of fragmented node recovery |
| `load_shedding_active` | true | Whether load shedding is applied |
| `load_shed_fraction` | 0.30 | Fraction of queue dropped at recovery |
| `traffic_decay_half_life_ticks` | 100 | Exponential decay half-life |
| `retry_backoff_multiplier` | 1.4 | Heat decay penalty when retry volume > 0 |
| `storm_latency_amplifier` | 2.0 | Latency multiplier for nodes in retry storm |

---

## Prometheus Metrics

Exposed at `http://localhost:9090/metrics` every tick when `--enable-metrics` is active:

| Metric | Type | Description |
|---|---|---|
| `cascade_p95_latency_ms` | Histogram | P95 round-trip latency across nodes |
| `cascade_p50_latency_ms` | Histogram | P50 round-trip latency across nodes |
| `cascade_queue_depth` | Gauge | Aggregate queue depth across all nodes |
| `cascade_retry_volume` | Gauge | Current retry storm volume |
| `cascade_fragmented_nodes` | Gauge | Nodes currently fragmented |
| `cascade_active_nodes` | Gauge | Nodes currently operational |
| `cascade_global_health_score` | Gauge | Composite health (0–1) |
| `cascade_stability_score` | Gauge | Stability score (0–1) |
| `cascade_edge_heat_avg` | Gauge | Average edge congestion (0–1) |
| `cascade_edge_heat_peak` | Gauge | Peak edge congestion (0–1) |
| `cascade_recovery_phase` | Gauge | Ordinal phase (0–4) |
| `cascade_total_requests` | Counter | Cumulative successful requests |
| `cascade_failed_requests` | Counter | Cumulative failed requests |
| `cascade_engine_info` | Info | Engine version metadata |

---

## Telemetry Exports

All exports are deterministic under fixed seeds:

| File | Contents |
|---|---|
| `telemetry.csv` | Tick-by-tick: latency, queue, retry, fragmentation, health, stability |
| `recovery_summary.json` | Peak values, outcome, fragmentation duration |
| `latency_distribution.csv` | Latency samples for histogram construction |
| `retry_storms.csv` | Retry volume over time |
| `contention_decay.csv` | Queue pressure decay post-recovery |
| `replays/<id>.json` | Full replay metadata for deterministic re-run |

---

## Reproducibility

**Every run is fully deterministic under fixed seeds.** No runtime-generated randomness outside the seeded RNG.

```bash
# Deterministic run via seed override
CASCADE_SEED=42 python run_v2.py --config configs/recovery_test.json --engine-only

# Save replay, then re-run identically
python run_v2.py --config configs/recovery_test.json --replay-save seed42_test
python run_v2.py --replay replays/seed42_test.json

# Replay verification
python -c "
from replays.replay_manager import load_replay, run_replay
result = run_replay('replays/seed42_test.json', 'metrics_verify')
print('Deterministic:', result['verification']['deterministic'])
"
```

---

## Determinism Guarantee

The simulation engine uses a single `random.Random(seed)` instance initialized at construction. All state transitions — latency sampling, fragmentation spreading, recovery roll — are derived from this RNG. As long as the config is identical and the seed is fixed, the simulation produces byte-identical telemetry CSVs across runs, machines, and container restarts.

**No hardcoded runtime values.** All parameters come from `configs/recovery_test.json` (or a provided replay metadata file).

---

## Grafana Dashboard

Pre-provisioned via `monitoring/grafana/provisioning/`. Panels:

**Recovery Dynamics — Overview:**
- p95 Latency (ms) — time series, threshold lines at 100ms/200ms
- Retry Storm Volume — bar chart with spike alerts
- Fragmented Nodes — gauge (0–12)
- Stability Score — gauge (0–1) with color gradient

**Network Pressure:**
- Aggregate Queue Depth — time series
- Edge Congestion Heat (avg + peak) — dual time series

**Simulation Health:**
- Global Health Score — gauge (0–1) with thresholds
- Active vs Total Nodes — step-before time series
- Request Rate (success vs fail) — rate/15s

Refresh: **1 second** (matches Prometheus scrape interval)

---

## Docker Operations

```bash
# Full stack
make up

# Engine only (iterate fast)
make engine-only

# Engine + metrics server
make engine-metrics

# Standalone metrics server
make metric-server

# Topology benchmarks
make test-benchmarks

# Tail logs
make logs-engine

# Stop
make down

# Full cleanup (including volumes)
make clean-all
```

---

## Research Applications

**Retry Storm Analysis** — Observe how retry amplification spikes latency beyond fragmentation thresholds. Run `experiments/topology_benchmarks.py --nodes 12 --seeds 42 1337 2023 7` to compare across topologies.

**Fragmentation Propagation** — Track how latency failures cascade through topological neighbors. Mesh topology vs ring vs scale-free show measurably different fragmentation spread patterns.

**Recovery Oscillation Detection** — Identify the oscillation outcome class (0.3 < stability ≤ 0.6, retry_volume > 15) which indicates the system is caught in a retry/fragmentation loop without full recovery.

**Deterministic Chaos Experimentation** — Run a replay with a modified config (e.g., lower `recovery_rate`) and compare outcomes. The replay system guarantees identical failure injection timing.

---

## Observability Stack

```
Prometheus ─── scrape_interval: 1s ─── simulation-engine:9090/metrics
     │
     ├── 15d retention (storage.tsdb.retention.time=15d)
     ├── alerting/alerts.yml (11 rules)
     └── alertmanager:9093

Grafana ─── Prometheus datasource ─── Recovery Dynamics dashboard
     └── pre-provisioned via grafana/provisioning/

Jaeger ─── OTLP gRPC :4317 ─── trace spans from engine + services
     └── Jaeger UI: http://localhost:16686
```