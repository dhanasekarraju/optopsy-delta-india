"""Minimal read-only Delta India REST client with HMAC SHA256 authentication.

No create/edit/cancel order method deliberately: a research library supplies no
live signal policy or autonomous protective-exit supervision. Implementing order
placement here would misleadingly imply that an unattended bot is safe to run.
"""
from __future__ import annotations

import hashlib
import hmac
import contextlib
import os
import re
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


def _diagnostic(path: str, response, exc: Exception) -> str:
    status = getattr(response, "status_code", None)
    code = None
    with contextlib.suppress(Exception):  # diagnostics must never raise
        err = (response.json() or {}).get("error")
        raw = err.get("code") if isinstance(err, dict) else err
        if isinstance(raw, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", raw):
            code = raw
    parts = [f"GET {path}", f"status={status}" if status else f"error={type(exc).__name__}"]
    if code:
        parts.append(f"delta_code={code}")
    return " [" + " ".join(parts) + "]"


class DeltaClient:
    def __init__(self, *, api_key: str | None = None, api_secret: str | None = None,
                 session: requests.Session | None = None) -> None:
        self._key = api_key if api_key is not None else os.getenv("DELTA_INDIA_API_KEY", "")
        self._secret = api_secret if api_secret is not None else os.getenv("DELTA_INDIA_API_SECRET", "")
        self._session = session or requests.Session()

    def _get(self, path: str, *, params: Mapping[str, str] | None = None,
             signed: bool = False, envelope: bool = False) -> Any:
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
        response = None
        try:
            response = self._session.get(BASE_URL + target, headers=headers, timeout=TIMEOUT)
            response.raise_for_status()
            obj = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Never include URLs/headers/bodies. Only the path, HTTP status, and Delta's short
            # error code (e.g. ip_not_whitelisted_for_api_key) are appended for diagnosis.
            raise DeltaError("Delta request failed; inspect connectivity and API permissions"
                             + _diagnostic(path, response, exc)) from exc
        if not isinstance(obj, dict) or obj.get("success") is not True or "result" not in obj:
            raise DeltaError("Delta returned an unsuccessful or unrecognized API response")
        return obj if envelope else obj["result"]

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

    def active_orders(self, *, max_pages: int = 40) -> list[dict]:
        """Follow every private active-order page; reject truncation."""
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("Invalid active-order pagination limit")
        all_orders: list[dict] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(max_pages):
            params = {"states": "open,pending", "page_size": "50"}
            if cursor is not None:
                params["after"] = cursor
            envelope = self._get("/v2/orders", params=params, signed=True, envelope=True)
            page, meta = envelope["result"], envelope.get("meta")
            if not isinstance(page, list) or not isinstance(meta, dict):
                raise DeltaError("Unverifiable active-order pagination response")
            for row in page:
                if not isinstance(row, dict) or type(row.get("id")) is not int:
                    raise DeltaError("Invalid active-order record")
                ident = str(row["id"])
                if ident in seen_ids or row.get("state") not in ("open", "pending"):
                    raise DeltaError("Duplicate or unknown active-order record")
                seen_ids.add(ident)
                all_orders.append(row)
            next_cursor = meta.get("after")
            if next_cursor is None or next_cursor == "":
                return all_orders
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                raise DeltaError("Repeated or invalid active-order cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise DeltaError("Incomplete active order history: pagination safety limit")

    def order_by_client_id(self, client_id: str) -> dict:
        """Signed, read-only lookup of a previously registered exit order."""
        import re

        if not isinstance(client_id, str) or re.fullmatch(r"os-[0-9a-f]{28}", client_id) is None:
            raise ValueError("Invalid bot exit client order ID")
        result = self._get("/v2/orders/client_order_id/" + client_id, signed=True)
        if not isinstance(result, dict) or result.get("client_order_id") != client_id:
            raise DeltaError("Mismatched client order lookup response")
        return result

    def order_by_id(self, order_id: int) -> dict:
        """Signed single-order lookup, no submission or cancellation."""
        if type(order_id) is not int or order_id <= 0:
            raise ValueError("Invalid broker order ID")
        result = self._get("/v2/orders/" + str(order_id), signed=True)
        if not isinstance(result, dict) or result.get("id") != order_id:
            raise DeltaError("Mismatched single-order response")
        return result

    def product_position(self, product_id: int) -> dict:
        """Get current per-product position; general margined list may lag."""
        if type(product_id) is not int or product_id <= 0:
            raise ValueError("Invalid product ID")
        result = self._get("/v2/positions", params={"product_id": str(product_id)}, signed=True)
        if not isinstance(result, dict) or "size" not in result:
            raise DeltaError("Invalid real-time position response")
        return result

    def fills_for_product(
        self, product_id: int, *, start_time_us: int, end_time_us: int,
        max_pages: int = 20,
    ) -> list[dict]:
        """Exhaustively read a *bounded interval* of authenticated fills.

        Callers must not consider orders filled unless ALL pages have been read.
        No place/edit/cancel requests exist in this read-only API client.
        """
        if type(product_id) is not int or product_id <= 0:
            raise ValueError("Invalid product ID")
        if (type(start_time_us) is not int or type(end_time_us) is not int
                or start_time_us <= 0 or end_time_us <= start_time_us):
            raise ValueError("Invalid microsecond fill time interval")
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("Invalid page cap")
        records: list[dict] = []
        seen_cursors: set[str] = set()
        seen_fills: set[str] = set()
        after: str | None = None
        for _ in range(max_pages):
            params = {
                "product_ids": str(product_id), "start_time": str(start_time_us),
                "end_time": str(end_time_us), "page_size": "50",
            }
            if after is not None:
                params["after"] = after
            response = self._get("/v2/fills", params=params, signed=True, envelope=True)
            page, meta = response["result"], response.get("meta")
            if not isinstance(page, list) or not isinstance(meta, dict):
                raise DeltaError("Missing fills page or pagination metadata")
            for fill in page:
                if not isinstance(fill, dict):
                    raise DeltaError("Malformed fill record")
                ident = str(fill.get("id", ""))
                if not ident or ident in seen_fills:
                    raise DeltaError("Missing or duplicate fill ID")
                if str(fill.get("product_id")) != str(product_id):
                    raise DeltaError("Fill from unexpected product")
                if not isinstance(fill.get("order_id"), (int, str)) or not str(
                    fill["order_id"]
                ):
                    raise DeltaError("Fill missing order ID")
                seen_fills.add(ident)
                records.append(fill)
            cursor = meta.get("after")
            if cursor is None or cursor == "":
                return records
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise DeltaError("Repeated or invalid fills cursor")
            seen_cursors.add(cursor)
            after = cursor
        raise DeltaError("Incomplete fill history: pagination safety limit")
