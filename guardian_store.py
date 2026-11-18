"""SQLite persistence for the Guardian Telegram bot.

Design notes
- One short-lived connection per operation, always closed, WAL mode, busy timeout.
- Schema is versioned with PRAGMA user_version and migrated additively.
- Alert de-duplication is *state based* (see alert_state), never a permanent fingerprint, and a
  state row is only written after the message was actually delivered (callers commit after send).
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

BASE_SQL = """
CREATE TABLE IF NOT EXISTS watches (
    chat_id TEXT NOT NULL,
    wallet TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    PRIMARY KEY (chat_id, wallet)
);
CREATE TABLE IF NOT EXISTS snapshots (
    wallet TEXT NOT NULL,
    branch TEXT NOT NULL,
    trove_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    status TEXT NOT NULL,
    collateral TEXT NOT NULL,
    debt_bold TEXT NOT NULL,
    interest_rate_pct TEXT NOT NULL,
    collateral_price_usd TEXT,
    collateral_ratio_pct TEXT,
    mcr_pct TEXT NOT NULL,
    liquidation_price_usd TEXT,
    distance_to_liquidation_pct TEXT,
    batch_manager TEXT,
    price_is_fallback INTEGER NOT NULL,
    block_number INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_wallet_time ON snapshots(wallet, captured_at DESC);
CREATE TABLE IF NOT EXISTS alerts (
    chat_id TEXT NOT NULL, wallet TEXT NOT NULL, branch TEXT NOT NULL, trove_id TEXT NOT NULL,
    kind TEXT NOT NULL, created_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
    PRIMARY KEY (chat_id, wallet, branch, trove_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS yield_subscriptions (
    chat_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS yield_snapshots (
    venue_key TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    protocol TEXT NOT NULL,
    asset TEXT NOT NULL,
    link TEXT,
    apr_pct TEXT,
    tvl_usd TEXT
);
CREATE INDEX IF NOT EXISTS idx_yield_snapshots_key_time ON yield_snapshots(venue_key, captured_at DESC);
CREATE TABLE IF NOT EXISTS yield_alerts (
    chat_id TEXT NOT NULL, venue_key TEXT NOT NULL, kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(chat_id, venue_key, kind, fingerprint)
);
CREATE TABLE IF NOT EXISTS alert_state (
    chat_id TEXT NOT NULL,
    wallet TEXT NOT NULL,
    branch TEXT NOT NULL,
    trove_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    level TEXT,
    label TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, wallet, branch, trove_id, kind)
);
CREATE TABLE IF NOT EXISTS watch_troves (
    chat_id TEXT NOT NULL,
    wallet TEXT NOT NULL,
    branch TEXT NOT NULL,
    trove_id TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    missing_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, wallet, branch, trove_id)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


class GuardianStore:
    def __init__(self, path: Path, watch_interval: int = 900):
        self.path = Path(path)
        self.watch_interval = watch_interval
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ schema
    def _init(self) -> None:
        with self._connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(BASE_SQL)
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version < 1 or version < SCHEMA_VERSION:
                self._migrate(c)
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _columns(c: sqlite3.Connection, table: str) -> set[str]:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}

    def _migrate(self, c: sqlite3.Connection) -> None:
        cols = self._columns(c, "watches")
        if "fail_count" not in cols:
            c.execute("ALTER TABLE watches ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
        if "degraded_notified" not in cols:
            c.execute("ALTER TABLE watches ADD COLUMN degraded_notified INTEGER NOT NULL DEFAULT 0")

    # ------------------------------------------------------------------ watches
    def add_watch(self, chat_id: int, wallet: str, limit: int, total_limit: int | None = None) -> bool:
        with self._connect() as c:
            if c.execute("SELECT 1 FROM watches WHERE chat_id=? AND wallet=?", (str(chat_id), wallet)).fetchone():
                return False
            count = c.execute("SELECT COUNT(*) FROM watches WHERE chat_id=?", (str(chat_id),)).fetchone()[0]
            if count >= limit:
                raise ValueError(f"watch limit reached ({limit})")
            if total_limit is not None:
                total = c.execute("SELECT COUNT(*) FROM watches").fetchone()[0]
                if total >= total_limit:
                    raise ValueError("Guardian is at its global watch capacity. Please try again later.")
            c.execute("INSERT INTO watches(chat_id,wallet,created_at) VALUES(?,?,?)", (str(chat_id), wallet, utc_now()))
            return True

    def remove_watch(self, chat_id: int, wallet: str) -> bool:
        with self._connect() as c:
            cur = c.execute("DELETE FROM watches WHERE chat_id=? AND wallet=?", (str(chat_id), wallet))
            c.execute("DELETE FROM alert_state WHERE chat_id=? AND wallet=?", (str(chat_id), wallet))
            c.execute("DELETE FROM watch_troves WHERE chat_id=? AND wallet=?", (str(chat_id), wallet))
            return cur.rowcount > 0

    def purge_chat(self, chat_id) -> None:
        """Forget a chat that can no longer receive messages (bot blocked, chat deleted)."""
        cid = str(chat_id)
        with self._connect() as c:
            for table in ("watches", "alert_state", "watch_troves", "yield_subscriptions", "yield_alerts"):
                c.execute(f"DELETE FROM {table} WHERE chat_id=?", (cid,))

    def watches_for_user(self, chat_id: int):
        with self._connect() as c:
            return c.execute("SELECT wallet,created_at,last_checked_at FROM watches WHERE chat_id=? ORDER BY created_at", (str(chat_id),)).fetchall()

    def watched_wallet(self, chat_id: int, wallet: str) -> bool:
        with self._connect() as c:
            return c.execute("SELECT 1 FROM watches WHERE chat_id=? AND wallet=?", (str(chat_id), wallet)).fetchone() is not None

    def due_watches(self, limit: int):
        cutoff = datetime.fromtimestamp(time.time() - self.watch_interval, timezone.utc).isoformat(timespec="seconds")
        with self._connect() as c:
            return c.execute(
                """SELECT chat_id,wallet,last_checked_at FROM watches
                   WHERE last_checked_at IS NULL OR last_checked_at <= ?
                   ORDER BY COALESCE(last_checked_at,'') ASC LIMIT ?""",
                (cutoff, limit),
            ).fetchall()

    def record_check_success(self, chat_id: str, wallet: str) -> bool:
        """Mark a successful check. Returns True if monitoring had been reported as degraded."""
        with self._connect() as c:
            row = c.execute("SELECT degraded_notified FROM watches WHERE chat_id=? AND wallet=?", (chat_id, wallet)).fetchone()
            c.execute("UPDATE watches SET last_checked_at=?, fail_count=0, degraded_notified=0 WHERE chat_id=? AND wallet=?",
                      (utc_now(), chat_id, wallet))
            return bool(row and row[0])

    def record_check_failure(self, chat_id: str, wallet: str) -> tuple[int, bool]:
        """Mark a failed check (still advances last_checked_at so retries respect the interval)."""
        with self._connect() as c:
            c.execute("UPDATE watches SET last_checked_at=?, fail_count=fail_count+1 WHERE chat_id=? AND wallet=?",
                      (utc_now(), chat_id, wallet))
            row = c.execute("SELECT fail_count, degraded_notified FROM watches WHERE chat_id=? AND wallet=?", (chat_id, wallet)).fetchone()
            return (row[0], bool(row[1])) if row else (0, False)

    def set_degraded_notified(self, chat_id: str, wallet: str) -> None:
        with self._connect() as c:
            c.execute("UPDATE watches SET degraded_notified=1 WHERE chat_id=? AND wallet=?", (chat_id, wallet))

    def mark_checked(self, chat_id: str, wallet: str) -> None:  # backwards-compatible helper
        self.record_check_success(chat_id, wallet)

    # ------------------------------------------------------------------ snapshots
    def save_positions(self, wallet: str, positions) -> None:
        now = utc_now()
        with self._connect() as c:
            for p in positions:
                d = p.to_dict()
                c.execute(
                    """INSERT INTO snapshots(wallet,branch,trove_id,captured_at,status,collateral,debt_bold,interest_rate_pct,
                       collateral_price_usd,collateral_ratio_pct,mcr_pct,liquidation_price_usd,distance_to_liquidation_pct,
                       batch_manager,price_is_fallback,block_number) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (wallet, p.branch, str(p.trove_id), now, p.status, d["collateral"], d["debt_bold"],
                     d["annual_interest_rate_pct"], d["collateral_price_usd"], d["collateral_ratio_pct"],
                     d["mcr_pct"], d["liquidation_price_usd"], d["distance_to_liquidation_pct"],
                     d["batch_manager"], int(bool(d["price_is_fallback"])), int(d["block_number"])),
                )

    def latest_snapshots(self, wallet: str, limit: int = 12):
        with self._connect() as c:
            return c.execute(
                """SELECT branch,trove_id,captured_at,status,debt_bold,interest_rate_pct,collateral_ratio_pct,distance_to_liquidation_pct
                   FROM snapshots WHERE wallet=? ORDER BY captured_at DESC LIMIT ?""", (wallet, limit)
            ).fetchall()

    def window_max_distance(self, wallet: str, branch: str, trove_id: str, hours: float) -> Decimal | None:
        """Highest liquidation buffer recorded for a Trove in the last ``hours`` (reference for drop alerts)."""
        with self._connect() as c:
            rows = c.execute(
                """SELECT distance_to_liquidation_pct FROM snapshots
                   WHERE wallet=? AND branch=? AND trove_id=? AND captured_at>=? AND distance_to_liquidation_pct IS NOT NULL""",
                (wallet, branch, str(trove_id), hours_ago(hours)),
            ).fetchall()
        values = [Decimal(r[0]) for r in rows]
        return max(values) if values else None

    # ------------------------------------------------------------------ per-user alert state
    def get_state(self, chat_id: str, wallet: str, branch: str, trove_id: str, kind: str):
        with self._connect() as c:
            return c.execute(
                "SELECT level,label,updated_at FROM alert_state WHERE chat_id=? AND wallet=? AND branch=? AND trove_id=? AND kind=?",
                (str(chat_id), wallet, branch, str(trove_id), kind),
            ).fetchone()

    def set_state(self, chat_id: str, wallet: str, branch: str, trove_id: str, kind: str,
                  level: str | None, label: str | None = None) -> None:
        with self._connect() as c:
            c.execute(
                """INSERT INTO alert_state(chat_id,wallet,branch,trove_id,kind,level,label,updated_at) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(chat_id,wallet,branch,trove_id,kind) DO UPDATE SET level=excluded.level,label=excluded.label,updated_at=excluded.updated_at""",
                (str(chat_id), wallet, branch, str(trove_id), kind, level, label, utc_now()),
            )

    def clear_state(self, chat_id: str, wallet: str, branch: str, trove_id: str, kind: str) -> None:
        with self._connect() as c:
            c.execute("DELETE FROM alert_state WHERE chat_id=? AND wallet=? AND branch=? AND trove_id=? AND kind=?",
                      (str(chat_id), wallet, branch, str(trove_id), kind))

    # ------------------------------------------------------------------ Trove presence tracking
    def known_troves(self, chat_id: str, wallet: str) -> set[tuple[str, str]]:
        with self._connect() as c:
            rows = c.execute("SELECT branch,trove_id FROM watch_troves WHERE chat_id=? AND wallet=?", (str(chat_id), wallet)).fetchall()
        return {(r[0], r[1]) for r in rows}

    def mark_seen(self, chat_id: str, wallet: str, branch: str, trove_id: str) -> None:
        with self._connect() as c:
            c.execute(
                """INSERT INTO watch_troves(chat_id,wallet,branch,trove_id,last_seen_at,missing_count) VALUES(?,?,?,?,?,0)
                   ON CONFLICT(chat_id,wallet,branch,trove_id) DO UPDATE SET last_seen_at=excluded.last_seen_at, missing_count=0""",
                (str(chat_id), wallet, branch, str(trove_id), utc_now()),
            )

    def bump_missing(self, chat_id: str, wallet: str, branch: str, trove_id: str) -> int:
        with self._connect() as c:
            c.execute("UPDATE watch_troves SET missing_count=missing_count+1 WHERE chat_id=? AND wallet=? AND branch=? AND trove_id=?",
                      (str(chat_id), wallet, branch, str(trove_id)))
            row = c.execute("SELECT missing_count FROM watch_troves WHERE chat_id=? AND wallet=? AND branch=? AND trove_id=?",
                            (str(chat_id), wallet, branch, str(trove_id))).fetchone()
            return row[0] if row else 0

    def forget_trove(self, chat_id: str, wallet: str, branch: str, trove_id: str) -> None:
        with self._connect() as c:
            c.execute("DELETE FROM watch_troves WHERE chat_id=? AND wallet=? AND branch=? AND trove_id=?",
                      (str(chat_id), wallet, branch, str(trove_id)))
            c.execute("DELETE FROM alert_state WHERE chat_id=? AND wallet=? AND branch=? AND trove_id=?",
                      (str(chat_id), wallet, branch, str(trove_id)))

    # ------------------------------------------------------------------ yields
    def subscribe_yields(self, chat_id: int) -> bool:
        with self._connect() as c:
            cur = c.execute("INSERT OR IGNORE INTO yield_subscriptions(chat_id,created_at) VALUES(?,?)", (str(chat_id), utc_now()))
            return cur.rowcount > 0

    def unsubscribe_yields(self, chat_id: int) -> bool:
        with self._connect() as c:
            cur = c.execute("DELETE FROM yield_subscriptions WHERE chat_id=?", (str(chat_id),))
            return cur.rowcount > 0

    def yield_subscribers(self):
        with self._connect() as c:
            return [r[0] for r in c.execute("SELECT chat_id FROM yield_subscriptions").fetchall()]

    def record_yield_rows(self, rows: list[dict], drop_threshold_pct: Decimal = Decimal(20),
                          window_hours: float = 24) -> list[dict]:
        """Persist one successful venue snapshot and return detected changes.

        An APR drop is measured against the highest APR seen for the venue in the last
        ``window_hours`` (not just the previous poll), because the source reports a weekly average
        that moves slowly. Invalid/empty responses must be rejected by the caller beforehand.
        """
        changes: list[dict] = []
        window_start = hours_ago(window_hours)
        with self._connect() as c:
            has_baseline = c.execute("SELECT 1 FROM yield_snapshots LIMIT 1").fetchone() is not None
            now = utc_now()
            seen_keys: set[str] = set()
            for row in rows:
                protocol = str(row.get("protocol") or "Data unavailable")
                asset = str(row.get("asset") or "Data unavailable")
                link = str(row.get("link") or "")
                venue_key = f"{protocol.lower().strip()}|{asset.lower().strip()}|{link.lower().strip()}"
                if venue_key in seen_keys:
                    continue
                seen_keys.add(venue_key)
                apr_pct = _dec(row.get("weekly_apr"), scale=Decimal(100), allow_negative=True)
                tvl = _dec(row.get("tvl"))
                known = c.execute("SELECT 1 FROM yield_snapshots WHERE venue_key=? LIMIT 1", (venue_key,)).fetchone()
                if known is None and has_baseline:
                    changes.append({"kind": "new_venue", "venue_key": venue_key, "protocol": protocol, "asset": asset,
                                    "link": link, "apr_pct": apr_pct, "tvl": tvl})
                elif known is not None and apr_pct is not None:
                    prior = c.execute(
                        "SELECT apr_pct FROM yield_snapshots WHERE venue_key=? AND captured_at>=? AND apr_pct IS NOT NULL",
                        (venue_key, window_start)).fetchall()
                    reference = max((Decimal(r[0]) for r in prior), default=None)
                    if reference and reference > 0 and apr_pct <= reference * (Decimal(1) - drop_threshold_pct / Decimal(100)):
                        changes.append({"kind": "apr_drop", "venue_key": venue_key, "protocol": protocol, "asset": asset,
                                        "link": link, "old_apr": reference, "apr_pct": apr_pct, "tvl": tvl})
                c.execute(
                    "INSERT INTO yield_snapshots(venue_key,captured_at,protocol,asset,link,apr_pct,tvl_usd) VALUES(?,?,?,?,?,?,?)",
                    (venue_key, now, protocol, asset, link or None,
                     str(apr_pct) if apr_pct is not None else None, str(tvl) if tvl is not None else None),
                )
        return changes

    def yield_alert_recent(self, chat_id: str, change: dict, cooldown_hours: float) -> bool:
        with self._connect() as c:
            return c.execute(
                "SELECT 1 FROM yield_alerts WHERE chat_id=? AND venue_key=? AND kind=? AND created_at>=? LIMIT 1",
                (str(chat_id), change["venue_key"], change["kind"], hours_ago(cooldown_hours)),
            ).fetchone() is not None

    def record_yield_alert(self, chat_id: str, change: dict) -> None:
        with self._connect() as c:
            c.execute("INSERT OR REPLACE INTO yield_alerts(chat_id,venue_key,kind,fingerprint,created_at) VALUES(?,?,?,?,?)",
                      (str(chat_id), change["venue_key"], change["kind"], f"{change.get('apr_pct')}@{utc_now()}", utc_now()))

    # ------------------------------------------------------------------ housekeeping
    def prune(self, snapshot_days: int = 30) -> dict[str, int]:
        cutoff = hours_ago(snapshot_days * 24)
        with self._connect() as c:
            a = c.execute("DELETE FROM snapshots WHERE captured_at<?", (cutoff,)).rowcount
            b = c.execute("DELETE FROM yield_snapshots WHERE captured_at<?", (cutoff,)).rowcount
            d = c.execute("DELETE FROM yield_alerts WHERE created_at<?", (cutoff,)).rowcount
        return {"snapshots": a, "yield_snapshots": b, "yield_alerts": d}


def _dec(value, scale: Decimal = Decimal(1), allow_negative: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value)) * scale
    except Exception:
        return None
    if not number.is_finite() or (number < 0 and not allow_negative):
        return None
    return number
