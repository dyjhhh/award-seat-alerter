"""Synthetic data shared by the tests and the demo. Every name, code and number is invented."""
from datetime import datetime, timedelta, timezone

T0 = datetime(2031, 3, 4, 2, 0, tzinfo=timezone.utc)
SENDERS = frozenset({"alerts@provider.example.com"})
OFFICIAL_HOSTS = frozenset({"awards.example.com"})
ALIASES = {"northwind miles": "Northwind Miles", "northwind": "Northwind Miles",
           "harbor rewards": "Harbor Rewards", "lantern stays": "Lantern Stays"}

POLICY = {
    "baseline_points": 100_000,
    "cabin_weights": {"Economy": "1", "Premium Economy": "1.25", "Business": "1.75"},
    "threshold": "2.5",
    "window": ["2031-06-01", "2031-06-30"],
    "max_fees_cents": 15_000,
    "currency": "USD",
    "max_connection_minutes": 360,
}


def api_row(**overrides):
    row = {"id": "row-7781", "kind": "flight", "program": "Northwind Miles", "origin": "PAV",
           "destination": "LMB", "flights": ["NW 401"], "date": "2031-06-12", "cabin": "business",
           "seats": 2, "points": 65_000, "fees": "89.40", "currency": "USD",
           "alert_group": "group-broad", "observed_at": (T0 - timedelta(minutes=3)).isoformat()}
    row.update(overrides)
    return row


def email(**overrides):
    fields = {"Program": "Northwind", "Origin": "pav", "Destination": "lmb", "Flights": "NW401",
              "Date": "2031-06-12", "Cabin": "Business", "Seats": "2", "Points": "65,000",
              "Fees": "89.40", "Currency": "USD", "Alert group": "group-one-shot",
              "Observed at": (T0 - timedelta(minutes=4)).isoformat()}
    fields.update(overrides.pop("fields", {}))
    message = {"from": "alerts@provider.example.com", "message_id": "<msg-0001@provider.example.com>",
               "body": "New availability\n" + "\n".join(f"{k}: {v}" for k, v in fields.items())}
    message.update(overrides)
    return message


# Known time zones as fixed offsets. QZH is a hub whose time zone is deliberately not listed.
ZONES = {"PAV": timezone(timedelta(hours=2)), "KRN": timezone(timedelta(hours=3)),
         "LMB": timezone(timedelta(hours=-5))}


def leg(origin, destination, cabin, departs, arrives):
    return {"origin": origin, "destination": destination, "cabin": cabin,
            "departs": departs, "arrives": arrives}


def feeder_then_long_haul(feeder="Business", long_haul="Economy", hub="KRN", **long_haul_changes):
    """A 155-minute feeder into the hub, then an 805-minute long haul (when the hub is KRN)."""
    second = leg(hub, "LMB", long_haul, "2031-06-12T13:40:00", "2031-06-12T19:05:00")
    second.update(long_haul_changes)
    return [leg("PAV", hub, feeder, "2031-06-12T08:00:00", "2031-06-12T11:35:00"), second]


SPEC = {"label": "demo-business", "program": "Northwind Miles", "origin": "PAV", "destination": "LMB",
        "date_from": "2031-06-10", "date_to": "2031-06-14", "cabins": ["Business"],
        "max_points": 70_000, "max_fees_cents": 15_000}


class FakeTransport:
    """Records what it was asked to send. Can be told to fail or raise."""

    def __init__(self, behaviour="accept"):
        self.behaviour = behaviour
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        if self.behaviour == "raise":
            raise ConnectionError("simulated outage")
        return self.behaviour == "accept"
