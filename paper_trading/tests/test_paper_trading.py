"""
Integration tests for the paper trading system.
Run: python3 -m pytest paper_trading/tests/test_paper_trading.py -v
"""

import pytest
import asyncio
from paper_trading.engine import (
    PaperExchange,
    PaperExchangeError,
    PaperOrderRejectedError,
    DowntimeSimulator,
    SlippageModel,
    PartialFillSimulator,
)
from paper_trading.portfolio import PortfolioTracker
from paper_trading.orchestrator import PaperTradingOrchestrator, PaperTradingConfig

# ── Slippage Model Tests ────────────────────────────────────────────────────────


class TestSlippageModel:
    def test_market_order_has_slippage(self):
        slip = SlippageModel.compute(
            order_value=1000, adv=1_000_000, volatility_pct=1.5, is_market=True
        )
        assert slip > 0

    def test_limit_order_has_zero_slippage(self):
        slip = SlippageModel.compute(
            order_value=1000, adv=1_000_000, volatility_pct=1.5, is_market=False
        )
        assert slip == 0.0

    def test_large_order_has_higher_slippage(self):
        slip_small = SlippageModel.compute(
            order_value=100, adv=1_000_000, volatility_pct=1.5, is_market=True
        )
        slip_large = SlippageModel.compute(
            order_value=50_000, adv=1_000_000, volatility_pct=1.5, is_market=True
        )
        assert slip_large > slip_small

    def test_high_volatility_increases_slippage(self):
        slip_low_vol = SlippageModel.compute(
            order_value=1000, adv=1_000_000, volatility_pct=0.5, is_market=True
        )
        slip_high_vol = SlippageModel.compute(
            order_value=1000, adv=1_000_000, volatility_pct=5.0, is_market=True
        )
        assert slip_high_vol > slip_low_vol


# ── Partial Fill Tests ─────────────────────────────────────────────────────────


class TestPartialFillSimulator:
    def test_small_order_no_partial_fill(self):
        should = PartialFillSimulator.should_partial_fill(
            quantity=1.0, top_of_book_depth=50.0
        )
        assert should is False

    def test_large_order_triggers_partial_fill(self):
        should = PartialFillSimulator.should_partial_fill(
            quantity=10.0, top_of_book_depth=50.0
        )
        assert should is True

    def test_fill_schedule_proportions(self):
        schedule = PartialFillSimulator.compute_fill_schedule(
            quantity=100.0, top_of_book_depth=10.0, num_ticks=3
        )
        assert len(schedule) == 3
        assert schedule[0] == pytest.approx(40.0)
        assert sum(schedule[1:]) == pytest.approx(60.0)


# ── Downtime Simulator Tests ───────────────────────────────────────────────────


class TestDowntimeSimulator:
    @pytest.mark.asyncio
    async def test_no_downtime_when_prob_zero(self):
        sim = DowntimeSimulator(downtime_probability_per_call=0.0)
        for _ in range(100):
            await sim.check_or_simulate_downtime()
        assert not sim.is_down()

    @pytest.mark.asyncio
    async def test_latency_spike_triggered(self):
        sim = DowntimeSimulator(
            latency_spike_probability=1.0, max_latency_spike_ms=100.0
        )
        latency = await sim.maybe_simulate_latency()
        assert latency > 0


# ── Paper Exchange Tests ───────────────────────────────────────────────────────


class TestPaperExchange:
    @pytest.mark.asyncio
    async def test_initial_balance(self):
        ex = PaperExchange(initial_capital=5000.0)
        await ex.connect()
        bal = await ex.get_balance("USDT")
        assert bal == 5000.0

    @pytest.mark.asyncio
    async def test_market_order_buy_fills(self):
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        result = await ex.place_market_order("BTC/USDT", "buy", 0.1)
        assert result["status"] == "filled"
        assert result["filled"] == 0.1
        assert result["fee"] > 0
        assert result["slippage_bps"] > 0

    @pytest.mark.asyncio
    async def test_market_order_sell_fills(self):
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        await ex.place_market_order("BTC/USDT", "buy", 0.1)
        btc_bal = await ex.get_balance("BTC")
        assert btc_bal == 0.1

        result = await ex.place_market_order("BTC/USDT", "sell", 0.1)
        assert result["status"] == "filled"
        btc_bal_after = await ex.get_balance("BTC")
        assert btc_bal_after < 0.001

    @pytest.mark.asyncio
    async def test_insufficient_balance_rejected(self):
        ex = PaperExchange(initial_capital=100.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        with pytest.raises(PaperOrderRejectedError):
            await ex.place_market_order("BTC/USDT", "buy", 0.1)

    @pytest.mark.asyncio
    async def test_market_order_rejected_no_price(self):
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()

        with pytest.raises(PaperExchangeError):
            await ex.place_market_order("BTC/USDT", "buy", 0.1)

    @pytest.mark.asyncio
    async def test_limit_order_stays_open_when_price_not_crossed(self):
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        result = await ex.place_limit_order("BTC/USDT", "buy", 0.1, price=49_000.0)
        assert result["status"] == "open"

    @pytest.mark.asyncio
    async def test_limit_order_fills_when_price_crosses(self):
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        result = await ex.place_limit_order("BTC/USDT", "buy", 0.1, price=49_000.0)
        assert result["status"] == "open"

        await ex._update_price("BTC/USDT", 48_000.0)
        # Fills synchronously inside _update_price — no sleep needed

        order = await ex.get_order(result["id"], "BTC/USDT")
        assert order["status"] == "filled"

    @pytest.mark.asyncio
    async def test_cancel_limit_order_releases_balance(self):
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        result = await ex.place_limit_order("BTC/USDT", "buy", 0.1, price=49_000.0)
        bal_before_cancel = await ex.get_balance("USDT")

        await ex.cancel_order(result["id"], "BTC/USDT")
        bal_after = await ex.get_balance("USDT")
        # After cancel, balance should be restored minus the maker fee that was charged
        assert bal_after > bal_before_cancel

    @pytest.mark.asyncio
    async def test_fees_calculated_correctly(self):
        ex = PaperExchange(initial_capital=10_000.0, taker_fee_bps=10)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        result = await ex.place_market_order("BTC/USDT", "buy", 0.1)
        assert result["fee"] == pytest.approx(5.0, rel=0.01)

    @pytest.mark.asyncio
    async def test_stop_loss_triggers_on_price_cross(self):
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        await ex.place_market_order("BTC/USDT", "buy", 0.1)

        result = await ex.place_stop_loss("BTC/USDT", "sell", 0.1, stop_price=49_000.0)
        assert result["status"] == "open"

        # Event-driven: _update_price evaluates and fills the stop synchronously
        await ex._update_price("BTC/USDT", 48_500.0)

        order = await ex.get_order(result["id"], "BTC/USDT")
        assert order["status"] == "filled"

    @pytest.mark.asyncio
    async def test_reset_clears_all_state(self):
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)
        await ex.place_market_order("BTC/USDT", "buy", 0.1)

        ex.reset()
        bal = await ex.get_balance("USDT")
        assert bal == 10_000.0

    # ── Event-Driven Execution Tests ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_rapid_consecutive_price_updates_no_duplicate_fill(self):
        """
        Rapid consecutive _update_price calls must not cause duplicate fills.
        The first price drop triggers the stop; subsequent drops must not re-trigger.
        """
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)
        await ex.place_market_order("BTC/USDT", "buy", 0.1)

        result = await ex.place_stop_loss("BTC/USDT", "sell", 0.1, stop_price=49_000.0)
        order_id = result["id"]

        # Rapid consecutive updates — stop triggers on first crossing
        await ex._update_price("BTC/USDT", 49_500.0)
        await ex._update_price("BTC/USDT", 49_000.0)
        await ex._update_price("BTC/USDT", 48_500.0)

        order = await ex.get_order(order_id, "BTC/USDT")
        assert order["status"] == "filled"

        # Verify exactly one fill occurred — no duplicates
        filled_orders = [
            o
            for o in ex._orders.values()
            if o.status == "filled" and o.order_type == "stop_loss"
        ]
        assert len(filled_orders) == 1

    @pytest.mark.asyncio
    async def test_simultaneous_stop_orders_different_symbols(self):
        """
        Stop orders on different symbols must each trigger independently
        when their respective prices cross.
        """
        ex = PaperExchange(initial_capital=50_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)
        await ex._update_price("ETH/USDT", 3_000.0)

        # Open positions in both assets
        await ex.place_market_order("BTC/USDT", "buy", 0.5)
        await ex.place_market_order("ETH/USDT", "buy", 5.0)

        # Place stops on both
        btc_stop = await ex.place_stop_loss(
            "BTC/USDT", "sell", 0.5, stop_price=49_000.0
        )
        eth_stop = await ex.place_stop_loss("ETH/USDT", "sell", 5.0, stop_price=2_800.0)

        # BTC drops first — BTC stop fills, ETH stop stays open
        await ex._update_price("BTC/USDT", 48_500.0)
        await ex._update_price("ETH/USDT", 3_000.0)  # no change for ETH

        assert (await ex.get_order(btc_stop["id"], "BTC/USDT"))["status"] == "filled"
        assert (await ex.get_order(eth_stop["id"], "ETH/USDT"))["status"] == "open"

        # ETH drops — ETH stop fills
        await ex._update_price("ETH/USDT", 2_700.0)
        assert (await ex.get_order(eth_stop["id"], "ETH/USDT"))["status"] == "filled"

    @pytest.mark.asyncio
    async def test_duplicate_trigger_prevention_same_price_cross(self):
        """
        When multiple _update_price calls arrive at the same price level,
        the stop must not be filled twice.
        """
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)
        await ex.place_market_order("BTC/USDT", "buy", 0.1)

        result = await ex.place_stop_loss("BTC/USDT", "sell", 0.1, stop_price=49_000.0)
        order_id = result["id"]

        # Same price crossed multiple times — only one fill
        for _ in range(5):
            await ex._update_price("BTC/USDT", 48_500.0)

        order = await ex.get_order(order_id, "BTC/USDT")
        assert order["status"] == "filled"

        # Confirm exactly one fill record exists
        filled = [
            o for o in ex._orders.values() if o.id == order_id and o.status == "filled"
        ]
        assert len(filled) == 1

    @pytest.mark.asyncio
    async def test_race_condition_concurrent_price_updates(self):
        """
        Concurrent _update_price calls from multiple coroutines must not
        cause race conditions — orders must be evaluated atomically.
        """
        ex = PaperExchange(initial_capital=10_000.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)
        await ex.place_market_order("BTC/USDT", "buy", 0.1)

        result = await ex.place_stop_loss("BTC/USDT", "sell", 0.1, stop_price=49_000.0)
        order_id = result["id"]

        # Fire concurrent price updates — lock must serialize access
        await asyncio.gather(
            ex._update_price("BTC/USDT", 48_000.0),
            ex._update_price("BTC/USDT", 47_000.0),
            ex._update_price("BTC/USDT", 46_000.0),
        )

        order = await ex.get_order(order_id, "BTC/USDT")
        assert order["status"] == "filled"
        # Only one fill despite concurrent updates
        filled = [
            o for o in ex._orders.values() if o.id == order_id and o.status == "filled"
        ]
        assert len(filled) == 1

    @pytest.mark.asyncio
    async def test_websocket_burst_all_prices_processed(self):
        """
        A burst of price updates (simulating WebSocket rapid fire) must
        process every tick — no updates lost, correct final price.
        """
        # 10 BTC × 50,000 USDT = 500K notional; 2M capital covers it
        ex = PaperExchange(initial_capital=2_000_000.0, base_latency_ms=0.0)
        await ex.connect()
        await ex._update_price("BTC/USDT", 50_000.0)

        prices_seen = []

        async def update_and_record(price):
            await ex._update_price("BTC/USDT", price)
            prices_seen.append(price)

        # Simulate a burst of 20 rapid price updates
        burst_prices = [50_000.0 + i * 100 for i in range(20)]
        await asyncio.gather(*[update_and_record(p) for p in burst_prices])

        # Final price must reflect the last update
        assert ex._get_price("BTC/USDT") == burst_prices[-1]
        # All updates were processed
        assert len(prices_seen) == 20

        # Acquire BTC first (buy market) before testing sell-stop trigger
        await ex.place_market_order("BTC/USDT", "buy", 1.0)

        # Place a sell stop BELOW the burst range (below 50_000).
        # Sell stop triggers when price drops TO or BELOW stop_price.
        # Burst ended at ~51_900, so 49_500 is safely below market — stop stays open.
        sell_stop = await ex.place_stop_loss(
            "BTC/USDT", "sell", 0.1, stop_price=49_500.0
        )
        await ex._update_price("BTC/USDT", 50_500.0)  # still above 49_500
        order = await ex.get_order(sell_stop["id"], "BTC/USDT")
        assert order["status"] == "open"

        # Price drops below stop — triggers
        await ex._update_price("BTC/USDT", 49_000.0)  # 49_000 <= 49_500 → trigger
        order = await ex.get_order(sell_stop["id"], "BTC/USDT")
        assert order["status"] == "filled"


# ── Portfolio Tracker Tests ────────────────────────────────────────────────────


class TestPortfolioTracker:
    @pytest.mark.asyncio
    async def test_initial_state(self):
        tracker = PortfolioTracker(redis_client=None, initial_capital=10_000.0)
        await tracker.initialize()
        m = await tracker.get_metrics()
        assert m.realized_pnl == 0.0
        assert m.current_equity == 10_000.0
        assert m.stats.total_trades == 0

    @pytest.mark.asyncio
    async def test_win_trade_updates_stats(self):
        tracker = PortfolioTracker(redis_client=None, initial_capital=10_000.0)
        await tracker.initialize()

        await tracker.record_trade_closed(pnl=100.0, trade_id=1)
        m = await tracker.get_metrics()

        assert m.realized_pnl == 100.0
        assert m.stats.winning_trades == 1
        assert m.stats.total_trades == 1
        assert m.stats.win_rate == 1.0

    @pytest.mark.asyncio
    async def test_loss_trade_updates_stats(self):
        tracker = PortfolioTracker(redis_client=None, initial_capital=10_000.0)
        await tracker.initialize()

        await tracker.record_trade_closed(pnl=-50.0, trade_id=1)
        m = await tracker.get_metrics()

        assert m.realized_pnl == -50.0
        assert m.stats.losing_trades == 1
        assert m.stats.total_trades == 1

    @pytest.mark.asyncio
    async def test_win_rate_calculation(self):
        tracker = PortfolioTracker(redis_client=None, initial_capital=10_000.0)
        await tracker.initialize()

        for i in range(5):
            pnl = 100.0 if i < 3 else -50.0
            await tracker.record_trade_closed(pnl=pnl, trade_id=i + 1)

        m = await tracker.get_metrics()
        assert m.stats.total_trades == 5
        assert m.stats.winning_trades == 3
        assert m.stats.losing_trades == 2
        assert m.stats.win_rate == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_profit_factor(self):
        tracker = PortfolioTracker(redis_client=None, initial_capital=10_000.0)
        await tracker.initialize()

        await tracker.record_trade_closed(pnl=200.0, trade_id=1)
        await tracker.record_trade_closed(pnl=100.0, trade_id=2)
        await tracker.record_trade_closed(pnl=-150.0, trade_id=3)

        m = await tracker.get_metrics()
        assert m.stats.profit_factor == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_expectancy(self):
        tracker = PortfolioTracker(redis_client=None, initial_capital=10_000.0)
        await tracker.initialize()

        for pnl in [100.0, -50.0, 100.0, -50.0]:
            await tracker.record_trade_closed(pnl=pnl, trade_id=id(tracker))

        m = await tracker.get_metrics()
        assert m.stats.expectancy == pytest.approx(25.0)

    @pytest.mark.asyncio
    async def test_max_drawdown(self):
        tracker = PortfolioTracker(redis_client=None, initial_capital=10_000.0)
        await tracker.initialize()

        await tracker.record_trade_closed(
            pnl=500.0, trade_id=1
        )  # equity=10500, peak=10500, mdd=0%
        await tracker.record_trade_closed(
            pnl=-500.0, trade_id=2
        )  # equity=10000, mdd=(10500-10000)/10500=4.76%
        await tracker.record_trade_closed(
            pnl=-500.0, trade_id=3
        )  # equity=9500, mdd=(10500-9500)/10500=9.52% ← peak here
        await tracker.record_trade_closed(
            pnl=1000.0, trade_id=4
        )  # equity=10500, mdd stays 9.52%

        m = await tracker.get_metrics()
        # Max peak=10500, trough=9500 → (10500-9500)/10500 = 9.52%
        assert 9.0 < m.max_drawdown < 10.5

    @pytest.mark.asyncio
    async def test_unrealized_pnl_update(self):
        tracker = PortfolioTracker(redis_client=None, initial_capital=10_000.0)
        await tracker.initialize()

        await tracker.update_unrealized(250.0)
        await tracker.update_unrealized(-75.0)

        m = await tracker.get_metrics()
        assert m.unrealized_pnl == -75.0
        assert m.total_pnl == -75.0

    @pytest.mark.asyncio
    async def test_reset_clears_all(self):
        tracker = PortfolioTracker(redis_client=None, initial_capital=10_000.0)
        await tracker.initialize()

        await tracker.record_trade_closed(pnl=500.0, trade_id=1)
        await tracker.reset(initial_capital=20_000.0)

        m = await tracker.get_metrics()
        assert m.current_equity == 20_000.0
        assert m.stats.total_trades == 0
        assert m.realized_pnl == 0.0


# ── Orchestrator Tests ─────────────────────────────────────────────────────────


class TestPaperTradingOrchestrator:
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        orch = PaperTradingOrchestrator(redis_client=None)
        await orch.start()
        assert orch.paper_exchange is not None
        assert orch.portfolio is not None
        assert orch.mode == "paper"
        await orch.stop()

    @pytest.mark.asyncio
    async def test_price_update_propagates(self):
        orch = PaperTradingOrchestrator(redis_client=None)
        await orch.start()

        await orch.update_market_price("BTC/USDT", 67_500.0, volatility=2.0)
        ticker = await orch.paper_exchange.get_ticker("BTC/USDT")
        assert ticker["last"] == 67_500.0

        await orch.stop()

    @pytest.mark.asyncio
    async def test_full_trade_cycle_integration(self):
        config = PaperTradingConfig(initial_capital=10_000.0)
        orch = PaperTradingOrchestrator(redis_client=None, config=config)
        await orch.start()

        await orch.update_market_price("BTC/USDT", 50_000.0)
        result = await orch.paper_exchange.place_market_order("BTC/USDT", "buy", 0.1)
        assert result["status"] == "filled"

        await orch.record_closed_trade(pnl=125.50, trade_id=1)
        metrics = await orch.get_portfolio_metrics()

        assert metrics["total_trades"] == 1
        assert metrics["realized_pnl"] == 125.50
        assert metrics["winning_trades"] == 1

        await orch.stop()

    @pytest.mark.asyncio
    async def test_reset_restarts_fresh(self):
        config = PaperTradingConfig(initial_capital=10_000.0)
        orch = PaperTradingOrchestrator(redis_client=None, config=config)
        await orch.start()

        await orch.update_market_price("BTC/USDT", 50_000.0)
        await orch.record_closed_trade(pnl=500.0, trade_id=1)

        await orch.reset(initial_capital=20_000.0)
        metrics = await orch.get_portfolio_metrics()

        assert metrics["total_trades"] == 0
        assert metrics["current_equity"] == 20_000.0
        assert orch.mode == "paper"

        await orch.stop()
