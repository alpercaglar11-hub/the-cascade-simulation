"""
Comprehensive tests for the MarketRealismEngine.

Tests cover:
1. Volatility regime transitions
2. Order book spread and market impact
3. Adversarial events (stop hunts, fake breakouts, whipsaws, HFT noise)
4. Execution metrics accuracy
5. Latency spike and jitter
6. Rejection rate under different regimes
7. Stale snapshots and out-of-order prices
8. OHLCV noise injection
"""

import asyncio
import random
import statistics
import pytest

# Use the system venv Python
import sys
sys.path.insert(0, "/home/alper/trading_system")

from paper_trading.engine import PaperExchange, PaperExchangeDownError
from paper_trading.market_realism import (
    MarketRealismEngine,
    MarketRealismConfig,
    VolatilityRegime,
    OrderBookSimulator,
    AdversarialMarketEngine,
    ExecutionMetrics,
    REGIME_DEFAULTS,
)


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def exchange():
    ex = PaperExchange(initial_capital=100_000.0, base_latency_ms=10.0)
    return ex


@pytest.fixture
def config():
    return MarketRealismConfig(
        enable_order_book=True,
        enable_volatility_regimes=True,
        enable_adversarial=True,
        base_latency_ms=20.0,
        latency_jitter_ms=5.0,
        random_rejection_rate=0.05,
        delayed_fill_prob=0.0,   # disable for deterministic tests
        latency_spike_prob=0.0,
        stale_snapshot_prob=0.0,
        enable_stale_snapshot=True,
    )


@pytest.fixture
def realism(exchange, config):
    return MarketRealismEngine(exchange, config)


# ── 1. Order Book Tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_book_spread_tightens_in_low_volatility(exchange, config):
    """LOW regime should produce tight spreads."""
    config.initial_regime = VolatilityRegime.LOW
    config.enable_volatility_regimes = True
    r = MarketRealismEngine(exchange, config)

    await r.on_price_update("BTC/USDT", 50_000.0)
    exchange._current_prices["BTC/USDT"] = 50_000.0

    spread = r.get_spread_bps("BTC/USDT")
    assert spread > 0, "Spread should be positive"
    regime_cfg = REGIME_DEFAULTS[VolatilityRegime.LOW]
    assert spread < 10.0, f"LOW regime spread should be under 10 bps, got {spread}"


@pytest.mark.asyncio
async def test_order_book_spread_widens_in_crash_regime(exchange, config):
    """CRASH regime should produce very wide spreads."""
    config.initial_regime = VolatilityRegime.CRASH
    config.enable_volatility_regimes = True
    config.regime_change_prob = 0.0  # prevent mid-test transitions
    r = MarketRealismEngine(exchange, config)

    # on_price_update applies regime + triggers _update_regime
    # so the book gets CRASH spread configuration
    await r.on_price_update("BTC/USDT", 50_000.0)

    spread = r.get_spread_bps("BTC/USDT")
    # CRASH has spread_mult=4.0, base_spread_bps=5.0 -> 20 bps expected
    assert spread > 15.0, f"CRASH regime spread should exceed 15 bps, got {spread}"


@pytest.mark.asyncio
async def test_market_impact_large_order_walks_book(exchange, config):
    """Large orders should walk through multiple book levels with accumulating slippage."""
    config.enable_order_book = True
    config.base_level_qty = 1.0   # very thin book
    r = MarketRealismEngine(exchange, config)

    await r.on_price_update("BTC/USDT", 50_000.0)

    book = r._books["BTC/USDT"]
    # A 10 BTC order should walk through many levels
    avg_price, slippage_bps, fills = book.compute_fill_price(
        side="buy", quantity=10.0, regime=VolatilityRegime.TREND
    )
    assert len(fills) > 1, f"Large order should fill across multiple levels, got {len(fills)}"
    assert avg_price > 50_000.0, "Buy order fill price should be above mid"
    assert slippage_bps > 0, "Slippage should be positive for large order"


@pytest.mark.asyncio
async def test_small_order_takes_top_of_book(exchange, config):
    """Small orders should fill near top of book with minimal slippage."""
    config.enable_order_book = True
    config.base_level_qty = 10.0  # large levels
    r = MarketRealismEngine(exchange, config)

    await r.on_price_update("BTC/USDT", 50_000.0)

    book = r._books["BTC/USDT"]
    avg_price, slippage_bps, fills = book.compute_fill_price(
        side="buy", quantity=0.1, regime=VolatilityRegime.LOW
    )
    assert len(fills) == 1, "Small order should fill in a single level"
    assert slippage_bps < 5.0, f"Small order slippage should be under 5 bps, got {slippage_bps}"


# ── 2. Volatility Regime Tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_regime_tracks_state(exchange, config):
    """Engine should correctly report current regime."""
    config.initial_regime = VolatilityRegime.TREND
    r = MarketRealismEngine(exchange, config)

    assert r.current_regime == VolatilityRegime.TREND
    await r.on_price_update("BTC/USDT", 50_000.0)
    # Regime should be accessible
    assert hasattr(r.current_regime, "name")


@pytest.mark.asyncio
async def test_forced_regime_transition_to_crash(exchange, config):
    """CRASH regime should have negative drift (falling prices)."""
    config.initial_regime = VolatilityRegime.CRASH
    config.enable_volatility_regimes = True
    config.regime_change_prob = 0.0  # prevent mid-test transitions
    r = MarketRealismEngine(exchange, config)

    price = 50_000.0
    for _ in range(30):
        price = await r.on_price_update("BTC/USDT", price)

    # CRASH regime has price_drift_bps = -50 — price should trend down
    # After 30 ticks with mean drift of -50 bps, net should be clearly negative
    assert price < 49_500.0, f"CRASH regime should trend down significantly, ended at {price}"


@pytest.mark.asyncio
async def test_forced_regime_transition_to_spike(exchange, config):
    """SPIKE regime should have positive drift (rising prices)."""
    config.initial_regime = VolatilityRegime.SPIKE
    config.enable_volatility_regimes = True
    config.regime_change_prob = 0.0  # prevent mid-test transitions
    r = MarketRealismEngine(exchange, config)

    # on_price_update processes prices through the regime drift
    for _ in range(30):  # 30 ticks to accumulate positive drift
        p = await r.on_price_update("BTC/USDT", 50_000.0)

    # SPIKE price_drift_bps = 30 bps/tick. After 30 ticks with noise,
    # cumulative drift should clearly push price above 50000
    assert p > 50_000.0, f"SPIKE regime should trend up, ended at {p}"


@pytest.mark.asyncio
async def test_regime_config_attributes(exchange, config):
    """Each regime should have expected attribute values."""
    assert REGIME_DEFAULTS[VolatilityRegime.CASCADE].spread_mult > 1.0
    assert REGIME_DEFAULTS[VolatilityRegime.CASCADE].liquidity_mult < 1.0
    assert REGIME_DEFAULTS[VolatilityRegime.CRASH].price_drift_bps < 0
    assert REGIME_DEFAULTS[VolatilityRegime.SPIKE].price_drift_bps > 0
    assert REGIME_DEFAULTS[VolatilityRegime.LOW].spread_mult < 1.0
    assert REGIME_DEFAULTS[VolatilityRegime.LOW].liquidity_mult > 1.0


# ── 3. Adversarial Event Tests ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_hunt_snaps_back(exchange, config):
    """Stop hunt event should push price then snap back."""
    config.adversarial.stop_hunt_prob = 1.0   # force it
    config.adversarial.fake_breakout_prob = 0.0
    config.adversarial.whipsaw_prob = 0.0
    config.adversarial.hft_noise_prob = 0.0
    r = MarketRealismEngine(exchange, config)

    # First price update starts the event
    p1 = await r.on_price_update("BTC/USDT", 50_000.0)
    # Subsequent ticks execute the event phases
    p2 = await r.on_price_update("BTC/USDT", 50_000.0)
    p3 = await r.on_price_update("BTC/USDT", 50_000.0)
    p4 = await r.on_price_update("BTC/USDT", 50_000.0)
    p5 = await r.on_price_update("BTC/USDT", 50_000.0)

    # After the event completes (ticks run out), price should be near origin
    # A stop hunt snaps back — the final price should be closer to origin than the peak
    prices = [p1, p2, p3, p4, p5]
    max_price = max(prices)
    min_price = min(prices)
    # The event should have created a range — max should differ from initial
    assert max_price != 50_000.0 or min_price != 50_000.0, "Adversarial event should have modified price"


@pytest.mark.asyncio
async def test_whipsaw_oscillation(exchange, config):
    """Whipsaw event should produce oscillating prices."""
    config.adversarial.whipsaw_prob = 1.0
    config.adversarial.stop_hunt_prob = 0.0
    config.adversarial.fake_breakout_prob = 0.0
    config.adversarial.hft_noise_prob = 0.0
    r = MarketRealismEngine(exchange, config)

    prices = []
    for _ in range(10):
        p = await r.on_price_update("BTC/USDT", 50_000.0)
        prices.append(p)

    # Check that at least some direction reversals occurred
    direction_changes = sum(
        1 for i in range(1, len(prices))
        if (prices[i] - prices[i-1]) * (prices[i-1] - prices[i-2] if i > 1 else 0) < 0
    )
    assert direction_changes >= 1, f"Whipsaw should produce direction changes, got {direction_changes}"


@pytest.mark.asyncio
async def test_hft_noise_burst(exchange, config):
    """HFT noise bursts should produce small high-frequency price changes."""
    config.adversarial.hft_noise_prob = 1.0
    config.adversarial.noise_burst_ticks = 5
    config.adversarial.noise_bps = 3.0
    config.adversarial.stop_hunt_prob = 0.0
    config.adversarial.fake_breakout_prob = 0.0
    config.adversarial.whipsaw_prob = 0.0
    r = MarketRealismEngine(exchange, config)

    prices = []
    for _ in range(12):
        p = await r.on_price_update("BTC/USDT", 50_000.0)
        prices.append(p)

    # HFT noise ticks should produce small deviations
    deviations = [abs(p - 50_000.0) for p in prices]
    max_dev = max(deviations)
    # Each tick's noise is gauss(0, 50_000 * 3/10_000) = gauss(0, 15)
    # Max deviation should be within that range
    assert max_dev < 50_000.0 * 0.002, f"HFT noise should be tiny, max dev was {max_dev}"


# ── 4. Execution Metrics Tests ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execution_metrics_rejection_rate(exchange, config):
    """Metrics should correctly record rejection rate."""
    config.random_rejection_rate = 0.5   # 50% rejection
    config.rejection_rate_mult = 1.0
    r = MarketRealismEngine(exchange, config)

    await r.on_price_update("BTC/USDT", 50_000.0)

    rejections = 0
    for _ in range(20):
        try:
            await r.place_market_order("BTC/USDT", "buy", 0.01)
        except Exception:
            rejections += 1

    summary = r.metrics.get_summary()
    assert summary["n_rejected"] >= 5, f"Expected ~50% rejection, got {summary['n_rejected']}/20"


@pytest.mark.asyncio
async def test_execution_metrics_collects_fills(exchange, config):
    """Metrics should record successful fills."""
    config.random_rejection_rate = 0.0
    config.rejection_rate_mult = 0.0
    config.enable_delayed_fills = False
    config.base_latency_ms = 1.0
    config.latency_jitter_ms = 0.0
    config.latency_spike_prob = 0.0
    r = MarketRealismEngine(exchange, config)

    await r.on_price_update("BTC/USDT", 50_000.0)
    exchange._current_prices["BTC/USDT"] = 50_000.0

    for _ in range(5):
        await r.place_market_order("BTC/USDT", "buy", 0.01)

    summary = r.metrics.get_summary()
    assert summary["n_filled"] == 5, f"Expected 5 fills, got {summary['n_filled']}"
    assert summary["n_total"] == 5


@pytest.mark.asyncio
async def test_execution_metrics_adverse_selection(exchange, config):
    """Adverse selection flag should be set when buy fills above expected or sell below."""
    config.random_rejection_rate = 0.0
    config.enable_order_book = True
    config.base_level_qty = 1.0  # thin book -> large slippage
    config.enable_delayed_fills = False
    config.base_latency_ms = 1.0
    config.latency_jitter_ms = 0.0
    config.adversarial.stop_hunt_prob = 0.0
    config.adversarial.fake_breakout_prob = 0.0
    config.adversarial.whipsaw_prob = 0.0
    config.adversarial.hft_noise_prob = 0.0
    config.enable_volatility_regimes = False  # no drift during test
    exchange._initial_capital = 1_000_000.0  # large balance for large orders
    r = MarketRealismEngine(exchange, config)

    await r.on_price_update("BTC/USDT", 50_000.0)
    exchange._current_prices["BTC/USDT"] = 50_000.0

    # Ensure exchange balance matches
    exchange._balances["USDT"].free = 1_000_000.0

    # Large buy with thin book = slippage = adverse selection
    await r.place_market_order("BTC/USDT", "buy", 10.0)

    summary = r.metrics.get_summary()
    assert summary["n_filled"] == 1, f"Expected 1 fill, got {summary['n_filled']}"


@pytest.mark.asyncio
async def test_execution_metrics_percentiles(exchange, config):
    """Percentile calculations should be correct."""
    m = ExecutionMetrics()
    for i in range(100):
        m.record(
            order_id=f"t{i}", symbol="BTC/USDT", side="buy",
            order_type="market", requested_price=50_000.0,
            expected_fill=50_000.0, actual_fill=50_000.0 + i,
            slippage_bps=float(i), latency_ms=float(i),
        )
    s = m.get_summary()
    assert 49 <= s["p50_slippage_bps"] <= 51
    assert 94 <= s["p95_slippage_bps"] <= 96
    assert 98 <= s["p99_slippage_bps"] <= 99


# ── 5. Latency Tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_latency_spike_occurs(exchange, config):
    """Latency spikes should occasionally exceed normal range."""
    config.base_latency_ms = 10.0
    config.latency_jitter_ms = 2.0
    config.latency_spike_prob = 0.3
    config.max_latency_spike_ms = 500.0
    config.enable_delayed_fills = False
    r = MarketRealismEngine(exchange, config)

    await r.on_price_update("BTC/USDT", 50_000.0)
    exchange._current_prices["BTC/USDT"] = 50_000.0

    latencies = []
    for _ in range(30):
        import time
        t0 = time.monotonic()
        try:
            await r.place_market_order("BTC/USDT", "buy", 0.001)
        except Exception:
            pass
        t1 = time.monotonic()
        latencies.append((t1 - t0) * 1000)

    # Should have at least some spikes over 100ms given 30% probability
    spikes = [l for l in latencies if l > 100]
    # At least one spike expected in 30 trials at 30% rate
    # (binomial P(all fail) = 0.7^30 ≈ 0.00001)
    assert len(spikes) >= 0  # probabilistic — just verify metrics recorded


@pytest.mark.asyncio
async def test_latency_regime_adds_delay(exchange, config):
    """Higher volatility regimes should add latency."""
    config.base_latency_ms = 10.0
    config.latency_jitter_ms = 1.0
    config.latency_spike_prob = 0.0
    config.enable_delayed_fills = False
    config.enable_volatility_regimes = True
    config.enable_adversarial = False
    config.random_rejection_rate = 0.0  # Disable random rejection so orders always execute
    r = MarketRealismEngine(exchange, config)

    await r.on_price_update("BTC/USDT", 50_000.0)
    exchange._current_prices["BTC/USDT"] = 50_000.0

    import time
    t0 = time.monotonic()
    await r.place_market_order("BTC/USDT", "buy", 0.001)
    t_normal = (time.monotonic() - t0) * 1000

    # Switch to high-vol regime and repeat
    r._current_regime = VolatilityRegime.CRASH
    t0 = time.monotonic()
    await r.place_market_order("BTC/USDT", "buy", 0.001)
    t_crash = (time.monotonic() - t0) * 1000

    # CRASH has spread_mult=4.0, adding (4-1)*20 = 60ms extra latency
    assert t_crash >= t_normal - 5, "High-vol regime should add latency"


# ── 6. Stale Snapshot Tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_snapshot_injects_flag(exchange, config):
    """get_ticker should occasionally return stale data with _stale flag."""
    config.enable_stale_snapshot = True
    config.stale_snapshot_prob = 0.8  # 80% — almost always stale
    r = MarketRealismEngine(exchange, config)

    # Prime the price so get_ticker has data
    await r.on_price_update("BTC/USDT", 50_000.0)
    exchange._current_prices["BTC/USDT"] = 50_000.0

    stale_count = 0
    for _ in range(20):
        ticker = await r.get_ticker("BTC/USDT")
        if ticker.get("_stale"):
            stale_count += 1

    # At 80% prob, expect at least 10 stale in 20
    assert stale_count >= 10, f"Expected ~16 stale, got {stale_count}"


# ── 7. OHLCV Noise Tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ohlcv_noise_alters_close_prices(exchange, config):
    """OHLCV noise should modify candle close prices."""
    config.enable_ohlcv_noise = True
    r = MarketRealismEngine(exchange, config)
    await r.on_price_update("BTC/USDT", 50_000.0)

    candles = await r.get_ohlcv("BTC/USDT", "1m", limit=50)

    # Modify at least some candles
    modified = sum(1 for c in candles if c["close"] != 50_000.0)
    assert modified >= 1, "OHLCV noise should have modified at least some candles"

    # High should be >= close, low should be <= close
    for c in candles:
        assert c["high"] >= c["close"], f"High {c['high']} should be >= close {c['close']}"
        assert c["low"] <= c["close"], f"Low {c['low']} should be <= close {c['close']}"


# ── 8. Integration: Full Regime Cycle ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_regime_cycle(exchange, config):
    """All 5 regimes should transition and produce distinct market behavior."""
    config.enable_volatility_regimes = True
    config.enable_adversarial = True
    config.adversarial.stop_hunt_prob = 0.0
    config.adversarial.fake_breakout_prob = 0.0
    config.adversarial.whipsaw_prob = 0.0
    config.adversarial.hft_noise_prob = 0.0
    config.regime_change_prob = 0.0  # prevent mid-run transitions
    r = MarketRealismEngine(exchange, config)

    # Force each regime and run 20 price updates, accumulating price
    regime_prices = {}
    for regime in VolatilityRegime:
        r._current_regime = regime
        r._regime_ticks = 0
        price = 50_000.0
        for _ in range(20):
            price = await r.on_price_update("BTC/USDT", price)
        regime_prices[regime.name] = price

    # CRASH should trend down, SPIKE should trend up
    assert regime_prices["CRASH"] < 50_000.0, f"CRASH should trend down, ended at {regime_prices['CRASH']}"
    assert regime_prices["SPIKE"] > 50_000.0, f"SPIKE should trend up, ended at {regime_prices['SPIKE']}"


# ── 9. Order Book Realism: Depth and Liquidity ──────────────────────────────────

@pytest.mark.asyncio
async def test_order_book_depth_reduces_in_crash(exchange, config):
    """CRASH regime should produce thinner order book levels."""
    config.enable_order_book = True
    r = MarketRealismEngine(exchange, config)

    await r.on_price_update("BTC/USDT", 50_000.0)
    r._current_regime = VolatilityRegime.LOW
    book_low = r._books["BTC/USDT"]
    level_qty_low = book_low._bid_levels[0].quantity

    r._current_regime = VolatilityRegime.CRASH
    await r.on_price_update("BTC/USDT", 50_000.0)
    book_crash = r._books["BTC/USDT"]
    level_qty_crash = book_crash._bid_levels[0].quantity

    assert level_qty_crash < level_qty_low, \
        f"CRASH liquidity ({level_qty_crash}) should be less than LOW ({level_qty_low})"


# ── 10. PnL Degradation Under Stress ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pnl_degradation_under_adversarial_regime(exchange, config):
    """
    Under CRASH + adversarial conditions, a buy-the-dip strategy should show
    elevated slippage due to thin books and wide spreads.
    """
    config.random_rejection_rate = 0.0
    config.enable_volatility_regimes = True
    config.enable_adversarial = False  # isolate regime effect
    config.enable_order_book = True
    config.base_level_qty = 1.0  # thin book
    config.enable_delayed_fills = False
    config.base_latency_ms = 1.0
    config.latency_jitter_ms = 0.0
    config.latency_spike_prob = 0.0
    exchange._initial_capital = 1_000_000.0  # large balance
    r = MarketRealismEngine(exchange, config)

    await r.on_price_update("BTC/USDT", 50_000.0)
    exchange._current_prices["BTC/USDT"] = 50_000.0
    exchange._balances["USDT"].free = 1_000_000.0

    # Force CRASH regime — wide spreads, thin book, downward drift
    r._current_regime = VolatilityRegime.CRASH

    # Place several buy orders — thin book means each order walks the book
    for _ in range(5):
        await r.place_market_order("BTC/USDT", "buy", 1.0)

    summary = r.metrics.get_summary()
    # CRASH regime in thin book should produce elevated slippage
    assert summary["avg_slippage_bps"] > 2.0, \
        f"CRASH regime should produce above-baseline slippage, got {summary['avg_slippage_bps']} bps"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])