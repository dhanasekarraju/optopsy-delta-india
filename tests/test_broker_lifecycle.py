"""Fake-client-only tests of the broker lifecycle integration."""
from unittest.mock import Mock

import pytest

from optopsy_delta.broker_lifecycle import reconcile_broker
from optopsy_delta.live_gateway import LiveIntentJournal, OrderIntent
from optopsy_delta.position_lifecycle import LifecycleError, LifecycleJournal


def build(tmp_path):
    lifecycle = LifecycleJournal(tmp_path / "lifecycle.sqlite")
    lifecycle.register_flat_baseline(
        product_id=44, baseline_us=100, account_positions=[], active_orders=[]
    )
    journal = LiveIntentJournal(tmp_path / "intent.sqlite")
    return lifecycle, journal


def broker():
    client = Mock()
    client.open_positions.return_value = []
    client.active_orders.return_value = []
    client.fills_for_product.return_value = []
    client.product_position.return_value = {"size": 0}
    return client


def intent():
    return OrderIntent(signal_id="test", product_id=44, side="buy", size=2,
                       limit_price="20", contract_value="0.001",
                       usd_inr="85", ticker_timestamp=100)


def test_empty_broker_reconciliation_is_not_trade_permission(tmp_path):
    lifecycle, journal = build(tmp_path)
    client = broker()
    r = reconcile_broker(client=client, intent_journal=journal,
                         lifecycle=lifecycle, now_us=200)
    assert r["status"] == "CONSISTENT_BUT_NOT_AUTHORIZED"
    assert r["live_order_authorized"] is False
    client.fills_for_product.assert_called_once_with(
        44, start_time_us=100, end_time_us=200
    )


def test_partial_ioc_entry_survives_restart(tmp_path):
    lifecycle, journal = build(tmp_path)
    key = journal.reserve(intent())
    journal.note(key, "ACKNOWLEDGED", order_id="555")
    client = broker()
    client.order_by_id.return_value = {
        "id": 555, "client_order_id": key, "product_id": 44,
        "side": "buy", "size": 2, "unfilled_size": 1,
        "state": "cancelled",
    }
    client.fills_for_product.return_value = [{
        "id": 123, "order_id": 555, "product_id": 44,
        "side": "buy", "size": 1,
    }]
    client.product_position.return_value = {"size": 1}
    first = reconcile_broker(client=client, intent_journal=journal,
                             lifecycle=lifecycle, now_us=200)
    assert first["reported_position_size"] == 1
    again = reconcile_broker(client=client, intent_journal=LiveIntentJournal(journal.path),
                             lifecycle=LifecycleJournal(lifecycle.path), now_us=201)
    assert again["reported_position_size"] == 1


def test_no_baseline_never_contacts_broker(tmp_path):
    lifecycle = LifecycleJournal(tmp_path / "blank.sqlite")
    client = broker()
    with pytest.raises(LifecycleError, match="baseline"):
        reconcile_broker(client=client, intent_journal=LiveIntentJournal(
            tmp_path / "empty.sqlite"), lifecycle=lifecycle, now_us=200)
    client.open_positions.assert_not_called()


@pytest.mark.parametrize("bad", [
    "RESERVED_UNKNOWN", "UNKNOWN", "REJECTED",
])
def test_ambiguous_intents_block_broker_work(tmp_path, bad):
    lifecycle, journal = build(tmp_path)
    key = journal.reserve(intent())
    if bad != "RESERVED_UNKNOWN":
        journal.note(key, bad)
    client = broker()
    with pytest.raises(LifecycleError, match="Ambiguous"):
        reconcile_broker(client=client, intent_journal=journal,
                         lifecycle=lifecycle, now_us=200)
    client.open_positions.assert_not_called()


def test_foreign_position_or_order_blocks(tmp_path):
    lifecycle, journal = build(tmp_path)
    client = broker()
    client.open_positions.return_value = [{"product_id": 45, "size": 1}]
    with pytest.raises(LifecycleError, match="Unrelated"):
        reconcile_broker(client=client, intent_journal=journal,
                         lifecycle=lifecycle, now_us=200)
    client.open_positions.return_value = []
    client.active_orders.return_value = [{"id": 600}]
    with pytest.raises(LifecycleError, match="active"):
        reconcile_broker(client=client, intent_journal=journal,
                         lifecycle=lifecycle, now_us=200)


def test_external_fill_quarantines_journal(tmp_path):
    lifecycle, journal = build(tmp_path)
    client = broker()
    client.fills_for_product.return_value = [{
        "id": 1000, "product_id": 44, "order_id": 22,
        "side": "buy", "size": 1,
    }]
    client.product_position.return_value = {"size": 1}
    with pytest.raises(LifecycleError, match="external"):
        reconcile_broker(client=client, intent_journal=journal,
                         lifecycle=lifecycle, now_us=200)
    assert lifecycle.status()["state"] == "QUARANTINED"


def test_mismatched_client_order_identity_refused(tmp_path):
    lifecycle, journal = build(tmp_path)
    key = journal.reserve(intent())
    journal.note(key, "ACKNOWLEDGED", order_id="555")
    client = broker()
    client.order_by_id.return_value = {
        "id": 555, "product_id": 44, "client_order_id": "wrong",
        "side": "buy", "size": 2,
    }
    with pytest.raises(LifecycleError, match="mismatch"):
        reconcile_broker(client=client, intent_journal=journal,
                         lifecycle=lifecycle, now_us=200)


def test_invalid_broker_position_identity_refused(tmp_path):
    lifecycle, journal = build(tmp_path)
    client = broker()
    client.product_position.return_value = {"size": 0, "product_id": 999}
    with pytest.raises(LifecycleError, match="identity mismatch"):
        reconcile_broker(client=client, intent_journal=journal,
                         lifecycle=lifecycle, now_us=200)
