"""Paper worker never uses a real order transport or trading API credentials."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from optopsy_delta.paper_worker import PaperBotError, PaperLedger, decide_paper, run_once

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def contract():
    return {
        "underlying": "BTC", "product_id": 44, "symbol": "C-BTC",
        "expiry_utc": (NOW + timedelta(days=8)).isoformat(),
        "contract_value": "0.01", "ask_usd": "20",
    }


def signal(entry=True, exit=False):
    return {
        "underlying": "BTC", "strategy": "long_calls",
        "entry_rule": "optopsy.rsi_below(period=14, threshold=30)",
        "exit_rule": "optopsy.rsi_above(period=14, threshold=70)",
        "parameters_tuned": False,
        "last_complete_utc_day": "2026-10-12",
        "entry": entry, "exit": exit,
    }


def tickers():
    return [{"product_id": 44, "quotes": {
        "best_bid": "19", "best_ask": "20",
        "best_ask_size": 1, "best_bid_size": 1,
    }}]


def test_paper_buy_exit_and_restart(tmp_path):
    path = tmp_path / "paper.sqlite"
    ledger = PaperLedger(path)
    entry = decide_paper(ledger=ledger, signal=signal(), contracts=[contract()],
                         quotes=tickers(), now=NOW)
    assert entry["event"] == "SIMULATED_BUY"
    assert Decimal(ledger.status()["cash_inr"]) == Decimal("4982.15")
    assert PaperLedger(path).status()["position"]["product_id"] == 44
    pending = decide_paper(ledger=PaperLedger(path),
                           signal=signal(entry=False), contracts=[],
                           quotes=tickers(), now=NOW)
    assert pending["event"] == "HOLD"
    exit_fill = decide_paper(ledger=PaperLedger(path), signal=signal(entry=False, exit=True),
                             contracts=[], quotes=tickers(), now=NOW)
    assert exit_fill["event"] == "SIMULATED_SELL"
    assert PaperLedger(path).status()["position"] is None
    assert PaperLedger(path).status()["fill_count"] == 2
    assert Decimal(PaperLedger(path).status()["cash_inr"]) == Decimal("4997.4925")


def test_duplicate_paper_fill_is_idempotent(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite")
    for index in range(2):
        result = decide_paper(ledger=ledger, signal=signal(), contracts=[contract()],
                              quotes=tickers(), now=NOW)
        assert result["event"] == ("SIMULATED_BUY" if index == 0 else "HOLD")
    assert ledger.status()["fill_count"] == 1


def test_budget_cap_and_reserve(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite")
    expensive = contract() | {"contract_value": "10", "ask_usd": "20"}
    with pytest.raises(PaperBotError, match="Insufficient"):
        decide_paper(ledger=ledger, signal=signal(), contracts=[expensive],
                     quotes=tickers(), now=NOW)
    assert ledger.status()["fill_count"] == 0


def test_invalid_signal_never_trades(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite")
    with pytest.raises(PaperBotError, match="Stale"):
        decide_paper(ledger=ledger, signal=signal() | {
            "last_complete_utc_day": "2026-10-11",
        }, contracts=[contract()], quotes=tickers(), now=NOW)
    assert ledger.status()["fill_count"] == 0


def test_missing_exit_quote_preserves_position(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite")
    decide_paper(ledger=ledger, signal=signal(), contracts=[contract()],
                 quotes=tickers(), now=NOW)
    result = decide_paper(ledger=ledger, signal=signal(entry=False, exit=True),
                          contracts=[], quotes=[], now=NOW)
    assert result["event"] == "EXIT_DUE_QUOTE_UNAVAILABLE"
    assert ledger.status()["position"]["product_id"] == 44


def test_paper_worker_runs_original_signal_adapter_with_fake_public_client(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite")
    class PublicClient:
        def __init__(self):
            self.calls = []
        def live_products(self):
            self.calls.append("products")
            return []
        def option_tickers(self, underlying):
            self.calls.append(underlying)
            return []
    client = PublicClient()
    def fake_eval(underlying, candles):
        assert candles == ["closed-day"]
        return signal(entry=False) | {"underlying": underlying}
    result = run_once(
        client=client, ledger=ledger, evaluate=fake_eval,
        load_candles=lambda underlying, now: ["closed-day"], now=NOW,
    )
    assert result["real_orders_sent"] == 0
    assert result["portfolio"]["fill_count"] == 0
    assert client.calls == ["products", "BTC", "ETH"]
