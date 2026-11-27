"""Tennis strategy safety fixes. Kalshi and SofaScore are mocked."""

import logging

import pytest

from src.clients.sofascore_client import LiveTennisMatch, names_match
from src.strategies.tennis_set3 import (
    TennisSet3Strategy,
    contracts_for_stake,
)
from src.utils.kalshi_fees import taker_fee_dollars
from src.utils.loop_pace import next_sleep_seconds


ALCARAZ = "KXATPMATCH-26NOV27-ALCARAZ"
SINNER = "KXATPMATCH-26NOV27-SINNER"


def _fee_table():
    # Published general trading-fees table (July 7, 2026), cents rounded up.
    assert taker_fee_dollars(1, 0.50) == 0.02
    assert taker_fee_dollars(1, 0.10) == 0.01
    assert taker_fee_dollars(1, 0.20) == 0.02
    assert taker_fee_dollars(1, 0.99) == 0.01
    assert taker_fee_dollars(1, 0.01) == 0.01
    assert taker_fee_dollars(100, 0.50) == 1.75
    assert taker_fee_dollars(100, 0.25) == 1.32
    assert taker_fee_dollars(100, 0.10) == 0.63
    assert taker_fee_dollars(100, 0.99) == 0.07


def test_taker_fee_matches_kalshi_schedule():
    _fee_table()


def test_contracts_leave_room_for_the_taker_fee():
    # 80c + 2c fee does not fit in 81c, and does fit in 82c.
    assert contracts_for_stake(0.81, 0.80, 0.81) == 0
    assert contracts_for_stake(0.82, 0.80, 0.82) == 1
    count = contracts_for_stake(30.0, 0.80, 30.0)
    assert count >= 1
    assert count * 0.80 + taker_fee_dollars(count, 0.80) <= 30.0 + 1e-9


def test_loop_pause_floor_and_backoff():
    assert next_sleep_seconds(0, 0) == 1.0
    assert next_sleep_seconds(0.5, 0) == 1.0
    assert next_sleep_seconds(2.5, 0) == 2.5
    assert next_sleep_seconds(0, 1) == 2.0
    assert next_sleep_seconds(0, 2) == 4.0
    assert next_sleep_seconds(0, 10) == 60.0
    assert next_sleep_seconds(30, 0) == 30.0
    assert next_sleep_seconds(30, 1) == 60.0


def test_interval_accepts_fraction_and_zero():
    from cli import build_parser

    parser = build_parser()
    zero = parser.parse_args(["run", "--tennis", "--loop", "--interval", "0"])
    half = parser.parse_args(["run", "--tennis", "--interval", "0.5"])
    assert zero.interval == 0.0
    assert half.interval == 0.5
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--tennis", "--interval", "-1"])


def test_names_match_given_name_and_same_surname():
    assert names_match("Carlos Alcaraz", "Alcaraz C.")
    assert names_match("Jannik Sinner", "Sinner J.")
    assert not names_match("Elena Rybakina", "Elena Milovanovic")
    assert names_match("Yafan Wang", "Wang Y.")
    assert not names_match("Yafan Wang", "Wang X.")
    assert not names_match("Yafan Wang", "Wang X.", allow_surname_only=False)
    assert not names_match("Wang", "Wang Y.", allow_surname_only=False)
    assert names_match("Luca Van Assche", "Van Assche")


def _wang_match() -> LiveTennisMatch:
    return LiveTennisMatch(
        home_name="Wang X.",
        away_name="Wang Y.",
        current_set=3,
        status_type="inprogress",
        status_description="3rd set",
        home_score={"period1": 6, "period2": 3, "period3": 1},
        away_score={"period1": 4, "period2": 6, "period3": 4},
    )


def test_same_surname_side_is_not_home_by_default():
    match = _wang_match()
    # Away Wang Y. is the one up 4-1. Home-first surname matching would
    # attribute those games to whoever we asked about.
    assert match._side_of("Yafan Wang") == "away"
    assert match._side_of("Xinyu Wang") == "home"
    assert match.games_ahead_in_set("Yafan Wang", 3) == 3
    assert match.games_ahead_in_set("Xinyu Wang", 3) == -3
    assert match._side_of("Wang") is None


class FakeSofa:
    def __init__(self, matches, blocked=False):
        self.matches = matches
        self.last_fetch_blocked = blocked
        self.closed = False

    async def get_live_tennis_matches(self):
        return list(self.matches)

    async def close(self):
        self.closed = True


class FakeKalshi:
    def __init__(self):
        self.balance = {"balance": 10000, "portfolio_value": 0}
        self.events = []
        self.positions = []
        self.orders = []
        self.placed = []
        self.cancelled = []
        self.fail_positions = False
        self.fail_orders = False
        self.fail_cancel = False
        self.hit_rate_limit = False

    async def get_balance(self):
        return dict(self.balance)

    async def list_market_positions(self):
        if self.fail_positions:
            raise RuntimeError("positions down")
        return list(self.positions)

    async def list_orders(self, ticker=None, status=None):
        if self.fail_orders:
            raise RuntimeError("orders down")
        rows = []
        for order in self.orders:
            if ticker and order.get("ticker") != ticker:
                continue
            if status and order.get("status") != status:
                continue
            rows.append(order)
        return rows

    async def get_market(self, ticker):
        for event in self.events:
            for market in event.get("markets", []):
                if market.get("ticker") == ticker:
                    return {"market": market}
        raise RuntimeError(f"no market {ticker}")

    async def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return {
            "order": {
                "order_id": "oid",
                "status": "executed",
                "fill_count": kwargs.get("count", 0),
                "remaining_count": 0,
            }
        }

    async def cancel_order(self, order_id, market_ticker=None):
        self.cancelled.append({"order_id": order_id, "market_ticker": market_ticker})
        if self.fail_cancel:
            raise RuntimeError("410 Gone")
        return {"order_id": order_id, "reduced_by": "1.00"}

    async def get_settlements(self, limit=100, cursor=None):
        return {"settlements": [], "cursor": ""}

    async def _make_authenticated_request(self, method, endpoint, params=None, json_data=None, require_auth=True):
        series = (params or {}).get("series_ticker")
        if series == "KXATPMATCH":
            return {"events": self.events, "cursor": ""}
        return {"events": [], "cursor": ""}


def _quotes(yes_bid, yes_ask, no_bid, no_ask):
    return {
        "yes_bid_dollars": f"{yes_bid:.2f}",
        "yes_ask_dollars": f"{yes_ask:.2f}",
        "no_bid_dollars": f"{no_bid:.2f}",
        "no_ask_dollars": f"{no_ask:.2f}",
    }


def _alcaraz_event(ask="0.80", bid="0.78"):
    alcaraz = {
        "ticker": ALCARAZ,
        "status": "active",
        "yes_sub_title": "Carlos Alcaraz",
        **_quotes(float(bid), float(ask), round(1 - float(ask), 2), round(1 - float(bid), 2)),
    }
    sinner = {
        "ticker": SINNER,
        "status": "active",
        "yes_sub_title": "Jannik Sinner",
        **_quotes(0.18, 0.22, 0.76, 0.80),
    }
    return {"title": "Alcaraz vs Sinner", "category": "Sports", "markets": [alcaraz, sinner]}


def _live_alcaraz():
    return LiveTennisMatch(
        home_name="Carlos Alcaraz",
        away_name="Jannik Sinner",
        current_set=3,
        status_type="inprogress",
        status_description="3rd set",
        home_score={"period1": 6, "period2": 3, "period3": 4},
        away_score={"period1": 4, "period2": 6, "period3": 1},
    )


def _strategy(client, matches, blocked=False):
    return TennisSet3Strategy(
        client=client,
        sofascore=FakeSofa(matches, blocked=blocked),
        dry_run=False,
    )


def _manage_stats():
    return {
        "errors": 0,
        "tp_lines_set": 0,
        "stopped_positions": 0,
        "stopped_contracts": 0,
        "needs_backoff": False,
    }


@pytest.mark.asyncio
async def test_buy_is_ioc_and_reserves_the_fee():
    client = FakeKalshi()
    client.balance = {"balance": 82, "portfolio_value": 0}
    client.events = [_alcaraz_event()]
    stats = await _strategy(client, [_live_alcaraz()]).run()
    buys = [order for order in client.placed if order["action"] == "buy"]
    sells = [order for order in client.placed if order["action"] == "sell"]
    assert len(buys) == 1
    order = buys[0]
    assert order["time_in_force"] == "immediate_or_cancel"
    assert order["count"] == 1
    assert order["yes_price"] == 80
    assert "expiration_ts" not in order
    assert len(sells) == 1
    assert sells[0]["time_in_force"] == "good_till_canceled"
    assert sells[0]["yes_price"] == 99
    assert stats["placed"] == 1


@pytest.mark.asyncio
async def test_all_cash_order_too_small_for_fee_is_not_sent():
    client = FakeKalshi()
    client.balance = {"balance": 81, "portfolio_value": 0}
    client.events = [_alcaraz_event()]
    await _strategy(client, [_live_alcaraz()]).run()
    assert client.placed == []


@pytest.mark.asyncio
async def test_sub_dollar_cash_does_not_buy_the_leader_at_25_to_50c():
    client = FakeKalshi()
    client.balance = {"balance": 90, "portfolio_value": 0}
    client.events = [_alcaraz_event(ask="0.40", bid="0.38")]
    await _strategy(client, [_live_alcaraz()]).run()
    assert client.placed == []


@pytest.mark.asyncio
async def test_exposure_failure_skips_entries():
    client = FakeKalshi()
    client.fail_orders = True
    client.events = [_alcaraz_event()]
    client.balance = {"balance": 10000, "portfolio_value": 0}
    stats = await _strategy(client, [_live_alcaraz()]).run()
    assert client.placed == []
    assert stats["needs_backoff"] is True
    assert stats["entries_blocked"] == 1


@pytest.mark.asyncio
async def test_duplicate_and_cap_block_entries():
    client = FakeKalshi()
    client.events = [_alcaraz_event()]
    client.balance = {"balance": 10000, "portfolio_value": 0}
    client.positions = [{"ticker": ALCARAZ, "position_fp": "2.00", "market_exposure_dollars": "1.60"}]
    client.orders = [{
        "order_id": "tp",
        "ticker": ALCARAZ,
        "status": "resting",
        "action": "sell",
        "side": "yes",
        "yes_price_dollars": "0.99",
        "remaining_count_fp": "2.00",
    }]
    await _strategy(client, [_live_alcaraz()]).run()
    assert [order["action"] for order in client.placed] == []

    client.positions = [
        {"ticker": f"KXNBAGAME-{i}", "position_fp": "1.00"} for i in range(5)
    ]
    await _strategy(client, [_live_alcaraz()]).run()
    assert client.placed == []


@pytest.mark.asyncio
async def test_stop_skips_empty_book_and_applies_to_small_stakes():
    client = FakeKalshi()
    strategy = _strategy(client, [])
    client.positions = [{
        "ticker": ALCARAZ,
        "position_fp": "3.00",
        "market_exposure_dollars": "2.40",
    }]
    client.events = [_alcaraz_event(ask="0.22", bid="0.00")]
    await strategy._manage_open_positions(_manage_stats())
    assert all(order.get("time_in_force") != "immediate_or_cancel" for order in client.placed)
    client.placed.clear()

    client.events = [_alcaraz_event(ask="0.22", bid="0.20")]
    await strategy._manage_open_positions(_manage_stats())
    assert len(client.placed) == 1
    sell = client.placed[0]
    assert sell["action"] == "sell"
    assert sell["side"] == "yes"
    assert sell["time_in_force"] == "immediate_or_cancel"
    assert sell["reduce_only"] is True
    assert sell["yes_price"] == 20
    assert sell["count"] == 3


@pytest.mark.asyncio
async def test_non_tennis_positions_are_not_managed():
    client = FakeKalshi()
    client.positions = [{
        "ticker": "KXNBAGAME-26NOV27-BOS",
        "position_fp": "10.00",
        "market_exposure_dollars": "4.00",
    }]
    client.events = [_alcaraz_event(ask="0.20", bid="0.10")]
    await _strategy(client, [])._manage_open_positions(_manage_stats())
    assert client.placed == []
    assert client.cancelled == []


@pytest.mark.asyncio
async def test_no_position_keeps_a_99c_sell():
    client = FakeKalshi()
    client.positions = [{
        "ticker": ALCARAZ,
        "position_fp": "-4.00",
        "market_exposure_dollars": "1.00",
    }]
    client.orders = [{
        "order_id": "no-99",
        "ticker": ALCARAZ,
        "status": "resting",
        "action": "sell",
        "side": "no",
        "yes_price_dollars": "0.01",
        "no_price_dollars": "0.99",
        "remaining_count_fp": "4.00",
    }]
    # NO bid is 76c, above the stop, so the resting 99c NO sell must stay.
    client.events = [_alcaraz_event(ask="0.22", bid="0.18")]
    await _strategy(client, [])._manage_open_positions(_manage_stats())
    assert client.placed == []
    assert client.cancelled == []


@pytest.mark.asyncio
async def test_no_exit_recognized_from_book_side_when_action_is_absent():
    client = FakeKalshi()
    client.positions = [{
        "ticker": ALCARAZ,
        "position_fp": "-4.00",
        "market_exposure_dollars": "1.00",
    }]
    client.orders = [{
        "order_id": "book-bid",
        "ticker": ALCARAZ,
        "status": "resting",
        "book_side": "bid",
        "yes_price_dollars": "0.01",
        "no_price_dollars": "0.99",
        "remaining_count_fp": "4.00",
    }]
    client.events = [_alcaraz_event(ask="0.22", bid="0.18")]
    await _strategy(client, [])._manage_open_positions(_manage_stats())
    assert client.placed == []
    assert client.cancelled == []


@pytest.mark.asyncio
async def test_cancel_failure_is_logged_and_does_not_stack_a_sell(caplog):
    client = FakeKalshi()
    client.fail_cancel = True
    client.positions = [{
        "ticker": ALCARAZ,
        "position_fp": "3.00",
        "market_exposure_dollars": "2.40",
    }]
    client.orders = [{
        "order_id": "stale-50",
        "ticker": ALCARAZ,
        "status": "resting",
        "action": "sell",
        "side": "yes",
        "yes_price_dollars": "0.50",
        "remaining_count_fp": "3.00",
    }]
    client.events = [_alcaraz_event(ask="0.22", bid="0.20")]
    with caplog.at_level(logging.ERROR, logger="src.strategies.tennis_set3"):
        await _strategy(client, [])._manage_open_positions(_manage_stats())
    assert client.placed == []
    assert any("410" in record.message or "cancel" in record.message.lower() for record in caplog.records)
    assert client.cancelled
    assert all(item["market_ticker"] == ALCARAZ for item in client.cancelled)


@pytest.mark.asyncio
async def test_sofascore_block_requests_backoff_and_ambiguous_names_do_not_trade():
    client = FakeKalshi()
    client.balance = {"balance": 10000, "portfolio_value": 0}
    blocked = await _strategy(client, [], blocked=True).run()
    assert blocked["needs_backoff"] is True
    assert client.placed == []

    match = _wang_match()
    client.events = [{
        "title": "Wang vs Wang",
        "category": "Sports",
        "markets": [
            {
                "ticker": "KXATPMATCH-26NOV27-WANGA",
                "status": "active",
                "yes_sub_title": "Wang",
                **_quotes(0.78, 0.80, 0.18, 0.22),
            },
            {
                "ticker": "KXATPMATCH-26NOV27-WANGB",
                "status": "active",
                "yes_sub_title": "Wang",
                **_quotes(0.18, 0.22, 0.76, 0.80),
            },
        ],
    }]
    strategy = _strategy(client, [match])
    assert strategy._live_match_for_market(
        {"yes_sub_title": "Wang", "_opponent": "Wang"}, [match]
    ) is None
    found = strategy._live_match_for_market(
        {"yes_sub_title": "Yafan Wang", "_opponent": "Xinyu Wang"}, [match]
    )
    assert found is match
    await strategy.run()
    assert client.placed == []


@pytest.mark.asyncio
async def test_missing_live_quote_does_not_use_the_bulk_snapshot():
    client = FakeKalshi()
    client.balance = {"balance": 82, "portfolio_value": 0}
    client.events = [_alcaraz_event()]

    async def no_quote(ticker):
        raise RuntimeError("market timeout")

    client.get_market = no_quote
    stats = await _strategy(client, [_live_alcaraz()]).run()
    assert [order for order in client.placed if order["action"] == "buy"] == []
    assert stats["errors"] >= 1


@pytest.mark.asyncio
async def test_incomplete_market_list_skips_new_entries():
    client = FakeKalshi()
    client.balance = {"balance": 82, "portfolio_value": 0}
    client.events = [_alcaraz_event()]

    async def events_down(method, endpoint, params=None, json_data=None, require_auth=True):
        raise RuntimeError("events down")

    client._make_authenticated_request = events_down
    stats = await _strategy(client, [_live_alcaraz()]).run()
    assert client.placed == []
    assert stats["needs_backoff"] is True
