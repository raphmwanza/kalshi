"""Kalshi order create, cancel, and pagination safety."""

import itertools
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.clients.kalshi_client import KalshiAPIError, KalshiClient


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class _Response:
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)
        self.request = httpx.Request("GET", "https://example.test/trade")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(
                self.status_code, text=self.text, request=self.request
            )
            raise httpx.HTTPStatusError("err", request=self.request, response=response)


def _client(monkeypatch) -> KalshiClient:
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", _pem())
    client = KalshiClient(api_key="test-key")
    client.request_spacing_seconds = 0
    client.backoff_factor = 0
    client.order_recovery_delay = 0
    client.order_recovery_attempts = 1
    client.max_retries = 2
    return client


def _ok_order(client_order_id, fill="1.00"):
    return {
        "order_id": "oid-1",
        "client_order_id": client_order_id,
        "fill_count_fp": fill,
        "remaining_count_fp": "0.00",
    }


@pytest.mark.asyncio
async def test_cancel_uses_v2_route_and_market_ticker(monkeypatch):
    client = _client(monkeypatch)
    seen = []

    async def request(method, url, headers, content):
        seen.append((method, url, headers.get("KALSHI-ACCESS-TIMESTAMP")))
        return _Response(200, {"order_id": "abc", "reduced_by": "1.00"})

    client.client.request = request
    try:
        await client.cancel_order("abc", market_ticker="KXATPMATCH-26NOV27-A")
    finally:
        await client.close()

    method, url, _ts = seen[0]
    assert method == "DELETE"
    assert "/trade-api/v2/portfolio/events/orders/abc" in url
    assert "market_ticker=KXATPMATCH-26NOV27-A" in url
    assert "/portfolio/orders/abc" not in url.split("?", 1)[0].replace(
        "/portfolio/events/orders/abc", ""
    )
    with pytest.raises(KalshiAPIError):
        await client.cancel_order("abc")


@pytest.mark.asyncio
async def test_order_post_is_not_resent_when_lookup_finds_it(monkeypatch):
    client = _client(monkeypatch)
    posts = []

    async def request_fixed(method, url, headers, content):
        if method == "POST":
            posts.append(json.loads(content)["client_order_id"])
            posts.append(headers["KALSHI-ACCESS-TIMESTAMP"])
            raise httpx.ConnectError("connection dropped")
        return _Response(
            200,
            {"orders": [_ok_order("cid-1", fill="2.00")], "cursor": ""},
        )

    client.client.request = request_fixed
    try:
        result = await client.place_order(
            ticker="KXATPMATCH-1",
            client_order_id="cid-1",
            side="yes",
            action="buy",
            count=2,
            type_="limit",
            yes_price=80,
            time_in_force="immediate_or_cancel",
        )
    finally:
        await client.close()

    assert posts.count("cid-1") == 1
    assert result["order"]["fill_count"] == 2
    assert result["order"]["order_id"] == "oid-1"


@pytest.mark.asyncio
async def test_order_post_retries_only_after_a_negative_lookup_and_resigns(monkeypatch):
    import src.clients.kalshi_client as kc

    client = _client(monkeypatch)
    clock = itertools.count(1_700_000_000_000, 5)
    monkeypatch.setattr(kc.time, "time", lambda: next(clock) / 1000.0)
    calls = []

    async def request(method, url, headers, content):
        calls.append((method, headers.get("KALSHI-ACCESS-TIMESTAMP"), url))
        posts = [c for c in calls if c[0] == "POST"]
        if method == "POST" and len(posts) == 1:
            raise httpx.ConnectError("timed out")
        if method == "GET":
            return _Response(200, {"orders": [], "cursor": ""})
        return _Response(201, _ok_order("cid-2", fill="1.00"))

    client.client.request = request
    try:
        result = await client.place_order(
            ticker="KXATPMATCH-1",
            client_order_id="cid-2",
            side="yes",
            action="buy",
            count=1,
            type_="limit",
            yes_price=80,
            time_in_force="immediate_or_cancel",
        )
    finally:
        await client.close()

    post_times = [ts for method, ts, _url in calls if method == "POST"]
    assert len(post_times) == 2
    assert post_times[0] != post_times[1]
    assert any(method == "GET" for method, _ts, _url in calls)
    assert result["order"]["fill_count"] == 1


@pytest.mark.asyncio
async def test_ambiguous_order_is_not_resent_when_lookup_fails(monkeypatch):
    client = _client(monkeypatch)
    posts = []

    async def request(method, url, headers, content):
        if method == "POST":
            posts.append(1)
            raise httpx.ConnectError("timed out")
        raise httpx.ConnectError("lookup down")

    client.client.request = request
    try:
        with pytest.raises(KalshiAPIError, match="not resubmitting"):
            await client.place_order(
                ticker="KXATPMATCH-1",
                client_order_id="cid-3",
                side="yes",
                action="buy",
                count=1,
                type_="limit",
                yes_price=80,
            )
    finally:
        await client.close()

    assert len(posts) == 1


@pytest.mark.asyncio
async def test_429_is_a_rate_limit_and_is_resigned(monkeypatch):
    import src.clients.kalshi_client as kc

    client = _client(monkeypatch)
    clock = itertools.count(1_800_000_000_000, 5)
    monkeypatch.setattr(kc.time, "time", lambda: next(clock) / 1000.0)
    calls = []

    async def request(method, url, headers, content):
        calls.append((method, headers.get("KALSHI-ACCESS-TIMESTAMP")))
        if method == "GET":
            return _Response(200, {"orders": [], "cursor": ""})
        if len([c for c in calls if c[0] == "POST"]) == 1:
            return _Response(429, text='{"error":{"code":"too_many_requests"}}')
        return _Response(201, _ok_order("cid-4"))

    client.client.request = request
    try:
        await client.place_order(
            ticker="KXATPMATCH-1",
            client_order_id="cid-4",
            side="no",
            action="sell",
            count=1,
            type_="limit",
            no_price=99,
            time_in_force="immediate_or_cancel",
            reduce_only=True,
        )
    finally:
        await client.close()

    assert client.hit_rate_limit is True
    post_times = [ts for method, ts in calls if method == "POST"]
    assert len(post_times) == 2
    assert post_times[0] != post_times[1]


@pytest.mark.asyncio
async def test_positions_and_orders_follow_the_cursor(monkeypatch):
    client = _client(monkeypatch)
    calls = []

    async def request(method, url, headers, content):
        calls.append(url)
        if "/positions" in url:
            if "cursor=p2" in url:
                payload = {"market_positions": [{"ticker": "B", "position_fp": "2.00"}], "cursor": ""}
            else:
                payload = {"market_positions": [{"ticker": "A", "position_fp": "1.00"}], "cursor": "p2"}
            return _Response(200, payload)
        if "cursor=o2" in url:
            payload = {"orders": [{"order_id": "2", "ticker": "D"}], "cursor": ""}
        else:
            payload = {"orders": [{"order_id": "1", "ticker": "C"}], "cursor": "o2"}
        return _Response(200, payload)

    client.client.request = request
    try:
        positions = await client.list_market_positions()
        orders = await client.list_orders(status="resting")
    finally:
        await client.close()

    assert [p["ticker"] for p in positions] == ["A", "B"]
    assert [o["ticker"] for o in orders] == ["C", "D"]
    assert any("count_filter=position" in url for url in calls)
    assert any("cursor=p2" in url for url in calls)
    assert any("cursor=o2" in url for url in calls)
    assert any("status=resting" in url for url in calls)
