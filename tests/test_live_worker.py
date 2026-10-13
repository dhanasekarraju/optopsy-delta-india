"""Live worker behaviour against a FAKE exchange: no network, no keys, no real orders."""
from datetime import UTC, datetime, timedelta

import pytest

from optopsy_delta.live_gateway import (
    DeltaTradingTransport,
    LiveIntentJournal,
    LiveUncertain,
    _client_id,
)
from optopsy_delta.live_worker import AttemptBook, LiveWorkerError, run_once
from optopsy_delta.mode import ModeError

NOW = datetime(2026, 10, 13, 8, 30, tzinfo=UTC)
YESTERDAY = (NOW.date() - timedelta(days=1)).isoformat()


def product(pid, *, days=7):
    return {
        "id": pid, "symbol": f"C-BTC-{pid}", "contract_type": "call_options", "state": "live",
        "trading_status": "operational", "underlying_asset": {"symbol": "BTC"},
        "settlement_time": (NOW + timedelta(days=days)).replace(hour=12).isoformat(),
        "strike_price": "90000", "tick_size": "0.5", "contract_value": "0.001",
    }


def ticker(pid, *, age=2, bid="12", ask="13"):
    return {
        "product_id": pid, "greeks": {"delta": "0.3"},
        "timestamp": int((NOW - timedelta(seconds=age)).timestamp() * 1_000_000),
        "quotes": {"best_bid": bid, "best_ask": ask, "best_bid_size": "5", "best_ask_size": "5"},
    }


def wallet():
    return [{"asset_symbol": "USD", "balance": "100", "available_balance": "100",
             "blocked_margin": "0", "order_margin": "0", "position_margin": "0"}]


class FakeClient:
    def __init__(self, products, tickers, *, wallets=None, positions=None, sizes=None):
        self.products, self.tickers = products, tickers
        self.wallets = wallet() if wallets is None else wallets
        self.positions = [] if positions is None else positions
        self.sizes = {} if sizes is None else sizes

    def live_products(self):
        return self.products

    def option_tickers(self, underlying="BTC", expiry_date=None):
        return self.tickers

    def wallet_balances(self):
        return self.wallets

    def open_positions(self):
        return self.positions

    def active_orders(self, **_):
        return []

    def product_position(self, pid):
        return {"size": self.sizes.get(pid, 0)}


class FakeTransport(DeltaTradingTransport):
    def __init__(self, *, filled=1, fail_submit=False):
        super().__init__(api_key="k", api_secret="s", live_enabled=True)
        self.sent, self.filled, self.fail_submit = [], filled, fail_submit

    def submit(self, intent):
        if self.fail_submit:
            raise LiveUncertain("network dropped")
        self.sent.append(intent)
        return {"client_order_id": _client_id(intent.signal_id, intent.side),
                "product_id": intent.product_id, "id": 1000 + len(self.sent)}

    def lookup(self, *, signal_id, side):
        intent = next(i for i in self.sent if i.signal_id == signal_id)
        return {"client_order_id": _client_id(signal_id, side), "id": 1001, "state": "closed",
                "size": intent.size, "unfilled_size": intent.size - self.filled}


def signal(entry=False, exit_=False, underlying="BTC"):
    return {"strategy": "long_calls", "parameters_tuned": False, "underlying": underlying,
            "entry_rule": "optopsy.rsi_below(period=14, threshold=30)",
            "exit_rule": "optopsy.rsi_above(period=14, threshold=70)",
            "last_complete_utc_day": YESTERDAY, "entry": entry, "exit": exit_}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTOPSY_LIVE_ENABLED", "YES_REAL_ORDERS")
    config = tmp_path / "config.toml"
    config.write_text("paper_enabled = false\n")
    return {"config_path": str(config), "data_dir": tmp_path,
            "journal": LiveIntentJournal(tmp_path / "j.sqlite"),
            "book": AttemptBook(tmp_path / "a.sqlite")}


def cycle(env, client, transport, *, sig, now=NOW, evaluate=None):
    return run_once(
        client=client, transport=transport, journal=env["journal"], book=env["book"],
        data_dir=env["data_dir"], config_path=env["config_path"], now=now,
        evaluate=evaluate or (lambda und, _c: sig(und)), load_candles=lambda und, now: None,
    )


def entry_signal(und):
    return signal(entry=(und == "BTC"), underlying=und)


def test_both_interlocks_required(env, monkeypatch, tmp_path):
    client, transport = FakeClient([product(1)], [ticker(1)]), FakeTransport()
    monkeypatch.delenv("OPTOPSY_LIVE_ENABLED")
    with pytest.raises(ModeError, match="LIVE_ENV_INTERLOCK"):
        cycle(env, client, transport, sig=entry_signal)
    monkeypatch.setenv("OPTOPSY_LIVE_ENABLED", "YES_REAL_ORDERS")
    (tmp_path / "config.toml").write_text("paper_enabled = true\n")
    with pytest.raises(ModeError, match="PAPER_MODE_ACTIVE"):
        cycle(env, client, transport, sig=entry_signal)
    assert transport.sent == []


def test_entry_sends_one_ioc_limit_buy_at_ask_and_reconciles(env):
    client, transport = FakeClient([product(1)], [ticker(1)]), FakeTransport()
    out = cycle(env, client, transport, sig=entry_signal)
    assert out["event"] == "ORDER_SUBMITTED" and out["side"] == "buy"
    assert len(transport.sent) == 1
    order = transport.sent[0]
    assert (order.product_id, order.size, order.limit_price, order.reduce_only) == (
        1, 1, "13", False)
    assert env["book"].entry_products() == {1: "BTC"}
    # Same signal day again: broker shows a position, so we hold and never buy twice.
    client.sizes = {1: 1}
    out = cycle(env, client, transport, sig=entry_signal)
    assert out["event"] == "HOLD" and len(transport.sent) == 1


def test_no_second_entry_attempt_same_day_after_unfilled_ioc(env):
    client, transport = FakeClient([product(1)], [ticker(1)]), FakeTransport(filled=0)
    cycle(env, client, transport, sig=entry_signal)
    out = cycle(env, client, transport, sig=entry_signal)
    assert out["event"] == "ALREADY_ATTEMPTED" and len(transport.sent) == 1


def test_entry_blocked_when_account_not_clear(env):
    client = FakeClient([product(1)], [ticker(1)], positions=[{"product_id": 9, "size": 1}])
    client.sizes = {9: 1}
    transport = FakeTransport()
    with pytest.raises(LiveWorkerError, match="UNKNOWN_POSITION"):
        cycle(env, client, transport, sig=entry_signal)
    client.positions, client.sizes = [], {}
    client.wallets[0]["position_margin"] = "5"
    out = cycle(env, client, transport, sig=entry_signal)
    assert out["event"] == "ENTRY_BLOCKED_ACCOUNT_NOT_CLEAR" and transport.sent == []


def test_stale_ticker_never_creates_order_or_attempt(env):
    client, transport = FakeClient([product(1)], [ticker(1, age=60)]), FakeTransport()
    with pytest.raises(Exception, match="Ticker too old"):
        cycle(env, client, transport, sig=entry_signal)
    assert transport.sent == [] and env["book"].unresolved() == []


def test_rsi_exit_sells_reduce_only_at_bid(env):
    client, transport = FakeClient([product(1)], [ticker(1)]), FakeTransport()
    cycle(env, client, transport, sig=entry_signal)
    client.sizes = {1: 1}
    out = cycle(env, client, transport, sig=lambda und: signal(exit_=True, underlying=und))
    assert out["event"] == "ORDER_SUBMITTED" and out["reason"] == "SIGNAL_EXIT"
    sell = transport.sent[-1]
    assert (sell.side, sell.size, sell.limit_price, sell.reduce_only) == ("sell", 1, "12", True)


def test_pre_expiry_exit_works_even_if_signal_fails(env):
    client, transport = FakeClient([product(1, days=7)], [ticker(1)]), FakeTransport()
    cycle(env, client, transport, sig=entry_signal)
    client.sizes = {1: 1}

    def broken(*_):
        raise RuntimeError("candle feed down")

    later = NOW + timedelta(days=6, hours=5)  # past expiry-24h (expiry 12:00 UTC)
    client.tickers = [ticker(1)]
    client.tickers[0]["timestamp"] = int(later.timestamp() * 1_000_000)
    out = cycle(env, client, transport, sig=None, now=later, evaluate=broken)
    assert out["reason"] == "PRE_EXPIRY_EXIT" and out["signal_error"] == "RuntimeError"
    assert transport.sent[-1].side == "sell"


def test_unknown_submit_outcome_blocks_all_new_orders(env):
    client, transport = FakeClient([product(1)], [ticker(1)]), FakeTransport(fail_submit=True)
    out = cycle(env, client, transport, sig=entry_signal)
    assert out["event"] == "ORDER_NOT_CONFIRMED" and out["journal_stage"] == "UNKNOWN"
    # Exchange lookup cannot find the order: stay stopped, never resubmit.
    transport.fail_submit = False
    out = cycle(env, client, transport, sig=entry_signal)
    assert out["event"] == "RECOVERY_REQUIRED_RECONCILE_FAILED"
    assert transport.sent == []


def test_standby_when_not_enabled_writes_heartbeat_and_never_trades(tmp_path, monkeypatch):
    import json

    from optopsy_delta.live_worker import main
    monkeypatch.delenv("OPTOPSY_LIVE_ENABLED", raising=False)
    config = tmp_path / "config.toml"
    config.write_text("paper_enabled = false\n")
    code = main(["--once", "--data-dir", str(tmp_path / "d"), "--config", str(config)])
    beat = json.loads((tmp_path / "d" / "live-heartbeat.json").read_text())
    assert code == 3 and beat["event"] == "LIVE_STANDBY_NOT_ENABLED"
    assert beat["real_orders_sent"] == 0
