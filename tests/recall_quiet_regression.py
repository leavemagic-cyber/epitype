"""Quieting must be based on complete, vault-specific observations."""
import sys
sys.dont_write_bytecode = True
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import memspec, recall_quiet


class RecallQuietRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-quiet-scope-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vaults = [self.root / "first vault", self.root / "second vault"]
        for vault in self.vaults:
            vault.mkdir()
            (vault / "shared.md").write_text(
                "---\nname: shared\ndescription: example\nmetadata:\n  type: reference\n---\nbody\n",
                encoding="utf-8")

    def transcript(self, rows):
        path = self.root / "session.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def injection(text, stamp="2026-10-21T12:00:00Z"):
        return {"type": "attachment", "timestamp": stamp,
                "attachment": {"type": "hook_additional_context", "content": [text]}}

    def test_same_filename_in_two_vaults_keeps_separate_counts(self):
        first, second = self.vaults
        rows = [self.injection(f"vaults: V1={first} V2={second}\n- first | V1/shared.md"),
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "I read shared.md"}]}},
                self.injection("- second | V2/shared.md")]
        shown = recall_quiet.measure([self.transcript(rows)], by_vault=True)
        self.assertEqual(shown, {(str(first), "shared.md"): [1, 1],
                                 (str(second), "shared.md"): [1, 0]})

    def test_legend_before_time_window_still_identifies_later_delivery(self):
        vault = self.vaults[0]
        rows = [self.injection(f"vaults: V1={vault}", "2026-10-20T12:00:00Z"),
                self.injection("- later | V1/shared.md")]
        self.assertEqual(recall_quiet.measure([self.transcript(rows)], since="2026-10-21",
                                              by_vault=True),
                         {(str(vault), "shared.md"): [1, 0]})

    def test_alias_reassignment_uses_new_vault(self):
        rows = [self.injection(f"vaults: V1={vault}\n- item | V1/shared.md")
                for vault in self.vaults]
        self.assertEqual(recall_quiet.measure([self.transcript(rows)], by_vault=True),
                         {(str(vault), "shared.md"): [1, 0] for vault in self.vaults})

    def test_compaction_does_not_reuse_an_unconfirmed_vault_alias(self):
        rows = [self.injection(f"vaults: V1={self.vaults[0]}\n- first | V1/shared.md"),
                {"type": "system", "subtype": "compact_boundary"},
                self.injection("- unresolved | V1/shared.md")]
        diagnostics = []
        shown = recall_quiet.measure([self.transcript(rows)], by_vault=True,
                                     diagnostics=diagnostics)
        self.assertEqual(shown, {(str(self.vaults[0]), "shared.md"): [1, 0]})
        self.assertTrue(diagnostics)

    def test_truncated_or_unreadable_transcript_cannot_establish_non_use(self):
        path = self.transcript([self.injection("- first | V1/shared.md"),
                                {"type": "assistant", "message": {"content": [
                                    {"type": "text", "text": "shared.md"}]}}])
        diagnostics = []
        self.assertEqual(recall_quiet.measure([path], max_bytes=path.stat().st_size - 1,
                                              diagnostics=diagnostics), {})
        self.assertTrue(diagnostics)
        diagnostics.clear()
        self.assertEqual(recall_quiet.measure([self.root / "missing.jsonl"],
                                              diagnostics=diagnostics), {})
        self.assertTrue(diagnostics)

    def test_current_behavior_or_unreadable_card_is_never_quieted(self):
        vault = self.vaults[0]
        recall_quiet.update(vault, {"shared.md": [memspec.RECALL_QUIET_MIN_SEGMENTS, 0]}, date.today())
        candidates = recall_quiet.quiet_cards(vault)
        self.assertTrue(recall_quiet.should_quiet(vault, "shared.md", candidates))
        path = vault / "shared.md"
        path.write_text(path.read_text(encoding="utf-8").replace("type: reference", "type: feedback"),
                        encoding="utf-8")
        self.assertFalse(recall_quiet.should_quiet(vault, "shared.md", candidates))
        path.unlink()
        self.assertFalse(recall_quiet.should_quiet(vault, "shared.md", candidates))

    def test_legacy_unscoped_cache_cannot_suppress_cards(self):
        vault = self.vaults[0]
        path = recall_quiet.health_path(vault)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"days": {date.today().isoformat(): {"shared.md": [99, 0]}},
                                    "quiet": ["shared.md"]}), encoding="utf-8")
        self.assertEqual(recall_quiet.quiet_cards(vault), frozenset())
        recall_quiet.update(vault, {}, date.today())
        self.assertEqual(recall_quiet.quiet_cards(vault), frozenset())


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
