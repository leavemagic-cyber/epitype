# -*- coding: utf-8 -*-
"""宿主改寫設定檔、丟掉我們的標記之後，Epitype 還認不認得自己的掛鉤登記。

2026-11-12 Claude Code 裝外掛時重寫 ~/.claude/settings.json，序列化器丟掉它不認得的鍵：
群組上的 "id": "epitype" 與單支掛鉤的 additionalContextLimit。掛鉤照跑，但只認標記的判斷
把七個時機點全當成沒登記：夜間檢查誤報「登記落後」，重裝則每個時機點再加一筆（跑兩次），
解除安裝也拿不掉它們。這裡的設定檔照真機被改寫後的樣子做，全在臨時家目錄裡跑。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from install import graft
from adapters.codex import hook_trust


def _no_scheduler(*args, **kwargs):
    raise AssertionError("hook ownership tests must not touch the system scheduler")


class HookOwnershipAfterHostRewrite(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-hookmark-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        environment = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            graft.DREAM_MODE_ENV: "off",
        })
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("EPITYPE_CONFIG", None)
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.parent.mkdir(parents=True)
        python = Path(sys.executable).resolve().as_posix()
        user_hooks = self.home / ".claude" / "hooks"
        # 使用者自己的掛鉤：同一個時機點、同一支直譯器，只是不在 Epitype 的掛鉤目錄。
        self.user_groups = {
            "Stop": [{"hooks": [{
                "type": "command",
                "command": f"{python} -B {user_hooks.as_posix()}/zh_reply_guard.py",
                "timeout": 10}]}],
            "SessionStart": [{"hooks": [{
                "type": "command",
                "command": f'"{python}" "{user_hooks.as_posix()}/memory_lint.py" --surface',
                "timeout": 10}]}],
        }
        self.settings.write_text(json.dumps(
            {"theme": "dark", "hooks": json.loads(json.dumps(self.user_groups))}), encoding="utf-8")
        self.hooks_root = self.home / graft.CONFIG_DIRECTORY / graft.HOOK_DIRECTORY

    def install(self):
        code = graft._install(self.home, output=io.StringIO(), dream_mode="off",
                              scheduler=_no_scheduler)
        self.assertEqual(code, 0)

    def strip_like_the_host(self):
        """Claude Code 2026-11-12 的改寫：群組標記與 additionalContextLimit 全被丟掉。"""
        value = json.loads(self.settings.read_text(encoding="utf-8"))
        stripped = 0
        for groups in value["hooks"].values():
            for group in groups:
                for field in graft.MARKER_FIELDS:
                    stripped += group.pop(field, None) == graft.MARKER_VALUE
                for hook in group.get("hooks", []):
                    hook.pop("additionalContextLimit", None)
        self.settings.write_text(json.dumps(value, indent=2), encoding="utf-8")
        self.assertEqual(stripped, len(graft.EVENTS), "fixture must start from a marked install")
        return value

    def epitype_groups(self, value=None):
        """各時機點裡跑 Epitype 掛鉤目錄程式的群組（不靠標記判斷）。"""
        value = value or json.loads(self.settings.read_text(encoding="utf-8"))
        root = self.hooks_root.resolve().as_posix()
        return {
            event: [group for group in groups
                    if any(root + "/" in hook.get("command", "") for hook in group.get("hooks", []))]
            for event, groups in value.get("hooks", {}).items()
        }

    def test_a_stripped_registration_is_not_reported_missing(self):
        self.install()
        self.strip_like_the_host()
        self.assertEqual(graft.registration_gaps(self.home, hosts=["claude"]), {})

    def test_reinstall_over_a_stripped_file_replaces_instead_of_duplicating(self):
        self.install()
        self.strip_like_the_host()
        self.install()
        after = json.loads(self.settings.read_text(encoding="utf-8"))
        ours = self.epitype_groups(after)
        for event in graft.EVENTS:
            with self.subTest(event=event):
                self.assertEqual(len(ours.get(event, [])), 1, f"{event} registered {len(ours.get(event, []))} times")
        self.assertEqual(graft.registration_gaps(self.home, hosts=["claude"]), {})
        for event, groups in self.user_groups.items():
            for group in groups:
                self.assertIn(group, after["hooks"][event])
        once = self.settings.read_bytes()
        self.install()
        self.assertEqual(self.settings.read_bytes(), once, "a second reinstall must change nothing")

    def test_uninstall_over_a_stripped_file_removes_ours_and_keeps_the_users(self):
        self.install()
        self.strip_like_the_host()
        self.assertEqual(graft._uninstall(self.home, output=io.StringIO(), scheduler=_no_scheduler), 0)
        after = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual({event: groups for event, groups in self.epitype_groups(after).items() if groups}, {})
        self.assertEqual(after["theme"], "dark")
        for event, groups in self.user_groups.items():
            self.assertEqual(after["hooks"][event], groups)

    def test_a_genuinely_missing_or_changed_registration_is_still_reported(self):
        self.install()
        value = self.strip_like_the_host()
        del value["hooks"]["SubagentStop"]
        value["hooks"]["PreCompact"][-1]["hooks"][0]["timeout"] = 30
        self.settings.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(graft.registration_gaps(self.home, hosts=["claude"]),
                         {"claude": ["PreCompact", "SubagentStop"]})

    def test_a_foreign_entry_that_only_mentions_epitype_is_not_adopted(self):
        self.install()
        value = json.loads(self.settings.read_text(encoding="utf-8"))
        python = Path(sys.executable).resolve().as_posix()
        root = self.hooks_root.resolve().as_posix()
        foreign = [
            {"description": "epitype", "hooks": [{
                "type": "command",
                "command": f"{python} {self.home.as_posix()}/.claude/hooks/epitype_notes.py --epitype",
                "timeout": 10}]},
            {"hooks": [{"type": "command", "command": f"{python} {root}-backup/stop.py", "timeout": 10}]},
            {"hooks": [{"type": "command", "command": f"{python} {root}/stop.py && echo epitype",
                        "timeout": 10}]},
            {"hooks": [
                {"type": "command", "command": f"{python} {root}/stop.py", "timeout": 10},
                {"type": "command", "command": f"{python} {self.home.as_posix()}/mine.py", "timeout": 10},
            ]},
        ]
        value["hooks"]["Stop"].extend(json.loads(json.dumps(foreign)))
        self.settings.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(graft.registration_gaps(self.home, hosts=["claude"]), {})
        self.install()
        after = json.loads(self.settings.read_text(encoding="utf-8"))
        for group in foreign:
            self.assertIn(group, after["hooks"]["Stop"])
        self.assertEqual(graft._uninstall(self.home, output=io.StringIO(), scheduler=_no_scheduler), 0)
        after = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(after["hooks"]["Stop"], self.user_groups["Stop"] + foreign)

    def test_an_older_claude_entry_still_carrying_the_codex_only_key_matches(self):
        self.install()
        value = json.loads(self.settings.read_text(encoding="utf-8"))
        for event in ("SessionStart", "UserPromptSubmit"):
            value["hooks"][event][-1]["hooks"][0]["additionalContextLimit"] = 1800
        self.settings.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(graft.registration_gaps(self.home, hosts=["claude"]), {})

    def test_a_marker_that_is_present_must_still_be_ours(self):
        self.install()
        value = json.loads(self.settings.read_text(encoding="utf-8"))
        value["hooks"]["Stop"][-1]["comment"] = "someone else"
        self.settings.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(graft.registration_gaps(self.home, hosts=["claude"]), {"claude": ["Stop"]})


class CodexTrustOwnershipAfterHostRewrite(unittest.TestCase):
    """Codex 信任查核認領自家掛鉤，也不能只靠 id／comment 標記。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-codex-trust-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.hooks_root = self.home / graft.CONFIG_DIRECTORY / graft.HOOK_DIRECTORY
        self.codex = self.home / ".codex"
        self.codex.mkdir()
        # 沒有任何信任紀錄：每一筆認得到的登記都該是 UNTRUSTED，查核不必去叫 codex 本體。
        (self.codex / "config.toml").write_text('model = "synthetic"\n', encoding="utf-8")
        python = Path(sys.executable).resolve().as_posix()
        root = self.hooks_root.resolve().as_posix()
        self.foreign = [
            {"description": "epitype", "hooks": [{
                "type": "command",
                "command": f"{python} {self.home.as_posix()}/.codex/hooks/epitype_notes.py --epitype"}]},
            {"hooks": [{"type": "command", "command": f"{python} {root}-backup/stop.py"}]},
            {"hooks": [{"type": "command", "command": f"{python} {root}/stop.py && echo epitype"}]},
            {"hooks": [
                {"type": "command", "command": f"{python} {root}/stop.py"},
                {"type": "command", "command": f"{python} {self.home.as_posix()}/mine.py"},
            ]},
        ]

    def stripped_registrations(self):
        """安裝器寫給 Codex 的登記，模擬宿主改寫後把 id／comment 標記丟掉。"""
        hooks = {}
        for event, entry in graft._hook_template(True, self.hooks_root).items():
            stripped = sum(entry.pop(field, None) == graft.MARKER_VALUE for field in graft.MARKER_FIELDS)
            self.assertEqual(stripped, 1, f"{event} fixture must start from a marked entry")
            hooks[event] = [entry]
        return hooks

    def check(self, hooks):
        (self.codex / "hooks.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        out = io.StringIO()
        code = hook_trust.run_check(self.home, out, seen_path=self.home / "seen.json")
        return code, out.getvalue()

    def test_stripped_codex_registrations_are_still_recognised(self):
        code, text = self.check(self.stripped_registrations())
        self.assertNotIn("no epitype registrations", text)
        self.assertNotIn("registration set", text)
        self.assertEqual(text.count(": " + hook_trust.UNTRUSTED), len(graft.EVENTS))
        self.assertIn(f"FAIL {len(graft.EVENTS)}/{len(graft.EVENTS)}", text)
        self.assertEqual(code, 1)

    def test_foreign_codex_hooks_are_not_claimed(self):
        code, text = self.check({"Stop": json.loads(json.dumps(self.foreign))})
        self.assertEqual(code, 1)
        self.assertIn("no epitype registrations", text)

    def test_foreign_codex_hooks_beside_stripped_ours_are_not_claimed(self):
        hooks = self.stripped_registrations()
        hooks["Stop"] = json.loads(json.dumps(self.foreign)) + hooks["Stop"]
        code, text = self.check(hooks)
        self.assertNotIn("duplicate=", text)
        self.assertEqual(text.count(": " + hook_trust.UNTRUSTED), len(graft.EVENTS))
        # 別人的四筆排在前面，我們那筆是第 4 號群組：位置鍵要指到它，不是指到別人的。
        self.assertIn("CODEX TRUST Stop stop:4:0: " + hook_trust.UNTRUSTED, text)


def _selftest():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(HookOwnershipAfterHostRewrite),
        loader.loadTestsFromTestCase(CodexTrustOwnershipAfterHostRewrite),
    ])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
