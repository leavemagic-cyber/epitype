"""Exercise harvest, rehoming, and batch writes through their real entrypoints."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from epitype import alias_batch, capture_route, card_io, card_lint, harvest, memsearch, memspec


CARD = "---\nname: original\ndescription: fixture\ndecision_key: fixture\nstatus: active\n---\nOwner body\n"


class MutationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.card = self.vault / "card.md"
        self.card.write_bytes(CARD.encode("utf-8"))


    def test_harvest_same_path_is_idempotent(self):
        directory = self.vault / harvest.CARD_DIRECTORIES["grant"]
        directory.mkdir()
        path = directory / "grant-20260907-fixture.md"
        path.write_bytes(CARD.encode())
        counts = {"moved": 0}
        self.assertEqual(harvest._move_card(path, self.vault, "grant", "fixture", counts), ("unchanged", path))
        self.assertEqual(path.read_bytes(), CARD.encode())
        self.assertEqual(counts["moved"], 0)


    def test_quarantine_collision_preserves_history(self):
        quarantine = self.root / "quarantine"
        target = quarantine / "grant" / self.card.name
        target.parent.mkdir(parents=True)
        target.write_bytes(b"earlier history")
        counts = {"moved": 0}
        result = harvest._quarantine_drop(self.card, "grant", quarantine, True, counts)
        self.assertIn("FAILED", result)
        self.assertEqual(counts, {"moved": 0, "failed": 1})
        self.assertEqual(target.read_bytes(), b"earlier history")
        self.assertEqual(self.card.read_bytes(), CARD.encode())



if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
