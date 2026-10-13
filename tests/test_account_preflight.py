"""Private-account preflight cannot grant live-order authorization."""
import pytest

from optopsy_delta.account_preflight import AccountPreflightError, inspect_account


def wallet(**changes):
    return {
        "asset_symbol": "USD", "balance": "60", "available_balance": "60",
        "blocked_margin": "0", "order_margin": "0", "position_margin": "0",
    } | changes


def test_preliminary_clear_never_enables_orders():
    r = inspect_account([wallet()], [], [])
    assert r["status"] == "PRELIMINARY_CLEAR"
    assert r["live_entry_authorized"] is False
    assert r["deployable_inr_after_reserve"] == "4500.00"


def test_existing_broker_exposure_blocks():
    r = inspect_account([wallet(blocked_margin="1")], [{"size": 1}], [{"id": 2}])
    assert r["status"] == "BLOCKED"
    assert {"EXISTING_POSITIONS", "ACTIVE_ORDERS", "EXISTING_MARGIN"} <= set(r["blockers"])


def test_missing_margin_and_other_collateral_block():
    w = wallet()
    del w["order_margin"]
    r = inspect_account([w, {"asset_symbol": "BTC", "balance": "0.1",
                              "available_balance": "0.1"}], [], [])
    assert r["status"] == "BLOCKED"
    assert "UNKNOWN_MARGIN_FIELDS" in r["blockers"]
    assert "OTHER_COLLATERAL" in r["blockers"]


@pytest.mark.parametrize("bad", [
    wallet(available_balance="NaN"),
    wallet(available_balance="-1"),
    wallet(available_balance="61"),
    wallet(blocked_margin="NaN"),
])
def test_invalid_numbers_are_not_silently_coerced(bad):
    with pytest.raises(AccountPreflightError):
        inspect_account([bad], [], [])


def test_low_balance_and_duplicate_wallet_block():
    assert inspect_account([wallet(balance="1", available_balance="1")], [], [])[
        "status"
    ] == "BLOCKED"
    assert inspect_account([wallet(), wallet()], [], [])["status"] == "BLOCKED"


def test_invalid_shapes_refused():
    with pytest.raises(AccountPreflightError):
        inspect_account([], None, [])
    with pytest.raises(AccountPreflightError):
        inspect_account([None], [], [])
