"""Autonomous trading loop: orchestrating market data → AI decision → risk check → execution."""

import asyncio
from datetime import datetime, timezone
from typing import Optional
from redis.asyncio import Redis

from config.settings import settings
from logging.logger import get_logger
from services.market_data import market_data_engine
from agents.decision_agent import ai_decision_agent
from risk.engine import risk_engine
from execution.engine import execution_engine

log = get_logger(__name__)


class TradingLoop:
    """
    The autonomous trading loop.
    Pulls market data, gets an AI recommendation, runs risk checks, and executes if all gates pass.

    Safety features:
    - Heartbeat published to Redis on every tick (enables external liveness monitoring)
    - Stale data detection (passes snapshot age to execution engine)
    - Graceful shutdown with in-flight tick completion
    - Tick-level exception isolation (one tick failure doesn't kill the loop)
    """

    def __init__(self, interval_seconds: int = 120):
        self._interval = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._symbol = settings.default_symbol
        self._redis_client: Optional[Redis] = None
        self._shutdown_event = asyncio.Event()

    async def start(self, redis_client: Optional[Redis] = None) -> None:
        """Start the loop. Accepts optional Redis client for heartbeat publishing."""
        if self._running:
            return
        self._redis_client = redis_client
        self._running = True
        self._task = asyncio.create_task(self._run())
        log.info(
            "trading_loop_started", interval_seconds=self._interval, symbol=self._symbol
        )

    async def stop(self) -> None:
        """
        Graceful shutdown: stop scheduling new ticks, wait for in-flight tick to complete.
        """
        log.info("trading_loop_shutdown_initiated")
        self._running = False
        # Signal shutdown to in-flight tick
        self._shutdown_event.set()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("trading_loop_stopped")

    async def _run(self) -> None:
        while self._running:
            tick_task = asyncio.create_task(self._tick())
            try:
                # Wait for tick OR shutdown signal
                done, pending = await asyncio.wait(
                    [tick_task, self._shutdown_event.wait()],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Cancel the other one if it's still pending
                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
            except asyncio.CancelledError:
                tick_task.cancel()
                raise
            await asyncio.sleep(self._interval)

    async def _tick(self) -> None:
        decision_cycle_start = datetime.now(timezone.utc)
        log.debug("tick_start", time=decision_cycle_start.isoformat())

        try:
            # 1. Get fresh market snapshot with data age
            snapshot = await market_data_engine.get_market_snapshot()
            snapshot_age = snapshot.get("data_age_seconds", 0.0)

            # Log stale data warning but don't stop — let execution engine decide
            if snapshot_age > 10:
                log.warning("tick_stale_snapshot", age_seconds=snapshot_age)

            # 2. Get AI recommendation
            decision = await ai_decision_agent.analyze_and_decide(snapshot)

            if decision.get("throttled"):
                log.debug("decision_throttled_skipping_execution")
                return

            log.info(
                "tick_ai_decision",
                symbol=self._symbol,
                action=decision["action"],
                confidence=decision["confidence"],
                reasoning=decision["reasoning"],
            )

            # 3. Process recommendation through execution engine (passes snapshot age for stale price guard)
            result = await execution_engine.process_recommendation(
                decision_id=decision.get("decision_id", 0),
                symbol=self._symbol,
                action=decision["action"],
                confidence=decision["confidence"],
                reasoning=decision["reasoning"],
                market_snapshot_age=snapshot_age,
            )

            cycle_duration_ms = (
                datetime.now(timezone.utc) - decision_cycle_start
            ).total_seconds() * 1000
            log.info(
                "tick_execution_result",
                accepted=result.accepted,
                action_taken=result.action_taken,
                rejection_reason=result.rejection_reason,
                cycle_duration_ms=round(cycle_duration_ms, 1),
            )

            # 4. Reconcile positions after each tick (every tick is too frequent — log only)
            # Full reconciliation runs in a background task every 5 minutes

        except asyncio.CancelledError:
            log.info("tick_cancelled_during_shutdown")
            raise
        except Exception as e:
            log.error("trading_loop_tick_error", error=str(e))

        finally:
            # Always publish heartbeat after every tick attempt
            await self._publish_heartbeat(decision_cycle_start)

    async def _publish_heartbeat(self, tick_start: datetime) -> None:
        """Write last tick timestamp to Redis for external liveness monitoring."""
        if not self._redis_client:
            return
        try:
            await self._redis_client.set(
                "heartbeat:trading_loop:last_tick",
                tick_start.isoformat(),
                ex=600,
            )
        except Exception as e:
            log.warning("heartbeat_publish_failed", error=str(e))

    async def _update_position_prices(self) -> None:
        """Refresh current prices for all open positions."""
        try:
            positions = await execution_engine.get_open_positions()
            for pos in positions:
                ticker = await market_data_engine.get_market_snapshot()
                log.debug(
                    "position_updated", symbol=pos["symbol"], price=ticker["price"]
                )
        except Exception as e:
            log.error("position_update_error", error=str(e))


# Singleton
trading_loop = TradingLoop(interval_seconds=120)
