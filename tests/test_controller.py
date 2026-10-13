"""Controller coordination tests use only fake broker and durable paper journals."""
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from optopsy_delta.controller import ControllerError, controller_cycle

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def inputs():
    client = Mock()
    client.open_positions.return_value = []
    client.active_orders.return_value = []
    client.product_position.return_value = {"size": 0}
    client.wallet_balances.return_value = []
    lifecycle = Mock()
    lifecycle.status.return_value = {"state": "OBSERVING", "product_id": 44}
    exits = Mock()
    exits.status.return_value = {"attempts": []}
    intents = Mock()
    intents.status.return_value = []
    contract = {
        "underlying": "BTC", "product_id": 44,
        "expiry_utc": (NOW + timedelta(days=7)).isoformat(),
        "live_order_authorized": False,
    }
    signal = {
        "underlying": "BTC", "entry": True, "exit": False,
        "last_complete_utc_day": "2026-10-12",
    }
    kw = {
        "client": client, "intent_journal": intents, "lifecycle": lifecycle,
        "exit_attempts": exits, "signal": signal, "contract": contract,
        "quote": None, "now": NOW,
        "safety_cutoff": NOW + timedelta(days=6), "order_size": 1,
    }
    return kw


def reconciliation(qty=0):
    return {
        "status": "CONSISTENT_BUT_NOT_AUTHORIZED",
        "product_id": 44, "consistent": True,
        "reported_position_size": qty,
        "registered_bought": qty, "registered_sold": 0,
        "live_order_authorized": False, "exclusive_ownership_proven": False,
    }


def safety(qty=0):
    return {
        "broker_healthy": True, "issues": [],
        "product_position_quantity": qty,
        "status": "OBSERVATION_CLEAR_NOT_AUTHORIZED",
        "live_entry_authorized": False,
    }


def test_unknown_exit_stops_before_broker_and_never_sends():
    kw = inputs()
    kw["exit_attempts"].status.return_value = {
        "attempts": [{"product_id": 44, "state": "PREPARED_UNKNOWN"}],
    }
    report = controller_cycle(**kw)
    assert report["state"] == "RECOVERY_REQUIRED"
    assert report["order_submitted"] is False
    kw["client"].open_positions.assert_not_called()


def test_reconciliation_error_stops():
    kw = inputs()
    with patch("optopsy_delta.controller.reconcile_broker",
               side_effect=ConnectionError("PRIVATE")):
        report = controller_cycle(**kw)
    assert report["state"] == "STOP_BROKER_RECONCILIATION"
    assert "PRIVATE" not in str(report)


def test_flat_no_entry_does_not_invoke_account_preflight():
    kw = inputs()
    kw["signal"] = None
    with (
        patch("optopsy_delta.controller.reconcile_broker",
              return_value=reconciliation()),
        patch("optopsy_delta.controller.read_only_watchdog_check",
              return_value=safety()),
        patch("optopsy_delta.controller.account_preflight") as preflight,
    ):
        report = controller_cycle(**kw)
    assert report["state"] == "FLAT_NO_ENTRY_SIGNAL"
    preflight.assert_not_called()


def test_existing_campaign_never_reenters():
    kw = inputs()
    kw["intent_journal"].status.return_value = [{"stage": "ACKNOWLEDGED"}]
    with (
        patch("optopsy_delta.controller.reconcile_broker",
              return_value=reconciliation()),
        patch("optopsy_delta.controller.read_only_watchdog_check",
              return_value=safety()),
    ):
        report = controller_cycle(**kw)
    assert report["state"] == "FLAT_PREVIOUS_CAMPAIGN"


def test_entry_candidate_never_submitted():
    kw = inputs()
    with (
        patch("optopsy_delta.controller.reconcile_broker",
              return_value=reconciliation()),
        patch("optopsy_delta.controller.read_only_watchdog_check",
              return_value=safety()),
        patch("optopsy_delta.controller.account_preflight",
              return_value={"status": "PRELIMINARY_CLEAR"}),
        patch("optopsy_delta.controller.plan_entry",
              return_value={"status": "NON_EXECUTABLE_ENTRY_INTENT",
                            "submitted": False}) as entry,
    ):
        report = controller_cycle(**kw)
    assert report["state"] == "ENTRY_CANDIDATE_NOT_SENT"
    assert report["live_entry_authorized"] is False
    assert report["order_submitted"] is False
    entry.assert_called_once()


def test_position_inventory_change_blocks_exit():
    kw = inputs()
    with (
        patch("optopsy_delta.controller.reconcile_broker",
              return_value=reconciliation(2)),
        patch("optopsy_delta.controller.read_only_watchdog_check",
              return_value=safety(1)),
    ):
        report = controller_cycle(**kw)
    assert report["state"] == "STOP_BROKER_POSITION_CHANGED"


def test_observed_position_routes_to_original_exit_supervisor():
    kw = inputs()
    kw["signal"]["entry"] = False
    kw["signal"]["exit"] = True
    with (
        patch("optopsy_delta.controller.reconcile_broker",
              return_value=reconciliation(2)),
        patch("optopsy_delta.controller.read_only_watchdog_check",
              return_value=safety(2)),
        patch("optopsy_delta.controller.assess_exit",
              return_value={"status": "DRAFT_EXIT_ONLY",
                            "reason": "ORIGINAL_OPTOSPY_RSI_ABOVE_70",
                            "submit_authorized": False}) as assessor,
    ):
        report = controller_cycle(**kw)
    assert report["state"] == "EXIT_DRAFT_NOT_SENT"
    assert report["live_exit_authorized"] is False
    assert report["order_submitted"] is False
    assessor.assert_called_once()


def test_pending_foreign_orders_block_draft():
    kw = inputs()
    with (
        patch("optopsy_delta.controller.reconcile_broker",
              return_value=reconciliation(2)),
        patch("optopsy_delta.controller.read_only_watchdog_check",
              return_value={**safety(2), "issues": ["ACCOUNT_ACTIVE_ORDERS_PRESENT"]}),
    ):
        report = controller_cycle(**kw)
    assert report["state"] == "STOP_EXIT_UNSAFE_BROKER"


def test_invalid_expiry_fails_closed():
    kw = inputs()
    kw["contract"]["expiry_utc"] = "broken"
    with pytest.raises(ControllerError, match="expiry"):
        controller_cycle(**kw)
