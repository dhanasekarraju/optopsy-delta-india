"""Integrated, authenticated-GET-only Optopsy options controller cycle.

This coordinates the existing entry/exit strategy gates and broker inventory.
It NEVER places, cancels or retries orders. This is the supervised decision
core for further execution work, not a deployable live order worker.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .account_preflight import account_preflight
from .broker_lifecycle import reconcile_broker
from .entry_admission import plan_entry
from .exit_supervisor import ExitSafetyError, assess_exit
from .safety_watchdog import read_only_watchdog_check
from .snapshot import SnapshotError, _iso_datetime


class ControllerError(ValueError):
    """Invalid external controller configuration; no order authorization."""


def _output(state: str, *, product_id: int, reasons: list[str],
            reconciliation: dict | None = None,
            draft: dict | None = None) -> dict:
    return {
        "mode": "NONTRADING_INTEGRATED_CONTROLLER",
        "state": state,
        "product_id": product_id,
        "blocking_reasons": sorted(set(reasons)),
        "reported_position_size": (
            reconciliation.get("reported_position_size")
            if isinstance(reconciliation, dict) else None
        ),
        "candidate": draft,
        "live_entry_authorized": False,
        "live_exit_authorized": False,
        "order_submitted": False,
        "production_ready": False,
    }


def controller_cycle(
    *, client: Any, intent_journal: Any, lifecycle: Any, exit_attempts: Any,
    signal: dict | None, contract: dict, quote: dict | None, now: datetime,
    safety_cutoff: datetime, order_size: int,
) -> dict:
    """One synchronous observation and decision; no broker writes.

    All broker reads are successive GETs, NOT an atomic exchange snapshot.
    Previous terminal exits must be passed to reconcile_broker. Unknown exit
    attempts stop before any new trading decision.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ControllerError("Timezone-aware evaluation required")
    if not isinstance(safety_cutoff, datetime) or safety_cutoff.tzinfo is None:
        raise ControllerError("Timezone-aware expiry cutoff required")
    if not isinstance(contract, dict):
        raise ControllerError("Verified contract metadata required")
    product_id = contract.get("product_id")
    underlying = contract.get("underlying")
    if type(product_id) is not int or product_id <= 0 or underlying not in ("BTC", "ETH"):
        raise ControllerError("Invalid contract product or underlying")
    if contract.get("live_order_authorized") is not False:
        raise ControllerError("Unsupported contract authorization")
    try:
        expiry = _iso_datetime(contract["expiry_utc"])
    except (KeyError, SnapshotError, TypeError, ValueError, OverflowError) as exc:
        raise ControllerError("Invalid actual option expiry") from exc
    now = now.astimezone(UTC)
    cutoff = safety_cutoff.astimezone(UTC)
    if cutoff >= expiry:
        raise ControllerError("Safety cutoff must precede option expiry")

    status = exit_attempts.status()
    attempts = status.get("attempts") if isinstance(status, dict) else None
    if not isinstance(attempts, list):
        return _output("STOP_UNKNOWN_EXIT_JOURNAL", product_id=product_id,
                       reasons=["EXIT_JOURNAL_UNAVAILABLE"])
    if any(not isinstance(row, dict) or row.get("product_id") != product_id
           for row in attempts):
        return _output("STOP_UNKNOWN_EXIT_JOURNAL", product_id=product_id,
                       reasons=["EXIT_JOURNAL_PRODUCT_MISMATCH"])
    if attempts and attempts[-1].get("state") != "TERMINAL_VERIFIED":
        return _output("RECOVERY_REQUIRED", product_id=product_id,
                       reasons=["EXIT_ACKNOWLEDGEMENT_NOT_TERMINAL_VERIFIED"])

    try:
        reconciliation = reconcile_broker(
            client=client, intent_journal=intent_journal,
            lifecycle=lifecycle, exit_attempts=exit_attempts,
            now_us=int(now.timestamp() * 1_000_000),
        )
    except Exception:  # noqa: BLE001 - broker failure always blocks, redact details
        return _output("STOP_BROKER_RECONCILIATION", product_id=product_id,
                       reasons=["BROKER_HISTORY_OR_INVENTORY_NOT_VERIFIED"])
    if (reconciliation.get("status") != "CONSISTENT_BUT_NOT_AUTHORIZED"
            or reconciliation.get("product_id") != product_id
            or reconciliation.get("consistent") is not True):
        return _output("STOP_BROKER_RECONCILIATION", product_id=product_id,
                       reasons=["BROKER_RECONCILIATION_INVALID"])

    snapshot = read_only_watchdog_check(
        client=client, product_id=product_id,
        lifecycle_status=lifecycle.status(), exit_status=status,
        expiry=expiry, exit_cutoff=cutoff, now=now,
    )
    if snapshot.get("broker_healthy") is not True:
        return _output("STOP_PRIVATE_BROKER_READ", product_id=product_id,
                       reasons=["BROKER_CURRENT_POSITION_UNKNOWN"],
                       reconciliation=reconciliation)
    if snapshot.get("product_position_quantity") != reconciliation.get(
        "reported_position_size"
    ):
        return _output("STOP_BROKER_POSITION_CHANGED", product_id=product_id,
                       reasons=["POSITION_CHANGED_BETWEEN_BROKER_READS"],
                       reconciliation=reconciliation)

    qty = reconciliation["reported_position_size"]
    if type(qty) is not int or qty < 0:
        return _output("STOP_BROKER_RECONCILIATION", product_id=product_id,
                       reasons=["INVALID_RECONCILED_QUANTITY"])
    if qty > 0:
        # A normal open position triggers a watchdog alert. That alone is
        # not grounds to skip a potentially due protective exit.
        disallowed = set(snapshot.get("issues", [])) - {
            "ACCOUNT_POSITIONS_PRESENT", "OPEN_POSITION_EXIT_CUTOFF_REACHED",
            "OPEN_POSITION_EXPIRED_SETTLEMENT_REVIEW",
        }
        if disallowed:
            return _output("STOP_EXIT_UNSAFE_BROKER", product_id=product_id,
                           reasons=sorted(disallowed), reconciliation=reconciliation)
        try:
            draft = assess_exit(
                reconciliation=reconciliation, underlying=underlying,
                now=now, expiry=expiry, safety_cutoff=cutoff,
                original_signal=signal, quote=quote,
            )
        except ExitSafetyError:
            return _output("STOP_EXIT_INVALID_SIGNAL_OR_METADATA",
                           product_id=product_id,
                           reasons=["EXIT_POLICY_EVALUATION_INVALID"],
                           reconciliation=reconciliation)
        state = {
            "DRAFT_EXIT_ONLY": "EXIT_DRAFT_NOT_SENT",
            "HOLD": "HOLD_OBSERVED_POSITION",
            "EXIT_DUE_BLOCKED": "EXIT_DUE_NEEDS_OPERATOR",
            "BLOCKED_NEEDS_ATTENTION": "EXIT_SIGNAL_NEEDS_OPERATOR",
            "URGENT_MANUAL_RECONCILIATION": "EXPIRED_NEEDS_OPERATOR",
            "FLAT": "STOP_BROKER_POSITION_CHANGED",
        }.get(draft["status"], "STOP_EXIT_UNKNOWN_DECISION")
        return _output(state, product_id=product_id,
                       reasons=[] if state in ("EXIT_DRAFT_NOT_SENT",
                                               "HOLD_OBSERVED_POSITION")
                       else [draft["reason"]],
                       reconciliation=reconciliation, draft=draft)

    # A completed trade is not automatically re-entered under a new signal;
    # require a new, separately reviewed flat baseline and campaign.
    if intent_journal.status() or attempts:
        return _output("FLAT_PREVIOUS_CAMPAIGN", product_id=product_id,
                       reasons=["PREVIOUS_ORDERS_REQUIRE_NEW_CAMPAIGN"],
                       reconciliation=reconciliation)
    if not isinstance(signal, dict) or signal.get("entry") is not True:
        return _output("FLAT_NO_ENTRY_SIGNAL", product_id=product_id,
                       reasons=[], reconciliation=reconciliation)
    if snapshot.get("issues"):
        return _output("STOP_ENTRY_WATCHDOG", product_id=product_id,
                       reasons=snapshot["issues"], reconciliation=reconciliation)
    try:
        account = account_preflight(client)
        result = plan_entry(
            signal=signal, contract=contract, now=now, account=account,
            broker_observation=snapshot, live_position_size=0,
            order_size=order_size, quote=quote,
        )
    except Exception:  # noqa: BLE001 - invalid/unavailable financial inputs block
        return _output("STOP_ENTRY_ADMISSION", product_id=product_id,
                       reasons=["ACCOUNT_OR_ENTRY_CANDIDATE_NOT_VERIFIED"],
                       reconciliation=reconciliation)
    return _output("ENTRY_CANDIDATE_NOT_SENT", product_id=product_id, reasons=[],
                   reconciliation=reconciliation, draft=result)
