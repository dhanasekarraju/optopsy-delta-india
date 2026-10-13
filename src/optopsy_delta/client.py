"""Minimal read-only Delta India REST client with HMAC SHA256 authentication.

No create/edit/cancel order method deliberately: a research library supplies no
live signal policy or autonomous protective-exit supervision. Implementing order
placement here would misleadingly imply that an unattended bot is safe to run.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import requests

BASE_URL = "https://api.india.delta.exchange"
PREFIX = "/v2"
TIMEOUT = (3.05, 8)


class DeltaError(RuntimeError):
    """Delta API or validation failure. Never embed request headers/keys."""


class SignatureError(DeltaError):
    pass


def sign_message(secret: str, method: str, timestamp: str, path: str,
                 query_string: str = "", body: str = "") -> str:
    if not secret:
        raise SignatureError("API secret missing")
    prehash = f"{method.upper()}{timestamp}{path}{query_string}{body}"
    return hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).hexdigest()


class DeltaClient:
    def __init__(self, *, api_key: str | None = None, api_secret: str | None = None,
                 session: requests.Session | None = None) -> None:
        self._key = api_key if api_key is not None else os.getenv("DELTA_INDIA_API_KEY", "")
        self._secret = api_secret if api_secret is not None else os.getenv("DELTA_INDIA_API_SECRET", "")
        self._session = session or requests.Session()

    def _get(self, path: str, *, params: Mapping[str, str] | None = None,
             signed: bool = False) -> Any:
        if not path.startswith(PREFIX + "/") or any(x in path for x in ("?", "#", "..")):
            raise ValueError("Only known Delta v2 endpoint paths are permitted")
        params = params or {}
        query = urlencode(sorted((str(k), str(v)) for k, v in params.items()))
        target = path + (("?" + query) if query else "")
        headers = {"Accept": "application/json", "User-Agent": "optopsy-delta-india/0.1.0"}
        if signed:
            if not self._key or not self._secret:
                raise SignatureError("Delta API credentials are required for private reads")
            timestamp = str(int(time.time()))
            headers.update({
                "api-key": self._key,
                "timestamp": timestamp,
                "signature": sign_message(self._secret, "GET", timestamp, path,
                                          ("?" + query) if query else ""),
            })
        try:
            response = self._session.get(BASE_URL + target, headers=headers, timeout=TIMEOUT)
            response.raise_for_status()
            obj = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Deliberately omit exception details (may contain request URL/metadata).
            raise DeltaError("Delta request failed; inspect connectivity and API permissions") from exc
        if not isinstance(obj, dict) or obj.get("success") is not True or "result" not in obj:
            raise DeltaError("Delta returned an unsuccessful or unrecognized API response")
        return obj["result"]

    def option_tickers(self, underlying: str = "BTC", expiry_date: str | None = None) -> list[dict]:
        underlying = underlying.upper()
        if underlying not in {"BTC", "ETH"}:
            raise ValueError("BTC and ETH options only")
        params = {"contract_types": "call_options,put_options",
                  "underlying_asset_symbols": underlying}
        if expiry_date is not None:
            # Delta accepts DD-MM-YYYY here, unlike products endpoint.
            from datetime import datetime
            if datetime.strptime(expiry_date, "%d-%m-%Y").strftime("%d-%m-%Y") != expiry_date:
                raise ValueError("expiry_date must be DD-MM-YYYY")
            params["expiry_date"] = expiry_date
        result = self._get("/v2/tickers", params=params)
        if not isinstance(result, list):
            raise DeltaError("Unexpected tickers response")
        return result

    def product(self, symbol: str) -> dict:
        # No user-supplied URL path characters.
        if not symbol or not all(c.isalnum() or c in "-_" for c in symbol):
            raise ValueError("Invalid product symbol")
        result = self._get("/v2/products/" + symbol)
        if not isinstance(result, dict):
            raise DeltaError("Unexpected product response")
        return result

    def live_products(self) -> list[dict]:
        """Fetch all pages of live options; fail if pagination cannot be reconciled."""
        results: list[dict] = []
        seen: set[str] = set()
        after = None
        for _ in range(100):
            params = {"contract_types": "call_options,put_options", "states": "live",
                      "page_size": "100"}
            if after:
                params["after"] = after
            # Endpoint can include pagination in 'meta', so preserve response envelope below.
            envelope = self._get_envelope("/v2/products", params=params)
            page = envelope["result"]
            if not isinstance(page, list):
                raise DeltaError("Unexpected products response")
            results.extend(page)
            cursor = (envelope.get("meta") or {}).get("after")
            if not cursor:
                return results
            if not isinstance(cursor, str) or cursor in seen:
                raise DeltaError("Invalid/repeated product pagination cursor")
            seen.add(cursor)
            after = cursor
        raise DeltaError("Product pagination exceeded maximum pages")

    def _get_envelope(self, path: str, *, params: Mapping[str, str]) -> dict:
        # Same authenticated-request restrictions as _get, but return 'meta'.
        # Public products endpoint only; all other calls use _get.
        if path != "/v2/products":
            raise ValueError("Only products support envelope reads")
        query = urlencode(sorted(params.items()))
        try:
            response = self._session.get(BASE_URL + path + "?" + query,
                    headers={"Accept": "application/json", "User-Agent": "optopsy-delta-india/0.1.0"},
                    timeout=TIMEOUT)
            response.raise_for_status()
            obj = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DeltaError("Delta products request failed") from exc
        if not isinstance(obj, dict) or obj.get("success") is not True or not isinstance(obj.get("result"), list):
            raise DeltaError("Invalid Delta products response")
        return obj

    def wallet_balances(self) -> list[dict]:
        result = self._get("/v2/wallet/balances", signed=True)
        if not isinstance(result, list):
            raise DeltaError("Unexpected wallet response")
        return result

    def open_positions(self) -> Any:
        return self._get("/v2/positions/margined", signed=True)

    def active_orders(self) -> list[dict]:
        result = self._get("/v2/orders", params={"states": "open,pending"}, signed=True)
        if not isinstance(result, list):
            raise DeltaError("Unexpected order response")
        return result
