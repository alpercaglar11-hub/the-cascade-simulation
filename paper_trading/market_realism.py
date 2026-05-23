"""
Market Realism Simulation Layer for Paper Trading Engine.

Wraps PaperExchange to simulate hostile exchange conditions:
- Realistic order book with limited liquidity and market impact
- Volatility regimes (low, trend, cascade, flash crash, news spike)
- Adversarial market behavior (stop hunts, fake breakouts, whipsaws)
- Realistic failures (partial fills, delayed fills, rejections, jitter)
- Execution quality metrics tracking

Usage:
    from paper_trading.market_realism import MarketRealismEngine, MarketRealismConfig

    config = MarketRealismConfig(
        enable_order_book=True,
        enable_adversarial=True,
        enable_volatility_regimes=True,
    )
    realism = MarketRealismEngine(exchange, config)
    # Feed prices through realism layer
    await realism.on_price_update("BTC/USDT", new_price)
    # Place orders through realism layer
    result = await realism.place_market_order("BTC/USDT", "buy", 0.5)
"""

import asyncio
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Optional

from paper_trading._logger import get_logger

log = get_logger(__name__)

SLIPPAGE_BASE_BPS = 3  # re-exported from engine for use in this module


# ── Volatility Regimes ──────────────────────────────────────────────────────────


class VolatilityRegime(Enum):
    LOW = auto()  # Tight spreads, small moves, predictable
    TREND = auto()  # Directional expansion, widening spreads
    CASCADE = auto()  # Liquidation cascade — falling prices trigger more liquidations
    CRASH = auto()  # Flash crash — gap down, then slow recovery
    SPIKE = auto()  # News-driven sudden jump, then settle


@dataclass
class VolatilityRegimeConfig:
    regime: VolatilityRegime
    duration_ticks: int = 60
    base_vol_mult: float = 1.0
    spread_mult: float = 1.0
    price_drift_bps: float = 0.0
    liquidity_mult: float = 1.0
    rejection_rate: float = 0.0
    partial_fill_mult: float = 1.0


REGIME_DEFAULTS: dict[VolatilityRegime, VolatilityRegimeConfig] = {
    VolatilityRegime.LOW: VolatilityRegimeConfig(
        VolatilityRegime.LOW, price_drift_bps=0, spread_mult=0.6, liquidity_mult=1.5
    ),
    VolatilityRegime.TREND: VolatilityRegimeConfig(
        VolatilityRegime.TREND, price_drift_bps=15, spread_mult=1.5, liquidity_mult=0.9
    ),
    VolatilityRegime.CASCADE: VolatilityRegimeConfig(
        VolatilityRegime.CASCADE,
        price_drift_bps=-25,
        spread_mult=2.5,
        liquidity_mult=0.4,
    ),
    VolatilityRegime.CRASH: VolatilityRegimeConfig(
        VolatilityRegime.CRASH, price_drift_bps=-50, spread_mult=4.0, liquidity_mult=0.2
    ),
    VolatilityRegime.SPIKE: VolatilityRegimeConfig(
        VolatilityRegime.SPIKE, price_drift_bps=30, spread_mult=3.0, liquidity_mult=0.5
    ),
}


# ── Order Book ─────────────────────────────────────────────────────────────────


@dataclass
class BookLevel:
    price: float
    quantity: float


class OrderBookSimulator:
    """
    Simulates a realistic limit order book with:
    - Multiple price levels on each side
    - Limited liquidity at each level (shrinks with distance from mid)
    - Dynamic spread based on volatility regime
    - Market impact when orders walk through the book
    """

    def __init__(
        self,
        mid_price: float = 50_000.0,
        num_levels: int = 20,
        base_spread_bps: float = 5.0,
        base_level_qty: float = 5.0,
        tick_size: float = 0.5,
    ):
        self._mid_price = mid_price
        self._num_levels = num_levels
        self._base_spread_bps = base_spread_bps
        self._base_level_qty = base_level_qty
        self._tick_size = tick_size
        self._regime_spread_mult: float = 1.0
        self._regime_liquidity_mult: float = 1.0
        self._bid_levels: list[BookLevel] = []
        self._ask_levels: list[BookLevel] = []
        self._refresh_book()

    def _refresh_book(self) -> None:
        spread = self._mid_price * (
            self._base_spread_bps * self._regime_spread_mult / 10_000
        )
        mid = self._mid_price
        self._bid_levels = []
        self._ask_levels = []
        for i in range(1, self._num_levels + 1):
            offset = spread / 2 + (i - 1) * self._tick_size
            qty = (
                self._base_level_qty * self._regime_liquidity_mult * math.exp(-i * 0.08)
            )
            self._bid_levels.append(
                BookLevel(price=round(mid - offset, 2), quantity=max(0.01, qty))
            )
            self._ask_levels.append(
                BookLevel(price=round(mid + offset, 2), quantity=max(0.01, qty))
            )

    def update_mid_price(self, new_mid: float) -> None:
        self._mid_price = new_mid
        self._refresh_book()

    def apply_regime(self, spread_mult: float, liquidity_mult: float) -> None:
        self._regime_spread_mult = max(0.1, spread_mult)
        self._regime_liquidity_mult = max(0.05, liquidity_mult)
        self._refresh_book()

    def get_spread_bps(self) -> float:
        if not self._ask_levels or not self._bid_levels:
            return 0.0
        return (
            (self._ask_levels[0].price - self._bid_levels[0].price)
            / self._mid_price
            * 10_000
        )

    def get_top_of_book(self) -> tuple[float, float, float]:
        bid = self._bid_levels[0].price if self._bid_levels else self._mid_price * 0.999
        ask = self._ask_levels[0].price if self._ask_levels else self._mid_price * 1.001
        depth = (
            (self._bid_levels[0].quantity + self._ask_levels[0].quantity)
            if self._bid_levels
            else 0.0
        )
        return bid, ask, depth

    def compute_fill_price(
        self,
        side: str,
        quantity: float,
        regime: VolatilityRegime,
    ) -> tuple[float, float, list[tuple[float, float]]]:
        """
        Walk through the order book to compute fill price for a market order.
        Returns (avg_fill_price, slippage_bps, fills_per_level)
        """
        levels = self._ask_levels if side == "buy" else self._bid_levels
        if not levels:
            return self._mid_price, 0.0, []

        regime_cfg = REGIME_DEFAULTS[regime]
        fills: list[tuple[float, float]] = []
        remaining = quantity
        total_cost = 0.0

        for i, level in enumerate(levels):
            if remaining <= 0:
                break
            filled_here = min(remaining, level.quantity)
            remaining -= filled_here
            impact_mult = (i + 1) ** 0.7 * regime_cfg.spread_mult
            level_slippage_bps = impact_mult * 0.5
            if side == "buy":
                fill_price = level.price * (1 + level_slippage_bps / 10_000)
            else:
                fill_price = level.price * (1 - level_slippage_bps / 10_000)
            total_cost += fill_price * filled_here
            fills.append((fill_price, filled_here))

        if remaining > 0:
            last = levels[-1].price
            worst = (
                last * (1 + regime_cfg.spread_mult * 2 / 10_000)
                if side == "buy"
                else last * (1 - regime_cfg.spread_mult * 2 / 10_000)
            )
            total_cost += worst * remaining
            fills.append((worst, remaining))

        avg_price = total_cost / quantity if quantity > 0 else self._mid_price
        actual_slippage = abs(avg_price - self._mid_price) / self._mid_price * 10_000
        return round(avg_price, 8), round(actual_slippage, 2), fills


# ── Execution Metrics ───────────────────────────────────────────────────────────


@dataclass
class ExecutionRecord:
    order_id: str
    symbol: str
    side: str
    order_type: str
    requested_price: float
    expected_fill: float
    actual_fill: float
    slippage_bps: float
    latency_ms: float
    rejected: bool = False
    partial: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    regime: str = "LOW"
    adverse_selection: bool = False


class ExecutionMetrics:
    """Tracks execution quality. Retrieve with get_summary()."""

    def __init__(self, max_records: int = 10_000):
        self._records: list[ExecutionRecord] = []
        self._max_records = max_records

    def record(
        self,
        order_id: str,
        symbol: str,
        side: str,
        order_type: str,
        requested_price: float,
        expected_fill: float,
        actual_fill: float,
        slippage_bps: float,
        latency_ms: float,
        rejected: bool = False,
        partial: bool = False,
        regime: str = "LOW",
    ) -> None:
        adverse = (
            not rejected
            and side == "buy"
            and actual_fill > expected_fill
            or not rejected
            and side == "sell"
            and actual_fill < expected_fill
        )
        rec = ExecutionRecord(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            requested_price=requested_price,
            expected_fill=expected_fill,
            actual_fill=actual_fill,
            slippage_bps=slippage_bps,
            latency_ms=latency_ms,
            rejected=rejected,
            partial=partial,
            regime=regime,
            adverse_selection=adverse,
        )
        self._records.append(rec)
        if len(self._records) > self._max_records:
            self._records = self._records[-self._max_records :]

    @staticmethod
    def _percentile(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        idx = int(len(s) * p / 100)
        return s[min(idx, len(s) - 1)]

    def get_summary(self) -> dict:
        if not self._records:
            return {"n": 0}
        fills = [r for r in self._records if not r.rejected]
        rejected = [r for r in self._records if r.rejected]
        adverse = [r for r in fills if r.adverse_selection]
        slippage = [r.slippage_bps for r in fills]
        latency = [r.latency_ms for r in fills]
        n = len(self._records)
        return {
            "n_total": n,
            "n_filled": len(fills),
            "n_partial": len([r for r in fills if r.partial]),
            "n_rejected": len(rejected),
            "n_adverse": len(adverse),
            "rejection_rate_pct": round(len(rejected) / n * 100, 2),
            "adverse_rate_pct": (
                round(len(adverse) / len(fills) * 100, 2) if fills else 0
            ),
            "avg_slippage_bps": (
                round(sum(slippage) / len(slippage), 2) if slippage else 0
            ),
            "p50_slippage_bps": self._percentile(slippage, 50),
            "p95_slippage_bps": self._percentile(slippage, 95),
            "p99_slippage_bps": self._percentile(slippage, 99),
            "avg_latency_ms": round(sum(latency) / len(latency), 2) if latency else 0,
            "p50_latency_ms": self._percentile(latency, 50),
            "p99_latency_ms": self._percentile(latency, 99),
        }


# ── Adversarial Events ──────────────────────────────────────────────────────────


@dataclass
class AdversarialConfig:
    stop_hunt_prob: float = 0.02
    fake_breakout_prob: float = 0.015
    whipsaw_prob: float = 0.01
    hft_noise_prob: float = 0.05
    noise_burst_ticks: int = 5
    noise_bps: float = 3.0
    stop_hunt_depth_bps: float = 20.0
    whipsaw_depth_bps: float = 15.0


class AdversarialMarketEngine:
    """
    Simulates adversarial price actions:
    - Stop hunts: price briefly penetrates stop levels then snaps back
    - Fake breakouts: breaks a high/low then reverses
    - Whipsaws: rapid back-and-forth catching momentum traders
    - HFT noise bursts: short bursts of high-frequency micro-moves
    """

    def __init__(self, config: AdversarialConfig):
        self._cfg = config
        self._active_event: Optional[str] = None
        self._event_ticks_remaining: int = 0
        self._event_origin_price: float = 0.0
        self._hft_burst_remaining: int = 0

    def apply_adversarial_modifier(
        self,
        base_price: float,
        regime: VolatilityRegime,
    ) -> float:
        """Main entry point. Call on every price update."""
        # HFT noise burst
        if self._hft_burst_remaining > 0:
            self._hft_burst_remaining -= 1
            return base_price + random.gauss(
                0, base_price * (self._cfg.noise_bps / 10_000)
            )

        # Continue active event
        if self._event_ticks_remaining > 0:
            self._event_ticks_remaining -= 1
            return self._execute_event_phase(base_price)

        # Maybe start a new adversarial event
        if random.random() < self._cfg.stop_hunt_prob:
            self._start_event("stop_hunt", base_price)
        elif random.random() < self._cfg.fake_breakout_prob:
            self._start_event("fake_breakout", base_price)
        elif random.random() < self._cfg.whipsaw_prob:
            self._start_event("whipsaw", base_price)
        elif random.random() < self._cfg.hft_noise_prob:
            self._hft_burst_remaining = self._cfg.noise_burst_ticks

        return base_price

    def _start_event(self, event_type: str, price: float) -> None:
        self._active_event = event_type
        self._event_ticks_remaining = random.randint(3, 8)
        self._event_origin_price = price

    def _execute_event_phase(self, base_price: float) -> float:
        if self._active_event == "stop_hunt":
            ticks_left = self._event_ticks_remaining
            if ticks_left > 2:
                drift = (
                    self._event_origin_price
                    * (self._cfg.stop_hunt_depth_bps / 10_000)
                    * 0.3
                )
                return self._event_origin_price + drift
            else:
                snap = (
                    self._event_origin_price
                    * (self._cfg.stop_hunt_depth_bps / 10_000)
                    * 0.8
                )
                return (
                    self._event_origin_price
                    - snap
                    + random.gauss(0, self._event_origin_price * 0.001)
                )

        elif self._active_event == "fake_breakout":
            ticks = self._event_ticks_remaining
            if ticks > 3:
                push = self._event_origin_price * (self._cfg.whipsaw_depth_bps / 10_000)
                return self._event_origin_price + push
            else:
                pullback = (
                    self._event_origin_price
                    * (self._cfg.whipsaw_depth_bps / 10_000)
                    * 0.9
                )
                return self._event_origin_price - pullback

        elif self._active_event == "whipsaw":
            phase = (8 - self._event_ticks_remaining) % 4
            amplitude = self._event_origin_price * (
                self._cfg.whipsaw_depth_bps / 10_000
            )
            direction = 1 if phase < 2 else -1
            return self._event_origin_price + direction * amplitude * 0.5 * (
                random.random() + 0.5
            )

        return base_price


# ── Config ──────────────────────────────────────────────────────────────────────


@dataclass
class MarketRealismConfig:
    enable_order_book: bool = True
    order_book_levels: int = 20
    base_spread_bps: float = 5.0
    base_level_qty: float = 5.0

    enable_volatility_regimes: bool = True
    regime_change_prob: float = 0.01
    initial_regime: VolatilityRegime = VolatilityRegime.LOW

    enable_adversarial: bool = True
    adversarial: AdversarialConfig = field(default_factory=AdversarialConfig)

    random_rejection_rate: float = 0.01
    rejection_rate_mult: float = 1.0
    base_latency_ms: float = 80.0
    latency_jitter_ms: float = 30.0
    latency_spike_prob: float = 0.02
    max_latency_spike_ms: float = 500.0

    enable_delayed_fills: bool = True
    delayed_fill_prob: float = 0.05
    delayed_fill_max_ms: float = 500.0

    enable_stale_snapshot: bool = True
    stale_snapshot_prob: float = 0.03
    enable_out_of_order: bool = True
    out_of_order_prob: float = 0.01

    enable_ohlcv_noise: bool = True


# ── Main Realism Engine ─────────────────────────────────────────────────────────


class MarketRealismEngine:
    """
    Wraps PaperExchange with a realistic market simulation layer.

    Usage:
        realism = MarketRealismEngine(exchange, config)
        # Feed every price update through the layer BEFORE passing to exchange
        price = await realism.on_price_update("BTC/USDT", incoming_price)
        await exchange._update_price("BTC/USDT", price)  # inject modified price

        # Place orders through realism layer for metrics + realistic fills
        result = await realism.place_market_order("BTC/USDT", "buy", 0.5)
    """

    def __init__(self, exchange, config: Optional[MarketRealismConfig] = None):
        self._exchange = exchange
        self._cfg = config or MarketRealismConfig()
        self._books: dict[str, OrderBookSimulator] = {}
        self._current_regime = self._cfg.initial_regime
        self._regime_ticks = 0
        self._adversary = AdversarialMarketEngine(self._cfg.adversarial)
        self._metrics = ExecutionMetrics()
        self._last_prices: dict[str, float] = {}

        # Apply initial regime config to the global regime multipliers so books
        # pick up the correct spread/liquidity the moment they are created.
        initial_cfg = REGIME_DEFAULTS[self._current_regime]
        self._regime_spread_mult: float = initial_cfg.spread_mult
        self._regime_liquidity_mult: float = initial_cfg.liquidity_mult

    # ── Price pipeline ─────────────────────────────────────────────────────────

    async def on_price_update(self, symbol: str, new_price: float) -> float:
        """
        Feed a price update through the full realism pipeline.
        Returns the (potentially modified) price to use downstream.
        """
        base_price = new_price

        # 1. Apply volatility regime directional drift
        if self._cfg.enable_volatility_regimes:
            base_price = self._apply_regime_drift(symbol, base_price)
            self._update_regime()
            regime_cfg = REGIME_DEFAULTS[self._current_regime]
            if symbol in self._books:
                self._books[symbol].apply_regime(
                    spread_mult=regime_cfg.spread_mult,
                    liquidity_mult=regime_cfg.liquidity_mult,
                )

        # 2. Apply adversarial modifications (stop hunts, fakeouts, whipsaws)
        if self._cfg.enable_adversarial:
            base_price = self._adversary.apply_adversarial_modifier(
                base_price, self._current_regime
            )

        # 3. Out-of-order: occasionally replay previous price instead
        if self._cfg.enable_out_of_order and symbol in self._last_prices:
            if random.random() < self._cfg.out_of_order_prob:
                base_price = self._last_prices[symbol] * random.uniform(0.9995, 1.0005)

        # 4. Update order book — create with current regime multipliers baked in
        if self._cfg.enable_order_book:
            if symbol not in self._books:
                book = OrderBookSimulator(
                    mid_price=base_price,
                    num_levels=self._cfg.order_book_levels,
                    base_spread_bps=self._cfg.base_spread_bps,
                    base_level_qty=self._cfg.base_level_qty,
                )
                book.apply_regime(self._regime_spread_mult, self._regime_liquidity_mult)
                self._books[symbol] = book
            else:
                self._books[symbol].update_mid_price(base_price)

        self._last_prices[symbol] = base_price
        return base_price

    def _apply_regime_drift(self, symbol: str, price: float) -> float:
        regime_cfg = REGIME_DEFAULTS[self._current_regime]
        drift = regime_cfg.price_drift_bps
        if drift == 0:
            return price
        noise = random.gauss(0, abs(drift) * 0.5)
        return price + price * ((drift + noise) / 10_000)

    def _update_regime(self) -> None:
        self._regime_ticks += 1
        regime_cfg = REGIME_DEFAULTS[self._current_regime]

        if self._regime_ticks > regime_cfg.duration_ticks:
            self._transition_regime()
            return

        if random.random() < self._cfg.regime_change_prob:
            self._transition_regime()

    def _transition_regime(self) -> None:
        """Switch to a new regime, preferring extreme ones occasionally."""
        candidates = [r for r in VolatilityRegime if r != self._current_regime]
        if random.random() < 0.3:
            candidates = [
                VolatilityRegime.CASCADE,
                VolatilityRegime.CRASH,
                VolatilityRegime.SPIKE,
            ] + candidates
        self._current_regime = random.choice(candidates)
        self._regime_ticks = 0
        regime_cfg = REGIME_DEFAULTS[self._current_regime]
        self._regime_spread_mult = regime_cfg.spread_mult
        self._regime_liquidity_mult = regime_cfg.liquidity_mult
        log.warning(
            "market_regime_change",
            regime=self._current_regime.name,
            duration_ticks=regime_cfg.duration_ticks,
            spread_mult=regime_cfg.spread_mult,
            drift_bps=regime_cfg.price_drift_bps,
        )

    # ── Order placement ────────────────────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        idempotency_key: str = "",
    ) -> dict:
        """
        Place a market order with full realism simulation:
        - Market impact from order book
        - Possible rejection
        - Realistic latency
        - Delayed fill simulation
        - Metrics recording
        """
        order_id = idempotency_key or f"realism_{uuid.uuid4().hex[:12]}"
        current_price = self._last_prices.get(symbol, 0.0)
        regime_cfg = REGIME_DEFAULTS[self._current_regime]

        # ── Rejection simulation ───────────────────────────────────────────────
        reject_prob = (
            self._cfg.random_rejection_rate * self._cfg.rejection_rate_mult
            + regime_cfg.rejection_rate
        )
        if random.random() < reject_prob:
            self._metrics.record(
                order_id=order_id,
                symbol=symbol,
                side=side,
                order_type="market",
                requested_price=current_price,
                expected_fill=current_price,
                actual_fill=0.0,
                slippage_bps=0.0,
                latency_ms=0.0,
                rejected=True,
                regime=self._current_regime.name,
            )
            raise Exception(
                f"[RealismLayer] Order rejected — market conditions (regime: {self._current_regime.name})"
            )

        # ── Realistic latency ─────────────────────────────────────────────────
        latency = await self._compute_realistic_latency(regime_cfg)

        # ── Expected fill from order book ─────────────────────────────────────
        expected_fill_price = current_price
        expected_slippage = 0.0
        if self._cfg.enable_order_book and symbol in self._books:
            expected_fill_price, expected_slippage, _ = self._books[
                symbol
            ].compute_fill_price(
                side=side, quantity=amount, regime=self._current_regime
            )

        # ── Delayed fill ──────────────────────────────────────────────────────
        if (
            self._cfg.enable_delayed_fills
            and random.random() < self._cfg.delayed_fill_prob
        ):
            delay_ms = random.uniform(50, self._cfg.delayed_fill_max_ms)
            await asyncio.sleep(delay_ms / 1000)
            expected_slippage += random.uniform(0.5, 2.0)

        # ── Execute via underlying exchange ───────────────────────────────────
        try:
            result = await self._exchange.place_market_order(
                symbol=symbol, side=side, amount=amount, idempotency_key=order_id
            )
            self._metrics.record(
                order_id=result.get("id", order_id),
                symbol=symbol,
                side=side,
                order_type="market",
                requested_price=current_price,
                expected_fill=expected_fill_price,
                actual_fill=result.get("fill_price", expected_fill_price),
                slippage_bps=result.get("slippage_bps", expected_slippage),
                latency_ms=result.get("latency_ms", latency),
                rejected=False,
                partial=result.get("status") == "partial",
                regime=self._current_regime.name,
            )
            return result
        except Exception as exc:
            self._metrics.record(
                order_id=order_id,
                symbol=symbol,
                side=side,
                order_type="market",
                requested_price=current_price,
                expected_fill=expected_fill_price,
                actual_fill=0.0,
                slippage_bps=0.0,
                latency_ms=latency,
                rejected=True,
                regime=self._current_regime.name,
            )
            raise

    async def _compute_realistic_latency(self, regime_cfg) -> float:
        if random.random() < self._cfg.latency_spike_prob:
            spike = random.uniform(
                self._cfg.max_latency_spike_ms * 0.5, self._cfg.max_latency_spike_ms
            )
            await asyncio.sleep(spike / 1000)
            return round(spike, 2)
        base = max(
            5.0, random.gauss(self._cfg.base_latency_ms, self._cfg.latency_jitter_ms)
        )
        regime_add = (regime_cfg.spread_mult - 1.0) * 20
        total = base + max(0.0, regime_add)
        await asyncio.sleep(total / 1000)
        return round(total, 2)

    # ── Market data ────────────────────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> dict:
        ticker = await self._exchange.get_ticker(symbol)
        if (
            self._cfg.enable_stale_snapshot
            and random.random() < self._cfg.stale_snapshot_prob
        ):
            adj = random.uniform(-0.002, 0.002)
            ticker["last"] = ticker["last"] * (1 + adj)
            ticker["bid"] = ticker["bid"] * (1 + adj)
            ticker["ask"] = ticker["ask"] * (1 + adj)
            ticker["_stale"] = True
        return ticker

    async def get_ohlcv(
        self, symbol: str, timeframe: str = "1m", limit: int = 100
    ) -> list:
        candles = await self._exchange.get_ohlcv(symbol, timeframe, limit)
        if not self._cfg.enable_ohlcv_noise:
            return candles
        regime_cfg = REGIME_DEFAULTS[self._current_regime]
        for candle in candles:
            regime_vol = abs(regime_cfg.price_drift_bps) * 0.5
            body_noise = random.gauss(0, regime_vol / 10_000 * candle["close"])
            candle["close"] = round(candle["close"] + body_noise, 8)
            candle["high"] = round(
                max(candle["high"], candle["close"] * (1 + random.uniform(0, 0.001))), 8
            )
            candle["low"] = round(
                min(candle["low"], candle["close"] * (1 - random.uniform(0, 0.001))), 8
            )
            if random.random() < 0.05:
                wick_mult = random.uniform(1.002, 1.008)
                if random.random() < 0.5:
                    candle["high"] = round(candle["high"] * wick_mult, 8)
                else:
                    candle["low"] = round(candle["low"] / wick_mult, 8)
        return candles

    # ── Passthrough helpers ───────────────────────────────────────────────────

    def get_spread_bps(self, symbol: str) -> float:
        if symbol in self._books:
            return self._books[symbol].get_spread_bps()
        return self._cfg.base_spread_bps

    @property
    def metrics(self) -> ExecutionMetrics:
        return self._metrics

    @property
    def current_regime(self) -> VolatilityRegime:
        return self._current_regime

    @property
    def config(self) -> MarketRealismConfig:
        return self._cfg
