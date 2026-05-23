"""
Pytest configuration for paper_trading tests.
"""

import pytest
import sys

sys.path.insert(0, "/home/alper/trading_system")

from paper_trading.engine import PaperExchange
from paper_trading.market_realism import MarketRealismConfig


@pytest.fixture
def exchange():
    """Paper exchange with deterministic settings for testing."""
    return PaperExchange(initial_capital=100_000.0, base_latency_ms=5.0)


@pytest.fixture
def config():
    return MarketRealismConfig(
        enable_order_book=True,
        enable_volatility_regimes=True,
        enable_adversarial=True,
        base_latency_ms=10.0,
        latency_jitter_ms=2.0,
        random_rejection_rate=0.01,
        delayed_fill_prob=0.0,
        latency_spike_prob=0.0,
        enable_stale_snapshot=True,
        stale_snapshot_prob=0.0,
    )
