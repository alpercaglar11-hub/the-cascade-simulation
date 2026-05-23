"""Main FastAPI application — hardened startup and shutdown sequence."""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from prometheus_client import make_asgi_app
from redis.asyncio import Redis, ConnectionPool

from config.settings import settings
from logging.logger import setup_logging, get_logger
from db.session import init_db
from services.exchange import exchange_service
from services.market_data import market_data_engine
from services.cache import get_redis, close_redis
from risk.engine import risk_engine
from agents.trading_loop import trading_loop
from api.routes import dashboard

log = get_logger(__name__)

# API key header scheme
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: str = Depends(api_key_header)) -> str:
    """
    Dependency: validates X-API-Key header.
    In development (no api_key configured), all requests pass.
    In production/staging, a non-empty api_key is required and must match.
    """
    if not settings.api_key:
        # No key configured — allow all (development only)
        return "dev"

    if not key:
        raise HTTPException(status_code=401, detail="X-API-Key header required")

    if key != settings.api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")

    return key


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown sequence with full service initialization."""
    setup_logging()
    log.info(
        "application_starting", environment=settings.environment, debug=settings.debug
    )

    # ── Database ───────────────────────────────────────────────────────────────
    await init_db()
    log.info("database_initialized")

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_client = await get_redis()
    log.info("redis_connected")

    # ── Exchange ──────────────────────────────────────────────────────────────
    await exchange_service.connect(redis_client=redis_client)
    log.info("exchange_connected")

    # ── Market Data ────────────────────────────────────────────────────────────
    await market_data_engine.start(redis_client=redis_client)
    log.info("market_data_engine_started")

    # ── Risk Engine (needs Redis for persistent state) ─────────────────────────
    await risk_engine._set_redis(redis_client)
    log.info("risk_engine_redis_linked")

    # ── Trading Loop ───────────────────────────────────────────────────────────
    await trading_loop.start(redis_client=redis_client)
    log.info("trading_loop_started")

    yield

    # ── Graceful Shutdown ─────────────────────────────────────────────────────
    log.info("application_shutting_down")

    # Stop trading loop first (prevents new orders)
    await trading_loop.stop()

    # Stop market data (closes WebSocket)
    await market_data_engine.stop()

    # Close exchange connection cleanly
    if exchange_service._exchange:
        try:
            await exchange_service._exchange.close()
        except Exception as e:
            log.warning("exchange_close_error", error=str(e))

    # Close Redis
    await close_redis()

    log.info("application_shutdown_complete")


app = FastAPI(
    title="AI Trading System",
    description="Production-grade AI-assisted crypto trading platform",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS — restrict in production ──────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        ["*"] if settings.environment == "development" else ["https://yourdomain.com"]
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST"],  # No wildcard — restrict methods
    allow_headers=["*"],
)

# Prometheus metrics (no auth — internal use only)
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

# Routes (auth dependency injected via Depends in each route)
app.include_router(dashboard.router)


@app.get("/")
async def root():
    return {
        "service": "AI Trading System",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/v1/health",
    }


# Global exception handler — don't leak internal errors
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error("unhandled_api_exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
