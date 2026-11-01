from decimal import Decimal
import copy
import unittest

from candidates import from_api
from fixtures import ALIASES, POLICY, api_row
from policy import PolicyError, ValuePolicy


def verdict(config=POLICY, **row):
    return ValuePolicy.from_config(config).evaluate(from_api(api_row(**row), aliases=ALIASES))


class PolicyTests(unittest.TestCase):
    def test_a_score_above_the_threshold_qualifies(self):
        v = verdict()
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.score, Decimal("2.69"))

    def test_a_score_exactly_at_the_threshold_fails(self):
        # 100,000 * 1.75 / 70,000 = 2.50 exactly.
        v = verdict(points=70_000)
        self.assertEqual(v.score, Decimal("2.50"))
        self.assertFalse(v.eligible)
        self.assertEqual(v.reasons, ("score_not_above_threshold",))

    def test_one_point_cheaper_than_the_threshold_qualifies(self):
        self.assertTrue(verdict(points=69_999).eligible)

    def test_each_gate_reports_its_own_reason(self):
        cases = {
            "outside_window": {"date": "2031-07-01"},
            "fees_over_cap": {"fees": "150.01"},
            "fee_currency_mismatch": {"currency": "EUR"},
            "cabin_not_scored": {"cabin": "first"},
        }
        for reason, change in cases.items():
            with self.subTest(reason=reason):
                v = verdict(**change)
                self.assertFalse(v.eligible)
                self.assertIn(reason, v.reasons)

    def test_minimum_inventory_is_a_gate(self):
        config = dict(POLICY, min_quantity=3)
        self.assertIn("not_enough_inventory", verdict(config).reasons)

    def test_an_unknown_balance_is_not_assumed_to_be_enough(self):
        v = verdict(program="Harbor Rewards")
        self.assertIn("balance_unknown", v.reasons)
        config = copy.deepcopy(POLICY)
        del config["balances"]["bank-pool-a"]
        self.assertIn("balance_unknown", verdict(config).reasons)

    def test_funding_counts_every_configured_pool_and_nothing_else(self):
        config = copy.deepcopy(POLICY)
        config["balances"]["bank-pool-a"] = 20_000  # 40,000 + 20,000 < 65,000
        self.assertIn("insufficient_funding", verdict(config).reasons)
        config["funding"]["Northwind Miles"] = []
        self.assertIn("program_not_fundable", verdict(config).reasons)

    def test_a_stay_needs_points_for_every_night(self):
        stay = dict(kind="stay", program="Lantern Stays", property="LAKESIDE-01", check_in="2031-06-12",
                    check_out="2031-06-17", cabin="Economy", points=45_000, origin=None,
                    destination=None, flights=None, date=None)
        row = {k: v for k, v in api_row(**stay).items() if v is not None}
        policy = ValuePolicy.from_config(POLICY)
        v = policy.evaluate(from_api(row, aliases=ALIASES))
        self.assertIn("insufficient_funding", v.reasons)  # 5 nights * 45,000 > 180,000

    def test_malformed_config_raises_instead_of_guessing(self):
        for change in ({"threshold": "high"}, {"baseline_points": 0}, {"baseline_points": True},
                       {"window": ["2031-06-30", "2031-06-01"]}, {"max_fees_cents": -1},
                       {"balances": {"x": -5}}, {"cabin_weights": {}}):
            with self.subTest(change=change), self.assertRaises(PolicyError):
                ValuePolicy.from_config({**POLICY, **change})
        with self.assertRaises(PolicyError):
            ValuePolicy.from_config({k: v for k, v in POLICY.items() if k != "threshold"})


if __name__ == "__main__":
    unittest.main()
