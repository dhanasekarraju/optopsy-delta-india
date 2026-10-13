"""Offline checks for the read-only dashboard: no real Delta or orders."""
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

from optopsy_delta.dashboard import collect_status, make_handler


def test_read_only_status_never_claims_trade_authority(monkeypatch):
    monkeypatch.delenv("OPTOPSY_DASHBOARD_PRODUCT_ID", raising=False)
    api = Mock()
    api._key = "dummy"
    api._secret = "dummy"
    api.wallet_balances.return_value = [{
        "asset_symbol": "USD", "balance": "0.000056834",
        "available_balance": "0.000056834",
        "blocked_margin": "0", "order_margin": "0",
        "position_margin": "0",
    }]
    api.open_positions.return_value = []
    api.active_orders.return_value = []
    status = collect_status(api)
    assert status["broker_healthy"] is True
    assert status["open_positions"] == 0
    assert status["active_orders"] == 0
    assert status["trading_enabled"] is False
    assert status["production_ready"] is False
    assert "AUTONOMOUS_ENTRY_EXIT_CONTROLLER_NOT_RELEASED" in status["blockers"]
    assert "INSUFFICIENT_AVAILABLE_BALANCE" in status["blockers"]
    api.wallet_balances.assert_called_once()
    api.open_positions.assert_called_once()
    api.active_orders.assert_called_once()
    api.product_position.assert_not_called()
    assert not any(x[0] in ("submit", "cancel", "place_order") for x in api.mock_calls)


def test_missing_credentials_fail_closed(monkeypatch):
    monkeypatch.delenv("OPTOPSY_DASHBOARD_PRODUCT_ID", raising=False)
    api = Mock()
    api._key = ""
    api._secret = ""
    status = collect_status(api)
    assert status["broker_healthy"] is False
    assert status["open_positions"] is None
    assert status["broker_status"] == "CREDENTIALS_MISSING"
    api.wallet_balances.assert_not_called()


def test_broker_exception_is_redacted(monkeypatch):
    monkeypatch.delenv("OPTOPSY_DASHBOARD_PRODUCT_ID", raising=False)
    api = Mock()
    api._key = "dummy"
    api._secret = "dummy"
    api.wallet_balances.side_effect = RuntimeError("SECRET_PRIVATE_VALUE")
    status = collect_status(api)
    assert status["broker_healthy"] is False
    assert "SECRET_PRIVATE_VALUE" not in json.dumps(status)
    assert status["open_positions"] is None


def test_invalid_product_binding_rejected(monkeypatch):
    monkeypatch.setenv("OPTOPSY_DASHBOARD_PRODUCT_ID", "../42")
    api = Mock()
    api._key = "dummy"
    api._secret = "dummy"
    try:
        collect_status(api)
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid product ID accepted")


def test_dashboard_has_no_order_endpoints_and_rejects_bad_host():
    def fake():
        return {
            "mode": "READ_ONLY_DASHBOARD", "broker_healthy": True,
            "blockers": [], "live_entry_authorized": True,
            "production_ready": True, "trading_enabled": True,
        }
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/status", headers={"Host": "localhost"})
        reply = conn.getresponse()
        payload = json.loads(reply.read())
        assert reply.status == 200
        assert payload["production_ready"] is False
        assert payload["trading_enabled"] is False
        assert payload["live_entry_authorized"] is False
        conn.request("POST", "/api/orders", headers={"Host": "localhost"})
        assert conn.getresponse().status == 405
        # The service rejects POST without invoking the provider.
        conn.close()
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/", headers={"Host": "evil.example"})
        response = conn.getresponse()
        assert response.status == 403
        response.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
