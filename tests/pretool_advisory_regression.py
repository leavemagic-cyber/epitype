"""Narrow owner-authorized A01-A04/A14-A25/A38 regressions.

EPITYPE_GATE_BASELINE may name the unchanged checkout for before/after evidence.
All state, files, Git repositories and subprocesses belong to temporary fixtures.
"""
import sys
sys.dont_write_bytecode = True
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude"), str(ROOT / "tests")]
import pretooluse_gate as gate
import _hook_common as common
from epitype import action_receipts, action_targets, control_lifecycle as life, memspec
from control_block_regression import chrome_create, chrome_context, keep_command, pre, COLAB, NOTEBOOK


def baseline_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASELINE = os.environ.get("EPITYPE_GATE_BASELINE")
old = baseline_module("before_pretool", Path(BASELINE) / "adapters/claude/pretooluse_gate.py") if BASELINE else None
old_life = baseline_module("before_lifecycle", Path(BASELINE) / "epitype/control_lifecycle.py") if BASELINE else None
if old:
    old.control_lifecycle = old_life


class AdvisoryRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-owner-advisory-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config_file = self.root / "config.json"
        common.write_config(self.config_file, [self.vault])
        self.config = {"vaults": [self.vault], "budget_bytes": 12000}
        self.path = life.database_path(self.config_file)
        env = mock.patch.dict(os.environ, {"EPITYPE_CONFIG": str(self.config_file),
            "EPITYPE_DREAM_MODE": "off", "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)

    def denied(self, value):
        return (value or {}).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def changed(self, name, value):
        self.assertFalse(self.denied(value), name)
        print("AFTER advisory/pass:", name)

    def before_block(self, name, callback):
        if old:
            self.assertTrue(self.denied(callback()), name)
            print("BEFORE deny:", name)

    def review(self, module, target, text="safe", **mocks):
        from epitype import owner_rules
        with mock.patch.object(module.rule_provenance, "claim_violation", return_value=mocks.get("claim")), \
             mock.patch.object(owner_rules, "violation", return_value=mocks.get("owner")), \
             mock.patch.object(module, "_forbidden_write", return_value=mocks.get("forbidden")), \
             mock.patch.object(module, "_recurrence_review", return_value=mocks.get("recurrence")), \
             mock.patch.object(module, "_write_marker", return_value=True), \
             mock.patch.object(module, "_card_review", return_value=mocks.get("lint", (None, None))):
            return module._review_one_target({"session_id": "write"}, self.config, target,
                [text], text, time.monotonic(), "write")

    def guard(self, sequence=(), tool="", substrings=(), on_hit="block"):
        return gate._Guard("fixture", tool, substrings, "bounded fixture", self.vault / "fixture.md",
            (), (), (), "", "", (), "", sequence, "", on_hit)

    def sequence(self, module, event, selector):
        notices = []
        with mock.patch.object(module, "_guards", return_value=[self.guard((selector,))]), \
             mock.patch.object(action_receipts, "read", return_value=()), \
             mock.patch.object(action_receipts, "coverage", return_value=True):
            value = module._guard_review(event, event["tool_name"], event["tool_input"],
                self.config, time.monotonic(), notices, [])
        return value, notices

    def test_a01_unrelated_tool_passes_and_still_reminds(self):
        life.observe(chrome_create("tabs", "create", 101), self.path)
        event = pre("tabs", "Bash", {"command": "echo fixture"})
        self.before_block("A01", lambda: old._control_review(event, "Bash", time.monotonic())[0])
        self.changed("A01", gate._control_review(event, "Bash", time.monotonic())[0])
        self.assertIn("仍未確認收尾", life.reminder(event, self.path, "tool"))
        self.assertEqual(life.status(self.path, "tabs")["pending"][0]["id"], "101")

    def test_a02_natural_keep_stop_and_negation(self):
        life.observe(chrome_create("natural", "create", 101), self.path)
        event = {"session_id": "natural", "prompt": "請保留分頁 101，稍後還要用"}
        if old_life:
            self.assertEqual(old_life.owner_release(event, self.path), [])
        self.assertEqual(life.owner_release(event, self.path), [("chrome-tab", "101")])
        self.assertEqual(life.owner_release({**event, "prompt": "不要保留分頁 101"}, self.path), [])
        self.assertEqual(life.owner_release({**event, "prompt": "先停止，不要再操作"}, self.path), [("chrome-tab", "101")])
        self.assertTrue(life.status(self.path, "natural")["pending"])

    def test_a03_keep_has_normal_result_without_ttl(self):
        life.observe(chrome_create("keep", "create", 102), self.path)
        life.observe(chrome_context("keep", "context", (102, "VM", COLAB)), self.path)
        event = pre("keep", "Bash", keep_command(102))
        self.before_block("A03", lambda: old._control_review(event, "Bash", time.monotonic())[0])
        value, notice = gate._control_review(event, "Bash", time.monotonic())
        self.changed("A03", value)
        self.assertIn("已登記保留", notice)
        with mock.patch.object(life, "_now", return_value=time.time() + 9 * 86400):
            self.assertTrue(life.status(self.path, "keep")["pending"][0].get("kept"))
            self.assertEqual(life.kept_record(self.path, "102", "3", NOTEBOOK, "keep"), "keep")
        with mock.patch.object(life, "_now", return_value=time.time() + 600):
            self.assertTrue(life.declare_keep(event, self.path, "102", "3", NOTEBOOK)[0])
        self.assertEqual(life._keep_main(["102", "--authuser", "3", "--notebook", NOTEBOOK, "--session", "keep"], self.path), 0)

    def test_a04_select_owned_idle_tab_and_allow_explicit_handover(self):
        life.observe(chrome_create("navigate", "create", 103), self.path)
        event = pre("navigate", life.CHROME + "navigate", {"url": "https://example.com"})
        required = self.guard(tool=life.CHROME + "navigate")._replace(requires=("tabId",))
        if old:
            with mock.patch.object(old, "_guards", return_value=[required]):
                self.before_block("A04 missing ID", lambda: old._guard_review(event, event["tool_name"], event["tool_input"], self.config, time.monotonic(), [], []))
        with mock.patch.object(gate, "_guards", return_value=[required]):
            result = gate._handle(event, time.monotonic(), [])
        self.assertFalse(self.denied(result))
        self.assertEqual(result["hookSpecificOutput"]["updatedInput"]["tabId"], 103)
        self.assertNotIn("permissionDecision", result["hookSpecificOutput"])
        notices = []
        self.changed("A04", gate._prepare_input(event, time.monotonic(), notices))
        self.assertEqual(event["tool_input"]["tabId"], 103)
        handed = pre("navigate", life.CHROME + "navigate", {"tabId": 999, "url": "https://example.com"})
        self.assertIsNone(gate._prepare_input(handed, time.monotonic(), []))
        missing = pre("no-ledger", life.CHROME + "navigate", {"url": "https://example.com"})
        notices = []
        self.assertIsNone(gate._prepare_input(missing, time.monotonic(), notices))
        self.assertTrue(notices)

    def test_a04_known_other_tab_and_vm_redirect_still_deny(self):
        life.observe(chrome_create("other-session", "create", 104), self.path)
        other = pre("navigate", life.CHROME + "navigate", {"tabId": 104, "url": "https://example.com"})
        self.assertTrue(self.denied(gate._prepare_input(other, time.monotonic(), [])))
        life.observe(chrome_create("vm", "create", 105), self.path)
        life.observe(chrome_context("vm", "context", (105, "VM", COLAB)), self.path)
        event = pre("vm", "Bash", keep_command(105))
        self.assertTrue(life.declare_keep(event, self.path, "105", "3", NOTEBOOK)[0])
        away = pre("vm", life.CHROME + "navigate", {"tabId": 105, "url": "https://example.com"})
        self.assertTrue(self.denied(gate._prepare_input(away, time.monotonic(), [])))
        self.assertIsNone(gate._prepare_input(pre("vm", life.CHROME + "navigate", {"tabId": 105, "url": COLAB}), time.monotonic(), []))

    def test_note_action_card_is_advisory_but_retained_block_survives(self):
        guard = self.guard(tool="Bash", substrings=("fixture-command",), on_hit="note")
        event = pre("guard", "Bash", {"command": "fixture-command"})
        if old:
            with mock.patch.object(old, "_guards", return_value=[guard]):
                self.before_block("A05-A13 note contract", lambda: old._guard_review(event, "Bash", event["tool_input"], self.config, time.monotonic(), [], []))
        advice = []
        with mock.patch.object(gate, "_guards", return_value=[guard]):
            self.assertIsNone(gate._guard_review(event, "Bash", event["tool_input"], self.config, time.monotonic(), [], advice))
        self.assertTrue(advice)
        with mock.patch.object(gate, "_guards", return_value=[guard._replace(on_hit="block")]):
            self.assertTrue(self.denied(gate._guard_review(event, "Bash", event["tool_input"], self.config, time.monotonic(), [], [])))

    def test_implicit_new_guard_defaults_to_note_explicit_valid_block_survives(self):
        from epitype import rule_context
        from rule_source_fixtures import source_bound_card
        path = self.vault / "implicit-guard.md"
        text = ("---\nname: implicit-guard\ndescription: isolated fixture guard\n"
                "source_agent: codex\nmetadata:\n  type: scar\nguard_tool: Bash\n"
                "guard_all_of:\n  - fixture-command\nguard_advice: isolated fixture advice\n"
                "---\nSynthetic guard body.\n")
        path.write_text(source_bound_card(text, path, source_root=self.root / "sources",
            case_id="implicit-new-guard"), encoding="utf-8")
        event = pre("implicit", "Bash", {"command": "fixture-command"})
        entry = gate._read_guard(path)
        self.assertEqual(entry["source_status"], "VERIFIED")
        self.assertEqual(entry["on_hit"], "note")
        self.assertEqual(gate._Guard(*self.guard()[:-1]).on_hit, "note")
        with mock.patch.object(rule_context, "applicability", return_value=("MATCH", "fixture scope")):
            if old:
                self.before_block("new guard without on_hit", lambda: old._guard_review(
                    event, "Bash", event["tool_input"], self.config, time.monotonic(), [], []))
            advice = []
            self.assertIsNone(gate._guard_review(event, "Bash", event["tool_input"],
                self.config, time.monotonic(), [], advice))
            self.assertTrue(advice)
            # Exercise loader fallback separately from the frontmatter default.
            legacy_entry = {key: value for key, value in entry.items() if key != "on_hit"}
            with mock.patch.object(gate, "_read_guard", return_value=legacy_entry):
                self.assertEqual(gate._guards(self.vault, time.monotonic(), [], event=event)[0].on_hit, "note")
            explicit = source_bound_card(text.replace("guard_tool: Bash", "on_hit: block\nguard_tool: Bash"),
                path, source_root=self.root / "sources", case_id="explicit-retained-guard")
            path.write_text(explicit, encoding="utf-8")
            explicit_entry = gate._read_guard(path)
            self.assertEqual(explicit_entry["source_status"], "VERIFIED")
            self.assertEqual(explicit_entry["on_hit"], "block")
            self.assertTrue(self.denied(gate._guard_review(event, "Bash", event["tool_input"],
                self.config, time.monotonic(), [], [])))
        print("AFTER advisory: implicit new guard; explicit VERIFIED block remains deny")

    def test_a14_encoding_is_repaired_and_parser_finishes(self):
        target = self.root / "fixture.ps1"
        guard = self.guard()._replace(write_check=memspec.WRITE_CHECK_PS51_UTF8_BOM)
        if old:
            with mock.patch.object(old, "_guards", return_value=[guard]):
                self.before_block("A14", lambda: old._write_check_review({}, self.config, target, "# 中文\n", None, time.monotonic()))
        event = pre("encoding", "Write", {"file_path": str(target), "content": "# 中文\nWrite-Output 'ok'\n"})
        notices = []
        self.assertIsNone(gate._prepare_input(event, time.monotonic(), notices))
        self.assertTrue(event["tool_input"]["content"].startswith("\ufeff"))
        self.assertTrue(any("PowerShell" in line for line in notices))
        self.assertFalse(target.exists())

    def test_real_adapter_emits_repaired_input_and_records_that_input(self):
        target = self.root / "actual-fixture.ps1"
        event = pre("real-adapter", "Write", {"file_path": str(target), "content": "# 中文\n"}, call_id="write-fixed")
        event["cwd"] = str(self.root)
        checked = subprocess.run([sys.executable, "-B", str(ROOT / "adapters/claude/pretooluse_gate.py")],
            input=json.dumps(event), capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=dict(os.environ), timeout=15, check=False)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        result = json.loads(checked.stdout)
        self.assertFalse(self.denied(result))
        self.assertNotIn("permissionDecision", result["hookSpecificOutput"])
        updated = result["hookSpecificOutput"]["updatedInput"]
        self.assertTrue(updated["content"].startswith("\ufeff"))
        completed = {**event, "hook_event_name": "PostToolUse", "tool_input": updated,
                     "tool_response": {"exit_code": 0}}
        self.assertTrue(action_receipts.finish(completed, action_receipts.database_path(self.config_file)))
        facts = action_receipts.read(completed, action_receipts.database_path(self.config_file))
        self.assertTrue(facts[-1].success)
        self.assertFalse(target.exists())

    def test_a15_third_read_and_a16_big_read_are_advisory(self):
        target = self.root / "read.txt"
        target.write_text("read fixture", encoding="utf-8")
        payload = {"file_path": str(target)}
        if old:
            value = None
            for _ in range(3):
                value, _ = old._waste_review({"session_id": "old-read"}, "Read", payload, self.config, time.monotonic())
            self.assertTrue(self.denied(value))
            print("BEFORE deny: A15")
        notice = None
        for _ in range(3):
            value, notice = gate._waste_review({"session_id": "new-read"}, "Read", payload, self.config, time.monotonic())
        self.changed("A15", value)
        self.assertIn("第 3 次", notice)
        target.write_bytes(b"x" * memspec.READ_WASTE_BIG_FILE_BYTES)
        self.before_block("A16", lambda: old._waste_review({"session_id": "big"}, "Read", payload, self.config, time.monotonic())[0])
        value, notice = gate._waste_review({"session_id": "big"}, "Read", payload, self.config, time.monotonic())
        self.changed("A16", value)
        self.assertIn("分段讀完", notice)

    def test_a17_a18_a19_write_findings_are_notes(self):
        target = self.vault / "fixture.md"
        target.write_text("previous", encoding="utf-8")
        cases = [
            ("A17", {"forbidden": (self.vault, "old-forbidden", "historical wording")}),
            ("A18", {"lint": ("card format FAIL", None)}),
            ("A19", {"recurrence": gate._deny_value("unarmed recurrence")}),
        ]
        for item, values in cases:
            self.before_block(item, lambda: self.review(old, target, **values)[0])
            value, notices = self.review(gate, target, **values)
            self.changed(item, value)
            self.assertTrue(notices)

    def test_a20_native_managed_safety_key_is_still_protected(self):
        target = self.root / memspec.HOST_NATIVE_CODEX_RULES_FILENAME
        record = {os.path.normcase(str(target)): {"kind": gate._NATIVE_CODEX_RULES, "rules": {"key": "owner-safety-card"}}}
        for module in ([old, gate] if old else [gate]):
            with mock.patch.object(module, "_native_state", return_value=record):
                self.assertTrue(self.denied(module._native_file_review(target, "changed")))

    def test_a21_timeout_dispatch_and_a22_owner_block_are_advisory(self):
        target = self.root / "audit.md"
        for item, values in [("A21", {"claim": "[owner-attribution] admission deadline exhausted (worker reaped)"}),
                             ("A22", {"owner": "generated ruling mismatch"})]:
            self.before_block(item, lambda: self.review(old, target, **values)[0])
            value, notices = self.review(gate, target, **values)
            self.changed(item, value)
            self.assertTrue(notices)
        event = pre("dispatch", "Task", {"prompt": "audit fixture"})
        with mock.patch.object(gate.rule_provenance, "claim_violation", return_value="[owner-attribution] worker reaped"):
            self.changed("A21 dispatch", gate._handle(event, time.monotonic(), []))

    def test_a23_missing_receipt_reads_exact_file_without_echo(self):
        target = self.root / "ordinary.py"
        target.write_text("ordinary fixture", encoding="utf-8")
        event = pre("file", "Edit", {"file_path": str(target), "old_string": "fixture", "new_string": "updated"})
        event["cwd"] = str(self.root)
        selector = "@file.read => @file.change"
        self.before_block("A23", lambda: self.sequence(old, event, selector)[0])
        value, notices = self.sequence(gate, event, selector)
        self.changed("A23", value)
        self.assertTrue(any("完整檔案" in line for line in notices))
        self.assertFalse(any("ordinary fixture" in line for line in notices))

    def test_a23_sensitive_targets_are_never_automatically_read(self):
        for filename in (".env", "rclone.conf", "token.json", "credentials.json", "brain.json", "council_snapshot.json"):
            target = self.root / filename
            event = pre("file", "Edit", {"file_path": str(target)})
            notices = []
            with mock.patch.object(Path, "open", side_effect=AssertionError("sensitive file read")):
                self.assertIsNone(gate._sequence_preflight(event, [action_targets.Action("file.change", str(target))], time.monotonic(), notices, read_files=True))
            self.assertTrue(notices)

    def test_a24_permanent_delete_is_not_authorized_by_worktree_check(self):
        target = self.root / "owned-dir"
        target.mkdir()
        event = pre("delete", "PowerShell", {"command": "Remove-Item -LiteralPath '" + str(target) + "' -Recurse"})
        event["cwd"] = str(self.root)
        self.assertTrue(self.denied(self.sequence(gate, event, "@git.worktrees => @directory.delete")[0]))

    def test_a25_clean_main_is_checked_dirty_main_is_preserved(self):
        repository = self.root / "repo"
        repository.mkdir()
        subprocess.run(["git", "init", str(repository)], capture_output=True, check=True, timeout=5)
        event = pre("main", "Bash", {"command": "git merge feature"})
        event["cwd"] = str(repository)
        selector = "@git.status => @git.main_update"
        self.before_block("A25", lambda: self.sequence(old, event, selector)[0])
        self.changed("A25 clean", self.sequence(gate, event, selector)[0])
        (repository / "owner-work.txt").write_text("uncommitted owner fixture", encoding="utf-8")
        self.assertTrue(self.denied(self.sequence(gate, event, selector)[0]))
        self.assertEqual((repository / "owner-work.txt").read_text(), "uncommitted owner fixture")

    def test_a38_other_project_guard_does_not_route_into_current_cwd(self):
        other = self.root / "other-vault"
        other.mkdir()
        config = {**self.config, "vaults": [self.vault, other]}
        event = pre("scope", "Bash", {"command": "fixture-command"})
        event["cwd"] = str(self.root)
        reader = lambda vault, *_args, **_kwargs: [self.guard(tool="Bash", substrings=("fixture-command",))._replace(path=other / "fixture.md")] if vault == other else []
        if old:
            with mock.patch.object(old, "_guards", side_effect=reader):
                self.before_block("A38", lambda: old._guard_review(event, "Bash", event["tool_input"], config, time.monotonic(), [], []))
        with mock.patch.object(gate, "_guards", side_effect=reader):
            self.changed("A38", gate._guard_review(event, "Bash", event["tool_input"], config, time.monotonic(), [], []))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main(verbosity=2)
