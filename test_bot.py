"""Regression tests for the Telegram bot's alerting, storage and command handling.

Every scenario here reproduces a fault found in the V1 audit and asserts the corrected behaviour.
"""
import gc
import os
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

os.environ["GUARDIAN_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "import.sqlite3")
os.environ.setdefault("GUARDIAN_TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")
import guardian_telegram as g  # noqa: E402
from guardian_store import GuardianStore  # noqa: E402
from test_core_rpc import make_position  # noqa: E402

WALLET = "0x" + "2" * 40


def pos(buffer, cr="120", rate="5", tid=42, **kw):
    return make_position(trove_id=tid, distance_to_liquidation_pct=Decimal(buffer), collateral_ratio_pct=Decimal(cr),
                         annual_interest_rate_pct=Decimal(rate), **kw)


class FakeReader:
    def __init__(self):
        self.p = []
        self.fail = False

    def inspect_wallet(self, wallet):
        if self.fail:
            raise RuntimeError("rpc down")
        return list(self.p)


class BotCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        g.STORE = GuardianStore(Path(self.tmp) / "t.sqlite3", 0)
        g._WALLET_CACHE.clear()
        self.sent = []
        self.fail_for = set()

        def fake_send(chat, text):
            if str(chat) in self.fail_for:
                raise RuntimeError("telegram down")
            self.sent.append((str(chat), text))

        g.send = fake_send
        self.reader = FakeReader()

    def cycle(self):
        self.sent.clear()
        g._WALLET_CACHE.clear()
        time.sleep(1.05)  # snapshot timestamps have 1s resolution
        g.monitor_watches(self.reader, workers=2)
        return [(c, t.split("\n")[0]) for c, t in self.sent]

    def watch(self, chat=1, wallet=WALLET):
        g.STORE.add_watch(chat, wallet, 5)


class AlertTests(BotCase):
    def test_no_spam_while_buffer_drifts(self):
        self.watch()
        results = []
        for b in ("9.0", "8.9", "8.8", "8.7"):
            self.reader.p = [pos(b)]
            results.append(self.cycle())
        self.assertEqual(len(results[0]), 1)           # first entry into RISKY alerts
        self.assertTrue(all(r == [] for r in results[1:]))  # tiny drift does not re-alert

    def test_risk_alert_fires_again_after_recovery(self):
        self.watch()
        self.reader.p = [pos("9.0")]
        self.assertEqual(len(self.cycle()), 1)
        self.reader.p = [pos("30", cr="160")]
        self.assertTrue(any("safer" in t for _, t in self.cycle()))
        self.reader.p = [pos("9.0")]
        self.assertTrue(any("attention" in t for _, t in self.cycle()))  # was permanently suppressed in V1

    def test_slow_bleed_is_detected(self):
        self.watch()
        self.reader.p = [pos("40", cr="180")]
        self.cycle()
        alerts = 0
        for b in range(39, 29, -1):  # lose 1 point per cycle, 10 points total
            self.reader.p = [pos(str(b), cr="150")]
            alerts += sum("deteriorated" in t for _, t in self.cycle())
        self.assertGreaterEqual(alerts, 1)

    def test_two_users_same_wallet_both_alerted(self):
        self.watch(10)
        self.watch(11)
        self.reader.p = [pos("40", cr="180")]
        self.cycle()
        self.reader.p = [pos("30", cr="150")]
        got = {c for c, t in self.cycle() if "deteriorated" in t}
        self.assertEqual(got, {"10", "11"})

    def test_vanished_trove_is_reported_after_confirmation(self):
        self.watch()
        self.reader.p = [pos("5", cr="115")]
        self.cycle()
        self.reader.p = []
        first = self.cycle()
        second = self.cycle()
        self.assertFalse(any("no longer found" in t for _, t in first))   # one miss may be an RPC/index glitch
        self.assertTrue(any("no longer found" in t for _, t in second))

    def test_failed_send_is_retried_not_lost(self):
        self.watch(30)
        self.reader.p = [pos("5", cr="115")]
        self.fail_for = {"30"}
        self.assertEqual(self.cycle(), [])
        self.fail_for = set()
        self.assertTrue(any("attention" in t for _, t in self.cycle()))  # delivered once Telegram recovers

    def test_blocked_subscriber_does_not_stop_others(self):
        g.STORE.subscribe_yields(901)
        g.STORE.subscribe_yields(902)
        base = [{"protocol": "A", "asset": "B", "link": "https://x", "weekly_apr": "0.10", "tvl": "1"}]
        g.fetch_yields = lambda force=False: (base, None, False)
        g.monitor_yield_changes()
        drop = [{"protocol": "A", "asset": "B", "link": "https://x", "weekly_apr": "0.05", "tvl": "1"}]
        g.fetch_yields = lambda force=False: (drop, None, False)
        self.fail_for = {"901"}
        g.monitor_yield_changes()
        self.assertIn("902", [c for c, _ in self.sent])

    def test_permanently_blocked_chat_is_purged(self):
        self.watch(77)

        def blocked(chat, text):
            raise g.TelegramError(403, "Forbidden: bot was blocked by the user")

        g.send = blocked
        self.assertFalse(g.deliver(77, "x"))
        self.assertEqual(len(g.STORE.watches_for_user(77)), 0)

    def test_rate_change_baseline_then_alert(self):
        self.watch()
        self.reader.p = [pos("40", cr="180", rate="5")]
        self.assertEqual(self.cycle(), [])           # baseline, silent
        self.reader.p = [pos("40", cr="180", rate="6")]
        self.assertTrue(any("rate changed" in t for _, t in self.cycle()))
        self.reader.p = [pos("40", cr="180", rate="5")]
        self.assertTrue(any("rate changed" in t for _, t in self.cycle()))  # 6->5 is a new change, not deduped away

    def test_monitoring_failure_is_reported_once_and_recovery_noted(self):
        self.watch()
        self.reader.fail = True
        msgs = []
        for _ in range(4):
            msgs += self.cycle()
        self.assertEqual(sum("degraded" in t for _, t in msgs), 1)
        self.reader.fail = False
        self.reader.p = []
        self.assertTrue(any("recovered" in t for _, t in self.cycle()))

    def test_wallet_read_once_per_cycle_for_many_watchers(self):
        calls = []
        orig = self.reader.inspect_wallet
        self.reader.inspect_wallet = lambda w: (calls.append(w), orig(w))[1]
        for chat in range(5):
            self.watch(chat)
        self.cycle()
        self.assertEqual(len(calls), 1)

    def test_all_due_watches_processed_not_just_twenty(self):
        for chat in range(50):
            self.watch(chat, "0x" + f"{chat:040x}")
        calls = []
        self.reader.inspect_wallet = lambda w: calls.append(w) or []
        self.cycle()
        self.assertEqual(len(calls), 50)


class YieldTests(BotCase):
    def test_apr_drop_measured_against_window_high(self):
        s = g.STORE
        row = lambda apr: [{"protocol": "A", "asset": "B", "link": "https://x", "weekly_apr": apr, "tvl": "1"}]
        self.assertEqual(s.record_yield_rows(row("0.100")), [])
        self.assertEqual(s.record_yield_rows(row("0.090")), [])   # -10%
        self.assertEqual(s.record_yield_rows(row("0.081")), [])   # -19% vs high
        changes = s.record_yield_rows(row("0.070"))               # -30% vs 24h high (each step alone was <20%)
        self.assertEqual([c["kind"] for c in changes], ["apr_drop"])

    def test_unavailable_apr_is_not_shown_as_zero(self):
        import datetime as dt
        g.fetch_yields = lambda force=False: ([{"protocol": "P", "asset": "A", "weekly_apr": None, "tvl": 5},
                                               {"protocol": "Q", "asset": "B", "weekly_apr": "0.03", "tvl": None}],
                                              dt.datetime.now(dt.timezone.utc), False)
        text = g.yield_messages()[0][0]
        self.assertIn("APR: Data unavailable", text)
        self.assertNotIn("$Data unavailable", text)
        self.assertLess(text.index("Q | B"), text.index("P | A"))  # unknown APR sorts last

    def test_envelope_feed_accepted(self):
        from guardian_mcp_logic import extract_rows
        self.assertEqual(len(extract_rows({"venues": [{"protocol": "x"}]})), 1)


class StoreTests(BotCase):
    def test_connections_are_closed(self):
        gc.disable()
        try:
            before = len(os.listdir("/proc/self/fd"))
            for _ in range(200):
                g.STORE.watches_for_user(1)
            after = len(os.listdir("/proc/self/fd"))
        finally:
            gc.enable()
        self.assertLess(after - before, 5)

    def test_prune_and_user_version(self):
        self.assertEqual(g.STORE.prune(30), {"snapshots": 0, "yield_snapshots": 0, "yield_alerts": 0})
        with g.STORE._connect() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_migrates_a_v1_database(self):
        import sqlite3
        path = Path(self.tmp) / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE watches (chat_id TEXT NOT NULL, wallet TEXT NOT NULL, created_at TEXT NOT NULL, last_checked_at TEXT, PRIMARY KEY (chat_id, wallet))")
        conn.execute("INSERT INTO watches VALUES ('1','0xabc','2026-01-01T00:00:00+00:00',NULL)")
        conn.commit(); conn.close()
        store = GuardianStore(path)
        self.assertEqual(len(store.watches_for_user(1)), 1)
        self.assertEqual(store.record_check_failure("1", "0xabc")[0], 1)

    def test_global_watch_cap(self):
        g.STORE.add_watch(1, "0xa", 5, total_limit=1)
        with self.assertRaises(ValueError):
            g.STORE.add_watch(2, "0xb", 5, total_limit=1)

    def test_history_only_for_own_watches(self):
        self.reader.p = [pos("40", cr="180")]
        self.watch(1)
        self.cycle()
        g.handle_history(2, WALLET)
        self.assertIn("watch", self.sent[-1][1])
        g.handle_history(1, WALLET)
        self.assertIn("RECENT POSITION HISTORY", self.sent[-1][1])

    def test_get_does_not_write_snapshots(self):
        self.reader.p = [pos("40", cr="180")]
        g.get_positions(self.reader, WALLET)
        self.assertEqual(g.STORE.latest_snapshots(WALLET), [])

    def test_wallet_cache_is_bounded(self):
        old = g.CACHE_MAX_ENTRIES
        g.CACHE_MAX_ENTRIES = 3
        try:
            for i in range(10):
                g.get_positions(self.reader, f"0x{i:040x}", force=True)
            self.assertLessEqual(len(g._WALLET_CACHE), 3)
        finally:
            g.CACHE_MAX_ENTRIES = old


class CommandTests(BotCase):
    def test_simulate(self):
        g.handle_simulate(1, "/simulate 500000 4 2.5 365")
        self.assertIn("20,000.00 BOLD", self.sent[-1][1])
        self.assertIn("7,500.00 BOLD", self.sent[-1][1])
        g.handle_simulate(1, "/simulate -1 4 2.5")
        self.assertIn("Invalid simulation input", self.sent[-1][1])

    def test_simulate_huge_input_rejected_without_giant_reply(self):
        g.handle_simulate(1, "/simulate 1e5000 4 2.5")
        self.assertIn("Invalid simulation input", self.sent[-1][1])
        self.assertLess(len(self.sent[-1][1]), 500)

    def test_plain_text_and_other_bots_ignored(self):
        original = g.command_allowed
        g.command_allowed = lambda c, cmd: True
        g.BOT_USERNAME = "guardian_bot"
        try:
            g.handle(self.reader, {"chat": {"id": 7}, "text": "hello everyone"})
            g.handle(self.reader, {"chat": {"id": 7}, "text": "/help@SomeOtherBot"})
            self.assertEqual(self.sent, [])
            g.handle(self.reader, {"chat": {"id": 7}, "text": "/help@Guardian_Bot"})
            self.assertEqual(len(self.sent), 1)
        finally:
            g.command_allowed = original
            g.BOT_USERNAME = None

    def test_rate_limit_replies_once_per_window(self):
        g.USER_RATE_LIMIT = 10
        g._LAST_COMMAND.clear(); g._RATE_NOTICE.clear()
        self.assertTrue(g.command_allowed(5, "/get"))
        for _ in range(5):
            self.assertFalse(g.command_allowed(5, "/get"))
        self.assertEqual(len(self.sent), 1)  # not one reply per rejected command

    def test_watch_validates_before_saving(self):
        self.reader.fail = True
        g.handle_watch(self.reader, 1, WALLET)
        self.assertEqual(g.STORE.watches_for_user(1), [])
        self.assertIn("Nothing was saved", self.sent[-1][1])

    def test_status_and_rate(self):
        self.reader.p = [pos("17.5", cr="133.33", rate="3.5", debt_bold=Decimal(150000), collateral=Decimal(100))]
        self.watch(300)
        g.handle_status(self.reader, 300)
        self.assertTrue(any("WATCHED TROVE STATUS" in t and "133.33%" in t for _, t in self.sent))
        g.handle_rate(self.reader, 300)
        self.assertTrue(any("RATE INTELLIGENCE" in t and "5,250.00 BOLD" in t for _, t in self.sent))

    def test_log_formatter_redacts_token_and_rpc(self):
        import logging
        rec = logging.LogRecord("x", logging.ERROR, "f", 1, "Max retries exceeded with url: /bot123456:TEST-TOKEN/getUpdates", None, None)
        self.assertNotIn("TEST-TOKEN", g.RedactingFormatter("%(message)s").format(rec))


if __name__ == "__main__":
    unittest.main()
