"""Operator-set capital limits (from .env). Defaults reproduce the original INR 5,000 policy.

OPTOPSY_CAPITAL_INR     budget ceiling for the strategy           (default 5000)
OPTOPSY_RESERVE_INR     never deployed, kept as cash buffer       (default 500)
OPTOPSY_MAX_CONTRACTS   most contracts bought per entry           (default 1)

Invalid values fail closed (the worker idles in standby and says why); they never fall back
to a larger limit. ABSOLUTE_MAX_* are typo guards (an extra zero), not recommendations.
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

DEFAULT_CAPITAL = Decimal(5000)
DEFAULT_RESERVE = Decimal(500)
ABSOLUTE_MAX_CAPITAL = Decimal(1_000_000)
ABSOLUTE_MAX_CONTRACTS = 100


class CapitalConfigError(ValueError):
    pass


def _env_decimal(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise CapitalConfigError(f"{name} is not a number") from exc
    if not value.is_finite():
        raise CapitalConfigError(f"{name} is not finite")
    return value


def capital_inr() -> Decimal:
    value = _env_decimal("OPTOPSY_CAPITAL_INR", DEFAULT_CAPITAL)
    if not Decimal(0) < value <= ABSOLUTE_MAX_CAPITAL:
        raise CapitalConfigError(
            f"OPTOPSY_CAPITAL_INR must be above 0 and at most {ABSOLUTE_MAX_CAPITAL}")
    if value <= reserve_inr(_check=False):
        raise CapitalConfigError("OPTOPSY_CAPITAL_INR must be larger than the reserve")
    return value


def reserve_inr(*, _check: bool = True) -> Decimal:
    value = _env_decimal("OPTOPSY_RESERVE_INR", DEFAULT_RESERVE)
    if value < 0:
        raise CapitalConfigError("OPTOPSY_RESERVE_INR cannot be negative")
    if _check and value >= capital_inr():
        raise CapitalConfigError("OPTOPSY_RESERVE_INR must be smaller than the capital")
    return value


def max_contracts() -> int:
    raw = os.getenv("OPTOPSY_MAX_CONTRACTS", "").strip()
    if not raw:
        return 1
    if not raw.isdigit() or not 1 <= int(raw) <= ABSOLUTE_MAX_CONTRACTS:
        raise CapitalConfigError(
            f"OPTOPSY_MAX_CONTRACTS must be a whole number from 1 to {ABSOLUTE_MAX_CONTRACTS}")
    return int(raw)


def _env_days(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= 84:
        raise CapitalConfigError(f"{name} must be a whole number of days from 1 to 84")
    return int(raw)


def target_dte_days() -> int:
    """Preferred days-to-expiry for entries (default 28 = the 3-4 week bucket)."""
    return _env_days("OPTOPSY_TARGET_DTE_DAYS", 28)


def min_dte_days() -> int:
    """Never enter an option with fewer days to expiry than this (default 7)."""
    value = _env_days("OPTOPSY_MIN_DTE_DAYS", 7)
    if value > target_dte_days():
        raise CapitalConfigError("OPTOPSY_MIN_DTE_DAYS cannot exceed OPTOPSY_TARGET_DTE_DAYS")
    return value


def validate_all() -> dict:
    """Check everything at once; returns the effective limits."""
    return {"capital_inr": str(capital_inr()), "reserve_inr": str(reserve_inr()),
            "max_contracts": max_contracts(),
            "target_dte_days": target_dte_days(), "min_dte_days": min_dte_days()}
