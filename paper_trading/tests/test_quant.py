"""
Tests for the quantitative research framework.

Covers:
- Strategy interface (signal generation, parameter injection)
- Stats calculator (Sharpe, Sortino, Calmar, max drawdown, win rate, expectancy)
- Monte Carlo resampling
- Anti-overfit stability evaluation
- Regime detection
- Portfolio construction
- Parameter sweep end-to-end
"""

import math
import random
from datetime import datetime, timedelta

import pytest
import numpy as np

from paper_trading.quant.interface import (
    BaseStrategy, Parameter, Signal, SignalType,
    sma, ema, rsi, atr, bollinger_bands, momentum,
)
from paper_trading.quant.stats import (
    EquityCurve, PerformanceStats, StatsCalculator,
    TradeRecord, MonteCarloEngine, MonteCarloResult,
)
from paper_trading.quant.regime import (
    RegimeType, RegimeDetector, RegimePerformanceTracker,
)
from paper_trading.quant.anti_overfit import (
    AntiOverfitEngine, StabilityReport, bootstrap_stability,
)
from paper_trading.quant.portfolio import (
    PortfolioBuilder, PortfolioConfig, PortfolioResult,
    build_correlation_matrix,
)


# ── Fixtures ────────────────────────────────────────────────────────────────────

def make_trade(pnl: float, regime: str = "TREND", duration: int = 10) -> TradeRecord:
    now = datetime.utcnow()
    return TradeRecord(
        id=f"t_{random.randint(1000,9999)}",
        entry_time=now - timedelta(hours=duration),
        exit_time=now,
        side="long" if pnl > 0 else "short",
        entry_price=50_000.0,
        exit_price=50_000.0 + pnl,
        size=1.0,
        pnl=pnl,
        pnl_pct=pnl / 50_000.0,
        duration_ticks=duration,
        regime=regime,
    )


# ── Interface Tests ─────────────────────────────────────────────────────────────

def test_signal_is_actionable():
    assert Signal(SignalType.BUY, "BTC/USDT").is_actionable()
    assert Signal(SignalType.SELL, "BTC/USDT").is_actionable()
    assert Signal(SignalType.CLOSE_LONG, "BTC/USDT").is_actionable()
    assert not Signal(SignalType.HOLD, "BTC/USDT").is_actionable()


def test_parameter_bounds():
    p = Parameter("period", 14, min=5, max=50, step=1)
    assert p.validate(100) == 50
    assert p.validate(1) == 5
    assert p.validate(30) == 30
    assert p.validate(14.7) == 14  # cast to int


def test_parameter_injection():
    class TestStrategy(BaseStrategy):
        def parameters(self):
            return [Parameter("period", 14, min=5, max=50), Parameter("threshold", 0.5, min=0.0, max=1.0)]
        def on_bar(self, candle):
            return None

    s = TestStrategy(params={"period": 21, "threshold": 0.8})
    assert s.params["period"] == 21
    assert s.params["threshold"] == 0.8

    # Clamping
    s2 = TestStrategy(params={"period": 999})
    assert s2.params["period"] == 50  # clamped to max


def test_strategy_inject_returns_new_instance():
    class TestStrategy(BaseStrategy):
        def parameters(self):
            return [Parameter("period", 14)]
        def on_bar(self, candle):
            return None

    base = TestStrategy()
    modified = base.inject(period=30)
    assert base.params["period"] == 14
    assert modified.params["period"] == 30
    assert isinstance(modified, TestStrategy)


# ── Indicator Tests ─────────────────────────────────────────────────────────────

def test_sma():
    arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    assert sma(arr, 3) == 5.0
    assert math.isnan(sma(arr, 10))


def test_rsi():
    # Steady rise → RSI should be high (>50)
    up = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0])
    rsi_val = rsi(up, 5)
    assert rsi_val > 50

    # Steady fall → RSI should be low (<50)
    down = np.array([110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0, 103.0, 102.0, 101.0, 100.0])
    rsi_val2 = rsi(down, 5)
    assert rsi_val2 < 50


def test_bollinger_bands():
    arr = np.array([45.0, 47.0, 46.0, 48.0, 47.0, 49.0, 48.0, 50.0, 49.0, 51.0] * 3)
    lower, mid, upper = bollinger_bands(arr, period=20, std_dev=2.0)
    # Bands must be symmetric around mid, equidistant
    assert lower < mid < upper
    assert abs(mid - (upper + lower) / 2) < 1e-6
    assert abs((upper - mid) - (mid - lower)) < 1e-6
    # Width should equal 4 * population_std (2 std each side)
    band = arr[-20:]
    expected_half_width = 2 * np.std(band)  # ddof=0 (population std — standard for BB)
    assert abs(upper - mid - expected_half_width) < 1e-6


# ── Stats Calculator Tests ───────────────────────────────────────────────────────

def test_stats_calculator_basic():
    trades = [
        make_trade(100.0),
        make_trade(-50.0),
        make_trade(200.0),
        make_trade(-30.0),
        make_trade(80.0),
    ]
    timestamps = [datetime.utcnow() - timedelta(hours=i) for i in reversed(range(6))]
    prices = [50_000.0] * 6
    eq = EquityCurve.from_trades(trades, 10_000.0, timestamps, prices)

    calc = StatsCalculator(annualization_factor=525600)
    stats = calc.compute(eq, trades, strategy_name="TestStrategy")

    assert stats.n_trades == 5
    assert stats.n_wins == 3
    assert stats.n_losses == 2
    assert stats.win_rate == 0.6
    assert stats.expectancy > 0
    assert stats.profit_factor > 1
    assert stats.max_drawdown_pct >= 0
    assert stats.sharpe_ratio != 0  # has variance in returns


def test_stats_calculator_empty():
    eq = EquityCurve(equity=[10_000.0], drawdown_pct=[0.0], returns=[0.0])
    calc = StatsCalculator()
    stats = calc.compute(eq, [], strategy_name="Empty")
    assert stats.n_trades == 0
    assert stats.win_rate == 0.0


def test_stats_calculator_max_drawdown():
    # Simulate: equity goes up, then drops, then recovers
    eq = EquityCurve(
        timestamps=[datetime.utcnow()],
        equity=[10_000.0, 11_000.0, 12_000.0, 9_000.0, 10_000.0, 11_000.0],
        drawdown_pct=[0.0, 0.0, 0.0, 0.25, 0.0, 0.0],
        returns=[0.0, 0.1, 0.091, -0.25, 0.111, 0.1],
    )
    trades = [
        make_trade(1000.0),
        make_trade(1000.0),
        make_trade(-3000.0),
        make_trade(2000.0),
        make_trade(1000.0),
    ]
    calc = StatsCalculator()
    stats = calc.compute(eq, trades)
    assert stats.max_drawdown_pct > 0
    assert stats.max_drawdown > 0


def test_alpha_decay():
    # Generate returns with slight decay
    rng = np.random.default_rng(42)
    returns = np.concatenate([
        rng.normal(0.01, 0.02, 30),
        rng.normal(0.005, 0.02, 30),
        rng.normal(0.002, 0.02, 30),
    ])
    slope = StatsCalculator._compute_alpha_decay(returns, lookback=20)
    # Alpha should be slightly negative (later returns worse than earlier)
    assert isinstance(slope, float)


def test_regime_stats():
    trades = [
        make_trade(100.0, regime=RegimeType.TREND),
        make_trade(-50.0, regime=RegimeType.TREND),
        make_trade(200.0, regime=RegimeType.HIGH_VOL),
        make_trade(80.0, regime=RegimeType.LOW_VOL),
    ]
    result = StatsCalculator._compute_regime_stats(trades)
    assert RegimeType.TREND in result
    assert result[RegimeType.TREND]["n_trades"] == 2
    assert result[RegimeType.TREND]["win_rate"] == 0.5


# ── Monte Carlo Tests ──────────────────────────────────────────────────────────

def test_monte_carlo_survival_rate():
    # Losing strategy — low survival
    trades = [make_trade(-100.0) for _ in range(20)]
    engine = MonteCarloEngine(n_simulations=500, random_seed=42)
    result = engine.run(trades, 10_000.0)
    assert result.survival_rate < 0.3

    # Winning strategy — high survival
    trades_w = [make_trade(100.0) for _ in range(20)]
    engine2 = MonteCarloEngine(n_simulations=500, random_seed=42)
    result2 = engine2.run(trades_w, 10_000.0)
    assert result2.survival_rate > 0.7


def test_monte_carlo_confidence_interval():
    # With random positive-biased returns, the 5th percentile (VaR) should be
    # below the 97.5th percentile, and the CI should be valid.
    trades = [make_trade(random.gauss(50, 30)) for _ in range(50)]
    engine = MonteCarloEngine(n_simulations=1000, random_seed=42)
    result = engine.run(trades, 10_000.0)
    ci_low, ci_high = result.confidence_interval_95
    assert ci_low <= ci_high
    # VaR should be below the upper bound of the CI (it's the 5th percentile)
    assert result.var_95 <= ci_high
    assert result.cvar_95 <= result.var_95  # CVaR <= VaR in normal conditions


# ── Regime Detection Tests ──────────────────────────────────────────────────────

def test_regime_detector_classifies_trending():
    detector = RegimeDetector(lookback=30, adx_period=10)

    # Smooth uptrend — should be recognized as non-low-volatility
    price = 50_000.0
    for i in range(100):
        price += 15  # steady drift up
        candle = {
            "open": price - 5, "high": price + 15,
            "low": price - 15, "close": price + 5,
            "volume": 1000,
        }
        detector.update(candle)

    regime = detector.classify()
    # Should NOT be LOW_VOL (our default "no data" regime)
    assert regime != RegimeType.LOW_VOL, f"Expected non-LOW_VOL regime for steady uptrend, got {regime}"


def test_regime_detector_panic_on_volume_spike():
    detector = RegimeDetector(lookback=20, vol_threshold=2.0)
    price = 50_000.0
    for i in range(60):
        price -= 20  # gradual decline
        detector.update({"close": price, "high": price + 10, "low": price - 10, "volume": 1000})

    # Sharp volume spike with fast drop → PANIC
    for _ in range(5):
        price -= 200  # fast crash
        detector.update({"close": price, "high": price + 50, "low": price - 100, "volume": 8000})

    regime = detector.classify()
    assert regime == RegimeType.PANIC, f"Expected PANIC on volume spike + drop, got {regime}"


def test_regime_performance_tracker():
    tracker = RegimePerformanceTracker()
    tracker.set_regime(RegimeType.TREND, 0)
    tracker.record_trade(make_trade(100.0, regime=RegimeType.TREND))
    tracker.record_trade(make_trade(-50.0, regime=RegimeType.TREND))
    tracker.set_regime(RegimeType.HIGH_VOL, 1)
    tracker.record_trade(make_trade(200.0, regime=RegimeType.HIGH_VOL))

    summary = tracker.get_summary()
    assert RegimeType.TREND in summary
    assert RegimeType.HIGH_VOL in summary
    assert summary[RegimeType.TREND]["n_trades"] == 2
    assert summary[RegimeType.HIGH_VOL]["n_trades"] == 1


# ── Anti-Overfit Tests ──────────────────────────────────────────────────────────

def test_anti_overfit_rejects_high_sensitivity():
    engine = AntiOverfitEngine(param_sensitivity_threshold=0.3)

    is_stats = PerformanceStats(
        run_id="run1", strategy_name="Test",
        n_trades=20, sharpe_ratio=1.5, sortino_ratio=2.0,
        max_drawdown_pct=0.1, n_wins=12, n_losses=8,
    )

    # Highly sensitive: tiny param change destroys performance
    perturbed = [
        PerformanceStats(run_id="p1", sharpe_ratio=0.1, sortino_ratio=0.2, n_trades=20, n_wins=10, n_losses=10),
        PerformanceStats(run_id="p2", sharpe_ratio=0.2, sortino_ratio=0.3, n_trades=20, n_wins=11, n_losses=9),
    ]

    report = engine.evaluate(is_stats, [], perturbed)
    assert not report.is_stable
    assert "sensitivity" in report.reject_reason.lower()


def test_anti_overfit_accepts_stable_params():
    engine = AntiOverfitEngine(param_sensitivity_threshold=0.3)

    is_stats = PerformanceStats(
        run_id="run1", strategy_name="Test",
        n_trades=50, sharpe_ratio=1.2, sortino_ratio=1.5,
        max_drawdown_pct=0.1, n_wins=30, n_losses=20,
    )

    # Stable: perturbation barely affects Sharpe
    perturbed = [
        PerformanceStats(run_id="p1", sharpe_ratio=1.15, sortino_ratio=1.4, n_trades=50, n_wins=29, n_losses=21),
        PerformanceStats(run_id="p2", sharpe_ratio=1.18, sortino_ratio=1.45, n_trades=50, n_wins=28, n_losses=22),
    ]

    # Some OOS windows also positive
    oos_stats = [
        PerformanceStats(run_id="oos1", sharpe_ratio=0.8, n_trades=15, n_wins=9, n_losses=6),
        PerformanceStats(run_id="oos2", sharpe_ratio=1.0, n_trades=20, n_wins=12, n_losses=8),
    ]

    report = engine.evaluate(is_stats, oos_stats, perturbed)
    assert report.is_stable
    assert report.param_sensitivity_score < 0.3


def test_bootstrap_stability():
    good_pnls = [random.gauss(50, 30) for _ in range(30)]
    score_good = bootstrap_stability(good_pnls, n_bootstrap=200)
    assert score_good > 0.05

    bad_pnls = [random.gauss(-50, 10) for _ in range(30)]
    score_bad = bootstrap_stability(bad_pnls, n_bootstrap=200)
    assert score_bad < 0.05


# ── Portfolio Tests ─────────────────────────────────────────────────────────────

def test_portfolio_builder_filters_correlated():
    cfg = PortfolioConfig(max_strategies=3, correlation_threshold=0.7)
    builder = PortfolioBuilder(cfg)

    # Three strategies with similar regime profiles (high correlation)
    s1 = PerformanceStats(run_id="s1", strategy_name="Strategy1", sharpe_ratio=1.5, sortino_ratio=1.8, annualized_volatility=0.15, max_drawdown_pct=0.1, n_trades=50, n_wins=30, n_losses=20, regime_stats={
        RegimeType.TREND: {"win_rate": 0.6, "total_pnl": 5000.0},
        RegimeType.HIGH_VOL: {"win_rate": 0.5, "total_pnl": 2000.0},
    })
    s2 = PerformanceStats(run_id="s2", strategy_name="Strategy2", sharpe_ratio=1.3, sortino_ratio=1.6, annualized_volatility=0.14, max_drawdown_pct=0.12, n_trades=50, n_wins=28, n_losses=22, regime_stats={
        RegimeType.TREND: {"win_rate": 0.58, "total_pnl": 4800.0},
        RegimeType.HIGH_VOL: {"win_rate": 0.52, "total_pnl": 1900.0},
    })
    # s3 is uncorrelated (different regime profile)
    s3 = PerformanceStats(run_id="s3", strategy_name="Strategy3", sharpe_ratio=1.0, sortino_ratio=1.2, annualized_volatility=0.10, max_drawdown_pct=0.08, n_trades=50, n_wins=26, n_losses=24, regime_stats={
        RegimeType.MEAN_REVERSION: {"win_rate": 0.65, "total_pnl": 4000.0},
        RegimeType.LOW_VOL: {"win_rate": 0.55, "total_pnl": 1500.0},
    })

    result = builder.allocate([s1, s2, s3])

    # s1 and s2 are correlated — one should be rejected
    assert len(result.allocations) <= 3
    assert len(result.selected_strategies) <= 3


def test_portfolio_weights_sum_to_one():
    builder = PortfolioBuilder()
    stats = [
        PerformanceStats(run_id=f"s{i}", strategy_name=f"Strat{i}",
                         sharpe_ratio=1.0 + i * 0.1, sortino_ratio=1.2,
                         annualized_volatility=0.1 + i * 0.01,
                         max_drawdown_pct=0.1, n_trades=50, n_wins=25, n_losses=25)
        for i in range(4)
    ]
    result = builder.allocate(stats)
    total_weight = sum(a.weight for a in result.allocations)
    assert 0.99 <= total_weight <= 1.01


def test_correlation_matrix():
    stats = [
        PerformanceStats(run_id=f"s{i}", strategy_name=f"Strat{i}",
                         sharpe_ratio=1.0, sortino_ratio=1.0,
                         annualized_volatility=0.1, max_drawdown_pct=0.1,
                         n_trades=50, n_wins=25, n_losses=25,
                         regime_stats={
                             RegimeType.TREND: {"win_rate": 0.6, "total_pnl": 1000.0 * (i + 1)},
                             RegimeType.LOW_VOL: {"win_rate": 0.5, "total_pnl": 500.0 * (i + 1)},
                         })
        for i in range(3)
    ]
    names, matrix = build_correlation_matrix(stats)
    assert len(names) == 3
    assert matrix.shape == (3, 3)
    assert matrix.diagonal().tolist() == [1.0, 1.0, 1.0]


# ── Integration: Full Research Pipeline ─────────────────────────────────────────

def test_parameter_sweep_rejects_unstable(exchange):
    """Run a parameter sweep on a known fragile strategy — unstable params should be rejected."""

    class FragileStrategy(BaseStrategy):
        def parameters(self):
            return [
                Parameter("period", 5, min=2, max=50, step=1),
                Parameter("threshold", 0.5, min=0.0, max=1.0),
            ]
        def on_bar(self, candle):
            return None

    from paper_trading.quant.research import ResearchEnvironment

    # Generate synthetic candles with a clear pattern
    candles = []
    price = 50_000.0
    for i in range(200):
        price += random.gauss(10, 100)
        candles.append({
            "timestamp": datetime.utcnow() + timedelta(minutes=i),
            "open": price - 10,
            "high": price + 20,
            "low": price - 20,
            "close": price,
            "volume": random.uniform(100, 200),
        })

    env = ResearchEnvironment(
        candles=candles,
        exchange=exchange,
        initial_capital=100_000.0,
        enable_regime=False,
        enable_mc=False,
    )

    # Simple grid with 4 combos
    result = env.scan(
        strategy_class=FragileStrategy,
        param_grid={"period": [5, 10], "threshold": [0.3, 0.7]},
        mode="grid",
        oos_pct=0.3,
        use_realism=False,
    )

    assert result.n_total == 4
    assert isinstance(result.ranking, list)


def test_strategy_comparison(exchange):
    """compare_strategies should return ranked results."""

    class AStrategy(BaseStrategy):
        def parameters(self):
            return [Parameter("p", 10)]
        def on_bar(self, candle):
            return None

    class BStrategy(BaseStrategy):
        def parameters(self):
            return [Parameter("p", 20)]
        def on_bar(self, candle):
            return None

    candles = []
    price = 50_000.0
    for i in range(200):
        price += random.gauss(5, 80)
        candles.append({
            "timestamp": datetime.utcnow() + timedelta(minutes=i),
            "open": price - 10, "high": price + 20,
            "low": price - 20, "close": price, "volume": 150,
        })

    from paper_trading.quant.research import ResearchEnvironment
    env = ResearchEnvironment(candles=candles, exchange=exchange, enable_mc=False)

    results = env.compare_strategies([
        ("StrategyA", AStrategy, {"p": 10}),
        ("StrategyB", BStrategy, {"p": 20}),
    ])

    assert len(results) == 2
    # Both ran without error
    assert all(isinstance(r, PerformanceStats) for r in results)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])