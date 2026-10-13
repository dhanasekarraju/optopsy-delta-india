"""Runs REAL optopsy (skipped when not installed): signals must actually fire on a clear trend."""
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("optopsy")

from optopsy_delta.original_signals import evaluate_original_rsi

END = datetime(2026, 10, 14, tzinfo=UTC)


def candles(fn, n=120):
    return [{"time": int((END - timedelta(days=n - i)).timestamp()), "close": fn(i)}
            for i in range(n)]


def test_falling_market_fires_entry_not_exit():
    sig = evaluate_original_rsi("BTC", candles(lambda i: 100000 - i * 400))
    assert sig["entry"] is True and sig["exit"] is False
    assert sig["last_complete_utc_day"] == "2026-10-13" and sig["parameters_tuned"] is False


def test_rising_market_fires_exit_not_entry():
    sig = evaluate_original_rsi("ETH", candles(lambda i: 3000 + i * 25))
    assert sig["exit"] is True and sig["entry"] is False
