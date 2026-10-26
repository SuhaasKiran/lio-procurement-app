"""Deterministic quote-total reconciliation.

AI identifies the lines and adjustments; this module is deliberately not AI
based. It is the single place that decides whether the quote's stated total is
consistent with the selected lines, taxes, and discounts.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Protocol


CENT = Decimal("0.01")


class HasTotalPrice(Protocol):
    total_price: float


class HasAdjustment(Protocol):
    kind: str
    amount: float
    scope: str
    line_index: int | None


def money(value: float | Decimal) -> Decimal:
    """Convert a JSON number without binary float arithmetic surprises."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_total(
    lines: Iterable[HasTotalPrice], adjustments: Iterable[HasAdjustment]
) -> Decimal:
    """Quoted line totals plus quote-level charges, taxes, and discounts.

    A line total is the amount printed for that item in the quote. It already
    captures any item-specific tax or discount, so line-level adjustments are
    saved for review only and intentionally do not participate in this check.
    """
    result = sum((money(line.total_price) for line in lines), Decimal("0.00"))
    for adjustment in adjustments:
        if adjustment.scope != "order":
            continue
        amount = money(adjustment.amount)
        result += amount if adjustment.kind in ("tax", "charge") else -amount
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


def validate_adjustment_line_indexes(
    adjustments: Iterable[HasAdjustment], line_count: int
) -> None:
    """Line adjustments must refer to a selected line in the submitted request."""
    for adjustment in adjustments:
        if adjustment.scope == "line" and (
            adjustment.line_index is None
            or adjustment.line_index < 0
            or adjustment.line_index >= line_count
        ):
            raise ValueError("An adjustment refers to an invalid order line")
