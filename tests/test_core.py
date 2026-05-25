"""
tests/test_core.py — The Cascade: core simulation engine tests
==============================================================
PEP8 compliant. Covers engine initialization, determinism, and CSV export.
"""

from __future__ import annotations

import os
import json
import csv as csv_lib
import tempfile
import pytest

from simulations.recovery_engine import (
    RecoveryEngine,
    load_config,
    SimulationMetrics,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def base_config(tmp_path) -> dict:
    """
    Isolated network topology config for testing.
    Mesh topology, 5 nodes, 10 edges, failure_tick at 10,
    recovery_tick at 30, fixed seed.
    """
    cfg = {
        "simulation_name": "test_isolated_topology",
        "topology": {
            "type": "mesh",
            "nodes": 5,
            "edges": 10,
            "seed": 42,
        },
        "failure_injection": {
            "target_node_id": 2,
            "latency_multiplier": 18.0,
            "failure_tick": 10,
            "recovery_tick": 30,
            "fragmentation_threshold_ms": 40,
        },
        "_fragmentation": {
            "spreading_factor": 0.35,
            "min_nodes_frag": 2,
        },
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
            "total_ticks": 100,
        },
    }
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_engine_initialization(base_config: dict):
    """
    Verify RecoveryEngine loads the configuration and initializes
    the correct number of nodes.
    """
    engine = RecoveryEngine(base_config)

    assert engine is not None
    assert hasattr(engine, "nodes")
    assert hasattr(engine, "edges")
    assert hasattr(engine, "tick")
    assert hasattr(engine, "recovery_outcome")

    expected_nodes = base_config["topology"]["nodes"]
    assert len(engine.nodes) == expected_nodes

    for node_id in range(expected_nodes):
        assert node_id in engine.nodes


def test_simulation_determinism(base_config: dict):
    """
    Run the engine twice with identical seeds. Confirm final metrics
    (global_health_score and stability_score) are identical.
    Proves reproducibility and replay guarantees.
    """
    cfg_r1 = json.loads(json.dumps(base_config))   # deep copy
    cfg_r2 = json.loads(json.dumps(base_config))   # deep copy

    engine_r1 = RecoveryEngine(cfg_r1)
    history_r1 = engine_r1.run()

    engine_r2 = RecoveryEngine(cfg_r2)
    history_r2 = engine_r2.run()

    # ── structural assertions ────────────────────────────────────────────────
    assert len(history_r1) == len(history_r2), (
        f"History length mismatch: {len(history_r1)} vs {len(history_r2)}"
    )

    # ── per-tick assertions ──────────────────────────────────────────────────
    for m1, m2 in zip(history_r1, history_r2):
        assert m1.tick == m2.tick, f"Tick mismatch at index"

        assert abs(m1.global_health_score - m2.global_health_score) < 1e-6, (
            f"Heath divergence at tick {m1.tick}: "
            f"{m1.global_health_score} vs {m2.global_health_score}"
        )
        assert abs(m1.stability_score - m2.stability_score) < 1e-6, (
            f"Stability divergence at tick {m1.tick}: "
            f"{m1.stability_score} vs {m2.stability_score}"
        )
        assert m1.recovery_outcome == m2.recovery_outcome, (
            f"Outcome mismatch at tick {m1.tick}: "
            f"{m1.recovery_outcome} vs {m2.recovery_outcome}"
        )

    # ── final metrics ────────────────────────────────────────────────────────
    final_r1 = history_r1[-1]
    final_r2 = history_r2[-1]

    assert abs(final_r1.global_health_score - final_r2.global_health_score) < 1e-6
    assert abs(final_r1.stability_score - final_r2.stability_score) < 1e-6
    assert final_r1.recovery_outcome == final_r2.recovery_outcome


def test_telemetry_export(base_config: dict, tmp_path):
    """
    Run a short simulation, export telemetry to a temporary directory,
    verify the CSV file exists and is non-empty with correct columns.
    """
    engine = RecoveryEngine(base_config)
    engine.run()

    csv_path = str(tmp_path / "telemetry.csv")
    engine.export_telemetry_csv(csv_path)

    assert os.path.exists(csv_path), f"CSV not written: {csv_path}"

    file_size = os.path.getsize(csv_path)
    assert file_size > 0, f"CSV is empty: {csv_path}"

    expected_fields = [
        "tick", "time_ms", "p50_latency", "p95_latency", "queue_depth",
        "retry_count", "fragmented_nodes", "stability_score",
        "global_health_score", "edge_heat_avg", "edge_heat_peak",
        "active_nodes", "total_requests", "failed_requests",
        "recovery_outcome",
    ]

    with open(csv_path, "r", newline="") as f:
        reader = csv_lib.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    assert fieldnames == expected_fields, (
        f"CSV schema drift:\n  expected: {expected_fields}\n  got:      {fieldnames}"
    )
    assert len(rows) == base_config["telemetry"]["total_ticks"], (
        f"Row count {len(rows)} != total_ticks {base_config['telemetry']['total_ticks']}"
    )

    # Verify data types and bounds
    for row in rows:
        assert 0 <= float(row["global_health_score"]) <= 1
        assert 0 <= float(row["stability_score"]) <= 1
        assert 0 <= int(row["fragmented_nodes"]) <= base_config["topology"]["nodes"]
