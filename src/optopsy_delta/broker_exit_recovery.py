"""Signed read-only broker reconciliation for a reserved exit attempt.

Never posts orders, retries unknown acknowledgements or authorizes trading.
"""
from __future__ import annotations

import time

from .exit_recovery import ExitRecoveryError
from .position_lifecycle import LifecycleError


def reconcile_exit_broker(*, client, entries, lifecycle, exits, now_us=None):
    """Match all product fills since flat baseline to registered broker orders."""
    baseline = lifecycle.status()
    attempts = exits.status()["attempts"]
    if baseline.get("state") != "OBSERVING" or not attempts:
        raise LifecycleError("Missing baseline or exit attempt")
    pending = attempts[-1]
    if pending["state"] != "PREPARED_UNKNOWN":
        raise ExitRecoveryError("No unresolved exit to reconcile")
    product = baseline["product_id"]
    start = baseline["baseline_us"]
    end = int(time.time() * 1_000_000) if now_us is None else now_us
    if type(end) is not int or end <= start:
        raise ExitRecoveryError("Invalid fill interval")
    if any(a["product_id"] != product for a in attempts):
        raise ExitRecoveryError("Cross-product exit attempts")

    # Read-only lookup. Exceptions leave PREPARED_UNKNOWN untouched.
    known = {}
    previous_buys = 0
    for record in entries.status():
        if record["stage"] not in ("ACKNOWLEDGED", "RECONCILED"):
            raise ExitRecoveryError("Unresolved entry intent")
        broker_id = record["broker_order_id"]
        if not isinstance(broker_id, str) or not broker_id.isdecimal():
            raise ExitRecoveryError("Missing entry broker ID")
        order = client.order_by_id(int(broker_id))
        if (order.get("id") != int(broker_id)
                or order.get("client_order_id") != record["client_id"]
                or order.get("product_id") != product
                or order.get("side") != "buy"
                or order.get("state") not in ("closed", "cancelled")):
            raise ExitRecoveryError("Entry identity or state mismatch")
        size = _qty(order.get("size"), positive=True)
        remaining = _qty(order.get("unfilled_size"))
        if remaining > size or broker_id in known:
            raise ExitRecoveryError("Invalid entry quantity or duplicate")
        known[broker_id] = ("buy", size - remaining)
        previous_buys += size - remaining
    if not known:
        raise ExitRecoveryError("No registered entry ownership evidence")

    previous_sells = 0
    for row in attempts[:-1]:
        if row["state"] != "TERMINAL_VERIFIED":
            raise ExitRecoveryError("Prior exit unresolved")
        identifier = row.get("broker_id")
        if not isinstance(identifier, str) or not identifier.isdecimal():
            raise ExitRecoveryError("Missing previous exit broker ID")
        old = client.order_by_id(int(identifier))
        if (old.get("id") != int(identifier)
                or old.get("client_order_id") != row["client_id"]
                or old.get("product_id") != product
                or old.get("side") != "sell"
                or old.get("reduce_only") is not True
                or old.get("state") not in ("closed", "cancelled")
                or _qty(old.get("size"), positive=True) != row["requested_qty"]):
            raise ExitRecoveryError("Prior exit broker identity mismatch")
        rest = _qty(old.get("unfilled_size"))
        filled = row["requested_qty"] - rest
        if filled < 0 or filled != row["filled_qty"] or identifier in known:
            raise ExitRecoveryError("Prior exit quantity mismatch")
        known[identifier] = ("sell", filled)
        previous_sells += filled
    if previous_buys - previous_sells != pending["before_qty"]:
        raise ExitRecoveryError("Starting inventory inconsistent")

    order = client.order_by_client_id(pending["client_id"])
    if (order.get("client_order_id") != pending["client_id"]
            or order.get("product_id") != product
            or order.get("side") != "sell"
            or order.get("reduce_only") is not True
            or _qty(order.get("size"), positive=True) != pending["requested_qty"]):
        raise ExitRecoveryError("Current exit identity mismatch")
    identifier = order.get("id")
    if type(identifier) is not int or identifier <= 0 or str(identifier) in known:
        raise ExitRecoveryError("Invalid current exit broker ID")
    if order.get("state") not in ("open", "pending", "closed", "cancelled"):
        raise ExitRecoveryError("Unknown exit state")
    rest = _qty(order.get("unfilled_size"))
    if rest > pending["requested_qty"]:
        raise ExitRecoveryError("Bad exit remainder")
    known[str(identifier)] = ("sell", pending["requested_qty"] - rest)

    active = client.active_orders()
    if not isinstance(active, list):
        raise ExitRecoveryError("Invalid active orders")
    if active and (len(active) != 1 or str(active[0].get("id")) != str(identifier)
                   or order["state"] not in ("open", "pending")):
        raise ExitRecoveryError("Foreign or inconsistent active order")
    positions = client.open_positions()
    if not isinstance(positions, list) or any(
        not isinstance(p, dict) or p.get("product_id") != product for p in positions
    ):
        raise ExitRecoveryError("Foreign or unknown position")
    current = client.product_position(product)
    if not isinstance(current, dict) or (
        "product_id" in current and current["product_id"] != product
    ):
        raise ExitRecoveryError("Invalid real-time position")
    position_size = _qty(current.get("size"))
    fills = client.fills_for_product(product, start_time_us=start, end_time_us=end)
    if not isinstance(fills, list):
        raise ExitRecoveryError("Invalid fill history")
    counts = {key: 0 for key in known}
    seen = set()
    exit_fills = []
    for item in fills:
        if not isinstance(item, dict):
            raise ExitRecoveryError("Malformed broker fill")
        fill_id = item.get("id")
        if type(fill_id) not in (str, int) or not str(fill_id) or str(fill_id) in seen:
            raise ExitRecoveryError("Duplicate or missing fill ID")
        seen.add(str(fill_id))
        key = str(item.get("order_id"))
        if key not in known or item.get("product_id") != product:
            raise ExitRecoveryError("Unregistered or external fill")
        side, _ = known[key]
        if item.get("side") != side:
            raise ExitRecoveryError("Fill side mismatch")
        counts[key] += _qty(item.get("size"), positive=True)
        if key == str(identifier):
            exit_fills.append(item)
    if any(counts[key] != expected for key, (_, expected) in known.items()):
        raise ExitRecoveryError("Order and individual fills disagree")
    if position_size != pending["before_qty"] - counts[str(identifier)]:
        raise ExitRecoveryError("Broker inventory mismatch")
    # This is observational. Only the terminal journal state may change.
    result = exits.reconcile_terminal(
        client_order_id=pending["client_id"], order=order,
        fills=exit_fills, position={**current, "product_id": product},
        all_active_orders=active, history_complete=True,
    )
    return {**result, "submit_authorized": False,
            "broker_history_reconciled": True}


def _qty(value, *, positive=False):
    if value is None or isinstance(value, bool):
        raise ExitRecoveryError("Invalid quantity")
    from decimal import Decimal, InvalidOperation

    try:
        n = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExitRecoveryError("Invalid quantity") from exc
    if not n.is_finite() or n != n.to_integral_value() or n < (1 if positive else 0):
        raise ExitRecoveryError("Invalid quantity")
    return int(n)
