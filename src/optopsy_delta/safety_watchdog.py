"""One-shot, nontrading safety watchdog with an append-only local incident journal.

Missing broker data is never interpreted as zero exposure. This module has no
network write, cancel, close, notification delivery, or order authorization.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .client import SignatureError


class SafetyWatchdogError(ValueError):
    """Invalid or untrusted safety observation."""


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SafetyWatchdogError(f"Timezone-aware {name} required")
    return value.astimezone(UTC)


def _size(value: Any) -> int:
    if value is None or isinstance(value, bool):
        raise SafetyWatchdogError("Invalid product position quantity")
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SafetyWatchdogError("Invalid product position quantity") from exc
    if (not quantity.is_finite() or quantity < 0
            or quantity != quantity.to_integral_value()):
        raise SafetyWatchdogError("Invalid product position quantity")
    return int(quantity)


def observe_safety(
    *, now: datetime, broker_checked_at: datetime | None, broker_healthy: bool,
    account_positions: Any, active_orders: Any, product_id: int | None,
    product_position: Any, lifecycle_status: Any, exit_status: Any,
    expiry: datetime | None = None, exit_cutoff: datetime | None = None,
    max_broker_age_seconds: int = 30,
    broker_unavailable_reason: str | None = None,
) -> dict:
    """Evaluate an observation, always refusing trade authorization."""
    now = _utc(now, "now")
    if type(broker_healthy) is not bool:
        raise SafetyWatchdogError("Invalid broker health flag")
    if broker_unavailable_reason not in (None, "CREDENTIALS_MISSING"):
        raise SafetyWatchdogError("Invalid broker failure classification")
    if broker_healthy and broker_unavailable_reason is not None:
        raise SafetyWatchdogError("Healthy broker cannot have a failure reason")
    if type(max_broker_age_seconds) is not int or not 1 <= max_broker_age_seconds <= 60:
        raise SafetyWatchdogError("Invalid observation freshness window")
    if product_id is not None and (type(product_id) is not int or product_id <= 0):
        raise SafetyWatchdogError("Invalid product binding")
    if (expiry is None) != (exit_cutoff is None):
        raise SafetyWatchdogError("Supply both expiry and exit cutoff")
    if expiry is not None:
        expiry = _utc(expiry, "expiry")
        exit_cutoff = _utc(exit_cutoff, "exit cutoff")
        if expiry <= exit_cutoff or product_id is None:
            raise SafetyWatchdogError("Invalid expiry safety guard")

    issues: set[str] = set()
    exposure = None
    if not broker_healthy:
        issues.add(
            "BROKER_CREDENTIALS_MISSING" if broker_unavailable_reason == "CREDENTIALS_MISSING"
            else "BROKER_UNREACHABLE_OR_INVALID"
        )
    else:
        if broker_checked_at is None:
            issues.add("BROKER_OBSERVATION_TIMESTAMP_MISSING")
        else:
            age = (now - _utc(broker_checked_at, "broker checked at")).total_seconds()
            if not -2 <= age <= max_broker_age_seconds:
                issues.add("BROKER_OBSERVATION_STALE_OR_FUTURE")
        if (not isinstance(account_positions, list)
                or not isinstance(active_orders, list)
                or any(not isinstance(x, dict) for x in account_positions)
                or any(not isinstance(x, dict) for x in active_orders)):
            issues.add("BROKER_ACCOUNT_SCHEMA_INVALID")
        else:
            if account_positions:
                issues.add("ACCOUNT_POSITIONS_PRESENT")
            if active_orders:
                issues.add("ACCOUNT_ACTIVE_ORDERS_PRESENT")
            if product_id is not None and any(
                row.get("product_id") != product_id for row in account_positions
            ):
                issues.add("FOREIGN_OR_UNKNOWN_POSITION")
        if product_id is not None:
            if not isinstance(product_position, dict):
                issues.add("PRODUCT_POSITION_UNAVAILABLE")
            else:
                if ("product_id" in product_position
                        and product_position["product_id"] != product_id):
                    issues.add("PRODUCT_POSITION_ID_MISMATCH")
                try:
                    exposure = _size(product_position.get("size"))
                except SafetyWatchdogError:
                    issues.add("PRODUCT_POSITION_INVALID")
    if product_id is None:
        issues.add("PRODUCT_NOT_BOUND")
    if not isinstance(lifecycle_status, dict):
        issues.add("LIFECYCLE_JOURNAL_UNAVAILABLE")
    elif lifecycle_status.get("state") != "OBSERVING":
        issues.add("LIFECYCLE_NOT_OBSERVING")
    elif lifecycle_status.get("product_id") != product_id:
        issues.add("LIFECYCLE_PRODUCT_MISMATCH")
    if not isinstance(exit_status, dict) or not isinstance(exit_status.get("attempts"), list):
        issues.add("EXIT_JOURNAL_UNAVAILABLE")
    else:
        attempts = exit_status["attempts"]
        if any(not isinstance(x, dict) or x.get("product_id") != product_id
               for x in attempts):
            issues.add("EXIT_ATTEMPTS_INVALID_OR_WRONG_PRODUCT")
        if attempts and isinstance(attempts[-1], dict):
            state = attempts[-1].get("state")
            if state == "PREPARED_UNKNOWN":
                issues.add("EXIT_ACKNOWLEDGEMENT_UNRESOLVED")
            elif state == "QUARANTINED":
                issues.add("EXIT_ATTEMPT_QUARANTINED")
            elif state != "TERMINAL_VERIFIED":
                issues.add("EXIT_ATTEMPT_STATE_UNRECOGNIZED")
    if exposure is not None and exposure > 0:
        if expiry is None:
            issues.add("OPEN_POSITION_EXPIRY_GUARD_MISSING")
        elif now >= expiry:
            issues.add("OPEN_POSITION_EXPIRED_SETTLEMENT_REVIEW")
        elif now >= exit_cutoff:
            issues.add("OPEN_POSITION_EXIT_CUTOFF_REACHED")
    blocking = sorted(issues)
    return {
        "mode": "READ_ONLY_SAFETY_WATCHDOG",
        "status": "ATTENTION_REQUIRED" if blocking else "OBSERVATION_CLEAR_NOT_AUTHORIZED",
        "issues": blocking,
        "account_position_rows": len(account_positions)
        if broker_healthy and isinstance(account_positions, list) else None,
        "active_order_rows": len(active_orders)
        if broker_healthy and isinstance(active_orders, list) else None,
        "product_position_quantity": exposure,
        "broker_healthy": broker_healthy,
        "account_wide_positions_may_lag": True,
        "live_entry_authorized": False,
        "live_exit_authorized": False,
        "autonomous_protection_active": False,
    }


def read_only_watchdog_check(
    *, client, product_id: int | None = None, lifecycle_status: Any = None,
    exit_status: Any = None, expiry: datetime | None = None,
    exit_cutoff: datetime | None = None, now: datetime | None = None,
) -> dict:
    """GET only, one time; redact all broker failures."""
    override_now = _utc(now, "now") if now is not None else None
    checked_at = None
    broker_unavailable_reason = None
    try:
        positions = client.open_positions()
        orders = client.active_orders()
        product = client.product_position(product_id) if product_id is not None else None
        checked_at = datetime.now(UTC) if override_now is None else override_now
        healthy = True
    except Exception as exc:  # noqa: BLE001 - boundary must redact and fail closed
        broker_unavailable_reason = (
            "CREDENTIALS_MISSING" if isinstance(exc, SignatureError) else None
        )
        positions = None
        orders = None
        product = None
        healthy = False
    evaluated_at = datetime.now(UTC) if override_now is None else override_now
    return observe_safety(
        now=evaluated_at, broker_checked_at=checked_at,
        broker_healthy=healthy, account_positions=positions, active_orders=orders,
        product_id=product_id, product_position=product,
        lifecycle_status=lifecycle_status, exit_status=exit_status,
        expiry=expiry, exit_cutoff=exit_cutoff,
        broker_unavailable_reason=broker_unavailable_reason,
    )


class SafetyIncidentJournal:
    """Append-only state transitions; no remote notifications or self-healing."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise SafetyWatchdogError("Refuse symlinked incident journal")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._db()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                issues TEXT NOT NULL
            )""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS deny_watchdog_update
                BEFORE UPDATE ON observations BEGIN
                SELECT RAISE(ABORT, 'Append-only watchdog'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS deny_watchdog_delete
                BEFORE DELETE ON observations BEGIN
                SELECT RAISE(ABORT, 'Append-only watchdog'); END""")
        os.chmod(self.path, 0o600)

    def _db(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def record(self, observation: dict, *, observed_at: int) -> dict:
        if (not isinstance(observation, dict)
                or observation.get("mode") != "READ_ONLY_SAFETY_WATCHDOG"
                or observation.get("live_entry_authorized") is not False
                or observation.get("live_exit_authorized") is not False
                or observation.get("autonomous_protection_active") is not False
                or observation.get("status") not in (
                    "ATTENTION_REQUIRED", "OBSERVATION_CLEAR_NOT_AUTHORIZED"
                ) or not isinstance(observation.get("issues"), list)
                or type(observed_at) is not int or observed_at <= 0):
            raise SafetyWatchdogError("Invalid nontrading safety observation")
        issues = observation["issues"]
        if (issues != sorted(set(issues))
                or any(not isinstance(x, str) or not x for x in issues)
                or bool(issues) != (observation["status"] == "ATTENTION_REQUIRED")):
            raise SafetyWatchdogError("Invalid safety incident list")
        canonical = json.dumps([observation["status"], issues], separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT fingerprint FROM observations ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if prior and prior[0] == digest:
                db.execute("COMMIT")
                return {"recorded": False, "reason": "UNCHANGED", "live_order_authorized": False}
            db.execute("INSERT INTO observations (observed_at,fingerprint,status,issues)"
                       " VALUES (?,?,?,?)", (
                           observed_at, digest, observation["status"],
                           json.dumps(issues, separators=(",", ":")),
                       ))
            db.execute("COMMIT")
        return {"recorded": True, "reason": "STATE_TRANSITION",
                "live_order_authorized": False}

    def status(self) -> dict:
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT id,status,issues FROM observations ORDER BY id"
            ).fetchall()
        return {
            "transitions": len(rows),
            "latest_status": rows[-1][1] if rows else "NO_OBSERVATIONS",
            "latest_issues": json.loads(rows[-1][2]) if rows else [],
            "live_order_authorized": False,
        }
