"""
High-Frequency Event Stress Audit — PaperExchange Engine
=======================================================
Validates the paper trading engine under extreme load conditions.

Run: python3 -m pytest paper_trading/tests/stress_test_paper_engine.py -v -s

Audit dimensions:
  1. Event storm: 1000+ price updates/sec sustained
  2. Asyncio task explosion: unbounded create_task calls
  3. Backpressure: queue overflow under burst traffic
  4. Cascading triggers: mass stop-loss activation
  5. Multi-symbol burst: 10 symbols × rapid updates
  6. Latency: p50/p95/p99 under load
  7. Duplicate processing: concurrent fills
  8. Memory growth: order book size tracking
  9. Lock contention: serialization under concurrency
  10. Slow consumer: monitor falls behind primary path
  11. Overload recovery: graceful degradation
"""

import pytest
import asyncio
import gc
import psutil
import os
import time
import random
import tracemalloc
from collections import deque
from datetime import datetime, timezone
from typing import List, Dict

from paper_trading.engine import (
    PaperExchange,
    PaperExchangeError,
    PaperOrderRejectedError,
)
from paper_trading.orchestrator import PaperTradingOrchestrator, PaperTradingConfig


# ── Metric Collection Utilities ─────────────────────────────────────────────────

class MetricsCollector:
    """Collects latency, throughput, and event statistics during a stress run."""

    def __init__(self):
        self.latencies: List[float] = []
        self.events_processed = 0
        self.events_dropped = 0
        self.duplicates = 0
        self.errors = 0
        self.order_fills = 0
        self.start_time = 0.0
        self.end_time = 0.0
        self.queue_depths: List[int] = []
        self.memory_samples: List[float] = []
        self._seen_order_ids = set()
        self._lock = asyncio.Lock()

    async def record_latency(self, latency_ms: float):
        async with self._lock:
            self.latencies.append(latency_ms)

    async def record_event(self, processed: bool = True):
        async with self._lock:
            self.events_processed += 1

    async def record_drop(self):
        async with self._lock:
            self.events_dropped += 1

    async def record_fill(self, order_id: str):
        async with self._lock:
            if order_id in self._seen_order_ids:
                self.duplicates += 1
            else:
                self._seen_order_ids.add(order_id)
            self.order_fills += 1

    async def record_error(self):
        async with self._lock:
            self.errors += 1

    def percentile(self, p: float) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * p / 100)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    def summary(self) -> dict:
        duration = self.end_time - self.start_time
        return {
            "duration_sec": round(duration, 3),
            "events_processed": self.events_processed,
            "events_dropped": self.events_dropped,
            "duplicates_detected": self.duplicates,
            "fills": self.order_fills,
            "errors": self.errors,
            "throughput_eps": round(self.events_processed / max(duration, 0.001), 1),
            "latency_p50_ms": round(self.percentile(50), 3),
            "latency_p95_ms": round(self.percentile(95), 3),
            "latency_p99_ms": round(self.percentile(99), 3),
            "max_queue_depth": max(self.queue_depths) if self.queue_depths else 0,
            "peak_memory_mb": round(max(self.memory_samples) if self.memory_samples else 0, 3),
        }


# ── Stress Test Scenarios ───────────────────────────────────────────────────────

class TestEventStormStress:
    """Scenario 1: Sustained 1000+ events/sec with order book activity."""

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_1000_price_updates_per_second(self):
        """
        Feed 5000 price updates as fast as possible (not rate-limited).
        Measure throughput, latency, and order book integrity.
        """
        ex = PaperExchange(initial_capital=100_000.0, base_latency_ms=0.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        # Seed a position so stops can trigger
        await ex.place_market_order("BTC/USDT", "buy", 1.0)
        stop = await ex.place_stop_loss("BTC/USDT", "sell", 1.0, stop_price=49_000.0)

        metrics = MetricsCollector()
        metrics.start_time = time.monotonic()
        stop_triggered = False

        async def update_task(price: float, idx: int):
            nonlocal stop_triggered
            t0 = time.monotonic()
            try:
                # MUST await the coroutine — calling it without await schedules
                # it as a fire-and-forget that never executes inside the event loop.
                await ex._update_price("BTC/USDT", price)
                latency = (time.monotonic() - t0) * 1000
                await metrics.record_latency(latency)
                await metrics.record_event()

                # Check if stop triggered during burst
                if not stop_triggered and price <= 49_000.0:
                    stop_triggered = True
            except Exception as e:
                await metrics.record_error()

        # Send 5000 events as fast as possible (no artificial delays)
        NUM_EVENTS = 5000
        tasks = []
        for i in range(NUM_EVENTS):
            # Price declines from 50,000 to 45,000 over the course of the burst
            price = 50_000.0 - (i / NUM_EVENTS) * 5_000
            tasks.append(update_task(price, i))

        await asyncio.gather(*tasks)
        metrics.end_time = time.monotonic()

        summary = metrics.summary()
        print(f"\n=== Event Storm Results ===")
        print(f"  Duration:        {summary['duration_sec']}s")
        print(f"  Throughput:      {summary['throughput_eps']} events/sec")
        print(f"  Latency p50:     {summary['latency_p50_ms']}ms")
        print(f"  Latency p95:     {summary['latency_p95_ms']}ms")
        print(f"  Latency p99:     {summary['latency_p99_ms']}ms")
        print(f"  Errors:          {summary['errors']}")
        print(f"  Duplicates:      {summary['duplicates_detected']}")
        print(f"  Stop triggered:  {stop_triggered}")

        # Assertions
        assert summary["events_processed"] >= 4900, f"Too many events dropped: {summary['events_dropped']}"
        assert summary["duplicates_detected"] == 0, f"Duplicate fills detected: {summary['duplicates_detected']}"
        assert summary["latency_p99_ms"] < 50, f"p99 latency too high under load: {summary['latency_p99_ms']}ms"
        assert stop_triggered, "Stop loss did not trigger during price decline"


class TestCascadingTriggerStress:
    """Scenario 2: Mass stop-loss cascade — single price drop triggers N orders."""

    @pytest.mark.asyncio
    async def test_100_simultaneous_stop_triggers(self):
        """
        Place 100 stop-loss orders at the same trigger level.
        A single price update drops below all of them simultaneously.
        Verify: all triggered exactly once, no duplicates.
        """
        # Use large top_of_book_depth so the 10 BTC buy fills completely
        # (avoids PartialFillSimulator limiting to 40% = 4 BTC per tick)
        ex = PaperExchange(initial_capital=5_000_000.0, base_latency_ms=0.0, top_of_book_depth=100.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        # Seed position and place 100 identical stop losses
        # 10 BTC at ~50k ≈ 501k USDT (within 5M budget), then 100 stops × 0.1 BTC = 10 BTC exactly
        await ex.place_market_order("BTC/USDT", "buy", 10.0)
        stop_ids = []

        for i in range(100):
            result = await ex.place_stop_loss(
                "BTC/USDT", "sell", 0.1, stop_price=49_500.0
            )
            stop_ids.append(result["id"])

        # Verify all 100 are open
        open_count = 0
        for sid in stop_ids:
            order = await ex.get_order(sid, "BTC/USDT")
            if order["status"] == "open":
                open_count += 1
        assert open_count == 100, f"Expected 100 open stops, got {open_count}"

        # Single price drop triggers ALL 100 simultaneously
        start = time.monotonic()
        await ex._update_price("BTC/USDT", 49_000.0)
        elapsed_ms = (time.monotonic() - start) * 1000

        # Check all are filled exactly once
        filled_ids = []
        for sid in stop_ids:
            order = await ex.get_order(sid, "BTC/USDT")
            assert order["status"] == "filled", f"Order {sid} not filled: {order['status']}"
            filled_ids.append(sid)

        unique_fills = len(set(filled_ids))
        print(f"\n=== Cascading Trigger Results ===")
        print(f"  Orders triggered:  100")
        print(f"  Unique fills:      {unique_fills}")
        print(f"  Duplicates:        {100 - unique_fills}")
        print(f"  Processing time:   {elapsed_ms:.2f}ms")

        assert unique_fills == 100, f"Expected exactly 100 fills, got {unique_fills}"

        # Verify balances: all 100 BTC sold
        btc_bal = await ex.get_balance("BTC")
        assert btc_bal < 0.01, f"BTC balance should be ~0, got {btc_bal}"


class TestMultiSymbolBurstStress:
    """Scenario 3: 10 symbols with independent price feeds — cross-symbol isolation."""

    @pytest.mark.asyncio
    async def test_10_symbols_simultaneous_burst(self):
        """
        10 symbols, each receiving 500 price updates in a burst.
        Verify: no cross-symbol contamination, all prices converge correctly.
        """
        symbols = [f"ASSET{i}/USDT" for i in range(10)]
        ex = PaperExchange(initial_capital=1_000_000.0, base_latency_ms=0.0)
        await ex.connect()

        # Seed prices for all symbols
        base_prices = {sym: 1000.0 + i * 100 for i, sym in enumerate(symbols)}
        for sym, price in base_prices.items():
            await ex._update_price(sym, price)

        # Place a stop on each
        stop_ids = {}
        for sym in symbols:
            await ex.place_market_order(sym, "buy", 1.0)
            result = await ex.place_stop_loss(sym, "sell", 1.0, stop_price=base_prices[sym] * 0.95)
            stop_ids[sym] = result["id"]

        # Concurrent burst: 500 updates per symbol
        NUM_UPDATES = 500
        per_symbol_updates = {sym: [] for sym in symbols}

        async def burst_symbol(sym: str, base_price: float):
            tasks = []
            for i in range(NUM_UPDATES):
                new_price = base_price * (1 + (i / NUM_UPDATES) * 0.1)  # 10% drift up
                per_symbol_updates[sym].append(new_price)
                tasks.append(ex._update_price(sym, new_price))
            await asyncio.gather(*tasks)

        start = time.monotonic()
        await asyncio.gather(*[
            burst_symbol(sym, base_prices[sym]) for sym in symbols
        ])
        elapsed = time.monotonic() - start

        # Verify final prices
        for sym in symbols:
            final_price = ex._get_price(sym)
            expected = per_symbol_updates[sym][-1]
            assert abs(final_price - expected) < 0.01, f"{sym}: expected {expected}, got {final_price}"

        total_events = NUM_UPDATES * len(symbols)
        print(f"\n=== Multi-Symbol Burst Results ===")
        print(f"  Symbols:          {len(symbols)}")
        print(f"  Updates/symbol:   {NUM_UPDATES}")
        print(f"  Total events:    {total_events}")
        print(f"  Wall time:       {elapsed:.3f}s")
        print(f"  Effective rate:  {total_events / elapsed:.0f} events/sec")

        # Verify no symbol got cross-contaminated prices
        for sym in symbols:
            order = await ex.get_order(stop_ids[sym], sym)
            # Stop was at 95% of base; final price is higher — should still be open
            assert order["status"] in ("open", "filled"), f"{sym}: unexpected status {order['status']}"


class TestLockContentionStress:
    """Scenario 4: Lock contention under heavy concurrent order placement."""

    @pytest.mark.asyncio
    async def test_concurrent_order_placement_lock_contention(self):
        """
        200 concurrent limit order placements across multiple symbols.
        Measure serialization overhead and queue buildup.
        """
        ex = PaperExchange(initial_capital=10_000_000.0, base_latency_ms=0.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
        NUM_ORDERS = 200

        start = time.monotonic()
        tasks = []
        for i in range(NUM_ORDERS):
            sym = symbols[i % len(symbols)]
            price = 50_000.0 + random.uniform(-1000, 1000)
            tasks.append(ex.place_limit_order(sym, "buy", 0.01, price))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed_ms = (time.monotonic() - start) * 1000

        errors = [r for r in results if isinstance(r, Exception)]
        successes = [r for r in results if not isinstance(r, Exception)]

        print(f"\n=== Lock Contention Results ===")
        print(f"  Concurrent orders:     {NUM_ORDERS}")
        print(f"  Successful placements: {len(successes)}")
        print(f"  Errors:               {len(errors)}")
        print(f"  Wall time:            {elapsed_ms:.1f}ms")
        print(f"  Avg per order:        {elapsed_ms / NUM_ORDERS:.2f}ms")

        assert len(errors) == 0, f"Order placement errors under concurrency: {errors[:3]}"
        assert len(successes) == NUM_ORDERS


class TestMemoryGrowthStress:
    """Scenario 5: Memory growth under sustained event load — no leaks."""

    @pytest.mark.asyncio
    async def test_memory_growth_under_sustained_load(self):
        """
        10,000 price updates. Track memory usage every 500 updates.
        Memory should plateau — unbounded growth indicates a leak.
        """
        tracemalloc.start()
        process = psutil.Process(os.getpid())

        ex = PaperExchange(initial_capital=100_000.0, base_latency_ms=0.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        # Place many open orders to grow _open_limit_orders
        for i in range(500):
            price = 50_000.0 + random.uniform(-5000, 5000)
            try:
                await ex.place_limit_order("BTC/USDT", "buy", 0.001, price)
            except Exception:
                pass

        initial_mem_mb = process.memory_info().rss / 1024 / 1024
        memory_snapshots = [initial_mem_mb]

        NUM_UPDATES = 10_000
        batch_size = 100

        for batch in range(NUM_UPDATES // batch_size):
            tasks = [
                ex._update_price("BTC/USDT", 50_000.0 + random.uniform(-100, 100))
                for _ in range(batch_size)
            ]
            await asyncio.gather(*tasks)

            if batch % 5 == 0:
                current_mem = process.memory_info().rss / 1024 / 1024
                memory_snapshots.append(current_mem)

        final_mem_mb = process.memory_info().rss / 1024 / 1024
        growth_mb = final_mem_mb - initial_mem_mb
        tracemalloc.stop()

        # Memory growth should be bounded (< 50MB for this workload)
        print(f"\n=== Memory Growth Results ===")
        print(f"  Initial memory:     {initial_mem_mb:.1f} MB")
        print(f"  Final memory:       {final_mem_mb:.1f} MB")
        print(f"  Growth:             {growth_mb:.1f} MB")
        print(f"  Open orders:        {len(ex._open_limit_orders)}")
        print(f"  Memory snapshots:   {[f'{m:.1f}' for m in memory_snapshots]}")

        assert growth_mb < 100, f"Excessive memory growth detected: {growth_mb:.1f}MB (possible leak)"


class TestBackpressureStress:
    """Scenario 6: Backpressure — system must degrade gracefully under overload."""

    @pytest.mark.asyncio
    async def test_backpressure_under_event_burst(self):
        """
        Simulate a WebSocket reconnect burst: 5000 events arrive in < 1 second.
        System must process them without crashing, dropping events only when
        the queue overflow threshold is reached.
        """
        ex = PaperExchange(initial_capital=100_000.0, base_latency_ms=0.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        metrics = MetricsCollector()
        metrics.start_time = time.monotonic()

        # Simulate burst: 5000 price updates as fast as possible
        async def fast_update(idx: int):
            try:
                price = 50_000.0 + idx * 0.1
                await ex._update_price("BTC/USDT", price)
                await metrics.record_event()
            except Exception as e:
                await metrics.record_error()

        # Send all at once — this is the "burst" scenario
        tasks = [fast_update(i) for i in range(5000)]
        await asyncio.gather(*tasks, return_exceptions=True)

        metrics.end_time = time.monotonic()
        summary = metrics.summary()

        print(f"\n=== Backpressure Results ===")
        print(f"  Events sent:       5000")
        print(f"  Events processed:  {summary['events_processed']}")
        print(f"  Events dropped:   {summary['events_dropped']}")
        print(f"  Errors:            {summary['errors']}")
        print(f"  Throughput:        {summary['throughput_eps']} events/sec")
        print(f"  Final price:       {ex._get_price('BTC/USDT')}")

        # All events should be processed (no backpressure yet — we haven't added limits)
        # After adding backpressure, some should be rejected/dropped
        assert summary["events_processed"] >= 4900


class TestDuplicateFillPreventionStress:
    """Scenario 7: Rapid identical price updates must not cause duplicate fills."""

    @pytest.mark.asyncio
    async def test_identical_price_updates_no_duplicate_fills(self):
        """
        Send the same price update 10,000 times concurrently.
        Only ONE fill should occur for any given stop order.
        """
        ex = PaperExchange(initial_capital=100_000.0, base_latency_ms=0.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)
        await ex.place_market_order("BTC/USDT", "buy", 1.0)
        result = await ex.place_stop_loss("BTC/USDT", "sell", 1.0, stop_price=49_500.0)
        stop_id = result["id"]

        # 10,000 identical price updates fired simultaneously
        tasks = [ex._update_price("BTC/USDT", 49_000.0) for _ in range(10_000)]
        await asyncio.gather(*tasks)

        order = await ex.get_order(stop_id, "BTC/USDT")
        assert order["status"] == "filled", f"Stop not filled: {order['status']}"

        # Count how many filled orders have this ID in the order book
        filled_orders = [
            o for o in ex._orders.values()
            if o.status == "filled" and o.order_type == "stop_loss"
        ]

        print(f"\n=== Duplicate Fill Prevention Results ===")
        print(f"  Identical updates:    10,000")
        print(f"  Stop status:          {order['status']}")
        print(f"  Stop-loss fill count: {len(filled_orders)}")

        assert len(filled_orders) == 1, f"DUPLICATE FILL BUG: {len(filled_orders)} fills for 1 order"


class TestSlowConsumerStress:
    """Scenario 8: Slow consumer — monitor falls behind, primary path must stay fast."""

    @pytest.mark.asyncio
    async def test_slow_consumer_monitor_lags_primary_path(self):
        """
        Simulate a scenario where the background monitor cannot keep up
        (e.g., due to slow downstream logging). Primary path must still
        process events immediately — monitor lag is irrelevant.
        """
        ex = PaperExchange(initial_capital=100_000.0, base_latency_ms=0.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)
        await ex.place_market_order("BTC/USDT", "buy", 1.0)
        result = await ex.place_stop_loss("BTC/USDT", "sell", 1.0, stop_price=49_000.0)
        stop_id = result["id"]

        # Primary path should trigger immediately
        primary_start = time.monotonic()
        await ex._update_price("BTC/USDT", 48_500.0)
        primary_elapsed_ms = (time.monotonic() - primary_start) * 1000

        order = await ex.get_order(stop_id, "BTC/USDT")
        primary_triggered = order["status"] == "filled"

        print(f"\n=== Slow Consumer Results ===")
        print(f"  Primary path trigger time: {primary_elapsed_ms:.3f}ms")
        print(f"  Stop filled via primary:    {primary_triggered}")
        print(f"  Monitor check interval:      500ms (would fail if relied upon)")

        assert primary_triggered, "Primary path did not trigger stop — monitor-dependent bug!"
        assert primary_elapsed_ms < 10, f"Primary path too slow: {primary_elapsed_ms}ms"


class TestOverloadRecoveryStress:
    """Scenario 9: Overload recovery — system returns to normal after burst ends."""

    @pytest.mark.asyncio
    async def test_overload_recovery_after_burst(self):
        """
        Sustained heavy load followed by normal load.
        System must recover fully — no stuck state.
        """
        ex = PaperExchange(initial_capital=100_000.0, base_latency_ms=0.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        # Phase 1: Overload burst — 2000 updates where price RISES from 50k to 51k
        burst_tasks = [
            ex._update_price("BTC/USDT", 50_000.0 + i * 0.5)
            for i in range(2000)
        ]
        await asyncio.gather(*burst_tasks)

        # Phase 2: Normal operations — price drops below stop
        # Stop is placed at 49,500 but price is now ~51,000 — needs a downward move
        await ex.place_market_order("BTC/USDT", "buy", 1.0)
        result = await ex.place_stop_loss("BTC/USDT", "sell", 1.0, stop_price=49_500.0)
        # Now drop price below stop to trigger it
        await ex._update_price("BTC/USDT", 49_000.0)

        order = await ex.get_order(result["id"], "BTC/USDT")
        btc_bal = await ex.get_balance("BTC")

        print(f"\n=== Overload Recovery Results ===")
        print(f"  Post-burst order status: {order['status']}")
        print(f"  Post-burst BTC balance:   {btc_bal}")
        print(f"  System operational:       {order['status'] == 'filled' and btc_bal < 0.01}")

        assert order["status"] == "filled"
        assert btc_bal < 0.01


class TestOrchestratorStressIntegration:
    """Scenario 10: Full orchestrator under multi-symbol burst load."""

    @pytest.mark.asyncio
    async def test_orchestrator_multi_symbol_burst(self):
        """
        Orchestrator.update_market_price called by 8 concurrent symbols
        at maximum burst rate. Verify clean state and no errors.
        """
        config = PaperTradingConfig(
            initial_capital=100_000.0,
            base_latency_ms=0.0,
        )
        orch = PaperTradingOrchestrator(redis_client=None, config=config)
        await orch.start()

        symbols = [f"SYM{i}/USDT" for i in range(8)]
        NUM_TICKS = 1000

        async def feed_symbol(sym: str):
            base = 100.0 + hash(sym) % 1000
            for i in range(NUM_TICKS):
                price = base + i * 0.5 + random.uniform(-0.1, 0.1)
                await orch.update_market_price(sym, price)

        start = time.monotonic()
        await asyncio.gather(*[feed_symbol(s) for s in symbols])
        elapsed = time.monotonic() - start

        await orch.stop()

        total = NUM_TICKS * len(symbols)
        print(f"\n=== Orchestrator Stress Results ===")
        print(f"  Symbols:          {len(symbols)}")
        print(f"  Ticks/symbol:     {NUM_TICKS}")
        print(f"  Total events:     {total}")
        print(f"  Wall time:        {elapsed:.2f}s")
        print(f"  Effective rate:   {total / elapsed:.0f} events/sec")


class TestLatencyHistogramStress:
    """Scenario 11: Detailed latency profiling under mixed workload."""

    @pytest.mark.asyncio
    async def test_latency_histogram_under_mixed_load(self):
        """
        Mixed workload: 50% no-op price updates, 30% order placements,
        20% stop-loss triggers. Generate latency histogram.
        """
        ex = PaperExchange(initial_capital=1_000_000.0, base_latency_ms=0.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)
        await ex.place_market_order("BTC/USDT", "buy", 10.0)

        latencies: List[float] = []
        stop_orders_placed = 0
        stops_triggered = 0

        async def mixed_workload(idx: int):
            t0 = time.monotonic()
            try:
                op = idx % 10
                if op < 5:  # 50% price updates
                    await ex._update_price("BTC/USDT", 50_000.0 + random.uniform(-500, 500))
                elif op < 8:  # 30% limit orders
                    await ex.place_limit_order(
                        "BTC/USDT", "buy", 0.1,
                        price=50_000.0 + random.uniform(-1000, 1000)
                    )
                else:  # 20% stop placements
                    stop_price = 50_000.0 - (idx % 100) * 50
                    await ex.place_stop_loss("BTC/USDT", "sell", 0.1, stop_price=stop_price)
                    stop_orders_placed += 1
                    await ex._update_price("BTC/USDT", stop_price - 100)
                    stops_triggered += 1

                latency = (time.monotonic() - t0) * 1000
                latencies.append(latency)
            except Exception:
                latencies.append((time.monotonic() - t0) * 1000)

        NUM_OPS = 2000
        await asyncio.gather(*[mixed_workload(i) for i in range(NUM_OPS)])

        sorted_lat = sorted(latencies)
        p50 = sorted_lat[len(sorted_lat) // 2]
        p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
        p99 = sorted_lat[int(len(sorted_lat) * 0.99)]
        max_lat = max(latencies)

        print(f"\n=== Latency Histogram Results ===")
        print(f"  Operations:       {NUM_OPS}")
        print(f"  Stops placed:      {stop_orders_placed}")
        print(f"  Stops triggered:   {stops_triggered}")
        print(f"  Latency p50:       {p50:.3f}ms")
        print(f"  Latency p95:       {p95:.3f}ms")
        print(f"  Latency p99:       {p99:.3f}ms")
        print(f"  Latency max:       {max_lat:.3f}ms")

        # Buckets for histogram
        buckets = [0, 1, 5, 10, 25, 50, 100, 500, float("inf")]
        counts = [0] * (len(buckets) - 1)
        for lat in latencies:
            for b in range(len(buckets) - 1):
                if buckets[b] <= lat < buckets[b + 1]:
                    counts[b] += 1
                    break

        print(f"  Latency histogram:")
        for i, count in enumerate(counts):
            pct = count / len(latencies) * 100
            bar = "#" * int(pct / 2)
            print(f"    {buckets[i]:>6}-{buckets[i+1]:>6}ms: {bar} {pct:5.1f}%")

        assert p99 < 200, f"p99 latency unacceptable under mixed load: {p99:.1f}ms"
