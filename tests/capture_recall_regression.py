import sys; sys.dont_write_bytecode = True
"""Captured quotes stay out of recall, stay reachable by memsearch (U-H)."""

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
            # 這一組驗的是「入庫的卡怎麼端出來」，所以固定走白名單那條路；白名單本身
            # 由 tests/capture_admission_regression.py 驗（owner 2026-09-09 Q5「C」）。
            source_text="不要再這樣做",
        )
        if legacy:
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            lines = [line[:line.index(": ", 13) + 2] + capture.one_line(quote)[:80]
                     if line.startswith("description: ") else line for line in lines]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        memsearch.build_index(self.vault)
        return path

    def plain_card(self, name="plain", needle="fixturehistory"):
        path = self.vault / f"{name}.md"
        path.write_text(f"---\nname: {name}\ndescription: {needle} 的整理卡\n---\n{needle} body\n",
                        encoding="utf-8")
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
        value = self.invoke()[0]
        return value["hookSpecificOutput"]["additionalContext"] if value else ""

    def searched(self, prompt="fixturehistory"):
        return [item["card_path"] for item in
                memsearch.recall_index(self.vault, prompt).get("results", ())]

    def test_new_description_retains_trailing_condition(self):
        path = self.card()
        fields, _ = memspec.frontmatter_fields(path)
        self.assertIn(TAIL, fields["description"])

    def test_captured_ruling_is_never_injected(self):
        path = self.card()
        self.plain_card()
        context = self.context()
        self.assertIn("plain", context)
        for absent in (path.name, path.stem, TAIL, "rulings/"):
            self.assertNotIn(absent, context)

    def test_captured_grant_is_never_injected(self):
        quote = "fixturehistory 你可以整理測試素材\n" + "保留對照資料；" * 16 + "本次授權到測試結束為止"
        path = self.card(quote, kind="grant", legacy=True)
        self.plain_card()
        context = self.context()
        self.assertNotIn(path.stem, context)
        self.assertNotIn("本次授權到測試結束為止", context)

    def test_a_vault_of_quotes_alone_injects_nothing(self):
        self.card()
        self.card("fixturehistory 另一句原話", kind="correction")
        self.assertEqual(self.invoke()[0], None)

    def test_quotes_recall_skips_are_still_reachable_by_memsearch(self):
        path = self.card()
        self.assertIn(f"{memspec.RULING_DIRECTORY}/{path.name}", self.searched())

    def test_a_promoted_decision_card_keeps_its_pinned_seat(self):
        path = self.card()
        text = path.read_text(encoding="utf-8")
        head, _, rest = text.partition("\n---\n")
        path.write_text(
            head
            + f"\n{memspec.DECISION_KEY_FIELD}: fixturehistory-key"
            + f"\n{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}"
            + f"\n{memspec.CURRENT_DECISION_AT_FIELD}: 2026-01-02"
            + f"\n{memspec.OWNER_QUOTE_FIELD}: fixturehistory 一律照這條走\n---\n"
            + rest,
            encoding="utf-8",
        )
        memsearch.build_index(self.vault)
        context = self.context()
        self.assertIn(memspec.DECISION_PREFIX + "fixturehistory-key（2026-01-02）", context)
        self.assertIn("fixturehistory 一律照這條走", context)

    def test_budget_drops_whole_card_without_delivery_marker(self):
        path = self.plain_card()
        full, _ = self.invoke()
        size = len(full["hookSpecificOutput"]["additionalContext"].encode("utf-8"))
        for budget in (size - 1, 100):
            value, markers = self.invoke("budget", budget)
            context = value["hookSpecificOutput"]["additionalContext"] if value else ""
            self.assertNotIn(path.name, context)
            self.assertFalse(any(digest.startswith("card-") for _session, digest in markers))
        self.assertIn(path.stem, self.context())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
