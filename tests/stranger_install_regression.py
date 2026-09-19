# -*- coding: utf-8 -*-
"""陌生人裝了會不會跟 owner 一樣：乾淨環境一行裝、真的擋得住、一行移除乾淨。

owner 2026-09-19 裁定只有一個版本（「我怎麼用，其他使用者我覺得應該一樣，不然為什麼做
開源」），所以這件事要有一題釘住，而不是靠「在我機器上會動」。

整個測試在一個臨時家目錄裡跑：HOME、USERPROFILE、設定檔全部指到那裡，不碰真的機器。
關鍵是**不用真機那四個庫**——陌生人沒有那些卡，他拿到的是空的記憶庫。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import json
import os
import subprocess
import tempfile
import unittest


class AStrangerInstall(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-stranger-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.project = self.home / "some-project"
        self.project.mkdir(parents=True, exist_ok=True)
        # 陌生人是在「已經有 Claude Code」的機器上裝 Epitype，所以這台合成機器要長得
        # 像那樣：宿主的設定檔在、而且是他自己的內容（裝完之後那些內容必須還在）。
        claude = self.home / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "settings.json").write_text(
            json.dumps({"theme": "dark"}, ensure_ascii=False), encoding="utf-8")
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "EPITYPE_DREAM_MODE": "off",
        })
        self.env.pop("EPITYPE_CONFIG", None)

    def epitype(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, "-m", "epitype", *args],
            cwd=str(cwd or self.project), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)

    def hook(self, script, event):
        return subprocess.run(
            [sys.executable, str(ROOT / "adapters" / "claude" / script)],
            input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def test_one_command_installs_blocks_and_one_command_removes(self):
        done = self.epitype("install", "--home", str(self.home))
        self.assertEqual(done.returncode, 0,
                         msg=done.stderr.decode("utf-8", "replace")[-600:])
        config = self.home / ".epitype" / "config.json"
        self.assertTrue(config.exists(), "裝完要有設定檔，不然掛鉤找不到任何庫")
        settings = self.home / ".claude" / "settings.json"
        self.assertTrue(settings.exists(), "裝完要接上宿主的掛鉤，不然規則到不了代理面前")
        # 那個檔是使用者的，不是我們的：他自己的設定必須原封不動留著。
        self.assertEqual(
            json.loads(settings.read_text(encoding="utf-8")).get("theme"), "dark",
            "裝的時候把使用者自己的設定洗掉了")

        # 陌生人的庫是空的，所以先給他一張跟我們一樣的卡——這正是「規則從糾正長出來」的
        # 第一步。卡的寫法與真機完全相同，沒有任何「示範用」的特例。
        vault = json.loads(config.read_text(encoding="utf-8"))["vaults"][0]
        card = Path(vault) / "scar-stranger.md"
        card.write_text(
            "---\nname: 陌生人的第一張傷疤卡\ndescription: 這個指令在這台機器上出過事\n"
            'guard_tool: Bash\nguard_all_of:\n  - "危險指令"\n  - "--force"\n'
            "guard_advice: 改用安全的那一種寫法\nlast_verified_at: 2026-09-19\n"
            'example_blocks:\n  - "危險指令 --force"\n'
            'example_allows:\n  - "git status --short"\n---\nbody\n',
            encoding="utf-8")

        blocked = self.hook("pretooluse_gate.py", {
            "tool_name": "Bash",
            "tool_input": {"command": "危險指令 --force"},
            "session_id": "stranger-1",
            "cwd": str(self.project),
        })
        payload = blocked.stdout.decode("utf-8", "replace").strip()
        self.assertTrue(payload, "裝好之後第一次踩到自己的卡，應該要被擋下來")
        decision = json.loads(payload)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "deny")

        allowed = self.hook("pretooluse_gate.py", {
            "tool_name": "Bash",
            "tool_input": {"command": "git status --short"},
            "session_id": "stranger-1",
            "cwd": str(self.project),
        })
        self.assertEqual(allowed.stdout.decode("utf-8", "replace").strip(), "",
                         "沒踩到規則的指令不該被擋——誤擋比漏擋貴")

        removed = self.epitype("uninstall", "--home", str(self.home))
        self.assertEqual(removed.returncode, 0,
                         msg=removed.stderr.decode("utf-8", "replace")[-600:])
        after = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
        hooks = json.dumps(after.get("hooks", {}), ensure_ascii=False)
        self.assertNotIn("epitype", hooks.lower(), "移除之後宿主設定裡不該還留著掛鉤")

    def test_the_subagent_event_is_registered(self):
        # 2026-09-20 Codex 審查抓到：子代理落檔的程式寫好了，安裝器卻沒註冊那個事件。
        # owner 的機器是當天手動補的，所以本機看起來正常——陌生人裝完永遠收不到。
        self.assertEqual(self.epitype("install", "--home", str(self.home)).returncode, 0)
        settings = json.loads(
            (self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("SubagentStop", settings.get("hooks", {}),
                      "標準安裝沒有接上子代理結束，落檔功能等於沒有")

    def test_the_doctor_runs_on_a_fresh_install(self):
        self.assertEqual(self.epitype("install", "--home", str(self.home)).returncode, 0)
        done = self.epitype("doctor")
        text = (done.stdout + done.stderr).decode("utf-8", "replace")
        self.assertIn("PASS", text, msg=text[-600:])


def _selftest():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(AStrangerInstall)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
