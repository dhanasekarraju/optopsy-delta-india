"""Delta errors expose only path/status/short code, never keys, URLs or headers."""
import pytest
import requests

from optopsy_delta.client import DeltaClient, DeltaError
from optopsy_delta.dashboard import _read_live_heartbeat


class Resp:
    status_code = 401

    def raise_for_status(self):
        raise requests.HTTPError("401 for url https://secret-url")

    def json(self):
        return {"success": False, "error": {"code": "ip_not_whitelisted_for_api_key"}}


class Session:
    def get(self, *_a, **_k):
        return Resp()


def test_error_includes_status_and_code_but_no_secrets():
    client = DeltaClient(api_key="KEYVALUE", api_secret="SECRETVALUE", session=Session())
    with pytest.raises(DeltaError) as err:
        client.wallet_balances()
    text = str(err.value)
    assert "GET /v2/wallet/balances" in text and "status=401" in text
    assert "delta_code=ip_not_whitelisted_for_api_key" in text
    assert "KEYVALUE" not in text and "SECRETVALUE" not in text and "secret-url" not in text


def test_live_heartbeat_reader_is_tolerant(tmp_path):
    assert _read_live_heartbeat(str(tmp_path / "none.json"))["status"] == "NO_LIVE_HEARTBEAT"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert _read_live_heartbeat(str(bad))["status"] == "LIVE_HEARTBEAT_UNREADABLE"
    good = tmp_path / "good.json"
    good.write_text('{"event":"HOLD","at":"t","api_key":"nope"}')
    assert _read_live_heartbeat(str(good)) == {"event": "HOLD", "at": "t"}


def test_live_orders_reader_is_read_only_and_tolerant(tmp_path):
    from datetime import UTC, datetime

    from optopsy_delta.dashboard import _read_live_orders
    from optopsy_delta.live_worker import AttemptBook
    path = tmp_path / "a.sqlite"
    assert _read_live_orders(str(path)) == []
    book = AttemptBook(path)
    book.add(signal_id="s1", side="buy", product_id=7, underlying="BTC", day="2026-10-13",
             now=datetime.now(UTC))
    rows = _read_live_orders(str(path))
    assert rows[0]["side"] == "buy" and rows[0]["product_id"] == 7 and rows[0]["resolved"] == 0
