# -*- coding: utf-8 -*-
"""被擋之後重貼整段，owner 會看到同一段話兩次。

宿主是先把字送到 owner 眼前、才跑回合閘，所以「擋下來」擋不掉他已經看到的那一段。
2026-09-20 owner 當場問：「為什麼回復要重複兩次一模一樣文字？根本性處理」。

根本處理不是少擋一點（那等於把規則關掉），是改掉重送的方式：被擋之後只講改掉的部分。
這件事機器看得見——把被擋那一段留著，跟重寫的那一段比重疊度。

2026-09-20 同日需求變更（不是為了讓測試過而放寬）：原本重貼會被「再擋一次」，但觸發的那一刻
重複的那一段 owner 早就看到了，再擋只是第三輪。owner 原話：「我們本意設定那個原則是為了節省
token，反而為了這個原則浪費token 就完全本末倒置」。所以在第一次擋下的理由裡提醒
只補缺少的內容、不用重貼前文（那是唯一來得及的時機）；真的重貼了只記帳、下一則提醒，不擋。

回合閘現在一律不擋（owner 2026-10-03；CORE-25）：沒有被擋下的那一段，也就沒有重寫要比。
舊版本留下的重寫事件只核對來源，不再判重貼、不再要求縮短交付；第一次的提醒說這一則不用重送。
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
import unittest
from unittest.mock import patch

from epitype import memspec

import _hook_common as common
import stop_gate


BLOCKED = (
    "我把三件事都做完了。第一件是把走訪搬出去，第二件是暫存目錄改讀環境變數，"
    "第三件是雜湊改成用到才載入。全套測試 68/68 通過，四個庫的卡片檢查都是 0 個失敗。"
)


class RepeatingABlockedTurn(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-echo-")
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
        import time as _time

        # load_config 的參數是「開跑時間」，超時就回 None——傳 0 等於一開始就過期。
        self.loaded = common.load_config(_time.monotonic())
        self.assertIsNotNone(self.loaded, "測試自己要先拿得到設定")

    def retry(self, message, session="echo-1"):
        return stop_gate._handle(
            {"session_id": session, "stop_hook_active": True,
             "last_assistant_message": message}, __import__("time").monotonic(), [])

    def notes(self, session="echo-1"):
        return common.take_notes(self.loaded, session)

    def test_a_repasted_retry_is_neither_blocked_nor_asked_to_shrink(self):
        self.assertIsNone(self.retry(BLOCKED), "重複的那一段已經在 owner 眼前，再擋只是第三輪")
        self.assertEqual(self.notes(), [])

    def test_a_retry_is_not_judged_again(self):
        defects = []
        self.assertIsNone(stop_gate._handle(
            {"session_id": "echo-1", "stop_hook_active": True, "last_assistant_message": BLOCKED},
            __import__("time").monotonic(), defects))
        log = self.vault / memspec.GATE_LOG_FILENAME
        rows = ([json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                if log.is_file() else [])
        self.assertEqual([row for row in rows if row.get("kind") == memspec.STOP_NOTE_LOG_KIND], [])

    def test_the_first_note_already_says_not_to_resend(self):
        # 能避免重貼的時機只有第一次命中的那一刻；回合不擋，提醒本身就說這一則不用重送。
        card = "\n".join(["---", "name: no-hedge", "description: 不說含糊的完成",
                          "forbidden:", '  - "應該可以"', "---", "", "測試用。", ""])
        (self.vault / "no-hedge.md").write_text(card, encoding="utf-8", newline="\n")
        value = stop_gate._handle(
            {"session_id": "echo-first", "cwd": str(self.root),
             "last_assistant_message": "這樣應該可以。"}, __import__("time").monotonic(), [])
        self.assertIsNone(value)
        notes = self.notes("echo-first")
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("no-hedge", notes[0])
        self.assertIn("這一則不用重送", notes[0])


def _selftest():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(RepeatingABlockedTurn)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
