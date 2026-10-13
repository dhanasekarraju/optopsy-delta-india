"""Exact raw public snapshot preservation; explicitly separate Optopsy daily conversion."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SnapshotError(ValueError):
    pass


def _iso_datetime(value: Any) -> datetime:
    if value is None:
        raise SnapshotError("Missing expiry/timestamp")
    if isinstance(value, (int, float)):
        # Delta timestamps can be seconds or microseconds depending on endpoint.
        v = float(value)
        if abs(v) > 1e14:
            v /= 1_000_000
        elif abs(v) > 1e11:
            v /= 1_000
        return datetime.fromtimestamp(v, tz=UTC)
    v = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        raise SnapshotError("Naive timestamp not accepted")
    return dt.astimezone(UTC)


def snapshot_to_rows(tickers: list[dict], products: list[dict], *,
                     captured_at: datetime) -> list[dict]:
    """Map Delta quotes to Optopsy input schema, retaining full UTC precision.

    Strictly fail for metadata omissions instead of fabricating expiry/strike/delta.
    A row contains an extra Delta product ID/symbol for traceability.
    """
    if captured_at.tzinfo is None:
        raise SnapshotError("Captured time must be timezone-aware")
    by_id = {p.get("id"): p for p in products}
    rows: list[dict] = []
    for quote in tickers:
        p = by_id.get(quote.get("product_id"))
        if not p or p.get("contract_type") not in ("call_options", "put_options"):
            continue
        candidates = ((p.get("underlying_asset") or {}).get("symbol"),
                      p.get("underlying_asset_symbol"), p.get("contract_unit_currency"))
        underlying = next((c for c in candidates if c in ("BTC", "ETH")), None)
        if underlying is None:
            # Fail closed: never guess underlying from symbol prefixes.
            continue
        q = quote.get("quotes") or {}
        greeks = quote.get("greeks") or {}
        try:
            bid, ask, delta = (float(q["best_bid"]), float(q["best_ask"]), float(greeks["delta"]))
            strike = float(p.get("strike_price", quote.get("strike_price")))
            expiry = _iso_datetime(p.get("settlement_time") or p.get("expiry_time")
                                   or p.get("expiry_date"))
            if not (0 <= bid <= ask and abs(delta) <= 1 and strike > 0):
                continue
        except (KeyError, ValueError, TypeError, OverflowError, SnapshotError):
            continue
        rows.append({
            "underlying_symbol": underlying,
            "option_type": "call" if p["contract_type"] == "call_options" else "put",
            "expiration": expiry.isoformat(),
            "quote_date": captured_at.astimezone(UTC).isoformat(),
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "delta": delta,
            "delta_product_id": p["id"],
            "delta_symbol": p.get("symbol", ""),
            "volume": quote.get("volume"),
            "open_interest": quote.get("oi"),
        })
    return rows


def write_snapshot(folder: str | Path, *, underlying: str,
                   captured_at: datetime, tickers: list[dict],
                   products: list[dict]) -> Path:
    """Durable local, atomic, immutable snapshot. Never put private keys here."""
    if underlying not in ("BTC", "ETH") or captured_at.tzinfo is None:
        raise SnapshotError("Only BTC/ETH and timezone-aware captures are allowed")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = f"{underlying}-{captured_at.astimezone(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    target = folder / filename
    payload = {"source": "Delta Exchange India public API", "underlying": underlying,
               "captured_at": captured_at.astimezone(UTC).isoformat(),
               "raw_tickers": tickers, "raw_products": products,
               "normalized": snapshot_to_rows(tickers, products, captured_at=captured_at)}
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(target, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True, separators=(",", ":"), allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def snapshots_to_daily_optopsy(snapshots: list[dict]):
    """Export ONE quote per contract per UTC day for Optopsy's DAILY engine.

    NEVER treat this output as an intraday backtest. All original snapshots remain
    available separately for a future independent intraday replay.
    """
    rows = [r for s in snapshots for r in s.get("normalized", [])]
    if not rows:
        raise SnapshotError("No valid option quotes available")
    # Heavy NumPy/Pandas imports are deliberately deferred to the optional daily export.
    # Public BTC/ETH capture must work even on VPS CPUs lacking X86_V2 instructions.
    try:
        import pandas as pd
    except ImportError as exc:
        raise SnapshotError("Daily CSV export requires the optional [analysis] extra") from exc
    df = pd.DataFrame(rows)
    df["quote_date"] = pd.to_datetime(df["quote_date"], utc=True)
    df["expiration"] = pd.to_datetime(df["expiration"], utc=True)
    df = df.sort_values("quote_date", kind="stable")
    df["_utc_day"] = df["quote_date"].dt.floor("D")
    df = df.drop_duplicates(subset=["_utc_day", "delta_product_id"], keep="last")
    df = df.drop(columns="_utc_day")
    # Export only the supported columns, not exchange-specific metadata.
    return df[["underlying_symbol", "option_type", "expiration", "quote_date",
               "strike", "bid", "ask", "delta", "volume", "open_interest"]]
