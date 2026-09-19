"""殭屍待辦的判準：條目才算條目，敘述與引用不算；掃不到的庫不准回報成乾淨。"""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date

from epitype import pending_lint


class WhatCountsAsAPendingEntry(unittest.TestCase):
    def test_a_list_entry_counts(self):
        self.assertTrue(pending_lint._is_pending("- ⏳ 這件事還沒做"))
        self.assertTrue(pending_lint._is_pending("- 2026-06-20 狀態：**待 owner 一句**"))

    def test_a_heading_counts(self):
        self.assertTrue(pending_lint._is_pending("## 待辦（2026-06-27 時點）"))

    def test_narrative_prose_does_not_count(self):
        # 2026-09-19：通用庫 7 行全是這種——卡片正文在敘述往事，不是一條沒做完的事。
        self.assertFalse(pending_lint._is_pending(
            "**Why:** 2026-09-02 某場照抄交接本的 ⏳ 清單回報「未決」，owner 當場糾正"))
        self.assertFalse(pending_lint._is_pending(
            "**追加(2026-09-01)**：同族行為——被問還有問題嗎，就把全局 ⏳待辦清單端出來"))

    def test_a_line_that_says_it_is_closed_does_not_count(self):
        self.assertFalse(pending_lint._is_pending("## 2026-08-27 到期檢視（本卡的待辦在此結案）"))
        self.assertFalse(pending_lint._is_pending("- 三個「未辦」已結案（owner 逐項裁決）"))

    def test_a_marker_inside_a_link_or_quotation_is_a_reference(self):
        self.assertFalse(pending_lint._is_pending("- 更版由 owner 觸發（常設待辦 [[decision-roles]]）"))
        self.assertFalse(pending_lint._is_pending("- 規則叫做「待辦清單要帶出口」，見上面那張卡"))

    def test_a_line_carrying_its_own_check_is_not_a_zombie(self):
        self.assertFalse(pending_lint._is_pending("- ⏳ 還沒做 verify: python x.py"))


class MissingVaultIsNotClean(unittest.TestCase):
    def test_a_vault_that_does_not_exist_is_an_error_not_a_zero(self):
        # 不存在的來源回「0 個問題」跟「很乾淨」長得一樣，而乾淨正是沒有人會再查的答案。
        with tempfile.TemporaryDirectory() as temporary:
            missing = str(Path(temporary) / "no-such-vault")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = pending_lint.main([missing])
            self.assertEqual(code, 2)
            self.assertIn("找不到這個記憶庫", err.getvalue())
            self.assertNotIn("zombies=0", out.getvalue())

    def test_a_real_vault_still_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "card.md").write_text(
                "---\nname: x\ndescription: y\n---\n- ⏳ 一件老的待辦（2026-01-01）\n",
                encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err):
                # 這支的輸出目的地是呼叫時綁定的，不是 sys.stdout 的當下值——
                # 用 redirect_stdout 抓不到，測試會誤判成「什麼都沒印」。
                code = pending_lint.main([str(vault)], output=out)
            self.assertEqual(code, 0)
            self.assertIn("zombies=1", out.getvalue())


class NoOrphans(unittest.TestCase):
    """owner 2026-09-19：「不能有孤兒！有孤兒很容易放到忘記」。

    孤兒＝沒有人掛名的待辦條目。不看年齡——等它變老才報，等於等它被忘記。
    判準比殭屍嚴：標記必須在條目最前面。誤報會變成每晚都在報的假帳。"""

    def test_a_marked_entry_is_an_orphan_candidate(self):
        for line in ("- ⏳ U-H 喚回不端原話", "* 待辦：把那段補上",
                     "1. TODO 接上那條線", "- [ ] ⏳ 還沒做"):
            self.assertTrue(pending_lint._is_orphan_candidate(line), msg=line)

    def test_prose_that_merely_contains_the_word_is_not(self):
        # 2026-09-19 通用庫實測的原文：這四種以前都被算成孤兒，11 報 3 準。
        for line in (
            "- **殭屍待辦 19 → 4**：原本把「卡片正文在敘述往事」也算成沒做完的事。",
            "- 時間到才能驗的三項：看待辦卡。",
            "- 此處舊「寫檔閘引用禁詞待修」已由 U64 處理；不列待辦。",
            "- [[project-titan-state]] — 同日七項待辦的深度分析",
        ):
            self.assertFalse(pending_lint._is_orphan_candidate(line), msg=line)

    def test_a_heading_is_not_an_item(self):
        # 標題不是一件事，它底下的條目才是。規則卡自己的標題最常被誤報。
        for line in ("# 待辦要帶可跑的唯讀檢查與出口條件", "## 待決／後續", "### ⏳ 待辦"):
            self.assertFalse(pending_lint._is_orphan_candidate(line), msg=line)

    def test_an_entry_that_carries_its_check_is_not_an_orphan(self):
        self.assertFalse(pending_lint._is_orphan_candidate("- ⏳ 還沒做 verify: python x.py"))

    def test_a_closed_entry_is_not_an_orphan(self):
        self.assertFalse(pending_lint._is_orphan_candidate("- ⏳ 那件事已結案"))

    def test_the_card_owner_field_clears_every_entry_on_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "owned.md").write_text(
                "---\nname: x\ndescription: y\nowner: Claude\n---\n- ⏳ 還沒做\n",
                encoding="utf-8")
            report = pending_lint.scan_vault(vault, today=date(2026, 9, 19))
            self.assertEqual(report["orphan_lines"], 0)

    def test_an_ownerless_card_reports_its_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "stray.md").write_text(
                "---\nname: x\ndescription: y\n---\n- ⏳ 還沒做\n- ⏳ 也還沒做\n",
                encoding="utf-8")
            report = pending_lint.scan_vault(vault, today=date(2026, 9, 19))
            self.assertEqual(report["orphan_lines"], 2)
            self.assertEqual(report["orphan_cards"], 1)

    def test_an_entry_naming_its_owner_inline_is_not_an_orphan(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "inline.md").write_text(
                "---\nname: x\ndescription: y\n---\n- ⏳ 待 owner 一句\n",
                encoding="utf-8")
            report = pending_lint.scan_vault(vault, today=date(2026, 9, 19))
            self.assertEqual(report["orphan_lines"], 0)

    def test_the_count_reaches_the_report_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "stray.md").write_text(
                "---\nname: x\ndescription: y\n---\n- ⏳ 還沒做\n", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err):
                code = pending_lint.main([str(vault)], output=out)
            self.assertEqual(code, 0)
            self.assertIn("orphans=1", out.getvalue())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
