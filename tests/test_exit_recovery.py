"""Offline restart-safe Delta reduce-only IOC exit recovery regression tests."""
import sqlite3

import pytest

from optopsy_delta.exit_recovery import ExitAttemptJournal, ExitRecoveryError
from optopsy_delta.live_gateway import _client_id


def draft(*, total=3, request=2, timestamp=1000):
    return {"status": "DRAFT_EXIT_ONLY", "submit_authorized": False,
            "side": "sell", "reduce_only": True, "order_type": "limit_order",
            "time_in_force": "ioc", "product_id": 44,
            "remaining_contracts": total, "draft_size": request,
            "quote_observed_unix": timestamp, "limit_price": "22.0",
            "available_bid_depth": 4, "reason": "PRE_EXPIRY_SAFETY_CUTOFF"}


def settled(cid, *, request=2, remain=1, pos=2):
    return {
        "order": {"id": 900, "client_order_id": cid, "product_id": 44,
                  "size": request, "unfilled_size": remain,
                  "side": "sell", "reduce_only": True, "state": "cancelled"},
        "fills": [{"id": "fill-1", "order_id": 900, "product_id": 44,
                   "side": "sell", "size": request - remain}] if request > remain else [],
        "position": {"size": pos, "product_id": 44},
        "all_active_orders": [], "history_complete": True,
    }


def make(tmp_path):
    return ExitAttemptJournal(tmp_path / "attempt.sqlite")


def test_unknown_ack_never_auto_retries(tmp_path):
    j = make(tmp_path)
    cid = j.prepare(draft())["client_order_id"]
    assert j.status()["live_order_authorized"] is False
    with pytest.raises(ExitRecoveryError, match="Unresolved prior"):
        j.prepare(draft(timestamp=1001))
    with pytest.raises(ExitRecoveryError, match="no retry"):
        j.reconcile_terminal(client_order_id=cid, order=None, fills=[],
                             position={"size": 3}, all_active_orders=[],
                             history_complete=True)
    assert make(tmp_path).status()["attempts"][0]["state"] == "PREPARED_UNKNOWN"


def test_partial_ioc_requires_fresh_quote_and_updated_inventory(tmp_path):
    j = make(tmp_path)
    cid = j.prepare(draft())["client_order_id"]
    outcome = j.reconcile_terminal(client_order_id=cid, **settled(cid))
    assert outcome["status"] == "PARTIAL_OR_UNFILLED_NEEDS_NEW_DRAFT"
    assert outcome["remaining_qty"] == 2
    with pytest.raises(ExitRecoveryError, match="fresh quote"):
        j.prepare(draft(total=2, request=2))
    new_cid = make(tmp_path).prepare(draft(total=2, request=2, timestamp=1002))[
        "client_order_id"]
    assert new_cid != cid
    assert len(j.status()["attempts"]) == 2
    with pytest.raises(ExitRecoveryError, match="Unresolved prior"):
        j.prepare(draft(total=2, request=1, timestamp=1003))


def test_fully_filled_position_is_flat_and_no_new_exit(tmp_path):
    j = make(tmp_path)
    cid = j.prepare(draft(total=2, request=2))["client_order_id"]
    result = j.reconcile_terminal(client_order_id=cid, **settled(
        cid, request=2, remain=0, pos=0))
    assert result["status"] == "TERMINAL_VERIFIED_FLAT"
    with pytest.raises(ExitRecoveryError, match="already flat"):
        j.prepare(draft(total=1, request=1, timestamp=1003))


def test_unfilled_ioc_still_requires_new_quote(tmp_path):
    j = make(tmp_path)
    cid = j.prepare(draft())["client_order_id"]
    result = j.reconcile_terminal(client_order_id=cid, **settled(
        cid, request=2, remain=2, pos=3))
    assert result["filled_qty"] == 0
    assert result["remaining_qty"] == 3


@pytest.mark.parametrize("bad", [
    {"history_complete": False},
    {"all_active_orders": [{"id": 1}]},
    {"order": None},
    {"position": {"size": 3}},
    {"position": {"size": -1}},
    {"order": {"id": 900, "client_order_id": "invalid", "product_id": 44,
               "size": 2, "unfilled_size": 1, "side": "sell",
               "reduce_only": True, "state": "closed"}},
    {"fills": [{"id": "f", "order_id": 901, "product_id": 44,
                "side": "sell", "size": 1}]},
    {"fills": [{"id": "f", "order_id": 900, "product_id": 44,
                "side": "buy", "size": 1}]},
    {"fills": []},
])
def test_mismatch_quarantines_durably(tmp_path, bad):
    j = make(tmp_path)
    cid = j.prepare(draft())["client_order_id"]
    incoming = settled(cid) | bad
    if incoming["order"] is None:
        with pytest.raises(ExitRecoveryError, match="no retry"):
            j.reconcile_terminal(client_order_id=cid, **incoming)
        assert j.status()["attempts"][0]["state"] == "PREPARED_UNKNOWN"
        return
    with pytest.raises(ExitRecoveryError):
        j.reconcile_terminal(client_order_id=cid, **incoming)
    assert make(tmp_path).status()["attempts"][0]["state"] == "QUARANTINED"
    with pytest.raises(ExitRecoveryError, match="Unresolved"):
        j.prepare(draft(timestamp=1002))


def test_invalid_draft_never_mutates_journal(tmp_path):
    j = make(tmp_path)
    for bad in [
        {"submit_authorized": True}, {"reduce_only": False},
        {"draft_size": 4.5}, {"draft_size": 1.5},
        {"remaining_contracts": 0}, {"quote_observed_unix": None},
        {"limit_price": "NaN"}, {"side": "buy"}, {"reason": "custom"},
        {"available_bid_depth": 1},
    ]:
        with pytest.raises(ExitRecoveryError):
            j.prepare(draft() | bad)
    assert j.status()["attempts"] == []


def test_reconcile_repeat_does_not_mutate_success(tmp_path):
    j = make(tmp_path)
    cid = j.prepare(draft())["client_order_id"]
    j.reconcile_terminal(client_order_id=cid, **settled(cid))
    with pytest.raises(ExitRecoveryError, match="already reconciled"):
        j.reconcile_terminal(client_order_id=cid, **settled(cid))
    assert make(tmp_path).status()["attempts"][0]["state"] == "TERMINAL_VERIFIED"


def test_persistent_sqlite_and_file_mode(tmp_path):
    j = make(tmp_path)
    j.prepare(draft())
    with sqlite3.connect(j.path) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    assert j.path.stat().st_mode & 0o777 == 0o600


def test_pending_broker_order_not_misclassified_or_retried(tmp_path):
    j = make(tmp_path)
    cid = j.prepare(draft())["client_order_id"]
    pending = settled(cid)
    pending["order"]["state"] = "open"
    result = j.reconcile_terminal(client_order_id=cid, **pending)
    assert result["status"] == "WAIT_FOR_BROKER_SETTLEMENT"
    assert j.status()["attempts"][0]["state"] == "PREPARED_UNKNOWN"
    with pytest.raises(ExitRecoveryError, match="Unresolved prior"):
        j.prepare(draft(timestamp=2000))
    assert j.reconcile_terminal(client_order_id=cid, **settled(cid))["filled_qty"] == 1


def test_order_client_id_matches_gateway_deterministic_id(tmp_path):
    j = make(tmp_path)
    prepared = j.prepare(draft())
    assert prepared["client_order_id"] == _client_id(prepared["signal_id"], "sell")
    assert len(prepared["client_order_id"]) <= 32
