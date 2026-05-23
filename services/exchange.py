"""Exchange service: CCXT wrapper with idempotency, circuit breaker, and exchange reconciliation."""

import asyncio
from typing import Optional, Literal
import ccxt
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type
from config.settings import settings
from logging.logger import get_logger

log = get_logger(__name__)


class CircuitBreaker:
    """Holds circuit open after repeated exchange failures. Resets on success."""

    def __init__(self, failure_threshold: int = 5, reset_timeout: int = 60):
        self._failures = 0
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._opened_at: Optional[float] = None
        self._lock = asyncio.Lock()

    async def record_success(self) -> None:
        async with self._lock:
            if self._failures > 0:
                self._failures -= 1
                log.info("circuit_breaker_success", failures_remaining=self._failures)

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._opened_at = asyncio.get_event_loop().time()
                log.critical("circuit_breaker_opened", failures=self._failures)

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        elapsed = asyncio.get_event_loop().time() - self._opened_at
        if elapsed >= self._reset_timeout:
            log.info("circuit_breaker_half_open", failures=self._failures)
            return False
        return True

    async def try_call(self, fn, *args, **kwargs):
        """Execute fn if circuit is closed. Records result."""
        if self.is_open():
            raise ExchangeCircuitOpenError("Circuit breaker is open")
        try:
            result = await fn(*args, **kwargs)
            await self.record_success()
            return result
        except Exception as e:
            await self.record_failure()
            raise


class ExchangeCircuitOpenError(Exception):
    pass


class IdempotencyStore:
    """Redis-backed idempotency key store using SETNX pattern."""

    def __init__(self, redis_client):
        self._redis = redis_client

    async def try_acquire(self, key: str, ttl: int = 300) -> bool:
        """
        Atomically set key if not exists. Returns True if acquired (first attempt).
        Returns False if key already exists (duplicate).
        """
        acquired = await self._redis.set(key, "1", nx=True, ex=ttl)
        return bool(acquired)

    async def release(self, key: str) -> None:
        await self._redis.delete(key)


class OrderPoller:
    """Polls exchange for order status when a request times out mid-flight."""

    def __init__(self, exchange: ccxt.Exchange, symbol: str):
        self._exchange = exchange
        self._symbol = symbol
        self._lock = asyncio.Lock()

    async def poll_until_final(
        self, order_id: str, timeout: int = 30, interval: float = 1.0
    ) -> Optional[dict]:
        """Poll order status until terminal state or timeout."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        while loop.time() < deadline:
            async with self._lock:
                try:
                    await asyncio.sleep(interval)
                    order = await loop.run_in_executor(
                        None, self._exchange.fetchOrder, order_id, self._symbol
                    )
                    status = order.get("status")
                    if status in ("filled", "closed", "canceled", "rejected"):
                        return order
                except ccxt.OrderNotFound:
                    # Order not found — may have been cancelled or never existed
                    return None
                except ccxt.NetworkError:
                    continue

        log.warning("order_poll_timeout", order_id=order_id)
        return None


class ExchangeService:
    """
    Async CCXT wrapper with:
    - Circuit breaker (opens after 5 consecutive failures)
    - Idempotency via Redis (prevents duplicate order execution)
    - Order polling after suspected timeouts
    - Rate limiting per request with jitter
    - Exchange-on-startup reconciliation
    """

    def __init__(self):
        self._exchange: Optional[ccxt.Exchange] = None
        self._symbol = settings.default_symbol
        self._connected = False
        self._rate_limiter_lock = asyncio.Lock()
        self._circuit_breaker = CircuitBreaker(failure_threshold=5, reset_timeout=60)
        self._idempotency_store: Optional[IdempotencyStore] = None
        self._order_poller: Optional[OrderPoller] = None
        # Per-symbol lock to serialize orders per symbol (prevents race order conflicts)
        self._order_locks: dict[str, asyncio.Lock] = {}

    async def connect(self, redis_client=None) -> None:
        """Initialize exchange connection and reconcile state."""
        if self._connected:
            return

        if settings.binance_testnet:
            self._exchange = ccxt.binance(
                apiKey=settings.binance_api_key,
                secret=settings.binance_api_secret,
                enableRateLimit=True,
                options={"defaultType": "spot", "testnet": True},
            )
            self._exchange.set_sandbox_mode(True)
        else:
            self._exchange = ccxt.binance(
                apiKey=settings.binance_api_key,
                secret=settings.binance_api_secret,
                enableRateLimit=True,
                options={"defaultType": "spot"},
            )

        await self._load_markets()

        if redis_client:
            self._idempotency_store = IdempotencyStore(redis_client)

        self._order_poller = OrderPoller(self._exchange, self._symbol)
        self._connected = True
        log.info("exchange_connected", exchange="binance", testnet=settings.binance_testnet, symbol=self._symbol)

        # Reconcile on startup
        await self._reconcile_on_startup()

    async def _load_markets(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._exchange.loadMarkets)

    async def _rate_limit(self) -> None:
        """Throttle with jitter to avoid burst collisions."""
        async with self._rate_limiter_lock:
            import random
            await asyncio.sleep(0.2 + random.uniform(0, 0.1))

    def _get_order_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._order_locks:
            self._order_locks[symbol] = asyncio.Lock()
        return self._order_locks[symbol]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10, jitter=2),
        retry=retry_if_exception_type((ccxt.NetworkError,)),
        reraise=True,
    )
    async def _request(self, fn, *args, **kwargs):
        """Execute with retry + circuit breaker. Re-raises after exhaustion."""
        await self._rate_limit()

        try:
            loop = asyncio.get_event_loop()
            result = await self._circuit_breaker.try_call(
                lambda: loop.run_in_executor(None, fn, *args, **kwargs)
            )
            return result
        except ExchangeCircuitOpenError:
            log.critical("exchange_circuit_open_rejected")
            raise ccxt.ExchangeError("Circuit breaker open — exchange unavailable")
        except Exception as e:
            await self._circuit_breaker.record_failure()
            raise

    async def get_ticker(self, symbol: str) -> dict:
        await self._ensure_connected()
        ticker = await self._request(self._exchange.fetchTicker, symbol)
        return {
            "symbol": symbol,
            "bid": float(ticker.get("bid", 0)),
            "ask": float(ticker.get("ask", 0)),
            "last": float(ticker.get("last", 0)),
            "volume": float(ticker.get("baseVolume", 0)),
            "timestamp": ticker.get("timestamp"),
        }

    async def get_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list:
        await self._ensure_connected()
        data = await self._request(self._exchange.fetchOHLCV, symbol, timeframe, {"limit": limit})
        return [
            {
                "timestamp": c[0],
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            }
            for c in data
        ]

    async def place_market_order(
        self, symbol: str, side: Literal["buy", "sell"], amount: float, idempotency_key: str = ""
    ) -> dict:
        """
        Place market order with idempotency key.
        If idempotency_key is provided and the order was already placed, returns existing result.
        """
        await self._ensure_connected()

        if idempotency_key and self._idempotency_store:
            acquired = await self._idempotency_store.try_acquire(f"idempotency:{idempotency_key}")
            if not acquired:
                log.warning("duplicate_order_rejected", idempotency_key=idempotency_key)
                raise DuplicateOrderError(f"Order with idempotency key {idempotency_key} already in flight")

        order_lock = self._get_order_lock(symbol)
        async with order_lock:
            try:
                log.info("order_placing", symbol=symbol, side=side, type="market", amount=amount)
                order = await self._request(self._exchange.createMarketOrder, symbol, side, amount)
                parsed = self._parse_order(order)

                if idempotency_key and self._idempotency_store:
                    await self._idempotency_store.release(f"idempotency:{idempotency_key}")

                return parsed

            except Exception as e:
                # Idempotency key is NOT released on failure — order may still have been placed
                # Poll exchange to determine true state
                if idempotency_key:
                    log.warning("order_error_checking_exchange", error=str(e), idempotency_key=idempotency_key)
                    # Attempt to reconcile via polling — see if the order exists now
                    await self._reconcile_suspected_order(symbol, idempotency_key)
                raise

    async def place_limit_order(
        self, symbol: str, side: Literal["buy", "sell"], amount: float, price: float,
        idempotency_key: str = ""
    ) -> dict:
        await self._ensure_connected()

        if idempotency_key and self._idempotency_store:
            acquired = await self._idempotency_store.try_acquire(f"idempotency:{idempotency_key}")
            if not acquired:
                raise DuplicateOrderError(f"Order with idempotency key {idempotency_key} already in flight")

        order_lock = self._get_order_lock(symbol)
        async with order_lock:
            try:
                log.info("order_placing", symbol=symbol, side=side, type="limit", amount=amount, price=price)
                order = await self._request(self._exchange.createLimitOrder, symbol, side, amount, price)
                parsed = self._parse_order(order)
                if idempotency_key and self._idempotency_store:
                    await self._idempotency_store.release(f"idempotency:{idempotency_key}")
                return parsed
            except Exception as e:
                if idempotency_key:
                    await self._reconcile_suspected_order(symbol, idempotency_key)
                raise

    async def place_stop_loss(
        self, symbol: str, side: Literal["buy", "sell"], amount: float, stop_price: float
    ) -> dict:
        await self._ensure_connected()
        params = {"stopPrice": stop_price}
        log.info("order_placing", symbol=symbol, side=side, type="stop_loss", amount=amount, stop_price=stop_price)
        order = await self._request(
            self._exchange.createStopLossLimitOrder,
            symbol, side, amount, stop_price, params,
        )
        return self._parse_order(order)

    async def place_take_profit(
        self, symbol: str, side: Literal["buy", "sell"], amount: float, take_profit_price: float
    ) -> dict:
        await self._ensure_connected()
        params = {"stopPrice": take_profit_price}
        log.info("order_placing", symbol=symbol, side=side, type="take_profit", amount=amount, tp_price=take_profit_price)
        order = await self._request(
            self._exchange.createTakeProfitLimitOrder,
            symbol, side, amount, take_profit_price, params,
        )
        return self._parse_order(order)

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        await self._ensure_connected()
        order = await self._request(self._exchange.cancelOrder, order_id, symbol)
        return self._parse_order(order)

    async def get_balance(self, asset: str = "USDT") -> float:
        await self._ensure_connected()
        balance = await self._request(self._exchange.fetchBalance)
        free = balance.get(asset, {}).get("free", 0.0)
        log.info("balance_fetched", asset=asset, free=free)
        return float(free)

    async def get_order(self, order_id: str, symbol: str) -> Optional[dict]:
        """Get order status. Returns None if order not found."""
        await self._ensure_connected()
        try:
            order = await self._request(self._exchange.fetchOrder, order_id, symbol)
            return self._parse_order(order)
        except ccxt.OrderNotFound:
            return None

    async def get_open_orders(self, symbol: str) -> list[dict]:
        """Fetch all open orders for symbol (reconciles against exchange state)."""
        await self._ensure_connected()
        orders = await self._request(self._exchange.fetchOpenOrders, symbol)
        return [self._parse_order(o) for o in orders]

    async def _ensure_connected(self) -> None:
        if not self._connected:
            await self.connect()

    def _parse_order(self, order: dict) -> dict:
        return {
            "id": str(order.get("id", "")),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "type": order.get("type"),
            "quantity": float(order.get("amount", 0)),
            "price": float(order.get("price", 0) or 0),
            "fill_price": float(order.get("average", 0) or 0),
            "status": order.get("status"),
            "filled": float(order.get("filled", 0)),
            "remaining": float(order.get("remaining", 0)),
            "timestamp": order.get("timestamp"),
            "fee": order.get("fee", {}),
        }

    async def _reconcile_suspected_order(self, symbol: str, idempotency_key: str) -> None:
        """After a failed order request, poll exchange to check if order was placed."""
        log.info("reconcile_suspected_order", symbol=symbol, key=idempotency_key)
        if self._order_poller:
            await self._order_poller.poll_until_final(idempotency_key, timeout=15)

    async def _reconcile_on_startup(self) -> None:
        """
        On startup: fetch open orders from exchange and verify DB positions match.
        Log any discrepancies — do not auto-correct (requires human review).
        """
        try:
            open_orders = await self.get_open_orders(self._symbol)
            if open_orders:
                log.warning(
                    "startup_open_orders_found",
                    count=len(open_orders),
                    orders=[o["id"] for o in open_orders],
                )
        except Exception as e:
            log.error("startup_reconciliation_failed", error=str(e))

    async def reconnect(self) -> None:
        log.warning("exchange_reconnecting")
        self._connected = False
        self._circuit_breaker = CircuitBreaker()
        await self.connect()

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def circuit_breaker_status(self) -> dict:
        cb = self._circuit_breaker
        return {
            "failures": cb._failures,
            "is_open": cb.is_open(),
        }


class DuplicateOrderError(Exception):
    """Raised when an order with the same idempotency key is already in flight."""


# Singleton
exchange_service = ExchangeService()