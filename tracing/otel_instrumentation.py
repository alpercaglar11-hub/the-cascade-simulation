"""
otel_instrumentation.py — OpenTelemetry Distributed Tracing for The Cascade
============================================================================
Instruments the recovery engine with OTel spans. Each major state transition
produces a span with business-relevant attributes:
  - node.id, edge.heat, retry.count, fragmentation.state, stability.score

Export configuration:
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
  OTEL_SERVICE_NAME=cascade-recovery-engine
  OTEL_TRACES_SAMPLER=parentbased_traceidratio
  OTEL_TRACES_SAMPLER_ARG=0.1  # 10% sampling, 100% on errors

Usage:
  from tracing.otel_instrumentation import OTelSpanRecorder, configure_otel

  configure_otel(service_name="cascade-recovery-engine")
  recorder = OTelSpanRecorder()

  engine = RecoveryEngine(config)
  recorder.instrument_engine(engine)

  # Or use as context manager:
  with OTelSpanRecorder().span("fragmentation_event", {"node.id": 7}):
      engine._fragment_nodes()
"""

from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# OTel is an optional dependency — gracefully degrade if not installed
# ─────────────────────────────────────────────────────────────────────────────

OTEL_AVAILABLE = False
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    from opentelemetry.trace import Status, StatusCode
    OTEL_AVAILABLE = True
except ImportError:
    logger.warning(
        "[OTel] opentelemetry package not found. "
        "Tracing disabled. Install with: pip install opentelemetry-api "
        "opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

def configure_otel(
    service_name: str = "cascade-recovery-engine",
    endpoint: str = None,
    sampler_ratio: float = 0.1,
) -> Optional[Any]:
    """
    Initialize global OTel tracer provider.
    Call once at application startup.

    Args:
        service_name: Identifier for this service in Jaeger
        endpoint: OTLP gRPC endpoint (e.g., http://jaeger:4317)
        sampler_ratio: Trace sampling rate (0.0–1.0)

    Returns:
        Tracer instance, or None if OTel unavailable
    """
    if not OTEL_AVAILABLE:
        return None

    if endpoint is None:
        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "http://localhost:4317"
        )

    resource = Resource.create({SERVICE_NAME: service_name})

    # Import sampler
    try:
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
        sampler = TraceIdRatioBased(sampler_ratio)
    except Exception:
        logger.warning("[OTel] Could not configure sampler, using default")
        sampler = None

    provider = TracerProvider(resource=resource)
    if sampler:
        provider = TracerProvider(resource=resource, sampler=sampler)

    try:
        span_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(provider)
        logger.info(f"[OTel] Configured — endpoint={endpoint}, service={service_name}")
    except Exception as exc:
        logger.warning(f"[OTel] Failed to configure OTLP exporter: {exc}")

    return trace.get_tracer(service_name)


def get_tracer(name: str = "cascade-recovery-engine") -> Any:
    """Get the global tracer for the given name."""
    if not OTEL_AVAILABLE:
        return NoOpTracer()
    return trace.get_tracer(name)


# ─────────────────────────────────────────────────────────────────────────────
# No-op tracer (used when OTel is unavailable)
# ─────────────────────────────────────────────────────────────────────────────

class NoOpSpan:
    """No-operation span — all methods are no-ops."""
    def set_attribute(self, key: str, value: Any): pass
    def set_attributes(self, attrs: Dict[str, Any]): pass
    def add_event(self, name: str, attributes: Dict[str, Any] = None): pass
    def set_status(self, status): pass
    def end(self, end_time: float = None): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass


class NoOpTracer:
    """No-operation tracer — returns no-op spans."""
    def start_span(self, name: str, **kwargs) -> NoOpSpan:
        return NoOpSpan()
    def start_as_current_span(self, name: str, **kwargs) -> NoOpSpan:
        return NoOpSpan()
    @contextmanager
    def span(self, name: str, attributes: Dict[str, Any] = None):
        yield NoOpSpan()


# ─────────────────────────────────────────────────────────────────────────────
# Span recorder — attaches OTel instrumentation to recovery engine
# ─────────────────────────────────────────────────────────────────────────────

class OTelSpanRecorder:
    """
    Records distributed tracing spans around key recovery engine operations.

    Usage:
        recorder = OTelSpanRecorder()
        recorder.instrument_engine(engine)
        # All tick_step() calls now produce spans

    Or use directly:
        with recorder.span("custom_event", {"node.id": 7, "edge.heat": 0.9}):
            do_work()
    """

    def __init__(self, service_name: str = "cascade-recovery-engine"):
        self.service_name = service_name
        self.tracer = get_tracer(service_name) if OTEL_AVAILABLE else NoOpTracer()
        self._enabled = OTEL_AVAILABLE

    @property
    def enabled(self) -> bool:
        return self._enabled

    @contextmanager
    def span(
        self,
        name: str,
        attributes: Dict[str, Any] = None,
        record_exception: bool = True,
    ):
        """
        Context manager for manual instrumentation.

        Example:
            with recorder.span("node_failure", {"node.id": 7, "retry.count": 120}):
                engine.inject_failure()
        """
        if not self._enabled:
            yield NoOpSpan()
            return

        with self.tracer.start_as_current_span(name) as span:
            if attributes:
                span.set_attributes(attributes)
            try:
                yield span
            except Exception as exc:
                if record_exception:
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    span.record_exception(exc)
                raise

    def record_tick_span(
        self,
        tick: int,
        phase: str,
        metrics: Any,  # SimulationMetrics
        node_states: Dict[int, Any] = None,
    ):
        """
        Record a span for a single simulation tick.
        Called automatically by instrumented engines.

        Key attributes:
          - tick, phase, p50_latency, p95_latency
          - queue_depth, retry_count, fragmented_nodes
          - stability_score, global_health_score
          - edge_heat_avg, edge_heat_peak
        """
        if not self._enabled:
            return

        attrs = {
            "tick": tick,
            "phase": phase,
            "p50_latency": round(metrics.p50_latency, 3),
            "p95_latency": round(metrics.p95_latency, 3),
            "queue_depth": metrics.queue_depth,
            "retry_count": metrics.retry_count,
            "fragmented_nodes": metrics.fragmented_nodes,
            "stability_score": round(metrics.stability_score, 4),
            "global_health_score": round(metrics.global_health_score, 4),
            "edge_heat_avg": round(metrics.edge_heat_avg, 4),
            "edge_heat_peak": round(metrics.edge_heat_peak, 4),
        }

        with self.tracer.start_as_current_span(f"tick_{tick}") as span:
            span.set_attributes(attrs)

            # Per-node attributes if provided
            if node_states:
                frag_nodes = [nid for nid, n in node_states.items() if n.fragmented]
                if frag_nodes:
                    span.set_attribute("fragmented.node_ids", str(frag_nodes))
                in_storm = [nid for nid, n in node_states.items() if n.in_retry_storm]
                if in_storm:
                    span.set_attribute("retry_storm.node_ids", str(in_storm))

    def record_state_transition(
        self,
        from_phase: str,
        to_phase: str,
        tick: int,
        triggered_by: str = None,
        metrics: Any = None,
    ):
        """Record a phase transition event (e.g., STABLE → FRAGMENTATION)."""
        if not self._enabled:
            return

        attrs = {
            "transition": f"{from_phase}→{to_phase}",
            "from_phase": from_phase,
            "to_phase": to_phase,
            "tick": tick,
        }
        if triggered_by:
            attrs["triggered_by"] = triggered_by
        if metrics:
            attrs.update({
                "stability_score": round(metrics.stability_score, 4),
                "global_health_score": round(metrics.global_health_score, 4),
                "retry_count": metrics.retry_count,
            })

        with self.tracer.start_as_current_span(f"transition_{from_phase}_to_{to_phase}") as span:
            span.set_attributes(attrs)
            span.add_event("phase_change", attrs)

    def record_retry_storm(
        self,
        tick: int,
        retry_volume: int,
        affected_nodes: list,
        latency_amplifier: float,
        p95_latency: float,
    ):
        """Record a retry storm event with full context."""
        if not self._enabled:
            return

        attrs = {
            "event.type": "retry_storm",
            "tick": tick,
            "retry.count": retry_volume,
            "affected_nodes": str(affected_nodes),
            "latency_amplifier": latency_amplifier,
            "p95_latency_ms": round(p95_latency, 3),
        }

        with self.tracer.start_as_current_span("retry_storm") as span:
            span.set_attributes(attrs)
            span.add_event("retry_storm_detected", attrs)

    def record_fragmentation(
        self,
        tick: int,
        fragmented_node_ids: list,
        primary_node: int,
        spreading_factor: float,
        fragmentation_threshold_ms: float,
    ):
        """Record fragmentation cascade onset."""
        if not self._enabled:
            return

        attrs = {
            "event.type": "fragmentation",
            "tick": tick,
            "fragmentation.state": "active",
            "primary_node_id": primary_node,
            "fragmented.node_ids": str(fragmented_node_ids),
            "fragmented.count": len(fragmented_node_ids),
            "spreading_factor": spreading_factor,
            "fragmentation_threshold_ms": fragmentation_threshold_ms,
        }

        with self.tracer.start_as_current_span("fragmentation_cascade") as span:
            span.set_attributes(attrs)
            span.add_event("fragmentation_detected", attrs)

    def record_recovery_initiation(
        self,
        tick: int,
        fragmented_node_ids: list,
        retry_volume: int,
        load_shedding_active: bool,
        traffic_decay_factor: float,
    ):
        """Record recovery procedure initiation."""
        if not self._enabled:
            return

        attrs = {
            "event.type": "recovery_initiation",
            "tick": tick,
            "fragmented.node_ids": str(fragmented_node_ids),
            "fragmented.count": len(fragmented_node_ids),
            "retry_count": retry_volume,
            "load_shedding_active": load_shedding_active,
            "traffic_decay_factor": round(traffic_decay_factor, 4),
        }

        with self.tracer.start_as_current_span("recovery_initiation") as span:
            span.set_attributes(attrs)
            span.add_event("recovery_started", attrs)

    def record_recovery_outcome(
        self,
        tick: int,
        outcome: str,
        final_health_score: float,
        final_stability_score: float,
        recovered_nodes: int,
        remaining_fragmented: int,
        total_ticks: int,
    ):
        """Record final recovery outcome."""
        if not self._enabled:
            return

        attrs = {
            "event.type": "recovery_outcome",
            "tick": tick,
            "recovery.outcome": outcome,
            "final_health_score": round(final_health_score, 4),
            "final_stability_score": round(final_stability_score, 4),
            "recovered_nodes": recovered_nodes,
            "remaining_fragmented": remaining_fragmented,
            "total_ticks": total_ticks,
        }

        with self.tracer.start_as_current_span("recovery_outcome") as span:
            span.set_attributes(attrs)
            span.add_event("simulation_complete", attrs)

    def instrument_engine(self, engine):
        """
        Monkey-patch a RecoveryEngine instance to emit OTel spans on every tick.
        Call this after creating the engine but before calling engine.run().

        Example:
            engine = RecoveryEngine(config)
            OTelSpanRecorder().instrument_engine(engine)
            metrics = engine.run()  # spans emitted automatically
        """
        if not self._enabled:
            logger.info("[OTel] Instrumenting engine (no-op mode)")
            return

        recorder = self
        original_tick_step = engine.tick_step

        def instrumented_tick_step():
            # Phase transition detection
            prev_phase = getattr(engine, 'phase', 'UNKNOWN')

            # Execute tick
            original_tick_step()

            # Record tick span
            if engine.metrics_history:
                latest = engine.metrics_history[-1]
                recorder.record_tick_span(
                    tick=latest.tick,
                    phase=engine.phase,
                    metrics=latest,
                    node_states=engine.nodes,
                )

                # Detect phase transitions
                if engine.phase != prev_phase:
                    recorder.record_state_transition(
                        from_phase=prev_phase,
                        to_phase=engine.phase,
                        tick=latest.tick,
                        metrics=latest,
                    )

                    # Specialized event recording per transition
                    if engine.phase == "FRAGMENTATION":
                        frag_ids = [nid for nid, n in engine.nodes.items() if n.fragmented]
                        fi = engine.cfg["failure_injection"]
                        recorder.record_fragmentation(
                            tick=latest.tick,
                            fragmented_node_ids=frag_ids,
                            primary_node=fi["target_node_id"],
                            spreading_factor=engine.cfg["_fragmentation"]["spreading_factor"],
                            fragmentation_threshold_ms=fi["fragmentation_threshold_ms"],
                        )
                    elif engine.phase == "RECOVERY_INITIATION":
                        frag_ids = [nid for nid, n in engine.nodes.items() if n.fragmented]
                        recorder.record_recovery_initiation(
                            tick=latest.tick,
                            fragmented_node_ids=frag_ids,
                            retry_volume=engine.retry_volume,
                            load_shedding_active=engine.cfg["recovery"]["load_shedding_active"],
                            traffic_decay_factor=0.5 ** (1.0 / engine.cfg["recovery"]["traffic_decay_half_life_ticks"]),
                        )
                    elif engine.phase == "RECOVERY_OUTCOME" and engine.recovery_outcome:
                        final = latest
                        recovered = sum(1 for n in engine.nodes.values() if not n.fragmented)
                        recorder.record_recovery_outcome(
                            tick=latest.tick,
                            outcome=engine.recovery_outcome,
                            final_health_score=final.global_health_score,
                            final_stability_score=final.stability_score,
                            recovered_nodes=recovered,
                            remaining_fragmented=final.fragmented_nodes,
                            total_ticks=engine.total_ticks,
                        )

            # Retry storm detection
            if engine.retry_volume > 100:
                storm_nodes = [nid for nid, n in engine.nodes.items() if n.in_retry_storm]
                recorder.record_retry_storm(
                    tick=engine.tick,
                    retry_volume=engine.retry_volume,
                    affected_nodes=storm_nodes,
                    latency_amplifier=engine.cfg["retry_storm_model"]["storm_latency_amplifier"],
                    p95_latency=engine.metrics_history[-1].p95_latency if engine.metrics_history else 0,
                )

        engine.tick_step = instrumented_tick_step
        logger.info("[OTel] Engine instrumented — spans will emit on each tick")


# ─────────────────────────────────────────────────────────────────────────────
# Prometheus metrics bridge (exposes telemetry for Prometheus scraping)
# ─────────────────────────────────────────────────────────────────────────────

class PrometheusMetricsBridge:
    """
    Exposes current simulation metrics as Prometheus gauge/counter values.
    Scraped by Prometheus every scrape_interval from the /metrics endpoint.

    Metrics exposed:
      cascade_global_health_score       (gauge, 0-1)
      cascade_stability_score          (gauge, 0-1)
      cascade_p50_latency              (gauge, ms)
      cascade_p95_latency              (gauge, ms)
      cascade_queue_depth              (gauge)
      cascade_retry_count              (gauge)
      cascade_fragmented_nodes         (gauge)
      cascade_edge_heat_avg            (gauge, 0-1)
      cascade_edge_heat_peak            (gauge, 0-1)
      cascade_active_nodes             (gauge)
      cascade_total_requests           (counter)
      cascade_failed_requests          (counter)
      cascade_recovery_outcome         (gauge, 0=unknown 1=full 2=partial 3=osc 4=collapse)
    """

    OUTCOME_MAP = {
        "in_progress": 0,
        "full_recovery": 1,
        "partial_recovery": 2,
        "oscillation": 3,
        "secondary_collapse": 4,
    }

    def __init__(self):
        self._metrics = {
            "cascade_global_health_score": 1.0,
            "cascade_stability_score": 1.0,
            "cascade_p50_latency": 0.0,
            "cascade_p95_latency": 0.0,
            "cascade_queue_depth": 0,
            "cascade_retry_count": 0,
            "cascade_fragmented_nodes": 0,
            "cascade_edge_heat_avg": 0.0,
            "cascade_edge_heat_peak": 0.0,
            "cascade_active_nodes": 12,
            "cascade_total_requests": 0,
            "cascade_failed_requests": 0,
            "cascade_recovery_outcome": 0,
        }
        self._tick = 0

    def update(self, metrics_record):
        """Update bridge state from a SimulationMetrics instance."""
        self._tick += 1
        self._metrics["cascade_global_health_score"] = round(
            metrics_record.global_health_score, 4
        )
        self._metrics["cascade_stability_score"] = round(
            metrics_record.stability_score, 4
        )
        self._metrics["cascade_p50_latency"] = round(
            metrics_record.p50_latency, 3
        )
        self._metrics["cascade_p95_latency"] = round(
            metrics_record.p95_latency, 3
        )
        self._metrics["cascade_queue_depth"] = metrics_record.queue_depth
        self._metrics["cascade_retry_count"] = metrics_record.retry_count
        self._metrics["cascade_fragmented_nodes"] = metrics_record.fragmented_nodes
        self._metrics["cascade_edge_heat_avg"] = round(
            metrics_record.edge_heat_avg, 4
        )
        self._metrics["cascade_edge_heat_peak"] = round(
            metrics_record.edge_heat_peak, 4
        )
        self._metrics["cascade_active_nodes"] = metrics_record.active_nodes
        self._metrics["cascade_total_requests"] = metrics_record.total_requests
        self._metrics["cascade_failed_requests"] = metrics_record.failed_requests
        self._metrics["cascade_recovery_outcome"] = self.OUTCOME_MAP.get(
            metrics_record.recovery_outcome, 0
        )

    def instrument_engine(self, engine):
        """Attach Prometheus bridge to a RecoveryEngine instance."""
        bridge = self
        original_tick_step = engine.tick_step

        def instrumented():
            original_tick_step()
            if engine.metrics_history:
                bridge.update(engine.metrics_history[-1])

        engine.tick_step = instrumented

    def render_prometheus_text(self) -> str:
        """Render current metrics in Prometheus text exposition format."""
        lines = ["# HELP cascade_global_health_score Global health score (0-1)", ]
        for name, value in self._metrics.items():
            lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Flask/HTTP metrics server (serves /metrics endpoint)
# ─────────────────────────────────────────────────────────────────────────────

def start_metrics_server(port: int = 9090, bridge: PrometheusMetricsBridge = None):
    """
    Start a lightweight HTTP server on port that serves /metrics endpoint.
    For use in local dev / docker environments where Prometheus scrapes this service.

    Requires: pip install flask

    Usage:
        bridge = PrometheusMetricsBridge()
        bridge.instrument_engine(engine)
        start_metrics_server(port=9090, bridge=bridge)
    """
    try:
        from flask import Flask, Response
    except ImportError:
        logger.warning("[Metrics] flask not installed — /metrics endpoint unavailable")
        return

    app = Flask(__name__)
    _bridge = bridge or PrometheusMetricsBridge()

    @app.route("/metrics")
    def metrics():
        text = _bridge.render_prometheus_text()
        return Response(text, mimetype="text/plain; charset=utf-8")

    @app.route("/health")
    def health():
        return {"status": "ok", "tick": _bridge._tick}

    logger.info(f"[Metrics] Starting HTTP server on :{port}")
    app.run(host="0.0.0.0", port=port, debug=False)