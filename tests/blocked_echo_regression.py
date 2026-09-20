# -*- coding: utf-8 -*-
"""被擋之後重貼整段，owner 會看到同一段話兩次。

宿主是先把字送到 owner 眼前、才跑回合閘，所以「擋下來」擋不掉他已經看到的那一段。
2026-09-20 owner 當場問：「為什麼回復要重複兩次一模一樣文字？根本性處理」。

根本處理不是少擋一點（那等於把規則關掉），是改掉重送的方式：被擋之後只講改掉的部分。
這件事機器看得見——把被擋那一段留著，跟重寫的那一段比重疊度。

2026-09-20 同日需求變更（不是為了讓測試過而放寬）：原本重貼會被「再擋一次」，但觸發的那一刻
重複的那一段 owner 早就看到了，再擋只是第三輪。owner 原話：「我們本意設定那個原則是為了節省
token，反而為了這個原則浪費token 就完全本末倒置」。所以改成：「不要整段重貼」這句話放進第一次
擋下的理由裡（那是唯一來得及的時機）；真的重貼了只記帳、下一則提醒，不擋。
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

    def remember(self, session="echo-1", message=BLOCKED):
        stop_gate._remember_blocked(self.loaded, session, message)

    def notes(self, session="echo-1"):
        return common.take_notes(self.loaded, session)

    def test_pasting_the_same_thing_again_is_noted_not_blocked(self):
        self.remember()
        self.assertIsNone(self.retry(BLOCKED), "重複的那一段已經在 owner 眼前，再擋只是第三輪")
        self.assertTrue(any("同一段話看了兩次" in note for note in self.notes()))

    def test_it_is_on_the_books(self):
        self.remember()
        self.retry(BLOCKED)
        rows = [json.loads(line) for line in
                (self.vault / memspec.GATE_LOG_FILENAME).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(row["kind"], row["decision"]) for row in rows],
                         [(memspec.STOP_NOTE_LOG_KIND, memspec.BLOCKED_ECHO_DECISION)])

    def test_a_small_edit_is_still_the_same_paste(self):
        self.remember()
        self.retry(BLOCKED.replace("68/68", "68 分之 68"))
        self.assertTrue(self.notes())

    def test_saying_only_what_changed_leaves_no_note(self):
        self.remember()
        self.assertIsNone(self.retry("更正一句：測試是 68/68，不是 67/67。"))
        self.assertEqual(self.notes(), [])

    def test_without_a_previous_block_nothing_is_judged(self):
        self.assertIsNone(self.retry(BLOCKED, session="echo-fresh"))
        self.assertEqual(self.notes("echo-fresh"), [])

    def test_a_short_previous_block_is_not_enough_to_judge_on(self):
        # 一句「好的。」重疊度永遠很高，拿它當判準會亂記。
        self.remember(message="好。")
        self.retry("好。")
        self.assertEqual(self.notes(), [])

    def test_the_first_block_already_says_not_to_repaste(self):
        # 能避免重貼的時機只有第一次擋下的那一刻；等重貼發生了才講，owner 已經看了兩次。
        card = "\n".join(["---", "name: no-hedge", "description: 不說含糊的完成",
                          "forbidden:", '  - "應該可以"', "---", "", "測試用。", ""])
        (self.vault / "no-hedge.md").write_text(card, encoding="utf-8", newline="\n")
        value = stop_gate._handle(
            {"session_id": "echo-first", "cwd": str(self.root),
             "last_assistant_message": "這樣應該可以。"}, __import__("time").monotonic(), [])
        self.assertIsNotNone(value)
        self.assertIn("不要整段重貼", value["reason"])


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
