"""Source selection, honest coverage and exact original-row integrity."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import source_lookup as source


class LookupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "conversation.jsonl"
        self.rows = [
            {"type": "user", "message": {"content": "Layout: provisionally compact"}},
            {"type": "user", "isCompactSummary": True, "message": {"content": "Layout: owner chose wide"}},
            {"type": "attachment", "attachment": {"type": "queued_command",
             "origin": {"kind": "human"}, "prompt": "Layout: compare per-device variants"}},
            {"type": "assistant", "message": {"content": "Layout: my new recommendation"}},
        ]
        self.raw = [(json.dumps(row) + "\n").encode() for row in self.rows]
        self.path.write_bytes(b"".join(self.raw))

    def test_user_lookup_preserves_later_queue_not_summary_or_recommendation(self):
        result = source.lookup(self.path, query="LAYOUT", role="user")
        self.assertEqual([1, 3], [r["line"] for r in result["records"]])
        self.assertTrue(result["scan_complete"])
        self.assertEqual(1, result["non_message_rows"])

    def test_latest_limit_discloses_omitted_matches(self):
        result = source.lookup(self.path, query="Layout", limit=1)
        self.assertEqual(4, result["records"][0]["line"])
        self.assertEqual(2, result["matches_omitted"])

    def test_exact_line_and_digest_do_not_borrow_another_records_words(self):
        result = source.lookup(self.path, line=3, limit=1)
        record = result["records"][0]
        self.assertEqual(self.rows[2]["attachment"]["prompt"], record["text"])
        self.assertEqual(hashlib.sha256(self.raw[2]).hexdigest(), record["record_sha256"])
        self.assertFalse(result["scan_complete"])

    def test_offsets_are_explicit_and_must_be_line_boundaries(self):
        result = source.lookup(self.path, offset=sum(map(len, self.raw[:2])), line=1, limit=1)
        self.assertEqual("Q", result["records"][0]["role"])
        self.assertEqual("relative-to-offset", result["line_base"])
        with self.assertRaises(ValueError):
            source.lookup(self.path, offset=1)

    def test_partial_scan_cannot_report_complete_absence(self):
        result = source.lookup(self.path, query="variants", max_bytes=len(self.raw[0]) + 10)
        self.assertEqual([], result["records"])
        self.assertFalse(result["scan_complete"])
        self.assertEqual(len(self.raw[0]), result["next_offset"])
        self.assertIn("no global absence", result["scope"])

    def test_timeout_and_malformed_rows_are_explicit(self):
        with patch.object(source.time, "monotonic", side_effect=[0, 4]):
            self.assertFalse(source.lookup(self.path)["scan_complete"])
        self.path.write_bytes(b"bad json\n" + self.raw[-1])
        self.assertEqual(1, source.lookup(self.path)["unreadable_rows"])

    def test_snapshot_change_invalidates_complete_scan(self):
        before = self.path.stat()
        after = type("Changed", (), {"st_size": before.st_size + 1, "st_mtime_ns": before.st_mtime_ns})()
        with patch.object(Path, "stat", side_effect=[before, after]):
            self.assertFalse(source.lookup(self.path)["scan_complete"])

    def test_preview_and_output_caps_are_disclosed(self):
        self.path.write_text("\n".join(json.dumps({"type": "user", "message": {"content": "中" * 1200}})
                                      for _ in range(8)), encoding="utf-8")
        result = json.loads(source.encode(source.lookup(self.path, limit=8)))
        self.assertTrue(all(r["text_truncated"] for r in result["records"]))
        self.assertGreater(result["matches_omitted"], 0)
        self.assertLessEqual(len(source.encode(result).encode()), source.OUTPUT_BYTES)

    def test_real_cli_and_source_bytes_are_unchanged(self):
        before = self.path.read_bytes()
        result = subprocess.run([sys.executable, "-m", "epitype", "source", str(self.path),
                                 "--role", "user", "--find", "variants"], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Q", json.loads(result.stdout)["records"][0]["role"])
        self.assertEqual(before, self.path.read_bytes())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
