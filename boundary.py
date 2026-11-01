"""The irreversible-action boundary.

The bridge finds, scores, announces and re-arms alerts. Every one of those can
be undone or ignored. Booking, holding, paying, transferring points,
cancelling and contacting anyone cannot, and none of them has a code path in
this repository. `tests/test_boundary.py` checks that by reading the source.

The last thing the bridge produces is a handoff: the exact official page, what
was and was not verified, and a plain statement that nothing was bought.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from candidates import Candidate
from policy import Verdict

REVERSIBLE = frozenset({"notify", "rearm_alert", "open_official_page"})
IRREVERSIBLE = frozenset({"book", "hold", "pay", "transfer_points", "cancel", "contact"})


class BoundaryError(PermissionError):
    pass


def require_reversible(action: str) -> str:
    if action in REVERSIBLE:
        return action
    if action in IRREVERSIBLE:
        raise BoundaryError(f"{action} needs the operator's fresh approval of a concrete offer, outside this bridge")
    raise BoundaryError(f"unknown action {action!r} is refused, not guessed")


def official_link(url: str, allowed_hosts: frozenset[str]) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in allowed_hosts or parts.username or parts.password:
        raise BoundaryError("handoff links must be https on a configured official host")
    return url


def handoff(c: Candidate, verdict: Verdict, url: str, *, allowed_hosts: frozenset[str],
            verification: str = "CHECK NEEDED") -> str:
    """The alert text. Verification state is stated, never upgraded by the bridge."""
    require_reversible("notify")
    if verification not in ("CHECK NEEDED", "VERIFIED AVAILABLE", "VERIFIED UNAVAILABLE"):
        raise ValueError("unknown verification state")
    when = c.start.isoformat() + (f" to {c.end.isoformat()}" if c.end else "")
    what = (f"{c.origin}-{c.destination} {'/'.join(c.items)}" if c.kind == "flight"
            else f"{c.origin} {c.items[0]}")
    return "\n".join([
        f"{c.program}: {what} {c.cabin}, {when}",
        f"{c.points:,} points + {c.fees_cents / 100:.2f} {c.currency}, score {verdict.score}",
        f"Official page: {official_link(url, allowed_hosts)}",
        f"Verification: {verification}. Checkout: NOT REACHED.",
        "Nothing was booked, held, paid or transferred."])
