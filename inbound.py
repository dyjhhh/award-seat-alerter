"""One durable inbound queue, one consumer of the chat provider.

A chat bot provider hands each update to whichever client asks first. A second
process polling the same bot silently steals replies from the first, so the
alert bridge never polls the provider. The existing chat listener is the only
consumer; it appends what it receives to this queue. The bridge gets a
read-only view and looks for an acknowledgement there.

ACK means "seen" and nothing else. It never books, pays, transfers, cancels or
contacts anyone.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3

DEFAULT_TOKEN = "ACK"


@contextmanager
def _session(conn):
    """Commit or roll back like `with conn`, then close the connection."""
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def is_exact_ack(text: str | None, token: str = DEFAULT_TOKEN) -> bool:
    """Only the bare token, ignoring case and surrounding whitespace."""
    return isinstance(text, str) and text.strip().upper() == token.upper()


@dataclass(frozen=True)
class Message:
    update_id: int
    text: str
    received_at: datetime


class InboundQueue:
    """Writer side. Construct this only in the process that owns the chat listener."""

    def __init__(self, path):
        self.path = str(path)
        with _session(self._connect()) as db:
            db.execute("CREATE TABLE IF NOT EXISTS inbound ("
                       "update_id INTEGER PRIMARY KEY, text TEXT NOT NULL, received_at TEXT NOT NULL)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5, isolation_level="IMMEDIATE")

    def append(self, update_id: int, text: str, received_at: datetime) -> bool:
        """Record one update. Replays of a known id are ignored; ids must not go backwards."""
        if type(update_id) is not int or update_id < 0 or received_at.tzinfo is None:
            raise ValueError("update id must be a non-negative int and time must be aware")
        with _session(self._connect()) as db:
            row = db.execute("SELECT text FROM inbound WHERE update_id = ?", (update_id,)).fetchone()
            if row is not None:
                if row[0] != text:
                    raise ValueError("update id reused with different text")
                return False
            top = db.execute("SELECT MAX(update_id) FROM inbound").fetchone()[0]
            if top is not None and update_id < top:
                raise ValueError("update ids must not go backwards")
            db.execute("INSERT INTO inbound VALUES (?, ?, ?)",
                       (update_id, text, received_at.astimezone(timezone.utc).isoformat()))
        return True

    def reader(self) -> "InboundReader":
        return InboundReader(self.path)


class InboundReader:
    """Read-only view. It has no way to consume, delete or acknowledge updates."""

    def __init__(self, path):
        self._uri = "file:" + str(path) + "?mode=ro"

    def messages(self, *, not_after: datetime | None = None) -> list[Message]:
        with _session(sqlite3.connect(self._uri, uri=True, timeout=5)) as db:
            rows = db.execute("SELECT update_id, text, received_at FROM inbound ORDER BY update_id").fetchall()
        out = [Message(r[0], r[1], datetime.fromisoformat(r[2])) for r in rows]
        if not_after is not None:
            out = [m for m in out if m.received_at <= not_after]
        return out

    def latest_ack(self, *, not_after: datetime, token: str = DEFAULT_TOKEN) -> datetime | None:
        acks = [m.received_at for m in self.messages(not_after=not_after) if is_exact_ack(m.text, token)]
        return max(acks) if acks else None
