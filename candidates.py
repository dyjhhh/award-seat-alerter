"""Normalize provider alerts from mail and from an API feed into one event key.

Two sources can report the same seat: a provider email and a polled API row.
Both become a `Candidate`, and `event_key` ignores which source, which alert
group and which message produced it, so one seat wakes the operator once.
Nothing here reads a mailbox or calls a network service; callers pass dicts.

A field the provider left out is unknown, and stays unknown: a missing seat
count is not one seat, and a fee with no currency is not dollars. A field that
is present but malformed still raises.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re

KINDS = frozenset({"flight", "stay"})
CODE = re.compile(r"^[A-Z]{3}$")
FLIGHT = re.compile(r"^[A-Z0-9]{2}\d{1,4}$")


class CandidateError(ValueError):
    """A message that claims to be an alert but cannot be trusted as one."""


@dataclass(frozen=True)
class Candidate:
    kind: str                    # "flight" or "stay"
    program: str                 # canonical program name after alias lookup
    origin: str                  # airport code, or the property code for a stay
    destination: str             # airport code, or "" for a stay
    items: tuple[str, ...]       # flight numbers in order, or (room type,)
    start: date                  # departure or check-in
    end: date | None             # None for a flight, check-out for a stay
    cabin: str                   # cabin, or room class for a stay
    quantity: int | None         # seats or rooms; None when missing or shown as 0
    points: int                  # per person or per night
    fees_cents: int
    currency: str | None         # None when the fees carry no currency
    source: str                  # "email" or "api"
    source_id: str               # message id or API row id
    alert_group: str             # the provider alert that matched
    observed_at: datetime        # provider observation time, not receipt time
    connection_minutes: int | None = None  # longest connection; 0 nonstop, None unknown
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def event_key(self) -> str:
        return event_key(self)


def event_key(c: Candidate) -> str:
    """Stable identity for one bookable thing, independent of source and group."""
    body = {"kind": c.kind, "program": c.program, "origin": c.origin,
            "destination": c.destination, "items": list(c.items),
            "start": c.start.isoformat(), "end": c.end.isoformat() if c.end else None,
            "cabin": c.cabin}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return f"{c.kind}:{digest[:16]}"


def _program(raw, aliases: dict[str, str]) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CandidateError("program missing")
    name = " ".join(raw.split())
    canonical = aliases.get(name.casefold())
    if canonical is None:
        raise CandidateError(f"program not configured: {name}")
    return canonical


def _code(raw, label) -> str:
    value = str(raw or "").strip().upper()
    if not CODE.match(value):
        raise CandidateError(f"{label} must be a three-letter code")
    return value


def _flights(raw) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = re.split(r"[,>]", raw)
    if not isinstance(raw, (list, tuple)) or not raw:
        raise CandidateError("flight numbers missing")
    out = tuple(str(item).replace(" ", "").upper() for item in raw)
    if any(not FLIGHT.match(item) for item in out):
        raise CandidateError("flight number malformed")
    return out


def _positive_int(raw, label, *, allow_zero=False) -> int:
    if isinstance(raw, bool):
        raise CandidateError(f"{label} must be an integer")
    try:
        value = int(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        raise CandidateError(f"{label} must be an integer") from None
    if value < 0 or (value == 0 and not allow_zero):
        raise CandidateError(f"{label} out of range")
    return value


def _cents(raw) -> int:
    text = str(raw).replace(",", "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        raise CandidateError("fees malformed")
    whole, _, frac = text.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def _moment(raw, label) -> datetime:
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise CandidateError(f"{label} is not an ISO timestamp") from None
    if value.tzinfo is None:
        raise CandidateError(f"{label} needs a time zone")
    return value.astimezone(timezone.utc)


def _date(raw, label) -> date:
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise CandidateError(f"{label} is not an ISO date") from None


def _build(fields: dict, *, source, source_id, aliases) -> Candidate:
    kind = str(fields.get("kind", "flight")).strip().lower()
    if kind not in KINDS:
        raise CandidateError("kind must be flight or stay")
    if kind == "flight":
        origin = _code(fields.get("origin"), "origin")
        destination = _code(fields.get("destination"), "destination")
        if origin == destination:
            raise CandidateError("origin equals destination")
        items = _flights(fields.get("flights"))
        end = None
    else:
        origin = str(fields.get("property") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]{3,24}", origin):
            raise CandidateError("property code malformed")
        destination = ""
        items = (" ".join(str(fields.get("room") or "standard").split()).lower(),)
        end = _date(fields.get("check_out"), "check_out")
    start = _date(fields.get("date") or fields.get("check_in"), "date")
    if end is not None and end <= start:
        raise CandidateError("check_out must follow check_in")
    currency = str(fields.get("currency") or "").strip().upper() or None
    if currency is not None and not re.fullmatch(r"[A-Z]{3}", currency):
        raise CandidateError("currency malformed")
    # Some feeds show a hidden seat count as 0, so 0 is unknown here, not sold out.
    count = fields.get("seats", fields.get("rooms"))
    blank = count is None or not str(count).strip()
    quantity = None if blank else _positive_int(count, "quantity", allow_zero=True) or None
    connection = None
    if kind == "flight":
        raw = fields.get("connection_minutes")
        connection = (0 if len(items) == 1 else
                      None if raw is None or not str(raw).strip()
                      else _positive_int(raw, "connection_minutes", allow_zero=True))
    group = str(fields.get("alert_group") or "").strip()
    if not group:
        raise CandidateError("alert group missing")
    return Candidate(
        kind=kind, program=_program(fields.get("program"), aliases), origin=origin,
        destination=destination, items=items, start=start, end=end,
        cabin=" ".join(str(fields.get("cabin") or "").split()).title() or "Standard",
        quantity=quantity,
        points=_positive_int(fields.get("points"), "points"),
        fees_cents=_cents(fields.get("fees", "0")), currency=currency,
        source=source, source_id=source_id, alert_group=group,
        observed_at=_moment(fields.get("observed_at"), "observed_at"),
        connection_minutes=connection)


def from_api(row: dict, *, aliases: dict[str, str]) -> Candidate:
    """One row of a polled API export."""
    if not isinstance(row, dict):
        raise CandidateError("API row must be an object")
    row_id = str(row.get("id") or "").strip()
    if not row_id:
        raise CandidateError("API row id missing")
    return _build(row, source="api", source_id=row_id, aliases=aliases)


LINE = re.compile(r"^\s*([A-Za-z_ ]+?)\s*:\s*(.+?)\s*$")


def from_email(message: dict, *, senders: frozenset[str], aliases: dict[str, str],
               max_body: int = 20_000) -> Candidate | None:
    """A provider alert email with `Key: value` lines.

    Returns None for mail that is not an alert from an exact configured sender.
    Raises CandidateError for mail that is from the sender but malformed, so it
    is surfaced instead of being read as "no alert".
    """
    if not isinstance(message, dict):
        raise CandidateError("message must be an object")
    sender = str(message.get("from") or "").strip().casefold()
    if sender not in senders:
        return None
    message_id = str(message.get("message_id") or "").strip()
    if not message_id:
        raise CandidateError("message id missing")
    body = message.get("body")
    if not isinstance(body, str) or len(body) > max_body:
        raise CandidateError("body missing or too large")
    fields: dict[str, str] = {}
    for line in body.splitlines():
        match = LINE.match(line)
        if match:
            key = match.group(1).strip().lower().replace(" ", "_")
            if key in fields:
                raise CandidateError(f"duplicate field: {key}")
            fields[key] = match.group(2)
    if "points" not in fields:
        return None  # newsletters and receipts from the same sender
    return _build(fields, source="email", source_id=message_id, aliases=aliases)


def freshness(c: Candidate, now: datetime, *, max_age: timedelta,
              skew: timedelta = timedelta(minutes=2)) -> str:
    """fresh, stale or future. A fresh export of old inventory is still stale."""
    if c.observed_at > now + skew:
        return "future"
    if now - c.observed_at > max_age:
        return "stale"
    return "fresh"
