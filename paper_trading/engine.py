"""Paper Trading Exchange Engine.

Simulates a crypto exchange with:
- Market order fills (slippage model)
- Limit order placement + conditional fills
- Maker / taker fees
- Simulated network latency
- Partial fills (for large orders)
- Order rejections (risk limits, insufficient balance)
- Exchange downtime simulation

The interface mirrors services/exchange.py so ExecutionEngine
can use either interchangeably.
"""

import asyncio
import random
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal
from dataclasses import dataclass, field
from collections import deque

from paper_trading._logger import get_logger

log = get_logger(__name__)


# ── Fee schedule ────────────────────────────────────────────────────────────────
MAKER_FEE_BPS = 5  # 0.05% — maker rebate (negative)
TAKER_FEE_BPS = 10  # 0.10% — taker fee
SLIPPAGE_BASE_BPS = 3  # 0.03% base slippage for market orders


@dataclass
class PaperOrder:
    id: str
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "stop_loss", "take_profit"]
    quantity: float  # requested quantity
    filled_quantity: float = 0.0
    price: float = 0.0  # limit price (0 for market)
    stop_price: float = 0.0  # trigger price for stop orders
    fill_price: float = 0.0  # average fill price
    status: Literal["open", "filled", "partial", "cancelled", "rejected"] = "open"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fee: float = 0.0
    slippage_bps: float = 0.0
    latency_ms: float = 0.0


@dataclass
class PaperBalance:
    asset: str
    free: float
    locked: float = 0.0

    def available(self) -> float:
        return self.free


class SlippageModel:
    """
    Slippage = f(volume, volatility, order_size_vs_adv).

    Model:
    - base_slippage_bps = 3 (market orders always have some slippage)
    - vol_adjustment = volatility_pct / 1.0 (more volatile means more slippage)
    - size_adjustment = min(order_value / (adv * 0.01), 1.0) (up to 100% penalty at 1% of ADV)
    - total_slippage_bps = (base + vol_adjustment * 5) * (1 + size_adjustment)
    """

    @staticmethod
    def compute(
        order_value: float,
        adv: float,  # average daily volume in quote currency
        volatility_pct: float,  # current volatility as percentage
        is_market: bool = True,
    ) -> float:
        if not is_market:
            return 0.0

        base = SLIPPAGE_BASE_BPS
        vol_adj = (
            min(volatility_pct / 1.0, 3.0) * 5
        )  # cap at 15 bps volatility adjustment
        size_ratio = min(
            order_value / max(adv * 0.01, 0.01), 1.0
        )  # 1% of ADV = max penalty
        total = (base + vol_adj) * (1 + size_ratio)
        return round(total, 2)  # bps


class DowntimeSimulator:
    """
    Simulates exchange downtime events:
    - Scheduled downtime (predictable, brief)
    - Random API errors (5xx responses)
    - Latency spikes

    Configured via settings; off by default in paper trading.
    """

    def __init__(
        self,
        downtime_probability_per_call: float = 0.0,  # probability of downtime per call
        mean_downtime_seconds: float = 30.0,
        latency_spike_probability: float = 0.0,
        max_latency_spike_ms: float = 500.0,
    ):
        self._prob = downtime_probability_per_call
        self._mean_downtime = mean_downtime_seconds
        self._latency_prob = latency_spike_probability
        self._max_spike_ms = max_latency_spike_ms
        self._down_until: Optional[datetime] = None

    def is_down(self) -> bool:
        if self._down_until is None:
            return False
        return datetime.now(timezone.utc) < self._down_until

    async def maybe_simulate_latency(self) -> float:
        """Return extra latency in seconds. 0 if no spike."""
        if random.random() < self._latency_prob:
            spike = random.uniform(0, self._max_spike_ms)
            await asyncio.sleep(spike / 1000)
            return spike
        return 0.0

    async def check_or_simulate_downtime(self) -> None:
        """
        Called before each exchange request.
        If currently down, sleep until up.
        If not down, maybe start a new downtime event.
        """
        if self._down_until and datetime.now(timezone.utc) < self._down_until:
            remaining = (self._down_until - datetime.now(timezone.utc)).total_seconds()
            log.warning(
                "paper_exchange_downtime_active", remaining_seconds=round(remaining, 1)
            )
            await asyncio.sleep(min(remaining, 5))  # wait at most 5s per call
            if datetime.now(timezone.utc) < self._down_until:
                raise PaperExchangeDownError(
                    "Exchange is down for scheduled maintenance"
                )
            self._down_until = None
            return

        # Maybe start a new downtime event
        if random.random() < self._prob:
            duration = random.expovariate(1.0 / self._mean_downtime)
            self._down_until = datetime.now(timezone.utc) + timedelta(
                seconds=min(duration, 300)
            )
            log.warning(
                "paper_exchange_downtime_scheduled",
                duration_seconds=round(duration, 1),
                until=self._down_until.isoformat(),
            )
            raise PaperExchangeDownError(
                f"Exchange downtime simulated — back at {self._down_until.isoformat()}"
            )

    def clear_downtime(self) -> None:
        self._down_until = None


class PartialFillSimulator:
    """
    Simulates partial fills for large market orders.

    Large order = > 10% of order book depth at top of book.
    Fill schedule: first 40% fills immediately, remainder fills over N ticks.
    """

    @staticmethod
    def should_partial_fill(quantity: float, top_of_book_depth: float) -> bool:
        """Return True if order should be partially filled."""
        return quantity > top_of_book_depth * 0.1

    @staticmethod
    def compute_fill_schedule(
        quantity: float,
        top_of_book_depth: float,
        num_ticks: int = 3,
    ) -> list[float]:
        """
        Compute quantity per tick for partial fill.
        First tick = 40% of order, rest split evenly.
        """
        if not PartialFillSimulator.should_partial_fill(quantity, top_of_book_depth):
            return [quantity]

        first_fill = quantity * 0.4
        remaining = quantity - first_fill
        per_tick = remaining / (num_ticks - 1)
        return [first_fill] + [per_tick] * (num_ticks - 1)


# ── Paper Exchange ───────────────────────────────────────────────────────────────


class PaperExchange:
    """
    Simulated exchange that mirrors the ExchangeService interface.

    Configuration:
        initial_capital: starting USDT balance
        maker_fee_bps: maker fee in basis points (5 = 0.05%)
        taker_fee_bps: taker fee in basis points (10 = 0.10%)
        avg_daily_volume: ADV in quote currency for slippage model
        volatility: current volatility % for slippage model
        latency_ms: base simulated network latency
        downtime_sim: optional DowntimeSimulator

    State:
        balances: dict of asset -> PaperBalance
        orders: dict of order_id -> PaperOrder
        open_limit_orders: list of active limit orders (for price-trigger fills)
    """

    def __init__(
        self,
        initial_capital: float = 10_000.0,
        maker_fee_bps: float = MAKER_FEE_BPS,
        taker_fee_bps: float = TAKER_FEE_BPS,
        avg_daily_volume: float = 1_000_000.0,
        volatility: float = 1.5,
        base_latency_ms: float = 50.0,
        downtime_sim: Optional[DowntimeSimulator] = None,
        top_of_book_depth: float = 50.0,
    ):
        self._balances: dict[str, PaperBalance] = {
            "USDT": PaperBalance(asset="USDT", free=initial_capital, locked=0.0),
        }
        self._orders: dict[str, PaperOrder] = {}
        self._open_limit_orders: deque[PaperOrder] = deque()
        self._initial_capital = initial_capital

        # Config
        self._maker_fee_bps = maker_fee_bps
        self._taker_fee_bps = taker_fee_bps
        self._adv = avg_daily_volume
        self._volatility = volatility
        self._base_latency_ms = base_latency_ms
        self._downtime_sim = downtime_sim or DowntimeSimulator()
        self._top_of_book_depth = top_of_book_depth

        # Simulated current price (updated by market data engine)
        self._current_prices: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._connected = False

        # Per-symbol order locks (same pattern as real exchange)
        self._order_locks: dict[str, asyncio.Lock] = {}

    async def connect(self) -> None:
        """Paper exchange connects instantly — no network needed."""
        await asyncio.sleep(0)  # yield to event loop
        self._connected = True
        log.info(
            "paper_exchange_connected",
            initial_capital=self._initial_capital,
            taker_fee_bps=self._taker_fee_bps,
            maker_fee_bps=self._maker_fee_bps,
        )

    async def _simulate_latency(self) -> float:
        """Apply base + random latency."""
        extra = await self._downtime_sim.maybe_simulate_latency()
        base = random.gauss(self._base_latency_ms, self._base_latency_ms * 0.3)
        await asyncio.sleep(max(0, (base + extra) / 1000))
        return base + extra

    async def _check_downtime(self) -> None:
        await self._downtime_sim.check_or_simulate_downtime()

    def _get_order_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._order_locks:
            self._order_locks[symbol] = asyncio.Lock()
        return self._order_locks[symbol]

    async def _update_price(self, symbol: str, price: float) -> None:
        """Update current price and synchronously evaluate all pending order triggers.

        This is the PRIMARY execution path for order triggering.
        Orders are evaluated IMMEDIATELY on every price update — no polling lag.
        The background monitor (_limit_order_monitor) is a safety net only.
        """
        self._current_prices[symbol] = price
        await self._check_and_trigger_orders(symbol)

    def _get_price(self, symbol: str) -> float:
        return self._current_prices.get(symbol, 0.0)

    def _get_order_lock_for_symbol(self, symbol: str) -> asyncio.Lock:
        return self._get_order_lock(symbol)

    # ── Balances ────────────────────────────────────────────────────────────────

    async def get_balance(self, asset: str = "USDT") -> float:
        await self._simulate_latency()
        await self._check_downtime()
        bal = self._balances.get(asset)
        if bal is None:
            return 0.0
        return bal.free

    async def _reserve_balance(self, asset: str, amount: float) -> bool:
        """
        Lock balance for a pending order. Returns True if sufficient balance.

        NOTE: Caller MUST hold self._lock before calling this.
        """
        bal = self._balances.get(asset)
        if bal is None or bal.free < amount - 1e-9:  # tolerance for float equality
            return False
        bal.free -= amount
        bal.locked += amount
        return True

    async def _release_balance(self, asset: str, amount: float) -> None:
        """
        Unlock balance after order cancellation or fill.

        NOTE: Caller MUST hold self._lock before calling this.
        """
        bal = self._balances.get(asset)
        if bal:
            bal.locked = max(0.0, bal.locked - amount)
            bal.free += amount

    # ── Market Orders ──────────────────────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        amount: float,
        idempotency_key: str = "",
    ) -> dict:
        """
        Place a market order with simulated fill.

        Slippage model:
            - Base slippage based on order size relative to ADV
            - Volatility adjustment
            - Applied to fill price

        Partial fill model:
            - Large orders (> 10% of top-of-book depth) fill in multiple ticks
            - First tick: 40% of quantity at base price + full slippage
            - Subsequent ticks: remaining quantity, each at slightly worse prices
        """
        await self._check_downtime()
        lat = await self._simulate_latency()
        order_lock = self._get_order_lock(symbol)
        async with order_lock:
            current_price = self._get_price(symbol)
            if current_price <= 0:
                raise PaperExchangeError(f"No price data for {symbol}")

            order_value = amount * current_price
            slippage_bps = SlippageModel.compute(
                order_value=order_value,
                adv=self._adv,
                volatility_pct=self._volatility,
                is_market=True,
            )

            # Compute fill price with slippage
            if side == "buy":
                slippage_multiplier = 1 + slippage_bps / 10_000
            else:
                slippage_multiplier = 1 - slippage_bps / 10_000
            fill_price = round(current_price * slippage_multiplier, 8)

            # Reserve balance: must cover the FULL expected cost (slippage-adjusted price × qty + fee).
            # Using raw current_price would under-reserve by the slippage buffer.
            base_asset = symbol.split("/")[0]
            if side == "buy":
                required = amount * fill_price * (1 + self._taker_fee_bps / 10_000)
                if not await self._reserve_balance("USDT", required):
                    raise PaperOrderRejectedError(
                        f"Insufficient USDT balance: need {required:.2f}, have {self._balances.get('USDT', PaperBalance('USDT', 0)).free:.2f}"
                    )
            else:
                if not await self._reserve_balance(base_asset, amount):
                    raise PaperOrderRejectedError(f"Insufficient {base_asset} balance")

            # Check for partial fill
            if PartialFillSimulator.should_partial_fill(
                amount, self._top_of_book_depth
            ):
                return await self._partial_fill_order(
                    symbol, side, amount, fill_price, slippage_bps, lat, idempotency_key
                )

            # Full fill — immediate
            return await self._complete_fill(
                symbol=symbol,
                side=side,
                order_type="market",
                quantity=amount,
                fill_price=fill_price,
                slippage_bps=slippage_bps,
                latency_ms=lat,
                idempotency_key=idempotency_key,
            )

    async def _partial_fill_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: float,
        first_fill_price: float,
        slippage_bps: float,
        latency_ms: float,
        idempotency_key: str,
    ) -> dict:
        """Simulate a partial fill across multiple ticks."""
        schedule = PartialFillSimulator.compute_fill_schedule(
            quantity, self._top_of_book_depth, num_ticks=3
        )
        order_id = idempotency_key or f"paper_{uuid.uuid4().hex[:12]}"

        first_tick = schedule[0]
        remaining = quantity - first_tick

        # Immediate first fill
        first_result = await self._complete_fill(
            symbol=symbol,
            side=side,
            order_type="market",
            quantity=first_tick,
            fill_price=first_fill_price,
            slippage_bps=slippage_bps,
            latency_ms=latency_ms,
            idempotency_key=order_id,
            status="partial",
        )

        # Schedule remaining fills as background tasks
        for i, tick_qty in enumerate(schedule[1:], start=1):
            # Each subsequent tick is slightly worse price
            worsen_bps = i * 2  # 2 bps worse per tick
            if side == "buy":
                tick_price = first_fill_price * (1 + worsen_bps / 10_000)
            else:
                tick_price = first_fill_price * (1 - worsen_bps / 10_000)
            asyncio.create_task(
                self._delayed_partial_fill(
                    order_id, symbol, side, tick_qty, tick_price, i
                )
            )

        return first_result

    async def _delayed_partial_fill(
        self,
        parent_order_id: str,
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: float,
        fill_price: float,
        tick_index: int,
    ) -> None:
        """Execute a delayed partial fill tick."""
        await asyncio.sleep(random.uniform(0.5, 2.0))  # 0.5–2s between ticks
        try:
            async with self._get_order_lock(symbol):
                # Update parent order
                parent = self._orders.get(parent_order_id)
                if parent:
                    parent.filled_quantity += quantity
                    parent.fill_price = (
                        parent.fill_price * (parent.filled_quantity - quantity)
                        + fill_price * quantity
                    ) / parent.filled_quantity
                    parent.updated_at = datetime.now(timezone.utc)
                    if parent.filled_quantity >= parent.quantity:
                        parent.status = "filled"
        except Exception as e:
            log.error(
                "partial_fill_tick_error",
                order_id=parent_order_id,
                tick=tick_index,
                error=str(e),
            )

    # ── Limit Orders ─────────────────────────────────────────────────────────────

    async def place_limit_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        amount: float,
        price: float,
        idempotency_key: str = "",
    ) -> dict:
        """
        Place a limit order. Immediately fills if price condition is already met:
        - BUY limit below current price - fills immediately at current price
        - SELL limit above current price - fills immediately at current price

        Otherwise placed in open orders book and checked on every price update.
        """
        await self._check_downtime()
        lat = await self._simulate_latency()
        current_price = self._get_price(symbol)

        # Check if should fill immediately (limit price already crossed)
        if current_price > 0:
            if side == "buy" and price >= current_price:
                # Buy limit at or above market - fill at current price (not limit price)
                slippage_bps = 0.0  # limit order fills at market price
                return await self._complete_fill(
                    symbol=symbol,
                    side=side,
                    order_type="limit",
                    quantity=amount,
                    fill_price=current_price,
                    slippage_bps=0.0,
                    latency_ms=lat,
                    idempotency_key=idempotency_key,
                )
            elif side == "sell" and price <= current_price:
                return await self._complete_fill(
                    symbol=symbol,
                    side=side,
                    order_type="limit",
                    quantity=amount,
                    fill_price=current_price,
                    slippage_bps=0.0,
                    latency_ms=lat,
                    idempotency_key=idempotency_key,
                )

        # Place as open limit order
        order_id = idempotency_key or f"paper_{uuid.uuid4().hex[:12]}"
        base_asset = symbol.split("/")[0]
        quote_asset = symbol.split("/")[1]

        # Buy side: reserve quote currency before placing the order.
        # Sell side: no pre-reserve needed — _complete_fill deducts base asset from
        # free balance directly (safer for cascading triggers that share a free pool).
        if side == "buy":
            required = amount * price * (1 + self._maker_fee_bps / 10_000)
            if not await self._reserve_balance(quote_asset, required):
                raise PaperOrderRejectedError(
                    f"Insufficient {quote_asset} for limit order"
                )

        order = PaperOrder(
            id=order_id,
            symbol=symbol,
            side=side,
            order_type="limit",
            quantity=amount,
            price=price,
            status="open",
            latency_ms=lat,
        )
        async with self._lock:
            self._orders[order_id] = order
            self._open_limit_orders.append(order)

        log.info(
            "paper_limit_order_placed",
            order_id=order_id,
            symbol=symbol,
            side=side,
            price=price,
            amount=amount,
        )
        return self._parse_order(order)

    async def _check_and_trigger_orders(self, symbol: str) -> None:
        """
        Synchronously evaluate all open orders for a symbol and trigger fills.

        Called immediately on every _update_price call (PRIMARY path).
        The polling monitor is a SAFETY NET — this is the hot path.

        Duplicate fill prevention:
          - Each order is evaluated exactly once per _update_price call.
          - Filled orders are removed from _open_limit_orders BEFORE _complete_fill
            returns, so a second rapid _update_price sees the order gone.
          - The reentrant asyncio.Lock serializes concurrent calls.
        """
        current_price = self._get_price(symbol)
        if current_price <= 0:
            return

        async with self._lock:
            to_remove = []
            for order in list(self._open_limit_orders):
                if order.symbol != symbol or order.status != "open":
                    continue

                # Determine if this order should fill at current_price
                should_fill = False
                if order.order_type in ("stop_loss", "take_profit"):
                    # Sell stop: trigger when price drops TO or BELOW stop_price
                    # Buy stop: trigger when price rises TO or ABOVE stop_price
                    if order.side == "sell" and current_price <= order.stop_price:
                        should_fill = True
                    elif order.side == "buy" and current_price >= order.stop_price:
                        should_fill = True
                elif order.order_type == "limit":
                    # Buy limit fills when price drops TO or BELOW the limit price
                    # Sell limit fills when price rises TO or ABOVE the limit price
                    if order.side == "buy" and current_price <= order.price:
                        should_fill = True
                    elif order.side == "sell" and current_price >= order.price:
                        should_fill = True

                if should_fill:
                    to_remove.append(order)

            # Remove from book BEFORE filling — prevents double-trigger
            # if _update_price is called again before _complete_fill returns.
            # (Also prevents the same order from being evaluated twice in
            # this same loop iteration.)
            for order in to_remove:
                self._open_limit_orders.remove(order)

        # Fills happen AFTER the lock is released.
        # _complete_fill acquires its own lock and is synchronous (no await
        # between acquire and release), so there is no deadlock risk even if
        # _update_price is called concurrently from multiple coroutines.
        for order in to_remove:
            # Use current_price as the fill price — this is the market price
            # at the moment the trigger condition was met. For stop orders,
            # the trigger price is the best available price at the time of
            # the break, which is current_price (not the stale stop_price).
            fill_price = current_price
            # Pre-check sell balance before deducting: skip if insufficient free balance.
            # Buy orders use _reserve_balance at placement, so no check needed here.
            base_asset = order.symbol.split("/")[0]
            if order.side == "sell":
                bal = self._balances.get(base_asset)
                if bal is None or bal.free < order.quantity - 1e-9:
                    log.warning(
                        "skip_fill_insufficient_balance",
                        symbol=order.symbol,
                        side=order.side,
                        free=bal.free if bal else 0,
                        required=order.quantity,
                    )
                    continue
            await self._complete_fill(
                symbol=order.symbol,
                side=order.side,
                order_type=order.order_type,
                quantity=order.quantity,
                fill_price=fill_price,
                slippage_bps=(
                    SLIPPAGE_BASE_BPS
                    if order.order_type in ("stop_loss", "take_profit")
                    else 0.0
                ),
                latency_ms=0.0,
                idempotency_key=order.id,
            )

    # ── Stop Loss / Take Profit ─────────────────────────────────────────────────

    async def place_stop_loss(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        amount: float,
        stop_price: float,
    ) -> dict:
        """Place a stop loss order. Fills immediately if stop price already crossed."""
        await self._check_downtime()
        current_price = self._get_price(symbol)

        if current_price > 0:
            if side == "sell" and current_price <= stop_price:
                return await self._complete_fill(
                    symbol=symbol,
                    side=side,
                    order_type="stop_loss",
                    quantity=amount,
                    fill_price=stop_price,
                    slippage_bps=SLIPPAGE_BASE_BPS,
                    latency_ms=0.0,
                    idempotency_key="",
                )
            elif side == "buy" and current_price >= stop_price:
                return await self._complete_fill(
                    symbol=symbol,
                    side=side,
                    order_type="stop_loss",
                    quantity=amount,
                    fill_price=stop_price,
                    slippage_bps=SLIPPAGE_BASE_BPS,
                    latency_ms=0.0,
                    idempotency_key="",
                )

        order_id = f"paper_{uuid.uuid4().hex[:12]}"
        order = PaperOrder(
            id=order_id,
            symbol=symbol,
            side=side,
            order_type="stop_loss",
            quantity=amount,
            stop_price=stop_price,
            status="open",
        )
        async with self._lock:
            self._orders[order_id] = order
            self._open_limit_orders.append(order)

        return self._parse_order(order)

    async def place_take_profit(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        amount: float,
        take_profit_price: float,
    ) -> dict:
        """Place a take profit order."""
        await self._check_downtime()
        current_price = self._get_price(symbol)

        if current_price > 0:
            if side == "sell" and current_price >= take_profit_price:
                return await self._complete_fill(
                    symbol=symbol,
                    side=side,
                    order_type="take_profit",
                    quantity=amount,
                    fill_price=take_profit_price,
                    slippage_bps=0.0,
                    latency_ms=0.0,
                    idempotency_key="",
                )
            elif side == "buy" and current_price <= take_profit_price:
                return await self._complete_fill(
                    symbol=symbol,
                    side=side,
                    order_type="take_profit",
                    quantity=amount,
                    fill_price=take_profit_price,
                    slippage_bps=0.0,
                    latency_ms=0.0,
                    idempotency_key="",
                )

        order_id = f"paper_{uuid.uuid4().hex[:12]}"
        order = PaperOrder(
            id=order_id,
            symbol=symbol,
            side=side,
            order_type="take_profit",
            quantity=amount,
            stop_price=take_profit_price,
            status="open",
        )
        async with self._lock:
            self._orders[order_id] = order
            self._open_limit_orders.append(order)

        return self._parse_order(order)

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an open limit/stop/TP order."""
        await self._check_downtime()
        lat = await self._simulate_latency()
        await asyncio.sleep(lat / 1000)

        async with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise PaperOrderNotFoundError(f"Order {order_id} not found")

            if order.status not in ("open", "partial"):
                raise PaperExchangeError(
                    f"Cannot cancel order in status: {order.status}"
                )

            order.status = "cancelled"
            order.updated_at = datetime.now(timezone.utc)

            # Release locked balance
            base_asset = symbol.split("/")[0]
            if order.side == "buy":
                quote_asset = symbol.split("/")[1]
                locked_amount = order.quantity * order.price
                await self._release_balance(quote_asset, locked_amount)
            else:
                await self._release_balance(base_asset, order.quantity)

            if order in self._open_limit_orders:
                self._open_limit_orders.remove(order)

        return self._parse_order(order)

    # ── Core Fill Logic ─────────────────────────────────────────────────────────

    async def _complete_fill(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: str,
        quantity: float,
        fill_price: float,
        slippage_bps: float,
        latency_ms: float,
        idempotency_key: str,
        status: str = "filled",
    ) -> dict:
        """Execute a complete (or partial) fill: update balances, record order, compute fees."""
        base_asset = symbol.split("/")[0]
        quote_asset = symbol.split("/")[1]
        order_value = quantity * fill_price
        fee_bps = self._taker_fee_bps if order_type == "market" else self._maker_fee_bps
        fee = order_value * (fee_bps / 10_000)

        async with self._lock:
            if side == "buy":
                # Deduct quote currency (already reserved, now settle)
                bal_quote = self._balances.get(quote_asset)
                if bal_quote:
                    bal_quote.locked = max(
                        0.0, bal_quote.locked - order_value * (1 + fee_bps / 10_000)
                    )
                    bal_quote.free += quantity  # receive base asset
                    if base_asset not in self._balances:
                        self._balances[base_asset] = PaperBalance(
                            asset=base_asset, free=0.0
                        )
                    self._balances[base_asset].free += quantity
            else:
                # Deduct base asset from FREE balance (seller's holdings)
                bal_base = self._balances.get(base_asset)
                if bal_base:
                    bal_base.free -= quantity
                # Receive quote currency
                proceeds = order_value - fee
                if quote_asset not in self._balances:
                    self._balances[quote_asset] = PaperBalance(
                        asset=quote_asset, free=0.0
                    )
                self._balances[quote_asset].free += proceeds

        order_id = idempotency_key or f"paper_{uuid.uuid4().hex[:12]}"
        order = PaperOrder(
            id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            filled_quantity=quantity,
            fill_price=fill_price,
            status=status,
            fee=fee,
            slippage_bps=slippage_bps,
            latency_ms=latency_ms,
        )
        async with self._lock:
            self._orders[order_id] = order

        log.info(
            "paper_order_filled",
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            fill_price=fill_price,
            fee=round(fee, 4),
            slippage_bps=slippage_bps,
        )
        return self._parse_order(order)

    def _parse_order(self, order: PaperOrder) -> dict:
        return {
            "id": order.id,
            "symbol": order.symbol,
            "side": order.side,
            "type": order.order_type,
            "quantity": order.quantity,
            "price": order.price,
            "fill_price": order.fill_price,
            "status": order.status,
            "filled": order.filled_quantity,
            "remaining": max(0.0, order.quantity - order.filled_quantity),
            "timestamp": int(order.created_at.timestamp() * 1000),
            "fee": order.fee,
            "slippage_bps": order.slippage_bps,
            "latency_ms": order.latency_ms,
        }

    # ── Market Data ──────────────────────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> dict:
        await self._check_downtime()
        await self._simulate_latency()
        price = self._get_price(symbol)
        if price <= 0:
            raise PaperExchangeError(f"No price data for {symbol}")
        return {
            "symbol": symbol,
            "bid": price * 0.9998,
            "ask": price * 1.0002,
            "last": price,
            "volume": self._adv * 0.01,  # rough daily volume proxy
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }

    async def get_ohlcv(
        self, symbol: str, timeframe: str = "1m", limit: int = 100
    ) -> list:
        """Return simulated OHLCV data based on current price with random walk."""
        await self._simulate_latency()
        await self._check_downtime()
        price = self._get_price(symbol)
        if price <= 0:
            price = 50_000.0

        candles = []
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        interval_ms = {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "1h": 3_600_000,
        }.get(timeframe, 60_000)

        for i in range(limit):
            t = now_ms - (limit - i - 1) * interval_ms
            # Simple random walk
            change = random.gauss(0, price * 0.001)
            price_sim = max(price + change, price * 0.5)
            o = price_sim * random.uniform(0.998, 1.002)
            h = price_sim * random.uniform(1.000, 1.005)
            l = price_sim * random.uniform(0.995, 1.000)
            c = price_sim
            v = random.uniform(10, 100)
            candles.append(
                {
                    "timestamp": t,
                    "open": round(o, 8),
                    "high": round(h, 8),
                    "low": round(l, 8),
                    "close": round(c, 8),
                    "volume": round(v, 4),
                }
            )

        return candles

    async def get_position(self, symbol: str) -> Optional[dict]:
        """Return current holdings of base asset."""
        await self._simulate_latency()
        base = symbol.split("/")[0]
        bal = self._balances.get(base)
        if not bal or bal.free + bal.locked <= 0:
            return None
        price = self._get_price(symbol)
        return {
            "symbol": symbol,
            "amount": bal.free + bal.locked,
            "entry_price": 0.0,  # paper exchange doesn't track entry price
            "current_price": price,
        }

    async def get_open_orders(self, symbol: str) -> list[dict]:
        await self._simulate_latency()
        return [
            self._parse_order(o)
            for o in self._orders.values()
            if o.symbol == symbol and o.status == "open"
        ]

    async def get_order(self, order_id: str, symbol: str) -> Optional[dict]:
        await self._simulate_latency()
        order = self._orders.get(order_id)
        if not order:
            return None
        return self._parse_order(order)

    # ── State ───────────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def initial_capital(self) -> float:
        return self._initial_capital

    def get_all_balances(self) -> dict:
        return {
            asset: {
                "free": bal.free,
                "locked": bal.locked,
                "total": bal.free + bal.locked,
            }
            for asset, bal in self._balances.items()
        }

    def set_volatility(self, vol: float) -> None:
        """Update volatility for slippage model."""
        self._volatility = vol

    def set_adv(self, adv: float) -> None:
        """Update ADV for slippage model."""
        self._adv = adv

    def reset(self) -> None:
        """Reset all balances and orders. Call between backtest runs."""
        self._balances = {
            "USDT": PaperBalance(asset="USDT", free=self._initial_capital, locked=0.0),
        }
        self._orders.clear()
        self._open_limit_orders.clear()
        self._downtime_sim.clear_downtime()
        log.warning("paper_exchange_reset", initial_capital=self._initial_capital)


# ── Exceptions ─────────────────────────────────────────────────────────────────


class PaperExchangeError(Exception):
    pass


class PaperExchangeDownError(PaperExchangeError):
    """Exchange is in a simulated downtime period."""


class PaperOrderRejectedError(PaperExchangeError):
    """Order rejected due to insufficient balance or other risk limit."""


class PaperOrderNotFoundError(PaperExchangeError):
    pass
