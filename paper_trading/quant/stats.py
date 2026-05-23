"""
Statistical validation module for strategy research.

Provides:
- Performance metrics: Sharpe, Sortino, Calmar, max drawdown, win rate, expectancy, profit factor
- Monte Carlo resampling
- Alpha decay analysis
- Bootstrap confidence intervals
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

# ── Trade Record ───────────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """A single closed trade for statistical analysis."""

    id: str
    entry_time: datetime
    exit_time: datetime
    side: str  # "long" or "short"
    entry_price: float
    exit_price: float
    size: float
    pnl: float  # net pnl after fees
    pnl_pct: float  # return as decimal
    duration_ticks: int  # number of bars held
    regime: str = "UNKNOWN"


# ── Equity Curve ───────────────────────────────────────────────────────────────


@dataclass
class EquityCurve:
    """
    Time series of portfolio values + drawdown for a backtest run.
    Used by ResearchEnvironment to compute all statistics.
    """

    timestamps: list[datetime] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    drawdown_pct: list[float] = field(default_factory=list)
    returns: list[float] = field(default_factory=list)  # period returns

    @classmethod
    def from_trades(
        cls,
        trades: list[TradeRecord],
        initial_capital: float,
        timestamps: list[datetime],
        prices: list[float],
        fee_bps: float = 10.0,
    ) -> "EquityCurve":
        """
        Reconstruct equity curve from a list of closed trades.
        Assumes 1 trade open at a time (no compounding positions yet).
        """
        timestamps_out = [timestamps[0]] if timestamps else [datetime.utcnow()]
        equity = [initial_capital]
        peak = initial_capital
        dd_pct = [0.0]
        rets = [0.0]

        closed_value = initial_capital
        last_bar = 0

        for t in trades:
            # Find bar index for exit time (for timestamp alignment)
            # Simple: advance to next bar after trade closes
            bars_held = t.duration_ticks
            last_bar += bars_held

            closed_value += t.pnl

            if last_bar < len(timestamps):
                ts = timestamps[min(last_bar, len(timestamps) - 1)]
            else:
                ts = timestamps[-1] if timestamps else t.exit_time

            timestamps_out.append(ts)
            equity.append(closed_value)

            peak = max(peak, closed_value)
            drawdown = (peak - closed_value) / peak if peak > 0 else 0.0
            dd_pct.append(drawdown)

            ret = t.pnl / equity[-2] if equity[-2] > 0 else 0.0
            rets.append(ret)

        return cls(
            timestamps=timestamps_out,
            equity=equity,
            drawdown_pct=dd_pct,
            returns=rets,
        )

    def to_array(self, field: str) -> np.ndarray:
        return np.array(getattr(self, field))


# ── Performance Statistics ─────────────────────────────────────────────────────


@dataclass
class PerformanceStats:
    """All computed statistics for a strategy run."""

    run_id: str = ""
    strategy_name: str = ""
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    max_drawdown: float = 0.0  # absolute
    max_drawdown_pct: float = 0.0  # as decimal
    max_drawdown_duration_bars: int = 0
    annualized_return: float = 0.0
    annualized_volatility: float = 0.0
    equity_final: float = 0.0
    total_pnl: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    avg_trade_pnl: float = 0.0
    avg_trade_duration_bars: float = 0.0
    alpha_decay_slope: float = 0.0  # daily alpha decay rate
    regime_stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


class StatsCalculator:
    """Computes performance statistics from equity curve + trade list."""

    def __init__(
        self, annualization_factor: int = 365 * 24
    ):  # ticks per year (hourly data)
        self._annualization = annualization_factor

    def compute(
        self,
        equity_curve: EquityCurve,
        trades: list[TradeRecord],
        strategy_name: str = "",
        run_id: str = "",
    ) -> PerformanceStats:
        eq = np.array(equity_curve.equity)
        rets = np.array(equity_curve.returns)
        dd_pct = np.array(equity_curve.drawdown_pct)

        n = len(trades)
        if n == 0:
            return PerformanceStats(run_id=run_id, strategy_name=strategy_name)

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]

        total_pnl = sum(t.pnl for t in trades)
        equity_final = eq[-1] if len(eq) > 0 else 0.0

        # ── Basic metrics ────────────────────────────────────────────────────────
        win_rate = len(wins) / n
        avg_win = np.mean([t.pnl for t in wins]) if wins else 0.0
        avg_loss = np.mean([t.pnl for t in losses]) if losses else 0.0
        expectancy = (
            (win_rate * avg_win) - ((1 - win_rate) * abs(avg_loss)) if n > 0 else 0.0
        )
        total_wins = sum(t.pnl for t in wins) if wins else 0.0
        total_losses = abs(sum(t.pnl for t in losses)) if losses else 0.0
        profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")

        # ── Return-based metrics ────────────────────────────────────────────────
        # Filter zero returns to avoid spurious Sharpe
        valid_rets = rets[rets != 0]
        if len(valid_rets) < 2:
            sharpe = sortino = 0.0
        else:
            mean_ret = np.mean(valid_rets)
            std_ret = np.std(valid_rets, ddof=1)
            neg_rets = valid_rets[valid_rets < 0]
            downside_std = np.std(neg_rets, ddof=1) if len(neg_rets) > 1 else std_ret

            sharpe = (
                (mean_ret / std_ret) * math.sqrt(self._annualization)
                if std_ret > 0
                else 0.0
            )
            sortino = (
                (mean_ret / downside_std) * math.sqrt(self._annualization)
                if downside_std > 0
                else 0.0
            )

        # ── Drawdown ─────────────────────────────────────────────────────────────
        max_dd = np.max(dd_pct)
        max_dd_dollars = max_dd * eq[0] if len(eq) > 0 else 0.0
        # Duration: count bars from peak to recovery
        dd_durations = self._drawdown_durations(eq, dd_pct)
        max_dd_duration = int(max(dd_durations)) if dd_durations else 0

        # ── Annualized ───────────────────────────────────────────────────────────
        total_return = (equity_final - eq[0]) / eq[0] if eq[0] > 0 else 0.0
        n_periods = max(len(valid_rets), 1)
        years = n_periods / self._annualization if self._annualization > 0 else 1
        # Clamp total_return to prevent overflow in CAGR calculation
        total_return_clamped = max(total_return, -0.9999)
        with np.errstate(over="ignore"):
            ann_return = (
                (1 + total_return_clamped) ** (1 / years) - 1 if years > 0 else 0.0
            )
        ann_vol = std_ret * math.sqrt(self._annualization) if std_ret > 0 else 0.0
        calmar = ann_return / max_dd if max_dd > 1e-10 else 0.0

        # ── Alpha decay ─────────────────────────────────────────────────────────
        alpha_decay_slope = self._compute_alpha_decay(valid_rets)

        # ── Per-regime stats ─────────────────────────────────────────────────────
        regime_stats = self._compute_regime_stats(trades)

        return PerformanceStats(
            run_id=run_id,
            strategy_name=strategy_name,
            n_trades=n,
            n_wins=len(wins),
            n_losses=len(losses),
            win_rate=round(win_rate, 4),
            avg_win=round(avg_win, 4),
            avg_loss=round(avg_loss, 4),
            expectancy=round(expectancy, 4),
            profit_factor=(
                round(profit_factor, 4) if profit_factor != float("inf") else 999.0
            ),
            sharpe_ratio=round(sharpe, 3),
            sortino_ratio=round(sortino, 3),
            calmar_ratio=round(calmar, 3),
            max_drawdown=round(max_dd_dollars, 2),
            max_drawdown_pct=round(max_dd, 4),
            max_drawdown_duration_bars=max_dd_duration,
            annualized_return=round(ann_return, 4),
            annualized_volatility=round(ann_vol, 4),
            equity_final=round(equity_final, 2),
            total_pnl=round(total_pnl, 2),
            best_trade=round(max(t.pnl for t in trades), 4) if trades else 0.0,
            worst_trade=round(min(t.pnl for t in trades), 4) if trades else 0.0,
            avg_trade_pnl=round(total_pnl / n, 4) if n > 0 else 0.0,
            avg_trade_duration_bars=(
                round(np.mean([t.duration_ticks for t in trades]), 2) if trades else 0.0
            ),
            alpha_decay_slope=round(alpha_decay_slope, 6),
            regime_stats=regime_stats,
        )

    @staticmethod
    def _drawdown_durations(equity: np.ndarray, dd_pct: np.ndarray) -> list[int]:
        """Compute drawdown duration in bars for each drawdown episode."""
        durations = []
        in_dd = False
        dur = 0
        for d in dd_pct:
            if d > 0 and not in_dd:
                in_dd = True
                dur = 1
            elif d > 0:
                dur += 1
            else:
                if in_dd:
                    durations.append(dur)
                    in_dd = False
                    dur = 0
        if in_dd:
            durations.append(dur)
        return durations

    @staticmethod
    def _compute_alpha_decay(returns: np.ndarray, lookback: int = 20) -> float:
        """
        Compute alpha decay: regress recent returns on older returns.
        A negative slope indicates alpha is decaying over time.
        Uses rolling window OLS.
        """
        if len(returns) < lookback * 2:
            return 0.0
        try:
            windows = len(returns) - lookback
            alphas = []
            for i in range(windows):
                old = returns[i : i + lookback]
                new = returns[i + lookback : i + lookback * 2]
                if len(old) < 2 or len(new) < 2:
                    continue
                old_mean = np.mean(old)
                cov = np.mean((old - old_mean) * (new - old_mean))
                var = np.var(old)
                slope = cov / var if var > 1e-10 else 0.0
                alphas.append(slope)
            return float(np.mean(alphas)) if alphas else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _compute_regime_stats(trades: list[TradeRecord]) -> dict:
        """Aggregate performance broken down by regime."""
        regime_map: dict[str, list[TradeRecord]] = {}
        for t in trades:
            regime_map.setdefault(t.regime, []).append(t)

        result = {}
        for regime, regime_trades in regime_map.items():
            wins = [t for t in regime_trades if t.pnl > 0]
            losses = [t for t in regime_trades if t.pnl <= 0]
            n = len(regime_trades)
            result[regime] = {
                "n_trades": n,
                "win_rate": round(len(wins) / n, 4) if n > 0 else 0,
                "total_pnl": round(sum(t.pnl for t in regime_trades), 2),
                "avg_pnl": (
                    round(sum(t.pnl for t in regime_trades) / n, 4) if n > 0 else 0
                ),
                "profit_factor": (
                    round(sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses)))
                    if losses
                    else float("inf")
                ),
            }
        return result


# ── Monte Carlo Resampling ─────────────────────────────────────────────────────


@dataclass
class MonteCarloResult:
    n_simulations: int
    sharpe_scores: list[float]
    max_drawdowns: list[float]
    total_returns: list[float]
    survival_rate: float  # fraction of sims with positive pnl
    var_95: float  # value at risk 95%
    cvar_95: float  # conditional VaR
    confidence_interval_95: tuple[float, float]


class MonteCarloEngine:
    """
    Resample trade returns to build a distribution of outcomes.
    Validates strategy robustness without assuming normal distribution.
    """

    def __init__(self, n_simulations: int = 1000, random_seed: int = 42):
        self._n = n_simulations
        self._rng = np.random.default_rng(random_seed)

    def run(
        self, trades: list[TradeRecord], initial_capital: float
    ) -> MonteCarloResult:
        if not trades:
            return MonteCarloResult(0, [], [], [], 0.0, 0.0, 0.0, (0.0, 0.0))

        pnls = np.array([t.pnl for t in trades])
        rets = pnls / initial_capital

        sharpe_scores = []
        max_dds = []
        total_returns = []

        for _ in range(self._n):
            # Bootstrap resample with replacement
            resampled = self._rng.choice(rets, size=len(rets), replace=True)
            resampled_pnls = resampled * initial_capital

            # Simulate equity curve
            equity = initial_capital + np.cumsum(resampled_pnls)
            peak = np.maximum.accumulate(np.maximum.accumulate(equity))
            drawdowns = (peak - equity) / peak
            max_dd = np.max(drawdowns)

            # Annualized Sharpe from resampled returns
            ret_series = resampled
            if len(ret_series) > 1 and np.std(ret_series) > 1e-10:
                sharpe = (
                    np.mean(ret_series) / np.std(ret_series, ddof=1) * math.sqrt(252)
                )
            else:
                sharpe = 0.0

            total_return = (equity[-1] - initial_capital) / initial_capital

            sharpe_scores.append(sharpe)
            max_dds.append(max_dd)
            total_returns.append(total_return)

        sharpe_scores = np.array(sharpe_scores)
        max_dds = np.array(max_dds)
        total_returns = np.array(total_returns)

        survival = float(np.mean(total_returns > 0))
        var_95 = float(np.percentile(total_returns, 5))
        cvar_95 = float(np.mean(total_returns[total_returns <= var_95]))
        ci_low = float(np.percentile(total_returns, 2.5))
        ci_high = float(np.percentile(total_returns, 97.5))

        return MonteCarloResult(
            n_simulations=self._n,
            sharpe_scores=sharpe_scores.tolist(),
            max_drawdowns=max_dds.tolist(),
            total_returns=total_returns.tolist(),
            survival_rate=round(survival, 4),
            var_95=round(var_95, 4),
            cvar_95=round(cvar_95, 4),
            confidence_interval_95=(round(ci_low, 4), round(ci_high, 4)),
        )
