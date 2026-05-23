"""Market data engine: WebSocket streaming with stale data detection and heartbeat."""

import asyncio
import websockets
import json
from datetime import datetime, timezone
from typing import Callable, Optional
from collections import deque, OrderedDict
import numpy as np
import pandas as pd
import ta
from redis.asyncio import Redis

from config.settings import settings
from logging.logger import get_logger
from db.session import async_session_factory
from db.models import OHLCV

log = get_logger(__name__)


class MarketDataEngine:
    """
    Manages WebSocket connection to Binance, stores OHLCV, and computes indicators.
    Features:
    - Stale data detection (alert when no updates received for N seconds)
    - Heartbeat published to Redis on every update
    - Subscriber callbacks for downstream consumers
    - Historical backfill on startup via REST
    """

    CANDLES_URL = "wss://stream.binance.com:9443/ws"
    TIMEFRAMES = ["1m", "5m", "15m", "1h"]
    MAX_STALE_SECONDS = 30  # Alert if no update received in this time

    def __init__(self, symbol: str = None):
        self._symbol = symbol or settings.default_symbol
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60

        # OHLCV buffers per timeframe — all access via self._lock
        self._candles: dict[str, deque] = {
            tf: deque(maxlen=500) for tf in self.TIMEFRAMES
        }
        self._lock = asyncio.Lock()

        # Subscriber callbacks
        self._subscribers: list[Callable] = []

        # Current market metrics — protected by self._lock
        self._current_price: float = 0.0
        self._spread: float = 0.0
        self._volume_24h: float = 0.0
        self._momentum: float = 0.0
        self._volatility: float = 0.0

        # Data freshness tracking
        self._last_update_received: Optional[datetime] = None
        self._is_stale = False
        self._stale_check_task: Optional[asyncio.Task] = None
        self._redis_client: Optional[Redis] = None

        # Per-symbol order locks (bounded)
        self._order_locks: OrderedDict = OrderedDict()

    async def start(self, redis_client: Optional[Redis] = None) -> None:
        """Start the WebSocket stream and background monitoring tasks."""
        if self._running:
            return
        self._running = True
        self._redis_client = redis_client

        asyncio.create_task(self._stream_loop())
        asyncio.create_task(self._fetch_historical())
        self._stale_check_task = asyncio.create_task(self._stale_check_loop())
        log.info("market_data_engine_started", symbol=self._symbol)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._stale_check_task:
            self._stale_check_task.cancel()
        log.info("market_data_engine_stopped")

    def subscribe(self, callback: Callable) -> None:
        self._subscribers.append(callback)

    async def _stale_check_loop(self) -> None:
        """Background task: check if data has gone stale and alert."""
        while self._running:
            await asyncio.sleep(5)
            async with self._lock:
                if self._last_update_received:
                    age = (
                        datetime.now(timezone.utc) - self._last_update_received
                    ).total_seconds()
                    was_stale = self._is_stale
                    self._is_stale = age > self.MAX_STALE_SECONDS
                    if self._is_stale and not was_stale:
                        log.error("market_data_stale", age_seconds=age)
                        # Publish stale alert to Redis
                        if self._redis_client:
                            await self._redis_client.publish(
                                "alerts:market_data_stale",
                                json.dumps(
                                    {"symbol": self._symbol, "age_seconds": age},
                                    default=str,
                                ),
                            )

    async def _stream_loop(self) -> None:
        """Main WebSocket loop with exponential backoff reconnect."""
        while self._running:
            try:
                streams = [f"{self._symbol.replace('/', '').lower()}@kline_1m"]
                ws_url = f"{self.CANDLES_URL}/{streams[0]}"

                async with websockets.connect(
                    ws_url, ping_interval=20, ping_timeout=10
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1  # reset backoff on successful connect
                    log.info("websocket_connected", url=ws_url)

                    async for raw in ws:
                        if not self._running:
                            break
                        await self._handle_message(raw)

            except (websockets.ConnectionClosed, OSError) as e:
                log.warning(
                    "websocket_disconnected",
                    error=str(e),
                    reconnecting_in=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    async def _handle_message(self, raw: str) -> None:
        """Parse and process a WebSocket kline message. All writes go through self._lock."""
        try:
            msg = json.loads(raw)
            kline = msg.get("k", {})
            if not kline:
                return

            tf = kline.get("i")
            candle = {
                "timestamp": datetime.fromtimestamp(
                    kline.get("t", 0) / 1000, tz=timezone.utc
                ),
                "open": float(kline.get("o", 0)),
                "high": float(kline.get("h", 0)),
                "low": float(kline.get("l", 0)),
                "close": float(kline.get("c", 0)),
                "volume": float(kline.get("v", 0)),
                "closed": kline.get("x", False),
            }

            async with self._lock:
                self._candles[tf].append(candle)
                self._current_price = candle["close"]
                self._last_update_received = datetime.now(timezone.utc)
                self._is_stale = False  # Reset stale flag on any update

            # Persist closed candles
            if candle["closed"]:
                await self._persist_candle(tf, candle)

            # Compute metrics (read from lock briefly)
            await self._compute_metrics()

            # Notify subscribers with a snapshot copy
            snapshot = await self.get_market_snapshot()
            for cb in self._subscribers:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(snapshot)
                    else:
                        cb(snapshot)
                except Exception as e:
                    log.error("subscriber_callback_error", error=str(e))

            # Publish heartbeat to Redis
            if self._redis_client:
                await self._redis_client.set(
                    "heartbeat:market_data:last_update",
                    datetime.now(timezone.utc).isoformat(),
                    ex=300,
                )

        except json.JSONDecodeError as e:
            log.error("websocket_parse_error", error=str(e))

    async def _fetch_historical(self) -> None:
        """Backfill recent candles on startup via REST (acquires self._lock)."""
        from services.exchange import exchange_service

        for tf in self.TIMEFRAMES:
            try:
                candles = await exchange_service.get_ohlcv(self._symbol, tf, limit=200)
                async with self._lock:
                    self._candles[tf].clear()
                    for c in candles:
                        self._candles[tf].append(
                            {
                                "timestamp": datetime.fromtimestamp(
                                    c["timestamp"] / 1000, tz=timezone.utc
                                ),
                                "open": c["open"],
                                "high": c["high"],
                                "low": c["low"],
                                "close": c["close"],
                                "volume": c["volume"],
                                "closed": True,
                            }
                        )
                await asyncio.sleep(1)
            except Exception as e:
                log.error("historical_fetch_error", timeframe=tf, error=str(e))

    async def _persist_candle(self, timeframe: str, candle: dict) -> None:
        try:
            async with async_session_factory() as session:
                record = OHLCV(
                    symbol=self._symbol,
                    timeframe=timeframe,
                    timestamp=candle["timestamp"],
                    open=candle["open"],
                    high=candle["high"],
                    low=candle["low"],
                    close=candle["close"],
                    volume=candle["volume"],
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            log.error("candle_persist_error", error=str(e))

    async def _compute_metrics(self) -> None:
        """Compute volatility and momentum. Reads from self._candles under lock."""
        buf = list(self._candles.get("1m", []))
        if len(buf) < 20:
            return

        closes = np.array([c["close"] for c in buf[-20:]])

        async with self._lock:
            self._volatility = float(np.std(closes[-20:]) / np.mean(closes[-20:]) * 100)
            if len(closes) >= 6:
                self._momentum = float((closes[-1] / closes[-6] - 1) * 100)
            self._spread = float((closes[-1] - buf[-1]["low"]) / closes[-1] * 100)

    async def get_market_snapshot(self) -> dict:
        """
        Return current market metrics + indicator values.
        Includes 'data_age_seconds' so callers can decide if data is fresh enough.
        """
        async with self._lock:
            candles_1m = list(self._candles["1m"])
            candles_15m = list(self._candles["15m"])

        indicators = {}
        if len(candles_1m) >= 50:
            closes = pd.Series([c["close"] for c in candles_1m])
            volumes = pd.Series([c["volume"] for c in candles_1m])

            try:
                indicators = {
                    "rsi_14": float(
                        ta.momentum.RSIIndicator(closes, window=14).rsi().iloc[-1]
                    ),
                    "ema_9": float(
                        ta.trend.EMAIndicator(closes, window=9).ema_indicator().iloc[-1]
                    ),
                    "ema_21": float(
                        ta.trend.EMAIndicator(closes, window=21)
                        .ema_indicator()
                        .iloc[-1]
                    ),
                    "atr_14": float(
                        ta.volatility.AverageTrueRange(
                            pd.Series([c["high"] for c in candles_1m]),
                            pd.Series([c["low"] for c in candles_1m]),
                            closes,
                            window=14,
                        )
                        .atr()
                        .iloc[-1]
                    ),
                    "macd": float(ta.trend.MACD(closes).macd().iloc[-1]),
                    "macd_signal": float(ta.trend.MACD(closes).macd_signal().iloc[-1]),
                    "volume_sma_20": float(volumes.rolling(20).mean().iloc[-1]),
                    "bb_upper": float(
                        ta.volatility.BollingerBands(closes).bollinger_hband().iloc[-1]
                    ),
                    "bb_lower": float(
                        ta.volatility.BollingerBands(closes).bollinger_lband().iloc[-1]
                    ),
                    "adx": float(
                        ta.trend.ADXIndicator(
                            pd.Series([c["high"] for c in candles_1m]),
                            pd.Series([c["low"] for c in candles_1m]),
                            closes,
                        )
                        .adx()
                        .iloc[-1]
                    ),
                }
            except Exception as e:
                log.warning("indicator_calculation_error", error=str(e))
                indicators = {}

        # Data age
        data_age_seconds = 0.0
        if self._last_update_received:
            data_age_seconds = (
                datetime.now(timezone.utc) - self._last_update_received
            ).total_seconds()

        return {
            "symbol": self._symbol,
            "price": self._current_price,
            "spread_pct": self._spread,
            "volume_24h": self._volume_24h,
            "momentum_pct": self._momentum,
            "volatility_pct": self._volatility,
            "data_age_seconds": data_age_seconds,
            "is_stale": self._is_stale,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "indicators": indicators,
        }

    async def get_latest_closes(
        self, timeframe: str = "1m", count: int = 100
    ) -> list[float]:
        async with self._lock:
            buf = list(self._candles.get(timeframe, []))
        return [c["close"] for c in buf[-count:]]

    def is_stale(self) -> bool:
        return self._is_stale

    @property
    def last_update(self) -> Optional[datetime]:
        return self._last_update_received


# Singleton
market_data_engine = MarketDataEngine()
