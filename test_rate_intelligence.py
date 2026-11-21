import unittest
from decimal import Decimal

from rate_intelligence import summarise_rate_sample, annual_interest_bold, annual_savings_vs_rate


class RateIntelligenceTests(unittest.TestCase):
    def test_sample_median_and_lower_share(self):
        result = summarise_rate_sample(
            [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5")],
            Decimal("3.5"),
        )
        self.assertEqual(result["sample_count"], 5)
        self.assertEqual(result["median_rate_pct"], Decimal("3"))
        self.assertEqual(result["lower_count"], 3)
        self.assertEqual(result["share_lower_pct"], Decimal("60"))

    def test_empty_sample(self):
        result = summarise_rate_sample([], Decimal("3"))
        self.assertEqual(result["sample_count"], 0)
        self.assertIsNone(result["median_rate_pct"])

    def test_annual_interest(self):
        self.assertEqual(annual_interest_bold("10000", "5"), Decimal("500"))

    def test_savings_vs_median(self):
        self.assertEqual(annual_savings_vs_rate("10000", "5", "3"), Decimal("200"))


if __name__ == "__main__":
    unittest.main()
