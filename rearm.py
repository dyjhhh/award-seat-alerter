"""Re-arm one-shot provider alerts, gated on a real hit and on exact evidence.

Some provider alerts fire once and then expire. Keeping one alive means
creating a replacement after each hit. Creating alerts from a guess is how a
monitor ends up with duplicates, widened searches, or alerts nobody approved,
so every step here is narrow:

1. `on_hit` opens a request only for a fresh source hit that names a tracked
   one-shot alert. A tracked alert that merely disappears opens nothing.
2. `claim` hands the request to one worker with a token and a lease. Attempts
   are bounded; the request is quarantined when they run out.
3. `authorize` needs a provider snapshot taken during this claim. It finishes
   without recreating anything when the alert, or one equivalent alert, is
   still active, and quarantines on more than one. The expired page must show
   exactly one row equal to the frozen spec, or 2 to 20 rows that are all
   identical to it. Any other picture is ambiguous and is quarantined.
4. `complete` reads the replacement back. It must be a new id, active, and
   equal to the frozen spec in every field.

Continuous alerts are never touched. This module never talks to the provider;
an adapter does the creation and reports what it read back.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile

ACTIVE_REQUESTS = ("REVIEW", "CLAIMED", "AUTHORIZED")
IDENTICAL_ROWS_MIN, IDENTICAL_ROWS_MAX = 2, 20
DISPLAYED_FIELDS = ("label", "program", "origin", "destination", "dates", "cabins", "caps")


class GateError(ValueError):
    """The caller does not hold the request, or the request has moved on."""


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def displayed(spec: dict) -> dict:
    """What the provider's expired-alerts page shows for a spec (it shows no ids)."""
    return {"label": spec["label"], "program": spec["program"], "origin": spec["origin"],
            "destination": spec["destination"], "dates": f"{spec['date_from']} - {spec['date_to']}",
            "cabins": sorted(spec["cabins"]),
            "caps": f"max {spec['max_points']:,} points, max {spec['max_fees_cents'] / 100:.2f} fees"}


class RearmGate:
    def __init__(self, path, *, lease: timedelta = timedelta(minutes=10), max_attempts: int = 3,
                 source_max_age: timedelta = timedelta(hours=2), snapshot_max_age: timedelta = timedelta(minutes=5),
                 retry_backoff: timedelta = timedelta(minutes=2)):
        self.path = Path(path)
        self.lease, self.max_attempts = lease, max_attempts
        self.source_max_age, self.snapshot_max_age = source_max_age, snapshot_max_age
        self.retry_backoff = retry_backoff

    # -- storage -------------------------------------------------------------
    @contextmanager
    def _state(self):
        lock = self.path.with_suffix(".lock")
        with open(lock, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                state = (json.loads(self.path.read_text()) if self.path.exists()
                         else {"alerts": {}, "requests": {}, "seen_sources": [], "notices": []})
                yield state
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".rearm-")
                with os.fdopen(fd, "w") as out:
                    json.dump(state, out, indent=1, sort_keys=True)
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def snapshot(self) -> dict:
        with self._state() as state:
            return json.loads(json.dumps(state))

    # -- registry ------------------------------------------------------------
    def register(self, alert_id: str, spec: dict, *, kind: str) -> None:
        if kind not in ("one_shot", "continuous"):
            raise ValueError("kind must be one_shot or continuous")
        displayed(spec)  # rejects a spec missing a displayed field
        with self._state() as state:
            if alert_id in state["alerts"]:
                raise ValueError("alert already registered")
            state["alerts"][alert_id] = {"spec": spec, "fingerprint": fingerprint(spec),
                                         "kind": kind, "status": "ACTIVE", "observed_at": None}

    @staticmethod
    def _quarantine(state, request, reason, now) -> dict:
        request.update(state="QUARANTINED", reason=reason, closed_at=now.isoformat())
        notice = {"request_id": request["request_id"], "alert_id": request["alert_id"],
                  "reason": reason, "at": now.isoformat()}
        state["notices"].append(notice)
        return {"status": "QUARANTINED", "reason": reason}

    # -- 1. hit --------------------------------------------------------------
    def on_hit(self, *, alert_id: str, source_id: str, source_at: datetime, now: datetime) -> dict:
        if source_at > now or now - source_at > self.source_max_age:
            return {"status": "IGNORED", "reason": "source_not_fresh"}
        with self._state() as state:
            alert = state["alerts"].get(alert_id)
            if alert is None:
                return {"status": "IGNORED", "reason": "alert_not_tracked"}
            if alert["kind"] != "one_shot":
                return {"status": "IGNORED", "reason": "continuous_alert_never_rearmed"}
            if source_id in state["seen_sources"]:
                return {"status": "DUPLICATE"}
            state["seen_sources"].append(source_id)
            del state["seen_sources"][:-1000]
            for request in state["requests"].values():
                if request["alert_id"] == alert_id and request["state"] in ACTIVE_REQUESTS:
                    return {"status": "ALREADY_PENDING", "request_id": request["request_id"]}
            request_id = secrets.token_hex(8)
            state["requests"][request_id] = {
                "request_id": request_id, "alert_id": alert_id, "fingerprint": alert["fingerprint"],
                "state": "REVIEW", "attempts": 0, "next_attempt_at": now.isoformat(),
                "source_id": source_id, "source_at": source_at.isoformat(), "history": []}
            return {"status": "REVIEW", "request_id": request_id}

    # -- 2. claim ------------------------------------------------------------
    def claim(self, now: datetime) -> dict:
        with self._state() as state:
            quarantined = []
            ordered = sorted(state["requests"].values(), key=lambda r: (r["source_at"], r["request_id"]))
            for request in ordered:
                if request["state"] not in ACTIVE_REQUESTS:
                    continue
                alert = state["alerts"][request["alert_id"]]
                if alert["fingerprint"] != request["fingerprint"]:
                    quarantined.append(self._quarantine(state, request, "spec_changed", now))
                    continue
                if request["state"] in ("CLAIMED", "AUTHORIZED"):
                    if now < datetime.fromisoformat(request["lease_until"]):
                        continue  # another worker owns it
                    request["history"].append({"at": now.isoformat(), "event": "lease_expired"})
                if request["attempts"] >= self.max_attempts:
                    quarantined.append(self._quarantine(state, request, "maximum_attempts", now))
                    continue
                if now - datetime.fromisoformat(request["source_at"]) > self.source_max_age:
                    quarantined.append(self._quarantine(state, request, "source_expired", now))
                    continue
                if now < datetime.fromisoformat(request["next_attempt_at"]):
                    continue
                request.update(state="CLAIMED", token=secrets.token_hex(16), attempts=request["attempts"] + 1,
                               claimed_at=now.isoformat(), lease_until=(now + self.lease).isoformat())
                return {"status": "CLAIMED", "request_id": request["request_id"], "token": request["token"],
                        "alert_id": request["alert_id"], "spec": alert["spec"], "quarantined": quarantined}
            return {"status": "NO_PENDING", "quarantined": quarantined}

    def _owned(self, state, request_id, token, now, allowed):
        request = state["requests"].get(request_id)
        if (request is None or request["state"] not in allowed or not isinstance(token, str)
                or not secrets.compare_digest(request.get("token", ""), token)
                or now >= datetime.fromisoformat(request["lease_until"])):
            raise GateError("a current, unexpired claim token is required")
        return request, state["alerts"][request["alert_id"]]

    def release(self, request_id: str, token: str, reason: str, now: datetime) -> dict:
        """A worker failed; the request waits and is retried until attempts run out."""
        with self._state() as state:
            request, _ = self._owned(state, request_id, token, now, ("CLAIMED", "AUTHORIZED"))
            request.update(state="REVIEW", token="", next_attempt_at=(now + self.retry_backoff).isoformat())
            request["history"].append({"at": now.isoformat(), "event": "released", "reason": reason})
            return {"status": "REVIEW", "attempts": request["attempts"]}

    # -- 3. authorize ----------------------------------------------------------
    def authorize(self, request_id: str, token: str, *, provider_rows: list[dict],
                  observed_at: datetime, expired_rows: list[dict], now: datetime) -> dict:
        """provider_rows: active alerts as {id, spec}. expired_rows: {marker, fields} as displayed."""
        with self._state() as state:
            request, alert = self._owned(state, request_id, token, now, ("CLAIMED",))
            claimed = datetime.fromisoformat(request["claimed_at"])
            if not claimed <= observed_at <= now or now - observed_at > self.snapshot_max_age:
                raise GateError("the provider snapshot must be taken during this claim")
            spec = alert["spec"]
            ids = [row.get("id") for row in provider_rows]
            if request["alert_id"] in ids:
                request.update(state="DONE", result="still_active", closed_at=now.isoformat())
                return {"status": "DONE", "result": "still_active"}
            equivalents = [row["id"] for row in provider_rows if row.get("spec") == spec]
            if len(equivalents) > 1:
                return self._quarantine(state, request, "duplicate_active_equivalents", now)
            if equivalents:
                request.update(state="DONE", result="equivalent_active", closed_at=now.isoformat(),
                               equivalent_id=equivalents[0])
                return {"status": "DONE", "result": "equivalent_active", "alert_id": equivalents[0]}
            selection = self._select_expired_row(expired_rows, spec)
            if selection is None:
                return self._quarantine(state, request, "expired_row_ambiguous", now)
            request.update(state="AUTHORIZED", authorized_at=now.isoformat(), selection=selection)
            return {"status": "AUTHORIZED", "spec": spec, "selection": selection["kind"],
                    "lease_until": request["lease_until"]}

    @staticmethod
    def _select_expired_row(rows, spec) -> dict | None:
        """SINGLE_ROW, IDENTICAL_EXPIRED_ROWS, or None when the page is ambiguous.

        The expired page shows no alert ids. A one-shot alert that fired twice
        leaves two expired rows. If every candidate row equals the frozen spec
        in every displayed field, recreating from any of them is the same
        action, so it is allowed. Any displayed difference, a missing field,
        or a repeated row marker is ambiguous.
        """
        if not isinstance(rows, list) or not rows or len(rows) > IDENTICAL_ROWS_MAX:
            return None
        expected = displayed(spec)
        markers = []
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"marker", "fields"}
                    or not isinstance(row["fields"], dict) or set(row["fields"]) != set(DISPLAYED_FIELDS)):
                return None
            if any(row["fields"][name] != expected[name] for name in DISPLAYED_FIELDS):
                return None
            if not isinstance(row["marker"], str) or not row["marker"].strip():
                return None
            markers.append(row["marker"])
        if len(set(markers)) != len(markers):
            return None
        kind = "SINGLE_ROW" if len(rows) == 1 else "IDENTICAL_EXPIRED_ROWS"
        if kind == "IDENTICAL_EXPIRED_ROWS" and len(rows) < IDENTICAL_ROWS_MIN:
            return None
        return {"kind": kind, "rows": len(rows), "rows_sha256": fingerprint(rows)}

    # -- 4. complete -----------------------------------------------------------
    def complete(self, request_id: str, token: str, *, readback: dict, now: datetime) -> dict:
        """readback: {id, state, spec} as read from the provider after creation."""
        with self._state() as state:
            request, alert = self._owned(state, request_id, token, now, ("AUTHORIZED",))
            new_id = readback.get("id")
            problems = []
            if not isinstance(new_id, str) or not new_id or new_id in state["alerts"]:
                problems.append("replacement_id_not_new")
            if readback.get("state") != "active":
                problems.append("replacement_not_active")
            if readback.get("spec") != alert["spec"]:
                problems.append("replacement_fields_differ")
            if problems:
                return self._quarantine(state, request, problems[0], now)
            old = request["alert_id"]
            state["alerts"][old]["status"] = "RETIRED"
            state["alerts"][old]["replaced_by"] = new_id
            state["alerts"][new_id] = {"spec": alert["spec"], "fingerprint": alert["fingerprint"],
                                       "kind": "one_shot", "status": "ACTIVE", "replaces": old}
            request.update(state="DONE", result="recreated", replacement_id=new_id, closed_at=now.isoformat())
            return {"status": "DONE", "result": "recreated", "replacement_id": new_id}

    def take_notices(self) -> list[dict]:
        """Quarantine notices, each returned exactly once."""
        with self._state() as state:
            notices, state["notices"] = state["notices"], []
            return notices
