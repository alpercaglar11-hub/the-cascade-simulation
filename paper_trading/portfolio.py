"""
Paper Trading Portfolio Tracker.

Tracks realized PnL, unrealized PnL, win rate, profit factor,
max drawdown, Sharpe ratio, and expectancy from closed trade history.

All metrics are computed from the trade log — no black-box state.
Persist equity curve snapshots to Redis for Sharpe/MDD computation.
"""

import asyncio
import numpy as np
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass
from collections import deque
import json

from paper_trading._logger import get_logger

log = get_logger(__name__)

# ── Redis key prefixes ───────────────────────────────────────────────────────────
_EQUITY_CURVE_KEY = "paper:equity_curve"
_TRADE_STATS_KEY = "paper:trade_stats"


@dataclass
class TradeStats:
    """Snapshot of cumulative trade-level statistics."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    gross_profit: float = 0.0  # sum of all positive PnLs
    gross_loss: float = 0.0  # sum of all negative PnLs (positive number)
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0

    # Computed properties
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float("inf") if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss

    @property
    def expectancy(self) -> float:
        wr = self.win_rate
        lr = 1.0 - wr
        avg_w = self.avg_win if self.winning_trades > 0 else 0.0
        avg_l = self.avg_loss if self.losing_trades > 0 else 0.0
        return (wr * avg_w) - (lr * avg_l)

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate, 4),
            "gross_profit": round(self.gross_profit, 4),
            "gross_loss": round(self.gross_loss, 4),
            "profit_factor": (
                round(self.profit_factor, 4)
                if self.profit_factor != float("inf")
                else "inf"
            ),
            "largest_win": round(self.largest_win, 4),
            "largest_loss": round(self.largest_loss, 4),
            "avg_win": round(self.avg_win, 4),
            "avg_loss": round(self.avg_loss, 4),
            "expectancy": round(self.expectancy, 4),
        }


@dataclass
class EquitySnapshot:
    timestamp: datetime
    equity: float  # total account equity (realized + unrealized)
    realized_pnl: float  # realized PnL only
    unrealized_pnl: float  # unrealized PnL only
    trade_id: Optional[int] = None  # which trade caused this snapshot (None = periodic)


@dataclass
class PortfolioMetrics:
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    open_positions_count: int
    stats: TradeStats
    max_drawdown: float
    sharpe_ratio: float
    equity_curve_length: int
    current_equity: float


class PortfolioTracker:
    """
    Tracks paper trading performance from the trade log.
    Persists equity curve to Redis for Sharpe/MDD computation.

    Usage:
        tracker = PortfolioTracker(redis_client, initial_capital=10_000.0)
        await tracker.record_trade_closed(pnl=125.50, trade_id=1)
        await tracker.update_unrealized(unrealized_pnl=-30.0)
        metrics = await tracker.get_metrics()
    """

    # Equity curve: keep last 10,000 snapshots (reset if Redis unavailable)
    MAX_EQUITY_CURVE = 10_000
    # Risk-free rate for Sharpe (USDT lending rate proxy, approx 5% APY → per-minute)
    RISK_FREE_RATE_ANNUAL = 0.05

    def __init__(self, redis_client, initial_capital: float = 10_000.0):
        self._redis = redis_client
        self._initial_capital = initial_capital
        self._lock = asyncio.Lock()
        # In-memory cache
        self._equity_curve: deque[EquitySnapshot] = deque(maxlen=self.MAX_EQUITY_CURVE)
        self._stats = TradeStats()
        self._current_equity = initial_capital
        self._realized_pnl = 0.0
        self._unrealized_pnl = 0.0

    async def initialize(self) -> None:
        """Load equity curve and stats from Redis on startup."""
        if not self._redis:
            log.warning("portfolio_tracker_redis_unavailable_starting_fresh")
            return

        try:
            # Load equity curve
            raw = await self._redis.lrange(_EQUITY_CURVE_KEY, 0, -1)
            for item in raw:
                snap = json.loads(item)
                self._equity_curve.append(
                    EquitySnapshot(
                        timestamp=datetime.fromisoformat(snap["timestamp"]),
                        equity=snap["equity"],
                        realized_pnl=snap["realized_pnl"],
                        unrealized_pnl=snap["unrealized_pnl"],
                        trade_id=snap.get("trade_id"),
                    )
                )

            # Load trade stats
            stats_raw = await self._redis.hgetall(_TRADE_STATS_KEY)
            if stats_raw:
                self._stats = TradeStats(
                    total_trades=int(stats_raw.get(b"total_trades", 0) or 0),
                    winning_trades=int(stats_raw.get(b"winning_trades", 0) or 0),
                    losing_trades=int(stats_raw.get(b"losing_trades", 0) or 0),
                    gross_profit=float(stats_raw.get(b"gross_profit", 0) or 0),
                    gross_loss=float(stats_raw.get(b"gross_loss", 0) or 0),
                    largest_win=float(stats_raw.get(b"largest_win", 0) or 0),
                    largest_loss=float(stats_raw.get(b"largest_loss", 0) or 0),
                    avg_win=float(stats_raw.get(b"avg_win", 0) or 0),
                    avg_loss=float(stats_raw.get(b"avg_loss", 0) or 0),
                )
                self._current_equity = (
                    self._initial_capital
                    + self._stats.gross_profit
                    - self._stats.gross_loss
                )

            log.info(
                "portfolio_tracker_initialized",
                initial_capital=self._initial_capital,
                equity_curve_length=len(self._equity_curve),
                total_trades=self._stats.total_trades,
            )
        except Exception as e:
            log.error("portfolio_tracker_init_error", error=str(e))

    # ── Recording ─────────────────────────────────────────────────────────────

    async def record_trade_closed(
        self, pnl: float, trade_id: int, timestamp: Optional[datetime] = None
    ) -> None:
        """
        Record a closed trade. Updates realized PnL, stats, and equity curve.
        Called when a position is fully closed.
        """
        async with self._lock:
            now = timestamp or datetime.now(timezone.utc)

            # Update realized PnL
            self._realized_pnl += pnl
            self._current_equity = (
                self._initial_capital + self._realized_pnl + self._unrealized_pnl
            )

            # Update trade stats
            self._stats.total_trades += 1
            if pnl > 0:
                self._stats.winning_trades += 1
                self._stats.gross_profit += pnl
                if pnl > self._stats.largest_win:
                    self._stats.largest_win = pnl
                # Rolling average
                n = self._stats.winning_trades
                self._stats.avg_win = ((n - 1) * self._stats.avg_win + pnl) / n
            else:
                self._stats.losing_trades += 1
                self._stats.gross_loss += abs(pnl)
                if abs(pnl) > self._stats.largest_loss:
                    self._stats.largest_loss = abs(pnl)
                n = self._stats.losing_trades
                self._stats.avg_loss = ((n - 1) * self._stats.avg_loss + abs(pnl)) / n

            # Append equity snapshot
            snapshot = EquitySnapshot(
                timestamp=now,
                equity=self._current_equity,
                realized_pnl=self._realized_pnl,
                unrealized_pnl=self._unrealized_pnl,
                trade_id=trade_id,
            )
            self._equity_curve.append(snapshot)

            # Persist to Redis
            await self._persist_snapshot(snapshot)
            await self._persist_stats()

            log.info(
                "paper_trade_closed",
                trade_id=trade_id,
                pnl=round(pnl, 4),
                total_realized_pnl=round(self._realized_pnl, 4),
                equity=round(self._current_equity, 4),
                win_rate=round(self._stats.win_rate, 4),
            )

    async def update_unrealized(self, unrealized_pnl: float) -> None:
        """
        Update the current unrealized PnL (called on every price tick for open positions).
        Updates equity curve snapshot but does NOT record a new equity point
        (only closed trades create equity curve entries for Sharpe computation).
        """
        async with self._lock:
            self._unrealized_pnl = unrealized_pnl
            self._current_equity = (
                self._initial_capital + self._realized_pnl + self._unrealized_pnl
            )

    async def reset(self, initial_capital: float = 10_000.0) -> None:
        """
        Reset all paper trading state. Use between backtest runs.
        """
        async with self._lock:
            self._initial_capital = initial_capital
            self._realized_pnl = 0.0
            self._unrealized_pnl = 0.0
            self._current_equity = initial_capital
            self._equity_curve.clear()
            self._stats = TradeStats()

            if self._redis:
                try:
                    await self._redis.delete(_EQUITY_CURVE_KEY)
                    await self._redis.delete(_TRADE_STATS_KEY)
                except Exception as e:
                    log.error("portfolio_reset_redis_error", error=str(e))

            log.warning("portfolio_reset", initial_capital=initial_capital)

    # ── Metrics computation ────────────────────────────────────────────────────

    async def get_metrics(self, open_positions_count: int = 0) -> PortfolioMetrics:
        """Return full performance metrics snapshot."""
        mdd = await self._compute_max_drawdown()
        sharpe = await self._compute_sharpe_ratio()

        return PortfolioMetrics(
            realized_pnl=round(self._realized_pnl, 4),
            unrealized_pnl=round(self._unrealized_pnl, 4),
            total_pnl=round(self._realized_pnl + self._unrealized_pnl, 4),
            open_positions_count=open_positions_count,
            stats=self._stats,
            max_drawdown=round(mdd, 4),
            sharpe_ratio=round(sharpe, 4),
            equity_curve_length=len(self._equity_curve),
            current_equity=round(self._current_equity, 4),
        )

    async def _compute_max_drawdown(self) -> float:
        """
        Compute max drawdown from equity curve.
        Max drawdown = max(peak - trough) / peak, expressed as a positive percentage.
        """
        if len(self._equity_curve) < 2:
            return 0.0

        try:
            equities = np.array(
                [s.equity for s in self._equity_curve], dtype=np.float64
            )
            running_max = np.maximum.accumulate(equities)
            drawdowns = (running_max - equities) / running_max
            return float(np.max(drawdowns) * 100)
        except Exception as e:
            log.error("mdd_computation_error", error=str(e))
            return 0.0

    async def _compute_sharpe_ratio(self) -> float:
        """
        Compute annualized Sharpe ratio from equity curve returns.

        Sharpe = (mean_return - risk_free_rate) / std_return * sqrt(annualization_factor)

        For minute-level data:
        - annualization_factor = 525600 (minutes per year)
        - risk_free_rate = self.RISK_FREE_RATE_ANNUAL / 525600 (per minute)

        Uses simple returns: (equity[i] - equity[i-1]) / equity[i-1]
        """
        if len(self._equity_curve) < 3:
            return 0.0

        try:
            equities = np.array(
                [s.equity for s in self._equity_curve], dtype=np.float64
            )
            # Filter out zero-equity points (shouldn't happen but protect against div by zero)
            mask = equities[:-1] > 0
            if not np.any(mask):
                return 0.0

            returns = np.diff(equities) / equities[:-1]
            returns = returns[mask]

            if len(returns) < 2 or np.std(returns) == 0:
                return 0.0

            # Per-minute rates
            risk_free_per_minute = self.RISK_FREE_RATE_ANNUAL / 525600
            excess_returns = returns - risk_free_per_minute

            mean_ret = np.mean(excess_returns)
            std_ret = np.std(excess_returns, ddof=1)

            if std_ret == 0:
                return 0.0

            # Annualize
            sharpe = (mean_ret / std_ret) * np.sqrt(525600)
            return float(sharpe)

        except Exception as e:
            log.error("sharpe_computation_error", error=str(e))
            return 0.0

    # ── Persistence ────────────────────────────────────────────────────────────

    async def _persist_snapshot(self, snapshot: EquitySnapshot) -> None:
        if not self._redis:
            return
        try:
            data = json.dumps(
                {
                    "timestamp": snapshot.timestamp.isoformat(),
                    "equity": snapshot.equity,
                    "realized_pnl": snapshot.realized_pnl,
                    "unrealized_pnl": snapshot.unrealized_pnl,
                    "trade_id": snapshot.trade_id,
                },
                default=str,
            )
            await self._redis.lpush(_EQUITY_CURVE_KEY, data)
            await self._redis.ltrim(_EQUITY_CURVE_KEY, 0, self.MAX_EQUITY_CURVE - 1)
        except Exception as e:
            log.warning("snapshot_persist_error", error=str(e))

    async def _persist_stats(self) -> None:
        if not self._redis:
            return
        try:
            await self._redis.hset(
                _TRADE_STATS_KEY,
                mapping={
                    "total_trades": str(self._stats.total_trades),
                    "winning_trades": str(self._stats.winning_trades),
                    "losing_trades": str(self._stats.losing_trades),
                    "gross_profit": str(self._stats.gross_profit),
                    "gross_loss": str(self._stats.gross_loss),
                    "largest_win": str(self._stats.largest_win),
                    "largest_loss": str(self._stats.largest_loss),
                    "avg_win": str(self._stats.avg_win),
                    "avg_loss": str(self._stats.avg_loss),
                },
            )
        except Exception as e:
            log.warning("stats_persist_error", error=str(e))

    # ── Equity curve export ────────────────────────────────────────────────────

    async def get_equity_curve(self, limit: int = 1000) -> list[dict]:
        """Return equity curve for charting. Most recent first."""
        curve = list(self._equity_curve)[-limit:]
        return [
            {
                "timestamp": s.timestamp.isoformat(),
                "equity": round(s.equity, 4),
                "realized_pnl": round(s.realized_pnl, 4),
                "unrealized_pnl": round(s.unrealized_pnl, 4),
            }
            for s in reversed(curve)
        ]
