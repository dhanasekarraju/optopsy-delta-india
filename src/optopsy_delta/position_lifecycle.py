"""Durable, fail-closed reconciliation of registered Delta options orders.

No network I/O or order execution exists in this module. A checkpoint means
that supplied broker records agree, NOT proof of exclusive broker ownership.
A dirty/unknown position requires manual reconciliation before any future bot
entry or exit can be authorized by higher layers.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class LifecycleError(RuntimeError):
    """Inconsistent state; never retry or create an order on this basis."""


def _integer(value: Any, field: str, *, allow_zero: bool = True) -> int:
    if type(value) is bool:
        raise LifecycleError(f"Invalid {field}")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LifecycleError(f"Invalid {field}") from exc
    if (not decimal.is_finite() or decimal != decimal.to_integral_value()
            or decimal < (0 if allow_zero else 1)):
        raise LifecycleError(f"Invalid {field}")
    return int(decimal)


def _identity(value: Any, field: str) -> str:
    if type(value) not in (int, str) or not str(value).strip():
        raise LifecycleError(f"Missing {field}")
    return str(value)


def _filled_order(order: dict, *, product_id: int, broker_id: str,
                  client_id: str, side: str, quantity: int) -> int:
    if not isinstance(order, dict):
        raise LifecycleError("Malformed registered order")
    if (_identity(order.get("id"), "order id") != broker_id
            or _integer(order.get("product_id"), "order product", allow_zero=False)
            != product_id or order.get("client_order_id") != client_id
            or order.get("side") != side):
        raise LifecycleError("Broker order identity mismatch")
    size = _integer(order.get("size"), "order quantity", allow_zero=False)
    remain = _integer(order.get("unfilled_size"), "unfilled quantity")
    if size != quantity or remain > size:
        raise LifecycleError("Broker order quantity mismatch")
    if order.get("state") not in ("closed", "cancelled"):
        raise LifecycleError("Unsettled order; stop and reconcile")
    return size - remain


def verify_snapshot(*, product_id: int, registered: list[dict],
                    broker_orders: list[dict], fills: list[dict],
                    position: dict, active_orders: list[dict],
                    history_complete: bool) -> dict:
    """Compare ALL fills since a separately documented flat-account baseline.

    Caller must provide complete unfiltered product fills; this function cannot
    demonstrate that the provider returned every possible fill. Matching state
    is necessary but not sufficient for safe unattended execution.
    """
    if type(product_id) is not int or product_id <= 0 or history_complete is not True:
        raise LifecycleError("Incomplete broker fill history")
    if (not isinstance(registered, list) or not isinstance(broker_orders, list)
            or not isinstance(fills, list) or not isinstance(active_orders, list)
            or not isinstance(position, dict)):
        raise LifecycleError("Invalid broker snapshot")
    if active_orders:
        raise LifecycleError("Unresolved active orders")
    if _integer(position.get("product_id"), "position product", allow_zero=False) != product_id:
        raise LifecycleError("Position product mismatch")
    # Signed real-time position, not an unsigned count: negative inventory fails.
    live_size = _integer(position.get("size"), "position size")
    own: dict[str, dict] = {}
    by_client: set[str] = set()
    for record in registered:
        if not isinstance(record, dict):
            raise LifecycleError("Malformed registered order")
        broker_id = _identity(record.get("broker_order_id"), "registered broker order")
        client_id = _identity(record.get("client_order_id"), "registered client order")
        side = record.get("side")
        if side not in ("buy", "sell") or not client_id.startswith(
            "ob-" if side == "buy" else "os-"
        ):
            raise LifecycleError("Unrecognized registered order side/client ID")
        quantity = _integer(record.get("size"), "registered size", allow_zero=False)
        if broker_id in own or client_id in by_client:
            raise LifecycleError("Duplicate registered order")
        if _integer(record.get("product_id"), "registered product", allow_zero=False) != product_id:
            raise LifecycleError("Cross-product registered order")
        own[broker_id] = {"client_id": client_id, "side": side, "size": quantity}
        by_client.add(client_id)
    if len(broker_orders) != len(own):
        raise LifecycleError("Missing or duplicate registered broker order")
    expected: dict[str, int] = {}
    for order in broker_orders:
        if not isinstance(order, dict):
            raise LifecycleError("Malformed broker order")
        oid = _identity(order.get("id"), "broker order id")
        if oid not in own or oid in expected:
            raise LifecycleError("Unknown or duplicate broker order")
        o = own[oid]
        expected[oid] = _filled_order(
            order, product_id=product_id, broker_id=oid, client_id=o["client_id"],
            side=o["side"], quantity=o["size"],
        )

    counts: dict[str, int] = {key: 0 for key in own}
    fingerprints: dict[str, str] = {}
    bought = sold = 0
    for fill in fills:
        if not isinstance(fill, dict):
            raise LifecycleError("Malformed fill")
        fid = _identity(fill.get("id"), "fill id")
        oid = _identity(fill.get("order_id"), "fill order id")
        if fid in fingerprints or oid not in own:
            raise LifecycleError("Duplicate or external/manual fill")
        if _integer(fill.get("product_id"), "fill product", allow_zero=False) != product_id:
            raise LifecycleError("Cross-product fill")
        o = own[oid]
        if fill.get("side") != o["side"]:
            raise LifecycleError("Fill side mismatch")
        amount = _integer(fill.get("size"), "fill size", allow_zero=False)
        counts[oid] += amount
        if o["side"] == "buy":
            bought += amount
        else:
            sold += amount
        # Fingerprint only immutable trade-identity fields; normalization
        # prevents differently formatted numbers passing as revised fills.
        canonical = {"fill": fid, "order": oid, "product": product_id,
                     "side": o["side"], "quantity": amount}
        fingerprints[fid] = hashlib.sha256(
            json.dumps(canonical, sort_keys=True).encode()
        ).hexdigest()
    if counts != expected:
        raise LifecycleError("Broker order versus individual fills disagree")
    inventory = bought - sold
    if inventory < 0 or inventory != live_size:
        raise LifecycleError("Ledger inventory does not match real-time broker position")
    return {"product_id": product_id, "reported_position_size": live_size,
            "registered_bought": bought, "registered_sold": sold,
            "fill_fingerprints": fingerprints,
            "consistent": True, "live_order_authorized": False,
            "exclusive_ownership_proven": False}


class LifecycleJournal:
    """Persist full fill snapshots; quarantine on discrepancies across restarts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise LifecycleError("Refuse symlinked position ledger")
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS lifecycle (
                id INTEGER PRIMARY KEY CHECK(id = 1), product_id INTEGER NOT NULL,
                baseline_us INTEGER NOT NULL, quarantined INTEGER NOT NULL,
                snapshot TEXT NOT NULL, updated_at INTEGER NOT NULL
            )""")
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def register_flat_baseline(self, *, product_id: int, baseline_us: int,
                               account_positions: list, active_orders: list) -> None:
        """Only enroll when account-wide GET lists are both empty.

        API latency/history incompleteness remains a reason not to trade.
        A separate execution controller must prove the baseline was timely.
        """
        if (type(product_id) is not int or product_id <= 0
                or type(baseline_us) is not int or baseline_us <= 0
                or not isinstance(account_positions, list)
                or not isinstance(active_orders, list)
                or account_positions or active_orders):
            raise LifecycleError("Cannot establish empty-account baseline")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM lifecycle WHERE id=1").fetchone():
                raise LifecycleError("Baseline already exists; never silently reset")
            db.execute("INSERT INTO lifecycle VALUES(1,?,?,0,?,?)", (
                product_id, baseline_us, json.dumps({"fills": {}}, sort_keys=True),
                int(time.time()),
            ))
            db.execute("COMMIT")

    def status(self) -> dict:
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT * FROM lifecycle WHERE id=1").fetchone()
            if row is None:
                return {"state": "NO_BASELINE", "live_order_authorized": False}
            snap = json.loads(row["snapshot"])
            return {"state": "QUARANTINED" if row["quarantined"] else "OBSERVING",
                    "product_id": row["product_id"], "baseline_us": row["baseline_us"],
                    "observed_fill_count": len(snap["fills"]),
                    "live_order_authorized": False}

    def reconcile(self, *, start_us: int, end_us: int, product_id: int,
                  registered: list[dict], broker_orders: list[dict],
                  fills: list[dict], position: dict,
                  active_orders: list[dict], history_complete: bool) -> dict:
        if (type(start_us) is not int or type(end_us) is not int
                or start_us <= 0 or end_us <= start_us):
            raise LifecycleError("Invalid fill history time coverage")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM lifecycle WHERE id=1").fetchone()
            if row is None:
                raise LifecycleError("No registered flat baseline")
            if row["quarantined"]:
                raise LifecycleError("Journal is quarantined; manual resolution required")
            try:
                if row["product_id"] != product_id or start_us > row["baseline_us"]:
                    raise LifecycleError("Baseline not covered by supplied fill history")
                if end_us <= row["baseline_us"]:
                    raise LifecycleError("Fill history ends before baseline")
                result = verify_snapshot(
                    product_id=product_id, registered=registered,
                    broker_orders=broker_orders, fills=fills, position=position,
                    active_orders=active_orders, history_complete=history_complete,
                )
                previous = json.loads(row["snapshot"])["fills"]
                current = result["fill_fingerprints"]
                if any(current.get(k) != v for k, v in previous.items()):
                    raise LifecycleError("Previously verified fill changed or disappeared")
            except LifecycleError:
                db.execute("UPDATE lifecycle SET quarantined=1,updated_at=? WHERE id=1",
                           (int(time.time()),))
                db.execute("COMMIT")
                raise
            db.execute("UPDATE lifecycle SET snapshot=?,updated_at=? WHERE id=1", (
                json.dumps({"fills": result["fill_fingerprints"]}, sort_keys=True),
                int(time.time()),
            ))
            db.execute("COMMIT")
            result.pop("fill_fingerprints")
            result["status"] = "CONSISTENT_BUT_NOT_AUTHORIZED"
            return result
