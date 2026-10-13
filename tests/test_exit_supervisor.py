"""All exit supervisor cases are offline; no API, credentials or network."""
from datetime import UTC, datetime, timedelta

import pytest

from optopsy_delta.exit_supervisor import (
    ExitDraftJournal,
    ExitSafetyError,
    assess_exit,
)

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)


def reconciled(qty=2, bought=2, sold=0):
    return {"status": "CONSISTENT_BUT_NOT_AUTHORIZED", "consistent": True,
            "live_order_authorized": False, "exclusive_ownership_proven": False,
            "product_id": 44, "reported_position_size": qty,
            "registered_bought": bought, "registered_sold": sold}


def signal(exit_value=False):
    return {"underlying": "BTC", "strategy": "long_calls",
            "last_complete_utc_day": "2026-10-12", "entry": False,
            "exit": exit_value, "parameters_tuned": False,
            "exit_rule": "optopsy.rsi_above(period=14, threshold=70)"}


def quote(**changes):
    return {"product_id": 44, "best_bid": "20.0", "best_ask": "20.5",
            "tick_size": "0.1", "best_bid_size": 2,
            "timestamp_unix": int(NOW.timestamp()) - 2} | changes


def evaluate(**changes):
    args = {"reconciliation": reconciled(), "underlying": "BTC",
            "now": NOW, "expiry": NOW + timedelta(days=1),
            "safety_cutoff": NOW + timedelta(hours=22),
            "original_signal": signal(), "quote": quote()}
    return assess_exit(**(args | changes))


def test_holding_without_rsi_exit():
    assert evaluate()["status"] == "HOLD"


def test_original_rsi_exit_is_draft_only():
    p = evaluate(original_signal=signal(True))
    assert (p["status"], p["reason"]) == (
        "DRAFT_EXIT_ONLY", "ORIGINAL_OPTOSPY_RSI_ABOVE_70"
    )
    assert p["draft_size"] == 2
    assert p["reduce_only"] and p["submit_authorized"] is False


def test_expiry_guard_requires_explicit_preexpiry_cutoff():
    p = evaluate(safety_cutoff=NOW - timedelta(minutes=1),
                 original_signal=None)
    assert p["reason"] == "PRE_EXPIRY_SAFETY_CUTOFF"
    with pytest.raises(ExitSafetyError, match="precede expiry"):
        evaluate(safety_cutoff=NOW + timedelta(days=2))


def test_expired_contract_requires_manual_settlement_review():
    p = evaluate(expiry=NOW - timedelta(seconds=1),
                 safety_cutoff=NOW - timedelta(days=1))
    assert p["status"] == "URGENT_MANUAL_RECONCILIATION"
    assert p["reason"] == "CONTRACT_EXPIRED_OR_SETTLING"


def test_zero_position_never_generates_exit():
    assert evaluate(reconciliation=reconciled(0, 2, 2),
                    original_signal=None, quote=None)["status"] == "FLAT"


def test_missing_daily_signal_when_position_open_is_not_hold():
    assert evaluate(original_signal=None)["status"] == "BLOCKED_NEEDS_ATTENTION"


@pytest.mark.parametrize("bad", [
    {"last_complete_utc_day": "2026-10-11"},
    {"underlying": "ETH"},
    {"parameters_tuned": True},
    {"exit": "true"},
    {"exit_rule": "custom-rsi"},
    {"entry": True, "exit": True},
])
def test_invalid_or_stale_signal_blocks(bad):
    assert evaluate(original_signal=signal(True) | bad)["status"] == (
        "BLOCKED_NEEDS_ATTENTION"
    )


@pytest.mark.parametrize("bad_quote", [
    {"product_id": 45}, {"best_bid": "0"}, {"best_bid": "21"},
    {"best_ask": "19"}, {"best_bid_size": 0},
    {"best_bid_size": "1.5"}, {"tick_size": "0"},
    {"timestamp_unix": int(NOW.timestamp()) - 11},
    {"timestamp_unix": int(NOW.timestamp()) + 1},
])
def test_due_exit_with_invalid_quote_needs_attention(bad_quote):
    p = evaluate(original_signal=signal(True), quote=quote(**bad_quote))
    assert p["status"] == "EXIT_DUE_BLOCKED"
    assert p["submit_authorized"] is False


def test_insufficient_bid_depth_only_drafts_partial_quantity():
    p = evaluate(original_signal=signal(True), quote=quote(best_bid_size=1))
    assert p["draft_size"] == 1 and p["partial_depth"] is True


@pytest.mark.parametrize("bad", [
    {"status": "QUARANTINED"},
    {"consistent": False},
    {"live_order_authorized": True},
    {"exclusive_ownership_proven": True},
    {"reported_position_size": 3},
    {"registered_sold": 3},
])
def test_uncertain_or_inconsistent_reconciliation_never_drafts(bad):
    with pytest.raises(ExitSafetyError):
        evaluate(reconciliation=reconciled() | bad, original_signal=signal(True))


def test_quote_age_restricted_to_ten_seconds():
    with pytest.raises(ExitSafetyError):
        evaluate(quote_max_age_s=30)


def test_exit_draft_journal_survives_restart_and_fails_closed(tmp_path):
    path = tmp_path / "exit.sqlite"
    first = ExitDraftJournal(path)
    draft = evaluate(original_signal=signal(True))
    saved = first.preserve(draft)
    assert saved["status"] == "NEW_DRAFT_SAVED_NOT_SENT"
    assert path.stat().st_mode & 0o777 == 0o600
    restarted = ExitDraftJournal(path)
    assert restarted.status()["status"] == "DRAFT_PENDING_MANUAL_REVIEW"
    assert restarted.preserve(draft)["status"] == "EXISTING_DRAFT_NEEDS_REVALIDATION"
    with pytest.raises(ExitSafetyError, match="Unresolved prior"):
        restarted.preserve(draft | {"limit_price": "19.9"})


def test_journal_rejects_hold_and_never_authorizes():
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        j = ExitDraftJournal(tmp + "/p.sqlite")
        with pytest.raises(ExitSafetyError):
            j.preserve(evaluate())
        assert j.status()["status"] == "EMPTY"
