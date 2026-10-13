"""Research-time budget estimates only; NEVER authorize live order execution.

Rates and balances in USD equivalent are exposed by Delta India at a fixed platform
conversion of 85 INR per USD per current documentation; verify before deployment.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class CapitalPolicyError(ValueError):
    pass


def dec(value: object) -> Decimal:
    if isinstance(value, bool):
        raise CapitalPolicyError("Boolean price/size unsupported")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapitalPolicyError("Invalid numeric amount") from exc
    if not result.is_finite():
        raise CapitalPolicyError("Nonfinite amount")
    return result


@dataclass(frozen=True)
class CapitalPolicy:
    budget_inr: Decimal = Decimal("5000")
    usd_inr: Decimal = Decimal("85")
    reserve_inr: Decimal = Decimal("500")
    max_premium_fee_buffer: Decimal = Decimal("0.03")

    def __post_init__(self) -> None:
        if not (Decimal(0) < self.budget_inr <= Decimal("5000")):
            raise CapitalPolicyError("Project budget must be between 0 and INR 5000")
        if self.usd_inr <= 0 or self.reserve_inr < 0 or self.reserve_inr >= self.budget_inr:
            raise CapitalPolicyError("Invalid currency rate or reserve")
        if not (Decimal(0) <= self.max_premium_fee_buffer <= Decimal("0.5")):
            raise CapitalPolicyError("Invalid fee buffer")

    def estimate_long_option(self, *, limit_price_usd: object,
                             contract_value: object, size: int,
                             available_usd: object) -> dict[str, str]:
        """Conservative preflight for *long-option research*, not an order router.

        Does not replace live bid/ask depth, exchange charges, order acknowledgements,
        protective exits, or authenticated position/order reconciliation.
        """
        if type(size) is not int or size < 1:
            raise CapitalPolicyError("Size must be a positive integer")
        price, contract, available = (dec(limit_price_usd), dec(contract_value), dec(available_usd))
        if price <= 0 or contract <= 0 or available < 0:
            raise CapitalPolicyError("Price/contract must be positive; balance nonnegative")
        premium = price * contract * size * self.usd_inr
        estimated = premium * (Decimal(1) + self.max_premium_fee_buffer)
        deployable = min(self.budget_inr, available * self.usd_inr) - self.reserve_inr
        if deployable <= 0 or estimated > deployable:
            raise CapitalPolicyError("Estimated premium plus buffer exceeds capped deployable funds")
        return {"premium_inr": str(premium), "estimate_with_buffer_inr": str(estimated),
                "deployable_inr": str(deployable)}
