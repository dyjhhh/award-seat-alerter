"""Mixed-cabin itineraries: is the long haul really in the premium cabin?

A fare sold as Business can be a short Business feeder in front of an
Economy long haul. The long haul is what the person is paying points for, so
the alert has to be about that segment, not about the fare's headline cabin.

The long haul is the longest segment. A segment's length is known only when
its departure and arrival wall-clock times are known and both airports' time
zones are known. Two kinds of gap look alike and must be kept apart:

- A hub whose time zone is not in the table. The length of that segment is
  unknown, but nothing is wrong with the data. If the long-haul cabin cannot
  be proven either way, the itinerary alerts with the label
  `long_haul_cabin_unconfirmed` and lists every segment's cabin. A person
  checks it in a minute; a dropped real premium seat is gone.
- A segment between two known time zones with a missing, unparsable or
  non-positive time. That is bad provider data, not an unknown hub. In a
  mixed-cabin itinerary the whole segment list is treated as unknown, so a
  disclosed mix is dropped as before. Without this rule a short premium
  feeder plus an Economy long haul whose arrival time went missing would come
  out as "unprovable" and wake the person for a seat that is Economy where it
  matters.

A fare's own legs can show the mix even when its mixed-cabin flag is off.
`verdict_from_legs` counts that as disclosed, so the trap above cannot come
back through a missing flag.

A known Economy long haul is always dropped. Nothing is guessed from
geography.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
import re

PREMIUM = frozenset({"Premium Economy", "Business", "First"})
CODE = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class Segment:
    origin: str
    destination: str
    cabin: str
    minutes: int | None          # None when the length cannot be computed


@dataclass(frozen=True)
class CabinVerdict:
    eligible: bool
    reason: str                  # why it alerts or why it was dropped
    labels: tuple[str, ...] = ()  # what the alert must say is unconfirmed
    detail: str = ""             # every segment's cabin, shown on the alert


def _cabin(raw) -> str | None:
    text = " ".join(str(raw or "").split()).title()
    return text or None


def _wallclock(raw) -> datetime | None:
    """A provider's local wall-clock time. Anything else counts as missing."""
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo is None else None


def segments_from_legs(legs, zones: dict[str, tzinfo]) -> tuple[Segment, ...] | None:
    """Provider legs to segments. None means the segment list is unknown.

    Each leg is a dict with origin, destination, cabin, departs and arrives
    (local wall-clock). `zones` maps the airports whose time zone is known.
    """
    if not isinstance(legs, (list, tuple)) or not legs or not all(isinstance(x, dict) for x in legs):
        return None
    segments, bad_timing = [], False
    for i, leg in enumerate(legs):
        origin = str(leg.get("origin") or "").strip().upper()
        destination = str(leg.get("destination") or "").strip().upper()
        cabin = _cabin(leg.get("cabin"))
        if not (CODE.match(origin) and CODE.match(destination)) or cabin is None:
            return None
        if i and segments[-1].destination != origin:
            return None  # legs that do not connect are not one itinerary
        minutes = None
        if origin in zones and destination in zones:
            departs, arrives = _wallclock(leg.get("departs")), _wallclock(leg.get("arrives"))
            if departs and arrives:
                span = arrives.replace(tzinfo=zones[destination]) - departs.replace(tzinfo=zones[origin])
                minutes = int(span.total_seconds() // 60)
            if minutes is None or minutes <= 0:
                minutes, bad_timing = None, True
        segments.append(Segment(origin, destination, cabin, minutes))
    if bad_timing and len({s.cabin for s in segments}) > 1:
        return None  # bad data in a mix: which leg is long cannot be argued from it
    return tuple(segments)


def long_haul_is_premium(segments: tuple[Segment, ...]) -> bool | None:
    """True or False when provable, None when the long-haul cabin cannot be proven.

    The longest segment is either an untimed one or the longest timed one, so
    those are the only candidates. If they agree, the answer is known.
    """
    timed = [s.minutes for s in segments if s.minutes is not None]
    longest = max(timed, default=None)
    candidates = [s for s in segments if s.minutes is None or s.minutes == longest]
    premium = {s.cabin in PREMIUM for s in candidates}
    return premium.pop() if len(premium) == 1 else None


def describe(segments: tuple[Segment, ...]) -> str:
    return " · ".join(f"{s.origin}-{s.destination} {s.cabin}" for s in segments)


def cabin_verdict(fare_cabin: str, segments: tuple[Segment, ...] | None, *,
                  mixed_disclosed: bool) -> CabinVerdict:
    """Admission for a premium-only watch. Every drop says why."""
    if _cabin(fare_cabin) not in PREMIUM:
        return CabinVerdict(False, "fare_cabin_not_premium")
    if segments is None:
        if mixed_disclosed:
            return CabinVerdict(False, "mixed_cabin_segments_unknown")
        return CabinVerdict(True, "premium_segments_unverified", ("segment_cabins_unconfirmed",))
    detail = describe(segments)
    answer = long_haul_is_premium(segments)
    if answer is True:
        return CabinVerdict(True, "premium_long_haul", (), detail)
    if answer is False:
        return CabinVerdict(False, "long_haul_not_premium", (), detail)
    return CabinVerdict(True, "long_haul_unverified", ("long_haul_cabin_unconfirmed",), detail)


def legs_show_mix(legs) -> bool:
    """True when the provider's own legs carry more than one cabin."""
    if not isinstance(legs, (list, tuple)):
        return False
    return len({_cabin(x.get("cabin")) for x in legs if isinstance(x, dict)} - {None}) > 1


def verdict_from_legs(fare_cabin: str, legs, zones: dict[str, tzinfo], *,
                      mix_flagged: bool) -> CabinVerdict:
    """The entry point for provider data. Legs that carry two cabins disclose a mix."""
    return cabin_verdict(fare_cabin, segments_from_legs(legs, zones),
                         mixed_disclosed=mix_flagged or legs_show_mix(legs))
