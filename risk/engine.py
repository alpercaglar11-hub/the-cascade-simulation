"""Risk Management Engine: enforces trading rules with Redis-backed persistent state."""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from logging.logger import get_logger
from db.models import RiskEvent, DailyStats, Trade, Position
from db.session import async_session_factory

log = get_logger(__name__)

# Redis key prefixes
_RISK_STATE_KEY = "risk:state"
_KILLSWITCH_KEY = "risk:killswitch"
_CONSECUTIVE_LOSSES_KEY = "risk:consecutive_losses"
_COOLDOWN_KEY = "risk:cooldown_until"


@dataclass
class RiskLimits:
    max_daily_loss_pct: float
    max_position_size_pct: float
    max_open_positions: int
    cooldown_minutes: int


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: Optional[str] = None
    severity: str = "none"


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    daily_trades: int = 0
    consecutive_losses: int = 0
    last_loss_at: Optional[datetime] = None
    last_trade_at: Optional[datetime] = None
    open_position_count: int = 0
    kill_switch: bool = False
    cooldown_until: Optional[datetime] = None
    total_equity: float = 0.0


class RiskEngine:
    """
    Every AI recommendation must pass through the RiskEngine before execution.

    State is persisted to Redis for crash recovery:
    - Kill switch state (survives process restart)
    - Consecutive loss count (survives process restart)
    - Cooldown expiry time (survives process restart)

    Critical decisions (kill switch, cooldown) always check Redis.
    Non-critical metrics are cached in-memory for performance.
    """

    def __init__(self, limits: Optional[RiskLimits] = None):
        self._limits = limits or RiskLimits(
            max_daily_loss_pct=settings.max_daily_loss_pct,
            max_position_size_pct=settings.max_position_size_pct,
            max_open_positions=settings.max_open_positions,
            cooldown_minutes=settings.cooldown_minutes_after_loss,
        )
        self._redis_client = None
        self._sync_lock = asyncio.Lock()
        # In-memory cache for non-critical display metrics
        self._state = RiskState()
        self._killswitch_active = settings.emergency_kill_switch

    async def _set_redis(self, redis_client) -> None:
        """Inject Redis client. Called once at startup."""
        self._redis_client = redis_client
        await self._load_persistent_state()

    async def _load_persistent_state(self) -> None:
        """Load kill switch, consecutive losses, and cooldown from Redis on startup."""
        if not self._redis_client:
            return
        try:
            # Load kill switch
            killswitch_raw = await self._redis_client.get(_KILLSWITCH_KEY)
            if killswitch_raw is not None:
                self._killswitch_active = (
                    killswitch_raw == b"1" or killswitch_raw == "1"
                )
                log.info("risk_killswitch_loaded", active=self._killswitch_active)

            # Load consecutive losses
            losses_raw = await self._redis_client.get(_CONSECUTIVE_LOSSES_KEY)
            if losses_raw:
                self._state.consecutive_losses = int(losses_raw)

            # Load cooldown
            cooldown_raw = await self._redis_client.get(_COOLDOWN_KEY)
            if cooldown_raw:
                cooldown_str = (
                    cooldown_raw.decode()
                    if isinstance(cooldown_raw, bytes)
                    else cooldown_raw
                )
                self._state.cooldown_until = datetime.fromisoformat(cooldown_str)
                log.info(
                    "risk_cooldown_loaded", until=self._state.cooldown_until.isoformat()
                )

        except Exception as e:
            log.error("risk_persistent_state_load_error", error=str(e))

    async def _persist_killswitch(self, active: bool) -> None:
        if not self._redis_client:
            return
        try:
            await self._redis_client.set(
                _KILLSWITCH_KEY, "1" if active else "0", ex=None
            )
        except Exception as e:
            log.error("risk_killswitch_persist_error", error=str(e))

    async def _persist_consecutive_losses(self) -> None:
        if not self._redis_client:
            return
        try:
            await self._redis_client.set(
                _CONSECUTIVE_LOSSES_KEY, str(self._state.consecutive_losses), ex=None
            )
        except Exception as e:
            log.error("risk_consecutive_losses_persist_error", error=str(e))

    async def _persist_cooldown(self) -> None:
        if not self._redis_client:
            return
        try:
            if self._state.cooldown_until:
                await self._redis_client.set(
                    _COOLDOWN_KEY, self._state.cooldown_until.isoformat(), ex=None
                )
            else:
                await self._redis_client.delete(_COOLDOWN_KEY)
        except Exception as e:
            log.error("risk_cooldown_persist_error", error=str(e))

    async def check(
        self, action: str, symbol: str, price: float, size: float
    ) -> RiskCheckResult:
        """
        Full risk check for a proposed trade.
        Always checks Redis-backed kill switch and cooldown (source of truth).
        """
        # Refresh state from DB — uses lock to prevent concurrent DB queries
        await self._sync_state()

        # 1. Kill switch — checked against Redis-backed value
        killswitch_val = await self._get_killswitch()
        if killswitch_val or self._killswitch_active:
            await self._log_event("killswitch", symbol, "Kill switch is active")
            return RiskCheckResult(
                allowed=False, reason="Kill switch is active", severity="critical"
            )

        # 2. Cooldown — always check Redis for expiry
        cooldown_until = await self._get_cooldown()
        if cooldown_until and datetime.now(timezone.utc) < cooldown_until:
            wait_minutes = (cooldown_until - datetime.now(timezone.utc)).seconds // 60
            return RiskCheckResult(
                allowed=False,
                reason=f"In cooldown — {wait_minutes} minutes remaining",
                severity="warning",
            )

        # 3. Daily loss limit
        daily_loss_limit = self._state.total_equity * (
            self._limits.max_daily_loss_pct / 100
        )
        if self._state.daily_pnl < -daily_loss_limit:
            await self._activate_cooldown("Daily loss limit exceeded")
            await self._log_event(
                "daily_loss_limit",
                symbol,
                f"Daily PnL: {self._state.daily_pnl}, limit: -{daily_loss_limit:.2f}",
            )
            return RiskCheckResult(
                allowed=False,
                reason=f"Daily loss limit reached: {self._state.daily_pnl:.2f}",
                severity="critical",
            )

        # 4. Position size limit
        position_value = size * price
        if (
            position_value
            > self._limits.max_position_size_pct / 100 * self._state.total_equity
        ):
            max_allowed_value = (
                self._limits.max_position_size_pct / 100 * self._state.total_equity
            )
            await self._log_event(
                "position_size_limit",
                symbol,
                f"Requested: {position_value:.2f}, max: {max_allowed_value:.2f}",
            )
            return RiskCheckResult(
                allowed=False,
                reason=f"Position size ${position_value:.2f} exceeds max ${max_allowed_value:.2f}",
                severity="warning",
            )

        # 5. Open positions limit
        if (
            action == "BUY"
            and self._state.open_position_count >= self._limits.max_open_positions
        ):
            return RiskCheckResult(
                allowed=False,
                reason=f"Max open positions reached ({self._limits.max_open_positions})",
                severity="warning",
            )

        # 6. Loss streak
        if self._state.consecutive_losses >= 3:
            await self._log_event(
                "loss_streak",
                symbol,
                f"Consecutive losses: {self._state.consecutive_losses}",
            )
            return RiskCheckResult(
                allowed=False,
                reason=f"Loss streak of {self._state.consecutive_losses} — cooling down",
                severity="warning",
            )

        return RiskCheckResult(allowed=True)

    async def record_trade(self, pnl: float, symbol: str) -> None:
        """Record a completed trade result. Updates persistent state."""
        now = datetime.now(timezone.utc)
        self._state.daily_trades += 1
        self._state.last_trade_at = now

        if pnl < 0:
            self._state.consecutive_losses += 1
            self._state.last_loss_at = now
            await self._persist_consecutive_losses()
            if self._state.consecutive_losses >= 3:
                await self._activate_cooldown(
                    f"3 consecutive losses, last PnL: {pnl:.2f}"
                )
        else:
            self._state.consecutive_losses = 0
            await self._persist_consecutive_losses()

        self._state.daily_pnl += pnl
        await self._persist_daily_stats(pnl)

    async def _get_killswitch(self) -> bool:
        """Always check Redis for kill switch state."""
        if not self._redis_client:
            return self._killswitch_active
        try:
            val = await self._redis_client.get(_KILLSWITCH_KEY)
            if val is not None:
                return val in (b"1", "1", 1, True)
        except Exception:
            pass
        return self._killswitch_active

    async def _get_cooldown(self) -> Optional[datetime]:
        """Check Redis for cooldown expiry."""
        if not self._redis_client:
            return self._state.cooldown_until
        try:
            raw = await self._redis_client.get(_COOLDOWN_KEY)
            if raw:
                s = raw.decode() if isinstance(raw, bytes) else raw
                return datetime.fromisoformat(s)
        except Exception:
            pass
        return self._state.cooldown_until

    async def _sync_state(self) -> None:
        """Load current state from database. Uses lock to prevent concurrent queries."""
        async with self._sync_lock:
            today = datetime.now(timezone.utc).date()
            try:
                async with async_session_factory() as session:
                    from sqlalchemy import select, func
                    from db.models import DailyStats as DS, Position as Pos

                    # Single query for today's stats
                    stats_result = await session.execute(
                        select(DS).where(DS.date == today)
                    )
                    stats = stats_result.scalar_one_or_none()
                    if stats:
                        self._state.daily_pnl = float(stats.total_pnl or 0)
                        self._state.daily_trades = int(stats.trade_count or 0)

                    # Count open positions + sum equity in one pass
                    pos_result = await session.execute(
                        select(
                            func.count(Pos.id),
                            func.sum(
                                Pos.quantity * (Pos.current_price or Pos.entry_price)
                            ),
                        ).where(Pos.status == "OPEN")
                    )
                    row = pos_result.one()
                    self._state.open_position_count = int(row[0] or 0)
                    self._state.total_equity = float(row[1] or 1000.0) or 1000.0

            except Exception as e:
                log.error("risk_state_sync_error", error=str(e))
                self._state.total_equity = max(self._state.total_equity, 1000.0)

    async def _activate_cooldown(self, reason: str) -> None:
        self._state.cooldown_until = datetime.now(timezone.utc) + timedelta(
            minutes=self._limits.cooldown_minutes
        )
        await self._persist_cooldown()
        await self._log_event("cooldown_activated", None, reason)
        log.warning(
            "risk_cooldown_activated",
            reason=reason,
            minutes=self._limits.cooldown_minutes,
        )

    async def _log_event(
        self, event_type: str, symbol: Optional[str], details: str
    ) -> None:
        try:
            async with async_session_factory() as session:
                session.add(
                    RiskEvent(event_type=event_type, symbol=symbol, details=details)
                )
                await session.commit()
        except Exception as e:
            log.error("risk_event_log_error", error=str(e))

    async def _persist_daily_stats(self, pnl: float) -> None:
        today = datetime.now(timezone.utc).date()
        try:
            async with async_session_factory() as session:
                from sqlalchemy import select
                from db.models import DailyStats as DS

                result = await session.execute(select(DS).where(DS.date == today))
                stats = result.scalar_one_or_none()
                if stats:
                    stats.total_pnl = (stats.total_pnl or 0) + pnl
                    stats.trade_count = (stats.trade_count or 0) + 1
                    if pnl > 0:
                        stats.win_count = (stats.win_count or 0) + 1
                        if pnl > (stats.largest_win or 0):
                            stats.largest_win = pnl
                    else:
                        stats.loss_count = (stats.loss_count or 0) + 1
                        if pnl < (stats.largest_loss or 0):
                            stats.largest_loss = pnl
                else:
                    session.add(
                        DS(
                            date=today,
                            total_pnl=pnl,
                            trade_count=1,
                            win_count=1 if pnl > 0 else 0,
                            loss_count=1 if pnl <= 0 else 0,
                            largest_win=max(pnl, 0),
                            largest_loss=min(pnl, 0),
                        )
                    )
                await session.commit()
        except Exception as e:
            log.error("daily_stats_persist_error", error=str(e))

    async def activate_kill_switch(self, reason: str = "manual") -> None:
        self._killswitch_active = True
        await self._persist_killswitch(True)
        log.critical("killswitch_activated", reason=reason)

    async def deactivate_kill_switch(self) -> None:
        self._killswitch_active = False
        self._state.consecutive_losses = 0
        self._state.cooldown_until = None
        await self._persist_killswitch(False)
        await self._persist_consecutive_losses()
        await self._persist_cooldown()
        log.info("killswitch_deactivated")

    async def get_risk_report(self) -> dict:
        """Return current risk state for the dashboard API."""
        await self._sync_state()
        cooldown_until = await self._get_cooldown()
        killswitch = await self._get_killswitch()
        return {
            "killswitch_active": killswitch,
            "daily_pnl": round(self._state.daily_pnl, 4),
            "daily_trades": self._state.daily_trades,
            "consecutive_losses": self._state.consecutive_losses,
            "open_positions": self._state.open_position_count,
            "total_equity": round(self._state.total_equity, 4),
            "cooldown_active": cooldown_until is not None
            and datetime.now(timezone.utc) < cooldown_until,
            "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
            "limits": {
                "max_daily_loss_pct": self._limits.max_daily_loss_pct,
                "max_position_size_pct": self._limits.max_position_size_pct,
                "max_open_positions": self._limits.max_open_positions,
                "cooldown_minutes": self._limits.cooldown_minutes,
            },
        }


risk_engine = RiskEngine()
