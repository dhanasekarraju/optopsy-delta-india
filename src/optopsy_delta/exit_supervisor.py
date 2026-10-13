"""Deterministic, NO-TRADE supervisor for RSI/expiry long-call exits.

This module NEVER connects to Delta or submits orders. It produces a draft
reduce-only IOC close instruction only from a coherent broker reconciliation,
explicit expiry guard, original closed-daily Optopsy signal, and fresh quote.
A draft is not an authorization to execute it.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class ExitSafetyError(ValueError):
    """Invalid input or changed/replayed exit draft: fail closed."""


def _number(value: Any, label: str, *, integer: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ExitSafetyError("Missing or invalid " + label)
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExitSafetyError("Invalid " + label) from exc
    if not amount.is_finite() or amount < 0:
        raise ExitSafetyError("Invalid " + label)
    if integer and amount != amount.to_integral_value():
        raise ExitSafetyError("Fractional " + label)
    return amount


def _datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ExitSafetyError("Timezone-aware " + label + " required")
    return value.astimezone(UTC)


def _answer(status: str, reason: str, product_id: int, qty: int) -> dict:
    return {"status": status, "reason": reason, "product_id": product_id,
            "remaining_contracts": qty, "submit_authorized": False}


def assess_exit(
    *, reconciliation: dict, underlying: str, now: datetime,
    expiry: datetime, safety_cutoff: datetime, original_signal: dict | None,
    quote: dict | None, quote_max_age_s: int = 10,
) -> dict:
    """Assess due exit and produce a draft, never execute it.

    safety_cutoff is a user-approved operational cutoff, distinct from Optopsy's
    historical expiration/exit DTE handling. A higher layer must fetch and
    authenticate/reconcile the actual account and refresh quotes before use.
    """
    now = _datetime(now, "current time")
    expiry = _datetime(expiry, "expiry")
    cutoff = _datetime(safety_cutoff, "safety cutoff")
    if cutoff >= expiry:
        raise ExitSafetyError("Exit safety cutoff must precede expiry")
    if type(quote_max_age_s) is not int or not 1 <= quote_max_age_s <= 10:
        raise ExitSafetyError("Invalid maximum quote age")
    if underlying not in ("BTC", "ETH"):
        raise ExitSafetyError("Unsupported underlying")
    if not isinstance(reconciliation, dict):
        raise ExitSafetyError("Missing broker reconciliation")
    if (reconciliation.get("status") != "CONSISTENT_BUT_NOT_AUTHORIZED"
            or reconciliation.get("consistent") is not True
            or reconciliation.get("live_order_authorized") is not False
            or reconciliation.get("exclusive_ownership_proven") is not False):
        raise ExitSafetyError("Broker reconciliation is not a consistent observation")
    product = reconciliation.get("product_id")
    if type(product) is not int or product <= 0:
        raise ExitSafetyError("Invalid reconciled product ID")
    qty = int(_number(reconciliation.get("reported_position_size"),
                      "position size", integer=True))
    bought = int(_number(reconciliation.get("registered_bought"),
                         "purchased contracts", integer=True))
    sold = int(_number(reconciliation.get("registered_sold"),
                       "sold contracts", integer=True))
    if bought < sold or bought - sold != qty:
        raise ExitSafetyError("Reconciled inventory quantities disagree")
    if qty == 0:
        return _answer("FLAT", "NO_POSITION", product, qty)
    if now >= expiry:
        return _answer("URGENT_MANUAL_RECONCILIATION",
                       "CONTRACT_EXPIRED_OR_SETTLING", product, qty)

    if now >= cutoff:
        reason = "PRE_EXPIRY_SAFETY_CUTOFF"
    else:
        if not isinstance(original_signal, dict):
            return _answer("BLOCKED_NEEDS_ATTENTION", "DAILY_SIGNAL_UNAVAILABLE",
                           product, qty)
        expected_day = (now.date() - timedelta(days=1)).isoformat()
        if (original_signal.get("underlying") != underlying
                or original_signal.get("last_complete_utc_day") != expected_day
                or original_signal.get("strategy") != "long_calls"
                or original_signal.get("exit_rule")
                != "optopsy.rsi_above(period=14, threshold=70)"
                or original_signal.get("parameters_tuned") is not False
                or type(original_signal.get("exit")) is not bool
                or type(original_signal.get("entry")) is not bool
                or (original_signal["entry"] and original_signal["exit"])):
            return _answer("BLOCKED_NEEDS_ATTENTION", "DAILY_SIGNAL_INVALID_OR_STALE",
                           product, qty)
        if original_signal["exit"] is False:
            return _answer("HOLD", "NO_ORIGINAL_RSI_EXIT", product, qty)
        reason = "ORIGINAL_OPTOSPY_RSI_ABOVE_70"

    # An exit is due. Missing/corrupted market data must never silently become HOLD.
    if not isinstance(quote, dict):
        return _answer("EXIT_DUE_BLOCKED", "QUOTE_UNAVAILABLE", product, qty)
    try:
        if quote.get("product_id") != product:
            raise ExitSafetyError("Wrong quote product")
        tick = _number(quote.get("tick_size"), "tick size")
        bid = _number(quote.get("best_bid"), "best bid")
        ask = _number(quote.get("best_ask"), "best ask")
        depth = int(_number(quote.get("best_bid_size"), "bid depth", integer=True))
        observed = quote.get("timestamp_unix")
        if type(observed) is not int:
            raise ExitSafetyError("Missing exchange quote timestamp")
        age = now.timestamp() - observed
        if not (0 <= age <= quote_max_age_s):
            raise ExitSafetyError("Stale/future market quote")
        if tick <= 0 or bid <= 0 or ask < bid or bid % tick != 0:
            raise ExitSafetyError("Untradable or off-tick quote")
        if depth < 1:
            raise ExitSafetyError("No executable bid depth")
    except ExitSafetyError:
        return _answer("EXIT_DUE_BLOCKED", "INVALID_STALE_OR_ILLIQUID_QUOTE", product, qty)
    draft = _answer("DRAFT_EXIT_ONLY", reason, product, qty)
    draft.update({
        "side": "sell", "order_type": "limit_order", "time_in_force": "ioc",
        "reduce_only": True, "limit_price": str(bid),
        "draft_size": min(qty, depth),
        "available_bid_depth": depth,
        "quote_observed_unix": observed,
        "partial_depth": depth < qty,
    })
    return draft


class ExitDraftJournal:
    """A single immutable proposal; no 'send' or 'retry' method exists.

    A later quote, inventory change, or trigger demands human reconciliation.
    This journal intentionally cannot be wired into real order submission.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ExitSafetyError("Refuse symlinked exit journal")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._db()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS draft (
                id INTEGER PRIMARY KEY CHECK(id=1),
                digest TEXT NOT NULL, content TEXT NOT NULL
            )""")
        os.chmod(self.path, 0o600)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def preserve(self, draft: dict) -> dict:
        if (not isinstance(draft, dict) or draft.get("status") != "DRAFT_EXIT_ONLY"
                or draft.get("submit_authorized") is not False
                or draft.get("reduce_only") is not True
                or draft.get("side") != "sell"
                or draft.get("time_in_force") != "ioc"):
            raise ExitSafetyError("Only non-executable reduce-only exit drafts allowed")
        payload = json.dumps(draft, sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest FROM draft WHERE id=1").fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ExitSafetyError("Unresolved prior exit draft: manual review required")
                db.execute("COMMIT")
                return {"status": "EXISTING_DRAFT_NEEDS_REVALIDATION",
                        "digest": fingerprint, "submit_authorized": False}
            db.execute("INSERT INTO draft VALUES(1,?,?)", (fingerprint, payload))
            db.execute("COMMIT")
        return {"status": "NEW_DRAFT_SAVED_NOT_SENT", "digest": fingerprint,
                "submit_authorized": False}

    def status(self) -> dict:
        with closing(self._db()) as db:
            row = db.execute("SELECT digest FROM draft WHERE id=1").fetchone()
        return {"status": "DRAFT_PENDING_MANUAL_REVIEW" if row else "EMPTY",
                "digest": row[0] if row else None, "submit_authorized": False}
