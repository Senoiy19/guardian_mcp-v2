from decimal import Decimal
import unittest

from guardian_mcp_logic import compare_yields, normalise_yield_rows, simulate_borrowing_cost


class SimulationTests(unittest.TestCase):
    def test_one_year_costs(self):
        result = simulate_borrowing_cost("500000", "4", "2.5", 365)
        self.assertEqual(Decimal(result["current_interest_estimate_bold"]), Decimal("20000"))
        self.assertEqual(Decimal(result["proposed_interest_estimate_bold"]), Decimal("12500.0"))
        self.assertEqual(Decimal(result["estimated_savings_bold"]), Decimal("7500.0"))

    def test_partial_year(self):
        result = simulate_borrowing_cost(100000, 5, 5, 182)
        self.assertAlmostEqual(float(result["current_interest_estimate_bold"]), 2493.1506849, places=5)

    def test_invalid_inputs(self):
        for args in [(0, 4, 3, 365), (100, -1, 3, 365), (100, 4, 3, 0), (100, 4, 3, 3651)]:
            with self.assertRaises(ValueError):
                simulate_borrowing_cost(*args)


class BoundsTests(unittest.TestCase):
    def test_absurd_inputs_rejected(self):
        for args in [("1e5000", 4, 3, 365), (100, 5000, 3, 365), (100, 4, 3, 365.5)]:
            with self.assertRaises(ValueError):
                simulate_borrowing_cost(*args)


class YieldTests(unittest.TestCase):
    def test_fraction_apr_to_percent(self):
        rows = normalise_yield_rows([
            {"protocol": "Curve", "asset": "BOLD/USDC", "weekly_apr": "0.125", "tvl": "1000000", "link": "https://example.com"}
        ])
        self.assertEqual(rows[0]["apr_pct"], "12.500")
        self.assertEqual(rows[0]["tvl_usd"], "1000000")

    def test_bad_payload_rejected(self):
        with self.assertRaises(ValueError):
            normalise_yield_rows([])
        with self.assertRaises(ValueError):
            normalise_yield_rows([{"protocol": "x"}])

    def test_comparison_filters_and_sorts(self):
        rows = normalise_yield_rows([
            {"protocol": "A", "asset": "BOLD", "weekly_apr": "0.05", "tvl": "500"},
            {"protocol": "B", "asset": "BOLD", "weekly_apr": "0.10", "tvl": "2000"},
            {"protocol": "C", "asset": "ETH", "weekly_apr": "0.50", "tvl": "9000"},
        ])
        result = compare_yields(rows, asset="BOLD", min_tvl_usd=1000)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["protocol"], "B")


if __name__ == "__main__":
    unittest.main()
