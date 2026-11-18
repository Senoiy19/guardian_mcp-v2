"""Pure, deterministic helpers used by the BOLD Guardian MCP server."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

YIELD_SOURCE = "https://api.liquity.org/v2/website/bold-venues.json"
CHAIN_ID = 1
MAX_DEBT_BOLD = Decimal("1e15")
MAX_RATE_PCT = Decimal("1000")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decimal_value(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid number") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number


def simulate_borrowing_cost(
    debt_bold: Any,
    current_rate_pct: Any,
    proposed_rate_pct: Any,
    days: int = 365,
) -> dict[str, Any]:
    debt = decimal_value(debt_bold, "debt_bold")
    current = decimal_value(current_rate_pct, "current_rate_pct")
    proposed = decimal_value(proposed_rate_pct, "proposed_rate_pct")
    if debt <= 0:
        raise ValueError("debt_bold must be greater than zero")
    if current < 0 or proposed < 0:
        raise ValueError("Interest rates cannot be negative")
    if debt > MAX_DEBT_BOLD:
        raise ValueError("debt_bold is unrealistically large")
    if current > MAX_RATE_PCT or proposed > MAX_RATE_PCT:
        raise ValueError(f"Interest rates above {MAX_RATE_PCT}% are not accepted")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
        raise ValueError("days must be an integer between 1 and 3650")

    year_fraction = Decimal(days) / Decimal(365)
    current_cost = debt * current / Decimal(100) * year_fraction
    proposed_cost = debt * proposed / Decimal(100) * year_fraction
    delta = current_cost - proposed_cost
    return {
        "debt_bold": str(debt),
        "current_rate_pct": str(current),
        "proposed_rate_pct": str(proposed),
        "days": days,
        "current_interest_estimate_bold": str(current_cost),
        "proposed_interest_estimate_bold": str(proposed_cost),
        "estimated_savings_bold": str(delta),
        "interpretation": "saving" if delta > 0 else "additional_cost" if delta < 0 else "no_difference",
        "method": "simple interest; constant debt balance; 365-day year",
        "exclusions": [
            "batch management fees",
            "compounding and changing debt balance",
            "rate-adjustment transaction costs and protocol constraints",
            "redemption exposure and collateral-price movements",
        ],
        "not_a_rate_recommendation": True,
    }


def extract_rows(payload: Any) -> list[Any]:
    """Return the venue list from either a bare list or a {venues|data|results: [...]} envelope.

    Shared by the MCP server and the Telegram bot so both accept the same feed shapes.
    """
    rows = payload.get("venues") or payload.get("data") or payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError("Yield source returned an empty or invalid venue list")
    return rows


def normalise_yield_rows(payload: Any) -> list[dict[str, Any]]:
    """Validate the Liquity venues payload and convert weekly_apr fraction to percent."""
    rows = extract_rows(payload)

    normalised: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        protocol = row.get("protocol")
        asset = row.get("asset")
        if not protocol or not asset:
            continue
        apr_raw = row.get("weekly_apr")
        apr_pct = None
        if apr_raw is not None:
            try:
                candidate = decimal_value(apr_raw, f"venues[{index}].weekly_apr") * Decimal(100)
                apr_pct = str(candidate) if candidate >= 0 else None
            except ValueError:
                apr_pct = None
        tvl_raw = row.get("tvl")
        tvl = None
        if tvl_raw is not None:
            try:
                candidate = decimal_value(tvl_raw, f"venues[{index}].tvl")
                tvl = str(candidate) if candidate >= 0 else None
            except ValueError:
                tvl = None
        normalised.append({
            "protocol": str(protocol),
            "asset": str(asset),
            "venue": str(row.get("venue") or row.get("name") or ""),
            "apr_pct": apr_pct,
            "tvl_usd": tvl,
            "link": str(row.get("link") or "") or None,
            "source_apr_field": "weekly_apr",
        })
    if not normalised:
        raise ValueError("Yield source contained no usable venues")
    return normalised


def compare_yields(
    venues: list[dict[str, Any]],
    asset: str | None = None,
    min_tvl_usd: Any | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    min_tvl = decimal_value(min_tvl_usd, "min_tvl_usd") if min_tvl_usd is not None else None
    if min_tvl is not None and min_tvl < 0:
        raise ValueError("min_tvl_usd cannot be negative")
    filtered = []
    for venue in venues:
        if asset and asset.casefold() not in venue["asset"].casefold():
            continue
        tvl = decimal_value(venue["tvl_usd"], "tvl_usd") if venue.get("tvl_usd") is not None else None
        if min_tvl is not None and (tvl is None or tvl < min_tvl):
            continue
        filtered.append(venue)
    # APR is a reported figure, not a risk-adjusted ranking. Null APRs go last.
    filtered.sort(key=lambda v: (v.get("apr_pct") is None, -decimal_value(v["apr_pct"], "apr_pct") if v.get("apr_pct") is not None else Decimal(0)))
    return filtered[:limit]


def source_metadata(source: str, fetched_at: str, block_number: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": source,
        "fetched_at": fetched_at,
        "chain_id": CHAIN_ID,
        "network": "Ethereum mainnet",
    }
    if block_number is not None:
        result["block_number"] = block_number
    return result
