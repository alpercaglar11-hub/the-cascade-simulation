#!/usr/bin/env python3
"""
run_v2.py — The Cascade v2 Execution Entry Point
==================================================
Single-command pipeline:
  1. Load config (or replay metadata)
  2. Execute simulation engine → generate telemetry CSVs
  3. Expose Prometheus metrics via metrics_server (optional, --enable-metrics)
  4. Trigger Manim render of scenes/v2_recovery.py

Usage:
  # Full pipeline: engine + Prometheus metrics + 4K render
  python run_v2.py --config configs/recovery_test.json --enable-metrics

  # Engine + Prometheus metrics (no render — fastest iteration loop)
  python run_v2.py --config configs/recovery_test.json --engine-only --enable-metrics

  # Standard run from config (no metrics server, unchanged behavior)
  python run_v2.py --config configs/recovery_test.json

  # Replay an existing run (re-runs engine + regenerates all telemetry)
  python run_v2.py --replay replays/<replay_id>.json

  # Engine-only (no render, no metrics server)
  python run_v2.py --config configs/recovery_test.json --engine-only

  # Render-only (from existing telemetry)
  python run_v2.py --config configs/recovery_test.json --render-only

Environment variables:
  CASCADE_METRICS_PORT  — port for Prometheus /metrics endpoint (default: 9090)
  CASCADE_SEED          — simulation seed override
  CASCADE_OUTPUT_DIR    — output directory (default: metrics)
"""

import argparse
import json
import logging
import os
import sys
import threading
import subprocess

MANIM_BIN = "/home/alper/.local/bin/manim"
DEFAULT_CONFIG = "configs/recovery_test.json"
SCENE_CLASS = "V2_RecoveryVisualization"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simulations.recovery_engine import RecoveryEngine, load_config
from replays.replay_manager import save_replay, run_replay

# ── Structured logger ─────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cascade.runner")


# ── Metrics server integration ───────────────────────────────────────────────

def _start_metrics_server(engine, port: int):
    """
    Start PrometheusMetricsServer in a background thread,
    instrumented on engine.tick_step.
    Returns the server instance so the caller can .stop() it after run().
    """
    from services.metrics_server import PrometheusMetricsServer

    srv = PrometheusMetricsServer(port=port, engine=engine)
    srv.instrument_engine(engine)
    srv.start(background=True)

    logger.info(
        f"[run_v2] Metrics server instrumented on engine — "
        f"/metrics at http://localhost:{port}/metrics"
    )
    return srv


# ── Resilience taxonomy (loaded lazily to avoid import cycles) ────────────────
def _run_classification(
    metrics_history,
    cfg: dict,
    output_dir: str,
    logger,
):
    """
    Execute OutcomeClassifier + PhaseDetector on completed telemetry.
    Always wrapped in try/except — classification failures never crash the pipeline.
    Writes: classification_summary.json, phase_transitions.csv,
            phase_transition_events.csv, resilience_metrics.json
    """
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from simulations.resilience_taxonomy import classify_and_analyze

        cls_output = os.path.join(output_dir, "resilience")
        os.makedirs(cls_output, exist_ok=True)

        outcome, evidence, transitions = classify_and_analyze(
            metrics_history, cfg, output_dir=cls_output
        )

        # Persist a flat summary alongside the structured exports
        summary_path = os.path.join(output_dir, "classification_summary.json")
        with open(summary_path, "w") as f:
            import json as _json
            _json.dump({
                "outcome": outcome,
                "primary_signal": evidence.primary_signal,
                "secondary_signals": evidence.secondary_signals,
                "violating_thresholds": evidence.violating_thresholds,
                "phase_sequence": evidence.phase_sequence,
                "signature": {
                    "initial_stability": evidence.stability_signature.initial_stability,
                    "final_stability": evidence.stability_signature.final_stability,
                    "stability_trend": evidence.stability_signature.stability_trend,
                    "oscillation_count": evidence.stability_signature.oscillation_count,
                    "oscillation_amplitude": evidence.stability_signature.oscillation_amplitude,
                    "fragmentation_peak": evidence.stability_signature.fragmentation_peak,
                    "fragmentation_duration_ticks": evidence.stability_signature.fragmentation_duration_ticks,
                    "fragmentation_persistence": evidence.stability_signature.fragmentation_persistence,
                    "retry_peak": evidence.stability_signature.retry_peak,
                    "retry_storm_duration_ticks": evidence.stability_signature.retry_storm_duration_ticks,
                    "recovery_convergence_slope": evidence.stability_signature.recovery_convergence_slope,
                    "collapse_velocity": evidence.stability_signature.collapse_velocity,
                    "secondary_failure_tick": evidence.stability_signature.secondary_failure_tick,
                    "health_final": evidence.stability_signature.health_final,
                    "health_trend": evidence.stability_signature.health_trend,
                    "latency_p95_peak": evidence.stability_signature.latency_p95_peak,
                    "latency_p95_trend": evidence.stability_signature.latency_p95_trend,
                    "active_nodes_final": evidence.stability_signature.active_nodes_final,
                    "active_nodes_initial": evidence.stability_signature.active_nodes_initial,
                },
            }, f, indent=2)

        logger.info(f"[run_v2] Classification complete: {outcome}")
        logger.info(f"  Frag peak: {evidence.stability_signature.fragmentation_peak},  "
                    f"Oscillations: {evidence.stability_signature.oscillation_count},  "
                    f"Convergence slope: {evidence.stability_signature.recovery_convergence_slope:.4f}")

    except Exception as exc:
        logger.warning(f"[run_v2] Classification step failed (non-fatal): {exc}")


# ── Pipeline steps ────────────────────────────────────────────────────────────

def run_engine(
    config_path: str,
    output_dir: str,
    metrics_port: int | None = None,
):
    """
    Execute the simulation engine to produce all telemetry exports.
    If metrics_port is set, starts PrometheusMetricsServer in background
    and instruments engine.tick_step to push metrics every tick.
    Returns (engine, metrics, metrics_server_or_None).
    """
    cfg = load_config(config_path)

    # Seed override via env (deterministic CI runs)
    seed_override = os.environ.get("CASCADE_SEED")
    if seed_override is not None:
        cfg["topology"]["seed"] = int(seed_override)
        logger.info(f"[run_v2] Seed overridden via CASCADE_SEED: {seed_override}")

    engine = RecoveryEngine(cfg)

    logger.info(f"[run_v2] Engine: {cfg['simulation_name']}")
    logger.info(f"[run_v2] Topology: {cfg['topology']['type']} "
                f"({cfg['topology']['nodes']} nodes, "
                f"{cfg['topology']['edges']} edges)")
    logger.info(f"[run_v2] Failure injection: tick "
                f"{cfg['failure_injection']['failure_tick']} "
                f"→ Node_{cfg['failure_injection']['target_node_id']} "
                f"({cfg['failure_injection']['latency_multiplier']}x latency)")
    logger.info(f"[run_v2] Recovery: tick "
                f"{cfg['failure_injection']['recovery_tick']}")

    # ── Metrics server (optional, background thread) ──────────────────────
    metrics_srv = None
    if metrics_port is not None:
        metrics_srv = _start_metrics_server(engine, metrics_port)

    # ── Engine run ─────────────────────────────────────────────────────────
    metrics = engine.run()

    final = metrics[-1]
    logger.info(f"[run_v2] Simulation complete.")
    logger.info(f"  Outcome       : {final.recovery_outcome}")
    logger.info(f"  Final health  : {final.global_health_score:.4f}")
    logger.info(f"  Final stable  : {final.stability_score:.4f}")
    logger.info(f"  Peak p95      : "
                f"{max(m.p95_latency for m in metrics):.1f}ms")
    logger.info(f"  Peak retries  : "
                f"{max(m.retry_count for m in metrics)}")

    # ── Telemetry CSV exports ───────────────────────────────────────────────
    os.makedirs(output_dir, exist_ok=True)

    engine.export_telemetry_csv(f"{output_dir}/telemetry.csv")
    engine.export_summary_json(f"{output_dir}/recovery_summary.json")
    engine.export_latency_distribution_csv(
        f"{output_dir}/latency_distribution.csv")
    engine.export_retry_storm_csv(f"{output_dir}/retry_storms.csv")
    engine.export_contention_decay_csv(
        f"{output_dir}/contention_decay.csv")

    logger.info(f"[run_v2] Telemetry exported to: {output_dir}/")
    for fname in [
        "telemetry.csv", "recovery_summary.json",
        "latency_distribution.csv", "retry_storms.csv",
        "contention_decay.csv",
    ]:
        full = os.path.join(output_dir, fname)
        if os.path.exists(full):
            size = os.path.getsize(full)
            logger.info(f"  {fname}: {size:,} bytes")

    # ── Resilience classification (non-blocking) ───────────────────────────
    _run_classification(metrics, cfg, output_dir, logger)

    return engine, metrics, metrics_srv


def run_manim_render(
    config_path: str,
    output_dir: str,
    render_4k: bool = True,
):
    """Trigger Manim scene render."""
    cfg = load_config(config_path)
    quality = "4K" if render_4k else "1080p"
    fps = cfg["visualization"].get("fps", 60)

    scene = cfg["visualization"].get("output_scene_class", SCENE_CLASS)
    manim_file = "scenes/v2_recovery.py"
    out_file = f"recovery_v2_{quality.lower()}"

    logger.info(f"[run_v2] Rendering Manim scene: {scene}")
    logger.info(f"  Quality: {quality} @ {fps}fps")
    logger.info(f"  Output : {output_dir}/{out_file}.mp4")

    os.makedirs(output_dir, exist_ok=True)

    cmd = [
        MANIM_BIN,
        "-q", "k" if render_4k else "h",
        manim_file,
        scene,
        "--output_file", out_file,
        "--format", "mp4",
        "--fps", str(fps),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"[run_v2] Manim render FAILED:\n{result.stderr[-2000:]}")
        sys.exit(1)
    else:
        logger.info("[run_v2] Manim render OK")
        for line in result.stdout.splitlines():
            if "File ready at" in line or "Played" in line or "ERROR" in line:
                logger.info(f"  {line.strip()}")


def run_from_replay(replay_path: str, output_dir: str):
    """Execute a deterministic replay — re-runs engine + optionally renders."""
    result = run_replay(replay_path, output_dir)
    verified = result["verification"]

    logger.info(f"[run_v2] Replay complete.")
    logger.info(f"  Replay ID    : {result['replay_id']}")
    logger.info(f"  Deterministic: {verified['deterministic']}")
    logger.info(f"  Observed     : {verified['observed_outcome']}")
    logger.info(f"  Expected     : {verified['expected_outcome']}")
    if verified["health_difference"] > 0:
        logger.info(f"  Health Δ    : {verified['health_difference']:.6f}")
    return result


def save_replay_after_run(
    config_path: str,
    output_dir: str,
    save_name: str,
):
    """Run engine, save replay metadata, export telemetry."""
    cfg = load_config(config_path)
    engine = RecoveryEngine(cfg)
    metrics = engine.run()
    final = metrics[-1]

    logger.info(f"[run_v2] Engine: {cfg['simulation_name']}")
    logger.info(f"  Outcome  : {final.recovery_outcome}  "
                f"health={final.global_health_score:.4f}  "
                f"stability={final.stability_score:.4f}")

    # Export telemetry first
    os.makedirs(output_dir, exist_ok=True)
    engine.export_telemetry_csv(f"{output_dir}/telemetry.csv")
    engine.export_summary_json(f"{output_dir}/recovery_summary.json")
    engine.export_latency_distribution_csv(
        f"{output_dir}/latency_distribution.csv")
    engine.export_retry_storm_csv(f"{output_dir}/retry_storms.csv")
    engine.export_contention_decay_csv(
        f"{output_dir}/contention_decay.csv")

    # ── Resilience classification (non-blocking) ───────────────────────────
    _run_classification(metrics, cfg, output_dir, logger)

    # Save replay metadata
    replay = save_replay(cfg, metrics, "2.0.0", output_dir, replay_id=save_name)
    logger.info(f"[run_v2] Replay saved: {save_name}")
    logger.info(f"  ID      : {replay['replay_id']}")
    logger.info(f"  Outcome : {replay['metrics_summary']['recovery_outcome']}")
    logger.info(f"  Config  : {output_dir}/{replay['replay_id']}.json")

    return engine, metrics


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="The Cascade v2 — Recovery Dynamics Runner"
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to simulation config JSON"
    )
    parser.add_argument(
        "--output-dir", default="metrics",
        help="Directory for telemetry and render output"
    )
    parser.add_argument(
        "--render-only", action="store_true",
        help="Skip engine run, only render Manim scene"
    )
    parser.add_argument(
        "--engine-only", action="store_true",
        help="Only run engine, skip Manim render"
    )
    parser.add_argument(
        "--no-4k", action="store_true",
        help="Render at 1080p instead of 4K"
    )
    parser.add_argument(
        "--enable-metrics", action="store_true",
        help=(
            "Start PrometheusMetricsServer and instrument engine.tick_step. "
            "Exposes /metrics on CASCADE_METRICS_PORT (default: 9090). "
            "Only valid with --engine-only or full pipeline runs."
        )
    )
    parser.add_argument(
        "--metrics-port", type=int,
        default=int(os.environ.get("CASCADE_METRICS_PORT", "9090")),
        help="Port for Prometheus /metrics endpoint (default: 9090)"
    )
    parser.add_argument(
        "--replay",
        help="Path to replay JSON to execute (re-runs engine identically)"
    )
    parser.add_argument(
        "--replay-save",
        help="After engine run, save replay metadata to replays/<name>.json"
    )
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # ── Path validation ────────────────────────────────────────────────────
    if args.replay:
        replay_path = args.replay
        if not os.path.exists(replay_path):
            logger.error(f"[run_v2] ERROR: replay not found: {replay_path}")
            sys.exit(1)
    else:
        config_path = args.config
        if not os.path.exists(config_path):
            logger.error(f"[run_v2] ERROR: config not found: {config_path}")
            sys.exit(1)

    logger.info(f"[run_v2] Working directory: {script_dir}")
    logger.info(f"[run_v2] Output directory : {args.output_dir}")

    if args.enable_metrics and args.engine_only is False and args.render_only:
        logger.warning(
            "[run_v2] --enable-metrics has no effect with --render-only. "
            "Ignoring."
        )
        args.enable_metrics = False

    metrics_port = args.metrics_port if args.enable_metrics else None

    # ── Dispatch ───────────────────────────────────────────────────────────
    if args.replay:
        run_from_replay(args.replay, args.output_dir)
        # Skip Manim render for replays (telemetry is the artifact)
        logger.info("[run_v2] Replay complete — skipping render.")

    elif args.replay_save:
        save_replay_after_run(args.config, args.output_dir, args.replay_save)
        if not args.engine_only:
            run_manim_render(
                args.config, args.output_dir, render_4k=not args.no_4k)

    else:
        if not args.render_only:
            _engine, _metrics, _srv = run_engine(
                args.config,
                args.output_dir,
                metrics_port=metrics_port,
            )

        if not args.engine_only:
            run_manim_render(
                args.config, args.output_dir, render_4k=not args.no_4k)

    logger.info("[run_v2] Pipeline complete.")


if __name__ == "__main__":
    main()