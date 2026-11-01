"""Wake the operator on several channels, repeat a finite number of times, stop on ACK.

Each channel (chat bot, phone relay, desktop alarm, ...) is a pluggable
transport with one method, `send(text) -> bool`. True means the transport
accepted the message. It does not mean a person heard it.

Rules this module enforces:

- One escalation per event key. Overlapping provider alert groups that match
  the same seat merge into the escalation that already exists.
- Each channel has two separate budgets: accepted sends (the repeat ceiling)
  and failed attempts. A failing channel never blocks the others.
- The durable inbound queue is checked before every send. An exact ACK closes
  every escalation that was already active when the ACK arrived. Escalations
  opened after it keep going.
- Nothing repeats forever. When every channel has used its budget the
  escalation closes on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
from typing import Protocol

from inbound import InboundReader

FOOTER = "Reply ACK to stop repeats. ACK means seen only: nothing is booked, paid or transferred."


class Transport(Protocol):
    def send(self, text: str) -> bool: ...


@dataclass(frozen=True)
class ChannelPolicy:
    max_sends: int
    max_failures: int
    repeat_every: timedelta
    retry_after: timedelta

    def __post_init__(self):
        if self.max_sends < 1 or self.max_failures < 1:
            raise ValueError("ceilings must be at least 1")
        if self.repeat_every <= timedelta(0) or self.retry_after <= timedelta(0):
            raise ValueError("intervals must be positive")


def _iso(value: datetime) -> str:
    return value.isoformat()


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


class WakeLedger:
    def __init__(self, path, channels: dict[str, ChannelPolicy], *, cooldown: timedelta,
                 ack_token: str = "ACK"):
        if not channels:
            raise ValueError("at least one channel is required")
        self.path = Path(path)
        self.channels = dict(channels)
        self.cooldown = cooldown
        self.ack_token = ack_token
        self.state = self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> dict:
        if not self.path.exists():
            return {"active": {}, "closed": {}}
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict) or set(data) != {"active", "closed"}:
            raise ValueError("wake ledger is malformed")
        return data

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".wake-")
        with os.fdopen(fd, "w") as fh:
            json.dump(self.state, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    # -- opening -----------------------------------------------------------
    def open(self, key: str, text: str, group: str, now: datetime) -> str:
        """OPENED, MERGED (already active) or COOLDOWN (recently closed)."""
        active = self.state["active"].get(key)
        if active is not None:
            if group not in active["groups"]:
                active["groups"].append(group)
                self._save()
            return "MERGED"
        closed = self.state["closed"].get(key)
        if closed is not None and now - _at(closed["first_at"]) < self.cooldown:
            if group not in closed["groups"]:
                closed["groups"].append(group)
                self._save()
            return "COOLDOWN"
        self.state["active"][key] = {
            "key": key, "text": text, "groups": [group], "first_at": _iso(now),
            "channels": {name: {"sent": 0, "failures": 0, "next_at": _iso(now), "status": "PENDING",
                                "last_error": None}
                         for name in self.channels}}
        self._save()
        return "OPENED"

    # -- ticking -----------------------------------------------------------
    def _close(self, key: str, status: str, now: datetime, **extra) -> None:
        record = self.state["active"].pop(key)
        record.update(status=status, closed_at=_iso(now), **extra)
        self.state["closed"][key] = record

    def _apply_ack(self, reader: InboundReader, now: datetime) -> list[str]:
        ack = reader.latest_ack(not_after=now, token=self.ack_token)
        if ack is None:
            return []
        hit = [k for k, r in self.state["active"].items() if _at(r["first_at"]) <= ack]
        for key in hit:
            self._close(key, "ACKED", now, acked_at=_iso(ack))
        return hit

    def tick(self, transports: dict[str, Transport], reader: InboundReader, now: datetime) -> list[dict]:
        """Run every due send once. Returns one result row per attempt or closure."""
        results = [{"key": k, "result": "ACKED"} for k in self._apply_ack(reader, now)]
        for key in sorted(self.state["active"]):
            for name, policy in self.channels.items():
                if key not in self.state["active"]:
                    break  # an ACK seen before this send closed it
                ch = self.state["active"][key]["channels"][name]
                if ch["status"] != "PENDING" or now < _at(ch["next_at"]):
                    continue
                acked = self._apply_ack(reader, now)
                if acked:
                    results += [{"key": k, "result": "ACKED"} for k in acked]
                    if key in acked:
                        break
                record = self.state["active"][key]
                label = f"(repeat {ch['sent'] + 1}/{policy.max_sends}) " if ch["sent"] else ""
                transport = transports.get(name)
                try:
                    if transport is None:
                        raise LookupError("transport_missing")
                    accepted = transport.send(label + record["text"] + "\n" + FOOTER) is True
                    error = None if accepted else "not_accepted"
                except Exception as exc:  # one broken channel must not stop the others
                    accepted, error = False, type(exc).__name__
                if accepted:
                    ch["sent"] += 1
                    ch["next_at"] = _iso(now + policy.repeat_every)
                    if ch["sent"] >= policy.max_sends:
                        ch["status"] = "DONE"
                else:
                    ch["failures"] += 1
                    ch["last_error"] = error
                    ch["next_at"] = _iso(now + policy.retry_after)
                    if ch["failures"] >= policy.max_failures:
                        ch["status"] = "EXHAUSTED"
                results.append({"key": key, "channel": name,
                                "result": "ACCEPTED" if accepted else "FAILED", "error": error})
            if key in self.state["active"]:
                record = self.state["active"][key]
                statuses = [c["status"] for c in record["channels"].values()]
                if "PENDING" not in statuses:
                    delivered = any(c["sent"] for c in record["channels"].values())
                    status = "CEILING" if delivered else "UNDELIVERED"
                    self._close(key, status, now)
                    results.append({"key": key, "result": status})
        self._save()
        return results

    def active_keys(self) -> list[str]:
        return sorted(self.state["active"])

    def closed(self, key: str) -> dict | None:
        return self.state["closed"].get(key)
