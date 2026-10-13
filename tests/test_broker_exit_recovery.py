"""Fake-broker tests for authenticated read-only exit reconciliation."""
from unittest.mock import Mock

import pytest

from optopsy_delta.broker_exit_recovery import reconcile_exit_broker
from optopsy_delta.exit_recovery import ExitAttemptJournal, ExitRecoveryError
from optopsy_delta.live_gateway import LiveIntentJournal, OrderIntent
from optopsy_delta.position_lifecycle import LifecycleJournal


def fixture(tmp_path, *, remaining=1):
    life = LifecycleJournal(tmp_path / "life.sqlite")
    life.register_flat_baseline(
        product_id=44, baseline_us=100, account_positions=[], active_orders=[]
    )
    entries = LiveIntentJournal(tmp_path / "entries.sqlite")
    entry = OrderIntent(
        signal_id="one", product_id=44, side="buy", size=2,
        limit_price="20", contract_value="0.01", usd_inr="85",
        ticker_timestamp=100,
    )
    key = entries.reserve(entry)
    entries.note(key, "ACKNOWLEDGED", order_id="101")
    attempts = ExitAttemptJournal(tmp_path / "exit.sqlite")
    draft = {
        "status": "DRAFT_EXIT_ONLY", "submit_authorized": False,
        "side": "sell", "reduce_only": True, "order_type": "limit_order",
        "time_in_force": "ioc", "product_id": 44,
        "remaining_contracts": 2, "draft_size": 2,
        "available_bid_depth": 2, "quote_observed_unix": 1000,
        "limit_price": "20", "reason": "PRE_EXPIRY_SAFETY_CUTOFF",
    }
    cid = attempts.prepare(draft)["client_order_id"]
    broker = Mock()
    broker.order_by_id.return_value = {
        "id": 101, "client_order_id": key, "product_id": 44,
        "side": "buy", "size": 2, "unfilled_size": 0, "state": "closed",
    }
    broker.order_by_client_id.return_value = {
        "id": 202, "client_order_id": cid, "product_id": 44,
        "side": "sell", "reduce_only": True, "size": 2,
        "unfilled_size": remaining, "state": "cancelled",
    }
    broker.active_orders.return_value = []
    broker.open_positions.return_value = [{"product_id": 44, "size": remaining}]
    broker.product_position.return_value = {"size": remaining}
    broker.fills_for_product.return_value = [
        {"id": "a", "order_id": 101, "product_id": 44,
         "side": "buy", "size": 2},
    ] + ([{"id": "b", "order_id": 202, "product_id": 44,
           "side": "sell", "size": 2 - remaining}] if remaining < 2 else [])
    return broker, entries, life, attempts


def check(group):
    client, entries, lifecycle, exits = group
    return reconcile_exit_broker(
        client=client, entries=entries, lifecycle=lifecycle,
        exits=exits, now_us=200,
    )


def test_partial_fill_restart_and_no_trading(tmp_path):
    group = fixture(tmp_path)
    result = check(group)
    assert result["remaining_qty"] == 1
    assert result["submit_authorized"] is False
    assert result["broker_history_reconciled"] is True
    assert ExitAttemptJournal(group[3].path).status()["attempts"][0][
        "state"
    ] == "TERMINAL_VERIFIED"


def test_fully_closed(tmp_path):
    assert check(fixture(tmp_path, remaining=0))["status"] == "TERMINAL_VERIFIED_FLAT"


def test_pending_waits_without_retry(tmp_path):
    group = fixture(tmp_path)
    group[0].order_by_client_id.return_value["state"] = "open"
    group[0].active_orders.return_value = [group[0].order_by_client_id.return_value]
    assert check(group)["status"] == "WAIT_FOR_BROKER_SETTLEMENT"
    assert group[3].status()["attempts"][0]["state"] == "PREPARED_UNKNOWN"


@pytest.mark.parametrize("bad", [
    "unknown_order", "missing_fill", "external_fill", "bad_position",
    "foreign_order", "foreign_position", "wrong_client", "broker_timeout",
])
def test_inconsistent_or_unavailable_broker_blocks(tmp_path, bad):
    group = fixture(tmp_path)
    broker = group[0]
    if bad == "unknown_order":
        broker.order_by_client_id.return_value["id"] = None
    elif bad == "missing_fill":
        broker.fills_for_product.return_value.pop()
    elif bad == "external_fill":
        broker.fills_for_product.return_value.append({
            "id": "manual", "order_id": 999, "product_id": 44,
            "side": "buy", "size": 1,
        })
    elif bad == "bad_position":
        broker.product_position.return_value["size"] = 2
    elif bad == "foreign_order":
        broker.active_orders.return_value = [{"id": 888}]
    elif bad == "foreign_position":
        broker.open_positions.return_value.append({"product_id": 55})
    elif bad == "wrong_client":
        broker.order_by_client_id.return_value["client_order_id"] = "wrong"
    elif bad == "broker_timeout":
        broker.order_by_client_id.side_effect = ConnectionError("unavailable")
    with pytest.raises((ExitRecoveryError, ConnectionError)):
        check(group)
    assert group[3].status()["attempts"][0]["state"] == "PREPARED_UNKNOWN"
