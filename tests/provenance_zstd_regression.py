"""Native compressed sources prove the same bounded original record, not an archive copy."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import copy
import hashlib
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from epitype import rule_provenance as p
try:
    import zstandard
except ImportError:
    zstandard = None


@unittest.skipIf(zstandard is None, "native optional zstandard support unavailable")
class CompressedSources(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "original.jsonl"
        self.rows = [json.dumps({"type": "session_meta", "payload": {"id": "test-session"}}).encode()+b"\n",
                     json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "Keep the original source."}]}}).encode()+b"\n"]
        self.raw = b"".join(self.rows)
        self.path.write_bytes(self.raw)
        self.zst = self.path.with_suffix(".jsonl.zst")
        self.zst.write_bytes(zstandard.ZstdCompressor().compress(self.raw))
        self.source = {"path": str(self.path), "line": 2, "record_sha256": hashlib.sha256(self.rows[1]).hexdigest(),
                       "session_id": "test-session", "role": "U"}

    def read(self, source=None, remaining=1024*1024):
        return p._original(source or self.source, self.root, {}, p._ReadBudget(time.monotonic()+5, remaining))

    def test_explicit_and_missing_plain_sidecar_return_the_same_original(self):
        original = self.read()
        source = {**self.source, "path": str(self.zst)}
        self.assertEqual(self.read(source), original)
        self.path.unlink()
        self.assertEqual(self.read(), original)

    def test_byte_offset_preserves_record_boundary_and_identity(self):
        source = {**self.source, "path": str(self.zst), "byte_offset": len(self.rows[0])}
        self.assertEqual(self.read(source)[0], "U")
        source["byte_offset"] += 1
        with self.assertRaises(ValueError): self.read(source)

    def test_wrong_hash_role_and_session_never_pass(self):
        for name, value in (("record_sha256", "0"*64), ("role", "A"), ("session_id", "other")):
            source = {**self.source, "path": str(self.zst), name: value}
            with self.assertRaises(ValueError): self.read(source)

    def test_decompressed_budget_and_missing_or_damaged_record_fail(self):
        source = {**self.source, "path": str(self.zst)}
        with self.assertRaises(OSError): self.read(source, remaining=32)
        self.zst.write_bytes(self.zst.read_bytes()[:16])
        with self.assertRaises((OSError, ValueError)): self.read(source)

    def test_existing_plain_source_is_not_silently_replaced_by_sidecar(self):
        self.path.write_bytes(self.raw.replace(b"Keep", b"Drop"))
        with self.assertRaises(ValueError): self.read()

    def test_known_over_budget_offset_does_not_rescan_the_compressed_prefix(self):
        source={**self.source,'path':str(self.zst),'byte_offset':500*1024*1024}
        with patch.object(p,'_read_row',side_effect=AssertionError('impossible prefix rescan')):
            with self.assertRaises(p._ReadBudgetExhausted):self.read(source)


if __name__ == "__main__": unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
