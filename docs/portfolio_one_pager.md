# The Cascade — Distributed Systems Resilience Platform
**An AI Engineering Portfolio Artifact**

---

## Vision

The Cascade began as a question: *what happens to a distributed coordination system when retry storms, latency cascades, and topology-induced fragmentation interact simultaneously?* Rather than approximating an answer analytically, we built a telemetry-driven simulation engine that produces reproducible, instrumented failure events — then applied supervised learning to extract predictive signal from the noise.

The result is a **structured resilience intelligence platform** combining deterministic simulation, phase-transition detection, topology-aware classification, and XGBoost-based failure prediction with SHAP explainability. The work is designed to transfer directly to production AI systems: real-time anomaly detection, adaptive backoff, and self-healing multi-agent pipelines.

---

## What We Built

### Simulation Engine
A deterministic, seeded mesh-network simulator modeling failure injection, retry amplification, fragmentation propagation, and recovery dynamics across 4 topologies (mesh, ring, scale-free, hierarchical). Each run produces tick-by-tick telemetry: 15 fields including latency, queue depth, retry volume, fragmented node count, stability score, and global health.

**Key properties:**
- Fully deterministic under fixed seeds — byte-identical outputs across machines
- 6-class outcome taxonomy: `full_recovery`, `partial_recovery`, `oscillatory_instability`, `secondary_collapse`, `cascading_fragmentation`, `unrecoverable_partition`
- 7-phase state machine: `stable → degradation → fragmentation → recovery_attempt → stabilized → unstable_equilibrium → collapse`
- Phase transitions exported to `metrics/phase_transitions.csv` for downstream ML

### ML Pipeline
XGBoost classifier trained on 64 simulation runs (96,000 samples, 37,200 critical examples) to predict `oscillatory_instability` and `secondary_collapse` 15–30 ticks in advance.

**Validation:** 5-fold GroupKFold (grouped by run_id) — no temporal leakage, each fold trains on ~51 runs and tests on ~13, both outcome types present in each fold.

| Metric | Mean ± Std |
|---|---|
| AUC-ROC | 0.7649 ± 0.0386 |
| Avg Precision | 0.6862 ± 0.1166 |
| Critical Recall | 0.6178 ± 0.1458 |

**SHAP Analysis:** `topology_id` (0.496) is the dominant failure signal — hub-based topologies (scale_free, hierarchical) fragment faster than connected topologies (mesh, ring). `since_stable` (0.329) is the primary temporal precursor. `roll_retry_count_std` (0.178) captures oscillation amplitude in retry volume.

### Observability Stack
Prometheus + Grafana pre-provisioned with 3 dashboards (Recovery Dynamics, Resilience Intelligence Research, Resilience Rankings), 16 alerting rules, and 3 alert groups. Prometheus recording rules compute resilience rankings, outcome distributions, fragmentation duration histograms, and topology-specific failure rates in real time.

---

## Technical Highlights

| Category | Detail |
|---|---|
| **Simulation Engine** | Seeded deterministic engine, 4 topologies, 6 outcome classes, 7-phase detection |
| **Telemetry** | 15 fields per tick, 20-tick rolling window features, per-run CSV exports |
| **Stability Mapping** | 2D parameter-space heatmaps: stable/unstable/oscillation/fragmentation/collapse regions |
| **Failure Prediction** | XGBoost + SHAP, 18 engineered features, GroupKFold CV, AUC-ROC 0.76 |
| **Dashboards** | Grafana provisioning via config-as-code, 3 dashboards, 16 alerting rules |
| **Docker** | Full stack: Prometheus + Grafana + Jaeger + Alertmanager |
| **CI/CD** | GitHub Actions, deterministic pytest suite, pytest-cov integration |
| **Reports** | Structured research summaries (topology weakness, failure modes, retry amplification) |

---

## AI Contribution

The ML layer adds a forward-looking capability that the simulation alone cannot provide: **prediction before the cascade completes.** By training on phase-transition sequences and rolling telemetry windows, the model learns to recognize the signature patterns that precede oscillatory instability:

1. **`topology_id` encodes structural fragility** — the model's dependence on topology confirms that coordination failure is topology-structured, not purely parameter-structured. Hub removal in scale-free networks disconnects all spokes simultaneously, a pattern the model learns to associate with `secondary_collapse`.

2. **`since_stable` is a learned alarm threshold** — the model discovered that sustained instability (>~40 ticks without stability > 0.95) is the primary oscillation precursor. This is a learned equivalent to an SLO burn rate alert.

3. **`roll_retry_count_std` detects oscillation waves** — variance in retry volume flags unstable recovery attempts before the system commits to a fragmentation cascade.

4. **Explainability via SHAP** — SHAP dependence plots show how each feature shifts the failure probability for specific topologies, making the model's decisions auditable for human-in-the-loop override in production autonomous fault management systems.

---

## Results

- **64 simulation runs** across 3 batch experiments (May 2026), 96,000 labeled samples
- **22/64 runs (34.4%)** resulted in critical failure (`oscillatory_instability`)
- **Scale-free + hierarchical** topologies show measurably higher failure rates than mesh + ring (hub connectivity is the primary structural risk factor)
- **XGBoost AUC-ROC 0.76** via 5-fold GroupKFold — model generalizes across unseen topology/parameter configurations
- **Stability maps** exported to `stability_maps/` and `resilience_frontiers/` for parameter sensitivity analysis
- **All outputs deterministic** — same seed + config produces byte-identical telemetry

---

## Relevance to Production AI Systems

The failure patterns this simulation models are structurally identical to real-world coordination failures in:

- **LLMOps batch schedulers** (vLLM, TGI, Ollama): Retry storms under GPU backpressure match the `oscillatory_instability` taxonomy exactly
- **Multi-agent orchestration** (LangGraph, AutoGen, CrewAI): Agent consensus failure under skewed task distribution produces oscillation patterns indistinguishable from the simulation's retry wave dynamics
- **Distributed inference serving**: Fragmentation cascades when one node's latency spikes cause request rerouting that overwhelms neighboring nodes

The pipeline is designed for extension: the same CSV export format that trains the current XGBoost model can consume live Prometheus metrics from a running inference cluster, enabling **real-time failure prediction in production** with the SHAP explainability layer providing human-readable rationale for autonomous remediation decisions.

---

## Quick Reference

```bash
# Run simulation
python run_v2.py --config configs/recovery_test.json --enable-metrics

# Run ML batch
python experiments/monte_carlo_runner.py --experiments 32

# Execute prediction notebook
jupyter nbconvert --to notebook --execute --inplace \
    experiments/ai_failure_prediction.ipynb

# Start observability stack
docker-compose up -d

# Run tests
pytest tests/ -v --cov=simulations --cov=experiments
```

**Repository:** github.com/alpercaglar11-hub/the-cascade-simulation  
**Branch for AI work:** `resilience-intelligence`
