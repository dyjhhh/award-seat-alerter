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

    def test_balances_in_the_config_are_refused_not_silently_ignored(self):
        for key in ("balances", "funding"):
            with self.subTest(key=key), self.assertRaises(PolicyError):
                ValuePolicy.from_config({**POLICY, key: {}})

    def test_a_program_with_no_known_balance_still_alerts(self):
        # The earlier version dropped this as balance_unknown.
        for program in ("Northwind Miles", "Harbor Rewards"):
            with self.subTest(program=program):
                v = verdict(program=program)
                self.assertTrue(v.eligible, v.reasons)
                self.assertEqual(v.labels, ())

    def test_a_stay_is_scored_per_night_with_no_balance_check(self):
        stay = dict(kind="stay", program="Lantern Stays", property="LAKESIDE-01", check_in="2031-06-12",
                    check_out="2031-06-17", cabin="Economy", points=38_000, origin=None,
                    destination=None, flights=None, date=None)
        row = {k: v for k, v in api_row(**stay).items() if v is not None}
        v = ValuePolicy.from_config(POLICY).evaluate(from_api(row, aliases=ALIASES))
        self.assertTrue(v.eligible, v.reasons)  # 5 nights * 38,000 is the person's call, not a gate

    def test_unstated_fields_alert_with_labels_instead_of_dropping(self):
        v = verdict(flights=["NW 401", "NW 88"], seats=0, currency=None)
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.labels, ("fee_currency_unconfirmed", "seats_unconfirmed",
                                    "connection_unconfirmed"))

    def test_a_known_value_that_fails_a_gate_still_drops(self):
        cases = {
            "connection_too_long": {"flights": ["NW 401", "NW 88"], "connection_minutes": 361},
            "not_enough_inventory": {"seats": 1},
            "fee_currency_mismatch": {"currency": "EUR"},
            "fees_over_cap": {"currency": None, "fees": "150.01"},
        }
        config = dict(POLICY, min_quantity=2)
        for reason, change in cases.items():
            with self.subTest(reason=reason):
                v = verdict(config, **change)
                self.assertFalse(v.eligible)
                self.assertIn(reason, v.reasons)
        self.assertTrue(verdict(config, flights=["NW 401", "NW 88"], connection_minutes=360).eligible)

    def test_malformed_config_raises_instead_of_guessing(self):
        for change in ({"threshold": "high"}, {"baseline_points": 0}, {"baseline_points": True},
                       {"window": ["2031-06-30", "2031-06-01"]}, {"max_fees_cents": -1},
                       {"balances": {"x": -5}}, {"cabin_weights": {}},
                       {"max_connection_minutes": -1}, {"max_connection_minutes": "6h"}):
            with self.subTest(change=change), self.assertRaises(PolicyError):
                ValuePolicy.from_config({**POLICY, **change})
        with self.assertRaises(PolicyError):
            ValuePolicy.from_config({k: v for k, v in POLICY.items() if k != "threshold"})


if __name__ == "__main__":
    unittest.main()
