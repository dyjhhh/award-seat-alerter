import unittest

from cabins import Segment, cabin_verdict, long_haul_is_premium, segments_from_legs
from fixtures import ZONES, feeder_then_long_haul, leg


def verdict(legs, fare="Business", disclosed=True):
    return cabin_verdict(fare, segments_from_legs(legs, ZONES), mixed_disclosed=disclosed)


class SegmentTests(unittest.TestCase):
    def test_known_zones_give_segment_lengths(self):
        segments = segments_from_legs(feeder_then_long_haul(), ZONES)
        self.assertEqual([s.minutes for s in segments], [155, 805])

    def test_a_hub_with_an_unknown_time_zone_leaves_its_segments_untimed(self):
        segments = segments_from_legs(feeder_then_long_haul(hub="QZH"), ZONES)
        self.assertEqual([s.minutes for s in segments], [None, None])

    def test_legs_that_do_not_connect_or_lack_a_cabin_are_unknown(self):
        broken = feeder_then_long_haul()
        broken[1]["origin"] = "QZH"
        self.assertIsNone(segments_from_legs(broken, ZONES))
        self.assertIsNone(segments_from_legs(feeder_then_long_haul(long_haul=""), ZONES))
        self.assertIsNone(segments_from_legs([], ZONES))


class LongHaulTests(unittest.TestCase):
    def test_a_short_premium_feeder_with_a_timed_economy_long_haul_is_dropped(self):
        v = verdict(feeder_then_long_haul())
        self.assertFalse(v.eligible)
        self.assertEqual(v.reason, "long_haul_not_premium")
        self.assertEqual(v.detail, "PAV-KRN Business · KRN-LMB Economy")

    def test_an_economy_feeder_with_a_premium_long_haul_alerts_without_labels(self):
        v = verdict(feeder_then_long_haul(feeder="Economy", long_haul="Business"))
        self.assertTrue(v.eligible)
        self.assertEqual((v.reason, v.labels), ("premium_long_haul", ()))

    def test_an_unknown_hub_in_a_mix_alerts_as_check_needed_with_every_cabin(self):
        v = verdict(feeder_then_long_haul(hub="QZH"))
        self.assertTrue(v.eligible, v.reason)
        self.assertEqual(v.reason, "long_haul_unverified")
        self.assertEqual(v.labels, ("long_haul_cabin_unconfirmed",))
        self.assertEqual(v.detail, "PAV-QZH Business · QZH-LMB Economy")

    def test_an_unknown_hub_does_not_blur_a_long_haul_that_is_provable(self):
        # The longest segment is untimed or the longest timed one; when those agree, it is known.
        economy = [leg("PAV", "KRN", "Business", "2031-06-12T08:00:00", "2031-06-12T10:00:00"),
                   leg("KRN", "LMB", "Economy", "2031-06-12T13:40:00", "2031-06-12T19:05:00"),
                   leg("LMB", "QZH", "Economy", "2031-06-13T08:00:00", "2031-06-13T09:00:00")]
        self.assertEqual(verdict(economy).reason, "long_haul_not_premium")
        premium = [dict(x, cabin="Business") for x in economy[1:]]
        premium.insert(0, dict(economy[0], cabin="Economy"))
        self.assertEqual(verdict(premium).reason, "premium_long_haul")

    def test_two_longest_segments_with_different_cabins_are_not_guessed(self):
        tied = (Segment("PAV", "KRN", "Business", 300), Segment("KRN", "LMB", "Economy", 300))
        self.assertIsNone(long_haul_is_premium(tied))
        self.assertEqual(cabin_verdict("Business", tied, mixed_disclosed=True).reason, "long_haul_unverified")


class BadTimingTests(unittest.TestCase):
    """Known zones with a missing time is bad data, not an unknown hub."""

    BAD = {"missing": {"arrives": None}, "unparsable": {"arrives": "evening"},
           "non-positive": {"arrives": "2031-06-12T05:00:00"},
           "not wall-clock": {"arrives": "2031-06-12T19:05:00-05:00"}}

    def test_a_disclosed_mix_with_bad_timing_stays_dropped(self):
        for name, change in self.BAD.items():
            with self.subTest(name):
                legs = feeder_then_long_haul(**change)
                self.assertIsNone(segments_from_legs(legs, ZONES))
                v = verdict(legs)
                self.assertFalse(v.eligible)
                self.assertEqual(v.reason, "mixed_cabin_segments_unknown")

    def test_without_the_rule_the_trap_would_have_alerted(self):
        # Same itinerary, the arrival time missing: read as untimed, the Business feeder and the
        # Economy long haul disagree, and the verdict would be a CHECK NEEDED wake.
        as_if_untimed = (Segment("PAV", "KRN", "Business", 155), Segment("KRN", "LMB", "Economy", None))
        self.assertEqual(cabin_verdict("Business", as_if_untimed, mixed_disclosed=True).reason,
                         "long_haul_unverified")

    def test_an_undisclosed_fare_with_unknown_segments_alerts_with_a_label(self):
        v = verdict(feeder_then_long_haul(arrives=None), disclosed=False)
        self.assertTrue(v.eligible)
        self.assertEqual(v.labels, ("segment_cabins_unconfirmed",))

    def test_a_single_cabin_itinerary_is_unaffected_by_bad_timing(self):
        legs = feeder_then_long_haul(long_haul="Business", arrives=None)
        segments = segments_from_legs(legs, ZONES)
        self.assertEqual([s.minutes for s in segments], [155, None])
        self.assertEqual(verdict(legs).reason, "premium_long_haul")


class FareCabinTests(unittest.TestCase):
    def test_an_economy_fare_never_alerts(self):
        v = verdict(feeder_then_long_haul(feeder="Economy"), fare="economy")
        self.assertEqual((v.eligible, v.reason), (False, "fare_cabin_not_premium"))


if __name__ == "__main__":
    unittest.main()
