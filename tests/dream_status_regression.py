import sys; sys.dont_write_bytecode = True
"""Dream CLI to next-session notice: missing checks remain unknown, never clean."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import dream, memspec
import sessionstart_hook as start


class DreamStatusRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-dream-status-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.pack = self.vault / ".epitype" / "pack.json"
        self.state = self.pack.parent / memspec.DREAM_STATE_FILENAME
        self.settings = {memspec.DREAM_MODE_FIELD: memspec.DREAM_MODE_NIGHTLY}

    def run_pack(self, *args, vaults=None):
        code = dream.main(["--json", "--out", str(self.pack), *args,
                           *map(str, vaults or [self.vault])], output=io.StringIO())
        self.assertEqual(code, 0)  # Partial packs remain useful delivered artifacts.
        state = json.loads(self.state.read_text(encoding="utf-8"))
        notice = start._dream_notice(self.vault, "startup", self.settings)
        self.assertIsNotNone(notice)
        return state, notice

    def assert_incomplete(self, state, notice):
        self.assertNotIn("沒有待處理項", notice)
        self.assertIn("未完整檢查", notice)
        self.assertIs(state.get("complete"), False)
        self.assertTrue(state.get("section_errors"))
        self.assertFalse(dream.due(state, 24))  # No immediate piggyback retry loop.
        self.assertIsNone(start._dream_notice(self.vault, "startup", self.settings))

    def test_exhausted_cli_budget_is_not_clean(self):
        ticks = iter([0.0])
        with patch.object(dream.time, "monotonic", side_effect=lambda: next(ticks, 1.0)):
            state, notice = self.run_pack("--time-budget-seconds", "0.001")
        self.assert_incomplete(state, notice)
        self.assertEqual(len(state["section_errors"]), 7)
        self.assertTrue(all(row["error"] == dream.TIME_BUDGET_ERROR
                            for row in state["section_errors"].values()))
        pack = json.loads(self.pack.read_text(encoding="utf-8"))
        self.assertTrue(any("盤點未完成" in step for step in pack["next_steps"]))
        self.assertFalse(any("目前沒有" in step for step in pack["next_steps"]))

    def test_one_failed_section_preserves_other_results(self):
        draft = self.vault / "_drafts" / "one.md"
        draft.parent.mkdir()
        draft.write_text("synthetic draft", encoding="utf-8")
        def broken(*_args):
            raise OSError("synthetic unreadable section")
        sections = tuple((sid, title, broken if sid == 2 else fn)
                         for sid, title, fn in dream._SECTIONS)
        with patch.object(dream, "_SECTIONS", sections):
            state, notice = self.run_pack()
        self.assert_incomplete(state, notice)
        self.assertEqual(set(state["section_errors"]), {"2"})
        self.assertEqual(state["sections"]["4"]["total_drafts"], 1)

    def test_one_vault_failure_keeps_the_error_and_completed_vault(self):
        other = self.root / "other"
        other.mkdir()
        original = dream.alias_batch._export_candidates
        def failing(vault, *args):
            if vault == other:
                raise PermissionError("synthetic vault denied")
            return original(vault, *args)
        with patch.object(dream.alias_batch, "_export_candidates", failing):
            state, notice = self.run_pack(vaults=[self.vault, other])
        self.assert_incomplete(state, notice)
        self.assertIn(str(other), state["section_errors"]["1"]["errors"][0])
        self.assertEqual(state["sections"]["1"]["missing_aliases"], 0)

    def test_successful_empty_scan_is_clean_and_legacy_unknown_is_not(self):
        state, notice = self.run_pack()
        self.assertTrue(state.get("complete"))
        self.assertEqual(state["section_errors"], {})
        self.assertIn("沒有待處理項", notice)
        del state["complete"]
        state.pop("notified_at", None)
        self.state.write_text(json.dumps(state), encoding="utf-8")
        notice = start._dream_notice(self.vault, "startup", self.settings)
        self.assertIn("未完整檢查", notice)
        self.assertNotIn("沒有待處理項", notice)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
