from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fixtures import T0
from inbound import InboundQueue, InboundReader, is_exact_ack


class InboundTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "inbound.sqlite"
        self.queue = InboundQueue(self.path)

    def test_only_the_bare_token_is_an_ack(self):
        for text in ("ACK", " ack ", "Ack\n"):
            self.assertTrue(is_exact_ack(text), text)
        for text in ("ACK flight", "ack!", "okay", "A C K", "BACK", "", None, "ACKACK"):
            self.assertFalse(is_exact_ack(text), text)

    def test_the_reader_has_no_way_to_consume_or_write(self):
        reader = self.queue.reader()
        self.assertIsInstance(reader, InboundReader)
        for name in ("append", "consume", "delete", "pop", "mark_read", "get_updates"):
            self.assertFalse(hasattr(reader, name), name)

    def test_reading_never_changes_the_queue(self):
        self.queue.append(10, "hello", T0)
        self.queue.append(11, "ACK", T0 + timedelta(seconds=5))
        before = self.path.read_bytes()
        first = self.queue.reader().messages()
        second = self.queue.reader().messages()
        self.assertEqual(first, second)
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_read_only_connection_refuses_writes(self):
        reader = self.queue.reader()
        from contextlib import closing
        import sqlite3
        with self.assertRaises(sqlite3.OperationalError):
            with closing(sqlite3.connect(reader._uri, uri=True)) as db:
                db.execute("DELETE FROM inbound")

    def test_latest_ack_ignores_lookalikes_and_the_future(self):
        self.queue.append(1, "ACK please stop", T0)
        self.queue.append(2, "ack", T0 + timedelta(minutes=1))
        self.queue.append(3, "ACK", T0 + timedelta(minutes=9))
        reader = self.queue.reader()
        self.assertEqual(reader.latest_ack(not_after=T0 + timedelta(minutes=5)), T0 + timedelta(minutes=1))
        self.assertIsNone(reader.latest_ack(not_after=T0 + timedelta(seconds=30)))

    def test_replayed_updates_are_idempotent_and_ids_cannot_go_backwards(self):
        self.assertTrue(self.queue.append(5, "ACK", T0))
        self.assertFalse(self.queue.append(5, "ACK", T0))
        with self.assertRaises(ValueError):
            self.queue.append(5, "different text", T0)
        with self.assertRaises(ValueError):
            self.queue.append(4, "late", T0)
        self.assertEqual(len(self.queue.reader().messages()), 1)


if __name__ == "__main__":
    unittest.main()
