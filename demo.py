"""Synthetic timeline, explicit clock, temporary state, no network and nothing sent."""
from datetime import timedelta
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))

from boundary import handoff  # noqa: E402
from candidates import from_api, from_email, freshness  # noqa: E402
from fixtures import ALIASES, OFFICIAL_HOSTS, POLICY, SENDERS, SPEC, T0, FakeTransport, api_row, email  # noqa: E402
from health import FAILURE, STALE, HealthGuard  # noqa: E402
from inbound import InboundQueue  # noqa: E402
from policy import ValuePolicy  # noqa: E402
from rearm import RearmGate, displayed  # noqa: E402
from wake import ChannelPolicy, WakeLedger  # noqa: E402

MIN = timedelta(minutes=1)


def section(title):
    print(f"\n== {title}")


def main():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        policy = ValuePolicy.from_config(POLICY)

        section("1. Two sources, one seat")
        by_api = from_api(api_row(), aliases=ALIASES)
        by_mail = from_email(email(), senders=SENDERS, aliases=ALIASES)
        print(f"api   {by_api.alert_group:15} {by_api.event_key}  {freshness(by_api, T0, max_age=30 * MIN)}")
        print(f"email {by_mail.alert_group:15} {by_mail.event_key}  {freshness(by_mail, T0, max_age=30 * MIN)}")

        section("2. Value policy (score must be strictly above 2.5)")
        for points in (65_000, 70_000):
            c = from_api(api_row(points=points), aliases=ALIASES)
            v = policy.evaluate(c)
            print(f"{points:,} points  score {v.score}  eligible={v.eligible}  {', '.join(v.reasons) or '-'}")
        v = policy.evaluate(from_api(api_row(program="Harbor Rewards"), aliases=ALIASES))
        print(f"other program  eligible={v.eligible}  {', '.join(v.reasons)}")

        section("3. Wake on three channels; the phone relay is down")
        queue = InboundQueue(root / "inbound.sqlite")
        channels = {"chat": ChannelPolicy(3, 2, MIN, MIN / 2), "phone": ChannelPolicy(3, 2, MIN, MIN / 2),
                    "desktop": ChannelPolicy(2, 2, MIN, MIN / 2)}
        transports = {"chat": FakeTransport(), "phone": FakeTransport("raise"), "desktop": FakeTransport()}
        ledger = WakeLedger(root / "wake.json", channels, cooldown=timedelta(hours=6))
        text = handoff(by_api, policy.evaluate(by_api), "https://awards.example.com/search?id=row-7781",
                       allowed_hosts=OFFICIAL_HOSTS)
        for c in (by_api, by_mail):
            print(f"open from {c.alert_group:15} -> {ledger.open(c.event_key, text, c.alert_group, T0)}")
        now = T0
        while now <= T0 + 2 * MIN:
            if now == T0 + timedelta(seconds=90):
                queue.append(101, "ACK", now)
                print(f"+{(now - T0).seconds:>4}s inbound queue: operator replied ACK")
            for r in ledger.tick(transports, queue.reader(), now):
                print(f"+{(now - T0).seconds:>4}s {r.get('channel', '-'):8} {r['result']:9} {r.get('error') or ''}")
            now += MIN / 2
        print("closed as", ledger.closed(by_api.event_key)["status"])
        print("first message:\n  " + transports["chat"].sent[0].replace("\n", "\n  "))

        section("4. Re-arm a one-shot alert that fired twice")
        gate = RearmGate(root / "rearm.json")
        gate.register("alert-0001", SPEC, kind="one_shot")
        print("disappearance only ->", gate.claim(T0)["status"])
        print("source hit        ->", gate.on_hit(alert_id="alert-0001", source_id=by_mail.source_id,
                                                 source_at=by_mail.observed_at, now=T0)["status"])
        claim = gate.claim(T0)
        rows = [{"marker": "row-a", "fields": displayed(SPEC)}, {"marker": "row-b", "fields": displayed(SPEC)}]
        auth = gate.authorize(claim["request_id"], claim["token"], provider_rows=[], observed_at=T0 + MIN,
                              expired_rows=rows, now=T0 + MIN)
        print("authorize         ->", auth["status"], auth["selection"])
        done = gate.complete(claim["request_id"], claim["token"], now=T0 + 2 * MIN,
                             readback={"id": "alert-0002", "state": "active", "spec": dict(SPEC)})
        print("readback          ->", done["result"], done["replacement_id"])

        section("5. Health: an aged phone test for 32 hours, then a flapping source")
        guard = HealthGuard()
        timeline = [(0, {"acceptance:phone_alarm": STALE})] + [(h * 60, {"acceptance:phone_alarm": STALE})
                                                               for h in range(1, 33)]
        timeline += [(2000, {"source:mail": FAILURE}), (2005, {}), (2010, {"source:mail": FAILURE}),
                     (2015, {}), (2050, {})]
        sent = [(m, n.text) for m, faults in timeline for n in guard.evaluate(faults, T0 + m * MIN)]
        for minute, line in sent:
            print(f"t+{minute:>4}m {line}")
        print(f"{len(timeline)} checks, {len(sent)} notices")


if __name__ == "__main__":
    main()
