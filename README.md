# AI Crypto Trading System — Setup & Operations Guide

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         FastAPI (8000)                          │
│                    Dashboard REST API + Metrics                 │
└─────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   ┌────▼──────┐      ┌──────▼──────┐      ┌─────▼──────┐
   │  Market   │      │     AI      │      │   Risk     │
   │   Data    │─────▶│  Decision   │─────▶│  Engine    │
   │  Engine   │      │   Agent     │      │            │
   └────┬──────┘      └─────────────┘      └─────┬──────┘
        │                                          │
   ┌────▼──────┐                            ┌─────▼──────┐
   │  Binance  │                            │ Execution  │
   │  WebSocket│                            │  Engine    │
   └───────────┘                            └─────┬──────┘
                                                 │
                                          ┌──────▼──────┐
                                          │   Exchange  │
                                          │  (CCXT)     │
                                          └─────────────┘
```

## Quick Start

### 1. Prerequisites

- Docker & Docker Compose
- Python 3.12+ (for local development)
- Binance testnet API keys ([get them here](https://testnet.binance.vision/))

### 2. Clone & Configure

```bash
cd /home/alper/trading_system
cp .env.example .env
```

Edit `.env`:
```env
BINANCE_API_KEY=your_testnet_api_key
BINANCE_API_SECRET=your_testnet_secret
ANTHROPIC_API_KEY=your_anthropic_key
POSTGRES_PASSWORD=choose_a_strong_password
ENVIRONMENT=development
```

### 3. Start with Docker

```bash
docker-compose up -d
```

Services will start:
- **API**: http://localhost:8000
- **Docs**: http://localhost:8000/docs
- **Prometheus**: http://localhost:9090
- **Grafana**: http://localhost:3000 (admin/admin)

### 4. Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Start Postgres and Redis (Docker)
docker-compose up -d postgres redis

# Run the API
uvicorn api.main:app --reload --port 8000
```

## Core API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/positions` | GET | Open positions with PnL |
| `/api/v1/pnl` | GET | Realized + unrealized PnL |
| `/api/v1/trades` | GET | Trade history |
| `/api/v1/ai-decisions` | GET | AI decision log |
| `/api/v1/risk` | GET | Risk state + limits |
| `/api/v1/market` | GET | Live market data + indicators |
| `/api/v1/risk/kill-switch?active=true` | GET | Toggle emergency kill switch |
| `/api/v1/ai/decide-now` | POST | Trigger manual AI decision |
| `/health` | GET | Service health check |

## Folder Structure

```
trading_system/
├── agents/
│   ├── decision_agent.py      # AI BUY/SELL/HOLD analyzer
│   └── trading_loop.py         # Autonomous trading orchestrator
├── api/
│   ├── main.py                 # FastAPI application
│   └── routes/
│       └── dashboard.py        # All dashboard endpoints
├── config/
│   └── settings.py             # Pydantic settings (from .env)
├── db/
│   ├── models.py               # SQLAlchemy ORM models
│   └── session.py              # Async DB session factory
├── execution/
│   └── engine.py               # Trade execution + position management
├── logging/
│   └── logger.py               # Structured logging (structlog)
├── monitoring/
│   ├── prometheus.yml
│   └── grafana/provisioning/   # Auto-provision dashboards
├── risk/
│   └── engine.py               # Risk checks, kill switch, cooldowns
├── services/
│   ├── cache.py                # Redis cache + rate limiter
│   ├── exchange.py             # CCXT exchange wrapper
│   └── market_data.py          # WebSocket streaming + indicators
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

## Key Design Decisions

### AI Never Executes Directly
The AI agent outputs only `BUY` / `SELL` / `HOLD`. The Execution Engine receives this recommendation and makes the final call after consulting the Risk Engine. If the AI says BUY but the risk engine says no, the trade doesn't happen. The AI never has keys.

### Kill Switch
The emergency kill switch can be toggled via API or activated automatically when daily loss limits are hit. When active, no new orders are placed regardless of AI recommendations.

### Indicator Suite
The market data engine calculates: RSI-14, EMA-9/21, ATR-14, MACD, Bollinger Bands, ADX, Volume SMA-20. These feed the AI prompt in real-time.

### WebSocket Resilience
The market data engine implements exponential backoff reconnection (1s → 60s max), ensuring it reconnects automatically after exchange disconnections.

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `BINANCE_TESTNET` | `true` | Use testnet vs mainnet |
| `DEFAULT_SYMBOL` | `BTC/USDT` | Trading pair |
| `MAX_DAILY_LOSS_PCT` | `5.0` | Daily loss limit (% of equity) |
| `MAX_POSITION_SIZE_PCT` | `10.0` | Max position size (% of equity) |
| `COOLDOWN_MINUTES_AFTER_LOSS` | `60` | Cooldown after 3 consecutive losses |
| `AI_MODEL` | `claude-sonnet-4-20250514` | Anthropic model |
| `EMERGENCY_KILL_SWITCH` | `false` | Start with kill switch active |

## Stopping

```bash
docker-compose down          # stop + keep data volumes
docker-compose down -v      # stop + destroy data (RESET)
```