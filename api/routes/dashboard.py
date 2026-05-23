"""Dashboard API — positions, PnL, history, risk metrics, AI decisions. All endpoints require auth."""

from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic import BaseModel

from db.session import get_db
from db.models import Trade, Position, AIDecision, RiskEvent, DailyStats
from risk.engine import risk_engine
from execution.engine import execution_engine
from agents.decision_agent import ai_decision_agent
from services.market_data import market_data_engine
from config.settings import settings

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

# ── Auth dependency (imported from main.py for reuse) ──────────────────────────
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _verify_key(key: str = Depends(_api_key_header)) -> str:
    """Validate API key. In dev (no key set), always pass."""
    if not settings.api_key:
        return "dev"
    if not key or key != settings.api_key:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="X-API-Key required")
    return key


def audit_log(action: str, request: Request, extra: dict = None):
    """Structured audit log for all state-changing operations."""
    from logging.logger import get_audit_logger

    log = get_audit_logger("audit")
    log.info(
        action,
        path=request.url.path,
        client_ip=request.client.host if request.client else None,
        **(extra or {}),
    )


# ── Response Models ────────────────────────────────────────────────────────────


class PositionResponse(BaseModel):
    id: int
    symbol: str
    side: str
    quantity: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    opened_at: Optional[str]

    class Config:
        from_attributes = True


class PnLResponse(BaseModel):
    total_realized_pnl: float
    total_unrealized_pnl: float
    total_pnl: float
    open_positions_count: int


class TradeResponse(BaseModel):
    id: int
    symbol: str
    side: str
    order_type: str
    quantity: float
    price: float
    fill_price: float
    status: str
    exchange_order_id: Optional[str]
    executed_at: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class AIDecisionResponse(BaseModel):
    id: int
    symbol: str
    action: str
    confidence: float
    reasoning: str
    accepted: Optional[bool]
    created_at: datetime

    class Config:
        from_attributes = True


class RiskReportResponse(BaseModel):
    killswitch_active: bool
    daily_pnl: float
    daily_trades: int
    consecutive_losses: int
    open_positions: int
    total_equity: float
    cooldown_active: bool
    cooldown_until: Optional[str]
    limits: dict


class MarketSnapshotResponse(BaseModel):
    symbol: str
    price: float
    momentum_pct: float
    volatility_pct: float
    spread_pct: float
    data_age_seconds: float
    is_stale: bool
    timestamp: str
    indicators: dict


class DailyStatsResponse(BaseModel):
    date: datetime
    total_pnl: float
    trade_count: int
    win_count: int
    loss_count: int
    largest_win: float
    largest_loss: float
    ai_decisions: int
    accepted_decisions: int
    rejected_decisions: int

    class Config:
        from_attributes = True


# ── Routes — All require API key ───────────────────────────────────────────────


@router.get(
    "/positions",
    response_model=list[PositionResponse],
    dependencies=[Depends(_verify_key)],
)
async def get_positions():
    """Current open positions."""
    return await execution_engine.get_open_positions()


@router.get("/pnl", response_model=PnLResponse, dependencies=[Depends(_verify_key)])
async def get_pnl():
    """PnL summary: realized + unrealized."""
    return await execution_engine.get_pnl_summary()


@router.get(
    "/trades", response_model=list[TradeResponse], dependencies=[Depends(_verify_key)]
)
async def get_trades(
    limit: int = Query(50, ge=1, le=500),
    symbol: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Trade execution history."""
    query = select(Trade).order_by(desc(Trade.created_at)).limit(limit)
    if symbol:
        query = query.where(Trade.symbol == symbol)
    result = await db.execute(query)
    trades = result.scalars().all()
    return [
        TradeResponse(
            id=t.id,
            symbol=t.symbol,
            side=t.side,
            order_type=t.order_type,
            quantity=t.quantity,
            price=t.price,
            fill_price=t.fill_price,
            status=t.status,
            exchange_order_id=t.exchange_order_id,
            executed_at=t.executed_at.isoformat() if t.executed_at else None,
            created_at=t.created_at,
        )
        for t in trades
    ]


@router.get(
    "/ai-decisions",
    response_model=list[AIDecisionResponse],
    dependencies=[Depends(_verify_key)],
)
async def get_ai_decisions(
    limit: int = Query(20, ge=1, le=200),
    symbol: Optional[str] = None,
    action: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """AI decision log — shows what the agent recommended."""
    query = select(AIDecision).order_by(desc(AIDecision.created_at)).limit(limit)
    if symbol:
        query = query.where(AIDecision.symbol == symbol)
    if action:
        query = query.where(AIDecision.action == action)
    result = await db.execute(query)
    decisions = result.scalars().all()
    return [AIDecisionResponse.model_validate(d) for d in decisions]


@router.get(
    "/risk", response_model=RiskReportResponse, dependencies=[Depends(_verify_key)]
)
async def get_risk_report() -> RiskReportResponse:
    """Current risk state and active limits."""
    report = await risk_engine.get_risk_report()
    return RiskReportResponse(**report)


@router.get(
    "/market",
    response_model=MarketSnapshotResponse,
    dependencies=[Depends(_verify_key)],
)
async def get_market_snapshot():
    """Live market data and indicators."""
    snapshot = await market_data_engine.get_market_snapshot()
    return MarketSnapshotResponse(
        symbol=snapshot["symbol"],
        price=snapshot["price"],
        momentum_pct=snapshot["momentum_pct"],
        volatility_pct=snapshot["volatility_pct"],
        spread_pct=snapshot["spread_pct"],
        data_age_seconds=snapshot.get("data_age_seconds", 0.0),
        is_stale=snapshot.get("is_stale", False),
        timestamp=snapshot["timestamp"],
        indicators=snapshot["indicators"],
    )


@router.get(
    "/stats/daily",
    response_model=list[DailyStatsResponse],
    dependencies=[Depends(_verify_key)],
)
async def get_daily_stats(
    days: int = Query(30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Daily performance statistics."""
    result = await db.execute(
        select(DailyStats).order_by(desc(DailyStats.date)).limit(days)
    )
    stats = result.scalars().all()
    return [DailyStatsResponse.model_validate(s) for s in stats]


# ── State-changing endpoints — logged for audit ────────────────────────────────


@router.post("/risk/kill-switch", dependencies=[Depends(_verify_key)])
async def toggle_kill_switch(
    active: bool,
    request: Request,
    reason: str = Query(default="api_toggle"),
):
    """
    Activate or deactivate the emergency kill switch.
    POST because this is a state-changing operation.
    Every call is logged with client IP for audit trail.
    """
    audit_log("kill_switch_toggle", request, {"active": active, "reason": reason})

    if active:
        await risk_engine.activate_kill_switch(f"api:{reason}")
    else:
        await risk_engine.deactivate_kill_switch()

    return {
        "killswitch_active": active,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }


@router.post("/ai/decide-now", dependencies=[Depends(_verify_key)])
async def trigger_ai_decision(
    request: Request,
    symbol: str = Query(default=settings.default_symbol),
):
    """
    Manually trigger an AI decision cycle.
    Useful for testing and on-demand analysis.
    """
    audit_log("manual_ai_decision_trigger", request, {"symbol": symbol})
    snapshot = await market_data_engine.get_market_snapshot()
    decision = await ai_decision_agent.analyze_and_decide(snapshot)
    return decision


@router.post("/execution/reconcile", dependencies=[Depends(_verify_key)])
async def reconcile_positions(request: Request):
    """
    Trigger manual position reconciliation against exchange state.
    Returns any discrepancies found.
    """
    audit_log("manual_reconciliation", request)
    result = await execution_engine.reconcile_positions()
    return result


@router.get("/health")
async def health_check():
    """
    Health check — no auth required (load balancers need this).
    Returns basic system status.
    """
    return {
        "status": "ok",
        "environment": settings.environment,
        "symbol": settings.default_symbol,
        "market_data_stale": market_data_engine.is_stale(),
        "last_market_update": (
            market_data_engine.last_update.isoformat()
            if market_data_engine.last_update
            else None
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
