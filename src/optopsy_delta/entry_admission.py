"""Fail-closed entry admission planner. It NEVER calls Delta order submission.

This is a safety gate, not proof of Optopsy signal execution or live readiness.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from .live_gateway import OrderIntent, validate


class AdmissionError(ValueError):
    """Untrusted, stale or contradictory entry evidence."""


def plan_entry(*, signal: dict, contract: dict, now: datetime,
               account: dict, broker_observation: dict,
               live_position_size: object, order_size: int,
               quote: dict) -> dict:
    """Validate one candidate and return non-executable intent evidence.

    The actual original Optopsy signal must come from upstream execution;
    this function does not generate or infer signals.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise AdmissionError("Aware evaluation timestamp required")
    now = now.astimezone(UTC)
    if not isinstance(signal, dict) or not isinstance(contract, dict):
        raise AdmissionError("Signal and contract evidence required")
    if not isinstance(account, dict) or not isinstance(broker_observation, dict):
        raise AdmissionError("Account and broker evidence required")
    expected = (now.date() - timedelta(days=1)).isoformat()
    if (signal.get("last_complete_utc_day") != expected
            or signal.get("underlying") not in ("BTC", "ETH")
            or signal.get("strategy") != "long_calls"
            or signal.get("entry_rule") != "optopsy.rsi_below(period=14, threshold=30)"
            or signal.get("parameters_tuned") is not False
            or signal.get("entry") is not True
            or signal.get("exit") is not False):
        raise AdmissionError("Not a valid fresh original Optopsy entry")
    if (contract.get("underlying") != signal["underlying"]
            or contract.get("live_order_authorized") is not False):
        raise AdmissionError("Untrusted screened option")
    if (account.get("status") != "PRELIMINARY_CLEAR"
            or account.get("live_entry_authorized") is not False
            or account.get("position_rows") != 0
            or account.get("active_order_rows") != 0):
        raise AdmissionError("Account preflight is not clear")
    if (broker_observation.get("status") != "OBSERVATION_CLEAR_NOT_AUTHORIZED"
            or broker_observation.get("broker_healthy") is not True
            or broker_observation.get("live_entry_authorized") is not False
            or broker_observation.get("account_position_rows") != 0
            or broker_observation.get("active_order_rows") != 0):
        raise AdmissionError("Broker watchdog cannot certify flat account")
    try:
        size = Decimal(str(live_position_size))
        available = Decimal(str(account["deployable_inr_after_reserve"]))
    except (InvalidOperation, TypeError, KeyError, ValueError) as exc:
        raise AdmissionError("Invalid cash or position evidence") from exc
    if not size.is_finite() or size != 0 or not available.is_finite() or available <= 0:
        raise AdmissionError("Unexpected position or insufficient cash")
    if type(order_size) is not int or order_size <= 0:
        raise AdmissionError("Invalid requested contract size")
    if not isinstance(quote, dict) or quote.get("product_id") != contract.get("product_id"):
        raise AdmissionError("Missing product-bound quote")
    if quote.get("captured_at") != contract.get("captured_at"):
        raise AdmissionError("Contract screen and quote snapshots differ")
    timestamp = quote.get("timestamp_unix")
    if type(timestamp) is not int:
        raise AdmissionError("Missing exchange quote timestamp")
    if type(contract.get("product_id")) is not int:
        raise AdmissionError("Invalid contract identity")
    if contract.get("tick_size") != quote.get("tick_size"):
        raise AdmissionError("Quote tick metadata differs")
    intent = OrderIntent(
        signal_id=f"rsi-{signal['underlying']}-{expected}-{contract['product_id']}",
        product_id=contract["product_id"], side="buy", size=order_size,
        limit_price=str(quote["best_ask"]),
        contract_value=str(contract["contract_value"]),
        usd_inr="85", ticker_timestamp=timestamp,
    )
    try:
        validate(
            intent, now=int(now.timestamp()),
            cash_inr=available + Decimal(500), tick_size=quote["tick_size"],
            observed_bid=quote["best_bid"], observed_ask=quote["best_ask"],
            observed_depth=quote["best_ask_size"], external_exposure=False,
        )
    except Exception as exc:
        raise AdmissionError("Gateway risk validation rejected candidate") from exc
    # No order submission or ledger reserve is performed by this module.
    return {"status": "NON_EXECUTABLE_ENTRY_INTENT", "product_id": intent.product_id,
            "contract_count": intent.size, "limit_price": intent.limit_price,
            "original_signal_day": expected, "live_entry_authorized": False,
            "submitted": False, "requires_independent_admission": True}
