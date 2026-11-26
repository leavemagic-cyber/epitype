"""Writing a new card lists the closest current cards, so the writer updates the old one.

2026-11-12, a project session: the lead wrote a new card about device test packages filling C while the
09-25 card about worktrees filling C stayed untouched. Notice only; never a deny.
"""
import sys
sys.dont_write_bytecode = True
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude"), str(ROOT / "tests")]
import pretooluse_gate as gate  # noqa: E402
import _hook_common as common  # noqa: E402
from epitype import memsearch, memspec  # noqa: E402

OLD_DISK = """---
name: feedback_worktrees_fill_c_drive
description: owner 問 C槽又快速被占，是不是又是你；一天開 25 個 worktree 只開不收，吃掉 38.9 GB
aliases:
  - C槽快滿
  - 硬碟空間不夠
metadata:
  type: feedback
---

owner 逐字：「C槽又快速被占，你確定一下是不是又是你」。

**How to apply:**
- 段落併入後立刻收掉 worktree，分支保留。
- 開新 worktree 前先數，超過 6 個先收。
- 查 C 槽只量自己的可疑目錄，不掃整顆硬碟。
"""

OTHERS = {
    "feedback_reply_in_plain_words.md": ("回覆 owner 用白話，不堆代號", "結論先講，原因一句，不要列內部代號。"),
    "feedback_firebase_runs.md": ("每次雲端真機測試都經過 fb_run.sh 上傳", "上傳平行跑，結果讀回才算。"),
    "reference_dot_bridge.md": ("dot 橋接的送件方式與訂閱", "送件用 send_file.py，狀態用 dot_status.py 查。"),
    "feedback_review_whole_first.md": ("整體檢討：最大浪費是寫好的單排隊等同一資料夾", "已做進 launch.py 自動排。"),
}

NEW_PACKAGES = """---
name: feedback-delete-device-packages-after-run
description: owner 11-12：C 槽又被塞滿；真機測試包（每包 2.5–3 GB）跑完就刪
metadata:
  type: feedback
---

owner 原話：「緊急！為什麼c槽又被你塞滿，立刻處理」。

實情：每次上雲端真機測試都把測試包下載到本機，跑完沒刪，累積 14 包約 37 GB。
- 新開的程式工作區併入後用 wt_retire.py 收掉。
- 開始會下載或產生超過 1 GB 的工作前，先看 C 槽剩多少。
"""


def card(description, body, name):
    return f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: feedback\n---\n\n{body}\n"


class SameClassCardRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-same-class-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / "feedback_worktrees_fill_c_drive.md").write_text(OLD_DISK, encoding="utf-8")
        for name, (description, body) in OTHERS.items():
            (self.vault / name).write_text(card(description, body, name[:-3]), encoding="utf-8")
        self.config_file = self.root / "config.json"
        common.write_config(self.config_file, [self.vault])
        self.config = {"vaults": [self.vault], "budget_bytes": 12000}
        env = mock.patch.dict(os.environ, {"EPITYPE_CONFIG": str(self.config_file),
            "EPITYPE_DREAM_MODE": "off", "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        memsearch.build_index(self.vault)

    def review(self, target, text):
        from epitype import owner_rules
        with mock.patch.object(gate.rule_provenance, "claim_violation", return_value=None), \
             mock.patch.object(owner_rules, "violation", return_value=None), \
             mock.patch.object(gate, "_forbidden_write", return_value=None), \
             mock.patch.object(gate, "_recurrence_review", return_value=None), \
             mock.patch.object(gate, "_write_marker", return_value=True), \
             mock.patch.object(gate, "_card_review", return_value=(None, None)):
            return gate._review_one_target({"session_id": "same-class"}, self.config, target,
                                           [text], text, time.monotonic(), "same-class")

    def same_class(self, notices):
        prefix = memspec.WRITE_NOTE_SAME_CLASS.split("{", 1)[0]
        return [notice for notice in notices if notice.startswith(prefix)]

    def test_new_card_names_the_old_same_class_card_first(self):
        value, notices = self.review(self.vault / "feedback_delete_device_packages_after_run.md", NEW_PACKAGES)
        self.assertIsNone(value)
        found = self.same_class(notices)
        self.assertEqual(len(found), 1, notices)
        listed = found[0].split("最接近的現行卡是", 1)[1]
        self.assertTrue(listed.lstrip().startswith("feedback_worktrees_fill_c_drive.md"), found[0])

    def test_new_topic_sharing_only_body_words_gets_no_notice(self):
        text = card("owner 11-12：影片字幕改用思源黑體，行距 1.4",
                    "字幕字型統一用思源黑體，行距 1.4，描邊 2 px；上傳前先看一次。", "feedback-subtitle-font")
        _value, notices = self.review(self.vault / "feedback_subtitle_font.md", text)
        self.assertEqual(self.same_class(notices), [])

    def test_editing_an_existing_card_gets_no_notice(self):
        target = self.vault / "feedback_worktrees_fill_c_drive.md"
        _value, notices = self.review(target, OLD_DISK + "\n- 測試包跑完就刪。\n")
        self.assertEqual(self.same_class(notices), [])

    def test_superseded_card_is_not_offered(self):
        old = self.vault / "feedback_worktrees_fill_c_drive.md"
        old.write_text(OLD_DISK.replace("metadata:\n", "status: superseded\nsuperseded_by: x.md\nmetadata:\n", 1),
                       encoding="utf-8")
        memsearch.build_index(self.vault)
        _value, notices = self.review(self.vault / "feedback_delete_device_packages_after_run.md", NEW_PACKAGES)
        self.assertFalse(any("feedback_worktrees_fill_c_drive.md" in notice
                             for notice in self.same_class(notices)), notices)

    def test_pending_capture_and_no_time_get_no_notice(self):
        pending = self.vault.joinpath(*memspec.CAPTURE_PENDING_SUBPATH) / "draft.md"
        _value, notices = self.review(pending, NEW_PACKAGES)
        self.assertEqual(self.same_class(notices), [])
        with mock.patch.object(gate, "attribution_seconds", return_value=0.0):
            _value, notices = self.review(self.vault / "feedback_new.md", NEW_PACKAGES)
        self.assertEqual(self.same_class(notices), [])


if __name__ == "__main__":
    unittest.main(argv=[argument for argument in sys.argv if argument != "--selftest"])
