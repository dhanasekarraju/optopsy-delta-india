"""Candles are complete and RSI delegates to the actual upstream module."""
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from optopsy_delta.original_signals import (
    OriginalSignalError,
    closed_candles,
    evaluate_original_rsi,
)

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


class Response:
    def __init__(self, rows):
        self.rows = rows
    def raise_for_status(self):
        pass
    def json(self):
        return {"success": True, "result": self.rows}


class Session:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
    def get(self, url, **kw):
        self.calls.append((url, kw))
        return Response(self.rows)


def candles(n=65):
    end = datetime(2026, 10, 13, tzinfo=UTC)
    return [{"time": int((end - timedelta(days=k)).timestamp()), "close": 100 + k}
            for k in range(n, 0, -1)]


def test_exact_delta_daily_history_from_closed_days():
    s = Session(candles() + [{"time": int(datetime(2026, 10, 13, tzinfo=UTC).timestamp()),
                              "close": 123}])
    result = closed_candles("BTC", now=NOW, session=s)
    assert len(result) == 65
    assert s.calls[0][1]["params"]["resolution"] == "1d"
    assert s.calls[0][1]["params"]["symbol"] == "BTCUSD"
    assert result[-1]["time"] == int(datetime(2026, 10, 12, tzinfo=UTC).timestamp())


def test_missing_completed_candle_rejected():
    with pytest.raises(OriginalSignalError, match="missing"):
        closed_candles("ETH", now=NOW, session=Session(candles()[:-1]))


def test_original_functions_called_without_reimplementation(monkeypatch):
    import pandas as pd
    called = []
    def below(**kwargs):
        called.append(("below", kwargs))
        return "ENTRY"
    def above(**kwargs):
        called.append(("above", kwargs))
        return "EXIT"
    def dates(data, fn):
        last = data.iloc[-1]["quote_date"]
        return pd.DataFrame({"quote_date": [last] if fn == "ENTRY" else [last - pd.Timedelta(days=1)]})
    monkeypatch.setitem(sys.modules, "optopsy", SimpleNamespace(
        signal_dates=dates, rsi_below=below, rsi_above=above))
    result = evaluate_original_rsi("BTC", candles())
    assert result["entry"] is True and result["exit"] is False
    assert called == [("below", {"period": 14, "threshold": 30}),
                      ("above", {"period": 14, "threshold": 70})]
