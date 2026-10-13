"""Fail-closed consistency check for a single Delta order and its actual fills.

This is *not* position ownership proof or a live exit controller.
The caller must supply a complete authenticated, paginated fill history.
"""
from __future__ import annotations

from decimal import Decimal

from .policy import CapitalPolicyError, dec


class ReconciliationError(ValueError):
    """Broker order / fill / position mismatch. Never assume a fill."""


def _nonnegative(value: object, *, integer: bool = False) -> Decimal:
    try:
        result = dec(value)
    except CapitalPolicyError as exc:
        raise ReconciliationError("Invalid numeric broker field") from exc
    if result < 0 or (integer and result != result.to_integral_value()):
        raise ReconciliationError("Invalid broker quantity")
    return result


def order_fill_status(order: dict, fills: list[dict], position: dict) -> dict:
    """Match a broker-reported order against all its individual fill records.

    Reject mismatched quantities or missing fill evidence. Even a successful
    result cannot infer exclusive ownership of the exchange position: another
    strategy/manual trader may have traded the same product.
    """
    if not isinstance(order, dict) or not isinstance(position, dict):
        raise ReconciliationError("Missing broker order or position")
    product_id = order.get("product_id")
    oid = order.get("id")
    if type(product_id) is not int or product_id <= 0 or type(oid) is not int or oid <= 0:
        raise ReconciliationError("Unrecognized broker order identity")
    side = order.get("side")
    state = order.get("state")
    if side not in ("buy", "sell") or state not in ("open", "pending", "closed", "cancelled"):
        raise ReconciliationError("Unknown order side/state")
    size = _nonnegative(order.get("size"), integer=True)
    remainder = _nonnegative(order.get("unfilled_size"), integer=True)
    if size == 0 or remainder > size:
        raise ReconciliationError("Invalid broker order size")
    position_size = _nonnegative(position.get("size"))
    filled = Decimal(0)
    seen: set[str] = set()
    for row in fills:
        if not isinstance(row, dict):
            raise ReconciliationError("Malformed fill record")
        key = str(row.get("id", ""))
        if not key or key in seen:
            raise ReconciliationError("Missing or duplicate fill")
        seen.add(key)
        if str(row.get("order_id")) != str(oid):
            raise ReconciliationError("Fill belongs to a different order")
        if row.get("product_id") != product_id or row.get("side") != side:
            raise ReconciliationError("Fill product or side mismatch")
        amount = _nonnegative(row.get("size"), integer=True)
        if amount == 0:
            raise ReconciliationError("Zero-quantity fill")
        filled += amount
    if filled != size - remainder:
        raise ReconciliationError("Exchange order and fills disagree")
    return {
        "order_id": oid,
        "product_id": product_id,
        "order_state": state,
        "order_filled_quantity": int(filled),
        "order_unfilled_quantity": int(remainder),
        "position_size": str(position_size),
        "position_ownership_verified": False,
        "eligible_for_automatic_entry_or_exit": False,
    }
