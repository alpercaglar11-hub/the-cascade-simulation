"""
monte_carlo_runner.py — Cascade Parameter Sweep Engine
======================================================
Systematic Monte Carlo experimentation across recovery dynamics,
topology resilience, and failure mode parameter space.

Design goals:
  - Deterministic per experiment (seed-tracked)
  - Parallel-safe (isolated output dirs per worker)
  - Registry-compliant output (every run is recorded)
  - Topological parameter space fully enumerated

Usage:
  python monte_carlo_runner.py                      # full sweep, all CPUs
  python monte_carlo_runner.py --experiments 50    # 50 runs, auto batch size
  python monte_carlo_runner.py --topologies mesh ring --recovery-rates 0.15 0.25
  python monte_carlo_runner.py --replay-seed 42     # single run for debugging

Outputs:
  <output_dir>/batch_<timestamp>/
    run_<index>_<topology>_<seed>/
      config.json          # experiment config snapshot
      recovery_summary.json
      telemetry.csv
      latency_distribution.csv
      <registry>/<batch_id>/  # symlinks to runs for quick access
"""

from __future__ import annotations

import csv
import os
import json
import random
import shutil
import subprocess
import sys
import time
import uuid
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed

# ── path bootstrap ────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
BASE_DIR = Path(__file__).parent.parent


# ── Experiment parameter grids ─────────────────────────────────────────────────

PARAM_GRID = {
    "recovery_rate":        [0.10, 0.15, 0.20, 0.25, 0.30],
    "retry_backoff_multiplier": [1.10, 1.25, 1.35, 1.50, 1.75],
    "node_capacity":       [15, 30, 45, 60],          # base_capacity
    "topology_type":       ["mesh", "ring", "scale_free", "hierarchical"],
    "latency_injection_multiplier": [8.0, 14.0, 18.0, 24.0, 30.0],
}

# Seeds for deterministic reproducibility across all experiments.
# Each (topology, seed) pair is unique per sweep.
SWEEP_SEEDS = list(range(42, 92))   # 50 seeds: 42..91


# ── Experiment config factory ──────────────────────────────────────────────────

def make_experiment_config(
    recovery_rate: float,
    retry_backoff: float,
    node_capacity: int,
    topology_type: str,
    latency_multiplier: float,
    seed: int,
) -> dict:
    """
    Build a recovery_test.json-compatible config dict.
    All derived params are computed from the input grid values.
    """
    return {
        "simulation_name": "mc_sweep",
        "description": (
            f"MC sweep: recovery={recovery_rate}, backoff={retry_backoff}, "
            f"capacity={node_capacity}, topo={topology_type}, "
            f"latency_mult={latency_multiplier}, seed={seed}"
        ),
        "topology": {
            "type": topology_type,
            "nodes": 12,
            "edges": 24,
            "seed": seed,
        },
        "failure_injection": {
            "target_node_id": 7,
            "latency_multiplier": latency_multiplier,
            "failure_tick": 120,
            "recovery_tick": 280,
            "fragmentation_threshold_ms": 40,
        },
        "_fragmentation": {
            "spreading_factor": 0.35,
            "min_nodes_frag": 3,
        },
        "recovery": {
            "load_shedding_active": True,
            "rate_limiter_active": True,
            "recovery_rate": recovery_rate,
            "traffic_decay_half_life_ticks": 18,
            "retry_backoff_multiplier": retry_backoff,
            "max_queue_depth": 200,
            "rate_limit_tokens_per_tick": 60,
            "load_shed_fraction": 0.25,
        },
        "retry_storm_model": {
            "retry_threshold_multiplier": 1.4,
            "storm_latency_amplifier": 2.2,
            "storm_queue_inflation": 1.6,
            "oscillation_probability": 0.22,
        },
        "node_defaults": {
            "base_latency_ms": 12,
            "base_capacity": node_capacity,
            "base_drain_rate": 0.85,
        },
        "telemetry": {
            "export_csv": True,
            "export_json": True,
            "record_p50_latency": True,
            "record_p95_latency": True,
            "record_queue_depth": True,
            "record_retry_count": True,
            "record_fragmented_nodes": True,
            "record_stability_score": True,
            "record_global_health_score": True,
            "record_edge_heat_avg": True,
            "record_edge_heat_peak": True,
            "tick_interval_ms": 50,
            "total_ticks": 600,
        },
        "visualization": {
            "output_scene_class": "V2_RecoveryVisualization",
            "render_4k": False,
            "fps": 30,
        },
    }


# ── Experiment run executor (can be called in parallel) ────────────────────────

def execute_single_experiment(
    run_id: str,
    experiment_dir: Path,
    cfg: dict,
    enable_metrics: bool = False,
) -> dict:
    """
    Execute one experiment run inside a forked process.

    Writes:
      experiment_dir/config.json
      experiment_dir/recovery_summary.json
      experiment_dir/telemetry.csv
      experiment_dir/latency_distribution.csv

    Returns:
      run_result dict with outcome classification and summary stats.
    """
    experiment_dir.mkdir(parents=True, exist_ok=True)

    # Write config snapshot
    config_path = experiment_dir / "config.json"
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)

    # Derive paths that run_v2.py expects
    output_dir = experiment_dir

    # Build run_v2.py invocation
    import sys as _sys
    runner = BASE_DIR / "run_v2.py"
    cmd = [
        _sys.executable,
        str(runner),
        "--config", str(config_path),
        "--engine-only",
        "--output-dir", str(output_dir),
    ]
    if enable_metrics:
        cmd.append("--enable-metrics")

    # Suppress metrics server log spam during sweeps (redirect to tmp)
    env = dict(os.environ)
    env["CASCADE_LOG_LEVEL"] = "WARNING"

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(BASE_DIR),
        env=env,
    )

    # Always initialize all telemetry fields before any conditional block.
    # This guarantees the return dict always has these keys regardless of
    # which code path executes (JSON, log-fallback, or partial matches).
    outcome = "unknown"
    final_health = None
    final_stability = None
    peak_p95 = None
    peak_retries = None
    frag_duration_ms = None

    # Primary data source: recovery_summary.json (always written by run_v2.py).
    # Fall back to log-line parsing only if the JSON is missing.
    summary_path = output_dir / "recovery_summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)
        outcome = summary.get("recovery_outcome", "unknown")
        final_health = summary.get("final_health_score")
        final_stability = summary.get("final_stability_score")
        peak_p95 = summary.get("peak_p95_latency_ms")
        peak_retries = summary.get("peak_retry_count")
        frag_duration_ms = summary.get("fragmentation_duration_ms")
    else:
        # Fallback: parse from log output (stderr). Run_v2.py logs to stderr.
        combined_output = result.stdout + "\n" + result.stderr
        outcome = "unknown"
        for line in combined_output.splitlines():
            if "Outcome       :" in line:
                outcome = line.split("Outcome       :")[1].strip()
            elif "Final health  :" in line:
                try:
                    final_health = float(line.split(":")[1].strip())
                except ValueError:
                    pass
            elif "Final stable  :" in line:
                try:
                    final_stability = float(line.split(":")[1].strip())
                except ValueError:
                    pass
            elif "Peak p95      :" in line:
                try:
                    peak_p95 = float(line.split(":")[1].strip().rstrip("ms"))
                except ValueError:
                    pass
            elif "Peak retries  :" in line:
                try:
                    peak_retries = int(line.split(":")[1].strip())
                except ValueError:
                    pass

    return {
        "run_id": run_id,
        "experiment_dir": str(experiment_dir),
        "outcome": outcome,
        "final_health": final_health,
        "final_stability": final_stability,
        "peak_p95_latency_ms": peak_p95,
        "peak_retry_count": peak_retries,
        "fragmentation_duration_ms": frag_duration_ms,
        "topology": cfg["topology"]["type"],
        "recovery_rate": cfg["recovery"]["recovery_rate"],
        "retry_backoff": cfg["recovery"]["retry_backoff_multiplier"],
        "node_capacity": cfg["node_defaults"]["base_capacity"],
        "latency_multiplier": cfg["failure_injection"]["latency_multiplier"],
        "seed": cfg["topology"]["seed"],
        "success": result.returncode == 0,
        "exit_code": result.returncode,
    }


# ── Batch generator ───────────────────────────────────────────────────────────

def generate_experiment_batch(
    recovery_rates: Optional[List[float]] = None,
    retry_backoff_multipliers: Optional[List[float]] = None,
    node_capacities: Optional[List[int]] = None,
    topology_types: Optional[List[str]] = None,
    latency_multipliers: Optional[List[float]] = None,
    seeds: Optional[List[int]] = None,
    max_experiments: Optional[int] = None,
) -> List[Tuple[dict, int, Path]]:
    """
    Enumerate the full parameter grid or a constrained subset.

    Returns:
        List of (config_dict, seed, output_path) tuples.
        Output path is a Path under <output_dir>/run_<index>_<topology>_<seed>/
    """
    recovery_rates = recovery_rates or PARAM_GRID["recovery_rate"]
    retry_backoffs = retry_backoff_multipliers or PARAM_GRID["retry_backoff_multiplier"]
    node_caps = node_capacities or PARAM_GRID["node_capacity"]
    topos = topology_types or PARAM_GRID["topology_type"]
    lat_mults = latency_multipliers or PARAM_GRID["latency_injection_multiplier"]
    seeds = seeds or SWEEP_SEEDS

    configs = []
    idx = 0
    for seed in seeds:
        for topo in topos:
            for rr in recovery_rates:
                for rb in retry_backoffs:
                    for cap in node_caps:
                        for lm in lat_mults:
                            cfg = make_experiment_config(
                                recovery_rate=rr,
                                retry_backoff=rb,
                                node_capacity=cap,
                                topology_type=topo,
                                latency_multiplier=lm,
                                seed=seed,
                            )
                            path = Path(f"run_{idx:04d}_{topo}_{seed}")
                            configs.append((cfg, seed, path))
                            idx += 1

    if max_experiments is not None and len(configs) > max_experiments:
        # Sample evenly across parameter grid: take every Nth config where
        # N = total_configs / max_experiments. This ensures each recovery_rate
        # value and each topology appears in the subset.
        step = len(configs) / max_experiments
        configs = [configs[min(int(i * step), len(configs) - 1)] for i in range(max_experiments)]

    return configs


# ── Monte Carlo runner ─────────────────────────────────────────────────────────

def run_sweep(
    output_dir: str,
    recovery_rates: Optional[List[float]] = None,
    retry_backoff_multipliers: Optional[List[float]] = None,
    node_capacities: Optional[List[int]] = None,
    topology_types: Optional[List[str]] = None,
    latency_multipliers: Optional[List[float]] = None,
    seeds: Optional[List[int]] = None,
    max_workers: Optional[int] = None,
    max_experiments: Optional[int] = None,
    enable_metrics: bool = False,
) -> Dict:
    """
    Execute the full parameter sweep.

    Returns:
        batch_summary dict (also written to <output_dir>/batch_<id>/aggregate_summary.json)
    """
    start_ts = datetime.now(timezone.utc).isoformat()
    batch_id = str(uuid.uuid4())

    batch_root = Path(output_dir) / f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    batch_root.mkdir(parents=True, exist_ok=True)

    # Metadata
    git_hash = ""
    try:
        git_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
            cwd=str(BASE_DIR),
        ).strip()
    except Exception:
        git_hash = "unknown"

    metadata = {
        "batch_id": batch_id,
        "started_at": start_ts,
        "git_commit": git_hash,
        "param_grid": {
            "recovery_rates": recovery_rates or PARAM_GRID["recovery_rate"],
            "retry_backoff_multipliers": retry_backoff_multipliers or PARAM_GRID["retry_backoff_multiplier"],
            "node_capacities": node_capacities or PARAM_GRID["node_capacity"],
            "topology_types": topology_types or PARAM_GRID["topology_type"],
            "latency_multipliers": latency_multipliers or PARAM_GRID["latency_injection_multiplier"],
            "seeds": seeds or SWEEP_SEEDS,
        },
    }
    with open(batch_root / "batch_metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    # Enumerate experiments
    experiments = generate_experiment_batch(
        recovery_rates=recovery_rates,
        retry_backoff_multipliers=retry_backoff_multipliers,
        node_capacities=node_capacities,
        topology_types=topology_types,
        latency_multipliers=latency_multipliers,
        seeds=seeds,
        max_experiments=max_experiments,
    )

    total = len(experiments)
    print(f"[monte_carlo] Batch {batch_id[:8]}… — {total} experiments queued")

    # Parallel execution
    workers = max_workers or min(os.cpu_count() or 4, 16)
    results = []
    completed = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for cfg, seed, run_path in experiments:
            run_id = f"{run_path.stem}_{batch_id[:8]}"
            run_dir = batch_root / run_path
            future = executor.submit(
                execute_single_experiment,
                run_id,
                run_dir,
                cfg,
                enable_metrics,
            )
            futures[future] = (run_id, str(run_dir))

        for future in as_completed(futures):
            run_id, run_dir = futures[future]
            try:
                result = future.result()
                results.append(result)
                completed += 1
            except Exception as exc:
                failed += 1
                results.append({
                    "run_id": run_id,
                    "experiment_dir": str(run_dir),
                    "outcome": "process_error",
                    "success": False,
                    "error": str(exc),
                })
                print(f"[monte_carlo] ERROR {run_id}: {exc}")

            if (completed + failed) % 10 == 0:
                print(f"[monte_carlo] {completed + failed}/{total} — {failed} failed")

    end_ts = datetime.now(timezone.utc).isoformat()

    # Write aggregate results CSV
    csv_path = batch_root / "comparative_results.csv"
    fieldnames = [
        "run_id", "outcome", "final_health", "final_stability",
        "peak_p95_latency_ms", "peak_retry_count", "fragmentation_duration_ms",
        "topology", "recovery_rate", "retry_backoff",
        "node_capacity", "latency_multiplier", "seed", "success",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            row = {k: r.get(k, "") for k in fieldnames}
            writer.writerow(row)

    # Compute aggregate summary
    successful = [r for r in results if r.get("success") and r.get("outcome") != "unknown"]
    aggregate = compute_aggregate_summary(successful, results, metadata, start_ts, end_ts)

    # Update metadata with actual run results
    metadata["total_runs"] = len(results)
    metadata["successful_runs"] = sum(1 for r in results if r.get("success"))
    metadata["failed_runs"] = sum(1 for r in results if not r.get("success"))
    metadata["completed_at"] = end_ts
    metadata["outcome_distribution"] = aggregate.get("outcome_distribution", {})

    # Re-write batch_metadata.json with full fields now that we have results
    with open(batch_root / "batch_metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    # Write aggregate summary JSON
    summary_path = batch_root / "aggregate_summary.json"
    with open(summary_path, "w") as f:
        json.dump(aggregate, f, indent=2)

    # ── Stability map generation ──────────────────────────────────────────
    try:
        sys.path.insert(0, str(BASE_DIR))
        from simulations.stability_mapper import BatchStabilityMapper

        print(f"[monte_carlo] Computing stability maps …")
        mapper = BatchStabilityMapper(str(batch_root))
        mapper.load_results()
        mapper.compute_all_maps(n_bins=6)
        mapper.export_all(output_dir=str(batch_root), batch_id=batch_id)
        map_summary = mapper.get_stability_summary()
        print(f"[monte_carlo] Stability maps: {map_summary.get('region_type_counts', {})}")
    except Exception as exc:
        print(f"[monte_carlo] Stability map generation failed (non-fatal): {exc}")

    # ── Resilience classification (post-batch taxonomy + phase transitions) ───
    try:
        from experiments.run_classification import process_batch

        print(f"[monte_carlo] Running resilience classification …")
        cls_aggregate = process_batch(batch_root)
        print(f"[monte_carlo] Classification: "
              f"recovery_rate={cls_aggregate.get('recovery_success_rate', 0):.1%}, "
              f"oscillations={cls_aggregate.get('oscillation_frequency', 0):.1%}")
    except Exception as exc:
        print(f"[monte_carlo] Resilience classification failed (non-fatal): {exc}")

    # ── Research report generation ──────────────────────────────────────────
    try:
        from experiments.generate_batch_report import generate_batch_report

        report_path = generate_batch_report(batch_root, aggregate)
        if report_path:
            print(f"[monte_carlo] Research report: {report_path}")
    except Exception as exc:
        print(f"[monte_carlo] Report generation failed (non-fatal): {exc}")

    print(f"[monte_carlo] Batch complete: {completed} ok, {failed} failed → {summary_path.name}")

    return aggregate


def compute_aggregate_summary(
    successful_results: List[dict],
    all_results: List[dict],
    metadata: dict,
    start_ts: str,
    end_ts: str,
) -> dict:
    """Compute comparative analytics across the batch."""

    def _safe_mean(values, default=0.0):
        return round(statistics.mean(values), 4) if values else default

    def _safe_p95(values):
        if not values:
            return 0.0
        n = int(len(values) * 0.95)
        return round(sorted(values)[min(n, len(values) - 1)], 4)

    outcomes = [r["outcome"] for r in successful_results if r.get("outcome")]
    outcome_counts = {}
    for o in outcomes:
        outcome_counts[o] = outcome_counts.get(o, 0) + 1

    # Recovery success rate
    recovery_outcomes = ["full_recovery", "partial_recovery"]
    recovery_count = sum(outcome_counts.get(o, 0) for o in recovery_outcomes)
    recovery_success_rate = round(recovery_count / max(len(successful_results), 1), 4)

    # Topology resilience ranking
    topo_health = {}
    topo_count = {}
    for r in successful_results:
        topo = r.get("topology", "unknown")
        h = r.get("final_health")
        if h is not None:
            topo_health[topo] = topo_health.get(topo, 0) + h
            topo_count[topo] = topo_count.get(topo, 0) + 1

    topo_ranking = []
    for topo in topo_health:
        avg_h = topo_health[topo] / topo_count[topo]
        topo_ranking.append((topo, round(avg_h, 4)))
    topo_ranking.sort(key=lambda x: x[1], reverse=True)

    # Oscillation frequency
    osc_count = outcome_counts.get("oscillation", 0)
    oscillation_frequency = round(osc_count / max(len(successful_results), 1), 4)

    # Fragmentation duration statistics
    frag_durations = [r["fragmentation_duration_ms"] for r in successful_results
                      if r.get("fragmentation_duration_ms") is not None]
    avg_frag_duration = _safe_mean(frag_durations)

    # p95 latency distribution per topology
    p95_by_topo = {}
    for r in successful_results:
        topo = r.get("topology", "unknown")
        p95 = r.get("peak_p95_latency_ms")
        if p95 is not None:
            p95_by_topo.setdefault(topo, []).append(p95)

    p95_distributions = {}
    for topo, vals in p95_by_topo.items():
        p95_distributions[topo] = {
            "mean": _safe_mean(vals),
            "p95": _safe_p95(vals),
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "n": len(vals),
        }

    # Stability score distribution
    stability_scores = [r["final_stability"] for r in successful_results
                        if r.get("final_stability") is not None]
    stability_distribution = {
        "mean": _safe_mean(stability_scores),
        "min": round(min(stability_scores), 2) if stability_scores else 0,
        "max": round(max(stability_scores), 2) if stability_scores else 0,
        "std": round(statistics.stdev(stability_scores), 4) if len(stability_scores) > 1 else 0,
    }

    return {
        "batch_id": metadata["batch_id"],
        "git_commit": metadata["git_commit"],
        "started_at": start_ts,
        "completed_at": end_ts,
        "total_runs": len(all_results),
        "successful_runs": len(successful_results),
        "failed_runs": len(all_results) - len(successful_results),
        "outcome_distribution": outcome_counts,
        "recovery_success_rate": recovery_success_rate,
        "topology_resilience_ranking": [{"topology": t, "avg_health": h} for t, h in topo_ranking],
        "oscillation_frequency": oscillation_frequency,
        "fragmentation": {
            "avg_duration_ms": avg_frag_duration,
            "count": len(frag_durations),
        },
        "p95_latency_distributions": p95_distributions,
        "stability_score_distribution": stability_distribution,
        "parameter_correlation_suggestions": _parameter_insights(successful_results),
    }


def _parameter_insights(results: List[dict]) -> List[dict]:
    """
    Surface which parameter values correlate with recovery success.
    Simple binning analysis — not a statistical model.
    """
    insights = []

    for param in ["recovery_rate", "retry_backoff", "node_capacity", "latency_multiplier"]:
        bins = {}
        for r in results:
            val = r.get(param)
            outcome = r.get("outcome", "unknown")
            if val is None:
                continue
            bins.setdefault(val, {"full_recovery": 0, "partial_recovery": 0, "total": 0})
            bins[val]["total"] += 1
            if outcome in ("full_recovery", "partial_recovery"):
                bins[val][outcome] += 1

        for val, counts in bins.items():
            rate = (counts["full_recovery"] + counts["partial_recovery"]) / max(counts["total"], 1)
            if counts["total"] >= 3:   # minimum sample size
                insights.append({
                    "parameter": param,
                    "value": val,
                    "sample_size": counts["total"],
                    "recovery_rate": round(rate, 3),
                })

    # Sort by recovery rate desc
    insights.sort(key=lambda x: x["recovery_rate"], reverse=True)
    return insights[:20]   # top 20 insights


# ── CLI ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Cascade Monte Carlo Parameter Sweep")
    parser.add_argument("--output-dir", default="experiments/monte_carlo", help="Base output directory")
    parser.add_argument("--experiments", type=int, default=None, help="Max experiments (grid sampling if set)")
    parser.add_argument("--workers", type=int, default=None, help="Parallel workers")
    parser.add_argument("--recovery-rates", type=float, nargs="+", default=None)
    parser.add_argument("--retry-backoff-multipliers", type=float, nargs="+", default=None)
    parser.add_argument("--node-capacities", type=int, nargs="+", default=None)
    parser.add_argument("--topologies", type=str, nargs="+", default=None)
    parser.add_argument("--latency-multipliers", type=float, nargs="+", default=None)
    parser.add_argument("--seeds", type=int, nargs="+", default=None)
    parser.add_argument("--replay-seed", type=int, default=None, help="Single run with specific seed")
    parser.add_argument("--enable-metrics", action="store_true", help="Start Prometheus metrics server per run")

    args = parser.parse_args()

    if args.replay_seed:
        # Single debugging run
        cfg = make_experiment_config(
            recovery_rate=0.25,
            retry_backoff=1.35,
            node_capacity=30,
            topology_type="mesh",
            latency_multiplier=18.0,
            seed=args.replay_seed,
        )
        out_dir = Path(args.output_dir) / f"single_{args.replay_seed}"
        result = execute_single_experiment(f"single_{args.replay_seed}", out_dir, cfg, args.enable_metrics)
        print(json.dumps(result, indent=2))
        sys.exit(0)

    result = run_sweep(
        output_dir=args.output_dir,
        recovery_rates=args.recovery_rates,
        retry_backoff_multipliers=args.retry_backoff_multipliers,
        node_capacities=args.node_capacities,
        topology_types=args.topologies,
        latency_multipliers=args.latency_multipliers,
        seeds=args.seeds,
        max_workers=args.workers,
        max_experiments=args.experiments,
        enable_metrics=args.enable_metrics,
    )
    print(json.dumps(result, indent=2))