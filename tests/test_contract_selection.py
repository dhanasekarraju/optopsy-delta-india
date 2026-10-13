"""Pure-data tests for Optopsy original call filters and expiry grouping."""
from datetime import UTC, datetime, timedelta

import pytest

from optopsy_delta.contract_selection import (
    ContractScreenError,
    screen_long_calls,
    screen_snapshot_file,
)

DAY = datetime(2026, 10, 13, 8, 30, tzinfo=UTC)


def product(pid, *, days=7, strike="90000", **change):
    item = {
        "id": pid, "symbol": f"C-BTC-{days}-{strike}-{pid}",
        "contract_type": "call_options", "state": "live",
        "trading_status": "operational", "underlying_asset": {"symbol": "BTC"},
        "settlement_time": (DAY + timedelta(days=days)).replace(
            hour=12, minute=0).isoformat(),
        "strike_price": strike, "tick_size": "0.5",
        "contract_value": "0.001",
    }
    item.update(change)
    return item


def ticker(pid, *, delta="0.3", bid="12", ask="13", **change):
    item = {
        "product_id": pid, "quotes": {"best_bid": bid, "best_ask": ask},
        "greeks": {"delta": delta},
    }
    item.update(change)
    return item


def snap(products, quotes, **change):
    item = {
        "underlying": "BTC", "captured_at": DAY.isoformat(),
        "raw_products": products, "raw_tickers": quotes,
    }
    item.update(change)
    return item


def test_selects_closest_delta_per_expiry_not_global_expiry():
    options = [
        product(1, days=7, strike="90500"),
        product(2, days=7, strike="90000"),
        product(3, days=14),
    ]
    quotes = [
        ticker(1, delta="0.31"), ticker(2, delta="0.29"),
        ticker(3, delta="0.3"),
    ]
    result = screen_long_calls(snap(options, quotes))
    assert [r["product_id"] for r in result] == [2, 3]
    assert [r["dte_group"] for r in result] == ["(0,7]", "(7,14]"]
    assert all(not r["live_order_authorized"] for r in result)


@pytest.mark.parametrize("days", [0, -1, 85, 90, 91])
def test_exact_optopsy_default_dte_bucket_boundaries(days):
    assert screen_long_calls(snap([product(1, days=days)], [ticker(1)])) == []


def test_dte_84_accepted_due_to_original_pd_cut_boundary():
    rows = screen_long_calls(snap([product(1, days=84)], [ticker(1)]))
    assert rows[0]["dte_group"] == "(77,84]"


def test_original_bid_ask_filter_is_strictly_greater_than_point_zero_five():
    options = [product(1), product(2, days=14), product(3, days=21)]
    quotes = [
        ticker(1, bid="0.05", ask="0.5"),
        ticker(2, bid="0.1", ask="0.5"),
        ticker(3, bid="0.5", ask="0.5"),
    ]
    assert [x["product_id"] for x in screen_long_calls(snap(options, quotes))] == [2, 3]


def test_original_abs_delta_band_and_nearest_delta():
    products = [product(1), product(2, days=14), product(3, days=21)]
    quotes = [
        ticker(1, delta="0.19"),
        ticker(2, delta="-0.4"),
        ticker(3, delta="0.400001"),
    ]
    assert [x["product_id"] for x in screen_long_calls(snap(products, quotes))] == [2]


@pytest.mark.parametrize("change", [
    {"state": "expired"}, {"trading_status": "halted"},
    {"contract_type": "put_options"}, {"contract_value": None},
    {"tick_size": "0"}, {"settlement_time": None},
    {"underlying_asset": {"symbol": "SOL"}},
])
def test_untradable_or_incomplete_product_metadata_is_excluded(change):
    assert screen_long_calls(snap([product(1, **change)], [ticker(1)])) == []


@pytest.mark.parametrize("bid,ask", [
    ("NaN", "13"), ("12", "Infinity"), ("15", "12"),
    ("12.1", "13"), ("-1", "5"),
])
def test_invalid_or_off_tick_quotes_rejected(bid, ask):
    assert screen_long_calls(snap([product(1)], [ticker(1, bid=bid, ask=ask)])) == []


def test_duplicate_metadata_or_quotes_reject_snapshot():
    p = product(1)
    t = ticker(1)
    with pytest.raises(ContractScreenError, match="Duplicate product metadata"):
        screen_long_calls(snap([p, p], [t]))
    with pytest.raises(ContractScreenError, match="Duplicate product ticker"):
        screen_long_calls(snap([p], [t, t]))


def test_reject_missing_snapshot_and_naive_timestamp():
    with pytest.raises(ContractScreenError):
        screen_long_calls(snap([], [], captured_at="2026-10-13T08:30:00"))
    with pytest.raises(ContractScreenError):
        screen_long_calls({"underlying": "BTC"})


def test_snapshot_file_reader_never_requests_exchange(tmp_path):
    import json

    file = tmp_path / "snapshot.json"
    file.write_text(json.dumps(snap([product(1)], [ticker(1)])), encoding="utf-8")
    result = screen_snapshot_file(str(file))
    assert result["mode"] == "READ_ONLY_CONTRACT_SCREEN"
    assert result["per_expiry_candidates"] == 1
