from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from decimal import Decimal, getcontext
from typing import Any
from urllib.parse import urlparse

from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError

getcontext().prec = 50
LOG = logging.getLogger("bold_guardian")

# Official Liquity Ethereum mainnet deployment addresses.
# Source: https://docs.liquity.org/v2-documentation/technical-docs-and-audits
COLLATERAL_REGISTRY = Web3.to_checksum_address("0xf949982b91c8c61e952b3ba942cbbfaef5386684")

BRANCHES = {
    "WETH": {
        "index": 0,
        "trove_manager": "0x7bcb64b2c9206a5b699ed43363f6f98d4776cf5a",
        "trove_nft": "0x1a0fc0b843afd9140267d25d4e575cb37a838013",
        "borrower_operations": "0x372abd1810eaf23cb9d941bbe7596dfb2c46bc65",
        "price_feed": "0xcc5f8102eb670c89a4a3c567c13851260303c24f",
    },
    "wstETH": {
        "index": 1,
        "trove_manager": "0xa2895d6a3bf110561dfe4b71ca539d84e1928b22",
        "trove_nft": "0x857aecebf75f1012dc18e15020c97096aea31b04",
        "borrower_operations": "0xa741a32f9dcfe6adba088fd0f97e90742d7d5da3",
        "price_feed": "0xe7aa2ba9e086a379d3beb224098bc634a46e314e",
    },
    "rETH": {
        "index": 2,
        "trove_manager": "0xb2b2abeb5c357a234363ff5d180912d319e3e19e",
        "trove_nft": "0x7ae430e25b67f19b431e1d1dc048a5bcf24c0873",
        "borrower_operations": "0xe8119fc02953b27a1b48d2573855738485a17329",
        "price_feed": "0x34f1e9c7dcc279ec70d3c4488eb2d80fba8b7b2b",
    },
}

for _branch in BRANCHES.values():
    for _key in ("trove_manager", "trove_nft", "borrower_operations", "price_feed"):
        _branch[_key] = Web3.to_checksum_address(_branch[_key])

REGISTRY_ABI = [
    {"name":"totalCollaterals","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint256"}]},
    {"name":"getTroveManager","type":"function","stateMutability":"view","inputs":[{"name":"_index","type":"uint256"}],"outputs":[{"type":"address"}]},
]
TM_ABI = [
    {"name":"getTroveIdsCount","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint256"}]},
    {"name":"getTroveFromTroveIdsArray","type":"function","stateMutability":"view","inputs":[{"name":"_index","type":"uint256"}],"outputs":[{"type":"uint256"}]},
    # Soft cross-checks of the hardcoded auxiliary addresses (see validate_deployments).
    {"name":"troveNFT","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},
    {"name":"borrowerOperations","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},
    {"name":"getLatestTroveData","type":"function","stateMutability":"view","inputs":[{"name":"_troveId","type":"uint256"}],
     "outputs":[{"name":"trove","type":"tuple","components":[
         {"name":"entireDebt","type":"uint256"},
         {"name":"entireColl","type":"uint256"},
         {"name":"redistBoldDebtGain","type":"uint256"},
         {"name":"redistCollGain","type":"uint256"},
         {"name":"accruedInterest","type":"uint256"},
         {"name":"recordedDebt","type":"uint256"},
         {"name":"annualInterestRate","type":"uint256"},
         {"name":"weightedRecordedDebt","type":"uint256"},
         {"name":"accruedBatchManagementFee","type":"uint256"},
         {"name":"lastInterestRateAdjTime","type":"uint256"},
     ]}]},
    {"name":"Troves","type":"function","stateMutability":"view","inputs":[{"name":"_id","type":"uint256"}],
     "outputs":[
         {"name":"debt","type":"uint256"},{"name":"coll","type":"uint256"},{"name":"stake","type":"uint256"},
         {"name":"status","type":"uint8"},{"name":"arrayIndex","type":"uint64"},
         {"name":"lastDebtUpdateTime","type":"uint64"},{"name":"lastInterestRateAdjTime","type":"uint64"},
         {"name":"annualInterestRate","type":"uint256"},{"name":"interestBatchManager","type":"address"},
         {"name":"batchDebtShares","type":"uint256"},
     ]},
]
NFT_ABI = [
    {"name":"ownerOf","type":"function","stateMutability":"view","inputs":[{"name":"tokenId","type":"uint256"}],"outputs":[{"type":"address"}]},
    {"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"name":"owner","type":"address"}],"outputs":[{"type":"uint256"}]},
]
BO_ABI = [
    {"name":"MCR","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint256"}]},
]
PRICE_ABI = [
    {"name":"fetchPrice","type":"function","stateMutability":"nonpayable","inputs":[],"outputs":[{"type":"uint256"},{"type":"bool"}]},
    {"name":"lastGoodPrice","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint256"}]},
]

STATUS = {0: "nonExistent", 1: "active", 2: "closedByOwner", 3: "closedByLiquidation", 4: "zombie"}
WAD = Decimal(10) ** 18
UINT256_MAX = 2**256 - 1

# Price source labels (see LiquityReader._branch_price).
PRICE_ORACLE = "oracle"
PRICE_ORACLE_FAILURE = "oracle_failure_flag"
PRICE_LAST_GOOD = "last_good_price"


def env_int(name: str, default: int, minimum: int = 0) -> int:
    """Read an integer env var; fall back to the default on bad input."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        LOG.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default


_URL_RE = re.compile(r"https?://[^\s'\"<>)]+")
_BOT_PATH_RE = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


def redact(text: object, *secrets: str | None) -> str:
    """Strip RPC URLs/API keys and Telegram bot tokens from text before it is logged or returned."""
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[redacted]")

    def _url(match: re.Match) -> str:
        parsed = urlparse(match.group(0))
        return f"{parsed.scheme}://{parsed.hostname or 'host'}/[redacted]"

    out = _URL_RE.sub(_url, out)
    return _BOT_PATH_RE.sub("/bot[redacted]", out)


class GuardianError(RuntimeError):
    pass


class RpcError(GuardianError):
    pass


class ContractRevert(RpcError):
    """Deterministic contract revert (e.g. ownerOf on a burned NFT). Never retried."""


class DeploymentMismatch(GuardianError):
    pass


@dataclass(frozen=True)
class TrovePosition:
    owner: str
    branch: str
    trove_id: int
    status: str
    collateral: Decimal
    debt_bold: Decimal
    annual_interest_rate_pct: Decimal
    collateral_price_usd: Decimal
    collateral_ratio_pct: Decimal | None
    mcr_pct: Decimal
    liquidation_price_usd: Decimal | None
    distance_to_liquidation_pct: Decimal | None
    batch_manager: str | None
    price_is_fallback: bool
    block_number: int
    price_source: str = PRICE_ORACLE

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Decimal):
                d[k] = str(v)
        # Trove IDs are 256-bit hashes: always serialise as strings so JSON/JS clients keep every digit.
        d["trove_id"] = str(self.trove_id)
        return d


def parse_trove_id(value: Any) -> int:
    """Accept a decimal or 0x-hex string (or int) and return a validated uint256 Trove ID."""
    if isinstance(value, bool):
        raise GuardianError("trove_id must be a decimal or 0x-prefixed hex string.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
        except ValueError:
            raise GuardianError("trove_id must be a decimal or 0x-prefixed hex string.") from None
    else:
        raise GuardianError("trove_id must be a decimal or 0x-prefixed hex string.")
    if not 0 < number <= UINT256_MAX:
        raise GuardianError("trove_id must be a positive integer below 2^256.")
    return number


def _wad(value: int) -> Decimal:
    return Decimal(value) / WAD


def _pct_wad(value: int) -> Decimal:
    return Decimal(value) * Decimal(100) / WAD


class LiquityReader:
    """Read-only Liquity V2 Ethereum mainnet reader.

    It discovers currently active/zombie Troves from each TroveNFT balance and verifies
    ownership on-chain. All reads of one wallet are pinned to a single block so a result
    is internally consistent.
    """

    def __init__(self, rpc_url: str | None = None, retries: int = 3, validation_ttl: int = 300):
        self.rpc_url = rpc_url or os.getenv("ETH_RPC_URL")
        if not self.rpc_url:
            raise GuardianError("ETH_RPC_URL is not set. Add an Ethereum mainnet RPC URL first.")
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 30}))
        self.retries = max(1, retries)
        self.validation_ttl = validation_ttl
        self.max_fallback_scan = env_int("GUARDIAN_MAX_FALLBACK_SCAN", 5000, 1)
        self._validated_at: float | None = None
        if not self._retry(lambda: self.w3.is_connected()):
            raise RpcError("Could not connect to the Ethereum RPC.")
        chain_id = self._retry(lambda: self.w3.eth.chain_id)
        if chain_id != 1:
            raise GuardianError(f"Wrong network: expected Ethereum mainnet chain ID 1, got {chain_id}.")
        self.registry = self.w3.eth.contract(address=COLLATERAL_REGISTRY, abi=REGISTRY_ABI)

    # ------------------------------------------------------------------ plumbing
    def _retry(self, fn):
        """Retry transport failures with backoff. Reverts are deterministic and fail immediately.

        Every error text is redacted and chained with ``from None`` so neither the RPC URL/API key
        nor a traceback containing it can reach logs or MCP clients.
        """
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                return fn()
            except (ContractLogicError, BadFunctionCallOutput) as exc:
                raise ContractRevert(f"Contract call reverted: {redact(exc, self.rpc_url)}") from None
            except Exception as exc:
                last = exc
                if attempt + 1 < self.retries:
                    time.sleep(0.6 * (2 ** attempt))
        raise RpcError(f"RPC call failed after {self.retries} attempts: {redact(last, self.rpc_url)}") from None

    def latest_block(self) -> int:
        return int(self._retry(lambda: self.w3.eth.block_number))

    def _call(self, fn, block: int | None):
        return self._retry(lambda: fn.call(block_identifier=block) if block is not None else fn.call())

    def _contracts(self, branch: str):
        cfg = BRANCHES[branch]
        tm = self.w3.eth.contract(address=cfg["trove_manager"], abi=TM_ABI)
        nft = self.w3.eth.contract(address=cfg["trove_nft"], abi=NFT_ABI)
        bo = self.w3.eth.contract(address=cfg["borrower_operations"], abi=BO_ABI)
        pf = self.w3.eth.contract(address=cfg["price_feed"], abi=PRICE_ABI)
        return tm, nft, bo, pf

    # ------------------------------------------------------------------ validation
    def validate_deployments(self, force: bool = False) -> None:
        """Fail closed if the on-chain registry no longer matches our verified branch map.

        The TroveManager of each branch is checked against the CollateralRegistry. The TroveNFT and
        BorrowerOperations addresses are then cross-checked against the TroveManager's own getters
        (a missing getter is logged, a mismatch is fatal). The result is cached for ``validation_ttl``
        seconds so it is not repeated on every query.
        """
        if not force and self._validated_at and time.monotonic() - self._validated_at < self.validation_ttl:
            return
        count = self._retry(lambda: self.registry.functions.totalCollaterals().call())
        if count < 3:
            raise DeploymentMismatch(f"CollateralRegistry reports only {count} collateral branches.")
        for name, cfg in BRANCHES.items():
            actual = self._retry(lambda c=cfg: self.registry.functions.getTroveManager(c["index"]).call())
            if Web3.to_checksum_address(actual) != cfg["trove_manager"]:
                raise DeploymentMismatch(
                    f"{name} TroveManager mismatch. Expected {cfg['trove_manager']}, registry returned {actual}. "
                    "Refusing to calculate risk from an unverified deployment."
                )
            tm = self.w3.eth.contract(address=cfg["trove_manager"], abi=TM_ABI)
            for label, getter, expected in (
                ("TroveNFT", tm.functions.troveNFT, cfg["trove_nft"]),
                ("BorrowerOperations", tm.functions.borrowerOperations, cfg["borrower_operations"]),
            ):
                try:
                    found = self._retry(lambda g=getter: g().call())
                except RpcError as exc:  # includes ContractRevert: getter not exposed
                    LOG.warning("%s: could not cross-check %s via TroveManager: %s", name, label, exc)
                    continue
                if Web3.to_checksum_address(found) != expected:
                    raise DeploymentMismatch(
                        f"{name} {label} mismatch. Expected {expected}, TroveManager reports {found}."
                    )
        self._validated_at = time.monotonic()

    # ------------------------------------------------------------------ prices
    def _branch_price(self, pf, block: int | None = None) -> tuple[int, str]:
        """Return (price, source).

        source is PRICE_ORACLE for a clean oracle read, PRICE_ORACLE_FAILURE when Liquity itself
        flagged an oracle failure (price is the protocol's fallback), and PRICE_LAST_GOOD when the
        fetchPrice eth_call failed and lastGoodPrice was read instead.
        """
        try:
            price, oracle_failure = self._call(pf.functions.fetchPrice(), block)
            if int(price) <= 0:
                raise ValueError("zero price")
            return int(price), (PRICE_ORACLE_FAILURE if oracle_failure else PRICE_ORACLE)
        except Exception as exc:
            LOG.warning("fetchPrice eth_call failed; using lastGoodPrice: %s", redact(exc, self.rpc_url))
            price = self._call(pf.functions.lastGoodPrice(), block)
            if int(price) <= 0:
                raise GuardianError("Liquity price feed returned no usable price.")
            return int(price), PRICE_LAST_GOOD

    def get_branch_price(self, branch: str) -> tuple[int, bool, int]:
        """Public price read: (raw price, is_fallback, block)."""
        if branch not in BRANCHES:
            raise GuardianError(f"Unsupported branch: {branch}.")
        block = self.latest_block()
        _, _, _, pf = self._contracts(branch)
        price, source = self._branch_price(pf, block)
        return price, source != PRICE_ORACLE, block

    # ------------------------------------------------------------------ discovery
    def _alchemy_incoming_nft_ids(self, wallet: str, nft_address: str) -> set[int]:
        """Use Alchemy's indexed transfer API to find NFT IDs ever received by wallet.

        Current ownership is still verified with ownerOf(), so transfers out, burns,
        stale index data, and historical ownership cannot create false positives.
        """
        host = (urlparse(self.rpc_url).hostname or "").lower()
        if not (host == "alchemy.com" or host.endswith(".alchemy.com")):
            raise GuardianError("Alchemy indexed lookup unavailable on this RPC provider.")

        ids: set[int] = set()
        page_key = None
        while True:
            query = {
                "fromBlock": "0x0",
                "toBlock": "latest",
                "toAddress": wallet,
                "contractAddresses": [nft_address],
                "category": ["erc721"],
                "withMetadata": False,
                "excludeZeroValue": False,
                "maxCount": "0x3e8",
                "order": "asc",
            }
            if page_key:
                query["pageKey"] = page_key
            response = self._retry(
                lambda q=query: self.w3.provider.make_request("alchemy_getAssetTransfers", [q])
            )
            if not isinstance(response, dict) or response.get("error"):
                err = response.get("error") if isinstance(response, dict) else response
                raise GuardianError(f"Alchemy transfer lookup failed: {redact(err, self.rpc_url)}")
            result = response.get("result") or {}
            for transfer in result.get("transfers") or []:
                raw = transfer.get("rawContract") or {}
                token = transfer.get("tokenId") or transfer.get("erc721TokenId") or raw.get("value")
                if token is None:
                    continue
                try:
                    ids.add(int(str(token), 0))
                except (TypeError, ValueError):
                    try:
                        ids.add(int(str(token), 16))
                    except (TypeError, ValueError):
                        LOG.warning("Could not parse NFT token id from Alchemy transfer: %r", token)
            page_key = result.get("pageKey")
            if not page_key:
                break
        return ids

    def _scan_branch_troves(self, wallet: str, branch: str, expected: int, block: int | None) -> list[int]:
        """Slow provider-independent fallback. Stops as soon as every expected Trove is found,
        and refuses to run on branches larger than GUARDIAN_MAX_FALLBACK_SCAN."""
        tm, nft, _, _ = self._contracts(branch)
        count = int(self._call(tm.functions.getTroveIdsCount(), block))
        if count > self.max_fallback_scan:
            raise GuardianError(
                f"{branch} has {count} Troves; the non-indexed scan is capped at {self.max_fallback_scan}. "
                "Use an Alchemy RPC URL for wallet discovery."
            )
        LOG.info("%s: fallback scanning up to %s active/zombie Troves", branch, count)
        found: list[int] = []
        for i in range(count):
            trove_id = int(self._call(tm.functions.getTroveFromTroveIdsArray(i), block))
            try:
                owner = self._call(nft.functions.ownerOf(trove_id), block)
            except ContractRevert:
                continue
            if Web3.to_checksum_address(owner) == wallet:
                found.append(trove_id)
                if len(found) >= expected:
                    break
        return found

    def find_wallet_trove_ids(self, wallet: str, block: int | None = None) -> list[tuple[str, int]]:
        """Find currently owned Troves, verified on-chain.

        balanceOf is the source of truth: zero balance means zero work, and a candidate list that
        cannot account for the full balance triggers the complete (capped) fallback scan rather
        than returning partial data.
        """
        wallet = Web3.to_checksum_address(wallet)
        found: list[tuple[str, int]] = []

        for branch, cfg in BRANCHES.items():
            _, nft, _, _ = self._contracts(branch)
            balance = int(self._call(nft.functions.balanceOf(wallet), block))
            if balance == 0:
                LOG.info("%s: wallet owns no Trove NFTs", branch)
                continue

            verified: list[int] = []
            try:
                candidates = self._alchemy_incoming_nft_ids(wallet, cfg["trove_nft"])
                LOG.info("%s: indexed lookup returned %s candidate Trove NFTs", branch, len(candidates))
                for trove_id in candidates:
                    try:
                        owner = self._call(nft.functions.ownerOf(trove_id), block)
                    except ContractRevert:
                        continue  # burned (closed/liquidated): expected, not an error
                    if Web3.to_checksum_address(owner) == wallet:
                        verified.append(trove_id)
            except GuardianError as exc:
                LOG.warning("%s: fast indexed lookup unavailable: %s", branch, exc)

            if len(verified) != balance:
                LOG.warning("%s: expected %s owned Troves, fast lookup verified %s; using fallback scan", branch, balance, len(verified))
                verified = self._scan_branch_troves(wallet, branch, balance, block)

            found.extend((branch, tid) for tid in verified)

        return found

    # ------------------------------------------------------------------ reads
    def read_position(self, wallet: str, branch: str, trove_id: int, block: int | None = None,
                      ctx: dict | None = None) -> TrovePosition:
        """Read one Trove at a single pinned block. ``ctx`` shares MCR/price across Troves of a branch."""
        wallet = Web3.to_checksum_address(wallet)
        tm, nft, bo, pf = self._contracts(branch)
        if block is None:
            block = self.latest_block()
        if ctx is None:
            ctx = {}

        owner = Web3.to_checksum_address(self._call(nft.functions.ownerOf(trove_id), block))
        if owner != wallet:
            raise GuardianError(f"Trove {trove_id} is no longer owned by {wallet}.")

        latest = self._call(tm.functions.getLatestTroveData(trove_id), block)
        raw = self._call(tm.functions.Troves(trove_id), block)
        if branch not in ctx:
            mcr_raw = int(self._call(bo.functions.MCR(), block))
            price_raw, source = self._branch_price(pf, block)
            ctx[branch] = (mcr_raw, price_raw, source)
        mcr_raw, price_raw, price_source = ctx[branch]

        debt = _wad(int(latest[0]))
        coll = _wad(int(latest[1]))
        rate_pct = _pct_wad(int(latest[6]))
        status = STATUS.get(int(raw[3]), f"unknown({raw[3]})")
        batch = Web3.to_checksum_address(raw[8])
        if int(batch, 16) == 0:
            batch = None

        price = _wad(price_raw)
        mcr = _pct_wad(mcr_raw)

        cr = None
        liq_price = None
        distance = None
        if debt > 0 and coll > 0:
            cr = (coll * price / debt) * Decimal(100)
            liq_price = (debt * (Decimal(mcr_raw) / WAD)) / coll
            if price > 0:
                distance = ((price - liq_price) / price) * Decimal(100)

        return TrovePosition(
            owner=owner, branch=branch, trove_id=trove_id, status=status,
            collateral=coll, debt_bold=debt, annual_interest_rate_pct=rate_pct,
            collateral_price_usd=price, collateral_ratio_pct=cr, mcr_pct=mcr,
            liquidation_price_usd=liq_price, distance_to_liquidation_pct=distance,
            batch_manager=batch, price_is_fallback=price_source != PRICE_ORACLE, block_number=block,
            price_source=price_source,
        )

    def sample_branch_rates(self, branch: str, sample_size: int = 100) -> dict[str, Any]:
        """Read a deterministic, evenly-spaced sample of active/zombie Trove rates.

        This is explicitly a sample, not a complete branch census. Reads are pinned to
        one block so the returned sample is internally consistent.
        """
        if branch not in BRANCHES:
            raise GuardianError(f"Unsupported branch: {branch}. Choose one of {', '.join(BRANCHES)}.")
        if isinstance(sample_size, bool) or not isinstance(sample_size, int) or not 10 <= sample_size <= 250:
            raise GuardianError("sample_size must be an integer between 10 and 250.")
        block = self.latest_block()
        tm, _, _, _ = self._contracts(branch)
        count = int(self._call(tm.functions.getTroveIdsCount(), block))
        if count <= 0:
            return {"branch": branch, "block_number": block, "total_trove_ids": count,
                    "sampled_ids": 0, "active_sample_count": 0, "rates_pct": []}
        n = min(count, sample_size)
        if n == count:
            indices = list(range(count))
        elif n == 1:
            indices = [count - 1]
        else:
            indices = sorted({round(i * (count - 1) / (n - 1)) for i in range(n)})
        rates: list[Decimal] = []
        for index in indices:
            try:
                trove_id = int(self._call(tm.functions.getTroveFromTroveIdsArray(index), block))
                raw = self._call(tm.functions.Troves(trove_id), block)
                status = int(raw[3])
                # Liquity V2 status: 1 = active, 4 = zombie. Closed/nonexistent are excluded.
                if status not in (1, 4):
                    continue
                rates.append(_pct_wad(int(raw[7])))
            except Exception as exc:
                LOG.warning("%s rate sample item %s skipped: %s", branch, index, redact(exc, self.rpc_url))
        return {"branch": branch, "block_number": block, "total_trove_ids": count,
                "sampled_ids": len(indices), "active_sample_count": len(rates),
                "rates_pct": rates}

    def inspect_trove(self, branch: str, trove_id: int) -> TrovePosition:
        """Read one Trove by branch and NFT id, resolving its current owner on-chain."""
        if branch not in BRANCHES:
            raise GuardianError(f"Unsupported branch: {branch}. Choose one of {', '.join(BRANCHES)}.")
        trove_id = parse_trove_id(trove_id)
        self.validate_deployments()
        block = self.latest_block()
        _, nft, _, _ = self._contracts(branch)
        try:
            owner = self._call(nft.functions.ownerOf(trove_id), block)
        except ContractRevert:
            raise GuardianError(
                f"Trove {trove_id} does not exist on the {branch} branch (never opened, closed or liquidated)."
            ) from None
        return self.read_position(Web3.to_checksum_address(owner), branch, trove_id, block)

    def inspect_wallet(self, wallet: str) -> list[TrovePosition]:
        if not Web3.is_address(wallet):
            raise GuardianError("That is not a valid Ethereum address.")
        wallet = Web3.to_checksum_address(wallet)
        self.validate_deployments()
        block = self.latest_block()
        ids = self.find_wallet_trove_ids(wallet, block)
        ctx: dict = {}
        positions = []
        for branch, trove_id in ids:
            try:
                positions.append(self.read_position(wallet, branch, trove_id, block, ctx))
            except (ContractLogicError, GuardianError) as exc:
                LOG.warning("Skipping Trove %s/%s: %s", branch, trove_id, redact(exc, self.rpc_url))
        return positions


def classify_ratio(cr: Decimal | None, mcr: Decimal) -> str:
    """Presentation-only first-pass labels. Not financial advice.

    Thresholds are relative buffers above the branch's actual MCR, not hard-coded CRs.
    """
    if cr is None:
        return "UNKNOWN"
    if cr < mcr:
        return "LIQUIDATABLE"
    buffer_ratio = cr / mcr
    if buffer_ratio < Decimal("1.10"):
        return "RISKY"
    if buffer_ratio < Decimal("1.25"):
        return "SICKLY"
    if buffer_ratio < Decimal("1.50"):
        return "MILDLY HEALTHY"
    return "HEALTHY"


def classify_health(position: TrovePosition) -> str:
    return classify_ratio(position.collateral_ratio_pct, position.mcr_pct)


def health_caveats(position: TrovePosition) -> list[str]:
    """Plain-language reasons the health label should be read with care."""
    notes: list[str] = []
    if position.price_source == PRICE_ORACLE_FAILURE:
        notes.append("Liquity flagged an oracle failure; the price is the protocol's fallback price.")
    elif position.price_source == PRICE_LAST_GOOD:
        notes.append("The live oracle read failed; the last stored Liquity price was used and may be stale.")
    if position.status == "zombie":
        notes.append("Zombie Trove: below the minimum debt and not in the normal redemption/ordering path.")
    if position.batch_manager:
        notes.append("Interest-batch Trove: the rate is set by the batch manager and may carry a management fee.")
    notes.append("Label ignores redemption exposure (low-rate Troves can be redeemed) and branch shutdown thresholds.")
    return notes
