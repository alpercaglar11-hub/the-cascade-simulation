"""
topology_benchmarks.py — Comparative Topology Benchmark Suite
=============================================================
Compares mesh, scale-free, ring, and hierarchical topologies across:
  - Fragmentation probability
  - Average recovery time
  - Retry storm amplification
  - Stability score decay

Outputs:
  experiments/topology_benchmarks/results_<timestamp>.csv
  experiments/topology_benchmarks/summary_<timestamp>.json

Usage:
  python experiments/topology_benchmarks.py
  python experiments/topology_benchmarks.py --topologies mesh ring scale_free hierarchical
  python experiments/topology_benchmarks.py --runs 5  # 5 seeds per topology
"""

from __future__ import annotations

import json
import os
import random
import statistics
import time
import csv
from datetime import datetime, timezone
from typing import Dict, List, Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from simulations.recovery_engine import RecoveryEngine


# ─────────────────────────────────────────────────────────────────────────────
# Topology generators
# ─────────────────────────────────────────────────────────────────────────────

def generate_mesh_edges(n: int, seed: int) -> List[tuple]:
    """Ring + chord mesh (the current default)."""
    rng = random.Random(seed)
    edges = []
    for i in range(n):
        edges.append((i, (i + 1) % n))  # ring
    for i in range(n):
        edges.append((i, (i + 4) % n))  # chord
    return list(set(edges))


def generate_ring_edges(n: int, seed: int) -> List[tuple]:
    """Pure ring — only immediate neighbors."""
    return [(i, (i + 1) % n) for i in range(n)]


def generate_scale_free_edges(n: int, seed: int) -> List[tuple]:
    """
    Barabási-Albert preferential attachment model.
    Simplified: each new node attaches to 2 existing nodes with probability
    proportional to current degree. Fast enough for benchmarking.
    """
    rng = random.Random(seed)
    if n <= 3:
        edges = [(0, 1)]
        if n > 2:
            edges.append((1, 2))
        if n > 3:
            edges.append((0, 2))
        return edges

    # Start with a triangle (3 nodes, each degree 2)
    edges = [(0, 1), (1, 2), (0, 2)]
    degrees = {0: 2, 1: 2, 2: 2}

    for new_node in range(3, n):
        existing_ids = list(range(new_node))
        total_deg = sum(degrees[i] for i in existing_ids)

        targets = set()
        for _ in range(2):
            if not existing_ids:
                break
            roll = rng.random() * total_deg
            cumsum = 0
            chosen = None
            for i in existing_ids:
                cumsum += degrees[i]
                if roll <= cumsum:
                    chosen = i
                    break
            if chosen is None:
                chosen = rng.choice(existing_ids)

            targets.add(chosen)
            total_deg += 2  # new node gains degree 2

        for t in targets:
            edges.append((new_node, t))
            degrees[new_node] = degrees.get(new_node, 0) + 1
            degrees[t] = degrees.get(t, 0) + 1

    return edges


def generate_hierarchical_edges(n: int, seed: int) -> List[tuple]:
    """
    Hierarchical tree: root cluster → mid-level clusters → leaf nodes.
    For n nodes, builds a binary-tree-like hierarchy with cross-links at each level.
    """
    rng = random.Random(seed)
    edges = []

    # Root (node 0) connected to level-1 nodes (1, 2)
    root_children = [1, 2]
    for child in root_children:
        edges.append((0, child))

    # Level 1: each mid node connects to 2 leaf nodes
    leaf_counter = 3
    for mid in root_children:
        for _ in range(2):
            if leaf_counter < n:
                edges.append((mid, leaf_counter))
                leaf_counter += 1

    # Remaining nodes form a second layer
    while leaf_counter < n:
        # Connect to random existing mid-level node
        mid = rng.choice(root_children)
        edges.append((mid, leaf_counter))
        leaf_counter += 1

    # Add ring across root-level nodes for redundancy
    for i in range(len(root_children)):
        edges.append((root_children[i], root_children[(i + 1) % len(root_children)]))

    return list(set(edges))[: n * 2]  # cap edges at ~2 per node


def build_topology_edges(topology_type: str, n: int, seed: int) -> List[tuple]:
    generators = {
        "mesh": generate_mesh_edges,
        "ring": generate_ring_edges,
        "scale_free": generate_scale_free_edges,
        "hierarchical": generate_hierarchical_edges,
    }
    gen = generators.get(topology_type)
    if gen is None:
        raise ValueError(f"Unknown topology: {topology_type}. Options: {list(generators.keys())}")
    return gen(n, seed)


# ─────────────────────────────────────────────────────────────────────────────
# Benchmark runner
# ─────────────────────────────────────────────────────────────────────────────

BASE_CONFIG = {
    "simulation_name": "topology_benchmark",
    "topology": {"type": "mesh", "nodes": 12, "edges": 24, "seed": 42},
    "failure_injection": {
        "target_node_id": 7,
        "latency_multiplier": 18.0,
        "failure_tick": 120,
        "recovery_tick": 280,
        "fragmentation_threshold_ms": 40,
    },
    "_fragmentation": {"spreading_factor": 0.35, "min_nodes_frag": 3},
    "recovery": {
        "load_shedding_active": True,
        "rate_limiter_active": True,
        "recovery_rate": 0.25,
        "traffic_decay_half_life_ticks": 18,
        "retry_backoff_multiplier": 1.35,
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
        "base_capacity": 30,
        "base_drain_rate": 0.85,
    },
    "telemetry": {
        "tick_interval_ms": 50,
        "total_ticks": 600,
    },
}


def run_single_benchmark(
    topology_type: str,
    n_nodes: int,
    seed: int,
    target_node: int = 7,
    latency_mult: float = 18.0,
    failure_tick: int = 120,
    recovery_tick: int = 280,
) -> dict:
    """Run one benchmark iteration. Returns metrics dict."""

    edges = build_topology_edges(topology_type, n_nodes, seed)
    n_edges = len(edges)

    config = {
        **BASE_CONFIG,
        "simulation_name": f"benchmark_{topology_type}_seed{seed}",
        "topology": {
            "type": topology_type,
            "nodes": n_nodes,
            "edges": n_edges,
            "seed": seed,
        },
        "failure_injection": {
            "target_node_id": target_node,
            "latency_multiplier": latency_mult,
            "failure_tick": failure_tick,
            "recovery_tick": recovery_tick,
            "fragmentation_threshold_ms": 40,
        },
    }

    engine = RecoveryEngine(config)
    metrics = engine.run()

    # Extract key benchmark figures
    history = metrics
    peak_p95 = max(m.p95_latency for m in history)
    peak_queue = max(m.queue_depth for m in history)
    peak_retries = max(m.retry_count for m in history)

    # Fragmentation window
    frag_start = next((m.tick for m in history if m.fragmented_nodes > 0), None)
    frag_end = next((m.tick for m in reversed(history) if m.fragmented_nodes > 0), None)
    frag_ticks = (frag_end - frag_start) if (frag_start and frag_end) else 0
    frag_duration_ms = frag_ticks * config["telemetry"]["tick_interval_ms"]

    # Recovery time: ticks from recovery_tick to stability > 0.8
    recovery_ticks = None
    recovery_ticks_measured = None
    for m in history:
        if m.tick >= recovery_tick and m.stability_score > 0.8:
            recovery_ticks = m.tick - recovery_tick
            recovery_ticks_measured = m.tick
            break

    final = history[-1]

    return {
        "topology_type": topology_type,
        "seed": seed,
        "n_nodes": n_nodes,
        "n_edges": n_edges,
        "recovery_outcome": final.recovery_outcome,
        "final_health_score": final.global_health_score,
        "final_stability_score": final.stability_score,
        "peak_p95_latency_ms": round(peak_p95, 3),
        "peak_queue_depth": peak_queue,
        "peak_retry_count": peak_retries,
        "fragmentation_duration_ms": round(frag_duration_ms, 1),
        "fragmented_nodes_final": final.fragmented_nodes,
        "recovery_ticks": recovery_ticks if recovery_ticks is not None else -1,
        "recovery_ticks_measured": recovery_ticks_measured if recovery_ticks_measured else -1,
        # Derived scores
        "fragmentation_probability": 1.0 if final.fragmented_nodes > 0 else 0.0,
        "avg_retry_storm_amplitude": round(
            statistics.mean([m.retry_count for m in history[120:280]]), 2
        ) if len(history) > 280 else 0.0,
        "stability_decay_max": round(
            max(abs(history[i].stability_score - (history[i-1].stability_score if i > 0 else 0))
                for i in range(120, len(history))), 4
        ),
        "health_nadir": round(min(m.global_health_score for m in history), 4),
    }


def run_topology_benchmark_suite(
    topologies: List[str] = None,
    n_nodes: int = 12,
    seeds: List[int] = None,
    target_node: int = 7,
    latency_mult: float = 18.0,
) -> tuple:
    """
    Run full benchmark across all topologies and seeds.
    Returns (rows, summary) — CSV rows and JSON summary dict.
    """
    if topologies is None:
        topologies = ["mesh", "ring", "scale_free", "hierarchical"]
    if seeds is None:
        seeds = [42, 1337, 2026, 7777, 999]

    print(f"\n[TopologyBench] Starting suite:")
    print(f"  Topologies : {topologies}")
    print(f"  Nodes      : {n_nodes}")
    print(f"  Seeds      : {seeds}")
    print(f"  Runs       : {len(seeds)} per topology")

    rows = []
    errors = []

    for topo in topologies:
        print(f"\n[TopologyBench] Running: {topo}")
        for seed in seeds:
            try:
                result = run_single_benchmark(
                    topology_type=topo,
                    n_nodes=n_nodes,
                    seed=seed,
                    target_node=target_node,
                    latency_mult=latency_mult,
                )
                rows.append(result)
                print(f"  seed={seed} → {result['recovery_outcome']} "
                      f"(health={result['final_health_score']:.3f}, "
                      f"stability={result['final_stability_score']:.3f})")
            except Exception as exc:
                errors.append({"topology": topo, "seed": seed, "error": str(exc)})
                print(f"  seed={seed} → ERROR: {exc}")

    # Aggregate summary per topology
    summary = {}
    for topo in topologies:
        topo_rows = [r for r in rows if r["topology_type"] == topo]
        if not topo_rows:
            continue

        outcomes = [r["recovery_outcome"] for r in topo_rows]
        frag_probs = [r["fragmentation_probability"] for r in topo_rows]
        health_scores = [r["final_health_score"] for r in topo_rows]
        stability_scores = [r["final_stability_score"] for r in topo_rows]
        peak_p95s = [r["peak_p95_latency_ms"] for r in topo_rows]
        peak_retries = [r["peak_retry_count"] for r in topo_rows]
        frag_durations = [r["fragmentation_duration_ms"] for r in topo_rows]
        recovery_ticks_list = [r["recovery_ticks"] for r in topo_rows if r["recovery_ticks"] >= 0]

        summary[topo] = {
            "topology_type": topo,
            "n_runs": len(topo_rows),
            "outcome_distribution": {
                o: outcomes.count(o) for o in set(outcomes)
            },
            "fragmentation_probability_mean": round(statistics.mean(frag_probs), 4),
            "fragmentation_probability_std": round(
                statistics.stdev(frag_probs) if len(frag_probs) > 1 else 0.0, 4
            ),
            "final_health_score_mean": round(statistics.mean(health_scores), 4),
            "final_health_score_std": round(
                statistics.stdev(health_scores) if len(health_scores) > 1 else 0.0, 4
            ),
            "final_stability_score_mean": round(statistics.mean(stability_scores), 4),
            "final_stability_score_std": round(
                statistics.stdev(stability_scores) if len(stability_scores) > 1 else 0.0, 4
            ),
            "peak_p95_latency_ms_mean": round(statistics.mean(peak_p95s), 3),
            "peak_p95_latency_ms_max": round(max(peak_p95s), 3),
            "peak_retry_count_mean": round(statistics.mean(peak_retries), 2),
            "peak_retry_count_max": max(peak_retries),
            "fragmentation_duration_ms_mean": round(statistics.mean(frag_durations), 1),
            "recovery_ticks_mean": round(
                statistics.mean(recovery_ticks_list), 1
            ) if recovery_ticks_list else -1.0,
            "avg_retry_storm_amplitude_mean": round(
                statistics.mean([r["avg_retry_storm_amplitude"] for r in topo_rows]), 2
            ),
            "stability_decay_max_mean": round(
                statistics.mean([r["stability_decay_max"] for r in topo_rows]), 4
            ),
            "health_nadir_mean": round(statistics.mean([r["health_nadir"] for r in topo_rows]), 4),
        }

    return rows, summary, errors


# ─────────────────────────────────────────────────────────────────────────────
# Results export
# ─────────────────────────────────────────────────────────────────────────────

def export_results(
    rows: List[dict],
    summary: dict,
    errors: List[dict],
    output_dir: str,
) -> tuple:
    """Write benchmark results to CSV and JSON."""
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # CSV — one row per run
    csv_path = os.path.join(output_dir, f"results_{ts}.csv")
    if rows:
        fieldnames = list(rows[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[TopologyBench] Results CSV: {csv_path}")

    # JSON — summary + errors + config snapshot
    json_path = os.path.join(output_dir, f"summary_{ts}.json")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_config": {
            "n_topologies": len(set(r["topology_type"] for r in rows)),
            "n_runs_total": len(rows),
            "n_seeds": len(set(r["seed"] for r in rows)),
            "n_nodes": rows[0]["n_nodes"] if rows else None,
        },
        "topology_summaries": summary,
        "errors": errors,
    }
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"[TopologyBench] Summary JSON: {json_path}")

    return csv_path, json_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="The Cascade — Topology Benchmark Suite"
    )
    parser.add_argument(
        "--topologies",
        nargs="+",
        default=["mesh", "ring", "scale_free", "hierarchical"],
        help="Topology types to compare"
    )
    parser.add_argument(
        "--seeds",
        type=int,
        nargs="+",
        default=[42, 1337, 2026, 7777, 999],
        help="Random seeds to test per topology"
    )
    parser.add_argument(
        "--nodes", type=int, default=12,
        help="Number of nodes per topology"
    )
    parser.add_argument(
        "--output-dir",
        default="experiments/topology_benchmarks",
        help="Directory for results"
    )
    parser.add_argument(
        "--latency-mult", type=float, default=18.0,
        help="Failure latency multiplier"
    )
    args = parser.parse_args()

    start = time.time()
    rows, summary, errors = run_topology_benchmark_suite(
        topologies=args.topologies,
        n_nodes=args.nodes,
        seeds=args.seeds,
        latency_mult=args.latency_mult,
    )
    csv_path, json_path = export_results(rows, summary, errors, args.output_dir)
    elapsed = time.time() - start

    print(f"\n[TopologyBench] Suite complete in {elapsed:.1f}s")
    print(f"[TopologyBench] {len(rows)} runs across {len(args.topologies)} topologies")
    print(f"\n=== Topology Rankings (by final health score mean) ===")
    ranked = sorted(summary.items(), key=lambda x: -x[1]["final_health_score_mean"])
    for i, (topo, s) in enumerate(ranked, 1):
        print(f"  {i}. {topo:<15} health={s['final_health_score_mean']:.4f}  "
              f"stability={s['final_stability_score_mean']:.4f}  "
              f"frag_prob={s['fragmentation_probability_mean']:.2f}  "
              f"recovery_ticks={s['recovery_ticks_mean']:.1f}")

    if errors:
        print(f"\n[TopologyBench] {len(errors)} errors occurred (see JSON)")


if __name__ == "__main__":
    main()