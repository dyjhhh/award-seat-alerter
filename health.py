"""Health notices: one warning per incident, one coalesced recovery, a separate retest channel.

Faults come in two kinds, and they go to different places:

- `failure`: something is broken now (a source read failing, a channel
  exhausted, the scheduler not running). These open an incident.
- `stale_evidence`: a physical test (did the phone actually ring?) has aged
  past its validity. Nothing is broken; a person should test again. These go
  to the retest channel and never open an incident.

Incident rules: one ⚠ when an incident opens, silence while its faults
change, and one ✅ only after the faults have stayed clear for the settle
window. A fault that returns inside the window re-opens the same incident
without a message, so a flapping check produces one ⚠ and at most one ✅.

Retest rules: one 🟡 when an item becomes due, another only when a new item
joins the due set, and one 🟢 when every announced item has passed again.
Missing acceptance data is unknown and changes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json

FAILURE, STALE = "failure", "stale_evidence"


@dataclass(frozen=True)
class Notice:
    channel: str        # "incident" or "retest"
    kind: str           # "open", "recovered", "retest_due", "retest_cleared"
    text: str
    key: str            # stable idempotency key for the sender


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


class HealthGuard:
    def __init__(self, state: dict | None = None, *, settle: timedelta = timedelta(minutes=30)):
        self.state = state if state is not None else {}
        self.state.setdefault("incident", None)
        self.state.setdefault("retest", {"announced": []})
        self.settle = settle

    def evaluate(self, faults: dict[str, str] | None, now: datetime) -> list[Notice]:
        """faults maps a code to FAILURE or STALE. None means the check itself could not run."""
        if faults is None:
            return []  # unknown is not healthy and not broken; the caller alerts on a stale check
        unknown = {code: kind for code, kind in faults.items() if kind not in (FAILURE, STALE)}
        if unknown:
            raise ValueError(f"unclassified faults: {sorted(unknown)}")
        failures = sorted(code for code, kind in faults.items() if kind == FAILURE)
        stale = sorted(code for code, kind in faults.items() if kind == STALE)
        return self._incident(failures, now) + self._retest(stale)

    def _incident(self, failures: list[str], now: datetime) -> list[Notice]:
        inc = self.state["incident"]
        if failures:
            if inc is None or inc["status"] == "recovered":
                inc = {"id": "inc-" + _key(now.isoformat(), failures), "status": "open",
                       "opened_at": now.isoformat(), "seen": list(failures), "reopened": 0}
                self.state["incident"] = inc
                text = "⚠ Alert bridge problem: " + ", ".join(failures)
                return [Notice("incident", "open", text, _key(inc["id"], "open"))]
            if inc["status"] == "clearing":
                inc["reopened"] += 1  # a flap inside the settle window: same incident, no message
            inc["status"] = "open"
            inc.pop("clear_since", None)
            inc["seen"] = sorted(set(inc["seen"]) | set(failures))
            return []
        if inc is None or inc["status"] == "recovered":
            return []
        if inc["status"] == "open":
            inc.update(status="clearing", clear_since=now.isoformat())
            return []
        if now - datetime.fromisoformat(inc["clear_since"]) < self.settle:
            return []
        inc.update(status="recovered", recovered_at=now.isoformat())
        text = "✅ Alert bridge recovered: " + ", ".join(inc["seen"])
        return [Notice("incident", "recovered", text, _key(inc["id"], "recovered"))]

    def _retest(self, due: list[str]) -> list[Notice]:
        record = self.state["retest"]
        announced = set(record["announced"])
        if due:
            new = set(due) - announced
            # Items that passed again drop out quietly; the 🟢 waits for all of them.
            record["announced"] = sorted(due)
            if not new:
                return []
            text = "🟡 Retest due (nothing is broken): " + ", ".join(due)
            return [Notice("retest", "retest_due", text, _key("retest", due))]
        if not announced:
            return []
        record["announced"] = []
        text = "🟢 Retest complete: " + ", ".join(sorted(announced)) + " passed again"
        return [Notice("retest", "retest_cleared", text, _key("retest-clear", sorted(announced)))]
