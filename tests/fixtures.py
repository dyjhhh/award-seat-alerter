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
