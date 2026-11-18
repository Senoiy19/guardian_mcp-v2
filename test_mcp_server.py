"""MCP server tests: schema, cache honesty, auth middleware."""
import asyncio
import json
import os
import time
import unittest

import guardian_mcp_server as s
from test_core_rpc import make_position


class FakeReader:
    def __init__(self):
        self.calls = 0

    def inspect_wallet(self, wallet):
        self.calls += 1
        return [make_position()]

    def latest_block(self):
        return 100


class ServerTests(unittest.TestCase):
    def setUp(self):
        s._wallet_cache.clear()
        s._reader = FakeReader()

    def test_trove_id_is_string_in_schema_and_output(self):
        tools = {t.name: t for t in asyncio.run(s.mcp.list_tools())}
        schema = getattr(tools["get_trove"], "input_schema", None) or tools["get_trove"].inputSchema
        self.assertEqual(schema["properties"]["trove_id"]["type"], "string")
        out = s.get_wallet_positions("0x" + "2" * 40)
        self.assertIsInstance(out["positions"][0]["trove_id"], str)
        self.assertEqual(out["positions"][0]["trove_id"], str(2**255 + 12345))  # exact, no float rounding

    def test_cache_hit_reports_original_fetch_time(self):
        w = "0x" + "2" * 40
        first = s.get_wallet_positions(w)
        time.sleep(1.1)
        second = s.get_wallet_positions(w)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["metadata"]["fetched_at"], second["metadata"]["fetched_at"])
        self.assertEqual(s._reader.calls, 1)

    def test_force_refresh_cannot_hammer_rpc(self):
        w = "0x" + "2" * 40
        s.get_wallet_positions(w)
        for _ in range(5):
            s.get_wallet_positions(w, force_refresh=True)
        self.assertEqual(s._reader.calls, 1)

    def test_cached_result_is_not_shared_mutable(self):
        w = "0x" + "2" * 40
        a = s.get_wallet_positions(w)
        a["positions"][0]["debt_bold"] = "tampered"
        b = s.get_wallet_positions(w)
        self.assertNotEqual(b["positions"][0]["debt_bold"], "tampered")

    def test_cache_bounded(self):
        old = s.CACHE_MAX_ENTRIES
        s.CACHE_MAX_ENTRIES = 3
        try:
            for i in range(10):
                s.get_wallet_positions(f"0x{i + 1:040x}")
            self.assertLessEqual(len(s._wallet_cache), 3)
        finally:
            s.CACHE_MAX_ENTRIES = old

    def test_bad_trove_id_rejected(self):
        with self.assertRaises(Exception):
            s.get_trove("WETH", "not-a-number")


class TokenTests(unittest.TestCase):
    def test_placeholder_and_weak_tokens_rejected(self):
        for bad in (None, "", "short", "replace-with-a-long-random-secret", "a" * 40, "your-token-" + "x1y2z3" * 5, "example-" + "q1w2e3r4t5" * 3):
            with self.assertRaises(RuntimeError):
                s.validate_token(bad)
        self.assertTrue(s.validate_token("Xk3v9QmZ7pLw2RtY8uNb4HcJ6dFgS1aE0oIz5VxC"))


class AuthTests(unittest.TestCase):
    TOKEN = "Xk3v9QmZ7pLw2RtY8uNb4HcJ6dFgS1aE0oIz5VxC"

    def call(self, mw, path="/mcp", method="POST", auth=None, ip="1.2.3.4"):
        headers = [(b"authorization", auth.encode())] if auth else []
        scope = {"type": "http", "path": path, "method": method, "headers": headers, "client": (ip, 1)}
        sent = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "http.request"}

        asyncio.run(mw(scope, receive, send))
        return sent[0]["status"]

    def make(self, **kw):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        return s.BearerAuthMiddleware(app, self.TOKEN, **kw)

    def test_denies_by_default_on_every_path(self):
        mw = self.make()
        for path in ("/mcp", "/mcp/", "/", "/anything", "/MCP"):
            self.assertEqual(self.call(mw, path), 401, path)
        self.assertEqual(self.call(mw, "/mcp", auth="Bearer " + self.TOKEN), 200)
        self.assertEqual(self.call(mw, "/other", method="OPTIONS", auth="Bearer " + self.TOKEN), 200)

    def test_health_endpoint_is_open_but_only_get(self):
        mw = self.make()
        self.assertEqual(self.call(mw, "/healthz", method="GET"), 200)
        self.assertEqual(self.call(mw, "/healthz", method="POST"), 401)

    def test_brute_force_is_throttled(self):
        mw = self.make(fail_limit=5, fail_window=60)
        codes = [self.call(mw, auth="Bearer wrong") for _ in range(8)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5:], [429] * 3)
        self.assertEqual(self.call(mw, auth="Bearer " + self.TOKEN), 429)       # blocked client stays blocked
        self.assertEqual(self.call(mw, auth="Bearer " + self.TOKEN, ip="9.9.9.9"), 200)  # others unaffected


if __name__ == "__main__":
    unittest.main()


class ToolErrorTests(unittest.TestCase):
    def test_expected_failures_surface_as_readable_tool_errors(self):
        s._reader = None
        old = os.environ.pop("ETH_RPC_URL", None)
        try:
            with self.assertRaises(s.ToolError) as ctx:
                s.get_trove("WETH", "123")
            self.assertIn("ETH_RPC_URL", str(ctx.exception))
            with self.assertRaises(s.ToolError):
                s.get_wallet_positions("nope")
        finally:
            if old:
                os.environ["ETH_RPC_URL"] = old
