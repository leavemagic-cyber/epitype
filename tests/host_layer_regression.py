import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""規則的執行層：每張行為卡寫明由誰來擋，能交給宿主原生設定擋的就由卡片渲染過去。

2026-09-28 實查：四個庫約 575 張有效行為卡，帶閘欄位的只有幾十張，其餘不是寫了
unenforceable 就是什麼都沒有——只靠代理自己讀。兩個宿主本身都有能直接擋的設定
（Claude 的 permissions.deny／env、Codex 的 config.toml 與 execpolicy 規則檔），卻散在
手寫的設定裡，同一條規則在好幾處各寫一份、沒有正本。

這裡驗四件事：卡片上的宿主原生欄位寫錯格式會被體檢攔下；每張行為卡算得出執行層；
`epitype sync` 把欄位寫進兩邊設定、只動自己寫的鍵、拿得回來、漂移會被點名；代理直接改
那些鍵會被寫檔閘擋下，改別的設定不會。
"""

from datetime import date
import os
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epitype import card_lint, memspec

TODAY = date(2026, 9, 28)


def _card(front, body="body\n"):
    return "---\n" + front.rstrip("\n") + "\n---\n" + body


FEEDBACK_HEAD = (
    "name: {stem}\ndescription: 2026-09-28 {stem}\naliases:\n  - {stem}\n"
    "metadata:\n  type: feedback\n"
)


class _TempVault(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="epitype-hostlayer-")).resolve()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, relative, text, vault=None):
        path = (vault or self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _findings(self, relative, text):
        path = self._write(relative, text)
        _card_type, findings = card_lint.check_card(path, relative, TODAY)
        return findings


class HostFieldLint(_TempVault):
    """D2：四個宿主原生欄位的格式由體檢把關，寫錯的值不准進任何一邊的設定。"""

    def _host_findings(self, extra, stem="host-card"):
        text = _card(FEEDBACK_HEAD.format(stem=stem) + extra)
        return [(level, reason) for level, rule, reason in self._findings(f"{stem}.md", text)
                if rule == "host-field"], self._findings(f"{stem}.md", text)

    def test_well_formed_fields_pass_and_count_as_armed(self):
        extra = (
            "host_claude_env:\n  CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS: \"4\"\n"
            "host_claude_deny:\n  - Bash(git reset --hard:*)\n  - Read(**/.env*)\n"
            "host_codex_config:\n  agents.max_concurrent_threads_per_session: 4\n"
            "  agents.default_subagent_model: gpt-5.6-terra\n"
            "host_codex_rules:\n  - git reset --hard\n  - git push --force|-f\n"
        )
        host, everything = self._host_findings(extra)
        self.assertEqual(host, [])
        rules = {rule for _level, rule, _reason in everything}
        # 宿主原生欄位就是一種擋得住的欄位：不該再被要求補閘欄位或 unenforceable。
        self.assertNotIn("unarmed", rules)

    def test_adding_a_host_field_counts_as_arming_for_the_recurrence_gate(self):
        # 替卡片補上宿主原生欄位就是在讓它擋得住；再犯閘不能把這種寫入當成「只加敘述」。
        for field in memspec.HOST_NATIVE_FIELDS:
            self.assertIn(field, memspec.RECURRENCE_GATE_ARMING_FIELDS)

    def test_codex_config_outside_the_allow_list_fails(self):
        host, _ = self._host_findings("host_codex_config:\n  model: gpt-6-sol\n")
        self.assertEqual(len(host), 1)
        self.assertEqual(host[0][0], card_lint.FAIL)
        self.assertIn("model", host[0][1])
        self.assertIn("agents.max_concurrent_threads_per_session", host[0][1])

    def test_legacy_alias_names_the_canonical_key(self):
        host, _ = self._host_findings("host_codex_config:\n  agents.max_threads: 4\n")
        self.assertEqual(len(host), 1)
        self.assertIn("agents.max_concurrent_threads_per_session", host[0][1])

    def test_codex_config_value_type_is_checked(self):
        for value in ("four", "0", "-2", "3.5"):
            with self.subTest(value=value):
                host, _ = self._host_findings(
                    f"host_codex_config:\n  agents.max_concurrent_threads_per_session: {value}\n",
                    stem=f"type-{value.replace('.', '_').replace('-', 'm')}")
                self.assertEqual(len(host), 1, host)
                self.assertIn(value, host[0][1])

    def test_env_key_and_value_shapes_fail(self):
        host, _ = self._host_findings("host_claude_env:\n  bad-key: \"1\"\n", stem="env-key")
        self.assertEqual(len(host), 1)
        self.assertIn("bad-key", host[0][1])
        host, _ = self._host_findings("host_claude_env:\n  GOOD_KEY: \"\"\n", stem="env-empty")
        self.assertEqual(len(host), 1)
        self.assertIn("GOOD_KEY", host[0][1])
        # 寫成清單不是鍵值：一個值都讀不出來卻不講，等於這條規則安靜地不存在。
        host, _ = self._host_findings("host_claude_env:\n  - GOOD_KEY=1\n", stem="env-list")
        self.assertEqual(len(host), 1)
        self.assertIn(memspec.HOST_CLAUDE_ENV_FIELD, host[0][1])

    def test_deny_rule_shape_fails(self):
        host, _ = self._host_findings("host_claude_deny:\n  - rm -rf /\n", stem="deny-shape")
        self.assertEqual(len(host), 1)
        self.assertIn("rm -rf /", host[0][1])
        host, _ = self._host_findings("host_claude_deny: []\n", stem="deny-empty")
        self.assertEqual(len(host), 1)

    def test_codex_rule_tokens_fail_on_quotes_and_empty_alternatives(self):
        host, _ = self._host_findings('host_codex_rules:\n  - git "push"\n', stem="rule-quote")
        self.assertEqual(len(host), 1)
        self.assertIn('"push"', host[0][1])
        host, _ = self._host_findings("host_codex_rules:\n  - git push --force|\n", stem="rule-alt")
        self.assertEqual(len(host), 1)

    def test_parsed_values_are_what_sync_will_write(self):
        text = _card(FEEDBACK_HEAD.format(stem="parsed") + (
            "host_claude_env: {A_KEY: \"1\", B_KEY: two}\n"
            "host_codex_config:\n  agents.max_depth: 2\n"
            "host_codex_rules: [git clean]\n"))
        front, _closing = memspec.split_frontmatter(text)
        values, problems = memspec.host_native_values(front)
        self.assertEqual(problems, [])
        self.assertEqual(values[memspec.HOST_CLAUDE_ENV_FIELD], {"A_KEY": "1", "B_KEY": "two"})
        self.assertEqual(values[memspec.HOST_CODEX_CONFIG_FIELD], {"agents.max_depth": 2})
        self.assertEqual(values[memspec.HOST_CODEX_RULES_FIELD], [[["git"], ["clean"]]])
        self.assertNotIn(memspec.HOST_CLAUDE_DENY_FIELD, values)


RULE_HEAD = (
    "name: {stem}\ndescription: 2026-09-28 {stem}\nlayer: resident\nsection: execution\n"
    "order: 10\ntext: {stem} text\ndecided_by: owner-explicit\napproved_by: owner\n"
    "approved_at: 2026-09-21\naliases: [{stem}]\nmetadata:\n  type: rule\n"
)


class EnforceTiers(_TempVault):
    """D1：每張行為卡的執行層（host／gate／text／gap），沒有任何一層的列成未分類清單。"""

    def _vault(self):
        vault = self.root / "vault"
        feedback = lambda stem, extra="": _card(FEEDBACK_HEAD.format(stem=stem) + extra)
        self._write("fb-host.md", feedback("fb-host", "host_claude_deny:\n  - Bash(git clean:*)\n"), vault)
        self._write("fb-gate.md", feedback(
            "fb-gate", "forbidden:\n  - 再提議.*改回\nexample_blocks:\n  - 再提議把它改回\n"
            "example_allows:\n  - 為什麼不改回\n"), vault)
        self._write("fb-text.md", feedback("fb-text", "unenforceable: 判斷型，要看整段語意\n"), vault)
        self._write("fb-gap.md", feedback("fb-gap", "unenforceable: 動作閘看不到先前呼叫\n"), vault)
        self._write("fb-none.md", feedback("fb-none"), vault)
        self._write("rule-none.md", _card(RULE_HEAD.format(stem="rule-none")), vault)
        self._write("rule-text.md", _card(
            RULE_HEAD.format(stem="rule-text") + "unenforceable: 判斷型\n"), vault)
        self._write("fb-declared.md", feedback(
            "fb-declared", "enforce: text\nunenforceable: 要讀整段脈絡\n"), vault)
        self._write("corrections/correction-1.md", _card(
            "name: correction-1\ndescription: 2026-09-28 原話\ncaptured_at: 2026-09-28T01:00:00Z\n"
            "session_id: s1\nmetadata:\n  type: correction\n"), vault)
        # 不算行為卡的、已退場的，都不進分層統計。
        self._write("project-x.md", _card(
            "name: project-x\ndescription: 2026-09-28 專案\naliases: [x]\nmetadata:\n  type: project\n"),
            vault)
        self._write("fb-old.md", feedback("fb-old", "status: superseded\n"), vault)
        return vault

    def test_every_behaviour_card_lands_in_exactly_one_layer(self):
        report = card_lint.scan_vault(self._vault(), TODAY)
        self.assertEqual(report["enforce"], {
            memspec.ENFORCE_HOST: 1, memspec.ENFORCE_GATE: 1, memspec.ENFORCE_TEXT: 3,
            memspec.ENFORCE_GAP: 1, memspec.ENFORCE_UNCLASSIFIED: 2, memspec.ENFORCE_EXEMPT: 1,
        })
        self.assertEqual(report["unclassified"], [
            {"path": "fb-none.md", "type": "feedback"},
            {"path": "rule-none.md", "type": "rule"},
        ])

    def test_unclassified_rule_cards_warn_and_feedback_keeps_its_one_finding(self):
        report = card_lint.scan_vault(self._vault(), TODAY)
        by_path = {card["path"]: card for card in report["cards"]}
        rule_findings = [(item["level"], item["rule"]) for item in by_path["rule-none.md"]["findings"]]
        self.assertIn((card_lint.WARN, "unclassified"), rule_findings)
        self.assertNotIn(card_lint.FAIL, [level for level, _rule in rule_findings
                                          if _rule == "unclassified"])
        # 行為糾正卡已經有「沒武裝」那一條，同一件事不重複講兩次。
        feedback = [item["rule"] for item in by_path["fb-none.md"]["findings"]]
        self.assertIn("unarmed", feedback)
        self.assertNotIn("unclassified", feedback)
        # 寫了 unenforceable 的規則卡已經有執行層（text），不再被點名未分類。
        self.assertNotIn("unclassified", [
            item["rule"] for item in by_path.get("rule-text.md", {}).get("findings", ())])

    def test_declared_layer_must_match_what_the_card_carries(self):
        cases = {
            "enforce: gate\nunenforceable: 判斷型\n": "gate",
            "enforce: host\n": "host",
            "enforce: text\n": "text",
            "enforce: loud\nunenforceable: 判斷型\n": "loud",
        }
        for index, (extra, word) in enumerate(cases.items()):
            with self.subTest(extra=extra):
                stem = f"declared-{index}"
                findings = self._findings(f"{stem}.md", _card(FEEDBACK_HEAD.format(stem=stem) + extra))
                enforce = [(level, reason) for level, rule, reason in findings if rule == "enforce"]
                self.assertEqual(len(enforce), 1, findings)
                self.assertEqual(enforce[0][0], card_lint.FAIL)
                self.assertIn(word, enforce[0][1])
        findings = self._findings("declared-ok.md", _card(
            FEEDBACK_HEAD.format(stem="declared-ok")
            + "enforce: host\nhost_codex_rules:\n  - git clean\n"))
        self.assertEqual([rule for _level, rule, _reason in findings if rule == "enforce"], [])

    def test_the_report_prints_the_layer_counts_and_the_unclassified_list(self):
        import io

        output = io.StringIO()
        card_lint._print_report(card_lint.scan_vault(self._vault(), TODAY), output)
        text = output.getvalue()
        self.assertIn("ENFORCE host=1 gate=1 text=3 gap=1 unclassified=2 exempt=1", text)
        self.assertIn("UNCLASSIFIED fb-none.md [feedback]", text)
        self.assertIn("UNCLASSIFIED rule-none.md [rule]", text)

    def test_the_nightly_card_section_carries_the_layers(self):
        from epitype import dream

        vault = self._vault()
        section = dream._section_card_lint([vault], TODAY, TODAY, {})
        counts = section["counts"]
        self.assertEqual(counts["enforce_host"], 1)
        self.assertEqual(counts["enforce_gap"], 1)
        self.assertEqual(counts["enforce_unclassified"], 2)
        self.assertEqual(
            [item["path"] for item in section["unclassified"]], ["fb-none.md", "rule-none.md"])


import contextlib
import io
import json
import tomllib

from epitype import capture_route, host_native, host_sync

GLOBAL_CARD = _card(FEEDBACK_HEAD.format(stem="fb-native") + (
    "host_claude_env:\n  CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS: \"4\"\n"
    "host_claude_deny:\n  - Bash(git clean:*)\n"
    "host_codex_config:\n  agents.max_concurrent_threads_per_session: 4\n"
    "host_codex_rules:\n  - git clean\n  - git push --force|-f\n"))
PROJECT_CARD = _card(FEEDBACK_HEAD.format(stem="fb-project") + (
    "host_claude_deny:\n  - Bash(rm -rf:*)\n"
    "host_codex_config:\n  agents.max_depth: 2\n"))
USER_SETTINGS = (
    '{\n  "env": {\n    "USER_KEY": "1"\n  },\n  "permissions": {\n    "allow": [\n'
    '      "Bash(git status)"\n    ],\n    "deny": [\n      "Read(**/.env*)"\n    ]\n  },\n'
    '  "hooks": {\n    "PreToolUse": [\n      {\n        "hooks": [\n          {\n'
    '            "type": "command",\n            "command": "python gate.py"\n'
    '          }\n        ]\n      }\n    ]\n  },\n  "theme": "dark"\n}\n'
)
HOOKS_BLOCK = USER_SETTINGS[USER_SETTINGS.index('  "hooks"'):USER_SETTINGS.index('  "theme"')]
USER_CONFIG = (
    'model = "synthetic"   # the user picked this\n\n[features]\nhooks = true\n\n'
    "[hooks.state]\n\n[hooks.state.'C:\\hooks.json:pre_tool_use:0:0']\n"
    'trusted_hash = "sha256:abc"\n\n'
)
CONFIG_HOOKS_BLOCK = USER_CONFIG[USER_CONFIG.index("[hooks.state]"):]
GUARD_RULES = 'prefix_rule(pattern=["git", "reset", "--hard"], decision="forbidden")\n'


@contextlib.contextmanager
def _fake_home(home):
    saved = {name: os.environ.get(name) for name in ("USERPROFILE", "HOME", memspec.EPITYPE_CONFIG_ENV)}
    os.environ["USERPROFILE"] = os.fspath(home)
    os.environ["HOME"] = os.fspath(home)
    os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class _NativeFixture(_TempVault):
    """一台合成機器：兩個宿主的使用者設定（含 hooks 段）、手寫規則檔、治理庫與一個專案庫。"""

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex" / "rules").mkdir(parents=True)
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_bytes(USER_SETTINGS.encode("utf-8"))
        self.config = self.home / ".codex" / "config.toml"
        self.guard_rules = self.home / ".codex" / "rules" / "epitype_guard.rules"
        self.guard_rules.write_bytes(GUARD_RULES.encode("utf-8"))
        self.rules = self.home / ".codex" / "rules" / memspec.HOST_NATIVE_CODEX_RULES_FILENAME
        self.governance = self.root / "governance"
        self.governance.mkdir()
        (self.governance / memspec.WORK_LEDGER_FILENAME).write_text("# 帳本\n", encoding="utf-8")
        self.global_card = self._write("fb-native.md", GLOBAL_CARD, self.governance)
        self.project = self.root / "shop"
        self.project.mkdir()
        self.vault = (self.home / ".claude" / "projects"
                      / capture_route.project_slug(self.project) / "memory")
        self.vault.mkdir(parents=True)
        (self.vault.parent / "session.jsonl").write_text(
            json.dumps({"type": "user", "cwd": os.fspath(self.project)}) + "\n", encoding="utf-8")
        self.project_card = self._write("fb-project.md", PROJECT_CARD, self.vault)
        # 專案要被 Codex 信任，專案層設定才會載入；這台合成機器信任它。
        trust = f"[projects.'{os.fspath(self.project)}']\ntrust_level = \"trusted\"\n"
        self.config.write_bytes((USER_CONFIG + trust).encode("utf-8"))
        self.vaults = [self.governance, self.vault]

    def _run(self, function):
        report = io.StringIO()
        with _fake_home(self.home):
            code = function(self.vaults, home=self.home, output=report)
        return code, report.getvalue()

    def _snapshot(self):
        return {path: path.read_bytes() for path in (self.settings, self.config, self.guard_rules)}



class NativeSync(_NativeFixture):
    """D3／D4／D6：卡片→兩邊設定，只動自己寫的鍵，漂移點名到卡片與鍵，拿得回來。"""

    def test_the_whole_round_trip(self):
        before = self._snapshot()
        code, report = self._run(host_sync.check)
        self.assertEqual(code, host_sync.EXIT_DRIFT, report)
        for expected in (
            "env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS 還沒寫入（卡片 fb-native.md",
            "permissions.deny[Bash(git clean:*)] 還沒寫入（卡片 fb-native.md）",
            "agents.max_concurrent_threads_per_session 還沒寫入（卡片 fb-native.md",
            "native:codex/rules/epitype_cards.rules: 規則檔與卡片不一致",
            "permissions.deny[Bash(rm -rf:*)] 還沒寫入（卡片 fb-project.md）",
            "agents.max_depth 還沒寫入（卡片 fb-project.md",
        ):
            self.assertIn(expected, report)
        # 比對只是比對：一個位元組都沒動。
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(self.rules.exists())

        code, report = self._run(host_sync.apply)
        self.assertEqual(code, host_sync.EXIT_OK, report)
        settings_text = self.settings.read_text(encoding="utf-8")
        settings = json.loads(settings_text)
        self.assertEqual(settings["env"], {"USER_KEY": "1", "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "4"})
        self.assertEqual(settings["permissions"]["deny"], ["Read(**/.env*)", "Bash(git clean:*)"])
        self.assertEqual(settings["permissions"]["allow"], ["Bash(git status)"])
        self.assertEqual(settings["theme"], "dark")
        self.assertIn(HOOKS_BLOCK, settings_text, "hooks 段必須逐位元組不變")
        config_text = self.config.read_text(encoding="utf-8")
        config = tomllib.loads(config_text)
        self.assertEqual(config["agents"], {"max_concurrent_threads_per_session": 4})
        self.assertEqual(config["model"], "synthetic")
        self.assertIn('model = "synthetic"   # the user picked this\n', config_text)
        self.assertIn(CONFIG_HOOKS_BLOCK, config_text, "hooks.state 段必須逐位元組不變")
        self.assertEqual(self.guard_rules.read_bytes(), before[self.guard_rules], "手寫規則檔不碰")
        rules = self.rules.read_text(encoding="utf-8")
        self.assertTrue(rules.startswith(memspec.HOST_NATIVE_RULES_HEADER))
        self.assertIn('prefix_rule(pattern=["git", "clean"], decision="forbidden"', rules)
        self.assertIn('prefix_rule(pattern=["git", "push", ["--force", "-f"]], decision="forbidden"', rules)
        for path in (self.settings, self.config):
            backups = [item.name for item in path.parent.iterdir()
                       if item.name.startswith(path.name + ".bak_epitype_native_")]
            self.assertEqual(len(backups), 1, backups)
            self.assertEqual((path.parent / backups[0]).read_bytes(), before[path])
        project_settings = json.loads(
            (self.project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(project_settings, {"permissions": {"deny": ["Bash(rm -rf:*)"]}})
        project_config = tomllib.loads(
            (self.project / ".codex" / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(project_config, {"agents": {"max_depth": 2}})
        # 專案卡沒有 Codex 規則：專案根不長出空的規則檔。
        self.assertFalse((self.project / ".codex" / "rules").exists())
        # 全域卡的值不會混進專案，專案卡的值也不會混進使用者層。
        self.assertNotIn("Bash(rm -rf:*)", settings_text)

        code, report = self._run(host_sync.check)
        self.assertEqual(code, host_sync.EXIT_OK, report)
        self.assertNotIn("DRIFT", report)
        code, report = self._run(host_sync.apply)
        self.assertNotIn("WROTE", report, "第二次同步不該再寫任何檔")

        # 手改一個被管理的鍵＝漂移：點名卡片與鍵，doctor 判 FAIL。
        self.settings.write_text(settings_text.replace('"4"', '"8"'), encoding="utf-8")
        from install import graft

        with _fake_home(self.home):
            lines, drifted = graft._native_sync_status(self.vaults, self.home)
        self.assertTrue(drifted)
        self.assertIn(
            "NATIVE: DRIFT  native:claude/settings.json: env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS "
            "與卡片 fb-native.md 不一致：卡片要 \"4\"，檔案是 \"8\"", lines)
        code, report = self._run(host_sync.check)
        self.assertEqual(code, host_sync.EXIT_DRIFT, report)
        code, report = self._run(host_sync.apply)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["env"][
            "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"], "4")

        # 卡片拿掉欄位→同步拿回 Epitype 寫的鍵，使用者原有的留著。
        self.global_card.write_text(_card(FEEDBACK_HEAD.format(stem="fb-native")
                                          + "unenforceable: 判斷型\n"), encoding="utf-8")
        self.project_card.unlink()
        code, report = self._run(host_sync.apply)
        self.assertEqual(code, host_sync.EXIT_OK, report)
        self.assertEqual(json.loads(self.settings.read_bytes()), json.loads(before[self.settings]))
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf-8")),
                         tomllib.loads(before[self.config].decode("utf-8")))
        self.assertEqual(self.config.read_bytes(), before[self.config])
        self.assertFalse(self.rules.exists())
        self.assertFalse((self.project / ".claude" / "settings.local.json").exists())
        self.assertFalse((self.project / ".codex" / "config.toml").exists())
        self.assertEqual(host_native.load_state(self.home), {})

    def test_a_user_value_that_differs_is_a_conflict_not_an_overwrite(self):
        self.settings.write_text(USER_SETTINGS.replace(
            '"USER_KEY": "1"', '"USER_KEY": "1",\n    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "20"'),
            encoding="utf-8")
        code, report = self._run(host_sync.apply)
        self.assertEqual(code, host_sync.EXIT_REFUSED, report)
        self.assertIn("env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS 使用者已經自己設成 \"20\"", report)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"], "20")
        # 衝突的只有那一個鍵，同一個檔裡的 deny 照寫。
        self.assertIn("Bash(git clean:*)", settings["permissions"]["deny"])

    def test_a_user_value_that_already_matches_is_left_in_place_when_the_card_goes(self):
        self.settings.write_text(USER_SETTINGS.replace(
            '"USER_KEY": "1"', '"USER_KEY": "1",\n    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "4"'),
            encoding="utf-8")
        original = self.settings.read_bytes()
        code, report = self._run(host_sync.apply)
        self.assertEqual(code, host_sync.EXIT_OK, report)
        self.global_card.unlink()
        self._run(host_sync.apply)
        # 那個值原本就是使用者的；卡片拿掉，Epitype 不替他刪。
        self.assertEqual(json.loads(self.settings.read_bytes())["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"], "4")
        self.assertEqual(json.loads(self.settings.read_bytes()), json.loads(original))

    def test_an_invalid_card_blocks_its_whole_vault(self):
        self._write("fb-broken.md", _card(FEEDBACK_HEAD.format(stem="fb-broken")
                                          + "host_codex_config:\n  sandbox_mode: danger-full-access\n"),
                    self.governance)
        before = self._snapshot()
        code, report = self._run(host_sync.apply)
        self.assertEqual(code, host_sync.EXIT_REFUSED, report)
        self.assertIn("fb-broken.md", report)
        self.assertIn("sandbox_mode", report)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(self.rules.exists())

    def test_a_foreign_file_with_our_name_is_never_overwritten(self):
        self.rules.write_text("# mine\n" + GUARD_RULES, encoding="utf-8")
        code, report = self._run(host_sync.apply)
        self.assertEqual(code, host_sync.EXIT_REFUSED, report)
        self.assertEqual(self.rules.read_text(encoding="utf-8"), "# mine\n" + GUARD_RULES)

    def test_an_untrusted_codex_project_is_named(self):
        self.config.write_bytes(USER_CONFIG.encode("utf-8"))
        code, report = self._run(host_sync.check)
        self.assertIn("NOTE   native:project:shop/.codex/config.toml: Codex 只在信任的專案", report)

    def test_uninstall_takes_back_only_what_epitype_wrote(self):
        before = self._snapshot()
        self._run(host_sync.apply)
        report = io.StringIO()
        with _fake_home(self.home):
            host_sync.remove(home=self.home, output=report, vaults=self.vaults)
        self.assertEqual(json.loads(self.settings.read_bytes()), json.loads(before[self.settings]))
        self.assertEqual(self.config.read_bytes(), before[self.config])
        self.assertFalse(self.rules.exists())
        self.assertEqual(self.guard_rules.read_bytes(), before[self.guard_rules])
        self.assertFalse((self.project / ".claude" / "settings.local.json").exists())

    def test_rendered_rules_parse_with_codex_when_it_is_installed(self):
        import shutil
        import subprocess

        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("codex CLI not installed; rules syntax verified by the renderer's own escaping")
        self._run(host_sync.apply)
        # CODEX_HOME 指向假家：驗語法不該碰使用者真正的 Codex 目錄。
        environment = dict(os.environ, CODEX_HOME=os.fspath(self.home / ".codex"))
        done = subprocess.run(
            [codex, "execpolicy", "check", "--rules", os.fspath(self.rules), "git", "push", "-f"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            env=environment)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('"decision":"forbidden"', done.stdout)


class NativeGuard(_NativeFixture):
    """D5：代理直接改 Epitype 管理的鍵時，寫檔閘擋下；讀、`epitype sync`、改別的設定都放行。"""

    def setUp(self):
        super().setUp()
        code, report = self._run(host_sync.apply)
        self.assertEqual(code, host_sync.EXIT_OK, report)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters" / "claude"))
        import _hook_common as common

        self.hook_config = self.home / ".epitype" / "config.json"
        common.write_config(self.hook_config, self.vaults)
        saved = {name: os.environ.get(name) for name in
                 ("USERPROFILE", "HOME", memspec.EPITYPE_CONFIG_ENV, memspec.DREAM_MODE_ENV)}
        os.environ.update({"USERPROFILE": os.fspath(self.home), "HOME": os.fspath(self.home),
                           memspec.EPITYPE_CONFIG_ENV: os.fspath(self.hook_config),
                           memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF})

        def restore():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)

    def _denial(self, tool_name, tool_input):
        import time

        import pretooluse_gate as pretool

        value = pretool._handle({"tool_name": tool_name, "tool_input": tool_input,
                                 "session_id": "native-guard", "cwd": os.fspath(self.root)},
                                time.monotonic(), [])
        return (value or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason")

    def _patch(self, path, body):
        return {"command": ["apply_patch", "*** Begin Patch\n*** Update File: " + os.fspath(path)
                            + "\n" + body + "*** End Patch\n"]}

    def test_editing_a_managed_key_is_denied_and_points_at_the_card(self):
        reason = self._denial("Edit", {
            "file_path": os.fspath(self.settings),
            "old_string": '"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "4"',
            "new_string": '"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "8"'})
        self.assertIsNotNone(reason)
        self.assertIn("env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", reason)
        self.assertIn("fb-native.md", reason)
        self.assertIn("epitype sync --apply", reason)
        self.assertIn(memspec.HOST_CLAUDE_ENV_FIELD, reason)

    def test_other_ways_of_changing_managed_keys_are_denied(self):
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        settings["permissions"]["deny"] = ["Read(**/.env*)"]
        cases = {
            "Write drops a managed deny rule": ("Write", {
                "file_path": os.fspath(self.settings), "content": json.dumps(settings, indent=2)}),
            "Edit a managed Codex key": ("Edit", {
                "file_path": os.fspath(self.config),
                "old_string": "max_concurrent_threads_per_session = 4",
                "new_string": "max_concurrent_threads_per_session = 16"}),
            "Write the generated rules file": ("Write", {
                "file_path": os.fspath(self.rules), "content": "# mine\n"}),
            "apply_patch a managed Codex key": ("Bash", self._patch(
                self.config, "@@\n-max_concurrent_threads_per_session = 4\n"
                "+max_concurrent_threads_per_session = 16\n")),
            "apply_patch removing a managed deny rule": ("Bash", self._patch(
                self.settings, '@@\n-      "Read(**/.env*)",\n-      "Bash(git clean:*)"\n'
                '+      "Read(**/.env*)"\n     ]\n')),
            "apply_patch deleting the rules file": ("Bash", {"command": [
                "apply_patch", "*** Begin Patch\n*** Delete File: " + os.fspath(self.rules)
                + "\n*** End Patch\n"]}),
            "project settings edited directly": ("Edit", {
                "file_path": os.fspath(self.project / ".claude" / "settings.local.json"),
                "old_string": '"Bash(rm -rf:*)"', "new_string": '"Bash(ls:*)"'}),
            "PowerShell rewrite": ("PowerShell", {"command": (
                f"(Get-Content '{self.settings}') -replace '\"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS\": \"4\"',"
                f"'\"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS\": \"8\"' | Set-Content '{self.settings}'")}),
            "python one-liner": ("Bash", {"command": (
                "python -c \"import json;p=r'" + os.fspath(self.config) + "';"
                "open(p,'a').write('max_concurrent_threads_per_session = 9')\"")}),
            "overwrite by redirection": ("Bash", {"command": f"echo {{}} > '{self.settings}'"}),
            "sed into the rules file": ("Bash", {"command": f"sed -i 's/git/xit/' {self.rules}"}),
        }
        for name, (tool, tool_input) in cases.items():
            with self.subTest(name=name):
                self.assertIsNotNone(self._denial(tool, tool_input), name)

    def test_reads_sync_and_the_users_other_settings_are_allowed(self):
        cases = {
            "Read tool": ("Read", {"file_path": os.fspath(self.settings)}),
            "Grep whose pattern looks like a write": ("Grep", {
                "pattern": "rm -rf|CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", "path": os.fspath(self.settings)}),
            "grep with stderr redirected": ("Bash", {"command": (
                f"grep CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS '{self.settings}' 2>/dev/null")}),
            "Get-Content of the rules file": ("PowerShell", {
                "command": f"Get-Content '{self.rules}'"}),
            "copy the settings out for a backup": ("Bash", {
                "command": f"cp '{self.settings}' /tmp/backup.json"}),
            "epitype sync": ("Bash", {"command": "python -m epitype sync --apply > sync.log"}),
            "epitype sync by path": ("Bash", {
                "command": "python epitype/host_sync.py --apply >> settings.json.log"}),
            "Edit the user's own key": ("Edit", {
                "file_path": os.fspath(self.settings),
                "old_string": '"theme": "dark"', "new_string": '"theme": "light"'}),
            "Edit the user's own env": ("Edit", {
                "file_path": os.fspath(self.settings),
                "old_string": '"USER_KEY": "1"', "new_string": '"USER_KEY": "2"'}),
            "add another deny rule by hand": ("Edit", {
                "file_path": os.fspath(self.settings),
                "old_string": '"Read(**/.env*)",', "new_string": '"Read(**/.env*)",\n      "Bash(curl:*)",'}),
            "apply_patch the user's own Codex key": ("Bash", self._patch(
                self.config, '@@\n-model = "synthetic"   # the user picked this\n'
                '+model = "other"   # the user picked this\n')),
            "the hand-written rules file": ("Write", {
                "file_path": os.fspath(self.guard_rules), "content": GUARD_RULES + "# more\n"}),
            "an unmanaged config.toml elsewhere": ("Write", {
                "file_path": os.fspath(self.root / "elsewhere" / "config.toml"),
                "content": "max_concurrent_threads_per_session = 99\n"}),
        }
        for name, (tool, tool_input) in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(self._denial(tool, tool_input), name)


class DoctorSeesNativeDrift(unittest.TestCase):
    """D4 端到端：真的裝一次，doctor 在設定被手改之後判 FAIL，並點名卡片與鍵。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-hostdoctor-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                        PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                        EPITYPE_DREAM_MODE="off")
        self.env.pop(memspec.EPITYPE_CONFIG_ENV, None)

    def epitype(self, *args):
        import subprocess

        done = subprocess.run([sys.executable, "-m", "epitype", *args], cwd=str(self.home),
                              env=self.env, capture_output=True, timeout=240)
        return done.returncode, (done.stdout + done.stderr).decode("utf-8", "replace")

    def test_doctor_fails_on_a_hand_edited_managed_key(self):
        code, text = self.epitype("install", "--home", str(self.home))
        self.assertEqual(code, 0, text[-800:])
        config = json.loads((self.home / ".epitype" / "config.json").read_text(encoding="utf-8"))
        vault = Path(config["vaults"][0])
        (vault / "fb-native.md").write_text(_card(FEEDBACK_HEAD.format(stem="fb-native") + (
            "host_claude_env:\n  CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS: \"4\"\n")), encoding="utf-8")
        code, text = self.epitype("doctor")
        self.assertEqual(code, 1, text[-800:])
        self.assertIn("NATIVE: DRIFT  native:claude/settings.json: "
                      "env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS 還沒寫入（卡片 fb-native.md", text)
        self.assertIn(memspec.HOST_NATIVE_DOCTOR_FAIL, text)
        code, text = self.epitype("sync", "--apply")
        self.assertEqual(code, 0, text[-800:])
        code, text = self.epitype("doctor")
        self.assertEqual(code, 0, text[-800:])
        self.assertIn("NATIVE: PASS", text)
        settings = self.home / ".claude" / "settings.json"
        settings.write_text(settings.read_text(encoding="utf-8").replace('"4"', '"8"'), encoding="utf-8")
        code, text = self.epitype("doctor")
        self.assertEqual(code, 1, text[-800:])
        self.assertIn("env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS 與卡片 fb-native.md 不一致："
                      "卡片要 \"4\"，檔案是 \"8\"", text)


def _selftest():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
