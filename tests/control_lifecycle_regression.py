import sys; sys.dont_write_bytecode = True
"""Control-tool delivery contracts; not a GUI state or semantic-completion oracle."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import memspec
import _hook_common as common
import pretooluse_gate as gate


class ControlLifecycleRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-control-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        env = patch.dict(os.environ, {
            memspec.EPITYPE_CONFIG_ENV: str(self.config),
            memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
        })
        env.start()
        self.addCleanup(env.stop)

    def call(self, name, arguments=None):
        return gate._handle({"tool_name": name, "tool_input": arguments or {},
                             "session_id": "synthetic-control"}, time.monotonic())

    def test_control_tools_receive_complete_rule_without_prompt_keywords(self):
        for name in ("mcp__node_repl__js", "mcp__node_repl.js",
                     "mcp__cua_repl__js", "mcp__cua_repl.js",
                     "mcp__claude-in-chrome__tabs_context_mcp",
                     "mcp__claude-in-chrome__tabs_close_mcp",
                     "mcp__Claude_Browser__synthetic_action", "mcp__computer-use__synthetic_action"):
            with self.subTest(tool=name):
                value = self.call(name, {"code": "42"})
                text = (value or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
                for fragment in ("[Epitype: control lifecycle]", "only when needed",
                                 "window/tab/group IDs", "pre-existing",
                                 "Immediately", "actual tool results", "interruption",
                                 "For mixed groups, close only your tabs"):
                    self.assertIn(fragment, text)
                self.assertNotIn("permissionDecision", value["hookSpecificOutput"])
                self.assertLess(len(common.encode_payload(value).encode()), 1400)

    def test_repeated_calls_keep_rule_and_reset_is_not_cleanup_proof(self):
        for _ in range(2):
            value = self.call("mcp__node_repl__js_reset")
            text = (value or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
            self.assertIn("reset does not close browser tabs/groups", text)
            self.assertIn("Do not restart control just to check reset", text)

    def test_generic_repl_advice_is_conditional_not_a_control_verdict(self):
        value = self.call("mcp__node_repl__js", {"code": "nodeRepl.write(2 + 2)"})
        self.assertIn("If this tool is used for UI control", str(value))
        self.assertNotIn("permissionDecision", str(value))

    def test_ordinary_calls_and_quoted_tool_names_do_not_trigger(self):
        for name in ("Read", "Bash", "apply_patch", "request_user_input",
                     "some_mcp__cua_repl__js", "mcp__node_repl__js_fake",
                     "mcp__claude-in-chrome-fake__computer", "mcp__claude-in-chrome__", None, 42):
            self.assertIsNone(self.call(name, {"text": "mcp__cua_repl__js; close Chrome"}))

    def test_existing_scar_deny_still_wins(self):
        (self.vault / "control-scar.md").write_text(
            "---\nname: control-scar\ntrigger:\n  tool: ^mcp__cua_repl__js$\n"
            "  input: forbidden_fixture\nadvice: Preserve the existing safety rule.\n"
            "incident: synthetic\n---\n", encoding="utf-8")
        value = self.call("mcp__cua_repl__js", {"code": "forbidden_fixture"})
        self.assertEqual(value["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("control lifecycle", str(value))

    def test_small_budget_omits_whole_rule_with_diagnostic(self):
        common.write_config(self.config, [self.vault], budget=100)
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertIsNone(self.call("mcp__cua_repl__js"))
        self.assertIn("control lifecycle omitted", error.getvalue())

    def test_expired_or_missing_config_fails_open(self):
        event = {"tool_name": "mcp__cua_repl__js", "tool_input": {}}
        self.assertIsNone(gate._handle(event, time.monotonic() - 20))
        self.config.unlink()
        result = common.run_synthetic(Path(gate.__file__), event, self.config)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
