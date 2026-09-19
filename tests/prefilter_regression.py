# -*- coding: utf-8 -*-
"""字面前置過濾的安全性：它只准說「一定不會命中」，不准說錯。

這道過濾省的是編譯時間（實測每回合 43 ms），代價是一個新的失敗模式：如果它把「其實會
命中」的訊息判成不可能命中，那條規則就默默不生效——比慢糟得多。所以這裡的測試方向是
單邊的：**凡是真的會命中的，過濾一律不准擋掉**。

最後一題拿四個庫裡所有真規則與它們自己的必擋例句來跑：規則與例句都是真的，不是編的。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import re
import unittest

from epitype import cardscan, memspec


class WhatItCanProve(unittest.TestCase):
    def test_a_plain_literal_prefix(self):
        self.assertEqual(memspec.required_alternatives(r"mcp__[A-Za-z0-9_]+"), ["mcp__"])

    def test_a_group_of_literals(self):
        self.assertEqual(
            memspec.required_alternatives("(測試|全套|自測)[^。]{0,20}過"),
            ["測試", "全套", "自測"])

    def test_a_non_capturing_group_too(self):
        self.assertEqual(memspec.required_alternatives("(?:甲式|乙式)後續"), ["甲式", "乙式"])

    def test_a_lookbehind_proves_nothing(self):
        self.assertIsNone(memspec.required_alternatives(r"(?<![\d./])\d{1,4}/\d{1,4}"))

    def test_an_optional_group_proves_nothing(self):
        self.assertIsNone(memspec.required_alternatives("(也許|大概)?一定會發生"))

    def test_a_nested_group_is_given_up_on(self):
        self.assertIsNone(memspec.required_alternatives("(同一(個|支))做法"))

    def test_a_quantified_last_character_is_dropped(self):
        # `abc?` 的 c 可有可無，必要字面只有 ab。
        self.assertEqual(memspec.required_alternatives("abc?d"), ["ab"])

    def test_a_one_character_literal_is_too_weak_to_bother(self):
        self.assertIsNone(memspec.required_alternatives("甲[乙丙]"))


class ItNeverRejectsARealMatch(unittest.TestCase):
    """單邊性質：regex 真的會命中時，過濾不准說「不可能」。"""

    CASES = (
        (r"mcp__[A-Za-z0-9_]+", "我用了 mcp__browser 那個工具"),
        ("(測試|全套|自測)[^。]{0,20}(過|通過)", "全套跑完，通過"),
        ("(?:甲式|乙式)後續", "乙式後續要補"),
        ("第三(次|輪)[^。]{0,10}(同樣|一樣)", "第三次用同樣的參數"),
        (r"(?<!不)(所以|因此)[^。]{0,8}上線", "所以已經上線了"),
        ("(亂碼|mojibake)[^。]{0,14}(檔案|檔)(壞|損毀)", "畫面亂碼，檔案壞了"),
        # 2026-09-20 Codex 審查抓到的三種漏擋：最外層還有分支、群組可以出現零次。
        # 這些是既有合法寫法，效能改動把它們的語意改壞了，不是少支援一種新寫法。
        ("apple|banana", "banana"),
        ("(apple|pear)|banana", "banana"),
        ("(apple|pear){0,1}banana", "banana"),
        ("(甲|乙){0,2}丙", "丙"),
        # 量詞只管前一個字元：`abc{0}d` 要的是 abd，`前綴{0,3}後面` 仍然要有「前」。
        ("abc{0}d", "abd"),
        ("前綴{0,3}後面", "前後面"),
    )

    def test_every_real_match_survives_the_prefilter(self):
        for pattern, text in self.CASES:
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(re.search(pattern, text), "樣本本身要真的命中")
                self.assertFalse(memspec.prefilter_misses(pattern, text))

    def test_it_does_reject_what_cannot_match(self):
        # 沒有省到任何東西的過濾等於白做，所以反向也要有一題。
        self.assertTrue(memspec.prefilter_misses("(測試|全套|自測)[^。]{0,20}過", "今天天氣不錯"))
        self.assertTrue(memspec.prefilter_misses(r"mcp__[A-Za-z0-9_]+", "沒有工具名的一句話"))


class AgainstFixtureCards(unittest.TestCase):
    """帶在專案裡的樣本卡：乾淨機器上也必跑，不靠任何人的私人記憶庫。

    2026-09-20 Codex 審查抓到：原本這一題只讀作者本機的四個庫，找不到就跳過，最後卻
    又要求「至少檢查過一組」——在乾淨的 checkout 上必定失敗，本機的 67/67 搬不過去。
    """

    FIXTURES = (
        ("(測試|全套|自測)[^。]{0,20}(過|通過)[^。]{0,12}(所以|因此)[^。不沒未]{0,14}上線",
         "全套測試過了所以已經上線"),
        (r"mcp__[A-Za-z0-9_]+", "我用了 mcp__browser"),
        ("(實查|查過|我讀了|確認過)", "我查過了"),
        ("第三(次|輪|遍)[^。\n]{0,10}(同樣|一樣)", "第三次用同樣的參數"),
        ("(亂碼|mojibake)[^。\n]{0,14}(檔案|檔)(壞|損毀)", "輸出是亂碼，檔案壞了"),
        ("apple|banana", "banana"),
    )

    def test_every_fixture_pattern_survives(self):
        for pattern, sentence in self.FIXTURES:
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(re.search(pattern, sentence), "樣本本身要真的命中")
                self.assertFalse(memspec.prefilter_misses(pattern, sentence))

    def test_a_synthetic_vault_card_is_covered_end_to_end(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="epitype-prefilter-") as temporary:
            vault = Path(temporary)
            (vault / "card.md").write_text(
                "---\nname: 樣本\ndescription: 說明\ndecision_key: sample\nstatus: active\n"
                "current_decision_at: 2026-09-20\ndecided_by: owner-explicit\nowner_quote: x\n"
                "forbidden:\n  - (甲式|乙式)一定會出現\n"
                'example_blocks:\n  - "乙式一定會出現"\n'
                'example_allows:\n  - "今天天氣不錯"\n---\nbody\n',
                encoding="utf-8")
            checked = 0
            for _relative, path, _m, _s, _c in cardscan.scan_vault(vault.resolve()):
                front_lines, _closing = memspec.split_frontmatter(
                    path.read_text(encoding="utf-8"))
                for pattern in memspec.sequence_items(front_lines, memspec.FORBIDDEN_FIELD):
                    for sentence in memspec.sequence_items(
                            front_lines, memspec.EXAMPLE_BLOCKS_FIELD):
                        if re.search(pattern, sentence) is None:
                            continue
                        checked += 1
                        self.assertFalse(memspec.prefilter_misses(pattern, sentence))
            self.assertGreater(checked, 0, "合成庫裡一組規則加例句都沒掃到")


class AgainstTheRealVaults(unittest.TestCase):
    """作者本機四個庫的真規則、真例句。**找不到就整題跳過**，不當成失敗。

    這一題是額外的稽核，不是必跑覆蓋——必跑那一份在上面用專案自帶的樣本。
    """

    VAULTS = (
        Path.home() / ".claude" / "projects" / "C--" / "memory",
        Path.home() / ".claude" / "projects" / "C--Users-User-Desktop-titan--" / "memory",
        Path.home() / ".claude" / "projects" / "C--dev-daipai" / "memory",
        Path.home() / ".claude" / "projects" / "C--Users-User-Desktop-beer--" / "memory",
    )

    def test_no_card_example_is_filtered_away(self):
        present = [vault for vault in self.VAULTS if vault.is_dir()]
        if not present:
            self.skipTest("這台機器上沒有那幾個私人記憶庫；必跑覆蓋在合成樣本那一題")
        checked = 0
        for vault in present:
            if not vault.is_dir():
                continue
            for _relative, path, _mtime, _size, _ctime in cardscan.scan_vault(vault.resolve()):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                front_lines, _closing = memspec.split_frontmatter(text)
                if front_lines is None:
                    continue
                patterns = list(memspec.sequence_items(front_lines, memspec.FORBIDDEN_FIELD))
                fields, _problem = memspec.frontmatter_text(text)
                when = str(fields.get(memspec.REQUIRE_WHEN_FIELD) or "").strip()
                if when:
                    patterns.append(when)
                examples = memspec.sequence_items(front_lines, memspec.EXAMPLE_BLOCKS_FIELD)
                if not patterns or not examples:
                    continue
                for pattern in patterns:
                    try:
                        regex = re.compile(pattern)
                    except re.error:
                        continue
                    for sentence in examples:
                        if regex.search(sentence) is None:
                            continue
                        checked += 1
                        self.assertFalse(
                            memspec.prefilter_misses(pattern, sentence),
                            msg="%s 的例句「%s」被前置過濾擋掉了" % (path.name, sentence[:40]))
        self.assertGreater(checked, 0, "四個庫裡一組真規則加真例句都沒掃到，這一題等於沒跑")


def _selftest():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromTestCase(case)
        for case in (WhatItCanProve, ItNeverRejectsARealMatch, AgainstFixtureCards,
                     AgainstTheRealVaults)
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
