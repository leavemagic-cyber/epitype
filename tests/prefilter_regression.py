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


class AgainstTheRealVaults(unittest.TestCase):
    """真規則、真例句：每一張卡自己的必擋例句，過濾都不准擋掉。"""

    VAULTS = (
        Path.home() / ".claude" / "projects" / "C--" / "memory",
        Path.home() / ".claude" / "projects" / "C--Users-User-Desktop-titan--" / "memory",
        Path.home() / ".claude" / "projects" / "C--dev-daipai" / "memory",
        Path.home() / ".claude" / "projects" / "C--Users-User-Desktop-beer--" / "memory",
    )

    def test_no_card_example_is_filtered_away(self):
        checked = 0
        for vault in self.VAULTS:
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
        for case in (WhatItCanProve, ItNeverRejectsARealMatch, AgainstTheRealVaults)
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
