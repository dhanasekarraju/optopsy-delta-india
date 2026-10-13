"""Continuous long-call paper execution using ORIGINAL optopsy==2.3.0 RSI.

PUBLIC Delta GET only. Durable simulated fills at observed bid/ask; they are
NOT actual or guaranteed executable fills. This process has no private keys,
trading transport, order POST method, or dependency on other VPS containers.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .client import DeltaClient
from .contract_selection import screen_long_calls
from .original_signals import closed_candles, evaluate_original_rsi
from .mode import require_paper

START_INR = Decimal(5000)
RESERVE_INR = Decimal(500)
FX_INR = Decimal(85)  # Paper-only Delta platform conversion assumption.
FEE_RATE = Decimal("0.05")  # Deliberately conservative simulated premium fee.


class PaperBotError(RuntimeError):
    pass


def _num(value, *, allow_zero=False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise PaperBotError("Missing numeric quote")
    try:
        n = Decimal(str(value))
    except (TypeError, InvalidOperation, ValueError) as exc:
        raise PaperBotError("Invalid numeric quote") from exc
    if not n.is_finite() or n < 0 or (n == 0 and not allow_zero):
        raise PaperBotError("Nonpositive/nonfinite quote")
    return n


class PaperLedger:
    """Atomic full-balance/position updates with restart-safe signal-day IDs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise PaperBotError("Symlinked paper ledger refused")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._db()) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK(id=1), cash TEXT NOT NULL,
                position TEXT, last_action_id TEXT, updated_at TEXT NOT NULL
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS fills (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL UNIQUE,
                side TEXT NOT NULL, underlying TEXT NOT NULL, product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL, price_usd TEXT NOT NULL,
                premium_inr TEXT NOT NULL, fee_inr TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            con.execute("""CREATE TRIGGER IF NOT EXISTS paper_fills_no_update
                BEFORE UPDATE ON fills BEGIN SELECT RAISE(ABORT,'Immutable fills'); END""")
            con.execute("""CREATE TRIGGER IF NOT EXISTS paper_fills_no_delete
                BEFORE DELETE ON fills BEGIN SELECT RAISE(ABORT,'Immutable fills'); END""")
            con.execute("""CREATE TABLE IF NOT EXISTS paper_heartbeat (
                id INTEGER PRIMARY KEY CHECK(id=1), observed_at TEXT NOT NULL,
                last_event TEXT NOT NULL, error_type TEXT
            )""")
            con.execute(
                "INSERT OR IGNORE INTO account VALUES(1,?,?,?,?)",
                (str(START_INR), None, None, datetime.now(UTC).isoformat()),
            )
        os.chmod(self.path, 0o600)

    def _db(self):
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def status(self) -> dict:
        with closing(self._db()) as con:
            row = con.execute("SELECT * FROM account WHERE id=1").fetchone()
            if row is None:
                raise PaperBotError("Paper account missing")
            fills = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        return {"mode": "PAPER_ONLY", "cash_inr": row["cash"],
                "position": json.loads(row["position"]) if row["position"] else None,
                "last_action_id": row["last_action_id"], "fill_count": fills,
                "real_orders_sent": 0}

    def heartbeat(self, event: str, now: datetime, error_type: str | None = None) -> None:
        with closing(self._db()) as con:
            con.execute("INSERT INTO paper_heartbeat VALUES(1,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at, "
                        "last_event=excluded.last_event,error_type=excluded.error_type",
                        (now.astimezone(UTC).isoformat(), event, error_type))

    def apply(self, *, action_id: str, side: str, contract: dict, price: Decimal,
              quantity: int, day: str, now: datetime) -> dict:
        """Transactional book entry; never assumes a broker fill occurred."""
        if (not action_id or side not in ("buy", "sell") or type(quantity) is not int
                or quantity != 1 or now.tzinfo is None):
            raise PaperBotError("Invalid paper action")
        if (type(contract.get("product_id")) is not int or contract["product_id"] <= 0
                or contract.get("underlying") not in ("BTC", "ETH")):
            raise PaperBotError("Invalid product identity")
        price = _num(price)
        unit = _num(contract.get("contract_value"))
        premium = price * unit * quantity * FX_INR
        fee = premium * FEE_RATE
        stamp = now.astimezone(UTC).isoformat()
        with closing(self._db()) as con:
            con.execute("BEGIN IMMEDIATE")
            prior = con.execute("SELECT * FROM fills WHERE action_id=?", (action_id,)).fetchone()
            if prior:
                con.execute("COMMIT")
                return {"event": "ALREADY_RECORDED", "action_id": action_id}
            row = con.execute("SELECT * FROM account WHERE id=1").fetchone()
            cash = _num(row["cash"], allow_zero=True)
            pos = json.loads(row["position"]) if row["position"] else None
            if side == "buy":
                if pos is not None:
                    raise PaperBotError("Another simulated position is open")
                if (premium + fee > START_INR - RESERVE_INR
                        or premium + fee > cash - RESERVE_INR):
                    raise PaperBotError("Insufficient simulated cash/reserve")
                # Never re-enter on same daily signal after an earlier close.
                if row["last_action_id"] and row["last_action_id"].startswith(
                    f"close:{day}:"
                ):
                    raise PaperBotError("Already exited this UTC signal day")
                cash -= premium + fee
                position = {
                    "underlying": contract["underlying"], "product_id": contract["product_id"],
                    "symbol": contract.get("symbol", ""), "contract_value": str(unit),
                    "expiry_utc": contract["expiry_utc"], "quantity": quantity,
                    "opened_on": day, "entry_price_usd": str(price),
                    "entry_cost_inr": str(premium + fee),
                }
            else:
                if (pos is None or pos["product_id"] != contract["product_id"]
                        or pos["quantity"] != quantity
                        or pos["underlying"] != contract["underlying"]
                        or _num(pos["contract_value"]) != unit):
                    raise PaperBotError("Position identity/inventory mismatch")
                cash += premium - fee
                position = None
            con.execute("UPDATE account SET cash=?, position=?, last_action_id=?, updated_at=?"
                        " WHERE id=1", (str(cash), json.dumps(position) if position else None,
                                        action_id, stamp))
            con.execute("""INSERT INTO fills (
                action_id,side,underlying,product_id,quantity,price_usd,
                premium_inr,fee_inr,created_at) VALUES (?,?,?,?,?,?,?,?,?)""", (
                action_id, side, contract["underlying"], contract["product_id"],
                quantity, str(price), str(premium), str(fee), stamp,
            ))
            con.execute("COMMIT")
        return {"event": "SIMULATED_" + ("BUY" if side == "buy" else "SELL"),
                "action_id": action_id, "cash_inr": str(cash),
                "premium_inr": str(premium), "fee_inr": str(fee),
                "real_orders_sent": 0}


def decide_paper(*, ledger: PaperLedger, signal: dict, contracts: list[dict],
                 quotes: list[dict], now: datetime) -> dict:
    """Apply one of original RSI entry/exit conditions; no strategy reimplementation."""
    if not isinstance(signal, dict) or signal.get("strategy") != "long_calls":
        raise PaperBotError("No verified Optopsy signal")
    if (signal.get("parameters_tuned") is not False
            or signal.get("entry_rule") != "optopsy.rsi_below(period=14, threshold=30)"
            or signal.get("exit_rule") != "optopsy.rsi_above(period=14, threshold=70)"
            or signal.get("last_complete_utc_day") != (
                now.astimezone(UTC).date() - timedelta(days=1)
            ).isoformat()):
        raise PaperBotError("Stale/modified signal")
    if signal.get("underlying") not in ("BTC", "ETH"):
        raise PaperBotError("Invalid signal underlying")
    state = ledger.status()
    pos = state["position"]
    if pos is not None and pos["underlying"] != signal["underlying"]:
        return {"event": "OTHER_UNDERLYING_POSITION", "real_orders_sent": 0}
    by_product = {q.get("product_id"): q for q in quotes if isinstance(q, dict)}
    if pos is None:
        if signal.get("entry") is not True or signal.get("exit") is not False:
            return {"event": "WAIT_NO_ENTRY_SIGNAL", "real_orders_sent": 0}
        # Original Optopsy screens one closest-0.30-delta call *per expiry*;
        # it does NOT define a unique live expiry. Explicitly paper-only
        # operational tie-break is the soonest eligible expiry.
        available = [c for c in contracts if c.get("underlying") == signal["underlying"]]
        if not available:
            return {"event": "NO_ELIGIBLE_CALL", "real_orders_sent": 0}
        contract = min(available, key=lambda c: (c["expiry_utc"], c["product_id"]))
        quote = by_product.get(contract["product_id"])
        if not isinstance(quote, dict):
            raise PaperBotError("No matching live ticker")
        book = quote.get("quotes")
        if not isinstance(book, dict):
            raise PaperBotError("Missing order book")
        ask = _num(book.get("best_ask"))
        _num(book.get("best_ask_size"))
        if ask != _num(contract["ask_usd"]):
            raise PaperBotError("Screen quote mismatch")
        return ledger.apply(
            action_id=f"open:{signal['last_complete_utc_day']}:{contract['product_id']}",
            side="buy", contract=contract, price=ask, quantity=1,
            day=signal["last_complete_utc_day"], now=now,
        )

    expiry = datetime.fromisoformat(pos["expiry_utc"]).astimezone(UTC)
    exit_due = signal.get("exit") is True or now >= expiry - timedelta(hours=24)
    if not exit_due:
        return {"event": "HOLD", "real_orders_sent": 0}
    quote = by_product.get(pos["product_id"])
    if not isinstance(quote, dict):
        return {"event": "EXIT_DUE_QUOTE_UNAVAILABLE", "real_orders_sent": 0}
    book = quote.get("quotes")
    if not isinstance(book, dict):
        raise PaperBotError("Invalid exit book")
    bid = _num(book.get("best_bid"))
    _num(book.get("best_bid_size"))
    contract = {
        "underlying": pos["underlying"], "product_id": pos["product_id"],
        "contract_value": pos["contract_value"], "expiry_utc": pos["expiry_utc"],
    }
    # A repeated close is idempotent; the ledger holds the remaining cash.
    return ledger.apply(
        action_id=f"close:{signal['last_complete_utc_day']}:{pos['product_id']}",
        side="sell", contract=contract, price=bid, quantity=pos["quantity"],
        day=signal["last_complete_utc_day"], now=now,
    )


def run_once(*, client: DeltaClient, ledger: PaperLedger,
             evaluate=evaluate_original_rsi, load_candles=closed_candles,
             now: datetime | None = None) -> dict:
    """Use live public Delta candles/options, but ONLY paper-account writes."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise PaperBotError("UTC-aware runtime required")
    products = client.live_products()
    output = []
    # Observe the open position first to ensure its RSI exit is evaluated even
    # when the original BTC-first list would otherwise try a fresh entry.
    pos = ledger.status()["position"]
    underlyings = [pos["underlying"]] if pos else ["BTC", "ETH"]
    for underlying in underlyings:
        candles = load_candles(underlying, now=now)
        signal = evaluate(underlying, candles)
        tickers = client.option_tickers(underlying)
        snap = {"underlying": underlying, "captured_at": now.isoformat(),
                "raw_products": products, "raw_tickers": tickers}
        eligible = screen_long_calls(snap)
        result = decide_paper(
            ledger=ledger, signal=signal, contracts=eligible,
            quotes=tickers, now=now,
        )
        output.append({"underlying": underlying, **result})
        if ledger.status()["position"] is not None:
            break
    return {"mode": "PAPER_ONLY", "at": now.isoformat(), "actions": output,
            "portfolio": ledger.status(), "real_orders_sent": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UNMODIFIED Optopsy RSI PAPER bot only")
    parser.add_argument("--db", default="data/paper-optopsy.sqlite")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    if not 30 <= args.interval <= 3600:
        parser.error("Interval must be from 30 to 3600 seconds")
    require_paper(args.config)
    ledger = PaperLedger(args.db)
    client = DeltaClient(api_key="", api_secret="")  # PUBLIC data only
    while True:
        try:
            require_paper(args.config)
            result = run_once(client=client, ledger=ledger)
            ledger.heartbeat(result["actions"][-1]["event"] if result["actions"]
                             else "OBSERVED", datetime.now(UTC))
            print(json.dumps(result, sort_keys=True), flush=True)
        except Exception as exc:  # noqa: BLE001 - never fake a trade on failure
            ledger.heartbeat("STOP_DATA_OR_SIGNAL_ERROR", datetime.now(UTC),
                             type(exc).__name__)
            print(json.dumps({"mode": "PAPER_ONLY", "event": "STOP_DATA_OR_SIGNAL_ERROR",
                              "error_type": type(exc).__name__,
                              "real_orders_sent": 0}), flush=True)
            if args.once:
                return 2
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
