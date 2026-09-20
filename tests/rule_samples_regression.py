# -*- coding: utf-8 -*-
"""拿真實對話看規則到底擋到哪些句子，以及夜間重放跟閘門的判讀必須一致。

寫卡當下跑的例句是作者自己想的，只測得到作者想得到的情況。2026-09-20 第一次拿真實被擋
的句子回頭看，多條規則大半擋錯。`python -m epitype.compliance --samples` 把那次手動翻
紀錄的做法做成指令：只印、不存。

同一天也照出重放與閘門不一致：閘門 09-19 起在**整則**訊息裡找證據（證據常常就是一段
引文），重放還在引號外找——於是附了引文的句子，閘放行、重放卻算成命中。
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

from epitype import compliance

import stop_gate

CARD = """---
name: owner-words
description: 說到原話就要附引號原文
require_when: 你的原話
require_text: 「
---

測試用。
"""


def _turn(session, text, stamp):
    return [
        json.dumps({"type": "user", "message": {"content": "問"}}, ensure_ascii=False),
        json.dumps({"type": "assistant", "sessionId": session, "timestamp": stamp,
                    "message": {"content": [{"type": "text", "text": text}]}}, ensure_ascii=False),
    ]


class SamplesFromRealTurns(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-samples-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / "owner-words.md").write_text(CARD, encoding="utf-8", newline="\n")
        self.logs = self.root / "projects" / "demo"
        self.logs.mkdir(parents=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 3600))
        lines = []
        lines += _turn("s1", "卡上有你的原話，但我這裡沒有抄下來。後面還有別的事。", stamp)
        lines += _turn("s1", "卡上留的你的原話是「不用導流了」。", stamp)
        lines += _turn("s2", "今天天氣不錯。", stamp)
        (self.logs / "a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def report(self, **options):
        return compliance.samples(self.vault, [self.root / "projects"], **options)

    def test_it_lists_the_real_sentence_that_would_be_blocked(self):
        cards = self.report()["cards"]
        self.assertEqual([entry["card"] for entry in cards], ["owner-words"])
        self.assertEqual(cards[0]["hits"], 1)
        fragment, sentence = cards[0]["sentences"][0]
        self.assertEqual(fragment, "你的原話")
        self.assertIn("沒有抄下來", sentence)
        self.assertNotIn("別的事", sentence, "只給命中的那一句，不是整則")

    def test_a_card_edited_a_minute_ago_is_still_judged_on_past_turns(self):
        # 夜間重放只算卡片存在之後的事；這支要問的相反：剛改好的樣式拿過去的對話跑會怎樣。
        now = time.time()
        os.utime(self.vault / "owner-words.md", (now, now))
        self.assertEqual(self.report()["cards"][0]["hits"], 1)

    def test_card_filter_and_window(self):
        self.assertEqual(self.report(card="no-such-card")["cards"], [])
        self.assertEqual(self.report(days=0)["cards"], [])

    def test_replay_agrees_with_the_gate_when_the_evidence_is_a_quotation(self):
        quoted = "卡上留的你的原話是「不用導流了」。"
        decision = stop_gate._Decision(
            key="owner-words", decided_at="", quote="", forbidden=(), aliases=(), path=Path("."),
            decided_by="", require_when="你的原話", require_text="「", advice="",
            applies_to="", turn_check="", turn_check_limit="")
        self.assertIsNone(stop_gate._requirement_gap(decision, quoted, []), "閘門：附了引文就放行")
        rules = [rule._replace(mtime=0) for rule in compliance.armed_rules(self.vault)]
        hit_texts = [hit.text for hit in compliance.replay(rules, [self.logs / "a.jsonl"])]
        self.assertEqual(len(hit_texts), 1, "沒附引文的那一句要命中——否則下面那個斷言是空的")
        self.assertNotIn(quoted, hit_texts, "重放必須跟閘門同一個判讀，否則報出來的是假漏擋")


def _selftest():
    suite = unittest.TestLoader().loadTestsFromTestCase(SamplesFromRealTurns)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    bad = len(result.failures) + len(result.errors)
    print("rule_samples_regression: %s (%d/%d)"
          % ("OK" if not bad else "FAIL", result.testsRun - bad, result.testsRun))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
