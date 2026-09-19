# -*- coding: utf-8 -*-
"""內建回合檢查的兩向實測：該擋的擋、不該擋的不擋。

字面比對看不到的兩件事——這回合的話有多長、引的檔這一場有沒有被打開過——由閘門內建的
檢查判。兩向都要測，因為這一類檢查的失敗模式是誤擋：它不是比對某個字，而是推論一個事實。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters" / "claude"))

import json
import tempfile
import unittest
from collections import namedtuple

from epitype import memspec, opened

import stop_gate

_Turn = stop_gate._Turn


def decision(**overrides):
    fields = {
        "key": "測試卡",
        "decided_at": "2026-09-19",
        "quote": "測試用",
        "forbidden": (),
        "aliases": (),
        "path": Path("測試卡.md"),
        "decided_by": "owner-explicit",
        "require_when": "",
        "require_text": "",
        "advice": "細節寫進檔案",
        "applies_to": "",
        "turn_check": "",
        "turn_check_limit": "",
    }
    fields.update(overrides)
    return stop_gate._Decision(**fields)


class ReportLength(unittest.TestCase):
    def setUp(self):
        self.card = decision(turn_check=memspec.TURN_CHECK_LENGTH, turn_check_limit="100")

    def test_a_short_answer_passes(self):
        self.assertIsNone(stop_gate._length_gap(self.card, "改好了，測試 61/61。", _Turn([], "好了嗎")))

    def test_a_long_answer_is_blocked(self):
        reason = stop_gate._length_gap(self.card, "話" * 400, _Turn([], "好了嗎"))
        self.assertIsNotNone(reason)
        self.assertIn("400 字", reason)
        self.assertIn("100 字上限", reason)

    def test_long_is_fine_when_the_owner_asked_for_the_whole_thing(self):
        # owner 自己點了完整，長就是他要的東西。這一向如果會擋，這道檢查就是在跟他吵架。
        for prompt in ("完整怎麼優化跟我說清楚", "詳細列出來", "給我 prompt", "計畫寫清楚",
                       "逐條對照", "怎麼用", "教我", "報告一下", "full plan", "步驟"):
            self.assertIsNone(
                stop_gate._length_gap(self.card, "話" * 400, _Turn([], prompt)),
                msg=prompt)

    def test_a_code_block_does_not_count_as_talking_too_much(self):
        # 貼給 owner 的指令與程式是他要的東西，不是話多。
        body = "這樣跑：\n```bash\n" + ("python x.py\n" * 200) + "```\n跑完回我一聲。"
        self.assertIsNone(stop_gate._length_gap(self.card, body, _Turn([], "怎麼跑")))

    def test_an_unclosed_code_block_still_does_not_count(self):
        # 串流中斷或忘了收尾的圍籬，以前會被算成正文，整回合被誤擋。
        body = "這樣跑：\n```bash\n" + ("python x.py\n" * 200)
        self.assertIsNone(stop_gate._length_gap(self.card, body, _Turn([], "怎麼跑")))

    def test_the_card_carries_the_limit(self):
        # 上限寫在卡上，owner 改卡就改得動，不必改程式。
        loose = decision(turn_check=memspec.TURN_CHECK_LENGTH, turn_check_limit="9000")
        self.assertIsNone(stop_gate._length_gap(loose, "話" * 400, _Turn([], "好了嗎")))

    def test_a_missing_limit_falls_back_to_the_measured_default(self):
        bare = decision(turn_check=memspec.TURN_CHECK_LENGTH)
        self.assertIsNone(stop_gate._length_gap(
            bare, "話" * (memspec.TURN_LENGTH_DEFAULT_LIMIT - 1), _Turn([], "好了嗎")))
        self.assertIsNotNone(stop_gate._length_gap(
            bare, "話" * (memspec.TURN_LENGTH_DEFAULT_LIMIT + 1), _Turn([], "好了嗎")))

    def test_a_junk_limit_does_not_disarm_the_card(self):
        # 上限寫成空字串、負數、中文字，都不該變成「不生效」——那是靜默失效。
        for bad in ("", "-5", "0", "三千", "abc"):
            card = decision(turn_check=memspec.TURN_CHECK_LENGTH, turn_check_limit=bad)
            self.assertIsNotNone(
                stop_gate._length_gap(card, "話" * (memspec.TURN_LENGTH_DEFAULT_LIMIT + 1),
                                      _Turn([], "好了嗎")),
                msg=bad)


class CitedButNeverOpened(unittest.TestCase):
    def setUp(self):
        self.card = decision(turn_check=memspec.TURN_CHECK_CITED_UNREAD)
        self.opened = frozenset({"memspec.py", "stop_gate.py"})

    def test_claiming_to_have_checked_a_file_nobody_opened_is_blocked(self):
        reason = stop_gate._cited_unread_gap(
            self.card, "我查過 dream.py，那一段沒問題。", _Turn([], "對不對"), self.opened)
        self.assertIsNotNone(reason)
        self.assertIn("dream.py", reason)
        self.assertIn("查過", reason)

    def test_claiming_to_have_checked_a_file_that_was_opened_passes(self):
        self.assertIsNone(stop_gate._cited_unread_gap(
            self.card, "我查過 memspec.py，那一段沒問題。", _Turn([], "對不對"), self.opened))

    def test_a_windows_path_matches_the_same_file(self):
        # 同一個檔有絕對、相對、正反斜線好幾種寫法；比對檔名就是為了這個。
        self.assertIsNone(stop_gate._cited_unread_gap(
            self.card, r"我查過 C:\Epitype\repo\epitype\memspec.py 了。",
            _Turn([], "對不對"), self.opened))

    def test_mentioning_a_file_without_claiming_to_have_read_it_passes(self):
        # 「接下來要改 dream.py」不是宣稱查過，不該擋。
        self.assertIsNone(stop_gate._cited_unread_gap(
            self.card, "接下來要改 dream.py，還沒動。", _Turn([], "下一步"), self.opened))

    def test_a_path_inside_a_code_block_is_not_a_claim(self):
        body = "我查過了，跑這個：\n```bash\npython dream.py --selftest\n```"
        self.assertIsNone(stop_gate._cited_unread_gap(
            self.card, body, _Turn([], "怎麼跑"), self.opened))

    def test_no_record_means_no_block(self):
        # 附記是空的時候，「動手閘沒註冊」跟「真的沒讀」長得一模一樣。分不出來就不擋人。
        self.assertIsNone(stop_gate._cited_unread_gap(
            self.card, "我查過 dream.py。", _Turn([], "對不對"), frozenset()))

    def test_generic_filenames_are_not_evidence_claims(self):
        # CLAUDE.md、README.md 到處都有，講的通常是那一類檔，比對只會誤擋。
        self.assertIsNone(stop_gate._cited_unread_gap(
            self.card, "我查過 CLAUDE.md 的規則塊了。", _Turn([], "對不對"), self.opened))


class WiringIntoTheGate(unittest.TestCase):
    def test_an_unknown_check_name_is_reported_not_swallowed(self):
        defects = []
        card = decision(turn_check="沒有這種檢查")
        self.assertIsNone(stop_gate._turn_check_gap(card, "話" * 9999, _Turn([], ""), frozenset(), defects))
        self.assertTrue(defects)
        self.assertIn("沒有這種檢查", defects[0])

    def test_a_card_without_the_field_is_not_touched(self):
        defects = []
        self.assertIsNone(stop_gate._turn_check_gap(
            decision(), "話" * 9999, _Turn([], ""), frozenset(), defects))
        self.assertEqual(defects, [])

    def test_the_field_counts_as_armed(self):
        # 夜間報表與卡片檢查器都要認這種武裝，不然它們會叫人去「補武裝」一張已經在擋的卡。
        self.assertIn(memspec.TURN_CHECK_FIELD, memspec.CARD_ARMING_FIELDS)

    def test_the_cache_only_accepts_its_own_version(self):
        # 舊版快取被新碼接受的話，新欄位對舊卡默默不生效——加一個欄位就漏一批卡。
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "stop_decisions.json"
            cache.write_text(json.dumps({"version": 4, "manifest": {"a.md": [1, 2, 3, 4, 5]},
                                         "decisions": {"a.md": {"key": "舊卡"}}}),
                             encoding="utf-8")
            manifest, rulings, _cursor = stop_gate._read_cache(cache)
            self.assertEqual(manifest, {})
            self.assertEqual(rulings, {})


class TheOpenedRecord(unittest.TestCase):
    def test_it_records_and_reads_back_filenames(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            self.assertTrue(opened.record(vault, "sess-1", r"C:\Epitype\repo\epitype\dream.py"))
            self.assertIn("dream.py", opened.names(vault, "sess-1"))

    def test_another_session_does_not_see_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            opened.record(vault, "sess-1", "dream.py")
            self.assertEqual(opened.names(vault, "sess-2"), frozenset())

    def test_a_payload_without_filenames_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            self.assertFalse(opened.record(vault, "sess-1", "git status --short"))
            self.assertEqual(opened.names(vault, "sess-1"), frozenset())

    def test_a_missing_session_id_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(opened.record(Path(temporary), "", "dream.py"))
            self.assertEqual(opened.names(Path(temporary), ""), frozenset())

    def test_only_target_fields_count_as_opening_something(self):
        # 寫一個檔案時，它的內容裡提到的檔名並沒有被打開。把內容也算進來，等於自己
        # 替自己背書：寫一句「我查過 x.py」進某個檔，就通過了「真的查過 x.py」。
        self.assertIn("file_path", memspec.OPENED_TARGET_FIELDS)
        self.assertNotIn("content", memspec.OPENED_TARGET_FIELDS)
        self.assertNotIn("new_string", memspec.OPENED_TARGET_FIELDS)
        self.assertNotIn("prompt", memspec.OPENED_TARGET_FIELDS)

    def test_a_shell_command_still_yields_its_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            opened.record(vault, "s", "grep -n turn_check epitype/memspec.py adapters/claude/stop_gate.py")
            names = opened.names(vault, "s")
            self.assertIn("memspec.py", names)
            self.assertIn("stop_gate.py", names)


def _selftest():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromTestCase(case)
        for case in (ReportLength, CitedButNeverOpened, WiringIntoTheGate, TheOpenedRecord)
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
