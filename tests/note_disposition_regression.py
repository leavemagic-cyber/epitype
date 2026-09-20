# -*- coding: utf-8 -*-
"""只管用詞與篇幅的規則：違反了要記、要提醒，但不准為了它多跑一輪。

宿主是先把字送到 owner 眼前、才跑回合閘。擋下一則回覆收不回任何一個字，只會逼出第二輪。
第二輪對「缺了東西」的規則有用（補證據、補查證）；對用詞與篇幅沒有用——客套話已經被
看到了，重寫只是再多一段。2026-09-20 owner 原話：「我們本意設定那個原則是為了節省
token，反而為了這個原則浪費token 就完全本末倒置」。

所以卡片可以標 `on_hit: note`：照樣比對、照樣進帳，不擋；下一則提問時附一行提醒。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "adapters" / "claude"))

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from epitype import memspec

import _hook_common as common
import recall_hook
import stop_gate

CARD = """---
name: {name}
description: 對 owner 不說客套話
forbidden:
  - "好問題"
{extra}---

測試用。
"""


class NoteInsteadOfBlock(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-note-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {
            memspec.EPITYPE_CONFIG_ENV: str(self.config),
            memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        marker = patch.object(
            stop_gate, "recall_marker_directory",
            lambda session: self.root / "markers" / common.session_component(session))
        marker.start()
        self.addCleanup(marker.stop)

    def card(self, name, extra=""):
        (self.vault / (name + ".md")).write_text(
            CARD.format(name=name, extra=extra), encoding="utf-8", newline="\n")

    def stop(self, message, session="note-1"):
        return stop_gate._handle(
            {"session_id": session, "cwd": str(self.root),
             "last_assistant_message": message}, time.monotonic(), [])

    def log_kinds(self):
        path = self.vault / memspec.GATE_LOG_FILENAME
        if not path.is_file():
            return []
        return [json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines()]

    def prompt(self, session="note-1"):
        return recall_hook._handle(
            {"session_id": session, "cwd": str(self.root), "prompt": "下一件事"},
            time.monotonic(), [])

    def test_unmarked_card_still_blocks(self):
        self.card("no-flattery")
        value = self.stop("好問題，我來看看。")
        self.assertIsNotNone(value, "沒標處置的卡，行為一個字都不能變")
        self.assertEqual(value["decision"], "block")
        self.assertEqual(self.log_kinds(), [memspec.STOP_GATE_LOG_KIND])

    def test_note_card_lets_the_turn_end(self):
        self.card("no-flattery", "on_hit: note\n")
        self.assertIsNone(self.stop("好問題，我來看看。"), "標了只提醒就不准再多跑一輪")

    def test_note_is_still_on_the_books(self):
        self.card("no-flattery", "on_hit: note\n")
        self.stop("好問題，我來看看。")
        self.assertEqual(self.log_kinds(), [memspec.STOP_NOTE_LOG_KIND],
                         "不擋不等於不記：夜間統計要看得到這一次")

    def test_next_prompt_carries_the_note_once(self):
        self.card("no-flattery", "on_hit: note\n")
        self.stop("好問題，我來看看。")
        first = self.prompt()
        self.assertIsNotNone(first)
        context = first["hookSpecificOutput"]["additionalContext"]
        self.assertIn("好問題", context)
        self.assertIn("不用重寫", context)
        second = self.prompt()
        text = "" if second is None else second["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("不用重寫", text, "提醒只送一次；每則都送就變成新的浪費")

    def test_clean_turn_leaves_no_note(self):
        self.card("no-flattery", "on_hit: note\n")
        self.assertIsNone(self.stop("查過了，檔案在這裡。"))
        self.assertEqual(self.log_kinds(), [])
        value = self.prompt()
        text = "" if value is None else value["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("不用重寫", text)

    def test_a_blocking_rule_outranks_a_note(self):
        self.card("no-flattery", "on_hit: note\n")
        (self.vault / "no-hedge.md").write_text(
            CARD.format(name="no-hedge", extra="").replace("好問題", "應該可以"),
            encoding="utf-8", newline="\n")
        value = self.stop("好問題，這樣應該可以。")
        self.assertIsNotNone(value)
        self.assertIn("應該可以", value["reason"])
        self.assertEqual(self.log_kinds(), [memspec.STOP_GATE_LOG_KIND])

    def test_notes_do_not_pile_up(self):
        self.card("no-flattery", "on_hit: note\n")
        for index in range(memspec.STOP_NOTE_MAX_PENDING + 4):
            self.stop("好問題，第 %d 次。" % index)
        loaded = common.load_config(time.monotonic())
        self.assertLessEqual(len(common.take_notes(loaded, "note-1")),
                             memspec.STOP_NOTE_MAX_PENDING)

    def test_unknown_value_is_reported_and_still_blocks(self):
        from epitype import card_lint

        self.card("no-flattery", "on_hit: whisper\n")
        self.assertIsNotNone(self.stop("好問題，我來看看。"), "打錯值要落在安全的那一邊")
        _type, findings = card_lint.check_card(self.vault / "no-flattery.md", "no-flattery.md")
        self.assertTrue(any(code == "on-hit" for _level, code, _text in findings))


def _selftest():
    suite = unittest.TestLoader().loadTestsFromTestCase(NoteInsteadOfBlock)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    bad = len(result.failures) + len(result.errors)
    print("note_disposition_regression: %s (%d/%d)"
          % ("OK" if not bad else "FAIL", result.testsRun - bad, result.testsRun))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
