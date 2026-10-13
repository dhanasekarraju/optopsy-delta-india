"""Read existing trading journals WITHOUT creating, initializing or resetting one.

A journal found on disk is only observational evidence. It is not proof that
the broker account was flat when its baseline was first established.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _read_existing(path: str | Path, query: str) -> list[sqlite3.Row] | None:
    candidate = Path(path)
    # Reject missing paths, symlinks and nonregular files before SQLite opens.
    # mode=ro is critical: unlike LifecycleJournal.__init__, it cannot CREATE.
    if candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        target = candidate.absolute().as_uri() + "?mode=ro"
        with sqlite3.connect(target, uri=True, timeout=3) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            return con.execute(query).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return None


def existing_lifecycle_status(path: str | Path | None) -> dict | None:
    """Read a ledger baseline only if the SQLite file and schema exist."""
    if path is None:
        return None
    rows = _read_existing(
        path, "SELECT product_id,baseline_us,quarantined,snapshot "
        "FROM lifecycle WHERE id=1",
    )
    if rows is None or len(rows) > 1:
        return {"state": "UNAVAILABLE"}
    if not rows:
        return {"state": "NO_BASELINE"}
    row = rows[0]
    try:
        snapshot = json.loads(row["snapshot"])
        if (type(row["product_id"]) is not int or row["product_id"] <= 0
                or type(row["baseline_us"]) is not int or row["baseline_us"] <= 0
                or row["quarantined"] not in (0, 1)
                or not isinstance(snapshot, dict)
                or not isinstance(snapshot.get("fills"), dict)):
            return {"state": "UNAVAILABLE"}
        state = "QUARANTINED" if row["quarantined"] else "OBSERVING"
        return {
            "state": state, "product_id": row["product_id"],
            "baseline_us": row["baseline_us"],
            "observed_fill_count": len(snapshot["fills"]),
        }
    except (KeyError, TypeError, ValueError):
        return {"state": "UNAVAILABLE"}


def existing_exit_status(path: str | Path | None) -> dict | None:
    """Read all registered exit states; too many records are untrusted."""
    if path is None:
        return None
    rows = _read_existing(
        path, "SELECT seq,product_id,state FROM attempts ORDER BY seq LIMIT 1001",
    )
    if rows is None or len(rows) > 1000:
        return {"attempts": [{"state": "INVALID", "product_id": None}]}
    attempts = []
    for row in rows:
        if (type(row["seq"]) is not int or row["seq"] <= 0
                or type(row["product_id"]) is not int or row["product_id"] <= 0
                or row["state"] not in (
                    "PREPARED_UNKNOWN", "TERMINAL_VERIFIED", "QUARANTINED"
                )):
            return {"attempts": [{"state": "INVALID", "product_id": None}]}
        attempts.append({"product_id": row["product_id"], "state": row["state"]})
    return {"attempts": attempts}
