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


def _selftest():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
