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


    def test_route_racing_target_and_annotation_failure_preserve_source(self):
        destination = self.root / "destination"
        source = self.vault / "grants" / "grant.md"
        source.parent.mkdir()
        source.write_bytes(CARD.encode())
        report = {"entries": [{"card": "grants/grant.md", "target": str(destination),
                  "status": capture_route.ROUTE_MISROUTED, memspec.CWD_FIELD: "synthetic"}]}
        unique = capture_route._unique_target
        def racing_target(*args):
            target, stem = unique(*args)
            target.write_bytes(b"other writer")
            return target, stem
        with patch.object(capture_route, "_unique_target", racing_target):
            counts, _ = capture_route.apply_routes(self.vault, report=report)
        self.assertEqual((counts["moved"], counts["failed"]), (0, 1))
        self.assertEqual(source.read_bytes(), CARD.encode())
        self.assertEqual((destination / "grants/grant.md").read_bytes(), b"other writer")
        with patch.object(capture_route, "_annotated", side_effect=OSError("annotation failed")):
            counts, _ = capture_route.apply_routes(self.vault, report=report)
        self.assertEqual((counts["moved"], counts["failed"]), (0, 1))
        self.assertEqual(source.read_bytes(), CARD.encode())



if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
