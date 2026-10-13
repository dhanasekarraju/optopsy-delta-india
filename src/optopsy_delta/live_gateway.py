"""Delta India live-order transport with persistent at-most-once submission.

This is an intentionally UNWIRED building block: importing it never trades.
No strategy is inferred here, and no live entry loop is enabled. All operations
are exclusively Delta India /v2. Real orders require an explicit user opt-in.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

HOST = "https://api.india.delta.exchange"
ORDER_PATH = "/v2/orders"
MAX_INR = Decimal(5000)
RESERVE_INR = Decimal(500)
BUFFER = Decimal("0.03")
TIMEOUT = (3.05, 8)
_ALLOWED_ID = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")


class LiveGatewayError(RuntimeError):
    """No exception ever includes API keys, signatures or HTTP payloads."""


class LiveRejected(LiveGatewayError):
    """Exchange returned an explicit rejected request; no execution implied."""


class LiveUncertain(LiveGatewayError):
    """An ambiguous acknowledgement; reconcile, never blindly resubmit."""


def _number(v: object, *, positive: bool = True) -> Decimal:
    if isinstance(v, bool):
        raise LiveGatewayError("Boolean amount not permitted")
    try:
        n = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiveGatewayError("Invalid numeric amount") from exc
    if not n.is_finite() or n < 0 or (positive and n == 0):
        raise LiveGatewayError("Invalid nonpositive or nonfinite amount")
    return n


def _client_id(strategy_signal: str, side: str) -> str:
    if not isinstance(strategy_signal, str) or not _ALLOWED_ID.fullmatch(strategy_signal):
        raise LiveGatewayError("Invalid strategy signal reference")
    if side not in ("buy", "sell"):
        raise LiveGatewayError("Unsupported order side")
    # 2 + 1 + 28 = 31 chars, under Delta's 32-character maximum.
    suffix = hashlib.sha256((side + ":" + strategy_signal).encode()).hexdigest()[:28]
    return ("ob" if side == "buy" else "os") + "-" + suffix


@dataclass(frozen=True)
class OrderIntent:
    signal_id: str
    product_id: int
    side: str
    size: int
    limit_price: str
    contract_value: str
    usd_inr: str
    # Seconds since Unix epoch from Delta ticker (not local guessed tick time).
    ticker_timestamp: int
    # Only for exits: caller must reconcile real filled quantities first.
    reduce_only: bool = False


def validate(intent: OrderIntent, *, now: int, cash_inr: object,
             tick_size: object, observed_bid: object, observed_ask: object,
             observed_depth: object, external_exposure: bool = False) -> Decimal:
    """Conservative limits independent of Optopsy strategy parameters.

    Live risk cap cannot be inferred from backtest strategy P&L. Caller must
    separately reconcile account/ledger exposure before calling this function.
    """
    _client_id(intent.signal_id, intent.side)
    if type(intent.product_id) is not int or intent.product_id <= 0:
        raise LiveGatewayError("Invalid Delta product ID")
    if type(intent.size) is not int or intent.size <= 0:
        raise LiveGatewayError("Invalid order size")
    if intent.side == "buy" and intent.reduce_only:
        raise LiveGatewayError("Buy cannot be reduce-only in this long-only module")
    if intent.side == "sell" and not intent.reduce_only:
        raise LiveGatewayError("Sell must be reduce-only")
    if type(intent.ticker_timestamp) is not int or now - intent.ticker_timestamp > 10:
        raise LiveGatewayError("Ticker too old or invalid")
    if intent.ticker_timestamp > now + 2:
        raise LiveGatewayError("Future ticker timestamp")
    p, unit, fx, tick = (
        _number(intent.limit_price), _number(intent.contract_value),
        _number(intent.usd_inr), _number(tick_size),
    )
    bid, ask, depth = (_number(observed_bid, positive=False),
                       _number(observed_ask), _number(observed_depth))
    if bid > ask or p % tick != 0:
        raise LiveGatewayError("Crossed quote or off-tick limit price")
    if intent.size > depth:
        raise LiveGatewayError("Insufficient observed top-of-book depth")
    if intent.side == "buy" and p < ask:
        raise LiveGatewayError("Buy limit is below current ask; no guaranteed execution")
    if intent.side == "sell" and p > bid:
        raise LiveGatewayError("Sell limit is above current bid; no guaranteed execution")
    if intent.side == "buy" and external_exposure:
        raise LiveGatewayError("Unreconciled broker exposure; refuse new entry")
    premium = p * unit * intent.size * fx
    if intent.side == "buy":
        available = _number(cash_inr, positive=False)
        if (premium * (1 + BUFFER) > MAX_INR - RESERVE_INR
                or premium * (1 + BUFFER) > available - RESERVE_INR):
            raise LiveRejected("Insufficient funds or INR 5000 allocation limit")
    return premium


class DeltaTradingTransport:
    """POST only explicit, signed, Delta India limit orders.

    Built to be injected with a fake requests session in deterministic tests.
    """

    def __init__(self, *, api_key: str | None = None, api_secret: str | None = None,
                 session: requests.Session | None = None, live_enabled: bool = False) -> None:
        self._key = api_key if api_key is not None else os.getenv("DELTA_TRADE_API_KEY", "")
        self._secret = (api_secret if api_secret is not None
                        else os.getenv("DELTA_TRADE_API_SECRET", ""))
        self._session = session or requests.Session()
        self.live_enabled = live_enabled

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        # Read-only reconciliation GETs must remain available after the live kill switch.
        if method == "POST" and (
            not self.live_enabled or os.getenv("OPTOPSY_LIVE_ENABLED", "") != "YES_REAL_ORDERS"
        ):
            raise LiveGatewayError("Live order transport disabled by two independent interlocks")
        if not self._key or not self._secret:
            raise LiveGatewayError("Separate Delta India trading credentials missing")
        if method not in ("GET", "POST") or not (
            path == ORDER_PATH or re.fullmatch(r"/v2/orders/client_order_id/[a-z0-9-]{1,32}", path)
        ):
            raise LiveGatewayError("Unapproved Delta request")
        if method == "POST" and path != ORDER_PATH:
            raise LiveGatewayError("Only standard order POST permitted")
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True) if body else ""
        timestamp = str(int(time.time()))
        signature = hmac.new(self._secret.encode(),
                             (method + timestamp + path + raw).encode(), hashlib.sha256).hexdigest()
        headers = {
            "api-key": self._key, "timestamp": timestamp, "signature": signature,
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "optopsy-delta-india/0.1.0",
        }
        try:
            reply = self._session.request(method, HOST + path, headers=headers,
                                          data=raw if method == "POST" else None, timeout=TIMEOUT)
            # Explicit validation rejects are distinct from unknown network outcomes.
            if 400 <= reply.status_code < 500 and reply.status_code not in (408, 409, 429):
                raise LiveRejected("Delta rejected the order; inspect your private exchange UI")
            reply.raise_for_status()
            obj = reply.json()
        except LiveRejected:
            raise
        except (requests.RequestException, ValueError) as exc:
            raise LiveUncertain("Order acknowledgement unknown; reconcile by client ID") from exc
        if not isinstance(obj, dict) or obj.get("success") is not True:
            raise LiveUncertain("Unexpected Delta response; reconcile before retrying")
        if not isinstance(obj.get("result"), dict):
            raise LiveUncertain("Delta order response shape unrecognized")
        return obj["result"]

    def submit(self, intent: OrderIntent) -> dict:
        payload = {
            "product_id": intent.product_id,
            "size": intent.size, "side": intent.side,
            "order_type": "limit_order", "limit_price": intent.limit_price,
            "time_in_force": "ioc", "reduce_only": intent.reduce_only,
            "client_order_id": _client_id(intent.signal_id, intent.side),
            "post_only": False,
        }
        return self._request("POST", ORDER_PATH, payload)

    def lookup(self, *, signal_id: str, side: str) -> dict:
        return self._request("GET", ORDER_PATH + "/client_order_id/" + _client_id(signal_id, side))


class LiveIntentJournal:
    """Durable write-ahead intent gate for at-most-once submission attempts.

    A request with an unknown outcome is deliberately NEVER resubmitted.
    Reconciliation must query the exchange by its stable client order ID.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self._db()
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS orders (
                client_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL,
                stage TEXT NOT NULL,
                broker_order_id TEXT,
                updated_at INTEGER NOT NULL
            )""")
        finally:
            con.close()
        os.chmod(self.path, 0o600)

    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def reserve(self, intent: OrderIntent) -> str:
        client_id = _client_id(intent.signal_id, intent.side)
        body = json.dumps(intent.__dict__, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(body.encode()).hexdigest()
        con = self._db()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT fingerprint FROM orders WHERE client_id=?",
                              (client_id,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise LiveGatewayError("Conflicting duplicate live order intent")
                raise LiveGatewayError("Order already reserved; reconcile, do not resubmit")
            con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)",
                        (client_id, fingerprint, body, "RESERVED_UNKNOWN", None, int(time.time())))
            con.commit()
            return client_id
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def note(self, client_id: str, stage: str, *, order_id: str | None = None) -> None:
        if stage not in ("ACKNOWLEDGED", "REJECTED", "UNKNOWN", "RECONCILED"):
            raise LiveGatewayError("Invalid ledger state")
        con = self._db()
        try:
            con.execute("BEGIN IMMEDIATE")
            result = con.execute("UPDATE orders SET stage=?,broker_order_id=?,updated_at=? WHERE client_id=?",
                                 (stage, order_id, int(time.time()), client_id))
            if result.rowcount != 1:
                raise LiveGatewayError("Unknown client order ID")
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def status(self) -> list[dict]:
        con = self._db()
        try:
            return [dict(r) for r in con.execute(
                "SELECT client_id,stage,broker_order_id,updated_at FROM orders ORDER BY rowid"
            )]
        finally:
            con.close()


def execute_once(
    intent: OrderIntent, *, transport: DeltaTradingTransport,
    journal: LiveIntentJournal, now: int, cash_inr: object,
    tick_size: object, observed_bid: object, observed_ask: object,
    observed_depth: object, external_exposure: bool,
) -> dict:
    """Validate before durable reserve, then do one broker submission only.

    This is a building block, NOT sufficient to operate autonomously.
    Real order fill/position reconciliation and an entry signal must be built first.
    """
    if not transport.live_enabled or os.getenv("OPTOPSY_LIVE_ENABLED") != "YES_REAL_ORDERS":
        raise LiveGatewayError("Live disabled")
    validate(intent, now=now, cash_inr=cash_inr, tick_size=tick_size,
             observed_bid=observed_bid, observed_ask=observed_ask,
             observed_depth=observed_depth, external_exposure=external_exposure)
    client_id = journal.reserve(intent)
    try:
        response = transport.submit(intent)
    except LiveRejected:
        journal.note(client_id, "REJECTED")
        raise
    except Exception:
        journal.note(client_id, "UNKNOWN")
        raise
    # Do not conflate an accepted order with a fill; IOC may partially fill.
    if (response.get("client_order_id") != client_id
            or response.get("product_id") != intent.product_id
            or not isinstance(response.get("id"), int)):
        journal.note(client_id, "UNKNOWN")
        raise LiveUncertain("Mismatched broker order ID or product; do not resubmit")
    journal.note(client_id, "ACKNOWLEDGED", order_id=str(response.get("id", "")))
    return {"client_id": client_id, "stage": "ACKNOWLEDGED",
            "broker_order_id": str(response.get("id", "")),
            "fill_status": "UNVERIFIED_RECONCILIATION_REQUIRED"}


def reconcile_once(*, transport: DeltaTradingTransport, journal: LiveIntentJournal,
                   signal_id: str, side: str) -> dict:
    """Read-only fill check; NEVER retry unknown orders and never guess positions.

    A one-off lookup is not enough to certify fills/account inventory; the
    private fills feed and independent current positions must also reconcile.
    """
    key = _client_id(signal_id, side)
    records = [row for row in journal.status() if row["client_id"] == key]
    if len(records) != 1:
        raise LiveGatewayError("Order not reserved in journal")
    result = transport.lookup(signal_id=signal_id, side=side)
    if result.get("client_order_id") != key:
        raise LiveUncertain("Broker returned mismatched client ID")
    try:
        size, remaining = int(result["size"]), int(result["unfilled_size"])
        if size < 1 or not 0 <= remaining <= size:
            raise ValueError("bad fill count")
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveUncertain("Invalid broker quantity response") from exc
    state = result.get("state")
    if state not in ("open", "pending", "closed", "cancelled"):
        raise LiveUncertain("Unknown broker order state")
    broker_id = result.get("id")
    if not isinstance(broker_id, int):
        raise LiveUncertain("Missing broker order ID")
    journal.note(key, "RECONCILED", order_id=str(broker_id))
    return {
        "client_id": key, "order_state": state, "submitted_size": size,
        "reported_filled_size": size - remaining,
        "remaining_size": remaining,
        "fills_and_position_verified": False,
    }
