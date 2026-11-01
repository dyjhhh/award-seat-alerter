from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fixtures import T0, FakeTransport
from inbound import InboundQueue
from wake import ChannelPolicy, WakeLedger

MIN = timedelta(minutes=1)


def policy(sends=3, failures=2):
    return ChannelPolicy(max_sends=sends, max_failures=failures, repeat_every=MIN, retry_after=MIN / 2)


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.queue = InboundQueue(root / "inbound.sqlite")
        self.reader = self.queue.reader()
        self.ledger_path = root / "wake.json"
        self.channels = {"chat": policy(), "phone": policy(), "desktop": policy(sends=2)}
        self.ledger = WakeLedger(self.ledger_path, self.channels, cooldown=timedelta(hours=6))
        self.transports = {name: FakeTransport() for name in self.channels}

    def run_minutes(self, minutes, start=T0, step=MIN / 2):
        now, results = start, []
        while now <= start + timedelta(minutes=minutes):
            results += self.ledger.tick(self.transports, self.reader, now)
            now += step
        return results

    def test_repeats_stop_at_the_ceiling_without_an_ack(self):
        self.ledger.open("flight:a", "Seat found", "group-broad", T0)
        self.run_minutes(10)
        self.assertEqual([len(self.transports[n].sent) for n in ("chat", "phone", "desktop")], [3, 3, 2])
        self.assertEqual(self.ledger.closed("flight:a")["status"], "CEILING")
        self.assertEqual(self.ledger.active_keys(), [])
        self.assertIn("(repeat 3/3)", self.transports["chat"].sent[-1])
        self.assertIn("nothing is booked", self.transports["chat"].sent[0])

    def test_overlapping_alert_groups_for_one_seat_wake_once(self):
        self.assertEqual(self.ledger.open("flight:a", "Seat found", "group-broad", T0), "OPENED")
        self.assertEqual(self.ledger.open("flight:a", "Seat found", "group-one-shot", T0), "MERGED")
        self.ledger.tick(self.transports, self.reader, T0)
        self.assertEqual(len(self.transports["chat"].sent), 1)
        self.assertEqual(self.ledger.state["active"]["flight:a"]["groups"], ["group-broad", "group-one-shot"])

    def test_a_recently_closed_event_does_not_reopen_inside_the_cooldown(self):
        self.ledger.open("flight:a", "Seat found", "g", T0)
        self.run_minutes(10)
        self.assertEqual(self.ledger.open("flight:a", "Seat found", "g2", T0 + timedelta(hours=1)), "COOLDOWN")
        self.assertEqual(self.ledger.open("flight:a", "Seat found", "g2", T0 + timedelta(hours=7)), "OPENED")

    def test_ack_stops_only_escalations_active_when_it_arrived(self):
        self.ledger.open("flight:a", "Seat A", "g", T0)
        self.ledger.tick(self.transports, self.reader, T0)
        self.queue.append(1, "ACK", T0 + MIN / 2)
        self.ledger.open("flight:b", "Seat B", "g", T0 + MIN)
        results = self.run_minutes(10, start=T0 + MIN)
        self.assertEqual(self.ledger.closed("flight:a")["status"], "ACKED")
        self.assertEqual(self.ledger.closed("flight:b")["status"], "CEILING")
        self.assertEqual(sum(1 for r in results if r.get("key") == "flight:a" and r["result"] == "ACCEPTED"), 0)

    def test_a_lookalike_reply_does_not_stop_repeats(self):
        self.ledger.open("flight:a", "Seat A", "g", T0)
        self.queue.append(1, "ACK later", T0)
        self.queue.append(2, "ok", T0)
        self.run_minutes(10)
        self.assertEqual(self.ledger.closed("flight:a")["status"], "CEILING")

    def test_ack_is_checked_before_every_send_not_once_per_tick(self):
        class AckingTransport(FakeTransport):
            def __init__(inner, queue):
                super().__init__()
                inner.queue = queue

            def send(inner, text):
                inner.queue.append(1, "ACK", T0)  # the operator replies while the chat send is in flight
                return super().send(text)

        self.transports["chat"] = AckingTransport(self.queue)
        self.ledger.open("flight:a", "Seat A", "g", T0)
        self.ledger.tick(self.transports, self.reader, T0)
        self.assertEqual(len(self.transports["chat"].sent), 1)
        self.assertEqual(self.transports["phone"].sent, [])
        self.assertEqual(self.ledger.closed("flight:a")["status"], "ACKED")

    def test_a_failing_channel_does_not_block_the_others_and_has_its_own_budget(self):
        self.transports["phone"] = FakeTransport("raise")
        self.transports["desktop"] = FakeTransport("reject")
        self.ledger.open("flight:a", "Seat A", "g", T0)
        results = self.run_minutes(10)
        self.assertEqual(len(self.transports["chat"].sent), 3)
        self.assertEqual(len(self.transports["phone"].sent), 2)   # max_failures, not max_sends
        record = self.ledger.closed("flight:a")
        self.assertEqual(record["channels"]["phone"]["status"], "EXHAUSTED")
        self.assertEqual(record["channels"]["phone"]["last_error"], "ConnectionError")
        self.assertEqual(record["channels"]["phone"]["sent"], 0)
        self.assertEqual(record["status"], "CEILING")
        self.assertTrue(any(r.get("error") == "not_accepted" for r in results))

    def test_nothing_accepted_anywhere_is_reported_as_undelivered(self):
        self.transports = {name: FakeTransport("raise") for name in self.channels}
        self.ledger.open("flight:a", "Seat A", "g", T0)
        self.run_minutes(10)
        self.assertEqual(self.ledger.closed("flight:a")["status"], "UNDELIVERED")

    def test_a_missing_transport_counts_as_a_failure_not_a_crash(self):
        del self.transports["desktop"]
        self.ledger.open("flight:a", "Seat A", "g", T0)
        self.run_minutes(10)
        self.assertEqual(self.ledger.closed("flight:a")["channels"]["desktop"]["last_error"], "LookupError")

    def test_state_survives_a_restart_without_a_second_first_send(self):
        self.ledger.open("flight:a", "Seat A", "g", T0)
        self.ledger.tick(self.transports, self.reader, T0)
        reopened = WakeLedger(self.ledger_path, self.channels, cooldown=timedelta(hours=6))
        self.assertEqual(reopened.open("flight:a", "Seat A", "g", T0), "MERGED")
        reopened.tick(self.transports, self.reader, T0 + timedelta(seconds=10))
        self.assertEqual(len(self.transports["chat"].sent), 1)

    def test_policies_must_be_finite_and_positive(self):
        with self.assertRaises(ValueError):
            ChannelPolicy(max_sends=0, max_failures=1, repeat_every=MIN, retry_after=MIN)
        with self.assertRaises(ValueError):
            ChannelPolicy(max_sends=1, max_failures=1, repeat_every=timedelta(0), retry_after=MIN)


if __name__ == "__main__":
    unittest.main()
