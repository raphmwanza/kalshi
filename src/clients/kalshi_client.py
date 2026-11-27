"""
Kalshi API client for trading operations.
Handles authentication, market data, and trade execution.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from src.clients.private_key import load_kalshi_private_key
from src.config.settings import settings
from src.utils.logging_setup import TradingLoggerMixin


class KalshiAPIError(Exception):
    """Custom exception for Kalshi API errors."""
    pass


class KalshiClient(TradingLoggerMixin):
    """
    Kalshi API client for automated trading.
    Handles authentication, market data retrieval, and trade execution.
    """
    
    def __init__(
        self, 
        api_key: Optional[str] = None, 
        private_key_path: str = None,
        max_retries: int = 5,
        backoff_factor: float = 0.5
    ):
        """
        Initialize Kalshi client.
        
        Args:
            api_key: Kalshi API key (Key ID from the API key generation)
            private_key_path: Path to private key file
            max_retries: Maximum number of retries for failed requests
            backoff_factor: Factor for exponential backoff
        """
        self.api_key = api_key or settings.api.kalshi_api_key
        self.base_url = settings.api.kalshi_base_url
        self.private_key_path = private_key_path or os.environ.get(
            "KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem"
        )
        self.private_key = None
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        # Gap before every HTTP call. Keeps a 0-second bot loop from bursting.
        self.request_spacing_seconds = 0.2
        # Set when Kalshi returns 429 so the trading loop can back off.
        self.hit_rate_limit = False
        # After an ambiguous order create, re-read the order this many times
        # before deciding it was not accepted (Get Orders is eventually consistent).
        self.order_recovery_attempts = 3
        self.order_recovery_delay = 0.25
        self.max_list_pages = 50
        
        # Load private key
        self._load_private_key()
        
        # HTTP client with timeouts
        self.client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
        )
        
        self.logger.info("Kalshi client initialized", api_key_length=len(self.api_key) if self.api_key else 0)
    
    def _load_private_key(self) -> None:
        """Load the RSA private key from env contents or a PEM file."""
        try:
            self.private_key, source = load_kalshi_private_key(self.private_key_path)
            if source == "KALSHI_PRIVATE_KEY":
                self.logger.info("Private key loaded from KALSHI_PRIVATE_KEY")
            else:
                self.private_key_path = source
                self.logger.info("Private key loaded successfully", path=source)
        except FileNotFoundError as e:
            raise KalshiAPIError(str(e)) from e
        except KalshiAPIError:
            raise
        except Exception as e:
            self.logger.error("Failed to load private key", error=str(e))
            raise KalshiAPIError(f"Failed to load private key: {e}")
    
    def _sign_request(self, timestamp: str, method: str, path: str) -> str:
        """
        Sign request using RSA PSS signing method as per Kalshi API docs.
        
        Args:
            timestamp: Request timestamp in milliseconds
            method: HTTP method
            path: Request path
        
        Returns:
            Base64 encoded signature
        """
        # Create message to sign: timestamp + method + path
        message = timestamp + method.upper() + path
        message_bytes = message.encode('utf-8')
        
        try:
            # Sign using RSA PSS as per Kalshi documentation
            signature = self.private_key.sign(
                message_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256()
            )
            
            return base64.b64encode(signature).decode('utf-8')
        except Exception as e:
            self.logger.error("Failed to sign request", error=str(e))
            raise KalshiAPIError(f"Failed to sign request: {e}")
    
    @staticmethod
    def _is_order_create(method: str, endpoint: str) -> bool:
        """True for Create Order (V2). Cancels and amends are not creates."""
        path = (endpoint or "").rstrip("/")
        return method.upper() == "POST" and path.endswith("/portfolio/events/orders")

    def _auth_headers(self, method: str, endpoint: str, require_auth: bool) -> Dict[str, str]:
        """Build headers. A new timestamp and signature are produced on every call."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if not require_auth:
            return headers
        timestamp = str(int(time.time() * 1000))
        signature = self._sign_request(timestamp, method, endpoint)
        headers.update({
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
        })
        return headers

    @staticmethod
    def _build_url(base_url: str, endpoint: str, params: Optional[Dict]) -> str:
        url = f"{base_url}{endpoint}"
        if not params:
            return url
        clean = {k: v for k, v in params.items() if v is not None}
        if not clean:
            return url
        return f"{url}?{urlencode(clean)}"

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = self.backoff_factor * (2 ** attempt)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _request_once(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict],
        json_data: Optional[Dict],
        require_auth: bool,
    ) -> Dict[str, Any]:
        """Send one request. Always signs with a fresh timestamp."""
        if self.request_spacing_seconds:
            await asyncio.sleep(self.request_spacing_seconds)
        headers = self._auth_headers(method, endpoint, require_auth)
        body = json.dumps(json_data, separators=(",", ":")) if json_data else None
        url = self._build_url(self.base_url, endpoint, params)
        self.logger.debug(
            "Making API request",
            method=method,
            endpoint=endpoint,
            has_auth=require_auth,
        )
        response = await self.client.request(
            method=method,
            url=url,
            headers=headers,
            content=body,
        )
        response.raise_for_status()
        return response.json()

    async def _request_with_retries(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        require_auth: bool = True,
    ) -> Dict[str, Any]:
        """Retry idempotent calls. Each attempt is signed again.

        Order creates do not use this path — a timed-out POST may already
        have been accepted, so resending it is not safe.
        """
        last_exception: Optional[BaseException] = None
        for attempt in range(self.max_retries):
            try:
                return await self._request_once(
                    method, endpoint, params, json_data, require_auth
                )
            except httpx.HTTPStatusError as exc:
                last_exception = exc
                code = exc.response.status_code
                if code == 429:
                    self.hit_rate_limit = True
                if code == 429 or code == 408 or code >= 500:
                    self.logger.warning(
                        "API request failed with status %s. Retrying...",
                        code,
                        endpoint=endpoint,
                        attempt=attempt + 1,
                    )
                    if attempt + 1 < self.max_retries:
                        await self._sleep_backoff(attempt)
                    continue
                error_msg = f"HTTP {code}: {exc.response.text}"
                self.logger.error(
                    "API request failed without retry", error=error_msg, endpoint=endpoint
                )
                raise KalshiAPIError(error_msg) from exc
            except httpx.TransportError as exc:
                last_exception = exc
                self.logger.warning(
                    "Request failed with a transport error. Retrying...",
                    error=str(exc),
                    endpoint=endpoint,
                    attempt=attempt + 1,
                )
                if attempt + 1 < self.max_retries:
                    await self._sleep_backoff(attempt)
        raise KalshiAPIError(
            f"API request failed after {self.max_retries} retries: {last_exception}"
        )

    async def _scan_for_client_order(
        self, client_order_id: str, ticker: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Return the order with this client_order_id, or None if a full scan missed it."""
        cursor: Optional[str] = None
        seen: set = set()
        for _ in range(self.max_list_pages):
            params: Dict[str, Any] = {"limit": 1000}
            if ticker:
                params["ticker"] = ticker
            if cursor:
                params["cursor"] = cursor
            resp = await self._request_with_retries(
                "GET", "/trade-api/v2/portfolio/orders", params=params
            )
            for order in resp.get("orders") or []:
                if str(order.get("client_order_id") or "") == client_order_id:
                    return order
            cursor = str(resp.get("cursor") or "").strip() or None
            if not cursor or cursor in seen:
                return None
            seen.add(cursor)
        return None

    async def _lookup_created_order(
        self, client_order_id: str, ticker: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Find an order after an ambiguous create.

        Returns the order dict, or None when at least one scan completed and
        the id was absent. Raises KalshiAPIError when every scan failed — the
        caller must not resubmit in that case.

        Get Orders has no client_order_id filter, so this pages orders for the
        ticker (all statuses: an IOC fill is ``executed``, not ``resting``).
        """
        if not client_order_id:
            raise KalshiAPIError(
                "Ambiguous order result without client_order_id; not resubmitting"
            )
        saw_success = False
        for attempt in range(self.order_recovery_attempts):
            if attempt and self.order_recovery_delay:
                await asyncio.sleep(self.order_recovery_delay * attempt)
            try:
                found = await self._scan_for_client_order(client_order_id, ticker)
            except Exception as exc:  # noqa: BLE001 - lookup failure must not resubmit
                self.logger.warning(
                    "Order lookup by client_order_id failed",
                    client_order_id=client_order_id,
                    error=str(exc),
                )
                continue
            saw_success = True
            if found is not None:
                return found
        if not saw_success:
            raise KalshiAPIError(
                "Order create outcome is unknown and lookup by client_order_id "
                f"failed for {client_order_id}; not resubmitting"
            )
        return None

    async def _post_order_idempotent(
        self,
        endpoint: str,
        params: Optional[Dict],
        json_data: Optional[Dict],
        require_auth: bool,
    ) -> Dict[str, Any]:
        """POST a create-order exactly once unless we know it was not accepted.

        A 408, 5xx, timeout, or dropped connection does not say whether Kalshi
        booked the order. Those are reconciled with Get Orders by
        ``client_order_id`` before any resend, and every attempt is re-signed.
        A 429 is a write-budget rejection; it is still looked up first so a
        request that actually landed is not duplicated.
        """
        payload = json_data or {}
        client_order_id = str(payload.get("client_order_id") or "")
        ticker = payload.get("ticker")
        last_exception: Optional[BaseException] = None

        for attempt in range(self.max_retries):
            try:
                return await self._request_once(
                    "POST", endpoint, params, json_data, require_auth
                )
            except httpx.HTTPStatusError as exc:
                last_exception = exc
                code = exc.response.status_code
                body = exc.response.text or ""
                if code == 409 and "order_already_exists" in body:
                    found = await self._lookup_created_order(client_order_id, ticker)
                    if found is not None:
                        self.logger.warning(
                            "Create returned order_already_exists; using the existing order",
                            client_order_id=client_order_id,
                        )
                        return found
                    raise KalshiAPIError(f"HTTP 409: {body}") from exc
                ambiguous = code == 408 or code >= 500
                if code == 429:
                    self.hit_rate_limit = True
                if not ambiguous and code != 429:
                    raise KalshiAPIError(f"HTTP {code}: {body}") from exc
                found = await self._reconcile_ambiguous_create(
                    client_order_id, ticker, ambiguous=ambiguous
                )
                if found is not None:
                    return found
                self.logger.warning(
                    "Order create was not accepted; retrying with a new signature",
                    client_order_id=client_order_id,
                    status=code,
                    attempt=attempt + 1,
                )
                if attempt + 1 < self.max_retries:
                    await self._sleep_backoff(attempt)
            except (httpx.TransportError, json.JSONDecodeError) as exc:
                # A dropped connection or an unreadable 2xx body does not say
                # whether the exchange accepted the order.
                last_exception = exc
                found = await self._reconcile_ambiguous_create(
                    client_order_id, ticker, ambiguous=True
                )
                if found is not None:
                    return found
                self.logger.warning(
                    "Order create transport error and order not found; retrying signed fresh",
                    client_order_id=client_order_id,
                    error=str(exc),
                    attempt=attempt + 1,
                )
                if attempt + 1 < self.max_retries:
                    await self._sleep_backoff(attempt)
        raise KalshiAPIError(
            f"Order create failed after {self.max_retries} attempts: {last_exception}"
        )

    async def _reconcile_ambiguous_create(
        self,
        client_order_id: str,
        ticker: Optional[str],
        *,
        ambiguous: bool,
    ) -> Optional[Dict[str, Any]]:
        """Return the existing order, or None if it is safe to resubmit.

        When ``ambiguous`` is true and the lookup itself fails, raise instead
        of resubmitting. A 429 (``ambiguous=False``) is a documented rejection,
        so a failed lookup still allows a signed retry.
        """
        try:
            found = await self._lookup_created_order(client_order_id, ticker)
        except KalshiAPIError:
            if ambiguous:
                raise
            return None
        if found is not None:
            self.logger.warning(
                "Recovered order after an uncertain create response",
                client_order_id=client_order_id,
                order_id=found.get("order_id"),
            )
        return found

    async def _make_authenticated_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        require_auth: bool = True
    ) -> Dict[str, Any]:
        """
        Make an authenticated request to the Kalshi API.

        Every attempt is signed with a new timestamp. Create-order POSTs are
        not retried until a lookup by ``client_order_id`` shows the order was
        not accepted.

        Args:
            method: HTTP method
            endpoint: API endpoint
            params: Query parameters
            json_data: JSON request body
            require_auth: Whether authentication is required

        Returns:
            API response data
        """
        if self._is_order_create(method, endpoint):
            return await self._post_order_idempotent(
                endpoint, params, json_data, require_auth
            )
        return await self._request_with_retries(
            method, endpoint, params, json_data, require_auth
        )

    async def _collect_pages(
        self,
        endpoint: str,
        params: Dict[str, Any],
        *list_keys: str,
    ) -> Dict[str, List]:
        """Follow ``cursor`` until it is empty. Raises if a page fails or the cap is hit."""
        collected: Dict[str, List] = {key: [] for key in list_keys}
        cursor: Optional[str] = None
        seen: set = set()
        for _ in range(self.max_list_pages):
            page_params = dict(params)
            page_params.setdefault("limit", 1000)
            if cursor:
                page_params["cursor"] = cursor
            resp = await self._make_authenticated_request(
                "GET", endpoint, params=page_params
            )
            for key in list_keys:
                collected[key].extend(resp.get(key) or [])
            cursor = str(resp.get("cursor") or "").strip() or None
            if not cursor or cursor in seen:
                return collected
            seen.add(cursor)
        raise KalshiAPIError(
            f"Pagination for {endpoint} still had a cursor after {self.max_list_pages} pages"
        )
    
    async def get_balance(self) -> Dict[str, Any]:
        """Get account balance."""
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/balance")
    
    async def get_positions(
        self,
        ticker: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        count_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get one page of portfolio positions.

        The API defaults to 100 rows. Use ``list_market_positions`` to follow
        the cursor.
        """
        params: Dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if limit is not None:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        if count_filter:
            params["count_filter"] = count_filter
        return await self._make_authenticated_request(
            "GET", "/trade-api/v2/portfolio/positions", params=params
        )

    async def list_market_positions(self, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every non-zero market position, following the pagination cursor.

        ``count_filter=position`` asks Kalshi for non-zero holdings so a page
        of flat markets cannot hide a later open position. Raises if a page
        fails — callers that size or cap entries must not treat that as "no
        positions".
        """
        params: Dict[str, Any] = {"count_filter": "position"}
        if ticker:
            params["ticker"] = ticker
        pages = await self._collect_pages(
            "/trade-api/v2/portfolio/positions",
            params,
            "market_positions",
        )
        return pages["market_positions"]
    
    async def get_fills(self, ticker: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        """Get order fills.""" 
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/fills", params=params)

    async def get_settlements(
        self, limit: int = 100, cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get portfolio settlements (resolved markets), newest first."""
        params: Dict[str, Any] = {"limit": min(int(limit), 200)}
        if cursor:
            params["cursor"] = cursor
        return await self._make_authenticated_request(
            "GET", "/trade-api/v2/portfolio/settlements", params=params
        )
    
    async def get_orders(
        self,
        ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get one page of orders. The API defaults to 100 rows.

        Use ``list_orders`` to follow the cursor. Omit ``status`` to include
        resting, canceled, and executed orders (needed to reconcile an IOC).
        """
        params: Dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        return await self._make_authenticated_request(
            "GET", "/trade-api/v2/portfolio/orders", params=params
        )

    async def list_orders(
        self,
        ticker: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Every order matching the filters, following the pagination cursor."""
        params: Dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        pages = await self._collect_pages(
            "/trade-api/v2/portfolio/orders",
            params,
            "orders",
        )
        return pages["orders"]
    
    async def get_markets(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
        tickers: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Get markets data.
        
        Args:
            limit: Maximum number of markets to return
            cursor: Pagination cursor
            event_ticker: Filter by event ticker
            series_ticker: Filter by series ticker
            status: Filter by market status
            tickers: List of specific tickers to fetch
        
        Returns:
            Markets data
        """
        params = {"limit": limit}
        
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if tickers:
            params["tickers"] = ",".join(tickers)
        
        return await self._make_authenticated_request(
            "GET", "/trade-api/v2/markets", params=params, require_auth=True
        )
    
    async def get_market(self, ticker: str) -> Dict[str, Any]:
        """Get specific market data."""
        return await self._make_authenticated_request(
            "GET", f"/trade-api/v2/markets/{ticker}", require_auth=False
        )
    
    async def get_orderbook(self, ticker: str, depth: int = 100) -> Dict[str, Any]:
        """
        Get market orderbook.
        
        Args:
            ticker: Market ticker
            depth: Orderbook depth
        
        Returns:
            Orderbook data
        """
        params = {"depth": depth}
        return await self._make_authenticated_request(
            "GET", f"/trade-api/v2/markets/{ticker}/orderbook", params=params, require_auth=False
        )
    
    async def get_market_history(
        self,
        ticker: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        Get market price history.
        
        Args:
            ticker: Market ticker
            start_ts: Start timestamp
            end_ts: End timestamp
            limit: Number of records to return
        
        Returns:
            Price history data
        """
        params = {"limit": limit}
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts
        
        return await self._make_authenticated_request(
            "GET", f"/trade-api/v2/markets/{ticker}/history", params=params, require_auth=False
        )
    
    async def place_order(
        self,
        ticker: str,
        client_order_id: str,
        side: str,
        action: str,
        count: int,
        type_: str = "market",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        expiration_ts: Optional[int] = None,
        time_in_force: Optional[str] = None,
        post_only: Optional[bool] = None,
        reduce_only: Optional[bool] = None,
        self_trade_prevention_type: str = "taker_at_cross",
    ) -> Dict[str, Any]:
        """
        Place a trading order via the Kalshi Create Order (V2) endpoint.

        The legacy ``/portfolio/orders`` create endpoint now returns HTTP 410,
        so this translates the historical yes/no + buy/sell + cents interface
        into the V2 shape (single YES-side book with ``bid``/``ask`` and
        fixed-point dollar prices) and normalizes the response back into the
        legacy ``{"order": {...}}`` structure the rest of the codebase expects.

        Args:
            ticker: Market ticker
            client_order_id: Unique client order ID
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            type_: Order type ("market" or "limit")
            yes_price: Yes price in cents (for limit orders)
            no_price: No price in cents (for limit orders)
            expiration_ts: Optional Unix expiration timestamp (seconds)
            time_in_force: Override TIF (fill_or_kill / good_till_canceled /
                immediate_or_cancel). Defaults to IOC for market, GTC for limit.
            post_only: If True, order only rests (never crosses the spread).
            reduce_only: If True, cap the order by current position.
            self_trade_prevention_type: taker_at_cross (default) or maker.

        Returns:
            Order response in the legacy shape: {"order": {order_id, status,
            fill_count, remaining_count, ...}} (raw V2 fields also included).
        """
        side_l = (side or "").lower()
        action_l = (action or "").lower()

        # V2 quotes everything from the YES leg: bid = buy YES, ask = sell YES.
        # Buying NO == selling YES at (1 - price); selling NO == buying YES.
        if side_l == "yes":
            book_side = "bid" if action_l == "buy" else "ask"
            yes_cents = yes_price
        elif side_l == "no":
            book_side = "ask" if action_l == "buy" else "bid"
            yes_cents = None if no_price is None else (100 - no_price)
        else:
            raise ValueError(f"Invalid order side: {side!r} (expected yes/no)")

        # Market orders may arrive without a price; use an aggressive marketable
        # price so they cross immediately (bounded to the 1..99c tradeable range).
        if yes_cents is None:
            yes_cents = 99 if book_side == "bid" else 1
        yes_cents = max(1, min(99, int(round(yes_cents))))
        price_dollars = f"{yes_cents / 100:.2f}"

        if time_in_force is None:
            time_in_force = (
                "immediate_or_cancel" if type_ == "market" else "good_till_canceled"
            )

        order_data: Dict[str, Any] = {
            "ticker": ticker,
            "side": book_side,
            "count": str(int(count)),
            "price": price_dollars,
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
        }
        if client_order_id:
            order_data["client_order_id"] = client_order_id
        if post_only is not None:
            order_data["post_only"] = post_only
        if reduce_only is not None:
            order_data["reduce_only"] = reduce_only
        if expiration_ts:
            # Expiring orders must ride on GTC per the V2 spec.
            order_data["expiration_time"] = expiration_ts
            order_data["time_in_force"] = "good_till_canceled"

        resp = await self._make_authenticated_request(
            "POST", "/trade-api/v2/portfolio/events/orders", json_data=order_data
        )
        return self._normalize_order_response(resp, client_order_id)

    @staticmethod
    def _normalize_order_response(resp: Dict[str, Any], client_order_id: str) -> Dict[str, Any]:
        """Map a V2 Create Order response into the legacy {"order": {...}} shape."""
        def _to_int(v: Any) -> int:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return 0

        if not isinstance(resp, dict):
            return {"order": {}, "raw": resp}

        fill = _to_int(resp.get("fill_count_fp", resp.get("fill_count")))
        remaining = _to_int(resp.get("remaining_count_fp", resp.get("remaining_count")))
        if fill > 0 and remaining == 0:
            status = "executed"
        elif fill > 0:
            status = "partially_filled"
        elif remaining > 0:
            status = "resting"
        else:
            status = "canceled"

        order = {
            "order_id": resp.get("order_id"),
            "client_order_id": resp.get("client_order_id", client_order_id),
            "status": status,
            "fill_count": fill,
            "remaining_count": remaining,
            "average_fill_price": resp.get("average_fill_price"),
            "ts_ms": resp.get("ts_ms"),
        }
        return {"order": order, **resp}
    
    async def cancel_order(
        self, order_id: str, market_ticker: Optional[str] = None
    ) -> Dict[str, Any]:
        """Cancel one order on the shard that trades ``market_ticker``.

        ``DELETE /trade-api/v2/portfolio/orders/{id}`` is a removed legacy
        write and returns HTTP 410 ``deprecated_v1_order_endpoint``. The
        replacement is Cancel Order (V2):

            DELETE /trade-api/v2/portfolio/events/orders/{order_id}?market_ticker=...

        ``market_ticker`` is required. Without it Kalshi routes the cancel to
        exchange shard 0, and an order resting on any other shard comes back
        404 while still live.
        """
        if not order_id:
            raise KalshiAPIError("cancel_order requires order_id")
        if not market_ticker:
            raise KalshiAPIError(
                "cancel_order requires market_ticker so DELETE "
                "/trade-api/v2/portfolio/events/orders/{order_id} is routed "
                "to the market's exchange shard"
            )
        return await self._make_authenticated_request(
            "DELETE",
            f"/trade-api/v2/portfolio/events/orders/{order_id}",
            params={"market_ticker": market_ticker},
        )
    
    async def get_trades(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get trade history.
        
        Args:
            ticker: Filter by ticker
            limit: Maximum number of trades to return
            cursor: Pagination cursor
        
        Returns:
            Trades data
        """
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        
        return await self._make_authenticated_request(
            "GET", "/trade-api/v2/portfolio/trades", params=params
        )
    
    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()
        self.logger.info("Kalshi client closed")
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close() 