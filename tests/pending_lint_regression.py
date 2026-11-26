"""殭屍待辦的判準：條目才算條目，敘述與引用不算；掃不到的庫不准回報成乾淨。"""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
import os

from epitype import pending_lint


class WhatCountsAsAPendingEntry(unittest.TestCase):
    def test_a_list_entry_counts(self):
        self.assertTrue(pending_lint._is_pending("- ⏳ 這件事還沒做"))
        self.assertTrue(pending_lint._is_pending("- 2026-06-20 狀態：**待 owner 一句**"))

    def test_a_heading_counts(self):
        self.assertTrue(pending_lint._is_pending("## 待辦（2026-06-27 時點）"))

    def test_narrative_prose_does_not_count(self):
        # Invented historical prose is not an unfinished list entry.
        self.assertFalse(pending_lint._is_pending(
            "**Why:** 合成花園的 ⏳ 標記曾被誤認為未決事項"))
        self.assertFalse(pending_lint._is_pending(
            "**追加(2026-01-01)**：範例花園曾把舊 ⏳待辦清單當成目前狀態"))

    def test_a_line_that_says_it_is_closed_does_not_count(self):
        self.assertFalse(pending_lint._is_pending("## 2026-08-27 到期檢視（本卡的待辦在此結案）"))
        self.assertFalse(pending_lint._is_pending("- 三個「未辦」已結案（owner 逐項裁決）"))

    def test_a_marker_inside_a_link_or_quotation_is_a_reference(self):
        self.assertFalse(pending_lint._is_pending("- 更版由 owner 觸發（常設待辦 [[decision-roles]]）"))
        self.assertFalse(pending_lint._is_pending("- 規則叫做「待辦清單要帶出口」，見上面那張卡"))

    def test_a_heading_counts_only_when_the_marker_opens_a_segment(self):
        # 標題裡的待辦字眼是一段的開頭（含 / — 空白之後）才是待辦區的標題。
        for heading in ("## 待決(owner)", "## 狀態 / 待辦", "## 未做 / owner 待決",
                        "## 🔑 owner-gate 待決（急迫序）", "## ⚠ 方法論決策(reset)— 待 owner 知會",
                        "## 待 owner 決定（已寫進 `_LEDGER.md` ②，每條帶 verify）"):
            self.assertTrue(pending_lint._is_pending(heading), heading)
        # 黏在別的字後面、或緊跟開引號，只是標題裡的一個詞。
        for heading in ("## 同日待辦深度分析結果",
                        "## 09-06 帳本補記（帳本只留現行與待辦）",
                        "## 分析結果（owner:「待辦事項都深度分析以後,再處理」）"):
            self.assertFalse(pending_lint._is_pending(heading), heading)

    def test_an_index_line_pointing_at_a_card_is_not_an_entry(self):
        self.assertFalse(pending_lint._is_pending(
            "- [some-card](some-card.md) — 七平台紀錄、待 owner 事項"))
        self.assertFalse(pending_lint._is_pending(
            "- 🆕🔴 [某次鑑識](forensics.md) — 未修待 owner 裁"))
        self.assertFalse(pending_lint._is_pending("- [[some-card]] — 同日七項待辦的分析"))
        self.assertFalse(pending_lint._is_pending("- 時間到才能驗的三項：看待辦卡。"))
        self.assertFalse(pending_lint._is_pending("- 一律寫進 `STATE.md` 待辦總表"))
        self.assertFalse(pending_lint._is_pending("- **殭屍待辦 19 → 4**：敘述不算條目"))
        # 條目本身是待辦、只是順帶附連結的，照樣是待辦。
        self.assertTrue(pending_lint._is_pending("- 待辦：補測試，見 [[some-card]]"))
        self.assertTrue(pending_lint._is_pending("- ⏳ [some-card](some-card.md) — 等 owner 回覆"))
        self.assertTrue(pending_lint._is_pending("- [ ] 待辦：補測試"))
        self.assertTrue(pending_lint._is_pending("1. **待辦**：Codex review、目錄清理（owner 決）"))

    def test_a_line_that_says_it_is_not_a_todo_does_not_count(self):
        for line in ("- 本機領先遠端是正常狀態，不是待辦：發布等 owner 觸發",
                     "- 清空回收筒是 owner 自己的動作，不列為待辦追蹤。",
                     "- 廠商安裝檔不入待辦。",
                     "## 建置完成(default-OFF,無待辦)",
                     "- owner handles it, not a TODO"):
            self.assertFalse(pending_lint._is_pending(line), line)
        # 「非零待辦」不是否定；真正等 owner 的條目照樣報。
        self.assertTrue(pending_lint._is_pending("- 來源界線保持未執行，非零待辦"))
        self.assertTrue(pending_lint._is_pending("- 仍待 owner（不要自己做）：清空回收筒"))

    def test_a_line_that_says_where_it_was_handled_does_not_count(self):
        self.assertFalse(pending_lint._is_pending(
            "- 此處舊「禁詞待修」已由 U64 `32a932b` 處理；見 §16。"))
        self.assertFalse(pending_lint._is_pending("- TODO flaky test, handled in commit 32a932b"))
        # 只是「已在某處登記、還待處理」的條目沒有處理掉。
        self.assertTrue(pending_lint._is_pending("- ⏳ 已在 issue 登記，待處理"))
        self.assertTrue(pending_lint._is_pending("- ⏳ 已在 issue 裡待處理"))

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
    """孤兒＝沒有人掛名的待辦條目，不以年齡作為必要條件。
    判準比殭屍嚴：標記必須在條目最前面。誤報會變成每晚都在報的假帳。"""

    def test_a_marked_entry_is_an_orphan_candidate(self):
        for line in ("- ⏳ U-H 喚回不端原話", "* 待辦：把那段補上",
                     "1. TODO 接上那條線", "- [ ] ⏳ 還沒做"):
            self.assertTrue(pending_lint._is_orphan_candidate(line), msg=line)

    def test_prose_that_merely_contains_the_word_is_not(self):
        # Invented reports, references and prose are not marked list entries.
        for line in (
            "- **範例待辦 8 → 2**：這裡描述的是歷史統計。",
            "- 時間到才能驗的三項：看待辦卡。",
            "- 此處舊「花盆待修」已由範例維修單處理；不列待辦。",
            "- [[synthetic-garden-state]] — 範例待辦的背景說明",
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

class CardsNotInForceAndRuleCards(unittest.TestCase):
    """停用的卡不報；規範卡只認開頭標記或沒打勾的方塊，其他卡照舊。"""

    TODAY = date(2026, 11, 13)

    def _scan(self, name, front, body):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            card = vault / name
            card.write_text(f"---\nname: x\ndescription: y\n{front}---\n{body}", encoding="utf-8")
            # Undated lines fall back to the card's mtime; pin it to TODAY so the age does not
            # depend on the day the suite runs (a fresh file is younger than a fixed TODAY).
            stamp = datetime(self.TODAY.year, self.TODAY.month, self.TODAY.day, tzinfo=timezone.utc).timestamp()
            os.utime(card, (stamp, stamp))
            return pending_lint.scan_vault(vault, max_age_days=0, today=self.TODAY)

    def test_a_superseded_card_reports_nothing(self):
        for status in ("superseded", "closed", "retired"):
            report = self._scan("project_a.md", f"status: {status}\n",
                                "- **Z3** = **待 owner 一句**：要不要 port\n- ⏳ 還沒做\n")
            self.assertEqual((report["zombie_lines"], report["orphan_lines"]), (0, 0), status)

    def test_status_nested_under_metadata_counts_too(self):
        report = self._scan("project_a.md", "metadata:\n  type: project\n  status: superseded\n",
                            "- ⏳ 還沒做\n")
        self.assertEqual((report["zombie_lines"], report["orphan_lines"]), (0, 0))

    def test_an_active_project_card_still_reports(self):
        report = self._scan("project_a.md", "status: active\nmetadata:\n  type: project\n",
                            "- 量測重連代價，待 owner 確認\n- **待辦（重量重連代價）**：還沒重量\n")
        self.assertEqual(report["zombie_lines"], 2)

    def test_a_rule_sentence_in_a_rule_card_is_not_a_todo(self):
        body = (
            "4. 有解法且在授權內就先修浪費；要 Owner 輸密碼、付費等做不了時，才留待辦並寫明阻礙。\n"
            "- 待辦清單要帶出口：只因提到「待辦清單」四個字就觸發，改成真的在列清單才觸發。\n"
            "6. **收尾簡報**：改了哪些檔、未完成與待 owner 決定的事。\n"
            "- 記憶注入是參考資料，不能把未辦字樣讀成授權。\n"
        )
        for name, front in (("decision-a.md", "decision_key: a\nstatus: active\n"),
                            ("feedback-a.md", "metadata:\n  type: feedback\n")):
            report = self._scan(name, front, body)
            # 「待辦清單」開頭的那句標記雖在最前面，但後面直接接字，是名詞不是一件事。
            self.assertEqual((report["zombie_lines"], report["orphan_lines"]), (0, 0), name)

    def test_a_leading_marker_or_open_box_in_a_rule_card_still_reports(self):
        report = self._scan("feedback-a.md", "metadata:\n  type: feedback\n",
                            "- 待辦：補兩向例句\n- [ ] 補測試，待 owner 確認\n- [x] 補文件，待 owner 確認\n"
                            "- 2026-07-22 未辦：補索引\n")
        texts = [line["text"] for card in report["cards"] for line in card["lines"]]
        self.assertEqual(texts, ["- 待辦：補兩向例句", "- [ ] 補測試，待 owner 確認", "- 2026-07-22 未辦：補索引"])
        self.assertEqual(report["orphan_lines"], 1)

    def test_a_card_that_says_it_is_a_project_keeps_the_loose_test(self):
        # 舊式頂層 type: project 的卡在 card_lint 會退回預設 feedback，但它是進度清單。
        report = self._scan("progress.md", "type: project\n", "## 尚未完成 ⏳\n")
        self.assertEqual(report["zombie_lines"], 1)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
