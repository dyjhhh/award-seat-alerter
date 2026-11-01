from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import copy
import unittest

from fixtures import SPEC, T0
from rearm import GateError, RearmGate, displayed

MIN = timedelta(minutes=1)


def row(marker, spec=SPEC, **changes):
    fields = displayed(spec)
    fields.update(changes)
    return {"marker": marker, "fields": fields}


class RearmTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gate = RearmGate(Path(self.temp.name) / "rearm.json", max_attempts=2)
        self.gate.register("alert-0001", SPEC, kind="one_shot")
        self.gate.register("alert-0900", dict(SPEC, label="demo-continuous"), kind="continuous")

    def hit(self, source_id="msg-1", at=T0, alert="alert-0001"):
        return self.gate.on_hit(alert_id=alert, source_id=source_id, source_at=at - MIN, now=at)

    def claimed(self, at=T0):
        self.hit(at=at)
        claim = self.gate.claim(at)
        self.assertEqual(claim["status"], "CLAIMED")
        return claim

    def authorize(self, claim, *, active=(), expired=None, at=T0 + MIN):
        expired = [row("row-a")] if expired is None else expired
        return self.gate.authorize(claim["request_id"], claim["token"], provider_rows=list(active),
                                   observed_at=at, expired_rows=expired, now=at)

    # -- event gating --------------------------------------------------------
    def test_only_a_fresh_hit_on_a_tracked_one_shot_alert_opens_a_request(self):
        self.assertEqual(self.hit(alert="alert-0900")["reason"], "continuous_alert_never_rearmed")
        self.assertEqual(self.hit(alert="alert-7777")["reason"], "alert_not_tracked")
        stale = self.gate.on_hit(alert_id="alert-0001", source_id="old", source_at=T0 - timedelta(hours=3), now=T0)
        self.assertEqual(stale["reason"], "source_not_fresh")
        future = self.gate.on_hit(alert_id="alert-0001", source_id="f", source_at=T0 + MIN, now=T0)
        self.assertEqual(future["reason"], "source_not_fresh")
        self.assertEqual(self.hit()["status"], "REVIEW")

    def test_disappearance_alone_never_creates_a_request(self):
        self.assertEqual(self.gate.claim(T0)["status"], "NO_PENDING")

    def test_the_same_source_or_a_second_hit_does_not_open_a_second_request(self):
        first = self.hit()
        self.assertEqual(self.hit()["status"], "DUPLICATE")
        again = self.hit(source_id="msg-2")
        self.assertEqual(again, {"status": "ALREADY_PENDING", "request_id": first["request_id"]})

    # -- lease and attempts -----------------------------------------------------
    def test_a_lease_keeps_a_second_worker_out_until_it_expires(self):
        claim = self.claimed()
        self.assertEqual(self.gate.claim(T0 + MIN)["status"], "NO_PENDING")
        later = self.gate.claim(T0 + timedelta(minutes=11))
        self.assertEqual(later["status"], "CLAIMED")
        with self.assertRaises(GateError):
            self.authorize(claim, at=T0 + timedelta(minutes=11))  # the first token is dead

    def test_attempts_are_bounded_then_the_request_is_quarantined(self):
        claim = self.claimed()
        self.gate.release(claim["request_id"], claim["token"], "browser_timeout", T0 + MIN)
        second = self.gate.claim(T0 + timedelta(minutes=4))
        self.gate.release(second["request_id"], second["token"], "browser_timeout", T0 + timedelta(minutes=5))
        result = self.gate.claim(T0 + timedelta(minutes=8))
        self.assertEqual(result["status"], "NO_PENDING")
        self.assertEqual(result["quarantined"], [{"status": "QUARANTINED", "reason": "maximum_attempts"}])
        notices = self.gate.take_notices()
        self.assertEqual([n["reason"] for n in notices], ["maximum_attempts"])
        self.assertEqual(self.gate.take_notices(), [])  # one notice, not one per run

    def test_a_request_whose_source_aged_out_is_quarantined(self):
        claim = self.claimed()
        self.gate.release(claim["request_id"], claim["token"], "worker_crash", T0 + MIN)
        result = self.gate.claim(T0 + timedelta(hours=3))
        self.assertEqual(result["quarantined"][0]["reason"], "source_expired")

    # -- authorization -----------------------------------------------------------
    def test_a_still_active_alert_finishes_without_recreating(self):
        claim = self.claimed()
        result = self.authorize(claim, active=[{"id": "alert-0001", "spec": SPEC}])
        self.assertEqual(result, {"status": "DONE", "result": "still_active"})

    def test_one_equivalent_active_alert_finishes_and_two_quarantine(self):
        claim = self.claimed()
        result = self.authorize(claim, active=[{"id": "alert-0002", "spec": copy.deepcopy(SPEC)}])
        self.assertEqual(result["result"], "equivalent_active")
        self.hit(source_id="msg-9", at=T0 + timedelta(minutes=2))
        claim = self.gate.claim(T0 + timedelta(minutes=2))
        result = self.authorize(claim, active=[{"id": "alert-0002", "spec": SPEC}, {"id": "alert-0003", "spec": SPEC}],
                                at=T0 + timedelta(minutes=3))
        self.assertEqual(result["reason"], "duplicate_active_equivalents")

    def test_one_matching_expired_row_authorizes(self):
        result = self.authorize(self.claimed())
        self.assertEqual(result["status"], "AUTHORIZED")
        self.assertEqual(result["selection"], "SINGLE_ROW")

    def test_identical_expired_rows_authorize_once(self):
        rows = [row("row-a"), row("row-b"), row("row-c")]
        result = self.authorize(self.claimed(), expired=rows)
        self.assertEqual(result["selection"], "IDENTICAL_EXPIRED_ROWS")

    def test_any_displayed_difference_or_unclear_page_quarantines(self):
        bad_pages = {
            "a field differs": [row("row-a"), row("row-b", caps="max 80,000 points, max 150.00 fees")],
            "another label": [row("row-a", label="demo-business-2")],
            "repeated marker": [row("row-a"), row("row-a")],
            "field missing": [{"marker": "row-a", "fields": {"label": "demo-business"}}],
            "empty page": [],
            "too many rows": [row(f"row-{i}") for i in range(21)],
        }
        for name, rows in bad_pages.items():
            with self.subTest(name):
                gate = RearmGate(Path(self.temp.name) / f"{len(name)}-{name[:3]}.json")
                gate.register("alert-0001", SPEC, kind="one_shot")
                gate.on_hit(alert_id="alert-0001", source_id="m", source_at=T0 - MIN, now=T0)
                claim = gate.claim(T0)
                result = gate.authorize(claim["request_id"], claim["token"], provider_rows=[],
                                        observed_at=T0 + MIN, expired_rows=rows, now=T0 + MIN)
                self.assertEqual(result, {"status": "QUARANTINED", "reason": "expired_row_ambiguous"})

    def test_the_snapshot_must_be_taken_during_this_claim(self):
        claim = self.claimed(at=T0 + timedelta(minutes=5))
        with self.assertRaises(GateError):  # observed before the claim started
            self.gate.authorize(claim["request_id"], claim["token"], provider_rows=[], observed_at=T0,
                                expired_rows=[row("row-a")], now=T0 + timedelta(minutes=6))

    # -- readback ----------------------------------------------------------------
    def test_completion_requires_a_new_active_id_with_every_frozen_field(self):
        claim = self.claimed()
        self.authorize(claim)
        result = self.gate.complete(claim["request_id"], claim["token"], now=T0 + 2 * MIN,
                                    readback={"id": "alert-0002", "state": "active", "spec": copy.deepcopy(SPEC)})
        self.assertEqual(result, {"status": "DONE", "result": "recreated", "replacement_id": "alert-0002"})
        alerts = self.gate.snapshot()["alerts"]
        self.assertEqual(alerts["alert-0001"]["status"], "RETIRED")
        self.assertEqual(alerts["alert-0002"]["replaces"], "alert-0001")
        self.assertEqual(alerts["alert-0900"]["status"], "ACTIVE")  # the continuous alert is untouched
        follow_up = self.hit(alert="alert-0002", source_id="msg-3", at=T0 + timedelta(minutes=30))
        self.assertEqual(follow_up["status"], "REVIEW")  # the replacement is tracked for the next hit

    def test_a_readback_that_differs_quarantines(self):
        widened = dict(SPEC, max_points=90_000)
        for name, readback in {"wider caps": {"id": "alert-0002", "state": "active", "spec": widened},
                               "not active": {"id": "alert-0002", "state": "paused", "spec": SPEC},
                               "old id": {"id": "alert-0001", "state": "active", "spec": SPEC}}.items():
            with self.subTest(name):
                gate = RearmGate(Path(self.temp.name) / f"rb-{name[:3]}.json")
                gate.register("alert-0001", SPEC, kind="one_shot")
                gate.on_hit(alert_id="alert-0001", source_id="m", source_at=T0 - MIN, now=T0)
                claim = gate.claim(T0)
                gate.authorize(claim["request_id"], claim["token"], provider_rows=[], observed_at=T0 + MIN,
                               expired_rows=[row("row-a")], now=T0 + MIN)
                result = gate.complete(claim["request_id"], claim["token"], readback=readback, now=T0 + 2 * MIN)
                self.assertEqual(result["status"], "QUARANTINED")

    def test_complete_without_authorization_is_refused(self):
        claim = self.claimed()
        with self.assertRaises(GateError):
            self.gate.complete(claim["request_id"], claim["token"], now=T0 + MIN,
                               readback={"id": "alert-0002", "state": "active", "spec": SPEC})


if __name__ == "__main__":
    unittest.main()
