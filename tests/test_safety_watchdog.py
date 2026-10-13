"""Offline tests for one-shot read-only watchdog and SQLite incident transitions."""
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from optopsy_delta.safety_watchdog import (
    SafetyIncidentJournal,
    SafetyWatchdogError,
    observe_safety,
    read_only_watchdog_check,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def check(**override):
    params = {
        "now": NOW, "broker_checked_at": NOW - timedelta(seconds=1),
        "broker_healthy": True, "account_positions": [], "active_orders": [],
        "product_id": 44, "product_position": {"size": 0},
        "lifecycle_status": {"state": "OBSERVING", "product_id": 44},
        "exit_status": {"attempts": []},
        "expiry": NOW + timedelta(days=2),
        "exit_cutoff": NOW + timedelta(days=1),
    }
    return observe_safety(**(params | override))


def test_flat_observation_never_authorizes_trading():
    report = check()
    assert report["status"] == "OBSERVATION_CLEAR_NOT_AUTHORIZED"
    assert report["issues"] == []
    assert report["live_entry_authorized"] is False
    assert report["live_exit_authorized"] is False
    assert report["autonomous_protection_active"] is False


def test_outage_does_not_become_flat():
    report = check(broker_healthy=False, broker_checked_at=None,
                   account_positions=None, active_orders=None, product_position=None)
    assert "BROKER_UNREACHABLE_OR_INVALID" in report["issues"]
    assert report["account_position_rows"] is None
    assert report["product_position_quantity"] is None


@pytest.mark.parametrize("delay", [-31, 3])
def test_stale_and_future_broker_snapshot(delay):
    report = check(broker_checked_at=NOW + timedelta(seconds=delay))
    assert "BROKER_OBSERVATION_STALE_OR_FUTURE" in report["issues"]


def test_expiry_and_preexpiry_cutoff_open_position():
    at_cutoff = check(
        product_position={"size": 2}, exit_cutoff=NOW - timedelta(seconds=1)
    )
    assert "OPEN_POSITION_EXIT_CUTOFF_REACHED" in at_cutoff["issues"]
    expired = check(
        product_position={"size": 2}, expiry=NOW - timedelta(seconds=1),
        exit_cutoff=NOW - timedelta(hours=1),
    )
    assert "OPEN_POSITION_EXPIRED_SETTLEMENT_REVIEW" in expired["issues"]


def test_missing_guard_with_open_inventory_is_not_clear():
    report = check(
        product_position={"size": 1}, expiry=None, exit_cutoff=None
    )
    assert "OPEN_POSITION_EXPIRY_GUARD_MISSING" in report["issues"]


def test_unknown_exit_and_quarantined_journal_block():
    report = check(
        lifecycle_status={"state": "QUARANTINED", "product_id": 44},
        exit_status={
            "attempts": [{"state": "PREPARED_UNKNOWN", "product_id": 44}]
        },
    )
    assert "LIFECYCLE_NOT_OBSERVING" in report["issues"]
    assert "EXIT_ACKNOWLEDGEMENT_UNRESOLVED" in report["issues"]


@pytest.mark.parametrize("bad_qty", [-1, True, None, "NaN", "1.5"])
def test_invalid_realtime_position_is_blocker(bad_qty):
    assert "PRODUCT_POSITION_INVALID" in check(
        product_position={"size": bad_qty}
    )["issues"]


def test_broker_account_schema_and_external_positions():
    assert "BROKER_ACCOUNT_SCHEMA_INVALID" in check(
        active_orders={"invalid": True}
    )["issues"]
    assert "FOREIGN_OR_UNKNOWN_POSITION" in check(
        account_positions=[{"product_id": 99, "size": 1}]
    )["issues"]


def test_unconfigured_ledgers_are_not_inferred_empty():
    report = check(
        lifecycle_status=None, exit_status=None, product_id=None,
        product_position=None, expiry=None, exit_cutoff=None
    )
    assert "LIFECYCLE_JOURNAL_UNAVAILABLE" in report["issues"]
    assert "EXIT_JOURNAL_UNAVAILABLE" in report["issues"]
    assert "PRODUCT_NOT_BOUND" in report["issues"]


def test_signed_read_failure_is_redacted_and_no_extra_reads():
    api = Mock()
    api.open_positions.side_effect = RuntimeError("secret=PRIVATE_SAMPLE")
    report = read_only_watchdog_check(
        client=api, product_id=44, now=NOW,
        lifecycle_status={"state": "OBSERVING", "product_id": 44},
        exit_status={"attempts": []},
    )
    assert report["broker_healthy"] is False
    assert "PRIVATE_SAMPLE" not in str(report)
    api.active_orders.assert_not_called()


def test_good_private_snapshot_is_one_shot_without_any_order_writes():
    api = Mock()
    api.open_positions.return_value = []
    api.active_orders.return_value = []
    api.product_position.return_value = {"size": 0}
    report = read_only_watchdog_check(
        client=api, product_id=44, now=NOW,
        lifecycle_status={"state": "OBSERVING", "product_id": 44},
        exit_status={"attempts": []},
    )
    assert report["issues"] == []
    api.product_position.assert_called_once_with(44)
    assert all(
        call[0] in ("open_positions", "active_orders", "product_position")
        for call in api.mock_calls
    )


def test_incident_transition_is_durable_and_append_only(tmp_path):
    journal = SafetyIncidentJournal(tmp_path / "alerts.sqlite")
    clear = check()
    unhealthy = check(
        broker_healthy=False, broker_checked_at=None,
        account_positions=None, active_orders=None, product_position=None,
    )
    assert journal.record(clear, observed_at=100)["recorded"]
    assert not journal.record(clear, observed_at=101)["recorded"]
    assert journal.record(unhealthy, observed_at=102)["recorded"]
    restarted = SafetyIncidentJournal(journal.path)
    assert restarted.status()["transitions"] == 2
    assert restarted.status()["latest_status"] == "ATTENTION_REQUIRED"
    assert journal.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(journal.path) as db:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM observations")


def test_forged_clear_report_fails_before_any_db_write(tmp_path):
    journal = SafetyIncidentJournal(tmp_path / "alerts.sqlite")
    with pytest.raises(SafetyWatchdogError):
        journal.record(check() | {"live_exit_authorized": True}, observed_at=2)
    with pytest.raises(SafetyWatchdogError):
        journal.record(check() | {"issues": ["fabricated"]}, observed_at=3)
    assert journal.status()["transitions"] == 0
