"""All broker interaction tested with inert fake HTTP transport only."""
import hashlib
import hmac
import json

import pytest

from optopsy_delta.live_gateway import (
    DeltaTradingTransport,
    LiveGatewayError,
    LiveIntentJournal,
    LiveRejected,
    LiveUncertain,
    OrderIntent,
    _client_id,
    execute_once,
    validate,
)

NOW = 1791878400


def intent(**kw):
    args = {"signal_id": "signal-a", "product_id": 123, "side": "buy", "size": 1,
            "limit_price": "20", "contract_value": "0.01", "usd_inr": "85",
            "ticker_timestamp": NOW, "reduce_only": False}
    return OrderIntent(**(args | kw))


def check(**kw):
    return {"now": NOW + 2, "cash_inr": "5000", "tick_size": "0.5",
            "observed_bid": "19", "observed_ask": "20", "observed_depth": "3",
            "external_exposure": False} | kw


def test_client_order_id_stable_distinct_below_32_chars():
    a = _client_id("xyz", "buy")
    assert len(a) <= 32 and a == _client_id("xyz", "buy")
    assert a != _client_id("xyz", "sell")


def test_validate_limit_and_cap():
    assert validate(intent(), **check()) == 17
    with pytest.raises(LiveRejected, match="funds"):
        validate(intent(limit_price="6000"), **check(observed_ask="6000"))
    with pytest.raises(LiveGatewayError, match="off-tick"):
        validate(intent(limit_price="20.25"), **check())
    with pytest.raises(LiveGatewayError, match="old"):
        validate(intent(ticker_timestamp=NOW - 20), **check())
    with pytest.raises(LiveGatewayError, match="exposure"):
        validate(intent(), **check(external_exposure=True))
    with pytest.raises(LiveGatewayError, match="depth"):
        validate(intent(size=5), **check())
    with pytest.raises(LiveGatewayError, match="reduce-only"):
        validate(intent(side="sell"), **check())
    with pytest.raises(LiveGatewayError):
        validate(intent(size=True), **check())


def test_gate_fails_closed_without_live_interlocks(monkeypatch):
    monkeypatch.delenv("OPTOPSY_LIVE_ENABLED", raising=False)
    transport = DeltaTradingTransport(api_key="x", api_secret="y", live_enabled=True)
    with pytest.raises(LiveGatewayError, match="disabled"):
        transport.submit(intent())
    monkeypatch.setenv("OPTOPSY_LIVE_ENABLED", "YES_REAL_ORDERS")
    disabled = DeltaTradingTransport(api_key="x", api_secret="y", live_enabled=False)
    with pytest.raises(LiveGatewayError, match="disabled"):
        disabled.submit(intent())
    missing_keys = DeltaTradingTransport(api_key="", api_secret="", live_enabled=True)
    with pytest.raises(LiveGatewayError, match="credentials"):
        missing_keys.submit(intent())


class FakeHTTP:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


class FakeResponse:
    def __init__(self, status_code=200, obj=None):
        self.status_code = status_code
        self.obj = obj or {"success": True, "result": {"id": 5}}

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError("fake status")

    def json(self):
        return self.obj


def test_post_signature_matches_exact_json_bytes(monkeypatch):
    monkeypatch.setenv("OPTOPSY_LIVE_ENABLED", "YES_REAL_ORDERS")
    monkeypatch.setattr("optopsy_delta.live_gateway.time.time", lambda: 1542110948)
    client = _client_id("signal-a", "buy")
    http = FakeHTTP(FakeResponse(obj={"success": True, "result": {
        "client_order_id": client, "product_id": 123, "id": 451
    }}))
    adapter = DeltaTradingTransport(api_key="fake-key", api_secret="fake-secret",
                                     session=http, live_enabled=True)
    result = adapter.submit(intent())
    assert result["id"] == 451
    method, url, kw = http.calls[0]
    assert (method, url) == ("POST", "https://api.india.delta.exchange/v2/orders")
    payload = json.loads(kw["data"])
    assert payload["time_in_force"] == "ioc"
    assert payload["reduce_only"] is False
    assert len(payload["client_order_id"]) <= 32
    expected = hmac.new(b"fake-secret",
                        ("POST1542110948/v2/orders" + kw["data"]).encode(),
                        hashlib.sha256).hexdigest()
    assert kw["headers"]["signature"] == expected
    assert "fake-secret" not in repr(kw)


def test_rejected_and_timeout_distinguished(monkeypatch):
    import requests
    monkeypatch.setenv("OPTOPSY_LIVE_ENABLED", "YES_REAL_ORDERS")
    http = FakeHTTP(FakeResponse(status_code=400))
    adapter = DeltaTradingTransport(api_key="fake", api_secret="fake", session=http,
                                     live_enabled=True)
    with pytest.raises(LiveRejected):
        adapter.submit(intent())
    http.error = requests.ReadTimeout("network")
    with pytest.raises(LiveUncertain, match="unknown"):
        adapter.submit(intent())


def test_write_ahead_journal_idempotency_and_durable(tmp_path):
    path = tmp_path / "live_orders.sqlite"
    journal = LiveIntentJournal(path)
    key = journal.reserve(intent())
    assert journal.status()[0]["stage"] == "RESERVED_UNKNOWN"
    with pytest.raises(LiveGatewayError, match="already reserved"):
        journal.reserve(intent())
    with pytest.raises(LiveGatewayError, match="Conflicting"):
        journal.reserve(intent(limit_price="21"))
    assert LiveIntentJournal(path).status()[0]["client_id"] == key
    journal.note(key, "UNKNOWN")
    assert journal.status()[0]["stage"] == "UNKNOWN"
    assert path.stat().st_mode & 0o777 == 0o600


def test_execute_once_no_second_post_even_after_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTOPSY_LIVE_ENABLED", "YES_REAL_ORDERS")
    client = _client_id("signal-a", "buy")
    http = FakeHTTP(error=__import__("requests").ReadTimeout("ambiguous ack"))
    adapter = DeltaTradingTransport(api_key="fake", api_secret="fake", session=http,
                                     live_enabled=True)
    journal = LiveIntentJournal(tmp_path / "orders.sqlite")
    with pytest.raises(LiveUncertain):
        execute_once(intent(), transport=adapter, journal=journal, **check())
    assert journal.status()[0]["stage"] == "UNKNOWN"
    with pytest.raises(LiveGatewayError, match="reserved"):
        execute_once(intent(), transport=adapter, journal=journal, **check())
    assert len(http.calls) == 1
    assert journal.status()[0]["client_id"] == client


def test_ack_does_not_claim_fill(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTOPSY_LIVE_ENABLED", "YES_REAL_ORDERS")
    key = _client_id("signal-a", "buy")
    http = FakeHTTP(FakeResponse(obj={"success": True, "result": {
        "client_order_id": key, "product_id": 123, "id": 889,
        "size": 1, "unfilled_size": 1, "state": "open"
    }}))
    adapter = DeltaTradingTransport(api_key="fake", api_secret="fake", session=http,
                                     live_enabled=True)
    journal = LiveIntentJournal(tmp_path / "orders.sqlite")
    result = execute_once(intent(), transport=adapter, journal=journal, **check())
    assert result["stage"] == "ACKNOWLEDGED"
    assert result["fill_status"] == "UNVERIFIED_RECONCILIATION_REQUIRED"


def test_kill_switch_still_allows_read_only_reconciliation(monkeypatch):
    from optopsy_delta.live_gateway import reconcile_once
    monkeypatch.delenv("OPTOPSY_LIVE_ENABLED", raising=False)
    key = _client_id("signal-a", "buy")
    http = FakeHTTP(FakeResponse(obj={"success": True, "result": {
        "client_order_id": key, "id": 778, "size": 2,
        "unfilled_size": 1, "state": "cancelled"
    }}))
    adapter = DeltaTradingTransport(api_key="fake", api_secret="fake", session=http,
                                     live_enabled=False)
    from pathlib import Path
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as tmp:
        journal = LiveIntentJournal(Path(tmp) / "orders.sqlite")
        journal.reserve(intent())
        result = reconcile_once(transport=adapter, journal=journal,
                                signal_id="signal-a", side="buy")
    assert result["reported_filled_size"] == 1
    assert result["fills_and_position_verified"] is False
    assert http.calls[0][0] == "GET"


def test_direct_post_remains_off_after_kill_switch(monkeypatch):
    monkeypatch.delenv("OPTOPSY_LIVE_ENABLED", raising=False)
    client = DeltaTradingTransport(api_key="fake", api_secret="fake", live_enabled=True)
    with pytest.raises(LiveGatewayError, match="disabled"):
        client.submit(intent())
