"""Monitoring utilities: latency tracking, error rate, Prometheus metrics."""

import time
from functools import wraps
from prometheus_client import Counter, Histogram, Gauge, Info
from logging.logger import get_logger

log = get_logger(__name__)

# ── Metrics ──────────────────────────────────────────────────────────────────

request_latency = Histogram(
    "http_request_latency_seconds",
    "HTTP request latency",
    ["method", "endpoint", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

trade_counter = Counter(
    "trades_executed_total",
    "Total trades executed",
    ["symbol", "side", "status"],
)

ai_decision_counter = Counter(
    "ai_decisions_total",
    "Total AI decisions",
    ["symbol", "action", "accepted"],
)

risk_rejection_counter = Counter(
    "risk_rejections_total",
    "Trades rejected by risk engine",
    ["symbol", "reason"],
)

position_gauge = Gauge(
    "open_positions",
    "Current number of open positions",
)

equity_gauge = Gauge(
    "total_equity_usdt",
    "Total account equity in USDT",
)

daily_pnl_gauge = Gauge(
    "daily_pnl_usdt",
    "Today's realized PnL in USDT",
)


def track_latency(metric: Histogram = request_latency):
    """Decorator to track function latency."""

    def decorator(fn):
        @wraps(fn)
        async def async_wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
                return result
            finally:
                duration = time.perf_counter() - start
                metric.labels(method="internal", endpoint=fn.__name__, status="ok").observe(duration)

        @wraps(fn)
        def sync_wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
                return result
            finally:
                duration = time.perf_counter() - start
                metric.labels(method="internal", endpoint=fn.__name__, status="ok").observe(duration)

        import asyncio
        if asyncio.iscoroutinefunction(fn):
            return async_wrapper
        return sync_wrapper

    return decorator


# ── System Info ───────────────────────────────────────────────────────────────

system_info = Info("trading_system", "Trading system information")
system_info.info({
    "version": "1.0.0",
    "symbol": "BTC/USDT",
    "environment": "development",
})