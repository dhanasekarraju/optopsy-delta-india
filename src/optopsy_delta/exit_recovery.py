"""Restart-safe, NO-TRADE accounting for Delta reduce-only IOC exits.

The journal assumes a submitted order may exist immediately after a draft is
reserved. It NEVER posts, cancels, retries, or claims permission to trade.
Every attempt needs independent authenticated order/fill/position reconciliation.
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


class ExitRecoveryError(RuntimeError):
    """Unsafe or inconsistent exit state. Stop rather than infer a fill."""


def _quantity(value: Any, *, positive: bool = False) -> int:
    if value is None or isinstance(value, bool):
        raise ExitRecoveryError("Invalid contract quantity")
    try:
        n = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExitRecoveryError("Invalid contract quantity") from exc
    if not n.is_finite() or n != n.to_integral_value() or n < (1 if positive else 0):
        raise ExitRecoveryError("Invalid contract quantity")
    return int(n)


def _id(value: Any) -> str:
    if type(value) not in (str, int) or not str(value).strip():
        raise ExitRecoveryError("Missing order or fill identity")
    return str(value)


def _valid_draft(draft: dict) -> dict:
    if not isinstance(draft, dict) or (
        draft.get("status") != "DRAFT_EXIT_ONLY"
        or draft.get("submit_authorized") is not False
        or draft.get("side") != "sell"
        or draft.get("reduce_only") is not True
        or draft.get("time_in_force") != "ioc"
        or draft.get("order_type") != "limit_order"
    ):
        raise ExitRecoveryError("Only unsubmitted reduce-only IOC exit drafts accepted")
    product = _quantity(draft.get("product_id"), positive=True)
    total = _quantity(draft.get("remaining_contracts"), positive=True)
    size = _quantity(draft.get("draft_size"), positive=True)
    displayed_depth = _quantity(draft.get("available_bid_depth"), positive=True)
    if draft.get("reason") not in (
        "ORIGINAL_OPTOSPY_RSI_ABOVE_70", "PRE_EXPIRY_SAFETY_CUTOFF"
    ):
        raise ExitRecoveryError("Unsupported exit reason")
    if size > displayed_depth:
        raise ExitRecoveryError("Exit exceeds observed bid depth")
    if size > total:
        raise ExitRecoveryError("Exit draft exceeds reconciled position")
    observed = _quantity(draft.get("quote_observed_unix"), positive=True)
    try:
        price = Decimal(str(draft.get("limit_price")))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ExitRecoveryError("Invalid exit limit price") from exc
    if not price.is_finite() or price <= 0:
        raise ExitRecoveryError("Invalid exit limit price")
    return {"product_id": product, "before_qty": total, "size": size,
            "quote_unix": observed, "limit_price": str(price)}


class ExitAttemptJournal:
    """One active possible broker submission at a time; no order transport.

    PREPARED_UNKNOWN means a POST might have happened even if the process died
    before communicating with the exchange. Thus it is never safe to resend
    that client ID or create another attempt while it remains unresolved.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ExitRecoveryError("Refuse symlinked exit journal")
        with closing(self._db()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT UNIQUE NOT NULL,
                signal_id TEXT UNIQUE NOT NULL,
                product_id INTEGER NOT NULL,
                before_qty INTEGER NOT NULL,
                requested_qty INTEGER NOT NULL,
                quote_unix INTEGER NOT NULL,
                limit_price TEXT NOT NULL,
                draft_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                broker_id TEXT,
                filled_qty INTEGER,
                after_qty INTEGER,
                created_at INTEGER NOT NULL
            )""")
        os.chmod(self.path, 0o600)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def status(self) -> dict:
        with closing(self._db()) as db:
            entries = [dict(row) for row in db.execute(
                "SELECT seq,client_id,signal_id,product_id,before_qty,requested_qty,"
                "state,broker_id,filled_qty,after_qty FROM attempts ORDER BY seq")]
        return {"attempts": entries, "live_order_authorized": False}

    def prepare(self, draft: dict) -> dict:
        clean = _valid_draft(draft)
        body = json.dumps(draft, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
        fingerprint = hashlib.sha256(body.encode()).hexdigest()
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM attempts ORDER BY seq DESC LIMIT 1").fetchone()
            if prior is not None:
                if prior["state"] != "TERMINAL_VERIFIED":
                    raise ExitRecoveryError("Unresolved prior exit attempt: no retry")
                if (prior["product_id"] != clean["product_id"]
                        or prior["after_qty"] != clean["before_qty"]
                        or prior["after_qty"] <= 0):
                    raise ExitRecoveryError("Exit inventory changed or already flat")
                if prior["quote_unix"] >= clean["quote_unix"]:
                    raise ExitRecoveryError("A fresh quote is required for the next exit")
            # An immutable, unique client ID means ambiguous submission
            # never gets reused, even across process restarts.
            sequence = 1 if prior is None else prior["seq"] + 1
            from .live_gateway import _client_id

            signal_id = "exit-" + hashlib.sha256(
                f"exit:{sequence}:{fingerprint}".encode()
            ).hexdigest()[:24]
            cid = _client_id(signal_id, "sell")
            db.execute("""INSERT INTO attempts (
                client_id,signal_id,product_id,before_qty,requested_qty,quote_unix,
                limit_price,draft_hash,state,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                cid, signal_id, clean["product_id"], clean["before_qty"],
                clean["size"], clean["quote_unix"], clean["limit_price"],
                fingerprint, "PREPARED_UNKNOWN", int(time.time()),
            ))
            db.execute("COMMIT")
        return {"client_order_id": cid, "signal_id": signal_id,
                "status": "PREPARED_UNKNOWN", "submit_authorized": False}

    def reconcile_terminal(self, *, client_order_id: str, order: dict | None,
                           fills: list[dict], position: dict,
                           all_active_orders: list[dict],
                           history_complete: bool) -> dict:
        """Clear the ambiguity ONLY with complete terminal broker evidence.

        If order is absent, no successful fill can be inferred. Explicit
        cancellation/closure, individual fills and per-product position must
        all agree. Failure is durable quarantine, never silent retry.
        """
        if not isinstance(client_order_id, str) or not client_order_id:
            raise ExitRecoveryError("Invalid client order ID")
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM attempts WHERE client_id=?",
                             (client_order_id,)).fetchone()
            if row is None:
                raise ExitRecoveryError("Unknown exit client ID")
            if row["state"] != "PREPARED_UNKNOWN":
                raise ExitRecoveryError("Exit is already reconciled or quarantined")
            if order is None:
                # Broker-not-found is not proof a prior POST did not execute.
                raise ExitRecoveryError("Exit acknowledgement still unknown: no retry")
            if isinstance(order, dict) and order.get("state") in ("open", "pending"):
                try:
                    _verify_identity(row, order)
                except ExitRecoveryError:
                    db.execute("UPDATE attempts SET state='QUARANTINED' WHERE client_id=?",
                               (client_order_id,))
                    db.execute("COMMIT")
                    raise
                return {"status": "WAIT_FOR_BROKER_SETTLEMENT",
                        "client_order_id": client_order_id,
                        "submit_authorized": False, "no_automatic_retry": True}
            try:
                result = _verify_terminal(
                    row, order=order, fills=fills, position=position,
                    all_active_orders=all_active_orders,
                    history_complete=history_complete,
                )
            except ExitRecoveryError:
                db.execute("UPDATE attempts SET state='QUARANTINED' WHERE client_id=?",
                           (client_order_id,))
                db.execute("COMMIT")
                raise
            db.execute("""UPDATE attempts SET state='TERMINAL_VERIFIED',
                broker_id=?,filled_qty=?,after_qty=? WHERE client_id=?""", (
                result["broker_order_id"], result["filled_qty"],
                result["remaining_qty"], client_order_id,
            ))
            db.execute("COMMIT")
            return result


def _verify_identity(row: sqlite3.Row, order: dict) -> None:
    if (order.get("client_order_id") != row["client_id"]
            or _quantity(order.get("product_id"), positive=True) != row["product_id"]
            or order.get("side") != "sell"
            or order.get("reduce_only") is not True
            or _quantity(order.get("size"), positive=True) != row["requested_qty"]):
        raise ExitRecoveryError("Broker order identity mismatch")


def _verify_terminal(row: sqlite3.Row, *, order: dict, fills: list[dict],
                     position: dict, all_active_orders: list[dict],
                     history_complete: bool) -> dict:
    if (history_complete is not True or not isinstance(order, dict)
            or not isinstance(fills, list) or not isinstance(position, dict)
            or not isinstance(all_active_orders, list) or all_active_orders):
        raise ExitRecoveryError("Unverifiable terminal order or incomplete fills")
    _verify_identity(row, order)
    if order.get("state") not in ("closed", "cancelled"):
        raise ExitRecoveryError("Broker order not terminal")
    oid = _id(order.get("id"))
    remainder = _quantity(order.get("unfilled_size"))
    if remainder > row["requested_qty"]:
        raise ExitRecoveryError("Broker order remaining size exceeds request")
    actually_filled = row["requested_qty"] - remainder
    counted = 0
    seen: set[str] = set()
    for fill in fills:
        if not isinstance(fill, dict):
            raise ExitRecoveryError("Malformed broker fill")
        fid = _id(fill.get("id"))
        if fid in seen:
            raise ExitRecoveryError("Duplicate broker fill")
        seen.add(fid)
        if (_id(fill.get("order_id")) != oid
                or _quantity(fill.get("product_id"), positive=True) != row["product_id"]
                or fill.get("side") != "sell"):
            raise ExitRecoveryError("Unregistered or mismatched exit fill")
        counted += _quantity(fill.get("size"), positive=True)
    if counted != actually_filled:
        raise ExitRecoveryError("Broker fills disagree with closed order")
    if ("product_id" in position
            and _quantity(position["product_id"], positive=True) != row["product_id"]):
        raise ExitRecoveryError("Current position belongs to another product")
    current = _quantity(position.get("size"))
    expected = row["before_qty"] - counted
    if current != expected:
        raise ExitRecoveryError("Position differs from independently verified exit fills")
    return {"status": "TERMINAL_VERIFIED_FLAT" if current == 0
            else "PARTIAL_OR_UNFILLED_NEEDS_NEW_DRAFT",
            "broker_order_id": oid, "filled_qty": counted,
            "remaining_qty": current, "submit_authorized": False,
            "no_automatic_retry": True}
