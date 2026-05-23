"""
Paper Trading Orchestrator.

Manages the paper trading environment end-to-end:
- PaperExchange instance lifecycle
- PortfolioTracker lifecycle
- Price feed into paper exchange
- Limit order trigger checking
- Unrealized PnL updates
- Switching between paper and live modes
- Backtest run management
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional, Literal, TYPE_CHECKING
from dataclasses import dataclass

from paper_trading._logger import get_logger

if TYPE_CHECKING:
    from paper_trading.engine import PaperExchange
    from paper_trading.portfolio import PortfolioTracker

log = get_logger(__name__)


@dataclass
class PaperTradingConfig:
    """Configuration for the paper trading environment."""
    initial_capital: float = 10_000.0
    maker_fee_bps: float = 5.0
    taker_fee_bps: float = 10.0
    base_latency_ms: float = 50.0
    avg_daily_volume: float = 1_000_000.0  # ADV in quote currency
    enable_downtime_simulation: bool = False
    downtime_probability_per_call: float = 0.0
    mean_downtime_seconds: float = 30.0
    latency_spike_probability: float = 0.0
    max_latency_spike_ms: float = 500.0


class PaperTradingOrchestrator:
    """
    Central manager for the paper trading environment.

    Usage:
        orchestrator = PaperTradingOrchestrator(redis_client)
        await orchestrator.start()
        await orchestrator.update_market_price("BTC/USDT", 67500.0, volatility=1.8)
        metrics = await orchestrator.get_portfolio_metrics()
        await orchestrator.reset()  # between backtest runs
    """

    def __init__(
        self,
        redis_client=None,
        config: Optional[PaperTradingConfig] = None,
    ):
        self._redis = redis_client
        self._config = config or PaperTradingConfig()
        self._paper_exchange: Optional[PaperExchange] = None
        self._portfolio: Optional[PortfolioTracker] = None
        self._limit_order_check_task: Optional[asyncio.Task] = None
        self._running = False
        self._mode: Literal["paper", "live"] = "paper"

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def paper_exchange(self) -> Optional[PaperExchange]:
        return self._paper_exchange

    @property
    def portfolio(self) -> Optional[PortfolioTracker]:
        return self._portfolio

    async def start(self) -> None:
        """Initialize and start the paper trading environment."""
        if self._running:
            return

        log.info("paper_trading_starting", config=self._config.__dict__)

        # ── Paper Exchange ──────────────────────────────────────────────────────
        from paper_trading.engine import DowntimeSimulator

        downtime_sim = None
        if self._config.enable_downtime_simulation:
            downtime_sim = DowntimeSimulator(
                downtime_probability_per_call=self._config.downtime_probability_per_call,
                mean_downtime_seconds=self._config.mean_downtime_seconds,
                latency_spike_probability=self._config.latency_spike_probability,
                max_latency_spike_ms=self._config.max_latency_spike_ms,
            )

        from paper_trading.engine import PaperExchange
        self._paper_exchange = PaperExchange(
            initial_capital=self._config.initial_capital,
            maker_fee_bps=self._config.maker_fee_bps,
            taker_fee_bps=self._config.taker_fee_bps,
            avg_daily_volume=self._config.avg_daily_volume,
            base_latency_ms=self._config.base_latency_ms,
            downtime_sim=downtime_sim,
        )
        await self._paper_exchange.connect()

        # ── Portfolio Tracker ──────────────────────────────────────────────────
        from paper_trading.portfolio import PortfolioTracker
        self._portfolio = PortfolioTracker(
            redis_client=self._redis,
            initial_capital=self._config.initial_capital,
        )
        await self._portfolio.initialize()

        # ── Limit Order Monitor ─────────────────────────────────────────────────
        self._running = True
        self._limit_order_check_task = asyncio.create_task(self._limit_order_monitor())
        log.info("paper_trading_started", initial_capital=self._config.initial_capital)

    async def stop(self) -> None:
        """Stop the paper trading environment."""
        self._running = False
        if self._limit_order_check_task:
            self._limit_order_check_task.cancel()
            try:
                await self._limit_order_check_task
            except asyncio.CancelledError:
                pass
        log.info("paper_trading_stopped")

    async def reset(self, initial_capital: Optional[float] = None) -> None:
        """
        Reset paper trading state.
        Use between backtest runs or to start a fresh simulation.
        """
        if initial_capital:
            self._config.initial_capital = initial_capital

        await self.stop()

        if self._paper_exchange:
            self._paper_exchange.reset()

        if self._portfolio:
            await self._portfolio.reset(initial_capital=self._config.initial_capital)

        self._running = False
        await self.start()
        log.warning("paper_trading_reset", initial_capital=self._config.initial_capital)

    async def update_market_price(
        self,
        symbol: str,
        price: float,
        volatility: Optional[float] = None,
        adv: Optional[float] = None,
    ) -> None:
        """
        Feed a new market price into the paper exchange.
        This updates the internal price state used for limit order triggering.

        Call this from the market data engine's WebSocket handler
        or after every new candle.

        Also updates slippage model parameters.
        """
        if not self._paper_exchange:
            return

        await self._paper_exchange._update_price(symbol, price)

        if volatility is not None:
            self._paper_exchange.set_volatility(volatility)
        if adv is not None:
            self._paper_exchange.set_adv(adv)

    async def _limit_order_monitor(self) -> None:
        """
        Background safety-net task: periodically verify open orders are evaluated.

        PRIMARY execution path is _update_price -> _check_and_trigger_orders.
        This monitor exists as a fallback in case:
          - _update_price is ever called without await somewhere
          - A price update slips through without triggering (edge case)

        Runs every 500ms. The interval is not latency-sensitive because
        the primary path handles all normal triggers immediately.
        """
        while self._running:
            await asyncio.sleep(0.5)
            if not self._paper_exchange or not self._running:
                continue

            try:
                open_orders = list(self._paper_exchange._open_limit_orders)
                symbols = set(o.symbol for o in open_orders)
                for symbol in symbols:
                    await self._paper_exchange._check_and_trigger_orders(symbol)
            except Exception as e:
                log.error("limit_order_monitor_error", error=str(e))

    async def get_portfolio_metrics(self, open_positions_count: int = 0) -> dict:
        """Return full performance metrics from the portfolio tracker."""
        if not self._portfolio:
            return {}
        metrics = await self._portfolio.get_metrics(open_positions_count)
        return {
            "realized_pnl": metrics.realized_pnl,
            "unrealized_pnl": metrics.unrealized_pnl,
            "total_pnl": metrics.total_pnl,
            "open_positions": metrics.open_positions_count,
            "current_equity": metrics.current_equity,
            "initial_capital": self._config.initial_capital,
            "roi_pct": round(
                (metrics.current_equity - self._config.initial_capital) / self._config.initial_capital * 100, 4
            ),
            "win_rate": metrics.stats.win_rate,
            "profit_factor": float(metrics.stats.profit_factor) if metrics.stats.profit_factor != float("inf") else "inf",
            "max_drawdown_pct": metrics.max_drawdown,
            "sharpe_ratio": metrics.sharpe_ratio,
            "expectancy": metrics.stats.expectancy,
            "total_trades": metrics.stats.total_trades,
            "winning_trades": metrics.stats.winning_trades,
            "losing_trades": metrics.stats.losing_trades,
            "avg_win": metrics.stats.avg_win,
            "avg_loss": metrics.stats.avg_loss,
            "largest_win": metrics.stats.largest_win,
            "largest_loss": metrics.stats.largest_loss,
            "equity_curve_length": metrics.equity_curve_length,
        }

    async def record_closed_trade(self, pnl: float, trade_id: int) -> None:
        """Called by execution engine when a paper trade closes."""
        if self._portfolio:
            await self._portfolio.record_trade_closed(pnl=pnl, trade_id=trade_id)

    async def update_unrealized_pnl(self, unrealized_pnl: float) -> None:
        """Called on every price tick to update unrealized PnL."""
        if self._portfolio:
            await self._portfolio.update_unrealized(unrealized_pnl)

    async def get_equity_curve(self, limit: int = 1000) -> list[dict]:
        """Return equity curve for charting."""
        if not self._portfolio:
            return []
        return await self._portfolio.get_equity_curve(limit=limit)

    async def get_balances(self) -> dict:
        """Return current paper exchange balances."""
        if not self._paper_exchange:
            return {}
        return self._paper_exchange.get_all_balances()


# ── Global singleton ─────────────────────────────────────────────────────────────
_paper_orchestrator: Optional[PaperTradingOrchestrator] = None


async def get_paper_orchestrator() -> PaperTradingOrchestrator:
    global _paper_orchestrator
    if _paper_orchestrator is None:
        from services.cache import get_redis
        redis = await get_redis()
        _paper_orchestrator = PaperTradingOrchestrator(redis_client=redis)
        await _paper_orchestrator.start()
    return _paper_orchestrator
