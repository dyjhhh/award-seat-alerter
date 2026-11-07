from datetime import timedelta
import json
import unittest

from fixtures import T0
from health import FAILURE, STALE, HealthGuard

MIN = timedelta(minutes=1)


def run(guard, timeline):
    """timeline: list of (minutes after T0, faults). Returns (minute, kind) for every notice."""
    out = []
    for minute, faults in timeline:
        for notice in guard.evaluate(faults, T0 + minute * MIN):
            out.append((minute, notice.kind))
    return out


class HealthTests(unittest.TestCase):
    def test_one_warning_per_incident_while_its_faults_change(self):
        guard = HealthGuard()
        notices = run(guard, [(0, {"source:mail": FAILURE}),
                              (5, {"source:mail": FAILURE, "channel:phone": FAILURE}),
                              (10, {"channel:phone": FAILURE})])
        self.assertEqual(notices, [(0, "open")])
        self.assertEqual(guard.state["incident"]["seen"], ["channel:phone", "source:mail"])

    def test_recovery_is_held_then_sent_once(self):
        guard = HealthGuard()
        notices = run(guard, [(0, {"source:mail": FAILURE}), (5, {}), (20, {}), (36, {}), (60, {})])
        self.assertEqual(notices, [(0, "open"), (36, "recovered")])

    def test_a_flap_inside_the_hold_window_reopens_silently(self):
        guard = HealthGuard()
        timeline = [(0, {"x": FAILURE})]
        for minute in range(5, 120, 5):  # clear for 5 minutes, fail for 5 minutes, repeat
            timeline.append((minute, {} if minute % 10 else {"x": FAILURE}))
        notices = run(guard, timeline)
        self.assertEqual(notices, [(0, "open")])
        self.assertGreater(guard.state["incident"]["reopened"], 5)
        notices = run(guard, [(130, {}), (161, {})])
        self.assertEqual(notices, [(161, "recovered")])

    def test_a_new_incident_after_recovery_warns_again(self):
        guard = HealthGuard()
        run(guard, [(0, {"x": FAILURE}), (1, {}), (40, {})])
        self.assertEqual(run(guard, [(50, {"y": FAILURE})]), [(50, "open")])

    def test_stale_evidence_sends_one_retest_reminder_and_never_an_incident(self):
        # 32 hours of hourly checks that only find an aged physical test.
        guard = HealthGuard()
        notices = run(guard, [(60 * hour, {"acceptance:phone_alarm": STALE}) for hour in range(33)])
        self.assertEqual(notices, [(0, "retest_due")])
        self.assertIsNone(guard.state["incident"])

    def test_retest_reminds_again_only_for_a_newly_due_item_and_clears_once(self):
        guard = HealthGuard()
        notices = run(guard, [(0, {"acceptance:phone": STALE}),
                              (60, {"acceptance:phone": STALE}),
                              (120, {"acceptance:phone": STALE, "acceptance:desktop": STALE}),
                              (180, {}), (240, {})])
        self.assertEqual(notices, [(0, "retest_due"), (120, "retest_due"), (180, "retest_cleared")])

    def test_both_channels_work_side_by_side(self):
        guard = HealthGuard()
        notices = run(guard, [(0, {"acceptance:phone": STALE, "scheduler:not_running": FAILURE})])
        self.assertEqual(sorted(kind for _, kind in notices), ["open", "retest_due"])
        # The retest passing again does not touch the open incident, and vice versa.
        notices = guard.evaluate({"scheduler:not_running": FAILURE}, T0 + MIN)
        self.assertEqual([(n.channel, n.kind) for n in notices], [("retest", "retest_cleared")])
        self.assertEqual(guard.state["incident"]["status"], "open")

    def test_an_unknown_check_changes_nothing_and_unclassified_faults_raise(self):
        guard = HealthGuard()
        run(guard, [(0, {"x": FAILURE}), (1, {})])
        before = json.dumps(guard.state, sort_keys=True)
        self.assertEqual(guard.evaluate(None, T0 + 60 * MIN), [])
        self.assertEqual(json.dumps(guard.state, sort_keys=True), before)
        with self.assertRaises(ValueError):
            guard.evaluate({"x": "maybe"}, T0)

    def test_notice_keys_are_stable_for_idempotent_sending(self):
        a = HealthGuard().evaluate({"x": FAILURE}, T0)[0].key
        b = HealthGuard().evaluate({"x": FAILURE}, T0)[0].key
        self.assertEqual(a, b)

    def test_state_is_plain_json_and_survives_a_restart(self):
        guard = HealthGuard()
        run(guard, [(0, {"x": FAILURE}), (5, {})])
        restored = HealthGuard(json.loads(json.dumps(guard.state)))
        self.assertEqual(run(restored, [(10, {"x": FAILURE}), (15, {}), (50, {})]), [(50, "recovered")])

    def test_an_incident_held_open_only_by_a_retired_check_closes_without_a_recovery(self):
        old = HealthGuard()
        self.assertEqual(run(old, [(0, {"funding:balance_age": FAILURE})]), [(0, "open")])
        guard = HealthGuard(old.state, retired=("funding:",))
        # A host still on older code keeps reporting the retired code; it is dropped on input.
        notices = run(guard, [(5, {"funding:balance_age": FAILURE}), (60, {}), (600, {})])
        self.assertEqual(notices, [])
        self.assertEqual(guard.state["incident"]["closed_by"], "retired_check")
        self.assertEqual(run(guard, [(700, {"source:mail": FAILURE})]), [(700, "open")])

    def test_a_real_fault_in_the_same_incident_keeps_its_recovery(self):
        old = HealthGuard()
        run(old, [(0, {"funding:balance_age": FAILURE, "source:mail": FAILURE})])
        guard = HealthGuard(old.state, retired=("funding:",))
        notices = []
        for minute, faults in [(5, {"funding:balance_age": FAILURE, "source:mail": FAILURE}), (10, {}), (45, {})]:
            notices += guard.evaluate(faults, T0 + minute * MIN)
        self.assertEqual([n.kind for n in notices], ["recovered"])
        self.assertEqual(notices[0].text, "✅ Alert bridge recovered: source:mail")

    def test_a_retired_code_never_opens_an_incident(self):
        guard = HealthGuard(retired=("funding:",))
        self.assertEqual(run(guard, [(0, {"funding:balance_age": FAILURE})]), [])
        self.assertIsNone(guard.state["incident"])


if __name__ == "__main__":
    unittest.main()
