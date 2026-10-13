"""Isolated long-option PAPER lifecycle; never sends broker orders.

This is infrastructure, not an Optopsy strategy implementation or signal generator.
Quotes and entries must be supplied explicitly by a trusted later integration.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .policy import CapitalPolicy, CapitalPolicyError, dec


class PaperError(ValueError):
    pass


def _amount(x: object, label: str, *, zero_ok: bool = False) -> Decimal:
    try:
        v = dec(x)
    except CapitalPolicyError as exc:
        raise PaperError(f"Invalid {label}") from exc
    if v < 0 or (v == 0 and not zero_ok):
        raise PaperError(f"Invalid {label}")
    return v


def _instant(d: datetime) -> str:
    if not isinstance(d, datetime) or d.tzinfo is None:
        raise PaperError("Timezone-aware timestamp required")
    return d.astimezone(UTC).isoformat()


def _check_quote(quoted: datetime, now: datetime) -> None:
    _instant(quoted)
    _instant(now)
    age = now - quoted
    if age > timedelta(seconds=30) or age < -timedelta(seconds=2):
        raise PaperError("Stale or future quote")


def _id(s: str) -> None:
    if not isinstance(s, str) or not 1 <= len(s) <= 64 or not all(
        c.isascii() and (c.isalnum() or c in "._:-") for c in s
    ):
        raise PaperError("Invalid stable event identifier")


def _hash(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


class PaperLedger:
    """Transactional journal, idempotent entries/exits, aggregate INR cash ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.policy = CapitalPolicy()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS entries(
                    signal TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    strategy TEXT NOT NULL, underlying TEXT NOT NULL,
                    product_id INTEGER NOT NULL, size INTEGER NOT NULL,
                    remaining INTEGER NOT NULL, contract TEXT NOT NULL,
                    debit TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    event_id TEXT PRIMARY KEY, signal TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, side TEXT NOT NULL,
                    size INTEGER NOT NULL, price TEXT NOT NULL,
                    cash_delta TEXT NOT NULL, recorded_at TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode='PAPER')
                );
                CREATE TRIGGER IF NOT EXISTS events_no_update
                BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable journal'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable journal'); END;
            """)
        os.chmod(self.path, 0o600)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _cash(self, db: sqlite3.Connection) -> Decimal:
        return self.policy.budget_inr + sum(
            (Decimal(row[0]) for row in db.execute("SELECT cash_delta FROM events ORDER BY rowid")),
            Decimal(0),
        )

    def enter(
        self, *, signal: str, strategy: str, underlying: str,
        product_id: int, size: int, ask: object, contract: object,
        quoted_at: datetime, now: datetime,
    ) -> dict:
        _id(signal)
        if strategy not in ("long_calls", "long_puts"):
            raise PaperError("Only long call/put paper labels supported")
        if underlying not in ("BTC", "ETH") or type(product_id) is not int or product_id <= 0:
            raise PaperError("Invalid contract identity")
        if type(size) is not int or size <= 0:
            raise PaperError("Invalid size")
        px, cv = _amount(ask, "ask"), _amount(contract, "contract")
        fingerprint = _hash({"signal": signal, "strategy": strategy, "underlying": underlying,
            "product_id": product_id, "size": size, "ask": str(px), "contract": str(cv),
            "quote": _instant(quoted_at)})
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fingerprint, debit FROM entries WHERE signal=?", (signal,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise PaperError("Conflicting duplicate signal")
                db.commit()
                return {"state": "ALREADY_RECORDED", "debit_inr": old["debit"]}
            _check_quote(quoted_at, now)
            try:
                quote = self.policy.estimate_long_option(
                    limit_price_usd=str(px), contract_value=str(cv), size=size,
                    available_usd=str(max(self._cash(db), Decimal(0)) / self.policy.usd_inr),
                )
            except CapitalPolicyError as exc:
                raise PaperError("INR capital limit or reserve breached") from exc
            debit = quote["estimate_with_buffer_inr"]
            db.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)",
                (signal, fingerprint, strategy, underlying, product_id, size, size, str(cv), debit))
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                ("entry:"+signal, signal, fingerprint, "BUY", size, str(px),
                 str(-Decimal(debit)), _instant(now), "PAPER"))
            db.commit()
            return {"state": "PAPER_ENTERED", "debit_inr": debit}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def exit(
        self, *, exit_id: str, signal: str, size: int, bid: object,
        quoted_at: datetime, now: datetime,
    ) -> dict:
        _id(exit_id)
        _id(signal)
        if type(size) is not int or size <= 0:
            raise PaperError("Invalid exit size")
        px = _amount(bid, "bid", zero_ok=True)
        fingerprint = _hash({"exit_id": exit_id, "signal": signal, "size": size,
                                 "bid": str(px), "quote": _instant(quoted_at)})
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fingerprint, cash_delta FROM events WHERE event_id=?",
                             ("exit:"+exit_id,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise PaperError("Conflicting duplicate exit")
                db.commit()
                return {"state": "ALREADY_RECORDED", "proceeds_inr": old["cash_delta"]}
            _check_quote(quoted_at, now)
            row = db.execute("SELECT remaining, contract FROM entries WHERE signal=?",
                             (signal,)).fetchone()
            if not row:
                raise PaperError("Unknown paper position")
            if size > row["remaining"]:
                raise PaperError("Exit exceeds remaining quantity")
            proceeds = (px * Decimal(row["contract"]) * size * self.policy.usd_inr
                        * (Decimal(1) - self.policy.max_premium_fee_buffer))
            db.execute("UPDATE entries SET remaining=remaining-? WHERE signal=?", (size, signal))
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                ("exit:"+exit_id, signal, fingerprint, "SELL", size, str(px),
                 str(proceeds), _instant(now), "PAPER"))
            db.commit()
            return {"state": "PAPER_EXITED", "proceeds_inr": str(proceeds)}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def status(self) -> dict:
        db = self._db()
        try:
            return {
                "mode": "PAPER",
                "cash_estimate_inr": str(self._cash(db)),
                "open_positions": [dict(r) for r in db.execute(
                    "SELECT signal, strategy, underlying, product_id, remaining "
                    "FROM entries WHERE remaining>0 ORDER BY signal"
                )],
                "events": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "note": "Hypothetical ask/bid fills; not Delta transactions",
            }
        finally:
            db.close()
