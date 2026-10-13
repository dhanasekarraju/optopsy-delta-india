"""Fully offline end-to-end rehearsal of the registered Delta options exit lifecycle.

No credentials, exchange API connections, live order transport, or market calls.
Every broker interaction uses deterministic in-memory simulated records. This
does not replace production or exchange-integration testing.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from .account_preflight import inspect_account
from .broker_exit_recovery import reconcile_exit_broker
from .exit_recovery import ExitAttemptJournal, ExitRecoveryError
from .exit_supervisor import assess_exit
from .live_gateway import LiveIntentJournal, OrderIntent
from .position_lifecycle import LifecycleJournal

PRODUCT = 44
START_US = 100
END_US = 20_000


class FakeBroker:
    """Deterministic stand-in for Delta signed GET endpoints; never network."""

    def __init__(self):
        self.orders: dict[int, dict] = {}
        self.fills: list[dict] = []
        self.position = 0
        self.pending: list[int] = []
        self.unreachable = False
        self.calls = 0

    def _read(self):
        self.calls += 1
        if self.unreachable:
            raise ConnectionError("Simulated broker outage")

    def order_by_id(self, order_id: int) -> dict:
        self._read()
        return dict(self.orders[order_id])

    def order_by_client_id(self, client_id: str) -> dict:
        self._read()
        for row in self.orders.values():
            if row["client_order_id"] == client_id:
                return dict(row)
        raise LookupError("Simulated unknown broker acknowledgement")

    def active_orders(self) -> list[dict]:
        self._read()
        return [dict(self.orders[oid]) for oid in self.pending]

    def open_positions(self) -> list[dict]:
        self._read()
        return ([{"product_id": PRODUCT, "size": self.position}]
                if self.position else [])

    def product_position(self, product_id: int) -> dict:
        self._read()
        if product_id != PRODUCT:
            raise ValueError("Unexpected product")
        # The actual real-time Delta response may omit product_id.
        return {"size": self.position}

    def fills_for_product(self, product_id: int, *,
                          start_time_us: int, end_time_us: int) -> list[dict]:
        self._read()
        if (product_id != PRODUCT or start_time_us != START_US
                or end_time_us <= START_US):
            raise ValueError("Unexpected fill-history request")
        return [dict(row) for row in self.fills]


def _environment(folder: Path) -> tuple[FakeBroker, LiveIntentJournal,
                                       LifecycleJournal, ExitAttemptJournal]:
    broker = FakeBroker()
    intent = LiveIntentJournal(folder / "intents.sqlite")
    lifecycle = LifecycleJournal(folder / "lifecycle.sqlite")
    lifecycle.register_flat_baseline(
        product_id=PRODUCT, baseline_us=START_US,
        account_positions=[], active_orders=[],
    )
    exits = ExitAttemptJournal(folder / "exits.sqlite")
    entry = OrderIntent(
        signal_id="rehearsal-entry", product_id=PRODUCT, side="buy",
        size=3, limit_price="20", contract_value="0.01",
        usd_inr="85", ticker_timestamp=1,
    )
    client_id = intent.reserve(entry)
    intent.note(client_id, "ACKNOWLEDGED", order_id="101")
    broker.orders[101] = {
        "id": 101, "client_order_id": client_id, "product_id": PRODUCT,
        "side": "buy", "size": 3, "unfilled_size": 0, "state": "closed",
    }
    broker.fills.append({
        "id": "entry-fill", "order_id": 101, "product_id": PRODUCT,
        "side": "buy", "size": 3,
    })
    broker.position = 3
    return broker, intent, lifecycle, exits


def _draft(remaining: int, size: int, quote_unix: int) -> dict:
    return {
        "status": "DRAFT_EXIT_ONLY", "submit_authorized": False,
        "side": "sell", "reduce_only": True,
        "order_type": "limit_order", "time_in_force": "ioc",
        "product_id": PRODUCT, "remaining_contracts": remaining,
        "draft_size": size, "available_bid_depth": size,
        "quote_observed_unix": quote_unix, "limit_price": "20",
        "reason": "PRE_EXPIRY_SAFETY_CUTOFF",
    }


def _register_exit(broker: FakeBroker, client_id: str, *, broker_id: int,
                   qty: int, filled: int, state: str = "cancelled") -> None:
    broker.orders[broker_id] = {
        "id": broker_id, "client_order_id": client_id, "product_id": PRODUCT,
        "side": "sell", "reduce_only": True, "size": qty,
        "unfilled_size": qty - filled, "state": state,
    }
    if filled:
        broker.fills.append({
            "id": f"exit-fill-{broker_id}", "order_id": broker_id,
            "product_id": PRODUCT, "side": "sell", "size": filled,
        })
        broker.position -= filled
    if state in ("open", "pending"):
        broker.pending.append(broker_id)


def _reconcile(broker, entries, lifecycle, exits):
    return reconcile_exit_broker(
        client=broker, entries=entries, lifecycle=lifecycle,
        exits=exits, now_us=END_US,
    )


def _partial_restart_flat(folder: Path) -> dict:
    broker, entries, lifecycle, exits = _environment(folder)
    first = exits.prepare(_draft(remaining=3, size=2, quote_unix=1000))
    _register_exit(broker, first["client_order_id"],
                   broker_id=201, qty=2, filled=1)
    partially_closed = _reconcile(broker, entries, lifecycle, exits)
    if (partially_closed["remaining_qty"] != 2
            or partially_closed["submit_authorized"] is not False):
        raise AssertionError("Partially filled exit incorrectly reconciled")

    # Simulate container process restart: reconstruct all three durable journals.
    entries = LiveIntentJournal(entries.path)
    lifecycle = LifecycleJournal(lifecycle.path)
    exits = ExitAttemptJournal(exits.path)
    if exits.status()["attempts"][0]["state"] != "TERMINAL_VERIFIED":
        raise AssertionError("Restart lost first exit terminal evidence")
    second = exits.prepare(_draft(remaining=2, size=2, quote_unix=1002))
    if second["client_order_id"] == first["client_order_id"]:
        raise AssertionError("Second exit reused the first client order ID")
    _register_exit(broker, second["client_order_id"],
                   broker_id=202, qty=2, filled=2)
    closed = _reconcile(broker, entries, lifecycle, exits)
    if (closed["status"] != "TERMINAL_VERIFIED_FLAT"
            or closed["remaining_qty"] != 0
            or exits.status()["attempts"][-1]["state"] != "TERMINAL_VERIFIED"):
        raise AssertionError("Reconciled exit not flat after restart")
    try:
        exits.prepare(_draft(remaining=1, size=1, quote_unix=1004))
    except ExitRecoveryError:
        pass
    else:
        raise AssertionError("Flat position incorrectly permitted third exit")
    return {"scenario": "partial_restart_then_flat", "result": "PASS",
            "remaining_contracts": closed["remaining_qty"],
            "exit_attempts": 2}


def _unknown_ack(folder: Path) -> dict:
    broker, entries, lifecycle, exits = _environment(folder)
    exits.prepare(_draft(remaining=3, size=2, quote_unix=1000))
    try:
        _reconcile(broker, entries, lifecycle, exits)
    except LookupError:
        pass
    else:
        raise AssertionError("Unknown acknowledgement was accepted")
    if exits.status()["attempts"][0]["state"] != "PREPARED_UNKNOWN":
        raise AssertionError("Unknown acknowledgement cleared prematurely")
    try:
        ExitAttemptJournal(exits.path).prepare(
            _draft(remaining=3, size=2, quote_unix=1005)
        )
    except ExitRecoveryError:
        pass
    else:
        raise AssertionError("Duplicate exit attempt allowed after unknown acknowledgement")
    return {"scenario": "unknown_ack_no_retry", "result": "PASS"}


def _unexpected_fill(folder: Path) -> dict:
    broker, entries, lifecycle, exits = _environment(folder)
    order = exits.prepare(_draft(remaining=3, size=2, quote_unix=1000))
    _register_exit(broker, order["client_order_id"], broker_id=201, qty=2, filled=1)
    broker.fills.append({
        "id": "manual-trade", "order_id": 9999,
        "product_id": PRODUCT, "side": "buy", "size": 1,
    })
    try:
        _reconcile(broker, entries, lifecycle, exits)
    except ExitRecoveryError:
        pass
    else:
        raise AssertionError("Unregistered fill was accepted")
    if exits.status()["attempts"][0]["state"] != "PREPARED_UNKNOWN":
        raise AssertionError("Unexpected fill incorrectly certified exit")
    return {"scenario": "external_fill_blocks", "result": "PASS"}


def _broker_outage(folder: Path) -> dict:
    broker, entries, lifecycle, exits = _environment(folder)
    exits.prepare(_draft(remaining=3, size=2, quote_unix=1000))
    broker.unreachable = True
    try:
        _reconcile(broker, entries, lifecycle, exits)
    except ConnectionError:
        pass
    else:
        raise AssertionError("Broker outage was accepted")
    if ExitAttemptJournal(exits.path).status()["attempts"][0][
        "state"
    ] != "PREPARED_UNKNOWN":
        raise AssertionError("Outage cleared unknown exit")
    return {"scenario": "broker_outage_blocks", "result": "PASS"}


def _pending_order(folder: Path) -> dict:
    broker, entries, lifecycle, exits = _environment(folder)
    prepared = exits.prepare(_draft(remaining=3, size=2, quote_unix=1000))
    _register_exit(broker, prepared["client_order_id"], broker_id=201,
                   qty=2, filled=1, state="open")
    observed = _reconcile(broker, entries, lifecycle, exits)
    if (observed["status"] != "WAIT_FOR_BROKER_SETTLEMENT"
            or exits.status()["attempts"][0]["state"] != "PREPARED_UNKNOWN"):
        raise AssertionError("Pending order wrongly declared terminal")
    return {"scenario": "pending_exit_waits", "result": "PASS"}


def _unfunded_and_stale_exit() -> dict:
    wallet = {
        "asset_symbol": "USD", "balance": "0.000056834",
        "available_balance": "0.000056834",
        "blocked_margin": "0", "order_margin": "0",
        "position_margin": "0",
    }
    preflight = inspect_account([wallet], [], [])
    if (preflight["status"] != "BLOCKED"
            or preflight["live_entry_authorized"] is not False):
        raise AssertionError("Unfunded account wrongly cleared")
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    observation = assess_exit(
        reconciliation={
            "status": "CONSISTENT_BUT_NOT_AUTHORIZED", "consistent": True,
            "live_order_authorized": False, "exclusive_ownership_proven": False,
            "product_id": PRODUCT, "reported_position_size": 3,
            "registered_bought": 3, "registered_sold": 0,
        },
        underlying="BTC", now=now, expiry=now + timedelta(days=1),
        safety_cutoff=now - timedelta(minutes=1),
        original_signal=None,
        quote={
            "product_id": PRODUCT, "best_bid": "20", "best_ask": "21",
            "tick_size": "0.1", "best_bid_size": 3,
            "timestamp_unix": int(now.timestamp()) - 20,
        },
    )
    if observation["status"] != "EXIT_DUE_BLOCKED":
        raise AssertionError("Stale quote incorrectly produced valid exit draft")
    return {"scenario": "unfunded_and_stale_quote", "result": "PASS"}


def run_rehearsal() -> dict:
    """Run all scenarios in isolated temporary directories, with zero keys."""
    # No API methods beyond our in-memory FakeBroker are ever instantiated.
    with TemporaryDirectory(prefix="optopsy-rehearsal-") as root:
        base = Path(root)
        results = [
            _partial_restart_flat(base / "complete"),
            _unknown_ack(base / "unknown"),
            _unexpected_fill(base / "foreign"),
            _broker_outage(base / "outage"),
            _pending_order(base / "pending"),
            _unfunded_and_stale_exit(),
        ]
    return {
        "mode": "OFFLINE_SIMULATED_EXIT_REHEARSAL",
        "scenarios": results, "passed": len(results),
        "failed": 0, "live_orders_sent": 0, "trading_enabled": False,
        "production_ready": False,
    }
