"""No-clobber publication, rollback, and stale-reader regression tests."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os
import tempfile
import unittest
from unittest.mock import patch

from epitype import card_io


class CardIOTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.source = self.root / "source.md"
        self.target = self.root / "target.md"
        self.source.write_bytes(b"original")

    def tearDown(self):
        self.directory.cleanup()

    def test_same_path_is_unchanged(self):
        self.assertFalse(card_io.move(self.source, self.source, b"replacement"))
        self.assertEqual(self.source.read_bytes(), b"original")

    def test_existing_target_preserves_both(self):
        self.target.write_bytes(b"history")
        with self.assertRaises(FileExistsError):
            card_io.move(self.source, self.target)
        self.assertEqual(self.source.read_bytes(), b"original")
        self.assertEqual(self.target.read_bytes(), b"history")
        self.assertFalse(list(self.root.glob("*.recovery")))

    def test_racing_target_is_never_replaced(self):
        link = os.link
        def competing_writer(source, target):
            if Path(target) == self.target:
                self.target.write_bytes(b"other writer")
            return link(source, target)
        with patch.object(card_io.os, "link", competing_writer):
            with self.assertRaises(FileExistsError):
                card_io.move(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"other writer")
        self.assertEqual(self.source.read_bytes(), b"original")

    def test_new_source_is_not_deleted(self):
        publish = card_io.publish
        def new_source(target, payload):
            self.source.write_bytes(b"new source")
            return publish(target, payload)
        with patch.object(card_io, "publish", new_source):
            self.assertTrue(card_io.move(self.source, self.target, b"annotated"))
        self.assertEqual(self.target.read_bytes(), b"annotated")
        self.assertEqual(self.source.read_bytes(), b"new source")

    def test_failed_rollback_preserves_recovery(self):
        def failed_publish(*args):
            self.source.write_bytes(b"new source")
            raise OSError("disk failure")
        with patch.object(card_io, "publish", failed_publish):
            with self.assertRaisesRegex(card_io.CardConflict, "original preserved at"):
                card_io.move(self.source, self.target)
        self.assertEqual(self.source.read_bytes(), b"new source")
        self.assertEqual([p.read_bytes() for p in self.root.glob("*.recovery")], [b"original"])

    def test_stale_update_and_stale_move_are_rejected(self):
        self.source.write_bytes(b"owner edit")
        with self.assertRaises(card_io.CardConflict):
            card_io.replace_if_unchanged(self.source, b"new", b"original")
        with self.assertRaises(card_io.CardConflict):
            card_io.move(self.source, self.target, expected=b"original")
        self.assertEqual(self.source.read_bytes(), b"owner edit")
        self.assertFalse(self.target.exists())

    def test_update_and_move_preserve_complete_bytes(self):
        card_io.replace_if_unchanged(self.source, b"updated\r\n", b"original")
        self.assertTrue(card_io.move(self.source, self.target, expected=b"updated\r\n"))
        self.assertEqual(self.target.read_bytes(), b"updated\r\n")
        self.assertFalse(self.source.exists())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
