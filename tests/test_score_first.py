"""Idle cycles do not call Kalshi. A 99c cash-out line is replaced at 94c."""

from __future__ import annotations

import time

import pytest

from src.strategies.tennis_set3 import SELL_ALL_AT, TennisSet3Strategy
from tests.test_tennis_safety import (
    ALCARAZ,
    FakeKalshi,
    FakeSofa,
    _alcaraz_event,
    _live_alcaraz,
    _manage_stats,
    _strategy,
)


def _fresh_empty(strategy: TennisSet3Strategy) -> None:
    strategy._book_positions = []
    strategy._book_orders = []
    strategy._book_synced_at = time.monotonic()


@pytest.mark.asyncio
async def test_idle_cycle_makes_no_kalshi_calls():
    client = FakeKalshi()
    strategy = _strategy(client, [])
    _fresh_empty(strategy)
    stats = await strategy.run(live_matches=[])
    assert stats["kalshi_calls"] == 0
    assert stats["actionable_matches"] == 0
    assert client.placed == []


@pytest.mark.asyncio
async def test_open_position_polls_only_that_market():
    client = FakeKalshi()
    client.events = [_alcaraz_event(ask="0.80", bid="0.78")]
    strategy = _strategy(client, [])
    strategy._book_positions = [{
        "ticker": ALCARAZ,
        "position_fp": "2.00",
        "market_exposure_dollars": "1.60",
    }]
    strategy._book_orders = [{
        "order_id": "tp94",
        "ticker": ALCARAZ,
        "status": "resting",
        "action": "sell",
        "side": "yes",
        "yes_price_dollars": "0.94",
        "remaining_count_fp": "2.00",
    }]
    strategy._book_synced_at = time.monotonic()
    stats = await strategy.run(live_matches=[])
    assert stats["kalshi_calls"] == 1
    assert strategy.client.calls == ["get_market"]
    assert client.placed == []


@pytest.mark.asyncio
async def test_actionable_match_fetches_balance_and_market_not_positions():
    client = FakeKalshi()
    client.balance = {"balance": 82, "portfolio_value": 0}
    client.events = [_alcaraz_event()]
    strategy = _strategy(client, [_live_alcaraz()])
    _fresh_empty(strategy)
    stats = await strategy.run(live_matches=[_live_alcaraz()])
    calls = list(strategy.client.calls)
    first_order = calls.index("place_order")
    before_order = calls[:first_order]
    assert "get_balance" in before_order
    assert "get_market" in before_order
    assert "list_market_positions" not in before_order
    assert "get_settlements" not in before_order
    assert any(order["action"] == "buy" for order in client.placed)
    assert stats["placed"] == 1
    sell = next(order for order in client.placed if order["action"] == "sell")
    assert sell["yes_price"] == 94


@pytest.mark.asyncio
async def test_resting_99c_sell_is_replaced_with_94c():
    assert SELL_ALL_AT == 0.94
    client = FakeKalshi()
    client.positions = [{
        "ticker": ALCARAZ,
        "position_fp": "3.00",
        "market_exposure_dollars": "2.40",
    }]
    client.orders = [{
        "order_id": "old-99",
        "ticker": ALCARAZ,
        "status": "resting",
        "action": "sell",
        "side": "yes",
        "yes_price_dollars": "0.99",
        "remaining_count_fp": "3.00",
    }]
    client.events = [_alcaraz_event(ask="0.80", bid="0.78")]
    await _strategy(client, [])._manage_open_positions(_manage_stats())
    assert client.cancelled
    assert client.cancelled[0]["order_id"] == "old-99"
    sells = [order for order in client.placed if order["action"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["yes_price"] == 94
    assert sells[0]["time_in_force"] == "good_till_canceled"
