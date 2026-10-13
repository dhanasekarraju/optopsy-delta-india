"""Read-only account diagnostic; never authorizes any order."""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any

from .capital import capital_inr, reserve_inr
from .client import DeltaClient
from .policy import CapitalPolicyError, dec


class AccountPreflightError(ValueError):
    """Invalid private account data; do not assume unused funds or no exposure."""


def number(value: object, label: str) -> Decimal:
    try:
        n = dec(value)
    except CapitalPolicyError as exc:
        raise AccountPreflightError("Invalid " + label) from exc
    if n < 0:
        raise AccountPreflightError("Negative " + label)
    return n


def inspect_account(wallets: Any, positions: Any, orders: Any) -> dict:
    """Read-only cash/exposure diagnosis; never sufficient for live entry."""
    if not all(isinstance(collection, list) for collection in (wallets, positions, orders)):
        raise AccountPreflightError("Incomplete account response")
    if any(not isinstance(row, dict) for row in wallets + positions + orders):
        raise AccountPreflightError("Malformed account row")
    blockers = []
    usd = [row for row in wallets if row.get("asset_symbol") == "USD"]
    if len(usd) != 1:
        blockers.append("USD_WALLET_MISSING_OR_DUPLICATE")
    if positions:
        blockers.append("EXISTING_POSITIONS")
    if orders:
        blockers.append("ACTIVE_ORDERS")
    for row in wallets:
        symbol = row.get("asset_symbol")
        if not isinstance(symbol, str) or not symbol:
            raise AccountPreflightError("Unknown wallet asset")
        if symbol != "USD" and (
            number(row.get("balance"), "balance") > 0
            or number(row.get("available_balance"), "available balance") > 0
        ):
            blockers.append("OTHER_COLLATERAL")
    available = Decimal(0)
    if len(usd) == 1:
        w = usd[0]
        balance = number(w.get("balance"), "balance")
        available = number(w.get("available_balance"), "available balance")
        if available > balance:
            raise AccountPreflightError("Available exceeds wallet balance")
        # Check only fields actually returned; absent fields cannot be assumed zero.
        for field in ("blocked_margin", "order_margin", "position_margin"):
            if field not in w:
                blockers.append("UNKNOWN_MARGIN_FIELDS")
            elif number(w[field], field) > 0:
                blockers.append("EXISTING_MARGIN")
    cap = max(Decimal(0), min(available * 85, capital_inr()) - reserve_inr())
    if cap <= 0:
        blockers.append("INSUFFICIENT_AVAILABLE_BALANCE")
    estimated_max_premium = cap / Decimal("1.05")
    def fmt(x: Decimal) -> str:
        return str(x.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
    return {
        "mode": "READ_ONLY_PRIVATE_PREFLIGHT",
        "status": "BLOCKED" if blockers else "PRELIMINARY_CLEAR",
        "blockers": sorted(set(blockers)),
        "wallet_rows": len(wallets),
        "position_rows": len(positions),
        "active_order_rows": len(orders),
        "available_usd_equivalent": str(available),
        "deployable_inr_after_reserve": fmt(cap),
        "estimated_max_entry_premium_inr": fmt(estimated_max_premium),
        "live_entry_authorized": False,
        "account_wide_positions_may_lag": True,
    }


def account_preflight(client: DeltaClient) -> dict:
    return inspect_account(
        client.wallet_balances(), client.open_positions(), client.active_orders()
    )
