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
    
    async def _make_authenticated_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        require_auth: bool = True
    ) -> Dict[str, Any]:
        """
        Make authenticated request to Kalshi API with retry logic.
        
        Args:
            method: HTTP method
            endpoint: API endpoint
            params: Query parameters
            json_data: JSON request body
            require_auth: Whether authentication is required
        
        Returns:
            API response data
        """
        # Prepare request
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # Add authentication headers if required
        if require_auth:
            # Get current timestamp in milliseconds
            timestamp = str(int(time.time() * 1000))
            
            # Create signature
            signature = self._sign_request(timestamp, method, endpoint)
            
            headers.update({
                "KALSHI-ACCESS-KEY": self.api_key,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": signature
            })
        
        # Prepare body
        body = None
        if json_data:
            body = json.dumps(json_data, separators=(',', ':'))
        
        # Add query parameters to URL if present
        if params:
            query_string = urlencode(params)
            url = f"{url}?{query_string}"
        
        last_exception = None
        for attempt in range(self.max_retries):
            try:
                self.logger.debug(
                    "Making API request",
                    method=method,
                    endpoint=endpoint,
                    has_auth=require_auth,
                    attempt=attempt + 1
                )
                
                # Rate limit delay to prevent 429s (200ms = 5 req/s)
                await asyncio.sleep(0.2)
                
                response = await self.client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=body if body else None
                )
                
                response.raise_for_status()
                return response.json()
                
            except httpx.HTTPStatusError as e:
                last_exception = e
                # Rate limit (429) or server errors (5xx) are worth retrying
                if e.response.status_code == 429 or e.response.status_code >= 500:
                    sleep_time = self.backoff_factor * (2 ** attempt)
                    self.logger.warning(
                        f"API request failed with status {e.response.status_code}. Retrying in {sleep_time:.2f}s...",
                        endpoint=endpoint,
                        attempt=attempt + 1
                    )
                    await asyncio.sleep(sleep_time)
                else:
                    # Don't retry on other client errors (e.g., 400, 401, 404)
                    error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
                    self.logger.error("API request failed without retry", error=error_msg, endpoint=endpoint)
                    raise KalshiAPIError(error_msg)
            except Exception as e:
                last_exception = e
                self.logger.warning(f"Request failed with general exception. Retrying...", error=str(e), endpoint=endpoint)
                sleep_time = self.backoff_factor * (2 ** attempt)
                await asyncio.sleep(sleep_time)
        
        raise KalshiAPIError(f"API request failed after {self.max_retries} retries: {last_exception}")
    
    async def get_balance(self) -> Dict[str, Any]:
        """Get account balance."""
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/balance")
    
    async def get_positions(self, ticker: Optional[str] = None) -> Dict[str, Any]:
        """Get portfolio positions."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/positions", params=params)
    
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
    
    async def get_orders(self, ticker: Optional[str] = None, status: Optional[str] = None) -> Dict[str, Any]:
        """Get orders."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/orders", params=params)
    
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
    
    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an order."""
        return await self._make_authenticated_request(
            "DELETE", f"/trade-api/v2/portfolio/orders/{order_id}"
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