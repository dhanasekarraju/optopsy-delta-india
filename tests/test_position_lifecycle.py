"""Broker snapshot ownership and durable restart recovery, no real orders."""
import sqlite3

import pytest

from optopsy_delta.position_lifecycle import (
    LifecycleError,
    LifecycleJournal,
    verify_snapshot,
)

PRODUCT = 157768


def registered(broker=123, client="ob-abc", side="buy", size=2):
    return {"broker_order_id": broker, "client_order_id": client,
            "product_id": PRODUCT, "side": side, "size": size}


def order(broker=123, client="ob-abc", side="buy", size=2,
          unfilled=0, state="closed"):
    return {"id": broker, "client_order_id": client, "product_id": PRODUCT,
            "side": side, "size": size, "unfilled_size": unfilled, "state": state}


def fill(fid, broker=123, side="buy", amount=1):
    return {"id": fid, "order_id": broker, "product_id": PRODUCT,
            "side": side, "size": amount}


def packet(*, reg=None, orders=None, fills=None, size=0, pending=None):
    return {"product_id": PRODUCT, "registered": reg or [],
            "broker_orders": orders or [], "fills": fills or [],
            "position": {"product_id": PRODUCT, "size": size},
            "active_orders": pending or [], "history_complete": True}


def test_empty_is_consistent_but_cannot_authorize():
    x = verify_snapshot(**packet())
    assert x["reported_position_size"] == 0
    assert x["live_order_authorized"] is False
    assert x["exclusive_ownership_proven"] is False


def test_partial_ioc_fill_and_matching_inventory():
    x = verify_snapshot(**packet(
        reg=[registered()], orders=[order(unfilled=1)],
        fills=[fill("a")], size=1))
    assert x["registered_bought"] == 1
    assert x["reported_position_size"] == 1


def test_entry_and_partial_exit():
    reg = [registered(), registered(124, "os-exit", "sell", 2)]
    orders = [order(), order(124, "os-exit", "sell", unfilled=1)]
    fills = [fill("a"), fill("b"), fill("c", 124, "sell")]
    x = verify_snapshot(**packet(reg=reg, orders=orders, fills=fills, size=1))
    assert x["registered_bought"] == 2
    assert x["registered_sold"] == 1


@pytest.mark.parametrize("bad", [
    {"fills": [fill("manual", 555)]},
    {"fills": [fill("a"), fill("a")]},
    {"fills": [fill("a", side="sell")]},
    {"fills": [fill("a", amount=0)]},
    {"fills": [fill("a", amount=-1)]},
    {"fills": [fill("a", amount="NaN")]},
    {"fills": [fill("a", amount="1.5")]},
    {"size": -1},
    {"size": 2},
    {"pending": [order(state="open")]},
    {"orders": [order(state="pending")]},
    {"orders": [order(unfilled=0)]},
    {"orders": [order(client="ob-wrong", unfilled=1)]},
    {"orders": [order(unfilled=1, size=3)]},
])
def test_fail_closed_missing_duplicate_external_mismatched(bad):
    p = packet(reg=[registered()], orders=[order(unfilled=1)],
               fills=[fill("a")], size=1)
    if "size" in bad:
        p["position"]["size"] = bad["size"]
    if "pending" in bad:
        p["active_orders"] = bad["pending"]
    if "orders" in bad:
        p["broker_orders"] = bad["orders"]
    if "fills" in bad:
        p["fills"] = bad["fills"]
    with pytest.raises(LifecycleError):
        verify_snapshot(**p)


def test_unregistered_sale_and_short_inventory_refused():
    reg = [registered(124, "os-exit", "sell", 1)]
    orders = [order(124, "os-exit", "sell", 1, 0)]
    with pytest.raises(LifecycleError, match="inventory"):
        verify_snapshot(**packet(reg=reg, orders=orders,
                                 fills=[fill("a", 124, "sell")], size=0))


def test_incomplete_history_refused():
    with pytest.raises(LifecycleError, match="Incomplete"):
        verify_snapshot(**(packet() | {"history_complete": False}))


def journal(tmp_path):
    return LifecycleJournal(tmp_path / "private-owned.sqlite")


def test_no_baseline_no_reconciliation(tmp_path):
    j = journal(tmp_path)
    assert j.status()["state"] == "NO_BASELINE"
    with pytest.raises(LifecycleError, match="No registered"):
        j.reconcile(start_us=1, end_us=3, **packet())


def test_registration_requires_flat_account_and_cannot_reset(tmp_path):
    j = journal(tmp_path)
    with pytest.raises(LifecycleError):
        j.register_flat_baseline(product_id=PRODUCT, baseline_us=5,
                                 account_positions=[{}], active_orders=[])
    j.register_flat_baseline(product_id=PRODUCT, baseline_us=5,
                             account_positions=[], active_orders=[])
    with pytest.raises(LifecycleError, match="already exists"):
        j.register_flat_baseline(product_id=PRODUCT, baseline_us=5,
                                 account_positions=[], active_orders=[])
    assert j.path.stat().st_mode & 0o777 == 0o600


def test_restart_recovers_observed_fills_and_rejects_history_rewrite(tmp_path):
    j = journal(tmp_path)
    j.register_flat_baseline(product_id=PRODUCT, baseline_us=100,
                             account_positions=[], active_orders=[])
    first = packet(reg=[registered()], orders=[order(unfilled=1)],
                   fills=[fill("a")], size=1)
    assert j.reconcile(start_us=100, end_us=200, **first)["status"] == (
        "CONSISTENT_BUT_NOT_AUTHORIZED")
    again = journal(tmp_path)
    assert again.status()["observed_fill_count"] == 1
    assert again.reconcile(start_us=99, end_us=201, **first)["registered_bought"] == 1
    altered = packet(reg=[registered()], orders=[order(unfilled=1)],
                     fills=[fill("DIFFERENT")], size=1)
    with pytest.raises(LifecycleError, match="changed or disappeared"):
        again.reconcile(start_us=99, end_us=202, **altered)
    assert journal(tmp_path).status()["state"] == "QUARANTINED"
    with pytest.raises(LifecycleError, match="quarantined"):
        again.reconcile(start_us=99, end_us=203, **first)


def test_invalid_broker_data_persistently_quarantines(tmp_path):
    j = journal(tmp_path)
    j.register_flat_baseline(product_id=PRODUCT, baseline_us=10,
                             account_positions=[], active_orders=[])
    with pytest.raises(LifecycleError, match="Incomplete"):
        j.reconcile(start_us=10, end_us=20,
                    **(packet() | {"history_complete": False}))
    assert journal(tmp_path).status()["state"] == "QUARANTINED"


def test_window_must_cover_baseline_and_failure_is_sticky(tmp_path):
    j = journal(tmp_path)
    j.register_flat_baseline(product_id=PRODUCT, baseline_us=10,
                             account_positions=[], active_orders=[])
    with pytest.raises(LifecycleError, match="Baseline not covered"):
        j.reconcile(start_us=11, end_us=20, **packet())
    assert j.status()["state"] == "QUARANTINED"


def test_sqlite_persistent_single_row(tmp_path):
    j = journal(tmp_path)
    j.register_flat_baseline(product_id=PRODUCT, baseline_us=5,
                             account_positions=[], active_orders=[])
    assert j.reconcile(start_us=5, end_us=100, **packet())["reported_position_size"] == 0
    with sqlite3.connect(j.path) as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0] == 1
