"""BOLD Guardian MCP server.

Local clients use stdio. Remote deployments use Streamable HTTP and must set
MCP_API_TOKEN plus MCP_ALLOWED_HOSTS before starting the server.
"""
from __future__ import annotations

import argparse
import copy
import hmac
import json
import logging
import os
import threading
import time
import functools
from collections import OrderedDict, defaultdict, deque
from typing import Any

import requests
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from web3 import Web3

try:  # optional: load a local .env for stdio/dev use
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from guardian_core import BRANCHES, GuardianError, LiquityReader, classify_health, env_int, health_caveats, parse_trove_id, redact
from guardian_mcp_logic import (
    YIELD_SOURCE,
    compare_yields,
    normalise_yield_rows,
    simulate_borrowing_cost as calculate_borrowing_cost,
    source_metadata,
    utc_now,
)

LOG = logging.getLogger("bold_guardian.mcp")
CACHE_TTL = env_int("GUARDIAN_MCP_CACHE_TTL_SECONDS", 30, 0)
YIELD_TIMEOUT = env_int("GUARDIAN_MCP_YIELD_TIMEOUT_SECONDS", 20, 3)
CACHE_MAX_ENTRIES = env_int("GUARDIAN_MCP_CACHE_MAX_ENTRIES", 256, 1)
MIN_FORCE_REFRESH_SECONDS = env_int("GUARDIAN_MCP_MIN_FORCE_REFRESH_SECONDS", 5, 0)
RPC_SOURCE = "Liquity V2 Ethereum mainnet contracts via configured ETH_RPC_URL"

mcp = MCPServer(
    "BOLD Guardian",
    instructions=(
        "Read-only Liquity V2 and BOLD data tools. Use returned source, timestamp, chain, "
        "block number and limitations. Do not invent missing data. Position health labels "
        "are Guardian heuristics, not official Liquity states. Borrowing simulations are "
        "simple-interest estimates, not rate recommendations. Trove IDs are 256-bit numbers "
        "and are always passed and returned as strings."
    ),
)

_reader: LiquityReader | None = None
_reader_lock = threading.Lock()
_cache_lock = threading.Lock()
# wallet -> (monotonic time, positions, block, fetched_at ISO string)
_wallet_cache: "OrderedDict[str, tuple[float, list[dict[str, Any]], int | None, str]]" = OrderedDict()
_yield_cache: tuple[float, list[dict[str, Any]], str] | None = None


def get_reader() -> LiquityReader:
    global _reader
    with _reader_lock:
        if _reader is None:
            _reader = LiquityReader()
            _reader.validate_deployments()
        return _reader


def read_wallet(wallet: str, force_refresh: bool = False) -> tuple[list[dict[str, Any]], int | None, bool, str]:
    """Return (positions, block, cache_hit, fetched_at). fetched_at is when the data was really read."""
    if not Web3.is_address(wallet):
        raise ValueError("wallet must be a valid Ethereum address")
    wallet = Web3.to_checksum_address(wallet)
    now = time.monotonic()
    with _cache_lock:
        cached = _wallet_cache.get(wallet)
    if cached:
        age = now - cached[0]
        # force_refresh is honoured only after a short minimum age so it cannot be used to hammer the RPC.
        if age < CACHE_TTL and not (force_refresh and age >= MIN_FORCE_REFRESH_SECONDS):
            return copy.deepcopy(cached[1]), cached[2], True, cached[3]
    reader = get_reader()
    positions = reader.inspect_wallet(wallet)
    serialised = []
    block_numbers = []
    for position in positions:
        item = position.to_dict()
        item["health_label"] = classify_health(position)
        item["health_label_note"] = "Guardian presentation heuristic; not an official Liquity protocol status."
        item["health_caveats"] = health_caveats(position)
        serialised.append(item)
        block_numbers.append(position.block_number)
    block = max(block_numbers) if block_numbers else reader.latest_block()
    fetched_at = utc_now()
    with _cache_lock:
        _wallet_cache[wallet] = (now, serialised, block, fetched_at)
        _wallet_cache.move_to_end(wallet)
        while len(_wallet_cache) > CACHE_MAX_ENTRIES:
            _wallet_cache.popitem(last=False)
    return copy.deepcopy(serialised), block, False, fetched_at


def read_yields(force_refresh: bool = False) -> tuple[list[dict[str, Any]], str, bool]:
    global _yield_cache
    now = time.monotonic()
    with _cache_lock:
        cached = _yield_cache
    if cached:
        age = now - cached[0]
        if age < CACHE_TTL and not (force_refresh and age >= MIN_FORCE_REFRESH_SECONDS):
            return copy.deepcopy(cached[1]), cached[2], True
    response = requests.get(YIELD_SOURCE, timeout=YIELD_TIMEOUT)
    response.raise_for_status()
    rows = normalise_yield_rows(response.json())
    fetched_at = utc_now()
    with _cache_lock:
        _yield_cache = (now, rows, fetched_at)
    return copy.deepcopy(rows), fetched_at, False


def tool(fn):
    """Register an MCP tool whose expected failures (bad input, RPC trouble) reach the model as a
    readable error instead of an opaque crash. Messages are already redacted by guardian_core."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (GuardianError, ValueError, requests.RequestException) as exc:
            raise ToolError(redact(exc, os.getenv("ETH_RPC_URL"))) from None
    return mcp.tool()(wrapper)


@tool
def get_wallet_positions(wallet: str, force_refresh: bool = False) -> dict[str, Any]:
    """Get every currently owned active or Zombie Liquity V2 Trove for an Ethereum wallet."""
    positions, block, cached, fetched_at = read_wallet(wallet, force_refresh)
    return {
        "wallet": Web3.to_checksum_address(wallet),
        "trove_count": len(positions),
        "positions": positions,
        "metadata": source_metadata(RPC_SOURCE, fetched_at, block),
        "cache_hit": cached,
        "limitations": [
            "Only WETH, wstETH and rETH branches configured in this Guardian build are scanned.",
            "An empty list means no active/Zombie Troves were found; it does not prove a liquidation or closure.",
        ],
    }


@tool
def get_trove(branch: str, trove_id: str) -> dict[str, Any]:
    """Get one Trove by branch (WETH, wstETH or rETH) and Trove NFT id (decimal or 0x-hex string)."""
    branch = branch.strip()
    branch_match = next((name for name in BRANCHES if name.casefold() == branch.casefold()), None)
    if branch_match is None:
        raise ValueError(f"Unsupported branch: {branch}. Choose one of {', '.join(BRANCHES)}.")
    parsed_id = parse_trove_id(trove_id)  # validate input before touching the RPC
    position = get_reader().inspect_trove(branch_match, parsed_id)
    result = position.to_dict()
    result["health_label"] = classify_health(position)
    result["health_label_note"] = "Guardian presentation heuristic; not an official Liquity protocol status."
    result["health_caveats"] = health_caveats(position)
    result["metadata"] = source_metadata(RPC_SOURCE, utc_now(), position.block_number)
    return result


@tool
def get_position_health(wallet: str, force_refresh: bool = False) -> dict[str, Any]:
    """Return Trove health metrics and Guardian heuristic labels for all Troves owned by a wallet."""
    positions, block, cached, fetched_at = read_wallet(wallet, force_refresh)
    summaries = []
    for p in positions:
        summaries.append({
            "branch": p["branch"],
            "trove_id": p["trove_id"],
            "status": p["status"],
            "collateral_ratio_pct": p["collateral_ratio_pct"],
            "mcr_pct": p["mcr_pct"],
            "liquidation_price_usd": p["liquidation_price_usd"],
            "distance_to_liquidation_pct": p["distance_to_liquidation_pct"],
            "health_label": p["health_label"],
            "price_is_fallback": p["price_is_fallback"],
            "price_source": p["price_source"],
            "health_caveats": p["health_caveats"],
        })
    return {
        "wallet": Web3.to_checksum_address(wallet),
        "positions": summaries,
        "metadata": source_metadata(RPC_SOURCE, fetched_at, block),
        "cache_hit": cached,
        "health_label_note": "Labels are Guardian heuristics relative to each branch MCR; they are not official Liquity states or financial advice.",
    }


@tool
def get_bold_yields(force_refresh: bool = False) -> dict[str, Any]:
    """Get currently reported BOLD yield venues from Liquity's published venue feed."""
    venues, fetched_at, cached = read_yields(force_refresh)
    return {
        "venue_count": len(venues),
        "venues": venues,
        "metadata": source_metadata(YIELD_SOURCE, fetched_at),
        "cache_hit": cached,
        "limitations": [
            "APR is the source-reported weekly_apr converted from a fraction to percent.",
            "Reported APR is not a guaranteed return and is not independently risk-adjusted.",
            "TVL and venue availability depend on the source feed and may be stale between refreshes.",
        ],
    }


@tool
def compare_yield_venues(asset: str | None = None, min_tvl_usd: float | None = None, limit: int = 25,
                         force_refresh: bool = False) -> dict[str, Any]:
    """Filter and sort supported BOLD yield venues by reported APR; optionally filter by asset and minimum TVL."""
    venues, fetched_at, cached = read_yields(force_refresh)
    selected = compare_yields(venues, asset=asset, min_tvl_usd=min_tvl_usd, limit=limit)
    return {
        "filters": {"asset_contains": asset, "min_tvl_usd": str(min_tvl_usd) if min_tvl_usd is not None else None, "limit": limit},
        "venue_count": len(selected),
        "venues": selected,
        "sort": "reported APR descending; venues with unavailable APR last",
        "metadata": source_metadata(YIELD_SOURCE, fetched_at),
        "cache_hit": cached,
        "warning": "This is not a risk-adjusted ranking. Review liquidity, withdrawal terms, incentives and protocol/contract risk independently.",
    }


@tool
def simulate_borrowing_cost(debt_bold: float, current_rate_pct: float, proposed_rate_pct: float, days: int = 365) -> dict[str, Any]:
    """Estimate borrowing interest at two rates using simple interest and a constant debt balance."""
    return calculate_borrowing_cost(debt_bold, current_rate_pct, proposed_rate_pct, days)


@mcp.resource("liquity://protocol/overview")
def protocol_overview() -> str:
    """Concise overview of Liquity V2 and the supported Guardian data scope."""
    return (
        "Liquity V2 is a decentralised borrowing protocol. Users open Troves backed by supported collateral "
        "and borrow BOLD. Troves are organised by collateral branch. Guardian currently reads Ethereum mainnet "
        "WETH, wstETH and rETH Trove data. This resource is an orientation aid, not a complete protocol specification. "
        "Consult official Liquity documentation for authoritative mechanics and current deployments: https://docs.liquity.org/"
    )


@mcp.resource("liquity://protocol/contracts")
def contract_registry() -> str:
    """Return the branch contract addresses configured in this Guardian release."""
    from guardian_core import COLLATERAL_REGISTRY
    return json.dumps({
        "chain_id": 1,
        "network": "Ethereum mainnet",
        "collateral_registry": COLLATERAL_REGISTRY,
        "branches": {name: dict(cfg) for name, cfg in BRANCHES.items()},
        "source": "Guardian's checked-in deployment map; runtime validation compares TroveManagers against the on-chain CollateralRegistry and cross-checks TroveNFT and BorrowerOperations against each TroveManager.",
    }, indent=2)


@mcp.resource("liquity://guardian/methodology")
def calculation_methodology() -> str:
    """Explain Guardian's position health and borrowing simulation assumptions."""
    return (
        "Position collateral ratio = collateral amount × branch collateral price ÷ entire debt × 100. "
        "Estimated liquidation price = entire debt × branch MCR ÷ collateral amount. Distance to liquidation "
        "is calculated relative to the current branch price. All values for one wallet are read at one pinned "
        "Ethereum block. Guardian health labels are presentation heuristics based on the ratio relative to MCR; "
        "they ignore redemption exposure and branch shutdown thresholds and are not official protocol statuses. "
        "Borrowing simulations use simple interest, constant debt and a 365-day year. They exclude batch "
        "management fees, compounding, rate adjustment costs, protocol constraints and redemption exposure."
    )


@mcp.resource("liquity://guardian/glossary")
def glossary() -> str:
    """Definitions of common Liquity V2 terms used by Guardian."""
    return (
        "BOLD: Liquity's USD-pegged decentralised stablecoin. Trove: a collateralised borrowing position. "
        "Branch: a collateral-specific Liquity market. MCR: minimum collateral ratio required to avoid liquidation. "
        "CR: collateral ratio. Stability Pool: BOLD deposits used to absorb liquidated debt in a branch, receiving "
        "collateral gains under protocol rules. Zombie Trove: a Trove that remains in the system after its debt "
        "falls below the minimum debt threshold and can be reactivated under protocol rules. Batch: a grouping of "
        "Troves managed under shared interest-rate settings."
    )


@mcp.prompt()
def analyse_liquity_wallet(wallet: str) -> str:
    """Guide an agent through a read-only analysis of a wallet's Liquity V2 Troves."""
    return (
        f"Analyse this Ethereum wallet's Liquity V2 positions: {wallet}. Call get_wallet_positions and "
        "get_position_health. Report each branch and Trove separately. Explain collateral ratio, MCR, "
        "liquidation price and distance to liquidation. Flag fallback-price warnings and stale/missing data. "
        "Do not call Guardian heuristic labels official protocol states and do not give transaction instructions."
    )


@mcp.prompt()
def compare_bold_yield_opportunities(asset: str = "BOLD") -> str:
    """Guide an agent through a cautious comparison of supported BOLD yield venues."""
    return (
        f"Compare supported yield opportunities relevant to {asset}. Call get_bold_yields and "
        "compare_yield_venues. Compare reported APR and TVL, clearly identify missing values, and explain that "
        "APR is not risk-adjusted or guaranteed. Do not recommend a venue based on APR alone. Identify what "
        "additional research is needed: liquidity, withdrawal terms, incentives and contract/protocol risk."
    )


# ---------------------------------------------------------------------- HTTP security
_PLACEHOLDER_MARKERS = ("replace-with", "changeme", "change-me", "your-token", "example", "secret-here", "password")
AUTH_FAIL_LIMIT = env_int("MCP_AUTH_FAIL_LIMIT", 10, 1)
AUTH_FAIL_WINDOW = env_int("MCP_AUTH_FAIL_WINDOW_SECONDS", 60, 1)


def validate_token(token: str | None) -> str:
    """Reject missing, short, low-entropy or placeholder bearer tokens."""
    if not token or len(token) < 32:
        raise RuntimeError("Remote HTTP mode requires MCP_API_TOKEN with at least 32 characters.")
    lowered = token.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS) or len(set(token)) < 10:
        raise RuntimeError("MCP_API_TOKEN looks like a placeholder. Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\"")
    return token


class BearerAuthMiddleware:
    """ASGI bearer-token gate. Deny by default: every HTTP path except GET /healthz needs the token.

    Repeated failures from one client are throttled with 429 to blunt token guessing.
    """

    def __init__(self, app, token: str, trust_proxy: bool = False, fail_limit: int = AUTH_FAIL_LIMIT,
                 fail_window: int = AUTH_FAIL_WINDOW):
        self.app = app
        self.token = token.encode("utf-8")
        self.trust_proxy = trust_proxy
        self.fail_limit = fail_limit
        self.fail_window = fail_window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _client(self, scope, headers) -> str:
        if self.trust_proxy:
            forwarded = headers.get(b"x-forwarded-for", b"").decode("latin-1").split(",")[0].strip()
            if forwarded:
                return forwarded
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _blocked(self, client: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._failures[client]
            while hits and now - hits[0] > self.fail_window:
                hits.popleft()
            if not hits:
                self._failures.pop(client, None)
            return len(hits) >= self.fail_limit

    def _record_failure(self, client: str) -> None:
        with self._lock:
            if len(self._failures) > 10000:  # bound memory
                self._failures.clear()
            self._failures[client].append(time.monotonic())

    @staticmethod
    async def _respond(send, status: int, payload: dict, extra: list[tuple[bytes, bytes]] | None = None):
        body = json.dumps(payload).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())] + (extra or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # lifespan etc.
            return
        if scope["type"] == "websocket":  # pragma: no cover
            return
        if scope.get("method") == "GET" and scope.get("path") == "/healthz":
            await self._respond(send, 200, {"status": "ok"})
            return
        headers = dict(scope.get("headers", []))
        client = self._client(scope, headers)
        if self._blocked(client):
            await self._respond(send, 429, {"error": "too_many_failed_attempts"},
                                [(b"retry-after", str(self.fail_window).encode())])
            return
        supplied = headers.get(b"authorization", b"")
        if not hmac.compare_digest(supplied, b"Bearer " + self.token):
            self._record_failure(client)
            await self._respond(send, 401, {"error": "unauthorized"}, [(b"www-authenticate", b"Bearer")])
            return
        await self.app(scope, receive, send)


def build_http_app():
    token = validate_token(os.getenv("MCP_API_TOKEN"))
    allowed_hosts = [x.strip() for x in os.getenv("MCP_ALLOWED_HOSTS", "localhost,localhost:*,127.0.0.1,127.0.0.1:*").split(",") if x.strip()]
    allowed_origins = [x.strip() for x in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if x.strip()]
    security = TransportSecuritySettings(allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)
    trust_proxy = os.getenv("MCP_TRUST_PROXY", "").lower() in {"1", "true", "yes"}
    return BearerAuthMiddleware(mcp.streamable_http_app(transport_security=security), token, trust_proxy=trust_proxy)


def run_http() -> None:
    import uvicorn
    app = build_http_app()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("LOG_LEVEL", "info").lower())


class _RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record), os.getenv("ETH_RPC_URL"), os.getenv("MCP_API_TOKEN"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BOLD Guardian MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))  # stderr; stdout is reserved for stdio MCP
    for _handler in logging.getLogger().handlers:
        _handler.setFormatter(_RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        run_http()
