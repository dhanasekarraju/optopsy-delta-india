"""Read-only accounting and operational status for the PAPER worker."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path


class PaperReportError(RuntimeError):
    pass


def read_paper_status(path: str | Path) -> dict:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        return {"mode": "PAPER", "status": "NO_LEDGER", "real_orders_sent": 0}
    try:
        with closing(sqlite3.connect(f"file:{source.absolute()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT cash,position,updated_at FROM account WHERE id=1").fetchone()
            fills = [dict(x) for x in db.execute(
                "SELECT seq,action_id,side,underlying,product_id,quantity,"
                "price_usd,premium_inr,fee_inr,created_at FROM fills ORDER BY seq"
            )]
            try:
                heartbeat = db.execute(
                    "SELECT observed_at,last_event,error_type FROM paper_heartbeat WHERE id=1"
                ).fetchone()
            except sqlite3.OperationalError:
                heartbeat = None  # Backward-compatible pre-heartbeat DB.
    except (sqlite3.Error, OSError) as exc:
        raise PaperReportError("Unable to read consistent paper ledger") from exc
    if row is None:
        raise PaperReportError("Missing paper account")
    try:
        cash = Decimal(row["cash"])
        if not cash.is_finite() or cash < 0:
            raise ValueError("cash")
        position = json.loads(row["position"]) if row["position"] else None
        fees = Decimal(0)
        net_closed = Decimal(0)
        opened = None
        closed = wins = losses = 0
        for fill in fills:
            premium = Decimal(fill["premium_inr"])
            fee = Decimal(fill["fee_inr"])
            if not all(x.is_finite() and x >= 0 for x in (premium, fee)):
                raise ValueError("invalid fill")
            fees += fee
            if fill["side"] == "buy":
                if opened is not None:
                    raise ValueError("overlapping paper entries")
                opened = (fill["product_id"], premium + fee)
            elif fill["side"] == "sell":
                if opened is None or opened[0] != fill["product_id"]:
                    raise ValueError("orphan paper exit")
                pnl = premium - fee - opened[1]
                net_closed += pnl
                closed += 1
                wins += int(pnl > 0)
                losses += int(pnl < 0)
                opened = None
            else:
                raise ValueError("unknown fill side")
        if bool(opened) != bool(position):
            raise ValueError("fill/position inconsistency")
    except (InvalidOperation, ValueError, KeyError, TypeError) as exc:
        raise PaperReportError("Paper ledger accounting inconsistent") from exc
    return {
        "mode": "PAPER",
        "status": ("RUNNING" if heartbeat and heartbeat["last_event"] != "NOT_STARTED"
                   and not heartbeat["error_type"] else
                   "ERROR" if heartbeat and heartbeat["error_type"] else "NO_HEARTBEAT"),
        "cash_inr": str(cash),
        "position": position,
        "fills": len(fills),
        "closed_trades": closed,
        "wins": wins, "losses": losses,
        "realized_pnl_inr": str(net_closed),
        "fees_inr": str(fees),
        "last_fills": fills[-10:][::-1],
        "observed_at_utc": heartbeat["observed_at"] if heartbeat else None,
        "last_event": heartbeat["last_event"] if heartbeat else None,
        "last_error_type": heartbeat["error_type"] if heartbeat else None,
        "last_account_update_utc": row["updated_at"],
        "real_orders_sent": 0,
        "mark_to_market_included": False,
    }
