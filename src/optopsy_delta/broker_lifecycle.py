"""Read-only Delta broker-to-lifecycle reconciliation; never submits orders.

Registered order IDs MUST already be present in the durable intent journal.
An API snapshot is not sufficient to approve autonomous trading.
"""
from __future__ import annotations

import time

from .client import DeltaClient, DeltaError
from .exit_recovery import ExitAttemptJournal
from .live_gateway import LiveIntentJournal
from .position_lifecycle import LifecycleError, LifecycleJournal


def reconcile_broker(
    *, client: DeltaClient, intent_journal: LiveIntentJournal,
    lifecycle: LifecycleJournal, now_us: int | None = None,
    exit_attempts: ExitAttemptJournal | None = None,
) -> dict:
    """Reconcile exact broker order IDs and complete product fill history.

    Fail closed on missing baseline, uncertain intents, nonempty other
    positions/orders, unverifiable history, or broker disagreement.
    """
    status = lifecycle.status()
    if status["state"] != "OBSERVING":
        raise LifecycleError("Lifecycle journal has no usable flat baseline")
    product = status["product_id"]
    baseline = status["baseline_us"]
    now_us = int(time.time() * 1_000_000) if now_us is None else now_us
    if type(now_us) is not int or now_us <= baseline:
        raise LifecycleError("Invalid reconciliation end time")

    intent_records = intent_journal.status()
    # The older intent journal stores the fingerprint and broker ID but not
    # product and side in status(), so resolve those through broker order GET.
    registered = []
    broker_orders = []
    for record in intent_records:
        if record.get("stage") not in ("ACKNOWLEDGED", "RECONCILED"):
            raise LifecycleError("Ambiguous or unresolved order intent")
        identifier = record.get("broker_order_id")
        if not isinstance(identifier, str) or not identifier.isdecimal():
            raise LifecycleError("Missing registered broker ID")
        broker_id = int(identifier)
        order = client.order_by_id(broker_id)
        client_id = record["client_id"]
        if order.get("client_order_id") != client_id:
            raise LifecycleError("Intent versus broker client ID mismatch")
        registered.append({
            "broker_order_id": broker_id,
            "client_order_id": client_id,
            "product_id": order.get("product_id"),
            "side": order.get("side"),
            "size": order.get("size"),
        })
        broker_orders.append(order)

    if exit_attempts is not None:
        # A settled, independently verified reduce-only exit is a REGISTERED
        # historical order, not a manual trade. Pending/unknown exits still
        # block reconciliation and any subsequent exit draft.
        for attempt in exit_attempts.status()["attempts"]:
            if attempt.get("state") != "TERMINAL_VERIFIED":
                raise LifecycleError("Unresolved exit attempt blocks position reconciliation")
            oid = attempt.get("broker_id")
            client_id = attempt.get("client_id")
            if (attempt.get("product_id") != product
                    or not isinstance(oid, str) or not oid.isdecimal()
                    or not isinstance(client_id, str) or not client_id.startswith("os-")):
                raise LifecycleError("Invalid verified exit identity")
            order = client.order_by_id(int(oid))
            if (order.get("id") != int(oid)
                    or order.get("client_order_id") != client_id
                    or order.get("product_id") != product
                    or order.get("side") != "sell"
                    or order.get("reduce_only") is not True
                    or order.get("state") not in ("closed", "cancelled")
                    or attempt.get("requested_qty") != order.get("size")
                    or type(order.get("unfilled_size")) is not int
                    or order["unfilled_size"] < 0
                    or order["unfilled_size"] > order["size"]
                    or attempt.get("filled_qty") != (
                        order["size"] - order["unfilled_size"]
                    )):
                raise LifecycleError("Historical exit differs from independently verified broker order")
            registered.append({
                "broker_order_id": int(oid),
                "client_order_id": client_id,
                "product_id": product,
                "side": "sell",
                "size": order["size"],
            })
            broker_orders.append(order)

    all_positions = client.open_positions()
    if not isinstance(all_positions, list):
        raise LifecycleError("Unexpected account-wide position response")
    # We cannot yet isolate all account-wide positions to this product,
    # so refuse any foreign or unparseable records rather than ignore them.
    for position in all_positions:
        if not isinstance(position, dict) or position.get("product_id") != product:
            raise LifecycleError("Unrelated or unknown account-wide broker position")

    active = client.active_orders()
    if active:
        raise LifecycleError("Unresolved active broker orders")

    fills = client.fills_for_product(
        product, start_time_us=baseline, end_time_us=now_us
    )
    current = client.product_position(product)
    if "product_id" in current and current["product_id"] != product:
        raise LifecycleError("Per-product position identity mismatch")
    current = {**current, "product_id": product}
    try:
        return lifecycle.reconcile(
            start_us=baseline, end_us=now_us, product_id=product,
            registered=registered, broker_orders=broker_orders,
            fills=fills, position=current,
            active_orders=active, history_complete=True,
        )
    except (DeltaError, ValueError) as exc:
        raise LifecycleError("Broker lifecycle reconciliation unavailable") from exc
