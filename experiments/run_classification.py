"""
run_classification.py — Cascade Post-Batch Resilience Classifier
==================================================================
Consumes a completed experiment batch directory and runs OutcomeClassifier
+ PhaseDetector on each run's telemetry.csv, producing per-run resilience
artifacts and aggregate taxonomy summaries.

This script is idempotent — running it multiple times on the same batch
produces identical outputs (deterministic classification of stored telemetry).

Usage:
  # Full batch
  python experiments/run_classification.py experiments/monte_carlo/batch_<id>/

  # Single run (for debugging)
  python experiments/run_classification.py experiments/monte_carlo/batch_<id>/run_0000_mesh_42/

  # Specific batch
  python experiments/run_classification.py --batch-id 0b2081bf-c7a5-49e2-907f-a1091811a8ae

Outputs (written to batch_dir/):
  classification_summary.json       — per-run outcome + stability signatures
  aggregate_taxonomy_summary.json   — batch-level taxonomy counts + topology breakdown
  phase_boundary_data.csv           — all detected phase boundary coordinates
  stability_region_map.csv           — parameter-space cell classifications
  failed_runs.json                  — runs where classification errored (for audit)

Exit codes:
  0  — classification complete
  1  — batch directory not found
  2  — no completed runs found
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from simulations.resilience_taxonomy import (
    OutcomeClassifier,
    PhaseDetector,
    classify_and_analyze,
    OUTCOME_CLASSES,
)
from simulations.recovery_engine import SimulationMetrics
from simulations.stability_mapper import BatchStabilityMapper


# ── Telemetry loader ──────────────────────────────────────────────────────────

def load_telemetry_metrics(csv_path: Path) -> List[SimulationMetrics]:
    """
    Parse a telemetry.csv into a list of SimulationMetrics.
    Safe: missing optional fields default to 0 or None.
    """
    if not csv_path.exists():
        return []

    rows: List[SimulationMetrics] = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                m = SimulationMetrics(
                    tick=int(row.get("tick", 0) or 0),
                    time_ms=float(row.get("time_ms", 0) or 0),
                    active_nodes=int(row.get("active_nodes", 12) or 12),
                    fragmented_nodes=int(row.get("fragmented_nodes", 0) or 0),
                    retry_count=int(row.get("retry_count", 0) or 0),
                    stability_score=float(row.get("stability_score", 1.0) or 1.0),
                    global_health_score=float(row.get("global_health_score", 1.0) or 1.0),
                    p50_latency=float(row.get("p50_latency", 0.0) or 0.0),
                    p95_latency=float(row.get("p95_latency", 0.0) or 0.0),
                    queue_depth=int(row.get("queue_depth", 0) or 0),
                    edge_heat_avg=float(row.get("edge_heat_avg", 0.0) or 0.0),
                    edge_heat_peak=float(row.get("edge_heat_peak", 0.0) or 0.0),
                    recovery_outcome=row.get("recovery_outcome", "unknown"),
                    total_requests=int(row.get("total_requests", 0) or 0),
                    failed_requests=int(row.get("failed_requests", 0) or 0),
                )
                rows.append(m)
            except Exception as exc:
                # Log the first few failures for debugging; stop on structural mismatches
                if len(rows) == 0 and row:
                    import sys as _sys
                    _sys.stderr.write(f"[load_telemetry_metrics] skip row tick={row.get('tick')}: {exc}\n")
    return rows


# ── Per-run classifier ─────────────────────────────────────────────────────────

def classify_run(run_dir: Path) -> Tuple[Optional[dict], Optional[str]]:
    """
    Classify a single experiment run.

    Returns:
        (result_dict, error_message)
        result_dict is None on failure.
    """
    config_path = run_dir / "config.json"
    telemetry_path = run_dir / "telemetry.csv"

    if not config_path.exists():
        return None, "config.json not found"
    if not telemetry_path.exists():
        return None, "telemetry.csv not found"

    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except Exception as exc:
        return None, f"config.json parse error: {exc}"

    metrics = load_telemetry_metrics(telemetry_path)
    if not metrics:
        return None, "telemetry.csv empty or unparseable"

    # Create per-run resilience directory for phase exports
    resilience_dir = str(run_dir / "resilience")
    os.makedirs(resilience_dir, exist_ok=True)

    try:
        outcome, evidence, transitions = classify_and_analyze(metrics, cfg, output_dir=resilience_dir)

        sig = evidence.stability_signature
        return {
            "run_id": run_dir.name,
            "outcome": outcome,
            "primary_signal": evidence.primary_signal,
            "secondary_signals": evidence.secondary_signals,
            "violating_thresholds": evidence.violating_thresholds,
            "phase_sequence": evidence.phase_sequence,
            "topology": cfg.get("topology", {}).get("type", "unknown"),
            "recovery_rate": cfg.get("recovery", {}).get("recovery_rate", None),
            "retry_backoff": cfg.get("recovery", {}).get("retry_backoff_multiplier", None),
            "node_capacity": cfg.get("node_defaults", {}).get("base_capacity", None),
            "latency_multiplier": cfg.get("failure_injection", {}).get("latency_multiplier", None),
            "seed": cfg.get("topology", {}).get("seed", None),
            "signature": {
                "initial_stability": sig.initial_stability,
                "final_stability": sig.final_stability,
                "min_stability": sig.min_stability,
                "stability_trend": sig.stability_trend,
                "oscillation_count": sig.oscillation_count,
                "oscillation_amplitude": sig.oscillation_amplitude,
                "fragmentation_peak": sig.fragmentation_peak,
                "fragmentation_duration_ticks": sig.fragmentation_duration_ticks,
                "fragmentation_persistence": sig.fragmentation_persistence,
                "retry_peak": sig.retry_peak,
                "retry_storm_duration_ticks": sig.retry_storm_duration_ticks,
                "recovery_convergence_slope": sig.recovery_convergence_slope,
                "collapse_velocity": sig.collapse_velocity,
                "secondary_failure_tick": sig.secondary_failure_tick,
                "health_final": sig.health_final,
                "health_trend": sig.health_trend,
                "latency_p95_peak": sig.latency_p95_peak,
                "latency_p95_trend": sig.latency_p95_trend,
                "active_nodes_final": sig.active_nodes_final,
                "active_nodes_initial": sig.active_nodes_initial,
            },
            "final_health": sig.health_final,
            "final_stability": sig.final_stability,
            "fragmentation_duration_ms": sig.fragmentation_duration_ticks * 50,
        }, None

    except Exception as exc:
        tb = traceback.format_exc()
        return None, f"classification error: {exc}\n{tb}"


# ── Aggregate summary builder ─────────────────────────────────────────────────

def build_aggregate_summary(
    results: List[dict],
    failed_runs: List[dict],
    batch_id: str,
    batch_dir: Path,
) -> dict:
    """Compute batch-level taxonomy distribution and per-topology breakdowns."""

    def _safe_mean(values: List[float], default: float = 0.0) -> float:
        return round(statistics.mean(values), 4) if values else default

    def _safe_p95(values: List[float]) -> float:
        if not values:
            return 0.0
        n = int(len(values) * 0.95)
        return round(sorted(values)[min(n, len(values) - 1)], 4)

    # ── Outcome distribution ───────────────────────────────────────────────
    outcome_counts: Dict[str, int] = {oc: 0 for oc in OUTCOME_CLASSES}
    outcome_counts["process_error"] = 0
    outcome_counts["unknown"] = 0

    for r in results:
        o = r.get("outcome", "unknown")
        if o in outcome_counts:
            outcome_counts[o] += 1
        else:
            outcome_counts["unknown"] += 1

    outcome_counts["process_error"] = len(failed_runs)

    # ── Recovery success rate ───────────────────────────────────────────────
    recovery_outcomes = ["full_recovery", "partial_recovery"]
    recovery_count = sum(outcome_counts.get(o, 0) for o in recovery_outcomes)
    total_classified = sum(outcome_counts.values()) - outcome_counts.get("process_error", 0)
    recovery_success_rate = round(
        recovery_count / max(total_classified, 1), 4
    )

    # ── Topology resilience ranking ────────────────────────────────────────
    topo_health: Dict[str, List[float]] = {}
    for r in results:
        topo = r.get("topology", "unknown")
        h = r.get("final_health")
        if h is not None:
            topo_health.setdefault(topo, []).append(h)

    topo_ranking = []
    for topo, healths in topo_health.items():
        topo_ranking.append({
            "topology": topo,
            "avg_health": _safe_mean(healths),
            "n": len(healths),
        })
    topo_ranking.sort(key=lambda x: x["avg_health"], reverse=True)

    # ── Oscillation frequency ─────────────────────────────────────────────
    osc_count = sum(
        1 for r in results
        if r.get("signature", {}).get("oscillation_count", 0) >= 3
    )
    oscillation_frequency = round(osc_count / max(len(results), 1), 4)

    # ── Fragmentation statistics ───────────────────────────────────────────
    frag_durations = [
        r.get("fragmentation_duration_ms", 0) or 0
        for r in results
        if r.get("fragmentation_duration_ms") is not None
    ]
    frag_persistences = [
        r.get("signature", {}).get("fragmentation_persistence", 0)
        for r in results
    ]
    retry_amps = [
        (r.get("signature", {}).get("retry_peak", 0) or 0) /
        max(r.get("node_capacity", 1) or 1, 1)
        for r in results
    ]
    collapse_velocities = [
        r.get("signature", {}).get("collapse_velocity", 0)
        for r in results
        if r.get("signature", {}).get("collapse_velocity") is not None
    ]

    # ── p95 latency distribution per topology ─────────────────────────────
    p95_by_topo: Dict[str, List[float]] = {}
    for r in results:
        topo = r.get("topology", "unknown")
        lat = r.get("signature", {}).get("latency_p95_peak", 0) or 0
        if lat > 0:
            p95_by_topo.setdefault(topo, []).append(lat)

    p95_distributions = {}
    for topo, vals in p95_by_topo.items():
        p95_distributions[topo] = {
            "mean": _safe_mean(vals),
            "p95": _safe_p95(vals),
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "n": len(vals),
        }

    # ── Stability score distribution ─────────────────────────────────────
    stability_scores = [
        r.get("final_stability") for r in results
        if r.get("final_stability") is not None
    ]
    stability_distribution = {
        "mean": _safe_mean(stability_scores),
        "min": round(min(stability_scores), 2) if stability_scores else 0,
        "max": round(max(stability_scores), 2) if stability_scores else 0,
        "std": round(statistics.stdev(stability_scores), 4)
        if len(stability_scores) > 1 else 0.0,
    }

    # ── Outcome × topology matrix ─────────────────────────────────────────
    outcome_topo_matrix: Dict[str, Dict[str, int]] = {}
    for r in results:
        o = r.get("outcome", "unknown")
        t = r.get("topology", "unknown")
        outcome_topo_matrix.setdefault(o, {}).setdefault(t, 0)
        outcome_topo_matrix[o][t] += 1

    # ── Convergence slope by outcome ─────────────────────────────────────
    convergence_by_outcome: Dict[str, List[float]] = {}
    for r in results:
        o = r.get("outcome", "unknown")
        slope = r.get("signature", {}).get("recovery_convergence_slope")
        if slope is not None:
            convergence_by_outcome.setdefault(o, []).append(slope)

    convergence_summary = {
        o: {"mean": _safe_mean(v), "n": len(v)}
        for o, v in convergence_by_outcome.items()
    }

    # ── Phase transition stats ─────────────────────────────────────────────
    phase_seq_lengths: Dict[str, List[int]] = {}
    for r in results:
        seq = r.get("phase_sequence", [])
        if seq:
            phase_seq_lengths.setdefault("total_transitions", []).append(len(seq) - 1)
        for phase in seq:
            phase_seq_lengths.setdefault(phase, []).append(
                sum(1 for p in seq if p == phase)
            )

    return {
        "batch_id": batch_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_runs": len(results) + len(failed_runs),
        "classified_runs": len(results),
        "failed_runs": len(failed_runs),
        "outcome_distribution": outcome_counts,
        "recovery_success_rate": recovery_success_rate,
        "topology_resilience_ranking": topo_ranking,
        "oscillation_frequency": oscillation_frequency,
        "oscillation_count_total": osc_count,
        "fragmentation": {
            "avg_duration_ms": _safe_mean(frag_durations),
            "max_duration_ms": max(frag_durations) if frag_durations else 0,
            "min_duration_ms": min(frag_durations) if frag_durations else 0,
            "mean_persistence": _safe_mean(frag_persistences),
            "count": len(frag_durations),
        },
        "retry_amplification": {
            "mean_factor": _safe_mean(retry_amps),
            "max_factor": max(retry_amps) if retry_amps else 0,
        },
        "collapse_velocity": {
            "mean": _safe_mean(collapse_velocities),
            "min": min(collapse_velocities) if collapse_velocities else 0,
        },
        "p95_latency_distributions": p95_distributions,
        "stability_score_distribution": stability_distribution,
        "outcome_topo_matrix": outcome_topo_matrix,
        "convergence_by_outcome": convergence_summary,
        "phase_sequence_stats": {
            phase: {"mean_length": _safe_mean(lengths), "n": len(lengths)}
            for phase, lengths in phase_seq_lengths.items()
            if phase != "total_transitions"
        },
        "total_transitions_mean": _safe_mean(
            phase_seq_lengths.get("total_transitions", [])
        ),
    }


# ── Main batch processor ──────────────────────────────────────────────────────

def process_batch(batch_dir: Path) -> Dict:
    """
    Process all run subdirectories in a batch.

    Returns the aggregate taxonomy summary dict.
    """
    if not batch_dir.exists():
        raise FileNotFoundError(f"Batch directory not found: {batch_dir}")

    batch_id = batch_dir.name

    # Find run directories
    run_dirs = sorted(
        d for d in batch_dir.iterdir()
        if d.is_dir() and d.name.startswith("run_")
    )

    if not run_dirs:
        raise ValueError(f"No run directories found in {batch_dir}")

    print(f"[run_classification] Batch {batch_id[:8]}... — {len(run_dirs)} runs found")

    results: List[dict] = []
    failed_runs: List[dict] = []

    for run_dir in run_dirs:
        result, error = classify_run(run_dir)
        if result is not None:
            results.append(result)
            # Write per-run classification_summary.json inside the run dir
            summary_out = run_dir / "classification_summary.json"
            with open(summary_out, "w") as f:
                json.dump(result, f, indent=2)
        else:
            failed_runs.append({"run_id": run_dir.name, "error": error})
            print(f"[run_classification] SKIP {run_dir.name}: {error}")

    print(f"[run_classification] Classified: {len(results)}, Failed: {len(failed_runs)}")

    # Write failed_runs.json
    if failed_runs:
        failed_path = batch_dir / "failed_classifications.json"
        with open(failed_path, "w") as f:
            json.dump(failed_runs, f, indent=2)
        print(f"[run_classification] Failed runs written to {failed_path.name}")

    # Build aggregate summary
    aggregate = build_aggregate_summary(results, failed_runs, batch_id, batch_dir)

    # Write aggregate_taxonomy_summary.json
    agg_path = batch_dir / "aggregate_taxonomy_summary.json"
    with open(agg_path, "w") as f:
        json.dump(aggregate, f, indent=2)
    print(f"[run_classification] Aggregate summary: {agg_path.name}")

    # ── Stability maps ──────────────────────────────────────────────────────
    try:
        # BatchStabilityMapper reads comparative_results.csv which was augmented
        # by this script above (comparative_results_augmented.csv was written first,
        # but load_results() reads comparative_results.csv — check which exists)
        mapper = BatchStabilityMapper(str(batch_dir))
        # Load the standard results path; if augmented CSV was written, it's a superset
        mapper.load_results()  # reads comparative_results.csv
        if not mapper.results:
            # Fallback: try augmented CSV
            aug_path = batch_dir / "comparative_results_augmented.csv"
            if aug_path.exists():
                import csv as _csv
                with open(aug_path, newline="") as _f:
                    mapper.results = list(_csv.DictReader(_f))
        mapper.topologies = list({r.get("topology") for r in mapper.results if r.get("topology")})
        mapper.compute_all_maps(n_bins=6)
        mapper.export_all(output_dir=str(batch_dir), batch_id=batch_id)
        map_summary = mapper.get_stability_summary()
        print(f"[run_classification] Stability maps: {map_summary.get('region_type_counts', {})}")
    except Exception as exc:
        import traceback as _tb
        _tb.print_exc()
        print(f"[run_classification] Stability map generation failed (non-fatal): {exc}")

    # ── Comparative results CSV (augmented with taxonomy fields) ────────────
    csv_path = batch_dir / "comparative_results.csv"
    if csv_path.exists():
        # Read existing — CSV run_ids are "{run_path.stem}_{batch_id[:8]}"
        # e.g. "run_0000_mesh_42_0b2081bf"; directories are "run_0000_mesh_42".
        # Normalize by stripping the trailing _<8-hex-chars> suffix.
        import re as _re
        existing: Dict[str, dict] = {}
        for row in csv.DictReader(open(csv_path, newline="")):
            rid_raw = row["run_id"]
            rid_norm = _re.sub(r"_[0-9a-f]{8}$", "", rid_raw)
            existing[rid_norm] = row

        # Merge with classification results (results use bare run dir names)
        merged_count = 0
        for r in results:
            rid = r["run_id"]  # e.g. "run_0000_mesh_42"
            if rid in existing:
                existing[rid]["outcome"] = r["outcome"]
                existing[rid]["final_stability"] = r.get("final_stability", "")
                existing[rid]["final_health"] = r.get("final_health", "")
                existing[rid]["oscillation_count"] = r.get("signature", {}).get("oscillation_count", "")
                existing[rid]["fragmentation_peak"] = r.get("signature", {}).get("fragmentation_peak", "")
                existing[rid]["recovery_convergence_slope"] = r.get("signature", {}).get("recovery_convergence_slope", "")
                existing[rid]["collapse_velocity"] = r.get("signature", {}).get("collapse_velocity", "")
                existing[rid]["fragmentation_persistence"] = r.get("signature", {}).get("fragmentation_persistence", "")
                merged_count += 1
            # else: run dir name not in CSV — skip (different batch or orphaned dir)

        # Write augmented CSV
        aug_path = batch_dir / "comparative_results_augmented.csv"
        fieldnames = list(next(iter(existing.values())).keys()) if existing else []
        with open(aug_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(existing.values())
        print(f"[run_classification] Augmented CSV: {aug_path.name} (merged {merged_count}/{len(results)} rows)")

    return aggregate


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Cascade post-batch resilience classifier"
    )
    parser.add_argument(
        "batch_dir",
        nargs="?",
        default=None,
        help="Path to batch directory (e.g. experiments/monte_carlo/batch_<id>/)",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help="Batch ID to look up in experiments/monte_carlo/ (overrides batch_dir)",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Use the most recent batch in experiments/monte_carlo/",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override output directory (defaults to batch_dir)",
    )

    args = parser.parse_args()

    # Resolve batch directory
    if args.batch_id:
        batch_dir = Path("experiments/monte_carlo")
        candidates = sorted(batch_dir.glob(f"batch_*"))
        matches = [d for d in candidates if d.name == f"batch_{args.batch_id}"]
        if matches:
            batch_dir = matches[0]
        else:
            # Try prefix match
            matches = [d for d in candidates if d.name.startswith(f"batch_{args.batch_id}")]
            if matches:
                batch_dir = matches[0]
            else:
                print(f"ERROR: No batch found with ID containing '{args.batch_id}'")
                sys.exit(1)
    elif args.latest:
        batch_dir = Path("experiments/monte_carlo")
        candidates = sorted(batch_dir.glob("batch_*"), key=lambda d: d.stat().st_mtime)
        if not candidates:
            print("ERROR: No batches found in experiments/monte_carlo/")
            sys.exit(2)
        batch_dir = candidates[-1]
    elif args.batch_dir:
        batch_dir = Path(args.batch_dir)
    else:
        parser.print_help()
        sys.exit(0)

    print(f"[run_classification] Processing: {batch_dir}")

    try:
        agg = process_batch(batch_dir)
        print(f"\n[run_classification] Done.")
        print(f"  Recovery rate : {agg['recovery_success_rate']:.1%}")
        print(f"  Oscillation   : {agg['oscillation_frequency']:.1%}")
        dist = agg["outcome_distribution"]
        for o in OUTCOME_CLASSES:
            n = dist.get(o, 0)
            if n:
                print(f"  {o:<30} {n:>4}")
        sys.exit(0)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(2)
    except Exception as exc:
        print(f"FATAL: {exc}")
        traceback.print_exc()
        sys.exit(3)
