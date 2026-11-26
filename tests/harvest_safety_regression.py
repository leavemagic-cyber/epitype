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

    def test_unreadable_transcript_is_not_recorded_as_harvested(self):
        home = self.root / "home"
        (home / ".epitype").mkdir(parents=True)
        (self.vault / memspec.WORK_LEDGER_FILENAME).write_text("# ledger\n", encoding="utf-8")
        (home / ".epitype" / "config.json").write_text(
            json.dumps({memspec.CONFIG_VAULTS_FIELD: [str(self.vault)]}), encoding="utf-8")
        transcript = home / ".claude" / "projects" / "C--Sample" / "session-one.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({
            "type": "user", "timestamp": "2026-11-01T10:00:00.000Z", "sessionId": "s1",
            "cwd": str(self.root), "message": {"role": "user", "content": "你可以直接改那個測試檔"},
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        real_open = Path.open

        def locked(path, *args, **kwargs):
            if path.name == transcript.name:
                raise PermissionError("locked (synthetic)")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", locked), contextlib.redirect_stdout(io.StringIO()):
            first, *_ = harvest.harvest(home, [self.vault])
        self.assertEqual(first["utterances"], 0)
        self.assertNotIn(str(transcript), harvest.load_manifest(self.vault))
        with contextlib.redirect_stdout(io.StringIO()):
            second, *_ = harvest.harvest(home, [self.vault])
        self.assertEqual((second["files"], second["utterances"]), (1, 1))
        self.assertIn(str(transcript), harvest.load_manifest(self.vault))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
