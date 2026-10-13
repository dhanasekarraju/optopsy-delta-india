"""Read-only daily RSI signals from UNMODIFIED upstream Optopsy functions.

No reimplementation of RSI or backtest mechanics. A compatible research Python
runtime with optopsy==2.3.0 must be installed separately from the lightweight
VPS broker client, because this host may lack NumPy's required CPU features.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

import requests


class OriginalSignalError(RuntimeError):
    pass


def closed_candles(
    underlying: str, *, now: datetime, session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    if underlying not in ("BTC", "ETH"):
        raise OriginalSignalError("Only BTC and ETH allowed")
    if now.tzinfo is None:
        raise OriginalSignalError("UTC-aware evaluation time required")
    today = now.astimezone(UTC).date()
    end = datetime(today.year, today.month, today.day, tzinfo=UTC)
    start = end - timedelta(days=180)
    client = session or requests.Session()
    try:
        response = client.get(
            "https://api.india.delta.exchange/v2/history/candles",
            params={"resolution": "1d", "symbol": underlying + "USD",
                    "start": int(start.timestamp()), "end": int(end.timestamp())},
            timeout=(3.05, 8),
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise OriginalSignalError("Delta daily candle history unavailable") from exc
    if not isinstance(body, dict) or body.get("success") is not True:
        raise OriginalSignalError("Unexpected Delta candle response")
    rows = body.get("result")
    if not isinstance(rows, list):
        raise OriginalSignalError("No candle collection")
    keyed: dict[int, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            raise OriginalSignalError("Malformed candle")
        try:
            unix = int(item["time"])
            close = float(item["close"])
            day = datetime.fromtimestamp(unix, tz=UTC)
            if day.hour or day.minute or day.second:
                raise ValueError("Non-daily timestamp")
            if close <= 0 or not isfinite(close):
                raise ValueError("Bad close")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise OriginalSignalError("Invalid candle data") from exc
        if unix >= int(end.timestamp()):
            continue  # Current, incomplete UTC day is never used.
        if unix in keyed:
            raise OriginalSignalError("Duplicate candle timestamps")
        keyed[unix] = item
    ordered = [keyed[t] for t in sorted(keyed)]
    if len(ordered) < 60:
        raise OriginalSignalError("Not enough fully closed daily candles")
    if ordered[-1]["time"] != int((end - timedelta(days=1)).timestamp()):
        raise OriginalSignalError("Last completed candle missing")
    if any(int(y["time"]) - int(x["time"]) != 86400
           for x, y in zip(ordered[:-1], ordered[1:])):
        raise OriginalSignalError("Gap in daily candles")
    return ordered


def evaluate_original_rsi(
    underlying: str, candles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate the actual Optopsy rsi_below/above implementation, unmodified."""
    if underlying not in ("BTC", "ETH") or len(candles) < 60:
        raise OriginalSignalError("BTC/ETH and at least 60 closed candles required")
    try:
        import optopsy as op
        import pandas as pd
    except ImportError as exc:
        raise OriginalSignalError(
            "Original signals require optopsy==2.3.0 on a compatible research CPU"
        ) from exc
    data = pd.DataFrame({
        "underlying_symbol": underlying,
        "quote_date": pd.to_datetime([row["time"] for row in candles], unit="s", utc=True),
        "close": [float(row["close"]) for row in candles],
    })
    # Parameters exactly match the published Optopsy entry-signals examples.
    entering = op.signal_dates(data, op.rsi_below(period=14, threshold=30))
    exiting = op.signal_dates(data, op.rsi_above(period=14, threshold=70))
    date = data.iloc[-1]["quote_date"].floor("D")
    is_entry = bool((entering["quote_date"] == date).any())
    is_exit = bool((exiting["quote_date"] == date).any())
    if is_entry and is_exit:
        raise OriginalSignalError("Contradictory original signals")
    return {
        "underlying": underlying,
        "last_complete_utc_day": str(date.date()),
        "entry": is_entry,
        "exit": is_exit,
        "strategy": "long_calls",
        "entry_rule": "optopsy.rsi_below(period=14, threshold=30)",
        "exit_rule": "optopsy.rsi_above(period=14, threshold=70)",
        "parameters_tuned": False,
    }
