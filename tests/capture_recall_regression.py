import sys; sys.dont_write_bytecode = True
"""Synthetic capture-to-recall fidelity, history and budget boundaries."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import capture, memsearch, memspec
import _hook_common as common
import recall_hook as recall

HISTORY = "歷史捕捉（非完整對話／現行裁定）"
TAIL = "但是只能在隔離副本操作，禁止修改正式資料"
QUOTE = "fixturehistory 這次先整理" + "相關資料與規則的對照，" * 9 + TAIL


class CaptureRecallRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-capture-recall-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {
            "EPITYPE_CONFIG": str(self.config), "EPITYPE_DREAM_MODE": "off",
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def card(self, quote=QUOTE, kind="ruling", legacy=False):
        path = capture.write_capture(
            self.vault, kind + "s", kind, capture.grant_digest(quote),
            f"owner {kind} auto-captured", quote,
            {"cwd": "synthetic-project", "session_id": "synthetic-source-session"}, None,
            replay=capture.Replay(stamp="2026-01-02T03:04:05Z"),
        )
        if legacy:
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            lines = [line[:line.index(": ", 13) + 2] + capture.one_line(quote)[:80]
                     if line.startswith("description: ") else line for line in lines]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        memsearch.build_index(self.vault)
        return path

    def invoke(self, session="", budget=10240):
        config = common.load_config(time.monotonic())
        config[memspec.CONFIG_BUDGET_BYTES_FIELD] = budget
        markers = []
        with patch.object(recall, "load_config", return_value=config):
            value = recall._handle({"prompt": "fixturehistory", "session_id": session,
                                    "cwd": "unrelated-current-project"}, time.monotonic(), markers)
        if value:
            self.assertTrue(common.payload_fits("UserPromptSubmit",
                            value["hookSpecificOutput"]["additionalContext"], budget))
        return value, markers

    def context(self):
        return self.invoke()[0]["hookSpecificOutput"]["additionalContext"]

    def test_new_description_retains_trailing_condition(self):
        path = self.card()
        fields, _ = memspec.frontmatter_fields(path)
        self.assertIn(TAIL, fields["description"])

    def test_old_truncated_description_recovers_stored_body_and_source(self):
        path = self.card(legacy=True)
        context = self.context()
        for expected in (TAIL, HISTORY, "2026-01-02T03:04:05Z", "synthetic-project",
                         "synthetic-source-session", path.name):
            self.assertIn(expected, context)
        self.assertNotIn(memspec.RULING_PREFIX, context)
        self.assertNotIn("unrelated-current-project", context)

    def test_held_out_multiline_and_grant_keep_conditions(self):
        quote = "fixturehistory 你可以整理測試素材\n" + "保留對照資料；" * 16 + "本次授權到測試結束為止"
        self.card(quote, kind="grant", legacy=True)
        context = self.context()
        self.assertIn("本次授權到測試結束為止", context)
        self.assertIn(HISTORY, context)

    def test_missing_source_never_becomes_verified_index_quote(self):
        path = self.card()
        with patch.object(Path, "open", side_effect=OSError("synthetic read failure")):
            rendered = recall._captured_context(path)
        self.assertIn("未讀得原卡", rendered)
        self.assertNotIn(QUOTE, rendered)

    def test_oversized_body_is_explicitly_partial(self):
        self.card("fixturehistory " + "內容" * 1000 + TAIL)
        context = self.context()
        self.assertIn("未完；先讀原卡", context)
        self.assertIn("rulings/ruling-", context)

    def test_unknown_provenance_is_not_borrowed_from_current_session(self):
        path = self.card("fixturehistory 短原話")
        text = path.read_text(encoding="utf-8")
        path.write_text("\n".join(line for line in text.splitlines()
                        if not line.startswith(("cwd:", "session_id:", "captured_at:"))) + "\n", encoding="utf-8")
        context = self.context()
        self.assertIn("來源欄位不全", context)
        self.assertNotIn("unrelated-current-project", context)

    def test_budget_drops_whole_card_without_delivery_marker(self):
        path = self.card()
        full, _ = self.invoke()
        size = len(full["hookSpecificOutput"]["additionalContext"].encode("utf-8"))
        for budget in (size - 1, 100):
            value, markers = self.invoke("budget", budget)
            context = value["hookSpecificOutput"]["additionalContext"] if value else ""
            self.assertNotIn(path.name, context)
            self.assertFalse(any(digest.startswith("card-") for _session, digest in markers))
        self.assertIn(TAIL, self.context())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
