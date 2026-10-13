"""Unit tests of non-executable original Optopsy admission gate."""
from datetime import UTC, datetime

import pytest

from optopsy_delta.entry_admission import AdmissionError, plan_entry

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def evidence():
    stamp = "2026-10-13T11:59:58+00:00"
    return {
        "signal": {
            "underlying": "BTC", "strategy": "long_calls",
            "entry_rule": "optopsy.rsi_below(period=14, threshold=30)",
            "last_complete_utc_day": "2026-10-12",
            "parameters_tuned": False, "entry": True, "exit": False,
        },
        "contract": {
            "underlying": "BTC", "product_id": 44, "contract_value": "0.001",
            "tick_size": "0.1", "captured_at": stamp,
            "live_order_authorized": False,
        },
        "now": NOW,
        "account": {
            "status": "PRELIMINARY_CLEAR", "live_entry_authorized": False,
            "position_rows": 0, "active_order_rows": 0,
            "deployable_inr_after_reserve": "2000",
        },
        "broker_observation": {
            "status": "OBSERVATION_CLEAR_NOT_AUTHORIZED",
            "broker_healthy": True, "live_entry_authorized": False,
            "account_position_rows": 0, "active_order_rows": 0,
        },
        "live_position_size": "0", "order_size": 1,
        "quote": {
            "product_id": 44, "captured_at": stamp,
            "timestamp_unix": int(NOW.timestamp()) - 2,
            "best_bid": "19.9", "best_ask": "20.0",
            "tick_size": "0.1", "best_ask_size": 3,
        },
    }


def test_fresh_candidate_remains_non_executable():
    report = plan_entry(**evidence())
    assert report["status"] == "NON_EXECUTABLE_ENTRY_INTENT"
    assert report["live_entry_authorized"] is False
    assert report["submitted"] is False


@pytest.mark.parametrize("field,change", [
    ("signal", {"entry": False}),
    ("signal", {"last_complete_utc_day": "2026-10-11"}),
    ("signal", {"parameters_tuned": True}),
    ("account", {"status": "BLOCKED"}),
    ("broker_observation", {"broker_healthy": False}),
    ("contract", {"product_id": 50}),
    ("quote", {"timestamp_unix": int(NOW.timestamp()) - 15}),
    ("quote", {"best_ask_size": 0}),
])
def test_unsafe_evidence_is_rejected(field, change):
    args = evidence()
    args[field].update(change)
    with pytest.raises(AdmissionError):
        plan_entry(**args)


def test_actual_position_blocks_candidate():
    with pytest.raises(AdmissionError, match="Unexpected position"):
        plan_entry(**(evidence() | {"live_position_size": 1}))
