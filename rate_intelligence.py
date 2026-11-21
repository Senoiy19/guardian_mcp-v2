"""Deterministic helpers for Liquity V2 borrowing-rate intelligence."""
from __future__ import annotations

from decimal import Decimal
from statistics import median


def summarise_rate_sample(rates_pct, current_rate_pct):
    """Summarise a branch sample without presenting it as a full branch census."""
    rates = sorted(Decimal(str(rate)) for rate in rates_pct if rate is not None)
    current = Decimal(str(current_rate_pct))
    if not rates:
        return {"sample_count": 0, "median_rate_pct": None, "share_lower_pct": None}
    med = Decimal(str(median(rates)))
    lower = sum(1 for rate in rates if rate < current)
    return {
        "sample_count": len(rates),
        "median_rate_pct": med,
        "share_lower_pct": Decimal(lower) * Decimal(100) / Decimal(len(rates)),
        "lower_count": lower,
    }


def annual_interest_bold(debt_bold, rate_pct):
    return Decimal(str(debt_bold)) * Decimal(str(rate_pct)) / Decimal(100)


def annual_savings_vs_rate(debt_bold, current_rate_pct, comparison_rate_pct):
    return annual_interest_bold(debt_bold, current_rate_pct) - annual_interest_bold(debt_bold, comparison_rate_pct)
