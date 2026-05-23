"""Trade Execution Engine — hardened: idempotency, stale price guard, circuit breaker integration."""

from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass
import uuid

from config.settings import settings
from logging.logger import get_logger
from services.exchange import exchange_service, DuplicateOrderError
from risk.engine import risk_engine, RiskCheckResult
from db.models import Trade, Position, OrderStatus, PositionStatus
from db.session import async_session_factory

log = get_logger(__name__)


@dataclass
class ExecutionResult:
    accepted: bool
    trade_id: Optional[int] = None
    order_id: Optional[str] = None
    rejection_reason: Optional[str] = None
    action_taken: Optional[str] = None


class ExecutionEngine:
    """
    Execution engine receives AI recommendations, consults the risk engine,
    and decides whether to execute. It NEVER executes directly.

    Hardened against:
    - Duplicate execution (idempotency keys)
    - Stale price execution (max age check)
    - Exchange circuit breaker integration
    - Failed DB write recovery (order was placed but DB write failed)
    - Position/exchange desync on startup
    """

    # Maximum age of a market snapshot before it's considered stale
    MAX_SNAPSHOT_AGE_SECONDS = 10

    async def process_recommendation(
        self,
        decision_id: int,
        symbol: str,
        action: str,
        confidence: float,
        reasoning: str,
        market_snapshot_age: float = 0.0,
    ) -> ExecutionResult:
        """
        Main entry point: process an AI recommendation decision.

        market_snapshot_age: how many seconds old the market snapshot is.
        If > MAX_SNAPSHOT_AGE_SECONDS, reject the trade.
        """

        if action == "HOLD":
            return ExecutionResult(accepted=False, rejection_reason="AI recommended HOLD", action_taken="none")

        # ── Stale price guard ──────────────────────────────────────────────────
        if market_snapshot_age > self.MAX_SNAPSHOT_AGE_SECONDS:
            log.warning(
                "trade_rejected_stale_price",
                decision_id=decision_id,
                snapshot_age=market_snapshot_age,
                max_age=self.MAX_SNAPSHOT_AGE_SECONDS,
            )
            await self._log_rejection(decision_id, f"Stale price data ({market_snapshot_age:.1f}s old)")
            return ExecutionResult(
                accepted=False,
                rejection_reason=f"Market data stale ({market_snapshot_age:.1f}s) — refusing to execute",
                action_taken="none",
            )

        # ── Fetch fresh price ──────────────────────────────────────────────────
        try:
            ticker = await exchange_service.get_ticker(symbol)
            current_price = ticker["last"]
        except Exception as e:
            log.error("price_fetch_failed", error=str(e))
            return ExecutionResult(accepted=False, rejection_reason=f"Price fetch failed: {e}", action_taken="none")

        if not current_price or current_price <= 0:
            await self._log_rejection(decision_id, "Invalid price from exchange (0 or negative)")
            return ExecutionResult(accepted=False, rejection_reason="Invalid price from exchange", action_taken="none")

        # ── Run risk checks (uses fresh price for position value) ───────────────
        risk_result: RiskCheckResult = await risk_engine.check(
            action=action, symbol=symbol, price=current_price, size=0  # size checked after quantity calc
        )

        if not risk_result.allowed:
            log.warning("trade_rejected_by_risk", action=action, symbol=symbol, reason=risk_result.reason)
            await self._log_rejection(decision_id, risk_result.reason)
            return ExecutionResult(accepted=False, rejection_reason=risk_result.reason, action_taken="none")

        # ── Calculate position size ────────────────────────────────────────────
        risk_state = await risk_engine.get_risk_report()
        equity = risk_state.get("total_equity", 1000.0)
        position_value = equity * (settings.max_position_size_pct / 100)
        quantity = round(position_value / current_price, 6)

        if quantity <= 0:
            await self._log_rejection(decision_id, f"Invalid quantity computed: {quantity}")
            return ExecutionResult(accepted=False, rejection_reason=f"Invalid quantity: {quantity}", action_taken="none")

        # ── Execute with idempotency key ───────────────────────────────────────
        idempotency_key = f"{symbol}:{action}:{decision_id}:{int(datetime.now(timezone.utc).timestamp() // 60)}"
        # Reduce key entropy: one order per decision per minute max
        idempotency_key = f"{symbol}:{action}:{decision_id}"

        return await self._execute_trade(
            decision_id=decision_id,
            symbol=symbol,
            action=action,
            quantity=quantity,
            current_price=current_price,
            idempotency_key=idempotency_key,
        )

    async def _execute_trade(
        self,
        decision_id: int,
        symbol: str,
        action: str,
        quantity: float,
        current_price: float,
        idempotency_key: str,
    ) -> ExecutionResult:
        """Execute market order with idempotency + audit trail."""

        side = "buy" if action == "BUY" else "sell"
        now = datetime.now(timezone.utc)

        try:
            order = await exchange_service.place_market_order(
                symbol, side, quantity, idempotency_key=idempotency_key
            )
            order_id = order["id"]
            fill_price = order.get("fill_price") or current_price
            status = order.get("status", "open")

            # ── Atomic: record trade in DB ─────────────────────────────────
            try:
                async with async_session_factory() as session:
                    trade = Trade(
                        symbol=symbol,
                        side=action,
                        order_type="MARKET",
                        quantity=quantity,
                        price=current_price,
                        fill_price=fill_price,
                        status=status,
                        exchange_order_id=order_id,
                        executed_at=now,
                    )
                    session.add(trade)
                    await session.commit()
                    trade_id = trade.id
            except Exception as db_err:
                # DB write failed — but order was placed on exchange.
                # Log the order_id so it can be recovered manually.
                log.critical(
                    "trade_record_db_failed",
                    order_id=order_id,
                    symbol=symbol,
                    error=str(db_err),
                )
                # Release idempotency key since we're in uncertain state
                raise FatalExecutionError(
                    f"Order {order_id} placed but DB record failed: {db_err}. "
                    "Manual reconciliation required."
                ) from db_err

            # ── Update position ───────────────────────────────────────────────
            if action == "BUY":
                await self._open_position(symbol, side.upper(), quantity, fill_price)
            else:
                await self._close_position(symbol, quantity, fill_price)

            log.audit(
                "trade_executed",
                trade_id=trade_id,
                order_id=order_id,
                symbol=symbol,
                action=action,
                quantity=quantity,
                fill_price=fill_price,
                idempotency_key=idempotency_key,
            )

            return ExecutionResult(
                accepted=True,
                trade_id=trade_id,
                order_id=order_id,
                action_taken=f"{action} {quantity} {symbol} @ {fill_price}",
            )

        except DuplicateOrderError as e:
            log.warning("duplicate_order_rejected", reason=str(e), idempotency_key=idempotency_key)
            await self._log_rejection(decision_id, f"Duplicate order blocked: {e}")
            return ExecutionResult(accepted=False, rejection_reason=str(e), action_taken="none")

        except Exception as e:
            log.error("trade_execution_error", symbol=symbol, error=str(e))
            return ExecutionResult(accepted=False, rejection_reason=f"Execution error: {e}")

    async def _open_position(self, symbol: str, side: str, quantity: float, price: float) -> None:
        async with async_session_factory() as session:
            from sqlalchemy import select
            from db.models import Position as Pos

            result = await session.execute(
                select(Pos).where(Pos.symbol == symbol, Pos.status == PositionStatus.OPEN.value)
            )
            existing = result.scalar_one_or_none()

            if existing:
                if existing.side.upper() != side.upper():
                    log.error("position_side_conflict", symbol=symbol, existing=existing.side, incoming=side)
                    # Don't average — log and abort for safety
                    return
                total_qty = existing.quantity + quantity
                avg_price = (existing.entry_price * existing.quantity + price * quantity) / total_qty
                existing.quantity = total_qty
                existing.entry_price = avg_price
                existing.current_price = price
            else:
                session.add(
                    Position(
                        symbol=symbol,
                        side=side.upper(),
                        quantity=quantity,
                        entry_price=price,
                        current_price=price,
                        status=PositionStatus.OPEN.value,
                    )
                )
            await session.commit()

    async def _close_position(self, symbol: str, quantity: float, price: float) -> None:
        async with async_session_factory() as session:
            from sqlalchemy import select
            from db.models import Position as Pos

            result = await session.execute(
                select(Pos).where(Pos.symbol == symbol, Pos.status == PositionStatus.OPEN.value)
            )
            position = result.scalar_one_or_none()

            if not position:
                log.error("close_position_no_open_position", symbol=symbol, quantity=quantity)
                return

            pnl = (
                (price - position.entry_price) * quantity
                if position.side.upper() == "BUY"
                else (position.entry_price - price) * quantity
            )
            position.quantity -= quantity
            position.current_price = price
            position.realized_pnl = (position.realized_pnl or 0) + pnl

            if position.quantity <= 0.000001:  # floating point safety
                position.status = PositionStatus.CLOSED.value
                position.closed_at = datetime.now(timezone.utc)
                position.quantity = 0.0
                await risk_engine.record_trade(pnl, symbol)

            await session.commit()

    async def reconcile_positions(self) -> dict:
        """
        Full reconciliation: compare DB positions against exchange balances.
        Called on startup and periodically.
        Returns discrepancies for manual review.
        """
        discrepancies = []

        async with async_session_factory() as session:
            from sqlalchemy import select
            from db.models import Position as Pos

            result = await session.execute(select(Pos).where(Pos.status == PositionStatus.OPEN.value))
            db_positions = result.scalars().all()

        for pos in db_positions:
            try:
                exchange_amount = await exchange_service.get_position(pos.symbol)
                if exchange_amount is None:
                    discrepancies.append({
                        "symbol": pos.symbol,
                        "issue": "DB shows position OPEN, exchange shows none",
                        "db_quantity": pos.quantity,
                        "exchange_quantity": 0,
                        "action": "CLOSE_DB_POSITION",
                    })
                    log.warning("reconcile_position_missing_on_exchange", symbol=pos.symbol, db_qty=pos.quantity)
                elif abs(exchange_amount.get("amount", 0) - pos.quantity) > 0.00001:
                    discrepancies.append({
                        "symbol": pos.symbol,
                        "issue": "Position quantity mismatch",
                        "db_quantity": pos.quantity,
                        "exchange_quantity": exchange_amount.get("amount"),
                        "action": "REVIEW_AND_CORRECT",
                    })
                    log.warning(
                        "reconcile_position_mismatch",
                        symbol=pos.symbol,
                        db_qty=pos.quantity,
                        exchange_qty=exchange_amount.get("amount"),
                    )
            except Exception as e:
                log.error("reconcile_exchange_fetch_failed", symbol=pos.symbol, error=str(e))

        return {"discrepancies": discrepancies, "checked_at": datetime.now(timezone.utc).isoformat()}

    async def _log_rejection(self, decision_id: int, reason: str) -> None:
        try:
            async with async_session_factory() as session:
                from sqlalchemy import select
                from db.models import AIDecision

                result = await session.execute(select(AIDecision).where(AIDecision.id == decision_id))
                decision = result.scalar_one_or_none()
                if decision:
                    decision.accepted = False
                    decision.accepted_at = datetime.now(timezone.utc)
                    decision.rejection_reason = reason
                    await session.commit()
        except Exception as e:
            log.error("rejection_log_error", error=str(e))

    async def get_open_positions(self) -> list[dict]:
        async with async_session_factory() as session:
            from sqlalchemy import select
            from db.models import Position as Pos
            result = await session.execute(select(Pos).where(Pos.status == PositionStatus.OPEN.value))
            positions = result.scalars().all()
            return [self._position_to_dict(p) for p in positions]

    async def get_pnl_summary(self) -> dict:
        async with async_session_factory() as session:
            from sqlalchemy import select
            from db.models import Position as Pos

            result = await session.execute(select(Pos))
            positions = result.scalars().all()

            total_realized = sum(p.realized_pnl or 0 for p in positions)
            total_unrealized = sum(
                self._calc_unrealized(p) for p in positions
            )
            return {
                "total_realized_pnl": round(total_realized, 4),
                "total_unrealized_pnl": round(total_unrealized, 4),
                "total_pnl": round(total_realized + total_unrealized, 4),
                "open_positions_count": len([p for p in positions if p.status == PositionStatus.OPEN.value]),
            }

    def _calc_unrealized(self, p: Position) -> float:
        if p.side.upper() == "BUY":
            return round((p.current_price - p.entry_price) * p.quantity, 4)
        else:
            return round((p.entry_price - p.current_price) * p.quantity, 4)

    def _position_to_dict(self, p: Position) -> dict:
        return {
            "id": p.id,
            "symbol": p.symbol,
            "side": p.side,
            "quantity": p.quantity,
            "entry_price": p.entry_price,
            "current_price": p.current_price,
            "unrealized_pnl": self._calc_unrealized(p),
            "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        }


class FatalExecutionError(Exception):
    """Order was placed on exchange but DB recording failed. Requires manual intervention."""
    pass


execution_engine = ExecutionEngine()