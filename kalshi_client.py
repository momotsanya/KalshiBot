# V1.0
"""
Minimal Kalshi trade-api v2 client.

Implements the RSA-PSS request-signing scheme Kalshi requires:
  message   = timestamp_ms (str) + HTTP_METHOD (upper) + full path (no query string)
  signature = base64( RSA-PSS-SHA256(message, salt_len=digest_len) )
  headers   = KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE

IMPORTANT: the signed "path" must include the full path prefix from the base
URL (e.g. "/trade-api/v2"), not just the endpoint-relative path - e.g. for a
request to /trade-api/v2/portfolio/orders, sign "/trade-api/v2/portfolio/orders",
not just "/portfolio/orders". Public market-data endpoints don't strictly
validate the signature, so an incorrect signature there can silently appear
to work - it's endpoints under /portfolio (like order placement) that
actually enforce it and will fail with INCORRECT_API_KEY_SIGNATURE otherwise.

Docs: https://docs.kalshi.com/getting_started/api_keys
"""
from __future__ import annotations

import base64
import time
import logging
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

log = logging.getLogger("kalshi_client")


class KalshiAuthError(Exception):
    pass


class KalshiAPIError(Exception):
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Kalshi API error {status_code}: {body}")


class KalshiClient:
    def __init__(self, key_id: str, private_key_path: str, base_url: str, timeout: float = 10.0):
        self.key_id = key_id
        self.base_url = base_url.rstrip("/")
        # The path prefix (e.g. "/trade-api/v2") baked into base_url - included
        # in the SIGNED message per Kalshi's docs, even though it's already
        # part of base_url for building the actual request URL.
        self._sign_path_prefix = urlparse(self.base_url).path
        self.timeout = timeout
        self._session = requests.Session()
        self._private_key = self._load_private_key(private_key_path)

    @staticmethod
    def _load_private_key(path: str):
        try:
            with open(path, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
        except FileNotFoundError as e:
            raise KalshiAuthError(
                f"Private key file not found at '{path}'. Download it from your Kalshi API key settings."
            ) from e

    def _sign(self, timestamp_ms: str, method: str, path_no_query: str) -> str:
        message = (timestamp_ms + method.upper() + path_no_query).encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path_no_query: str) -> dict:
        ts = str(int(time.time() * 1000))
        full_path = self._sign_path_prefix + path_no_query  # e.g. "/trade-api/v2" + "/portfolio/events/orders"
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, full_path),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None):
        url = self.base_url + path
        headers = self._headers(method, path)  # path must exclude query string, per docs
        resp = self._session.request(method, url, headers=headers, params=params, json=json_body, timeout=self.timeout)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise KalshiAPIError(resp.status_code, body)
        if resp.content:
            return resp.json()
        return None

    # ---------- Market data ----------

    def get_markets(self, series_ticker: str, status: Optional[str] = None, limit: int = 50) -> list[dict]:
        params = {"series_ticker": series_ticker, "limit": limit}
        if status:
            params["status"] = status
        data = self._request("GET", "/markets", params=params)
        return data.get("markets", []) if data else []

    def get_market(self, ticker: str) -> dict:
        data = self._request("GET", f"/markets/{ticker}")
        return data["market"]

    def get_orderbook(self, ticker: str, depth: Optional[int] = 5) -> dict:
        params = {"depth": depth} if depth else None
        data = self._request("GET", f"/markets/{ticker}/orderbook", params=params)
        return data.get("orderbook_fp") or data.get("orderbook") or {}

    def get_markets_history(
        self,
        series_ticker: str,
        status: Optional[str] = "settled",
        min_close_ts: Optional[int] = None,
        max_close_ts: Optional[int] = None,
        limit: int = 1000,
        max_pages: int = 200,
    ) -> list[dict]:
        """
        Fetch ALL markets matching the filters, following pagination cursors
        (unlike get_markets, which only returns a single page). Used for
        backtesting, where we need full history rather than just the most
        recent markets.
        """
        all_markets = []
        cursor = None
        for _ in range(max_pages):
            params = {"series_ticker": series_ticker, "limit": limit}
            if status:
                params["status"] = status
            if min_close_ts is not None:
                params["min_close_ts"] = min_close_ts
            if max_close_ts is not None:
                params["max_close_ts"] = max_close_ts
            if cursor:
                params["cursor"] = cursor

            data = self._request("GET", "/markets", params=params)
            if not data:
                break
            batch = data.get("markets", [])
            all_markets.extend(batch)

            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return all_markets

    def get_candlesticks(
        self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1,
    ) -> list[dict]:
        """Live endpoint - only covers roughly the last 3 months (see /historical/cutoff)."""
        params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
        data = self._request("GET", f"/series/{series_ticker}/markets/{ticker}/candlesticks", params=params)
        return data.get("candlesticks", []) if data else []

    def get_historical_candlesticks(
        self, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1,
    ) -> list[dict]:
        """Fallback for markets older than the live cutoff (archived data)."""
        params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
        data = self._request("GET", f"/historical/markets/{ticker}/candlesticks", params=params)
        return data.get("candlesticks", []) if data else []

    def get_candlesticks_auto(
        self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1,
    ) -> list[dict]:
        """Try the live endpoint first; fall back to the historical/archived endpoint on failure."""
        try:
            candles = self.get_candlesticks(series_ticker, ticker, start_ts, end_ts, period_interval)
            if candles:
                return candles
        except KalshiAPIError:
            pass
        try:
            return self.get_historical_candlesticks(ticker, start_ts, end_ts, period_interval)
        except KalshiAPIError:
            return []

    # ---------- Portfolio / trading ----------

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> dict:
        return self._request("GET", "/portfolio/positions")

    def create_order(
        self,
        ticker: str,
        side: str,        # "yes" or "no" - our natural side; translated to V2's bid/ask below
        count: int,
        price_cents: int,  # price for `side` (as before) - translated to V2's YES-side price below
        client_order_id: str,
        time_in_force: str = "immediate_or_cancel",
        self_trade_prevention_type: str = "taker_at_cross",
        exchange_index: Optional[int] = None,
    ) -> dict:
        """
        Places an order via Kalshi's V2 endpoint (POST /portfolio/events/orders).
        The old /portfolio/orders endpoint is deprecated (returns HTTP 410).

        V2 unifies the orderbook to the YES side only: 'bid' = buy YES,
        'ask' = sell YES. Buying NO at price P is economically identical to
        selling (shorting) YES at price (1 - P), so a caller's side='no' is
        expressed here as an 'ask' at the complementary price - the caller
        still just passes their natural side/price for that side; this
        function does the translation.

        time_in_force defaults to "immediate_or_cancel": try to fill at/better
        than price_cents right away, cancel whatever doesn't fill immediately,
        matching this bot's existing "check price, then place" behavior rather
        than leaving a resting order that could fill much later at a stale
        decision. self_trade_prevention_type defaults to "taker_at_cross" (a
        safe default for automated systems that aren't intentionally trading
        against their own resting orders).
        """
        if side == "yes":
            book_side = "bid"
            yes_price_dollars = price_cents / 100.0
        elif side == "no":
            book_side = "ask"
            yes_price_dollars = (100 - price_cents) / 100.0
        else:
            raise ValueError(f"Unknown side: {side}")

        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": str(int(count)),
            "price": f"{yes_price_dollars:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
        }

        if exchange_index is not None:
            body["exchange_index"] = exchange_index

        return self._request("POST", "/portfolio/events/orders", json_body=body)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/portfolio/orders/{order_id}")

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
