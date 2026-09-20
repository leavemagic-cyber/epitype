# -*- coding: utf-8 -*-
"""太久沒驗證的狀態卡被端出來時，那一行要自己說出它有多舊。

專案卡記的是某個時間點的狀態。喚回只端一行摘要，三個月前寫的「等 owner 一句話」跟昨天
寫的長得一模一樣。2026-09-19～21 一張 97 天前的卡就這樣被當成現況，連錯五次。常駐規則
寫著「先讀現行版」，但那只是被讀到的字；年齡寫在那一行上，才看得到。

只標、不擋、不多跑一輪。標錯比不標糟（會讓人學會忽略它），所以判不出來的一律不標。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "adapters" / "claude"))

import os
import tempfile
import time
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from epitype import memsearch, memspec

import _hook_common as common
import recall_hook

CARD = """---
name: {name}
description: 飛輪袖套的狀態：{state}
metadata:
  type: {kind}
{verified}aliases:
  - 飛輪袖套
---

飛輪袖套 {state}。
"""


def _days_ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


class OldStateSaysItIsOld(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-stale-")
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

    def card(self, name, kind, verified_days, state="等 owner 一句話"):
        verified = "" if verified_days is None else "last_verified_at: %s\n" % _days_ago(verified_days)
        path = self.vault / (name + ".md")
        path.write_text(CARD.format(name=name, kind=kind, verified=verified, state=state),
                        encoding="utf-8", newline="\n")
        return path

    def recalled(self, session):
        memsearch.build_index(self.vault)
        value = recall_hook._handle(
            {"session_id": session, "cwd": str(self.root), "prompt": "飛輪袖套現在怎麼樣"},
            time.monotonic(), [])
        return "" if value is None else value["hookSpecificOutput"]["additionalContext"]

    def test_mark_is_decided_from_the_card_alone(self):
        old = self.card("project-old", "project", 97)
        self.assertIn("97 天前", recall_hook._stale_state_mark(old))
        self.assertEqual(recall_hook._stale_state_mark(self.card("project-fresh", "project", 3)), "")
        self.assertEqual(recall_hook._stale_state_mark(self.card("lesson-old", "feedback", 400)), "",
                         "教訓不會因為舊就失效；每一行都標等於沒標")
        self.assertEqual(recall_hook._stale_state_mark(self.card("project-undated", "project", None)), "",
                         "沒寫驗證日就是不知道，不准猜")
        broken = self.vault / "broken.md"
        broken.write_text("---\nmetadata:\n  type: project\nlast_verified_at: 2026-13-45\n---\n", encoding="utf-8")
        self.assertEqual(recall_hook._stale_state_mark(broken), "")
        self.assertEqual(recall_hook._stale_state_mark(self.vault / "missing.md"), "")

    def test_the_recalled_line_carries_the_age(self):
        self.card("project-flywheel-sleeve", "project", 97)
        context = self.recalled("stale-1")
        self.assertIn("project-flywheel-sleeve", context, msg=context)
        line = next(item for item in context.splitlines() if "project-flywheel-sleeve" in item)
        self.assertIn("97 天前的狀態", line)
        self.assertLess(line.index("97 天前"), line.index("飛輪袖套"), "標記要在摘要前面，先看到年齡")

    def test_a_recent_state_card_is_recalled_unmarked(self):
        self.card("project-flywheel-sleeve", "project", 2)
        context = self.recalled("stale-2")
        self.assertIn("project-flywheel-sleeve", context, msg=context)
        self.assertNotIn("天前的狀態", context)


def _selftest():
    suite = unittest.TestLoader().loadTestsFromTestCase(OldStateSaysItIsOld)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    bad = len(result.failures) + len(result.errors)
    print("stale_state_recall_regression: %s (%d/%d)"
          % ("OK" if not bad else "FAIL", result.testsRun - bad, result.testsRun))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
