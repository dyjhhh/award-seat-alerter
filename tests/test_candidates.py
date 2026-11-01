from datetime import timedelta
import unittest

from candidates import CandidateError, freshness, from_api, from_email
from fixtures import ALIASES, SENDERS, T0, api_row, email


def parse_email(message):
    return from_email(message, senders=SENDERS, aliases=ALIASES)


class CandidateTests(unittest.TestCase):
    def test_email_and_api_for_the_same_seat_share_one_event_key(self):
        a = from_api(api_row(), aliases=ALIASES)
        b = parse_email(email())
        self.assertNotEqual((a.source, a.source_id, a.alert_group), (b.source, b.source_id, b.alert_group))
        self.assertEqual(a.event_key, b.event_key)

    def test_a_different_cabin_date_or_flight_is_a_different_event(self):
        base = from_api(api_row(), aliases=ALIASES).event_key
        for change in ({"cabin": "economy"}, {"date": "2031-06-13"}, {"flights": ["NW 403"]},
                       {"flights": ["NW 401", "NW 88"]}):
            with self.subTest(change=change):
                self.assertNotEqual(from_api(api_row(**change), aliases=ALIASES).event_key, base)

    def test_points_fees_and_seat_count_do_not_change_identity(self):
        base = from_api(api_row(), aliases=ALIASES).event_key
        moved = from_api(api_row(points=60_000, fees="12.00", seats=1, id="row-9"), aliases=ALIASES)
        self.assertEqual(moved.event_key, base)

    def test_mail_from_another_sender_is_not_an_alert(self):
        self.assertIsNone(parse_email(email(**{"from": "alerts@provider.example.com.lookalike.example.com"})))
        self.assertIsNone(parse_email(email(**{"from": "news@provider.example.com"})))

    def test_non_alert_mail_from_the_sender_is_ignored_but_a_broken_alert_raises(self):
        self.assertIsNone(parse_email(email(body="Your monthly newsletter")))
        with self.assertRaises(CandidateError):
            parse_email(email(fields={"Points": "lots"}))
        with self.assertRaises(CandidateError):
            parse_email(email(message_id=""))

    def test_duplicate_fields_in_one_body_are_refused(self):
        message = email()
        message["body"] += "\nPoints: 1"
        with self.assertRaises(CandidateError):
            parse_email(message)

    def test_unconfigured_programs_and_bad_codes_are_refused(self):
        for change in ({"program": "Unknown Club"}, {"origin": "PA"}, {"destination": "PAV"},
                       {"observed_at": "2031-03-04T02:00:00"}, {"currency": "dollars"}):
            with self.subTest(change=change), self.assertRaises(CandidateError):
                from_api(api_row(**change), aliases=ALIASES)

    def test_a_stay_is_keyed_by_property_room_and_nights(self):
        stay = dict(kind="stay", program="Lantern Stays", property="LAKESIDE-01", room="Deluxe King",
                    check_in="2031-06-12", check_out="2031-06-15", cabin="Suite", rooms=1,
                    points=50_000, id="row-s1")
        for key in ("origin", "destination", "flights", "date"):
            stay.setdefault(key, None)
        row = {k: v for k, v in api_row(**stay).items() if v is not None}
        a = from_api(row, aliases=ALIASES)
        self.assertEqual(a.kind, "stay")
        b = from_api({**row, "check_out": "2031-06-16"}, aliases=ALIASES)
        self.assertNotEqual(a.event_key, b.event_key)

    def test_freshness_uses_provider_observation_time(self):
        c = from_api(api_row(), aliases=ALIASES)
        self.assertEqual(freshness(c, T0, max_age=timedelta(minutes=30)), "fresh")
        self.assertEqual(freshness(c, T0 + timedelta(hours=2), max_age=timedelta(minutes=30)), "stale")
        future = from_api(api_row(observed_at=(T0 + timedelta(minutes=10)).isoformat()), aliases=ALIASES)
        self.assertEqual(freshness(future, T0, max_age=timedelta(minutes=30)), "future")


if __name__ == "__main__":
    unittest.main()
