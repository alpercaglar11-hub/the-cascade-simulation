"""
Quantitative Research Environment.

Provides:
- Historical replay with strategy execution
- Walk-forward testing
- Parameter sweeps (grid + random)
- Strategy ranking
- Integration with MarketRealismEngine for hostile market simulation
- Out-of-sample validation per parameter set
- Full stats + regime tracking + anti-overfitting

Usage:
    env = ResearchEnvironment(
        candles=candles,
        exchange=paper_exchange,
        initial_capital=100_000,
    )

    result = env.scan(
        strategy_class=RSIStrategy,
        param_grid={"period": [7, 14, 21], "oversold": [25, 30, 35]},
        mode="grid",
    )
    print(result.ranking[0])  # best parameter set
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Type

import numpy as np

from paper_trading.quant.interface import BaseStrategy, Signal, SignalType
from paper_trading.quant.stats import (
    EquityCurve,
    PerformanceStats,
    StatsCalculator,
    TradeRecord,
    MonteCarloEngine,
    MonteCarloResult,
)
from paper_trading.quant.regime import (
    RegimeDetector,
    RegimePerformanceTracker,
    RegimeType,
)
from paper_trading.quant.anti_overfit import AntiOverfitEngine, StabilityReport
from paper_trading.quant.portfolio import PortfolioBuilder, PortfolioResult

# ── Parameter Sweep ─────────────────────────────────────────────────────────────


@dataclass
class ParameterSet:
    """A single parameter combination within a sweep."""

    params: dict
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    stats: Optional[PerformanceStats] = None
    equity_curve: Optional[EquityCurve] = None
    mc_result: Optional[MonteCarloResult] = None
    stability: Optional[StabilityReport] = None
    is_oos: bool = False


@dataclass
class SweepResult:
    """Results from a full parameter sweep."""

    strategy_name: str
    mode: str
    n_total: int
    n_rejected: int
    best: Optional[ParameterSet] = None
    ranking: list[ParameterSet] = field(default_factory=list)
    all_params: list[ParameterSet] = field(default_factory=list)


# ── Core Research Environment ───────────────────────────────────────────────────


class ResearchEnvironment:
    """
    Orchestrates backtesting, parameter sweeps, walk-forward, and validation.

    Pipeline per parameter set:
        1. Run in-sample backtest
        2. Run OOS validation (walk-forward or hold-out)
        3. Parameter sensitivity (perturbed params)
        4. Anti-overfit evaluation → accept / reject
        5. Monte Carlo resampling
        6. Rank by composite score
    """

    def __init__(
        self,
        candles: list[dict],
        exchange,
        initial_capital: float = 100_000.0,
        fee_bps: float = 10.0,
        enable_regime: bool = True,
        enable_mc: bool = True,
        mc_n_simulations: int = 500,
        annualization_factor: int = 525600,  # minutes in a year (for 1m data)
    ):
        self._candles = candles
        self._exchange = exchange
        self._initial_capital = initial_capital
        self._fee_bps = fee_bps
        self._enable_regime = enable_regime
        self._enable_mc = enable_mc
        self._mc_n = mc_n_simulations
        self._annualization = annualization_factor

        self._stats_calc = StatsCalculator(annualization_factor=annualization_factor)
        self._anti_overfit = AntiOverfitEngine()
        self._mc_engine = MonteCarloEngine(n_simulations=mc_n_simulations)
        self._portfolio_builder = PortfolioBuilder()

    # ── Public API ─────────────────────────────────────────────────────────────

    def scan(
        self,
        strategy_class: Type[BaseStrategy],
        param_grid: dict[str, list],
        mode: str = "grid",
        n_random: int = 100,
        oos_pct: float = 0.3,
        walk_forward: bool = False,
        n_wf_windows: int = 3,
        wf_step_pct: float = 0.2,
        use_realism: bool = False,
        symbol: str = "BTC/USDT",
    ) -> SweepResult:
        """
        Run a full parameter sweep with validation and ranking.
        """
        strategy_name = strategy_class.__name__
        param_combinations = self._build_param_combinations(param_grid, mode, n_random)
        total = len(param_combinations)

        all_results: list[ParameterSet] = []
        best: Optional[ParameterSet] = None

        for i, params in enumerate(param_combinations):
            ps = ParameterSet(params=params)

            train_end = int(len(self._candles) * (1 - oos_pct))

            # ── In-sample run ───────────────────────────────────────────────────
            is_stats, is_trades, is_eq = self._run_backtest(
                strategy_class,
                params,
                symbol,
                train_end=train_end,
                use_realism=use_realism,
            )
            ps.stats = is_stats
            ps.equity_curve = is_eq
            ps.is_oos = False

            if is_stats.n_trades == 0:
                all_results.append(ps)
                continue

            # ── OOS validation ─────────────────────────────────────────────────
            oos_stats_list: list[PerformanceStats] = []
            if walk_forward:
                oos_stats_list = self._walk_forward_oos(
                    strategy_class,
                    params,
                    symbol,
                    n_wf_windows,
                    wf_step_pct,
                    use_realism,
                )
            else:
                oos_stats, _, _ = self._run_backtest(
                    strategy_class,
                    params,
                    symbol,
                    train_end=train_end,
                    oos_only=True,
                    use_realism=use_realism,
                )
                if oos_stats.n_trades > 0:
                    oos_stats_list.append(oos_stats)

            # ── Parameter sensitivity ─────────────────────────────────────────────
            perturbed = self._run_perturbations(
                strategy_class, params, symbol, param_grid, use_realism
            )

            # ── Anti-overfit evaluation ─────────────────────────────────────────
            stability = self._anti_overfit.evaluate(is_stats, oos_stats_list, perturbed)
            ps.stability = stability

            if not stability.is_stable:
                ps.stats = None  # mark as rejected
            else:
                if self._enable_mc:
                    mc_result = self._mc_engine.run(is_trades, self._initial_capital)
                    ps.mc_result = mc_result

            all_results.append(ps)

            # Track best
            if ps.stats is not None:
                if best is None or ps.stats.sharpe_ratio > best.stats.sharpe_ratio:
                    best = ps

        # ── Ranking ───────────────────────────────────────────────────────────────
        ranked = [p for p in all_results if p.stats is not None]
        ranked.sort(
            key=lambda p: (
                p.stats.sharpe_ratio * 0.5
                + p.stats.sortino_ratio * 0.3
                - abs(p.stats.max_drawdown_pct) * 0.2
            ),
            reverse=True,
        )

        return SweepResult(
            strategy_name=strategy_name,
            mode=mode,
            n_total=total,
            n_rejected=sum(1 for p in all_results if p.stats is None),
            best=best,
            ranking=ranked,
            all_params=all_results,
        )

    def compare_strategies(
        self,
        strategies: list[tuple[str, Type[BaseStrategy], dict]],
        oos_pct: float = 0.3,
        use_realism: bool = False,
        symbol: str = "BTC/USDT",
    ) -> list[PerformanceStats]:
        """Compare multiple strategies on the same data."""
        results = []
        train_end = int(len(self._candles) * (1 - oos_pct))
        for name, cls, params in strategies:
            stats, _, _ = self._run_backtest(
                cls,
                params,
                symbol,
                train_end=train_end,
                use_realism=use_realism,
            )
            stats.strategy_name = name
            results.append(stats)

        results.sort(
            key=lambda s: (
                s.sharpe_ratio * 0.5
                + s.sortino_ratio * 0.3
                - abs(s.max_drawdown_pct) * 0.2
            ),
            reverse=True,
        )
        return results

    def build_portfolio(
        self,
        strategy_stats: list[PerformanceStats],
        portfolio_config: Optional[Any] = None,
    ) -> PortfolioResult:
        """Build multi-strategy portfolio from stats."""
        return self._portfolio_builder.allocate(strategy_stats)

    # ── Internal: Backtest ─────────────────────────────────────────────────────

    def _run_backtest(
        self,
        strategy_class: Type[BaseStrategy],
        params: dict,
        symbol: str,
        train_end: int,
        oos_only: bool = False,
        use_realism: bool = False,
    ) -> tuple[PerformanceStats, list[TradeRecord], EquityCurve]:
        """
        Run a single backtest. Uses asyncio.run() to handle async exchange.
        """
        return asyncio.run(
            self._run_backtest_async(
                strategy_class,
                params,
                symbol,
                train_end,
                oos_only,
                use_realism,
            )
        )

    async def _run_backtest_async(
        self,
        strategy_class: Type[BaseStrategy],
        params: dict,
        symbol: str,
        train_end: int,
        oos_only: bool,
        use_realism: bool,
    ) -> tuple[PerformanceStats, list[TradeRecord], EquityCurve]:
        strategy = strategy_class(params=params)
        strategy.symbol = symbol
        strategy.on_init()

        regime_detector = RegimeDetector() if self._enable_regime else None
        regime_tracker = RegimePerformanceTracker()

        position: dict = {}
        trades: list[TradeRecord] = []

        if oos_only:
            bars = self._candles[train_end:]
            start_bar = train_end
        else:
            bars = self._candles[:train_end]
            start_bar = 0

        for bar_idx, candle in enumerate(bars):
            global_idx = start_bar + bar_idx
            current_time = candle.get("timestamp", datetime.utcnow())
            price = float(candle["close"])

            # Update regime
            if regime_detector:
                regime_detector.update(candle)
                if regime_detector.ready:
                    regime = regime_detector.classify()
                    regime_tracker.set_regime(regime, global_idx)

            # Feed price through realism layer if needed
            if use_realism and hasattr(self._exchange, "on_price_update"):
                price = await self._exchange.on_price_update(symbol, price)
            await self._exchange._update_price(symbol, price)

            # Generate signal
            signal = strategy.on_bar(candle)

            # Execute actionable signals
            if signal and signal.is_actionable():
                await self._execute_signal_async(
                    signal,
                    position,
                    trades,
                    regime_tracker,
                    global_idx,
                    current_time,
                    symbol,
                )

            # Check SL/TP on open position
            if symbol in position:
                pos = position[symbol]
                sl = pos.get("stop_loss")
                tp = pos.get("take_profit")
                if sl and (
                    pos["side"] == "long"
                    and price <= sl
                    or pos["side"] == "short"
                    and price >= sl
                ):
                    await self._close_position_async(
                        symbol,
                        position,
                        trades,
                        regime_tracker,
                        global_idx,
                        current_time,
                        "sl",
                    )
                elif tp and (
                    pos["side"] == "long"
                    and price >= tp
                    or pos["side"] == "short"
                    and price <= tp
                ):
                    await self._close_position_async(
                        symbol,
                        position,
                        trades,
                        regime_tracker,
                        global_idx,
                        current_time,
                        "tp",
                    )

        # Close any open position at end of backtest
        if symbol in position:
            await self._close_position_async(
                symbol,
                position,
                trades,
                regime_tracker,
                global_idx,
                current_time,
                "eob",
            )

        strategy.on_reset()
        regime_tracker.reset()

        # Build equity curve
        timestamps = [c.get("timestamp", datetime.utcnow()) for c in bars]
        prices = [float(c["close"]) for c in bars]
        eq = EquityCurve.from_trades(
            trades, self._initial_capital, timestamps, prices, self._fee_bps
        )
        stats = self._stats_calc.compute(
            eq, trades, strategy_name=strategy_class.__name__
        )

        return stats, trades, eq

    async def _execute_signal_async(
        self,
        signal: Signal,
        position: dict,
        trades: list[TradeRecord],
        regime_tracker: RegimePerformanceTracker,
        bar: int,
        timestamp: datetime,
        symbol: str,
    ) -> None:
        """Process a signal and update position state (async)."""
        # Determine action from signal type
        if signal.type == SignalType.CLOSE_LONG:
            if symbol in position and position[symbol]["side"] == "long":
                await self._close_position_async(
                    symbol, position, trades, regime_tracker, bar, timestamp, "signal"
                )
            return
        if signal.type == SignalType.CLOSE_SHORT:
            if symbol in position and position[symbol]["side"] == "short":
                await self._close_position_async(
                    symbol, position, trades, regime_tracker, bar, timestamp, "signal"
                )
            return
        if signal.type == SignalType.HOLD:
            return

        action = "buy" if signal.type == SignalType.BUY else "sell"

        # Close opposing position first
        if symbol in position:
            pos_side = position[symbol]["side"]
            if (pos_side == "long" and action == "sell") or (
                pos_side == "short" and action == "buy"
            ):
                await self._close_position_async(
                    symbol, position, trades, regime_tracker, bar, timestamp, "reversal"
                )

        if symbol in position:
            return  # position still open

        # Open new position — size = 10% of capital
        size = (self._initial_capital * 0.1) / signal.price
        try:
            await self._exchange.place_market_order(symbol, action, size)
        except Exception:
            return

        position[symbol] = {
            "side": "long" if action == "buy" else "short",
            "size": size,
            "entry_price": signal.price,
            "entry_time": timestamp,
            "entry_bar": bar,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
        }

    async def _close_position_async(
        self,
        symbol: str,
        position: dict,
        trades: list[TradeRecord],
        regime_tracker: RegimePerformanceTracker,
        bar: int,
        timestamp: datetime,
        reason: str,
    ) -> None:
        pos = position.pop(symbol, None)
        if not pos:
            return

        try:
            if pos["side"] == "long":
                result = await self._exchange.place_market_order(
                    symbol, "sell", pos["size"]
                )
            else:
                result = await self._exchange.place_market_order(
                    symbol, "buy", pos["size"]
                )
            exit_price = result.get("fill_price", pos["entry_price"])
        except Exception:
            exit_price = pos["entry_price"]

        pnl = (exit_price - pos["entry_price"]) * pos["size"]
        if pos["side"] == "short":
            pnl = -pnl
        # Round-trip fees
        pnl -= abs(pnl) * (self._fee_bps / 10_000) * 2

        trade = TradeRecord(
            id=uuid.uuid4().hex[:12],
            entry_time=pos["entry_time"],
            exit_time=timestamp,
            side=pos["side"],
            entry_price=pos["entry_price"],
            exit_price=exit_price,
            size=pos["size"],
            pnl=pnl,
            pnl_pct=(
                pnl / (pos["entry_price"] * pos["size"])
                if pos["entry_price"] > 0
                else 0.0
            ),
            duration_ticks=bar - pos["entry_bar"],
        )
        regime_tracker.record_trade(trade)
        trades.append(trade)

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _build_param_combinations(
        self,
        param_grid: dict[str, list],
        mode: str,
        n_random: int,
    ) -> list[dict]:
        if mode == "grid":
            keys = list(param_grid.keys())
            combos = list(itertools.product(*[param_grid[k] for k in keys]))
            return [dict(zip(keys, c)) for c in combos]
        else:
            import random

            combos = []
            for _ in range(n_random):
                combos.append({k: random.choice(v) for k, v in param_grid.items()})
            return combos

    def _run_perturbations(
        self,
        strategy_class: Type[BaseStrategy],
        base_params: dict,
        symbol: str,
        param_grid: dict[str, list],
        use_realism: bool,
    ) -> list[PerformanceStats]:
        """Run backtests for ±1 step perturbations of each numeric parameter."""
        results = []
        schema = {p.name: p for p in strategy_class().parameters()}

        for pname, base_val in base_params.items():
            if not isinstance(base_val, (int, float)):
                continue
            step = param_grid.get(pname, [1])
            step_val = (
                float(step[0]) if step and isinstance(step[0], (int, float)) else 1.0
            )

            for delta in [-step_val, step_val]:
                perturbed = {**base_params, pname: base_val + delta}
                if pname in schema:
                    perturbed[pname] = schema[pname].validate(perturbed[pname])

                try:
                    stats, _, _ = self._run_backtest(
                        strategy_class,
                        perturbed,
                        symbol,
                        train_end=int(len(self._candles) * 0.7),
                        use_realism=use_realism,
                    )
                    results.append(stats)
                except Exception:
                    pass
        return results

    def _walk_forward_oos(
        self,
        strategy_class: Type[BaseStrategy],
        params: dict,
        symbol: str,
        n_windows: int,
        step_pct: float,
        use_realism: bool,
    ) -> list[PerformanceStats]:
        n = len(self._candles)
        window_size = int(n * step_pct)
        results = []

        for i in range(1, n_windows + 1):
            train_end = n - i * window_size
            if train_end < window_size:
                break
            try:
                stats, _, _ = self._run_backtest(
                    strategy_class,
                    params,
                    symbol,
                    train_end=train_end,
                    oos_only=True,
                    use_realism=use_realism,
                )
                if stats.n_trades > 0:
                    results.append(stats)
            except Exception:
                pass
        return results
