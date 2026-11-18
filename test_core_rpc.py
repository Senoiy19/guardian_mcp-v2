"""Core reader tests against a local mock JSON-RPC server (no network needed)."""
import http.server
import json
import threading
import time
import unittest
from decimal import Decimal

from guardian_core import (GuardianError, LiquityReader, RpcError, TrovePosition, classify_health,
                           health_caveats, parse_trove_id, redact)

MODE = {"m": "ok"}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        reqs = body if isinstance(body, list) else [body]
        out = []
        for r in reqs:
            if r["method"] == "eth_chainId":
                out.append({"jsonrpc": "2.0", "id": r["id"], "result": "0x1"})
            elif r["method"] == "eth_blockNumber":
                out.append({"jsonrpc": "2.0", "id": r["id"], "result": "0x64"})
            elif MODE["m"] == "revert" and r["method"] == "eth_call":
                out.append({"jsonrpc": "2.0", "id": r["id"], "error": {"code": 3, "message": "execution reverted: ERC721: invalid token ID"}})
            else:
                out.append({"jsonrpc": "2.0", "id": r["id"], "result": hex(123)})
        if MODE["m"] == "429" and not any(r["method"] == "eth_chainId" for r in reqs):
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b"rate limited")
            return
        data = json.dumps(out if isinstance(body, list) else out[0]).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_position(**kw):
    base = dict(owner="0x" + "1" * 40, branch="WETH", trove_id=2**255 + 12345, status="active",
                collateral=Decimal(1), debt_bold=Decimal(1000), annual_interest_rate_pct=Decimal(5),
                collateral_price_usd=Decimal(2000), collateral_ratio_pct=Decimal(200), mcr_pct=Decimal(110),
                liquidation_price_usd=Decimal(1100), distance_to_liquidation_pct=Decimal(45), batch_manager=None,
                price_is_fallback=False, block_number=1)
    base.update(kw)
    return TrovePosition(**base)


class MockRpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/v2/SUPERSECRETAPIKEY123"
        cls.reader = LiquityReader(cls.url, retries=3)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_rpc_url_and_key_never_appear_in_errors(self):
        MODE["m"] = "429"
        with self.assertRaises(RpcError) as ctx:
            self.reader.validate_deployments(force=True)
        self.assertNotIn("SUPERSECRETAPIKEY123", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)  # no chained exception carrying the URL into tracebacks
        MODE["m"] = "ok"

    def test_revert_is_not_retried(self):
        self.reader._validated_at = time.monotonic()  # skip registry validation; it would hit the same mock revert
        MODE["m"] = "revert"
        started = time.time()
        with self.assertRaises(GuardianError) as ctx:
            self.reader.inspect_trove("WETH", 123456789)
        MODE["m"] = "ok"
        self.assertLess(time.time() - started, 1.0)  # previously 1.8s of pointless backoff
        self.assertIn("does not exist", str(ctx.exception))


class PureTests(unittest.TestCase):
    def test_redact(self):
        text = redact("failed for url: https://eth-mainnet.g.alchemy.com/v2/KEY123 and /bot12345:ABC-def/getUpdates", "KEY123")
        self.assertNotIn("KEY123", text)
        self.assertNotIn("ABC-def", text)

    def test_parse_trove_id(self):
        big = 2**255 + 12345
        self.assertEqual(parse_trove_id(str(big)), big)
        self.assertEqual(parse_trove_id(hex(big)), big)
        for bad in ("0", "-1", "abc", True, 2**256, 1.5, None):
            with self.assertRaises(GuardianError):
                parse_trove_id(bad)

    def test_trove_id_serialised_as_string(self):
        d = make_position().to_dict()
        self.assertIsInstance(d["trove_id"], str)
        self.assertEqual(int(d["trove_id"]), 2**255 + 12345)

    def test_health_and_caveats(self):
        self.assertEqual(classify_health(make_position(collateral_ratio_pct=Decimal(109))), "LIQUIDATABLE")
        self.assertEqual(classify_health(make_position(collateral_ratio_pct=Decimal(115))), "RISKY")
        self.assertEqual(classify_health(make_position(collateral_ratio_pct=None)), "UNKNOWN")
        notes = health_caveats(make_position(price_source="last_good_price", price_is_fallback=True))
        self.assertTrue(any("stale" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
