from __future__ import annotations

import html
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

import requests
from web3 import Web3

try:  # optional: load a local .env
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from guardian_core import GuardianError, LiquityReader, TrovePosition, classify_ratio, env_int, redact
from guardian_mcp_logic import extract_rows, simulate_borrowing_cost
from guardian_store import GuardianStore, utc_now

LOG = logging.getLogger("bold_guardian.telegram")
TOKEN = os.getenv("GUARDIAN_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
LIQUITY_YIELDS = "https://api.liquity.org/v2/website/bold-venues.json"
MAX_MESSAGE = 4096

# Operational settings (override on Railway without changing code).
DB_PATH = Path(os.getenv("GUARDIAN_DB_PATH", "guardian.sqlite3"))
CACHE_TTL = env_int("GUARDIAN_CACHE_TTL_SECONDS", 60)
CACHE_MAX_ENTRIES = env_int("GUARDIAN_CACHE_MAX_ENTRIES", 512, 1)
USER_RATE_LIMIT = env_int("GUARDIAN_USER_RATE_LIMIT_SECONDS", 10)
WATCH_INTERVAL = env_int("GUARDIAN_WATCH_INTERVAL_SECONDS", 900, 30)
WATCH_LIMIT_PER_USER = env_int("GUARDIAN_WATCH_LIMIT_PER_USER", 5, 1)
MAX_TOTAL_WATCHES = env_int("GUARDIAN_MAX_TOTAL_WATCHES", 2000, 1)
MAX_WATCHES_PER_CYCLE = env_int("GUARDIAN_MAX_WATCHES_PER_CYCLE", 500, 1)
WATCH_WORKERS = env_int("GUARDIAN_WATCH_WORKERS", 4, 1)
HANDLER_WORKERS = env_int("GUARDIAN_HANDLER_WORKERS", 8, 1)
MONITOR_TICK = env_int("GUARDIAN_MONITOR_TICK_SECONDS", 30, 1)
SNAPSHOT_RETENTION_DAYS = env_int("GUARDIAN_SNAPSHOT_RETENTION_DAYS", 30, 1)
YIELD_DROP_THRESHOLD_PCT = Decimal(os.getenv("GUARDIAN_YIELD_DROP_THRESHOLD_PCT", "20"))
YIELD_ALERT_COOLDOWN_HOURS = env_int("GUARDIAN_YIELD_ALERT_COOLDOWN_HOURS", 24, 1)

# Alert tuning (percentage points of liquidation buffer unless stated).
DROP_ALERT_POINTS = Decimal(os.getenv("GUARDIAN_BUFFER_DROP_POINTS", "5"))
DROP_WINDOW_HOURS = env_int("GUARDIAN_BUFFER_WINDOW_HOURS", 24, 1)
RISK_REALERT_POINTS = Decimal(os.getenv("GUARDIAN_RISK_REALERT_POINTS", "2"))
RISK_REMINDER_HOURS = env_int("GUARDIAN_RISK_REMINDER_HOURS", 6, 1)
MISSING_CONFIRM_CYCLES = env_int("GUARDIAN_MISSING_CONFIRM_CYCLES", 2, 1)
DEGRADED_AFTER_FAILURES = env_int("GUARDIAN_DEGRADED_AFTER_FAILURES", 3, 1)

RISKY_LABELS = {"RISKY", "LIQUIDATABLE"}
BOT_USERNAME: str | None = None

START_TEXT = (
    "🛡 <b>BOLD GUARDIAN</b>\n\n"
    "A read-only Liquity V2 position monitor. Guardian checks your Troves, liquidation risk and BOLD yields, and can watch positions for changes.\n\n"
    "<b>Commands</b>\n"
    "<code>/get 0xWallet</code> - inspect all Troves owned by a wallet\n"
    "<code>/yield</code> - current BOLD yield venues\n"
    "<code>/eth</code> - current Liquity WETH collateral price\n"
    "<code>/opportunities 0xWallet</code> - compare borrowing costs with current BOLD yields\n"
    "<code>/watch 0xWallet</code> - watch the wallet for risk changes\n"
    "<code>/stop 0xWallet</code> - stop watching a wallet\n"
    "<code>/watching</code> - show your watched wallets\n"
    "<code>/history 0xWallet</code> - recent history of a wallet you watch\n"
    "<code>/status</code> - show the state of your watched Troves\n"
    "<code>/rate [0xWallet]</code> - analyse borrowing costs for watched Troves\n"
    "<code>/simulate DEBT CURRENT_RATE NEW_RATE [DAYS]</code> - compare borrowing costs\n"
    "<code>/yieldwatch</code> - subscribe to BOLD yield change alerts\n"
    "<code>/yieldstop</code> - stop BOLD yield alerts\n"
    "<code>/help</code> - show this menu\n\n"
    "Guardian never needs your private key or seed phrase."
)


# ---------------------------------------------------------------------- formatting
def fmt_num(value, places=2):
    return "Data unavailable" if value is None else f"{value:,.{places}f}"


def fmt_usd(value, places=2):
    return "Data unavailable" if value is None else f"${value:,.{places}f}"


def fmt_pct(value):
    return "Data unavailable" if value is None else f"{value:,.2f}%"


def risk_label(cr: Decimal | None, mcr: Decimal) -> str:
    """Guardian product labels (shared with the MCP server via guardian_core), not Liquity states."""
    return classify_ratio(cr, mcr)


class RedactingFormatter(logging.Formatter):
    """Removes the Telegram bot token and RPC URL/API key from every log line and traceback."""

    def format(self, record):
        return redact(super().format(record), TOKEN, os.getenv("ETH_RPC_URL"))


# ---------------------------------------------------------------------- state
STORE = GuardianStore(DB_PATH, WATCH_INTERVAL)
_LAST_COMMAND: dict[str, float] = {}
_RATE_NOTICE: dict[str, float] = {}
_RATE_LOCK = threading.Lock()
_WALLET_CACHE: "OrderedDict[str, tuple[float, list[TrovePosition]]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_YIELD_CACHE: tuple[float, list[dict], datetime] | None = None


# ---------------------------------------------------------------------- Telegram transport
class TelegramError(RuntimeError):
    def __init__(self, status: int, description: str):
        super().__init__(f"Telegram HTTP {status}: {description}")
        self.status = status
        self.description = description

    @property
    def permanent(self) -> bool:
        """The chat can never receive messages (bot blocked / chat gone): stop trying."""
        text = self.description.lower()
        return self.status == 403 or "chat not found" in text or "deactivated" in text


def telegram(method, payload):
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(f"{API}/{method}", json=payload, timeout=40)
            data = response.json()
            if response.ok and data.get("ok"):
                return data
            status = response.status_code
            description = str(data.get("description", ""))
            if status == 429:
                wait = int((data.get("parameters") or {}).get("retry_after", 2))
                last_error = TelegramError(status, description)
                time.sleep(min(max(wait, 1), 30))
                continue
            if status in (400, 401, 403, 404):  # not transient: do not retry
                raise TelegramError(status, description)
            last_error = TelegramError(status, description)
        except TelegramError:
            raise
        except Exception as exc:
            last_error = RuntimeError(redact(exc, TOKEN))
        if attempt < 2:
            time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"Telegram request failed: {redact(last_error, TOKEN)}") from None


def send(chat_id, text):
    telegram("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})


def deliver(chat_id, text) -> bool:
    """Send a proactive alert. Never raises. Returns True only if Telegram accepted the message.

    A permanently undeliverable chat (bot blocked etc.) is purged so it stops consuming work.
    """
    try:
        send(chat_id, text)
        return True
    except TelegramError as exc:
        if exc.permanent:
            LOG.info("Chat %s is permanently unreachable (%s); removing its watches and subscriptions", chat_id, exc.description)
            STORE.purge_chat(chat_id)
        else:
            LOG.warning("Could not deliver alert to %s: %s", chat_id, exc)
    except Exception as exc:
        LOG.warning("Could not deliver alert to %s: %s", chat_id, redact(exc, TOKEN))
    return False


# ---------------------------------------------------------------------- rendering
def position_block(position: TrovePosition):
    label = risk_label(position.collateral_ratio_pct, position.mcr_pct)
    lines = [
        f"<b>{html.escape(position.branch)} Trove | {label}</b>",
        f"Trove ID: <code>{position.trove_id}</code>",
        f"Status: {html.escape(position.status)}",
        f"Collateral: {fmt_num(position.collateral, 6)} {html.escape(position.branch)}",
        f"Debt: {fmt_num(position.debt_bold)} BOLD",
        f"Interest rate: {fmt_pct(position.annual_interest_rate_pct)}",
        f"Collateral price: {fmt_usd(position.collateral_price_usd)}",
        f"Collateral ratio: {fmt_pct(position.collateral_ratio_pct)}",
        f"Liquidation threshold (MCR): {fmt_pct(position.mcr_pct)}",
        f"Estimated liquidation price: {fmt_usd(position.liquidation_price_usd)}",
        f"Price buffer to liquidation: {fmt_pct(position.distance_to_liquidation_pct)}",
    ]
    if position.batch_manager:
        lines.append(f"Interest batch manager: <code>{html.escape(position.batch_manager)}</code>")
    source = getattr(position, "price_source", "oracle")
    if source == "last_good_price":
        lines.append("⚠️ <b>Price warning:</b> the live oracle read failed; Liquity's last stored price is shown and may be stale.")
    elif position.price_is_fallback:
        lines.append("⚠️ <b>Price warning:</b> Liquity reported an oracle failure; its fallback price is being used.")
    return "\n".join(lines)


def reports(wallet, positions):
    header = f"🛡 <b>BOLD GUARDIAN</b>\nWallet: <code>{html.escape(wallet)}</code>\nLiquity Troves found: {len(positions)}"
    if not positions:
        return [header + "\n\nNo active or zombie Liquity V2 Troves found for this wallet."]
    output, current = [], header
    for position in positions:
        block = position_block(position)
        candidate = current + "\n\n" + block
        if len(candidate) > MAX_MESSAGE and current != header:
            output.append(current)
            current = header + "\n\n" + block
        else:
            current = candidate
    output.append(current)
    return output


# ---------------------------------------------------------------------- data access
def get_positions(reader: LiquityReader, wallet: str, force: bool = False, save: bool = False) -> tuple[list[TrovePosition], float, bool]:
    """Read a wallet's Troves. Snapshots are saved only when ``save`` is set (watch cycles), so
    arbitrary /get queries cannot grow the database."""
    now = time.monotonic()
    if not force:
        with _CACHE_LOCK:
            cached = _WALLET_CACHE.get(wallet)
        if cached and now - cached[0] < CACHE_TTL:
            return cached[1], now - cached[0], True
    started = time.perf_counter()
    positions = reader.inspect_wallet(wallet)
    elapsed = time.perf_counter() - started
    with _CACHE_LOCK:
        _WALLET_CACHE[wallet] = (time.monotonic(), positions)
        _WALLET_CACHE.move_to_end(wallet)
        while len(_WALLET_CACHE) > CACHE_MAX_ENTRIES:
            _WALLET_CACHE.popitem(last=False)
    if save:
        STORE.save_positions(wallet, positions)
    return positions, elapsed, False


def fetch_yields(force: bool = False):
    global _YIELD_CACHE
    now = time.monotonic()
    cached = _YIELD_CACHE
    if cached and not force and now - cached[0] < CACHE_TTL:
        return cached[1], cached[2], True
    r = requests.get(LIQUITY_YIELDS, timeout=25)
    r.raise_for_status()
    rows = extract_rows(r.json())  # accepts a bare list or a {venues|data|results} envelope, like the MCP server
    if not all(isinstance(row, dict) and (row.get("protocol") or row.get("asset")) for row in rows):
        raise RuntimeError("Liquity yield response failed venue validation")
    checked = datetime.now(timezone.utc)
    _YIELD_CACHE = (now, rows, checked)
    return rows, checked, False


def _apr(row) -> Decimal | None:
    """Reported weekly APR as a fraction, or None when the source gave nothing usable (never 0)."""
    value = row.get("weekly_apr")
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    return number if number.is_finite() else None


def yield_messages(limit=None):
    rows, checked, _ = fetch_yields()
    rows = sorted(rows, key=lambda r: (_apr(r) is None, -(_apr(r) or Decimal(0))))
    if limit:
        rows = rows[:limit]
    blocks = []
    for i, row in enumerate(rows, 1):
        provider = html.escape(str(row.get("protocol") or "Data unavailable"))
        venue = html.escape(str(row.get("asset") or "Data unavailable"))
        link = str(row.get("link") or "")
        title = f'<a href="{html.escape(link, quote=True)}">{provider} | {venue}</a>' if link.startswith(("http://", "https://")) else f"{provider} | {venue}"
        apr = _apr(row)
        apr_text = "Data unavailable" if apr is None else f"{apr * 100:.2f}%"
        tvl = row.get("tvl")
        try:
            tvl_text = f"${Decimal(str(tvl)):,.0f}" if tvl is not None and Decimal(str(tvl)) >= 0 else "Data unavailable"
        except Exception:
            tvl_text = "Data unavailable"
        warning = " ⚠️" if tvl_text == "$0" else ""
        blocks.append(f"{i}. {title}{warning}\n   APR: {apr_text}\n   TVL: {tvl_text}")

    footer = f"\nLast successful check: {checked.strftime('%Y-%m-%d %H:%M UTC')}\nSource: Liquity API"
    messages, current = [], "📊 <b>BOLD YIELD VENUES</b>\n"
    for block in blocks:
        candidate = current + "\n" + block + "\n"
        if len(candidate + footer) > MAX_MESSAGE and current.strip() != "📊 <b>BOLD YIELD VENUES</b>":
            messages.append(current.rstrip())
            current = "📊 <b>BOLD YIELD VENUES (continued)</b>\n\n" + block + "\n"
        else:
            current = candidate
    messages.append(current.rstrip() + footer)
    return messages, rows


def parse_wallet_command(text):
    parts = text.split()
    if len(parts) != 2 or not Web3.is_address(parts[1]):
        return None
    return Web3.to_checksum_address(parts[1])


def command_allowed(chat_id: int, command: str) -> bool:
    key = str(chat_id)
    now = time.monotonic()
    with _RATE_LOCK:
        if len(_LAST_COMMAND) > 10000:  # bound memory: drop entries older than an hour
            for k in [k for k, v in _LAST_COMMAND.items() if now - v > 3600]:
                _LAST_COMMAND.pop(k, None)
                _RATE_NOTICE.pop(k, None)
        previous = _LAST_COMMAND.get(key, 0.0)
        if now - previous < USER_RATE_LIMIT and command not in {"/start", "/help", "/watching"}:
            notify = now - _RATE_NOTICE.get(key, 0.0) >= USER_RATE_LIMIT  # one notice per window, no reply storm
            if notify:
                _RATE_NOTICE[key] = now
            wait = max(1, int(USER_RATE_LIMIT - (now - previous)))
        else:
            _LAST_COMMAND[key] = now
            return True
    if notify:
        send(chat_id, f"⏳ Slow down a little. Try again in {wait}s.")
    return False


# ---------------------------------------------------------------------- command handlers
def handle_get(reader, chat_id, wallet):
    send(chat_id, f"🔎 Checking <code>{wallet[:8]}…{wallet[-6:]}</code>…")
    try:
        positions, elapsed, cached = get_positions(reader, wallet)
        for report in reports(wallet, positions):
            send(chat_id, report)
        LOG.info("/get %s completed for %s via %s", wallet, chat_id, "cache" if cached else f"RPC ({elapsed:.1f}s)")
    except GuardianError as exc:
        LOG.warning("Guardian read failed: %s", redact(exc, TOKEN, os.getenv("ETH_RPC_URL")))
        send(chat_id, "⚠️ Guardian couldn't complete the Liquity check. Try again shortly.")
    except TelegramError:
        raise
    except Exception:
        LOG.exception("Unexpected /get failure")
        send(chat_id, "⚠️ Guardian hit an unexpected error. No position data was returned.")


def handle_opportunities(reader, chat_id, wallet):
    send(chat_id, f"🔎 Checking <code>{wallet[:8]}…{wallet[-6:]}</code>…")
    try:
        positions, _, _ = get_positions(reader, wallet)
        _, rows = yield_messages(limit=5)
        if not positions:
            send(chat_id, "No active or zombie Liquity V2 Troves found for that wallet.")
            return
        loans = []
        for p in positions:
            annual = p.debt_bold * p.annual_interest_rate_pct / Decimal(100)
            loans.append(
                f"• <b>{html.escape(p.branch)} Trove</b>: {p.annual_interest_rate_pct:.2f}% rate | "
                f"~{annual:,.0f} BOLD/year interest | CR {fmt_pct(p.collateral_ratio_pct)}"
            )
        top = []
        for row in rows:
            apr = _apr(row)
            apr_text = "APR unavailable" if apr is None else f"{apr * 100:.2f}%"
            top.append(f"• {html.escape(str(row.get('protocol') or 'Data unavailable'))} | {html.escape(str(row.get('asset') or 'Data unavailable'))}: {apr_text}")
        send(chat_id,
             "💡 <b>GUARDIAN MARKET CONTEXT</b>\n\n"
             "<b>Your borrowing costs</b>\n" + "\n".join(loans) +
             "\n\n<b>Current BOLD yield observations</b>\n" + "\n".join(top) +
             "\n\n⚠️ Yield APR and borrowing rate are not directly comparable returns. "
             "Yield can carry liquidity, smart-contract, incentive and market risks. Guardian does not recommend leverage or a specific rate yet.")
    except TelegramError:
        raise
    except Exception as exc:
        LOG.warning("Opportunity check failed: %s", redact(exc, TOKEN, os.getenv("ETH_RPC_URL")))
        send(chat_id, "⚠️ Couldn't complete the opportunity check right now.")


def watched_positions(reader, chat_id, wallet: str | None = None):
    watches = STORE.watches_for_user(chat_id)
    if wallet:
        if not STORE.watched_wallet(chat_id, wallet):
            raise ValueError("That wallet is not on your watchlist. Use /watch first.")
        wallets = [wallet]
    else:
        wallets = [r["wallet"] for r in watches]
    if not wallets:
        raise ValueError("You are not watching any wallets. Use /watch 0xWallet first.")
    return [(address, get_positions(reader, address, force=True)[0]) for address in wallets]


def handle_status(reader, chat_id, wallet: str | None = None):
    try:
        groups = watched_positions(reader, chat_id, wallet)
        for address, positions in groups:
            refreshed = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            header = f"🛡 <b>WATCHED TROVE STATUS</b>\nWallet: <code>{html.escape(address)}</code>\nActive/zombie Troves found: {len(positions)}\nLast refreshed: {refreshed}"
            if not positions:
                send(chat_id, header + "\n\nNo active or zombie Troves were returned by the latest check. This alone does not establish liquidation or closure.")
                continue
            current = header
            for position in positions:
                block = position_block(position)
                if len(current) + len(block) + 2 > MAX_MESSAGE:
                    send(chat_id, current)
                    current = header + "\n\n" + block
                else:
                    current += "\n\n" + block
            send(chat_id, current)
    except ValueError as exc:
        send(chat_id, f"👀 {html.escape(str(exc))}")
    except TelegramError:
        raise
    except Exception as exc:
        LOG.warning("Watched Trove status failed: %s", redact(exc, TOKEN, os.getenv("ETH_RPC_URL")))
        send(chat_id, "⚠️ Couldn't refresh your watched Trove status. Previous saved history has been preserved.")


def handle_rate(reader, chat_id, wallet: str | None = None):
    try:
        groups = watched_positions(reader, chat_id, wallet)
        for address, positions in groups:
            if not positions:
                send(chat_id, f"No Troves found for <code>{html.escape(address)}</code>.")
                continue
            lines = [f"📈 <b>RATE INTELLIGENCE</b>\nWallet: <code>{html.escape(address)}</code>"]
            for p in positions:
                annual_cost = p.debt_bold * p.annual_interest_rate_pct / Decimal(100)
                lines.extend([
                    f"\n<b>{html.escape(p.branch)} Trove #{str(p.trove_id)[-8:]}</b>",
                    f"Debt: {p.debt_bold:,.2f} BOLD",
                    f"Current annual rate: {p.annual_interest_rate_pct:.3f}%",
                    f"Estimated annual interest: {annual_cost:,.2f} BOLD",
                ])
                if p.batch_manager:
                    lines.append("⚠️ This Trove belongs to an interest batch. The estimate uses the reported Trove rate and excludes any separate batch management fee.")
            lines.append("\nThis is a borrowing-cost estimate, not a rate recommendation. Branch-wide rate ranking and redemption-context analysis are not yet enabled in this release.")
            send(chat_id, "\n".join(lines))
    except ValueError as exc:
        send(chat_id, f"📈 {html.escape(str(exc))}")
    except TelegramError:
        raise
    except Exception as exc:
        LOG.warning("Rate analysis failed: %s", redact(exc, TOKEN, os.getenv("ETH_RPC_URL")))
        send(chat_id, "⚠️ Couldn't complete the rate analysis. No recommendation was generated.")


def handle_simulate(chat_id, text):
    parts = text.split()
    if len(parts) not in (4, 5):
        send(chat_id, "Usage: <code>/simulate DEBT CURRENT_RATE NEW_RATE [DAYS]</code>\nExample: <code>/simulate 500000 4 2.5 365</code>")
        return
    try:
        days = int(parts[4]) if len(parts) == 5 else 365
        result = simulate_borrowing_cost(parts[1], parts[2], parts[3], days)  # same bounded maths as the MCP tool
    except (ArithmeticError, ValueError) as exc:
        send(chat_id, f"⚠️ Invalid simulation input: {html.escape(str(exc))}")
        return
    old_cost = Decimal(result["current_interest_estimate_bold"])
    new_cost = Decimal(result["proposed_interest_estimate_bold"])
    difference = Decimal(result["estimated_savings_bold"])
    outcome = f"Estimated saving: <b>{difference:,.2f} BOLD</b>" if difference >= 0 else f"Estimated additional cost: <b>{abs(difference):,.2f} BOLD</b>"
    send(chat_id,
         "🧮 <b>RATE SIMULATION</b>\n\n"
         f"Debt: {Decimal(result['debt_bold']):,.2f} BOLD\nCurrent rate: {Decimal(result['current_rate_pct']):.3f}%\n"
         f"Proposed rate: {Decimal(result['proposed_rate_pct']):.3f}%\nPeriod: {days} days\n\n"
         f"Current estimated interest: {old_cost:,.2f} BOLD\nProposed estimated interest: {new_cost:,.2f} BOLD\n{outcome}\n\n"
         "Simple-interest estimate using a constant debt balance. It excludes batch management fees, rate-adjustment constraints, compounding effects and redemption exposure. It does not recommend changing your rate.")


def handle_watch(reader, chat_id, wallet):
    if STORE.watched_wallet(chat_id, wallet):
        send(chat_id, "👀 That wallet is already being watched.")
        return
    try:
        # Validate first, so a bad RPC never leaves a meaningless watch behind.
        positions, _, _ = get_positions(reader, wallet, force=True, save=True)
        STORE.add_watch(chat_id, wallet, WATCH_LIMIT_PER_USER, MAX_TOTAL_WATCHES)
        for p in positions:
            STORE.mark_seen(str(chat_id), wallet, p.branch, str(p.trove_id))
    except ValueError as exc:
        send(chat_id, f"⚠️ {html.escape(str(exc))}")
        return
    except TelegramError:
        raise
    except Exception as exc:
        LOG.warning("Watch setup failed: %s", redact(exc, TOKEN, os.getenv("ETH_RPC_URL")))
        send(chat_id, "⚠️ Couldn't start watching that wallet. Nothing was saved.")
        return
    send(chat_id, f"👀 <b>Watching</b> <code>{wallet}</code>\nCurrent Troves: {len(positions)}\n\nI'll check it about every {WATCH_INTERVAL // 60} minutes and alert you on material risk changes, rate changes, or if a Trove stops being found.")


# ---------------------------------------------------------------------- alert evaluation
Alert = tuple[str, Callable[[], None]]


def evaluate_alerts(chat_id: str, wallet: str, p: TrovePosition) -> list[Alert]:
    """Decide which alerts a user should receive for one Trove.

    Returns (message, commit) pairs. ``commit`` records the new alert state and must be called only
    after the message was delivered, so a failed send is retried on the next cycle instead of being
    silently lost. State is per user, so several users watching one wallet never affect each other.
    """
    tid = str(p.trove_id)
    key = (str(chat_id), wallet, p.branch, tid)
    label = risk_label(p.collateral_ratio_pct, p.mcr_pct)
    buffer = p.distance_to_liquidation_pct
    name = html.escape(p.branch)
    out: list[Alert] = []
    risk_alerted = False

    # 1. Risk label -------------------------------------------------------------------------
    state = STORE.get_state(*key, "risk")
    if label in RISKY_LABELS:
        should = state is None
        reason_text = ""
        if state is not None:
            if label == "LIQUIDATABLE" and state["label"] != "LIQUIDATABLE":
                should = True
            elif buffer is not None and state["level"] not in (None, "") and buffer <= Decimal(state["level"]) - RISK_REALERT_POINTS:
                should = True
                reason_text = "\nThe buffer has shrunk further since the last alert."
            else:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["updated_at"])).total_seconds()
                if age >= RISK_REMINDER_HOURS * 3600:
                    should = True
                    reason_text = "\nReminder: this Trove is still in this state."
        if should:
            risk_alerted = True
            level = str(buffer) if buffer is not None else None
            out.append((
                f"🚨 <b>Guardian attention needed</b>\n{name} Trove is <b>{label}</b>.\nLiquidation buffer: {fmt_pct(buffer)}{reason_text}",
                lambda lvl=level, lab=label: STORE.set_state(*key, "risk", lvl, lab),
            ))
    elif state is not None:
        out.append((
            f"✅ <b>Back to safer territory</b>\n{name} Trove is now <b>{label}</b>. Liquidation buffer: {fmt_pct(buffer)}",
            lambda: STORE.clear_state(*key, "risk"),
        ))

    # 2. Buffer deterioration vs the best buffer seen in the window ------------------------------
    if buffer is not None:
        ref = STORE.window_max_distance(wallet, p.branch, tid, DROP_WINDOW_HOURS)
        drop_state = STORE.get_state(*key, "drop")
        if ref is not None and ref - buffer >= DROP_ALERT_POINTS:
            last = Decimal(drop_state["level"]) if drop_state and drop_state["level"] else None
            if last is None or buffer <= last - DROP_ALERT_POINTS:
                if risk_alerted:  # the risk alert already tells the story; just record the level
                    STORE.set_state(*key, "drop", str(buffer))
                else:
                    out.append((
                        f"📉 <b>Liquidation buffer deteriorated</b>\n{name} Trove: {ref:.1f}% → {buffer:.1f}% over the last {DROP_WINDOW_HOURS}h",
                        lambda b=str(buffer): STORE.set_state(*key, "drop", b),
                    ))
        elif drop_state is not None and ref is not None and ref - buffer < DROP_ALERT_POINTS / 2:
            STORE.clear_state(*key, "drop")  # recovered; a future decline can alert again

    # 3. Interest-rate change (baseline is set silently on first sight) -------------------------
    rate_state = STORE.get_state(*key, "rate")
    if rate_state is None or rate_state["level"] in (None, ""):
        STORE.set_state(*key, "rate", str(p.annual_interest_rate_pct))
    else:
        previous = Decimal(rate_state["level"])
        if previous != p.annual_interest_rate_pct:
            out.append((
                f"📈 <b>Borrowing rate changed</b>\n{name} Trove: {previous:.2f}% → {p.annual_interest_rate_pct:.2f}%",
                lambda r=str(p.annual_interest_rate_pct): STORE.set_state(*key, "rate", r),
            ))
    return out


def evaluate_missing(chat_id: str, wallet: str, positions: list[TrovePosition]) -> list[Alert]:
    """Alert when a previously seen Trove is not found for MISSING_CONFIRM_CYCLES consecutive checks.

    Wording stays factual: a missing Trove may be closed, liquidated, transferred, or an RPC/index gap.
    """
    current = {(p.branch, str(p.trove_id)) for p in positions}
    for branch, tid in current:
        STORE.mark_seen(chat_id, wallet, branch, tid)
    out: list[Alert] = []
    for branch, tid in STORE.known_troves(chat_id, wallet) - current:
        if STORE.bump_missing(chat_id, wallet, branch, tid) >= MISSING_CONFIRM_CYCLES:
            out.append((
                f"❓ <b>Trove no longer found</b>\n{html.escape(branch)} Trove <code>#{html.escape(tid[-8:])}</code> was not found in "
                f"{MISSING_CONFIRM_CYCLES} consecutive checks of <code>{html.escape(wallet)}</code>.\n"
                "It may have been closed, liquidated, or transferred to another wallet. Check it on-chain.",
                lambda b=branch, t=tid: STORE.forget_trove(chat_id, wallet, b, t),
            ))
    return out


def process_watch(chat_id: str, wallet: str, positions: list[TrovePosition]) -> None:
    alerts: list[Alert] = []
    for p in positions:
        alerts.extend(evaluate_alerts(chat_id, wallet, p))
    alerts.extend(evaluate_missing(chat_id, wallet, positions))
    for message, commit in alerts:
        if deliver(chat_id, message + "\n\nUse /get to inspect the latest position."):
            commit()  # only after Telegram accepted it
    if STORE.record_check_success(chat_id, wallet):
        deliver(chat_id, f"✅ Monitoring of <code>{html.escape(wallet)}</code> has recovered.")


def handle_watch_failure(chat_id: str, wallet: str) -> None:
    failures, notified = STORE.record_check_failure(chat_id, wallet)
    if failures >= DEGRADED_AFTER_FAILURES and not notified:
        if deliver(chat_id, f"⚠️ <b>Monitoring degraded</b>\nGuardian could not check <code>{html.escape(wallet)}</code> "
                            f"{failures} times in a row. Alerts for it may be delayed until the data source recovers."):
            STORE.set_degraded_notified(chat_id, wallet)


def monitor_watches(reader, workers: int | None = None) -> int:
    """Check every due watch. Each distinct wallet is read once and shared by all its watchers."""
    rows = STORE.due_watches(MAX_WATCHES_PER_CYCLE)
    by_wallet: dict[str, list[str]] = {}
    for row in rows:
        by_wallet.setdefault(row["wallet"], []).append(row["chat_id"])

    def work(item):
        wallet, chats = item
        try:
            positions, _, _ = get_positions(reader, wallet, force=True, save=True)
        except Exception as exc:
            LOG.warning("Watch read failed for %s: %s", wallet, redact(exc, TOKEN, os.getenv("ETH_RPC_URL")))
            for chat in chats:
                handle_watch_failure(chat, wallet)
            return
        for chat in chats:
            try:
                process_watch(chat, wallet, positions)
            except Exception:
                LOG.exception("Watch processing failed for %s / %s", chat, wallet)

    if by_wallet:
        with ThreadPoolExecutor(max_workers=workers or WATCH_WORKERS) as pool:
            list(pool.map(work, by_wallet.items()))
    return len(rows)


def monitor_yield_changes():
    try:
        rows, _, _ = fetch_yields(force=True)
        changes = STORE.record_yield_rows(rows, YIELD_DROP_THRESHOLD_PCT)  # always record: keeps the baseline fresh
    except Exception as exc:
        # Keep last good observations. Never translate an API outage into venue removals.
        LOG.warning("Yield monitoring failed; previous observations retained: %s", redact(exc, TOKEN))
        return
    subscribers = STORE.yield_subscribers()
    for change in changes:
        cooldown = YIELD_ALERT_COOLDOWN_HOURS if change["kind"] == "apr_drop" else 24 * 3650
        if change["kind"] == "new_venue":
            title = "🆕 <b>NEW BOLD YIELD VENUE</b>"
            detail = f"{html.escape(change['protocol'])} | {html.escape(change['asset'])}\nAPR: {fmt_pct(change['apr_pct'])}\nTVL: {fmt_usd(change['tvl'], 0)}"
        else:
            title = "📉 <b>BOLD YIELD DROP</b>"
            detail = (f"{html.escape(change['protocol'])} | {html.escape(change['asset'])}\n"
                      f"APR: {change['old_apr']:.2f}% (24h high) → {change['apr_pct']:.2f}%\n"
                      f"TVL: {fmt_usd(change['tvl'], 0)}")
        link = change.get("link") or ""
        if link.startswith(("https://", "http://")):
            detail += f'\n<a href="{html.escape(link, quote=True)}">Open venue</a>'
        text = title + "\n\n" + detail + "\n\nReview liquidity and venue risks before moving funds."
        for chat_id in subscribers:  # one failing chat never stops the others
            if STORE.yield_alert_recent(chat_id, change, cooldown):
                continue
            if deliver(chat_id, text):
                STORE.record_yield_alert(chat_id, change)


# ---------------------------------------------------------------------- command router
def handle(reader, message):
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if chat_id is None or not text.startswith("/"):
        return  # ignore plain chat; never reply "Unknown command" to ordinary messages
    first = text.split()[0]
    name, _, target = first.partition("@")
    command = name.lower()
    if target and BOT_USERNAME and target.lower() != BOT_USERNAME.lower():
        return  # addressed to a different bot
    if not command_allowed(chat_id, command):
        return

    if command in ("/start", "/help"):
        send(chat_id, START_TEXT)
        return
    if command == "/get":
        wallet = parse_wallet_command(text)
        if wallet is None:
            send(chat_id, "Usage: <code>/get 0xYourEthereumWallet</code>")
            return
        handle_get(reader, chat_id, wallet)
        return
    if command == "/yield":
        try:
            messages, _ = yield_messages()
        except Exception as exc:
            LOG.warning("Yield fetch failed: %s", redact(exc, TOKEN))
            send(chat_id, "⚠️ Couldn't fetch Liquity BOLD yield venues right now.")
            return
        for msg in messages:
            send(chat_id, msg)
        return
    if command == "/eth":
        try:
            price_raw, fallback, block = reader.get_branch_price("WETH")
            price = Decimal(price_raw) / Decimal(10) ** 18
            warning = "\n⚠️ Liquity fallback price is being used." if fallback else ""
        except Exception as exc:
            LOG.warning("ETH price read failed: %s", redact(exc, TOKEN, os.getenv("ETH_RPC_URL")))
            send(chat_id, "⚠️ Couldn't read Liquity's WETH collateral price right now.")
            return
        send(chat_id, f"Ξ <b>LIQUITY WETH PRICE</b>\n\nPrice: <b>${price:,.2f}</b>\nEthereum block: {block}{warning}\n\nSource: Liquity WETH branch price feed")
        return
    if command in ("/opportunities", "/watch", "/history"):
        wallet = parse_wallet_command(text)
        if wallet is None:
            send(chat_id, f"Usage: <code>{command} 0xYourEthereumWallet</code>")
            return
        if command == "/opportunities":
            handle_opportunities(reader, chat_id, wallet)
        elif command == "/watch":
            handle_watch(reader, chat_id, wallet)
        else:
            handle_history(chat_id, wallet)
        return
    if command == "/stop":
        parts = text.split()
        watches = STORE.watches_for_user(chat_id)
        if len(parts) == 1:
            if len(watches) == 1:
                wallet = watches[0]["wallet"]
            elif not watches:
                send(chat_id, "You are not watching any wallets.")
                return
            else:
                send(chat_id, "You have multiple watched wallets. Specify one: <code>/stop 0xWallet</code>")
                return
        else:
            wallet = parse_wallet_command(text)
            if wallet is None:
                send(chat_id, "Usage: <code>/stop 0xYourEthereumWallet</code>")
                return
        send(chat_id, "🛑 " + ("Stopped watching that wallet." if STORE.remove_watch(chat_id, wallet) else "That wallet was not being watched."))
        return
    if command == "/watching":
        watches = STORE.watches_for_user(chat_id)
        if not watches:
            send(chat_id, "👀 You are not watching any wallets.\nUse <code>/watch 0xWallet</code> to start.")
            return
        body = "\n".join(f"• <code>{html.escape(r['wallet'])}</code>" for r in watches)
        send(chat_id, f"👀 <b>WATCHED WALLETS</b>\n\n{body}")
        return
    if command in ("/status", "/rate"):
        parts = text.split()
        wallet = None
        if len(parts) == 2:
            wallet = parse_wallet_command(text)
        if len(parts) > 2 or (len(parts) == 2 and wallet is None):
            send(chat_id, f"Usage: <code>{command} [0xWatchedWallet]</code>")
            return
        (handle_status if command == "/status" else handle_rate)(reader, chat_id, wallet)
        return
    if command == "/simulate":
        handle_simulate(chat_id, text)
        return
    if command == "/yieldwatch":
        send(chat_id, "🔔 " + ("You are now subscribed to BOLD yield venue alerts. I'll notify you about newly detected venues and significant APR drops." if STORE.subscribe_yields(chat_id) else "You are already subscribed to BOLD yield alerts."))
        return
    if command == "/yieldstop":
        send(chat_id, "🔕 " + ("BOLD yield alerts have been stopped." if STORE.unsubscribe_yields(chat_id) else "You were not subscribed to BOLD yield alerts."))
        return
    send(chat_id, "Unknown command. Send /help to see Guardian commands.")


def handle_history(chat_id, wallet):
    """History is shown only for wallets on the caller's own watchlist, so users cannot read
    what other people have queried."""
    if not STORE.watched_wallet(chat_id, wallet):
        send(chat_id, "History is available for wallets you watch. Use <code>/watch 0xWallet</code> first.")
        return
    rows = STORE.latest_snapshots(wallet, 12)
    if not rows:
        send(chat_id, "No saved history for that wallet yet. The first snapshot is taken at the next check.")
        return
    lines = []
    for r in rows:
        cr = r["collateral_ratio_pct"] or "n/a"
        buf = r["distance_to_liquidation_pct"] or "n/a"
        lines.append(f"{r['captured_at']} | {r['branch']} #{r['trove_id'][-8:]} | CR {cr}% | buffer {buf}% | rate {r['interest_rate_pct']}%")
    send(chat_id, "📈 <b>RECENT POSITION HISTORY</b>\n\n<code>" + html.escape("\n".join(lines)) + "</code>")


def safe_handle(reader, message):
    """Run one update; a failure (including a failed reply) never affects other updates."""
    try:
        handle(reader, message)
    except TelegramError as exc:
        if exc.permanent:
            chat_id = message.get("chat", {}).get("id")
            if chat_id is not None:
                STORE.purge_chat(chat_id)
        LOG.warning("Reply failed: %s", exc)
    except Exception as exc:
        LOG.warning("Update handling failed: %s", redact(exc, TOKEN, os.getenv("ETH_RPC_URL")))


# ---------------------------------------------------------------------- runtime
def monitor_loop(reader, stop: threading.Event):
    """Background thread: watch checks every tick, yield checks and pruning on their own timers."""
    last_yield = 0.0
    last_prune = 0.0
    while not stop.is_set():
        try:
            monitor_watches(reader)
        except Exception as exc:
            LOG.warning("Watch monitor cycle failed: %s", redact(exc, TOKEN, os.getenv("ETH_RPC_URL")))
        if time.monotonic() - last_yield >= WATCH_INTERVAL:
            monitor_yield_changes()
            last_yield = time.monotonic()
        if time.monotonic() - last_prune >= 86400:
            try:
                LOG.info("Pruned old rows: %s", STORE.prune(SNAPSHOT_RETENTION_DAYS))
            except Exception as exc:
                LOG.warning("Prune failed: %s", exc)
            last_prune = time.monotonic()
        stop.wait(MONITOR_TICK)


def build_reader(attempts: int = 5) -> LiquityReader:
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            reader = LiquityReader()
            reader.validate_deployments()
            return reader
        except GuardianError as exc:
            if attempt == attempts:
                raise SystemExit(f"ERROR: could not initialise Liquity reader: {redact(exc, os.getenv('ETH_RPC_URL'))}")
            LOG.warning("Reader init failed (%s/%s): %s", attempt, attempts, redact(exc, os.getenv("ETH_RPC_URL")))
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise SystemExit("unreachable")


def main():
    global BOT_USERNAME
    if not TOKEN:
        raise SystemExit("ERROR: GUARDIAN_TELEGRAM_BOT_TOKEN is not set.")
    if not os.getenv("ETH_RPC_URL"):
        raise SystemExit("ERROR: ETH_RPC_URL is not set.")

    logging.basicConfig(level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s"))
    reader = build_reader()
    me = telegram("getMe", {})
    BOT_USERNAME = me["result"].get("username")
    telegram("setMyCommands", {"commands": [
        {"command": "get", "description": "Inspect a Liquity V2 wallet"},
        {"command": "yield", "description": "Show current BOLD yield venues"},
        {"command": "eth", "description": "Show Liquity WETH price"},
        {"command": "opportunities", "description": "Show loan and yield market context"},
        {"command": "watch", "description": "Watch a wallet for risk changes"},
        {"command": "stop", "description": "Stop watching a wallet"},
        {"command": "rate", "description": "Analyse borrowing costs for watched Troves"},
        {"command": "simulate", "description": "Simulate alternative borrowing rates"},
        {"command": "yieldwatch", "description": "Subscribe to BOLD yield change alerts"},
        {"command": "yieldstop", "description": "Stop BOLD yield alerts"},
        {"command": "watching", "description": "Show watched wallets"},
        {"command": "history", "description": "Show history of a watched wallet"},
        {"command": "status", "description": "Show the state of watched Troves"},
        {"command": "help", "description": "Show Guardian commands"},
    ]})
    LOG.info("Connected to Telegram as @%s. Public multi-user mode; chat-ID authorization is disabled.", BOT_USERNAME or "unknown")

    stop = threading.Event()
    threading.Thread(target=monitor_loop, args=(reader, stop), name="guardian-monitor", daemon=True).start()
    pool = ThreadPoolExecutor(max_workers=HANDLER_WORKERS, thread_name_prefix="guardian-cmd")
    offset = None
    try:
        while True:
            payload = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            try:
                result = telegram("getUpdates", payload)
            except Exception as exc:
                LOG.error("Polling error: %s", redact(exc, TOKEN))
                time.sleep(3)
                continue
            for update in result.get("result", []):
                offset = int(update["update_id"]) + 1
                if update.get("message"):
                    pool.submit(safe_handle, reader, update["message"])
    except KeyboardInterrupt:
        LOG.info("Stopped.")
    finally:
        stop.set()
        pool.shutdown(wait=False)


if __name__ == "__main__":
    main()
