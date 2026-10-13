"""Opt-in LIVE long-call worker driven by the ORIGINAL optopsy==2.3.0 RSI signals.

Strategy is identical to ``paper_worker``: entry on ``rsi_below(14, 30)``, exit on
``rsi_above(14, 70)``, closest-0.30-delta call per expiry (upstream screen), soonest eligible
expiry as the operational tie-break, plus the same 24h pre-expiry exit. Nothing is tuned.

Safety model (kept from the repository's gateway design):
* Runs only when BOTH operator interlocks are set (``paper_enabled = false`` in config.toml
  and ``OPTOPSY_LIVE_ENABLED=YES_REAL_ORDERS``). This code never sets either.
* The BROKER is the source of truth for positions; local state only records our attempts.
* One position at a time, IOC limit orders only. Allocation, reserve and max contracts come from
  .env (defaults INR 5,000 / 500 / 1) and are enforced again by ``live_gateway.validate``. Entries need a flat, order-free account.
* Every order is journaled before submission; an order with unknown outcome is never
  resubmitted, and no new order is sent while any attempt is unreconciled.
* ``data/STOP_ENTRIES`` (an empty file) pauses new entries while exits keep running.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

from .account_preflight import account_preflight
from .capital import CapitalConfigError, capital_inr, max_contracts, reserve_inr, validate_all
from .client import DeltaClient
from .contract_selection import screen_long_calls
from .live_gateway import (
    BUFFER,
    DeltaTradingTransport,
    LiveIntentJournal,
    OrderIntent,
    _client_id,
    execute_once,
    reconcile_once,
    validate,
)
from .mode import ModeError, require_live
from .original_signals import closed_candles, evaluate_original_rsi
from .snapshot import _iso_datetime

FX_INR = Decimal(85)  # Delta India documented fixed USD/INR convention (may change).
EXPIRY_GUARD = timedelta(hours=24)
MAX_EXIT_ATTEMPTS_PER_DAY = 20
ENTRY_RULE = "optopsy.rsi_below(period=14, threshold=30)"
EXIT_RULE = "optopsy.rsi_above(period=14, threshold=70)"


class LiveWorkerError(RuntimeError):
    """Fail closed: stop trading actions and require the operator to look."""


def _dec(value: object, label: str, *, positive: bool = True) -> Decimal:
    if value is None or isinstance(value, bool):
        raise LiveWorkerError(f"Missing {label}")
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LiveWorkerError(f"Invalid {label}") from exc
    if not n.is_finite() or n < 0 or (positive and n == 0):
        raise LiveWorkerError(f"Invalid {label}")
    return n


def _fmt(value: Decimal) -> str:
    return format(value, "f")


class AttemptBook:
    """Durable record of every order attempt we made (the broker stays authoritative)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.is_symlink():
            raise LiveWorkerError("Symlinked attempt book refused")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._db()) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS attempts (
                signal_id TEXT PRIMARY KEY, side TEXT NOT NULL, product_id INTEGER NOT NULL,
                underlying TEXT NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0, filled INTEGER)""")
        os.chmod(self.path, 0o600)

    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def add(self, *, signal_id: str, side: str, product_id: int, underlying: str, day: str,
            now: datetime) -> bool:
        with closing(self._db()) as con:
            try:
                con.execute(
                    "INSERT INTO attempts (signal_id,side,product_id,underlying,day,created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (signal_id, side, product_id, underlying, day, now.astimezone(UTC).isoformat()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def drop(self, signal_id: str) -> None:
        with closing(self._db()) as con:
            con.execute("DELETE FROM attempts WHERE signal_id=? AND resolved=0", (signal_id,))

    def resolve(self, signal_id: str, filled: int) -> None:
        with closing(self._db()) as con:
            con.execute("UPDATE attempts SET resolved=1, filled=? WHERE signal_id=?",
                        (filled, signal_id))

    def unresolved(self) -> list[sqlite3.Row]:
        with closing(self._db()) as con:
            return con.execute(
                "SELECT * FROM attempts WHERE resolved=0 ORDER BY created_at").fetchall()

    def entry_products(self) -> dict[int, str]:
        with closing(self._db()) as con:
            rows = con.execute(
                "SELECT DISTINCT product_id, underlying FROM attempts"
                " WHERE side='buy' AND filled > 0").fetchall()
        return {r["product_id"]: r["underlying"] for r in rows}

    def exit_attempts(self, day: str, product_id: int) -> int:
        with closing(self._db()) as con:
            return con.execute(
                "SELECT COUNT(*) FROM attempts WHERE side='sell' AND day=? AND product_id=?",
                (day, product_id)).fetchone()[0]

    def exited_on(self, day: str) -> bool:
        """True once an exit with real fills happened on this UTC day (paper-worker parity)."""
        with closing(self._db()) as con:
            return con.execute(
                "SELECT 1 FROM attempts WHERE side='sell' AND day=? AND filled > 0",
                (day,)).fetchone() is not None


def entry_size(*, ask: Decimal, contract_value: Decimal, ask_size: Decimal, cash_inr: Decimal) -> int:
    """Contracts to buy: min(configured max, visible ask depth, what the budget affords). 0 = skip."""
    per_contract = ask * contract_value * FX_INR * (1 + BUFFER)
    budget = min(capital_inr(), cash_inr) - reserve_inr()
    if budget <= 0 or per_contract <= 0:
        return 0
    affordable = int((budget / per_contract).to_integral_value(rounding=ROUND_FLOOR))
    return max(0, min(max_contracts(), int(ask_size), affordable))


def _verified_signal(signal: object, now: datetime) -> dict:
    """Same acceptance test as the paper worker: unmodified Optopsy RSI, fully closed day."""
    if not isinstance(signal, dict) or signal.get("strategy") != "long_calls":
        raise LiveWorkerError("No verified Optopsy signal")
    expected = (now.astimezone(UTC).date() - timedelta(days=1)).isoformat()
    if (signal.get("parameters_tuned") is not False or signal.get("entry_rule") != ENTRY_RULE
            or signal.get("exit_rule") != EXIT_RULE
            or signal.get("last_complete_utc_day") != expected):
        raise LiveWorkerError("Stale or modified signal")
    if signal.get("underlying") not in ("BTC", "ETH"):
        raise LiveWorkerError("Invalid signal underlying")
    return signal


def _quote(ticker: dict, product: dict) -> dict:
    book = ticker.get("quotes")
    stamp = ticker.get("timestamp")
    if not isinstance(book, dict) or isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        raise LiveWorkerError("Ticker missing order book or exchange timestamp")
    return {
        "bid": _dec(book.get("best_bid"), "best bid"),
        "ask": _dec(book.get("best_ask"), "best ask"),
        "bid_size": _dec(book.get("best_bid_size"), "best bid size"),
        "ask_size": _dec(book.get("best_ask_size"), "best ask size"),
        "tick": _dec(product.get("tick_size"), "tick size"),
        "ts": int(_iso_datetime(stamp).timestamp()),
    }


def resolve_pending(*, book: AttemptBook, journal: LiveIntentJournal,
                    transport: DeltaTradingTransport) -> dict | None:
    """Reconcile every unresolved attempt by client order ID. Never resubmits anything."""
    for row in book.unresolved():
        try:
            info = reconcile_once(transport=transport, journal=journal,
                                  signal_id=row["signal_id"], side=row["side"])
        except Exception as exc:  # noqa: BLE001 - unknown outcome must block new orders
            return {"event": "RECOVERY_REQUIRED_RECONCILE_FAILED",
                    "signal_id": row["signal_id"], "error_type": type(exc).__name__}
        if info["order_state"] in ("open", "pending"):
            return {"event": "PENDING_ORDER_WAIT", "signal_id": row["signal_id"]}
        book.resolve(row["signal_id"], info["reported_filled_size"])
    return None


def _settle_failed_submit(*, book: AttemptBook, journal: LiveIntentJournal, intent: OrderIntent,
                          error: Exception) -> dict:
    """Decide what a failed submission means without ever assuming 'it did not happen'."""
    client_id = _client_id(intent.signal_id, intent.side)
    stage = next((r["stage"] for r in journal.status() if r["client_id"] == client_id), None)
    if stage is None:  # Gateway refused before reserving: nothing reached the exchange.
        book.drop(intent.signal_id)
    elif stage == "REJECTED":  # Explicit exchange rejection; no execution implied.
        book.resolve(intent.signal_id, 0)
    # Otherwise (RESERVED_UNKNOWN / UNKNOWN) stays unresolved and is reconciled, never retried.
    return {"event": "ORDER_NOT_CONFIRMED", "journal_stage": stage or "NOT_RESERVED",
            "error_type": type(error).__name__}


def _place(*, intent: OrderIntent, underlying: str, day: str, now: datetime, book: AttemptBook,
           journal: LiveIntentJournal, transport: DeltaTradingTransport, checks: dict) -> dict:
    stamp = int(now.timestamp())
    validate(intent, now=stamp, **checks)  # Raises before anything is recorded or sent.
    if not book.add(signal_id=intent.signal_id, side=intent.side, product_id=intent.product_id,
                    underlying=underlying, day=day, now=now):
        return {"event": "ALREADY_ATTEMPTED", "signal_id": intent.signal_id}
    try:
        sent = execute_once(intent, transport=transport, journal=journal, now=stamp, **checks)
    except Exception as exc:  # noqa: BLE001
        return _settle_failed_submit(book=book, journal=journal, intent=intent, error=exc)
    result = {"event": "ORDER_SUBMITTED", "side": intent.side, "size": intent.size,
              "limit_price": intent.limit_price, "client_id": sent["client_id"]}
    follow_up = resolve_pending(book=book, journal=journal, transport=transport)
    if follow_up:
        result["reconcile"] = follow_up
    return result


def _held_position(client: DeltaClient, book: AttemptBook, live_ids: set[int]) -> dict | None:
    known = book.entry_products()
    margined = client.open_positions()
    if not isinstance(margined, list):
        raise LiveWorkerError("Unverifiable positions response")
    pids = {pid for pid in known if pid in live_ids}
    for row in margined:
        if not isinstance(row, dict):
            raise LiveWorkerError("Malformed position row")
        if _dec(row.get("size", 0), "position size", positive=False) != 0:
            pids.add(row.get("product_id"))
    open_positions = []
    for pid in sorted(pids, key=str):
        if type(pid) is not int:
            raise LiveWorkerError("Invalid position product ID")
        size = Decimal(str(client.product_position(pid).get("size", "")))
        if size != 0:
            open_positions.append((pid, size))
    if not open_positions:
        return None
    if len(open_positions) > 1:
        raise LiveWorkerError("MULTIPLE_OPEN_POSITIONS_MANUAL_ACTION")
    pid, size = open_positions[0]
    if pid not in known:
        raise LiveWorkerError("UNKNOWN_POSITION_NOT_OPENED_BY_THIS_BOT_MANUAL_ACTION")
    if size < 0 or size != size.to_integral_value():
        raise LiveWorkerError("UNEXPECTED_SHORT_OR_FRACTIONAL_POSITION_MANUAL_ACTION")
    return {"product_id": pid, "underlying": known[pid], "size": int(size)}


def _manage_exit(*, held: dict, by_id: dict, client: DeltaClient, now: datetime,
                 evaluate, load_candles, book: AttemptBook, journal: LiveIntentJournal,
                 transport: DeltaTradingTransport) -> dict:
    pid, underlying = held["product_id"], held["underlying"]
    product = by_id.get(pid)
    if product is None:
        raise LiveWorkerError("HELD_PRODUCT_NOT_LIVE_MANUAL_ACTION")
    expiry = _iso_datetime(product.get("settlement_time") or product.get("expiry_time"))
    signal_exit, signal_error = False, None
    try:
        signal = _verified_signal(evaluate(underlying, load_candles(underlying, now=now)), now)
        signal_exit = signal.get("exit") is True
    except Exception as exc:  # noqa: BLE001 - the time-based exit must not depend on signals
        signal_error = type(exc).__name__
    time_exit = now >= expiry - EXPIRY_GUARD
    base = {"position": held, "signal_error": signal_error}
    if not (signal_exit or time_exit):
        return {"event": "HOLD", **base}
    ticker = next((t for t in client.option_tickers(underlying)
                   if isinstance(t, dict) and t.get("product_id") == pid), None)
    try:
        if ticker is None:
            raise LiveWorkerError("No ticker for held product")
        quote = _quote(ticker, product)
        size = min(held["size"], int(quote["bid_size"]))
        if size < 1:
            raise LiveWorkerError("No bid depth")
    except LiveWorkerError:
        return {"event": "EXIT_DUE_QUOTE_UNAVAILABLE", **base}
    day = now.astimezone(UTC).date().isoformat()
    attempt = book.exit_attempts(day, pid) + 1
    if attempt > MAX_EXIT_ATTEMPTS_PER_DAY:
        raise LiveWorkerError("EXIT_ATTEMPT_LIMIT_REACHED_MANUAL_ACTION")
    intent = OrderIntent(
        signal_id=f"exit-{day}-{pid}-{attempt}", product_id=pid, side="sell", size=size,
        limit_price=_fmt(quote["bid"]), contract_value=str(product["contract_value"]),
        usd_inr=str(FX_INR), ticker_timestamp=quote["ts"], reduce_only=True,
    )
    checks = {"cash_inr": 0, "tick_size": _fmt(quote["tick"]), "observed_bid": _fmt(quote["bid"]),
              "observed_ask": _fmt(quote["ask"]), "observed_depth": _fmt(quote["bid_size"]),
              "external_exposure": False}
    reason = "SIGNAL_EXIT" if signal_exit else "PRE_EXPIRY_EXIT"
    return {**base, "reason": reason,
            **_place(intent=intent, underlying=underlying, day=day, now=now, book=book,
                     journal=journal, transport=transport, checks=checks)}


def _consider_entry(*, products: list, client: DeltaClient, now: datetime, evaluate, load_candles,
                    book: AttemptBook, journal: LiveIntentJournal,
                    transport: DeltaTradingTransport, data_dir: Path) -> dict:
    if (data_dir / "STOP_ENTRIES").exists():
        return {"event": "ENTRIES_PAUSED_BY_OPERATOR_FILE"}
    by_id = {p.get("id"): p for p in products if isinstance(p, dict)}
    waits = []
    for underlying in ("BTC", "ETH"):
        signal = _verified_signal(evaluate(underlying, load_candles(underlying, now=now)), now)
        day = signal["last_complete_utc_day"]
        if signal.get("entry") is not True or signal.get("exit") is not False:
            waits.append({"underlying": underlying, "event": "WAIT_NO_ENTRY_SIGNAL"})
            continue
        if book.exited_on(day):
            waits.append({"underlying": underlying, "event": "WAIT_EXITED_THIS_SIGNAL_DAY"})
            continue
        tickers = client.option_tickers(underlying)
        snap = {"underlying": underlying, "captured_at": now.isoformat(),
                "raw_products": products, "raw_tickers": tickers}
        eligible = [c for c in screen_long_calls(snap) if c.get("underlying") == underlying]
        if not eligible:
            waits.append({"underlying": underlying, "event": "NO_ELIGIBLE_CALL"})
            continue
        # Upstream screens one call per expiry; soonest expiry is the declared tie-break.
        contract = min(eligible, key=lambda c: (c["expiry_utc"], c["product_id"]))
        pid = contract["product_id"]
        ticker = next((t for t in tickers if isinstance(t, dict) and t.get("product_id") == pid),
                      None)
        if ticker is None:
            raise LiveWorkerError("No matching live ticker")
        quote = _quote(ticker, by_id[pid])
        if quote["ask"] != _dec(contract["ask_usd"], "screened ask"):
            raise LiveWorkerError("Screen quote mismatch")
        account = account_preflight(client)
        if account["status"] != "PRELIMINARY_CLEAR":
            return {"event": "ENTRY_BLOCKED_ACCOUNT_NOT_CLEAR", "blockers": account["blockers"]}
        cash_inr = _dec(account["available_usd_equivalent"], "wallet", positive=False) * FX_INR
        size = entry_size(ask=quote["ask"], contract_value=_dec(contract["contract_value"], "unit"),
                          ask_size=quote["ask_size"], cash_inr=cash_inr)
        if size < 1:
            return {"event": "ENTRY_SKIPPED_BUDGET_TOO_SMALL", "underlying": underlying,
                    "symbol": contract["symbol"], "cash_inr": _fmt(cash_inr)}
        intent = OrderIntent(
            signal_id=f"rsi-{underlying}-{day}-{pid}", product_id=pid, side="buy", size=size,
            limit_price=_fmt(quote["ask"]), contract_value=str(contract["contract_value"]),
            usd_inr=str(FX_INR), ticker_timestamp=quote["ts"],
        )
        checks = {"cash_inr": _fmt(cash_inr), "tick_size": _fmt(quote["tick"]),
                  "observed_bid": _fmt(quote["bid"]), "observed_ask": _fmt(quote["ask"]),
                  "observed_depth": _fmt(quote["ask_size"]), "external_exposure": False}
        return {"underlying": underlying, "symbol": contract["symbol"],
                **_place(intent=intent, underlying=underlying, day=day, now=now, book=book,
                         journal=journal, transport=transport, checks=checks)}
    return {"event": "WAIT_NO_ENTRY_SIGNAL", "checked": waits}


def run_once(*, client: DeltaClient, transport: DeltaTradingTransport, journal: LiveIntentJournal,
             book: AttemptBook, data_dir: str | Path = "data", config_path: str = "config.toml",
             evaluate=evaluate_original_rsi, load_candles=closed_candles,
             now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise LiveWorkerError("UTC-aware runtime required")
    require_live(config_path)
    pending = resolve_pending(book=book, journal=journal, transport=transport)
    if pending:
        return {"mode": "LIVE", "at": now.isoformat(), **pending}
    products = client.live_products()
    by_id = {p.get("id"): p for p in products if isinstance(p, dict)}
    held = _held_position(client, book, {pid for pid in by_id if type(pid) is int})
    if held:
        result = _manage_exit(held=held, by_id=by_id, client=client, now=now, evaluate=evaluate,
                              load_candles=load_candles, book=book, journal=journal,
                              transport=transport)
    else:
        result = _consider_entry(products=products, client=client, now=now, evaluate=evaluate,
                                 load_candles=load_candles, book=book, journal=journal,
                                 transport=transport, data_dir=Path(data_dir))
    return {"mode": "LIVE", "at": now.isoformat(), **result}


def _heartbeat(data_dir: Path, payload: dict) -> None:
    target = data_dir / "live-heartbeat.json"
    temp = data_dir / "live-heartbeat.json.tmp"
    temp.write_text(json.dumps(payload, sort_keys=True))
    os.chmod(temp, 0o600)
    os.replace(temp, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LIVE Optopsy RSI long-call worker (real orders)")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    if not 30 <= args.interval <= 3600:
        parser.error("Interval must be from 30 to 3600 seconds")
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key, secret = os.getenv("DELTA_TRADE_API_KEY", ""), os.getenv("DELTA_TRADE_API_SECRET", "")
    while True:  # Standby (no restart loop) until BOTH interlocks and the keys are present.
        standby = None
        try:
            require_live(args.config)
        except ModeError as exc:
            standby = {"event": "LIVE_STANDBY_NOT_ENABLED", "detail": str(exc)}
        else:
            try:
                limits = validate_all()
            except CapitalConfigError as exc:
                standby = {"event": "CAPITAL_CONFIG_INVALID", "detail": str(exc)}
            else:
                if not key or not secret:
                    standby = {"event": "TRADING_CREDENTIALS_MISSING"}
        if standby is None:
            print(json.dumps({"mode": "LIVE", "event": "LIVE_START", **limits}), flush=True)
            break
        standby.update(mode="LIVE", at=datetime.now(UTC).isoformat(), real_orders_sent=0)
        _heartbeat(data_dir, standby)
        print(json.dumps(standby, sort_keys=True), flush=True)
        if args.once:
            return 3
        time.sleep(args.interval)
    journal = LiveIntentJournal(data_dir / "live-orders.sqlite")
    book = AttemptBook(data_dir / "live-attempts.sqlite")
    transport = DeltaTradingTransport(api_key=key, api_secret=secret, live_enabled=True)
    client = DeltaClient(api_key=key, api_secret=secret)
    while True:
        try:
            result = run_once(client=client, transport=transport, journal=journal, book=book,
                              data_dir=data_dir, config_path=args.config)
        except ModeError as exc:
            # Interlock removed while running: stop acting. An open position is NOT auto-closed.
            print(json.dumps({"mode": "LIVE", "event": "LIVE_DISABLED_STOPPED", "detail": str(exc),
                              "warning": "Check Delta for any open position"}), flush=True)
            return 3
        except Exception as exc:  # noqa: BLE001 - never act on failure; try again next cycle
            result = {"mode": "LIVE", "at": datetime.now(UTC).isoformat(),
                      "event": "STOP_ERROR_NO_ACTION", "error_type": type(exc).__name__,
                      "detail": str(exc)[:200]}
            if args.once:
                print(json.dumps(result, sort_keys=True), flush=True)
                return 2
        _heartbeat(data_dir, result)
        print(json.dumps(result, sort_keys=True, default=str), flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
