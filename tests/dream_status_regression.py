import sys; sys.dont_write_bytecode = True
"""Dream CLI to next-session notice: missing checks remain unknown, never clean."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import date

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
        # 第 8 節從家目錄推口袋庫、第 11 節讀設定的上限鍵：不指進暫存目錄，這份回歸
        # 就會去掃跑測試的人的真實家目錄，結果隨機器而異。
        home = self.root / "home"
        (home / memspec.HOST_STATE_DIRECTORY / memspec.HOST_PROJECTS_DIRECTORY).mkdir(parents=True)
        config = self.root / "config.json"
        config.write_text(json.dumps({memspec.CONFIG_VAULTS_FIELD: [str(self.vault)]}), encoding="utf-8")
        for name, value in (("HOME", str(home)), ("USERPROFILE", str(home)),
                            (memspec.EPITYPE_CONFIG_ENV, str(config))):
            patched = patch.dict(os.environ, {name: value})
            patched.start()
            self.addCleanup(patched.stop)

    def run_pack(self, *args, vaults=None):
        code = dream.main(["--json", "--out", str(self.pack), *args,
                           *map(str, vaults or [self.vault])], output=io.StringIO())
        self.assertEqual(code, 0)  # Partial packs remain useful delivered artifacts.
        state = json.loads(self.state.read_text(encoding="utf-8"))
        notice = start._dream_notice(self.vault, "startup", self.settings)
        return state, notice

    def assert_incomplete(self, state, notice):
        self.assertIsNotNone(notice)
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
        # 宿主同步先跑；其餘節超時要逐一留下缺口紀錄。
        self.assertEqual(len(state["section_errors"]), len(dream._SECTION_IDS) - 1)
        self.assertNotIn("14", state["section_errors"])
        self.assertIn(str(dream.REVIEW_PACK_SECTION_ID), state["section_errors"])
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

    def test_successful_empty_scan_says_nothing_and_legacy_unknown_is_not_clean(self):
        # §35：跑完而且乾淨＝沒有人要做任何事，開場就不出聲；「跑過了」本身不是通知。
        state, notice = self.run_pack()
        self.assertTrue(state.get("complete"))
        self.assertEqual(state["section_errors"], {})
        self.assertIsNone(notice)
        del state["complete"]
        state.pop("notified_at", None)
        self.state.write_text(json.dumps(state), encoding="utf-8")
        notice = start._dream_notice(self.vault, "startup", self.settings)
        self.assertIn("未完整檢查", notice)
        self.assertNotIn("沒有待處理項", notice)

    def test_sync_and_review_finish_before_expensive_replay(self):
        order = []

        def section(number):
            def run(*_args):
                order.append(number)
                return {"counts": {}, "examples": [], "errors": []}
            return run

        sections = tuple((number, title, section(number))
                         for number, title, _fn in dream._SECTIONS)
        with patch.object(dream, "_SECTIONS", sections), patch.object(
                dream, "_section_review_pack", side_effect=lambda *_args: section(15)()):
            report = dream.build_report([self.vault], today=date(2026, 9, 27), config={})
        self.assertEqual(order, [14, *range(1, 13), 15, 13])
        self.assertEqual([part["id"] for part in report["sections"]], list(range(1, 16)))

    def test_compliance_checkpoint_survives_partial_retry(self):
        prior = {memspec.DREAM_STATE_COMPLETED_EPOCH_FIELD: 1000.0,
                 memspec.DREAM_STATE_ELAPSED_FIELD: 40.0,
                 memspec.DREAM_STATE_SECTIONS_FIELD: {"13": {"transcripts_skipped": 0}},
                 memspec.DREAM_STATE_ERRORS_FIELD: {"14": {"error": "budget"}}}
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps(prior), encoding="utf-8")
        self.assertEqual(dream._previous_compliance_start(self.state, 2000.0), 960.0)
        sections = [{"id": number, "counts": {}, "error": None, "errors": []}
                    for number in range(1, 16)]
        sections[12]["counts"] = {"transcripts_skipped": 2}
        report = {"today": "2026-09-27", "sections": sections}
        dream._write_run_state(self.state, report, self.pack, 10.0, 2000.0)
        retained = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(retained[dream.COMPLIANCE_CHECKPOINT_FIELD], 960.0)
        sections[12]["counts"]["transcripts_skipped"] = 0
        dream._write_run_state(self.state, report, self.pack, 10.0, 2100.0)
        advanced = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(advanced[dream.COMPLIANCE_CHECKPOINT_FIELD], 2100.0)

    def test_read_only_compliance_never_updates_gate_health(self):
        vault = self.root / "home" / ".claude" / "projects" / "task" / "memory"
        vault.mkdir(parents=True)
        since = 1_790_000_000.0
        observed = []

        def transcripts(_roots, since_stamp=None):
            observed.append(since_stamp)
            return [], 0

        with patch("epitype.compliance.transcripts_for", side_effect=transcripts), \
             patch("epitype.compliance.update_health", side_effect=AssertionError("health write")), \
             patch("epitype.recall_quiet.update", side_effect=AssertionError("quiet write")):
            result = dream._section_compliance([vault], date(2026, 9, 27),
                                               date(2026, 6, 29), {},
                                               {"compliance_since_stamp": since})
        self.assertEqual(observed, [since])
        self.assertEqual(result["errors"], [])

    def test_cli_sync_precedes_view_and_index_work(self):
        order = []

        def sync(*_args, **_kwargs):
            order.append("sync")
            return {"counts": {"drifted": 0, "written": 0},
                    "examples": [], "commands": [], "errors": []}

        def views(_vault):
            order.append("views")

        def shape(*_args, **_kwargs):
            order.append("shape")
            return {"status": "unchanged", "kept": 0}

        with patch.object(dream, "_section_host_sync", side_effect=sync), \
             patch.object(dream, "_views_module") as view_module, \
             patch.object(dream, "shape_index", side_effect=shape):
            view_module.return_value.generate.side_effect = views
            self.run_pack()
        self.assertEqual(order[:3], ["sync", "views", "shape"])

    def test_capped_replay_is_incomplete_and_does_not_write_health(self):
        vault = self.root / "home" / ".claude" / "projects" / "task" / "memory"
        vault.mkdir(parents=True)
        with patch("epitype.compliance.transcripts_for", return_value=([], 1)), \
             patch("epitype.compliance.update_health", side_effect=AssertionError("health write")), \
             patch("epitype.recall_quiet.update", side_effect=AssertionError("quiet write")):
            result = dream._section_compliance([vault], date(2026, 9, 27),
                                               date(2026, 6, 29), {},
                                               {"write_health": True,
                                                "compliance_since_stamp": 1_790_000_000.0})
        self.assertEqual(result["counts"]["transcripts_skipped"], 1)
        report = {"sections": [{"id": number, "error": None, "errors": [],
                                "counts": result["counts"] if number == 13 else {}}
                               for number in range(1, 16)]}
        self.assertIn("13", dream._report_errors(report))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
