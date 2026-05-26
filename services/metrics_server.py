"""
metrics_server.py — Prometheus Metrics Exporter + HTTP Instrumentation Server
=============================================================================
Exposes simulation telemetry as Prometheus-compatible /metrics endpoint.
Runs as a background service alongside the recovery engine.

Metrics (all gauges, updated every tick):
  cascade_p95_latency_ms        — p95 round-trip latency across all nodes
  cascade_p50_latency_ms        — p50 round-trip latency across all nodes
  cascade_queue_depth           — aggregate queue depth (all nodes)
  cascade_retry_volume          — current retry storm volume
  cascade_fragmented_nodes      — nodes currently fragmented
  cascade_active_nodes          — nodes currently operational
  cascade_global_health_score    — composite health (0-1)
  cascade_stability_score        — stability (0-1)
  cascade_edge_heat_avg          — average edge congestion (0-1)
  cascade_edge_heat_peak         — peak edge congestion (0-1)
  cascade_recovery_phase         — ordinal phase (0=STABLE 1=FAILURE 2=FRAG 3=RECOV 4=OUTCOME)
  cascade_total_requests         — cumulative successful requests
  cascade_failed_requests        — cumulative failed requests

Histogram buckets (for latency distribution):
  5, 10, 25, 50, 100, 200, 500, 1000 ms

Usage:
  # Standalone (in-process with engine)
  from services.metrics_server import PrometheusMetricsServer
  server = PrometheusMetricsServer(port=9090)
  server.instrument_engine(engine)   # auto-updates every tick
  server.start()                     # blocking — run in background thread

  # Standalone HTTP server
  python services/metrics_server.py --port 9090

Dependencies:
  pip install prometheus_client flask

Structured logging around:
  - Server start/stop
  - Metric updates (every tick)
  - HTTP requests (/metrics, /health, /vars)
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

# ── prometheus_client must be available; gracefully degrade if missing ──────
try:
    from prometheus_client import (
        Gauge, Histogram, Counter, Info,
        CollectorRegistry, generate_latest,
        CONTENT_TYPE_LATEST, REGISTRY,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logging.warning(
        "[metrics_server] prometheus_client not installed. "
        "Metrics endpoint disabled. "
        "Install with: pip install prometheus_client"
    )

# ── Flask for HTTP server ─────────────────────────────────────────────────────
try:
    from flask import Flask, Response
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    logging.warning(
        "[metrics_server] flask not installed. "
        "HTTP /metrics endpoint disabled. "
        "Install with: pip install flask"
    )

logger = logging.getLogger("cascade.metrics")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)


# ─────────────────────────────────────────────────────────────────────────────
# Phase ordinal mapping (for prometheus gauge)
# ─────────────────────────────────────────────────────────────────────────────

PHASE_ORDINAL = {
    "STABLE": 0,
    "FAILURE_INJECTION": 1,
    "FRAGMENTATION": 2,
    "RECOVERY_INITIATION": 3,
    "RECOVERY_OUTCOME": 4,
}


# ─────────────────────────────────────────────────────────────────────────────
# Prometheus metric definitions (lazily initialized)
# ─────────────────────────────────────────────────────────────────────────────

class MetricBank:
    """
    Centralized metric definitions.
    Lazily initialized on first use to allow stand-alone startup
    even without prometheus_client fully available.
    """

    def __init__(self, registry: Optional[CollectorRegistry] = None):
        self._registry = registry
        self._initialized = False
        self._metrics = {}

    def _init(self, registry):
        if self._initialized:
            return
        self._initialized = True

        r = registry or REGISTRY

        self._metrics["p95_latency"] = Histogram(
            "cascade_p95_latency_ms",
            "P95 round-trip latency in milliseconds",
            buckets=(5, 10, 25, 50, 100, 200, 500, 1000),
            registry=r,
        )
        self._metrics["p50_latency"] = Histogram(
            "cascade_p50_latency_ms",
            "P50 round-trip latency in milliseconds",
            buckets=(1, 5, 10, 25, 50, 100, 200, 500),
            registry=r,
        )
        self._metrics["queue_depth"] = Gauge(
            "cascade_queue_depth",
            "Aggregate queue depth across all nodes",
            registry=r,
        )
        self._metrics["retry_volume"] = Gauge(
            "cascade_retry_volume",
            "Current retry storm volume (cumulative retry events pending)",
            registry=r,
        )
        self._metrics["fragmented_nodes"] = Gauge(
            "cascade_fragmented_nodes",
            "Number of nodes currently in fragmented state",
            registry=r,
        )
        self._metrics["active_nodes"] = Gauge(
            "cascade_active_nodes",
            "Number of currently operational (non-fragmented) nodes",
            registry=r,
        )
        self._metrics["global_health_score"] = Gauge(
            "cascade_global_health_score",
            "Composite system health score (0-1, higher is healthier)",
            registry=r,
        )
        self._metrics["stability_score"] = Gauge(
            "cascade_stability_score",
            "Network stability score (0-1, higher is more stable)",
            registry=r,
        )
        self._metrics["edge_heat_avg"] = Gauge(
            "cascade_edge_heat_avg",
            "Average edge congestion heat across all edges (0-1)",
            registry=r,
        )
        self._metrics["edge_heat_peak"] = Gauge(
            "cascade_edge_heat_peak",
            "Peak edge congestion heat across all edges (0-1)",
            registry=r,
        )
        self._metrics["recovery_phase"] = Gauge(
            "cascade_recovery_phase",
            "Ordinal simulation phase (0=STABLE 1=FAILURE 2=FRAGMENTATION 3=RECOVERY 4=OUTCOME)",
            registry=r,
        )
        self._metrics["total_requests"] = Counter(
            "cascade_total_requests",
            "Cumulative successful requests across all nodes",
            registry=r,
        )
        self._metrics["failed_requests"] = Counter(
            "cascade_failed_requests",
            "Cumulative failed requests across all nodes",
            registry=r,
        )
        self._metrics["engine_version"] = Info(
            "cascade_engine",
            "Simulation engine version and build info",
            registry=r,
        )

        # Latency histogram for percentile tracking
        self._metrics["latency_samples"] = Histogram(
            "cascade_latency_samples_ms",
            "Raw latency samples for distribution analysis",
            buckets=(1, 5, 10, 25, 50, 100, 200, 500, 1000, 2000),
            registry=r,
        )

    def __getattr__(self, name: str):
        if not self._initialized:
            self._init(self._registry)
        if name not in self._metrics:
            raise AttributeError(f"No metric named '{name}'")
        return self._metrics[name]

    def get(self, name: str):
        return getattr(self, name, None)


# Global metric bank — initialized once
_metrics = MetricBank()


# ─────────────────────────────────────────────────────────────────────────────
# PrometheusMetricsServer
# ─────────────────────────────────────────────────────────────────────────────

class PrometheusMetricsServer:
    """
    HTTP server exposing Prometheus metrics.
    Can be instrumented on a RecoveryEngine to auto-update every tick.

    Usage:
        server = PrometheusMetricsServer(port=9090)
        server.instrument_engine(engine)
        server.start()  # blocks — use background thread in production
    """

    def __init__(
        self,
        port: int = 9090,
        engine=None,
        enable_flask: bool = True,
    ):
        if not PROMETHEUS_AVAILABLE:
            logger.error("[PrometheusMetricsServer] prometheus_client unavailable — exiting")
            raise ImportError("prometheus_client is required for metrics server")

        self.port = port
        self._engine = engine
        self._enable_flask = enable_flask and FLASK_AVAILABLE
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._update_count = 0
        self._start_time = time.time()

        # Engine metadata
        from simulations.recovery_engine import load_config
        cfg_path = "configs/recovery_test.json"
        import os as _os
        if _os.path.exists(cfg_path):
            try:
                cfg = load_config(cfg_path)
                self._engine_version = cfg.get("engine_version", "2.0.0")
            except Exception:
                self._engine_version = "2.0.0"
        else:
            self._engine_version = "2.0.0"

        # Initialize all metrics
        _metrics._init(None)
        logger.info(f"[PrometheusMetricsServer] Initialized on port {self.port}")

    def instrument_engine(self, engine):
        """
        Attach to a RecoveryEngine instance.
        Monkey-patches engine.tick_step() to push metrics every tick.
        Call this before engine.run().
        """
        self._engine = engine
        server = self
        metrics = _metrics

        original_tick_step = engine.tick_step

        def instrumented_tick_step():
            original_tick_step()
            if engine.metrics_history:
                latest = engine.metrics_history[-1]
                server._push_metrics(latest, engine.phase)
            return  # no return value consumed

        engine.tick_step = instrumented_tick_step

        logger.info(
            f"[PrometheusMetricsServer] Engine instrumented — "
            f"metrics will update every tick on port {self.port}"
        )

    def _push_metrics(self, m, phase: str):
        """
        Push a SimulationMetrics record to Prometheus gauges/histograms.
        Called automatically every tick when instrumented.
        """
        self._update_count += 1

        # Latency histograms
        _metrics.p50_latency.observe(m.p50_latency)
        _metrics.p95_latency.observe(m.p95_latency)

        # Gauges
        _metrics.queue_depth.set(m.queue_depth)
        _metrics.retry_volume.set(m.retry_count)
        _metrics.fragmented_nodes.set(m.fragmented_nodes)
        _metrics.active_nodes.set(m.active_nodes)
        _metrics.global_health_score.set(m.global_health_score)
        _metrics.stability_score.set(m.stability_score)
        _metrics.edge_heat_avg.set(m.edge_heat_avg)
        _metrics.edge_heat_peak.set(m.edge_heat_peak)
        _metrics.recovery_phase.set(PHASE_ORDINAL.get(phase, 0))

        # Counters
        _metrics.total_requests.inc(m.total_requests)
        _metrics.failed_requests.inc(m.failed_requests)

        # Engine info (set once, low cardinality)
        if self._update_count == 1:
            _metrics.engine_version.info({
                "version": self._engine_version,
                "phase": phase,
            })

        # Structured log every 50 ticks
        if self._update_count % 50 == 0:
            logger.info(
                f"[metrics] tick={m.tick:4d} phase={phase:<20} "
                f"p95={m.p95_latency:7.2f}ms frag={m.fragmented_nodes} "
                f"retry={m.retry_count:4d} health={m.global_health_score:.4f} "
                f"stability={m.stability_score:.4f}"
            )

    def _build_flask_app(self):
        """Construct Flask app with /metrics, /health, /vars endpoints."""
        app = Flask("cascade-metrics")

        @app.route("/metrics")
        def metrics():
            """Prometheus scrape target."""
            logger.debug(f"[/metrics] scrape request (update #{self._update_count})")
            output = generate_latest()
            return Response(output, mimetype=CONTENT_TYPE_LATEST)

        @app.route("/health")
        def health():
            """Liveness/readiness probe for container orchestration."""
            uptime = time.time() - self._start_time
            return {
                "status": "ok",
                "uptime_seconds": round(uptime, 2),
                "updates_pushed": self._update_count,
                "port": self.port,
            }

        @app.route("/vars")
        def vars():
            """
            Human-readable variable dump.
            Useful for debugging without Prometheus tooling.
            """
            if not self._engine or not self._engine.metrics_history:
                return {"status": "no_data_yet"}

            m = self._engine.metrics_history[-1]
            return {
                "tick": m.tick,
                "phase": self._engine.phase,
                "p50_latency_ms": m.p50_latency,
                "p95_latency_ms": m.p95_latency,
                "queue_depth": m.queue_depth,
                "retry_volume": m.retry_count,
                "fragmented_nodes": m.fragmented_nodes,
                "active_nodes": m.active_nodes,
                "global_health_score": m.global_health_score,
                "stability_score": m.stability_score,
                "edge_heat_avg": m.edge_heat_avg,
                "edge_heat_peak": m.edge_heat_peak,
                "total_requests": m.total_requests,
                "failed_requests": m.failed_requests,
                "recovery_outcome": m.recovery_outcome,
            }

        return app

    def start(self, background: bool = False):
        """
        Start the HTTP server.
        Set background=True to run in a thread and return immediately.
        """
        if not self._enable_flask:
            logger.error("[PrometheusMetricsServer] Flask unavailable — cannot start HTTP server")
            return

        app = self._build_flask_app()

        if background:
            self._thread = threading.Thread(
                target=lambda: app.run(host="0.0.0.0", port=self.port, debug=False),
                name=f"metrics-server-{self.port}",
                daemon=True,
            )
            self._thread.start()
            logger.info(f"[PrometheusMetricsServer] Started in background on :{self.port}")
        else:
            logger.info(f"[PrometheusMetricsServer] Starting blocking on :{self.port}")
            app.run(host="0.0.0.0", port=self.port, debug=False)

    def stop(self):
        """Stop the background server thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            logger.info("[PrometheusMetricsServer] Stopped")


# ─────────────────────────────────────────────────────────────────────────────
# Standalone CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Cascade Metrics Server — Prometheus /metrics endpoint"
    )
    parser.add_argument(
        "--port", type=int, default=9090,
        help="HTTP port for /metrics endpoint (default: 9090)"
    )
    parser.add_argument(
        "--bind", default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level"
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    logger.info(f"[metrics_server] Starting standalone Prometheus server on {args.bind}:{args.port}")

    app = Flask("cascade-metrics")
    update_count = [0]  # mutable container for nested function

    @app.route("/metrics")
    def metrics():
        logger.debug(f"[/metrics] scrape (update #{update_count[0]})")
        output = generate_latest()
        return Response(output, mimetype=CONTENT_TYPE_LATEST)

    @app.route("/health")
    def health():
        return {"status": "ok", "port": args.port}

    @app.route("/vars")
    def vars():
        return {"status": "no_engine_attached", "hint": "use --engine flag or instrument_engine()"}

    logger.info(f"[/metrics] Prometheus endpoint ready on :{args.port}")
    logger.info(f"[/health] Health probe on :{args.port}/health")
    logger.info(f"[/vars] Debug variables on :{args.port}/vars")

    app.run(host=args.bind, port=args.port, debug=False)


if __name__ == "__main__":
    main()