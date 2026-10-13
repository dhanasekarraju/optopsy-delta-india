"""Read-only BTC/ETH long-call screen mirroring Optopsy's entry eligibility.

Upstream Optopsy chooses one closest-delta call PER EXPIRY; it does not specify
a single expiry for autonomous live trading. Do not use this as an order source.
The original Optopsy source and strategy parameters are never modified.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .snapshot import SnapshotError, _iso_datetime

TARGET_DELTA = Decimal("0.30")
MIN_DELTA = Decimal("0.20")
MAX_DELTA = Decimal("0.40")
MIN_BID_ASK = Decimal("0.05")
# Exact upstream StrategyParams: max_entry_dte=90, dte_interval=7.
# _cut_options_by_dte uses range(0, 90, 7) and pd.cut(right=True):
# its final defined upper boundary is 84, NOT 90.
DTE_EDGES = tuple(range(0, 90, 7))


class ContractScreenError(ValueError):
    """Invalid snapshot or contradictory public product data."""


def _decimal(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ContractScreenError("Missing or invalid numeric metadata")
    try:
        number = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ContractScreenError("Invalid numeric metadata") from exc
    if not number.is_finite():
        raise ContractScreenError("Non-finite numeric metadata")
    return number


def screen_long_calls(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Eligible calls, selected closest to 0.30 delta per exact expiry timestamp.

    Implements Optopsy's existing bid/ask >0.05 filter, abs(delta) in
    [0.20,0.40], nearest-to-target and lower-strike tie break. DTE is
    date-normalized like Optopsy; DTE buckets mirror the original 7-day
    pd.cut bins. Additional *operational* metadata checks reject missing
    contract value/tick or non-operational products. No single global winner.
    """
    if not isinstance(snapshot, dict) or snapshot.get("underlying") not in ("BTC", "ETH"):
        raise ContractScreenError("Snapshot must identify BTC or ETH")
    products, tickers = snapshot.get("raw_products"), snapshot.get("raw_tickers")
    if not isinstance(products, list) or not isinstance(tickers, list):
        raise ContractScreenError("Snapshot raw products and tickers must be arrays")
    try:
        observed_at = _iso_datetime(snapshot["captured_at"])
    except (KeyError, ValueError, TypeError, OverflowError, SnapshotError) as exc:
        raise ContractScreenError("Invalid snapshot collection timestamp") from exc
    underlying = snapshot["underlying"]
    by_id: dict[int, dict] = {}
    for product in products:
        if not isinstance(product, dict):
            raise ContractScreenError("Malformed product record")
        product_id = product.get("id")
        if type(product_id) is not int or product_id <= 0:
            continue
        if product_id in by_id:
            raise ContractScreenError("Duplicate product metadata ID")
        by_id[product_id] = product

    chosen: dict[str, tuple[tuple[Decimal, Decimal, int], dict[str, Any]]] = {}
    seen_tickers: set[int] = set()
    for ticker in tickers:
        if not isinstance(ticker, dict):
            raise ContractScreenError("Malformed ticker record")
        product_id = ticker.get("product_id")
        if type(product_id) is not int or product_id not in by_id:
            continue
        if product_id in seen_tickers:
            raise ContractScreenError("Duplicate product ticker ID")
        seen_tickers.add(product_id)
        p = by_id[product_id]
        if (p.get("contract_type") != "call_options"
                or p.get("state") != "live"
                or p.get("trading_status") != "operational"):
            continue
        candidates = (
            (p.get("underlying_asset") or {}).get("symbol"),
            p.get("underlying_asset_symbol"),
            p.get("contract_unit_currency"),
        )
        if underlying not in candidates:
            continue
        try:
            expiry = _iso_datetime(p.get("settlement_time") or p.get("expiry_time"))
            dte = (expiry.date() - observed_at.date()).days
            if not (DTE_EDGES[0] < dte <= DTE_EDGES[-1]):
                continue
            quote = ticker.get("quotes") or {}
            greeks = ticker.get("greeks") or {}
            bid = _decimal(quote["best_bid"])
            ask = _decimal(quote["best_ask"])
            delta = _decimal(greeks["delta"])
            strike = _decimal(p["strike_price"])
            tick = _decimal(p["tick_size"])
            multiplier = _decimal(p["contract_value"])
            if not (MIN_BID_ASK < bid <= ask and ask > MIN_BID_ASK):
                continue
            if not (MIN_DELTA <= abs(delta) <= MAX_DELTA):
                continue
            if not (strike > 0 and tick > 0 and multiplier > 0):
                continue
            if (bid % tick) != 0 or (ask % tick) != 0:
                continue
        except (KeyError, ValueError, TypeError, OverflowError,
                ContractScreenError, SnapshotError):
            continue
        upper = next(high for high in DTE_EDGES[1:] if dte <= high)
        symbol = p.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        result = {
            "underlying": underlying,
            "product_id": product_id,
            "symbol": symbol,
            "expiry_utc": expiry.isoformat(),
            "dte_days": dte,
            "dte_group": f"({upper - 7},{upper}]",
            "delta": str(delta),
            "strike": str(strike),
            "bid_usd": str(bid),
            "ask_usd": str(ask),
            "tick_size": str(tick),
            "contract_value": str(multiplier),
            "captured_at": observed_at.isoformat(),
            "live_order_authorized": False,
        }
        rank = (abs(abs(delta) - TARGET_DELTA), strike, product_id)
        key = expiry.isoformat()
        if key not in chosen or rank < chosen[key][0]:
            chosen[key] = (rank, result)
    return [x[1] for _, x in sorted(chosen.items())]


def screen_snapshot_file(path: str) -> dict[str, Any]:
    """Read a previously saved immutable snapshot; never refresh/order."""
    import json
    from pathlib import Path

    source = Path(path)
    snapshot = json.loads(source.read_text(encoding="utf-8"))
    selected = screen_long_calls(snapshot)
    return {
        "mode": "READ_ONLY_CONTRACT_SCREEN",
        "underlying": snapshot["underlying"],
        "snapshot_at": snapshot["captured_at"],
        "per_expiry_candidates": len(selected),
        "selected_per_expiry": selected,
        "note": "No unique expiry is defined by Optopsy; no trading authorized",
    }
