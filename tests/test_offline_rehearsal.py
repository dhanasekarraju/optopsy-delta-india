"""Offline integrated broker/exit lifecycle smoke regression tests."""
from optopsy_delta.offline_rehearsal import run_rehearsal


def test_rehearsal_all_scenarios_pass_without_real_orders():
    result = run_rehearsal()
    assert result["passed"] == 6
    assert result["failed"] == 0
    assert result["live_orders_sent"] == 0
    assert result["production_ready"] is False
    assert all(x["result"] == "PASS" for x in result["scenarios"])


def test_rehearsal_is_repeatable():
    a = run_rehearsal()
    b = run_rehearsal()
    assert a == b
