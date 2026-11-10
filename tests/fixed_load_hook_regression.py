"""Delivery readback and lint integration, without touching host configuration."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util
import io
import json
import os
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import ExitStack, contextmanager, redirect_stdout
from epitype import fixed_load as f
from _fixed_load_fixtures import isolate_catalogs


class Delivery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="epitype-fixed-hook-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        environment = patch.dict(os.environ, {
            "HOME": str(self.root), "USERPROFILE": str(self.root),
            "CODEX_HOME": str(self.root / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / ".claude"),
            "EPITYPE_CONFIG": str(self.root / ".epitype/config.json"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        catalogs = isolate_catalogs(self.root)
        catalogs.start()
        self.addCleanup(catalogs.stop)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.report, self.receipt = f.cache_paths(self.root, self.cwd)

    def write_report(self):
        value = f.report(home=self.root, cwd=self.cwd)
        f.publish(value, self.report)
        return value

    def test_repeat_changed_source_and_explicit_receipt(self):
        self.write_report()
        line, state = f.notice(self.report, self.receipt, self.cwd)
        self.assertIn("RUNTIME_UNMEASURED", line)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (line, state))
        f.publish(state, self.receipt)
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))
        self.write_report()
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))
        path = self.root / ".claude/agents/new.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\ndescription: new agent\n---\nbody", encoding="utf-8")
        changed_line, changed_state = f.notice(self.report, self.receipt, self.cwd)
        self.assertIn("報告待更新", changed_line)
        f.publish(changed_state, self.receipt)
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))
        self.write_report()
        self.assertIsNotNone(f.notice(self.report, self.receipt, self.cwd)[0])

    def test_mcp_metadata_refresh_ignores_other_project_and_account_churn(self):
        path = self.root / '.claude.json'
        payload = {'mcpServers': {'shared': {'type': 'http', 'url': 'https://example.invalid'}}}
        path.write_text(json.dumps(payload), encoding='utf-8')
        self.write_report()
        line, state = f.notice(self.report, self.receipt, self.cwd)
        f.publish(state, self.receipt)
        payload['oauthAccount'] = {'private': 'DO_NOT_EXPORT_ACCOUNT_CHANGE'}
        payload['projects'] = {str(self.root / 'other'): {'mcpServers': {'foreign': {'command': 'not-this-project'}}}}
        path.write_text(json.dumps(payload), encoding='utf-8')
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))
        self.write_report()
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))
        payload['mcpServers']['shared']['url'] = 'https://changed.invalid'
        path.write_text(json.dumps(payload), encoding='utf-8')
        changed_line, changed_state = f.notice(self.report, self.receipt, self.cwd)
        self.assertIn('報告待更新', changed_line)
        self.assertNotIn('DO_NOT_EXPORT_ACCOUNT_CHANGE', json.dumps(changed_state))
        self.assertNotIn('https://changed.invalid', json.dumps(changed_state))
        f.publish(changed_state, self.receipt)
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))

    def test_creating_and_removing_mcp_profile_invalidates_a_scoped_report(self):
        self.write_report()
        _, state = f.notice(self.report, self.receipt, self.cwd)
        f.publish(state, self.receipt)
        path = self.root / '.claude.json'
        path.write_text(json.dumps({'mcpServers': {'added': {}}}), encoding='utf-8')
        line, state = f.notice(self.report, self.receipt, self.cwd)
        self.assertIn('報告待更新', line)
        self.write_report()
        _, state = f.notice(self.report, self.receipt, self.cwd)
        f.publish(state, self.receipt)
        path.unlink()
        line, _ = f.notice(self.report, self.receipt, self.cwd)
        self.assertIn('報告待更新', line)

    def test_current_project_mcp_change_is_detected_but_its_session_state_is_ignored(self):
        path = self.root / '.claude.json'
        payload = {'projects': {str(self.cwd): {'mcpServers': {'local': {}}}}}
        path.write_text(json.dumps(payload), encoding='utf-8')
        self.write_report()
        _, state = f.notice(self.report, self.receipt, self.cwd)
        f.publish(state, self.receipt)
        payload['projects'][str(self.cwd)]['hasTrustDialogAccepted'] = True
        path.write_text(json.dumps(payload), encoding='utf-8')
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))
        self.write_report()
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))
        payload['projects'][str(self.cwd)]['disabledMcpServers'] = ['local']
        path.write_text(json.dumps(payload), encoding='utf-8')
        line, state = f.notice(self.report, self.receipt, self.cwd)
        self.assertIn('報告待更新', line)
        self.assertNotIn('disabledMcpServers', json.dumps(state))
        f.publish(state, self.receipt)
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))

    def test_malformed_profile_and_repair_have_sanitized_stable_receipts(self):
        path = self.root / '.claude.json'
        path.write_text('{}', encoding='utf-8')
        self.write_report()
        _, state = f.notice(self.report, self.receipt, self.cwd)
        f.publish(state, self.receipt)
        path.write_text('{"PRIVATE_PARSE_TEXT":', encoding='utf-8')
        line, state = f.notice(self.report, self.receipt, self.cwd)
        self.assertIn('報告待更新', line)
        self.assertNotIn('PRIVATE_PARSE_TEXT', json.dumps(state))
        f.publish(state, self.receipt)
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))
        self.write_report()
        line, state = f.notice(self.report, self.receipt, self.cwd)
        self.assertIn('MCP_SETTINGS_UNREADABLE_OR_INVALID', json.dumps(state))
        f.publish(state, self.receipt)
        path.write_text('{"DIFFERENT_PRIVATE_TEXT":', encoding='utf-8')
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))
        path.write_text('{}', encoding='utf-8')
        line, _ = f.notice(self.report, self.receipt, self.cwd)
        self.assertIn('報告待更新', line)

    def test_other_cwd_and_missing_report_do_not_inject(self):
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))
        self.write_report()
        self.assertEqual(f.notice(self.report, self.receipt, self.root), (None, None))

    def test_corrupt_report_diagnostic_once_without_secret(self):
        self.report.parent.mkdir(parents=True)
        self.report.write_text('{"SECRET_SENTINEL":', encoding="utf-8")
        line, state = f.notice(self.report, self.receipt, self.cwd)
        self.assertNotIn("SECRET_SENTINEL", line)
        f.publish(state, self.receipt)
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))

    def test_lint_refreshes_each_launch_directory_without_overwriting_another(self):
        other = self.root / "other-project"
        other.mkdir()
        with patch.object(Path, "home", return_value=self.root):
            f.memory_lint_lines(self.cwd, self.cwd)
            first_bytes = self.report.read_bytes()
            f.memory_lint_lines(other, other)
        self.assertEqual(self.report.read_bytes(), first_bytes)
        other_report, other_receipt = f.cache_paths(self.root, other)
        self.assertEqual(json.loads(other_report.read_text())["cwd"], str(other))
        for cwd, report, receipt in ((self.cwd, self.report, self.receipt),
                                     (other, other_report, other_receipt)):
            line, state = f.notice(report, receipt, cwd)
            self.assertIsNotNone(line)
            f.publish(state, receipt)
        self.assertEqual(f.notice(self.report, self.receipt, self.cwd), (None, None))

    def test_legacy_fallback_is_same_cwd_only_and_scoped_cache_wins(self):
        legacy = self.root / ".epitype/fixed_load_reports/current.json"
        value = f.report(home=self.root, cwd=self.cwd)
        f.publish(value, legacy)
        report, receipt = f.cache_paths(self.root, self.cwd, legacy=True)
        self.assertEqual(report, legacy)
        self.assertEqual(receipt, self.receipt)
        self.assertIsNotNone(f.notice(report, receipt, self.cwd)[0])
        other = self.root / "other-project"
        other.mkdir()
        report, receipt = f.cache_paths(self.root, other, legacy=True)
        self.assertEqual(f.notice(report, receipt, other), (None, None))
        self.write_report()
        legacy.write_text("corrupt", encoding="utf-8")
        self.assertEqual(f.cache_paths(self.root, self.cwd, legacy=True)[0], self.report)

    def test_refresh_command_publishes_the_cache_consumed_by_sessionstart(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(f.main(["--home", str(self.root), "--cwd", str(self.cwd), "--refresh"]), 0)
        self.assertTrue(self.report.is_file())
        self.assertEqual(json.loads(self.report.read_text())["cwd"], str(self.cwd))
        self.assertIsNotNone(f.notice(self.report, self.receipt, self.cwd)[0])

    def test_cache_keys_do_not_collapse_unicode_slug_collisions(self):
        paths = [f.cache_paths(self.root, self.root / name)[0] for name in ("中文", "測試")]
        self.assertNotEqual(*paths)
        for path in paths:
            self.assertLessEqual(max(len(part) for part in path.relative_to(
                self.root / ".epitype/fixed_load_reports/by-cwd").parts[:-1]), 64)

    def test_lint_wiring_idempotence_failure_and_exit_preservation(self):
        text = '''import os, sys
def _epitype_repo(): return None
def _start_candidates(raw, argv): return [os.getcwd()]
def _status_body(r, mem_dir): return "existing status"
def run_lint(mem_dir, strict, budget, background_full=False):
    r = {}
    body = _status_body(r, mem_dir)
    return body, (1 if strict else 0)
def main(stdin_raw, argv, mem_dir, strict=False, budget=None, full=False):
        return run_lint(mem_dir, strict, budget, background_full=full)
'''
        path = self.root / "memory_lint.py"
        path.write_text(text, encoding="utf-8")
        self.assertTrue(f.wire_memory_lint(path))
        self.assertFalse(f.wire_memory_lint(path))
        scope = {}
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), scope)
        with patch.object(f, "memory_lint_lines", return_value=["advisory source 100/35"]):
            body, status = scope["main"]("", [], self.cwd)
            self.assertIn("advisory source 100/35", body)
            self.assertEqual(status, 0)
        with patch.object(f, "memory_lint_lines", side_effect=ValueError("SECRET_SENTINEL")):
            body, status = scope["main"]("", [], self.cwd, strict=True)
            self.assertIn("INCOMPLETE", body)
            self.assertNotIn("SECRET_SENTINEL", body)
            self.assertEqual(status, 1)

    @contextmanager
    def sessionstart(self):
        # Keep unrelated startup work deterministic while exercising main's output.
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "adapters/claude"))
        spec = importlib.util.spec_from_file_location("fixed_sessionstart", root / "adapters/claude/sessionstart_hook.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from epitype import memspec
        config = {memspec.CONFIG_BUDGET_BYTES_FIELD: 20000, memspec.DREAM_CONFIG_FIELD: {memspec.DREAM_MODE_FIELD: memspec.DREAM_MODE_OFF}}
        vault = self.root / "vault"
        vault.mkdir(exist_ok=True)
        with patch.object(Path, "home", return_value=self.root), \
             patch.object(module, "load_config", return_value=config), \
             patch.object(module, "resolve_vaults", return_value=[]), \
             patch.object(module, "native_cwd_vaults", return_value=[]), \
             patch.object(module, "governance_vault", return_value=vault), \
             patch.object(module, "_startup_segments", side_effect=lambda pieces, *a, **kw: pieces.append("current task signal")), \
             patch.object(module, "_compact_map_line", return_value="current task signal"), \
             patch.object(module, "_handoff_line", return_value=None), \
             patch.object(module, "config_path", return_value=self.root / "no-config"), \
             patch.object(module, "trace_hook"), \
             patch.object(module, "_dream_spawn"), \
             patch.object(module, "_dream_notice", return_value=None), \
             patch.object(module, "_soft_remaining", return_value=0), \
             patch.object(module, "expired", return_value=False):
            yield module

    def event(self, host, source="startup", cwd=None):
        event = {"cwd": str(cwd or self.cwd), "source": source,
                 "session_id": "fixed-load-test", "hook_event_name": "SessionStart"}
        if host == "claude":
            event["transcript_path"] = str(self.root / "session.jsonl")
            event["permission_mode"] = "default"
        else:
            event["model"] = "fixture-model"
        return event

    def invoke(self, module, event):
        output = io.StringIO()
        with patch.object(sys, "argv", ["sessionstart.py"]), \
             patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
             patch.object(module, "_STARTED_AT", time.monotonic()), \
             redirect_stdout(output):
            self.assertEqual(module.main(), 0)
        return output.getvalue()

    def prepare_state(self, status):
        source = self.cwd / "CLAUDE.md"
        source.write_text("synthetic instruction", encoding="utf-8")
        self.receipt.unlink(missing_ok=True)
        self.write_report()
        if status == "stale":
            stamp = source.stat().st_mtime_ns + 1_000_000_000
            source.write_text("changed synthetic instruction", encoding="utf-8")
            os.utime(source, ns=(stamp, stamp))
        elif status == "unreadable":
            self.report.write_text('{"PRIVATE_PARSE_SENTINEL":', encoding="utf-8")
        return f.notice(self.report, self.receipt, self.cwd)

    def test_real_sessionstart_keeps_all_fixed_load_states_off_context_both_hosts(self):
        with self.sessionstart() as module:
            for host in ("claude", "codex"):
                for source in ("startup", "compact"):
                    for status, expected in (("normal", "RUNTIME_UNMEASURED"),
                                             ("stale", "報告待更新"),
                                             ("unreadable", "報告無法讀取")):
                        with self.subTest(host=host, source=source, status=status):
                            line, state = self.prepare_state(status)
                            self.assertIn(expected, line)
                            report_bytes = self.report.read_bytes()
                            event = self.event(host, source)
                            output = self.invoke(module, event)
                            context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
                            self.assertEqual(context, "current task signal")
                            self.assertNotIn("固定負載", output)
                            self.assertNotIn(line, output)
                            self.assertNotIn("PRIVATE_PARSE_SENTINEL", output)
                            self.assertEqual(json.loads(self.receipt.read_text()), state)
                            self.assertEqual(self.report.read_bytes(), report_bytes)
                            before = (self.receipt.read_bytes(), self.receipt.stat().st_mtime_ns)
                            self.assertEqual(self.invoke(module, event), output)
                            self.assertEqual((self.receipt.read_bytes(), self.receipt.stat().st_mtime_ns), before)
                            # Explicit maintenance repairs/refreshes this same cache.
                            maintenance = io.StringIO()
                            with redirect_stdout(maintenance):
                                self.assertEqual(f.main(["--home", str(self.root), "--cwd", str(self.cwd),
                                                         "--refresh", "--json"]), 0)
                            value = json.loads(maintenance.getvalue())
                            self.assertEqual(value, json.loads(self.report.read_text()))
                            self.assertEqual(value["cwd"], str(self.cwd))
                            self.assertEqual(value["runtime_status"], "RUNTIME_UNMEASURED")
                            _, refreshed_state = f.notice(self.report, self.receipt, self.cwd)
                            self.invoke(module, event)
                            self.assertEqual(json.loads(self.receipt.read_text()), refreshed_state or state)

    def test_empty_or_trimmed_context_still_records_observation_both_hosts(self):
        with self.sessionstart() as module:
            for host in ("claude", "codex"):
                for trimmed in (False, True):
                    with self.subTest(host=host, trimmed=trimmed), ExitStack() as stack:
                        _, state = self.prepare_state("stale")
                        if trimmed:
                            stack.enter_context(patch.object(module, "bounded_context", return_value=""))
                        else:
                            stack.enter_context(patch.object(module, "_startup_segments"))
                        self.assertEqual(self.invoke(module, self.event(host)), "")
                        self.assertEqual(json.loads(self.receipt.read_text()), state)

    def test_real_sessionstart_keeps_receipts_scoped_to_cwd(self):
        self.write_report()
        other = self.root / "other-project"
        other.mkdir()
        other_report, other_receipt = f.cache_paths(self.root, other)
        f.publish(f.report(home=self.root, cwd=other), other_report)
        with self.sessionstart() as module:
            self.invoke(module, self.event("claude"))
            original = self.receipt.read_bytes()
            self.assertFalse(other_receipt.exists())
            self.invoke(module, self.event("codex", cwd=other))
            self.assertEqual(json.loads(other_receipt.read_text())["cwd"], str(other))
            self.assertEqual(self.receipt.read_bytes(), original)

    def test_installed_sessionstart_shim_both_host_registrations(self):
        from install import graft
        root = Path(__file__).resolve().parents[1]
        hooks = self.root / ".epitype/hooks"
        hooks.mkdir(parents=True)
        shim = hooks / "sessionstart.py"
        shim.write_bytes(graft._shim_payloads(root)["sessionstart.py"])
        vault = self.root / "vault"
        vault.mkdir()
        config = self.root / ".epitype/config.json"
        config.write_text(json.dumps({"repo_root": str(root), "vaults": [str(vault)],
                                      "dream": {"mode": "off"}}), encoding="utf-8")
        env = dict(os.environ, HOME=str(self.root), USERPROFILE=str(self.root),
                   CODEX_HOME=str(self.root / ".codex"), EPITYPE_CONFIG=str(config),
                   CLAUDE_CONFIG_DIR=str(self.root / ".claude"), PYTHONIOENCODING="utf-8")
        trace = self.root / "shim-trace.jsonl"
        env["EPITYPE_SHIM_TRACE"] = str(trace)
        for host in ("claude", "codex"):
            registration = graft._hook_template(host == "codex", hooks, root)["SessionStart"]
            command = shlex.split(registration["hooks"][0]["command"])
            self.assertEqual(Path(command[1]), shim)
            for status in ("normal", "stale", "unreadable"):
                with self.subTest(host=host, status=status):
                    line, state = self.prepare_state(status)
                    report_bytes = self.report.read_bytes()
                    result = subprocess.run(command, input=json.dumps(self.event(host)), env=env,
                                            cwd=self.cwd, capture_output=True, text=True, encoding="utf-8", timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertNotIn("固定負載", result.stdout)
                    self.assertNotIn(line, result.stdout)
                    self.assertEqual(json.loads(self.receipt.read_text()), state)
                    self.assertEqual(self.report.read_bytes(), report_bytes)
                    record = json.loads(trace.read_text().splitlines()[-1])
                    self.assertEqual(record["exit"], 0)
                    self.assertEqual(Path(record["adapter"]), root / "adapters/claude/sessionstart_hook.py")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
