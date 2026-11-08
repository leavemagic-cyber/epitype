"""Delivery readback and lint integration, without touching host configuration."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ast
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from epitype import fixed_load as f


class Delivery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="epitype-fixed-hook-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.report = self.root / ".epitype/fixed_load_reports/current.json"
        self.receipt = self.report.with_name("notified.json")

    def write_report(self):
        value = f.report(home=self.root, cwd=self.cwd)
        f.publish(value, self.report)
        return value

    def test_repeat_changed_source_and_receipt_only_after_delivery(self):
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

    def test_real_sessionstart_delivery_and_repeat(self):
        # Exercise the actual adapter with unrelated startup segments neutralized.
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "adapters/claude"))
        spec = importlib.util.spec_from_file_location("fixed_sessionstart", root / "adapters/claude/sessionstart_hook.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.write_report()
        from epitype import memspec
        config = {memspec.CONFIG_BUDGET_BYTES_FIELD: 20000, memspec.DREAM_CONFIG_FIELD: {memspec.DREAM_MODE_FIELD: memspec.DREAM_MODE_OFF}}
        vault = self.root / "vault"
        vault.mkdir()
        with patch.object(Path, "home", return_value=self.root), \
             patch.object(module, "load_config", return_value=config), \
             patch.object(module, "resolve_vaults", return_value=[]), \
             patch.object(module, "native_cwd_vaults", return_value=[]), \
             patch.object(module, "governance_vault", return_value=vault), \
             patch.object(module, "_startup_segments"), \
             patch.object(module, "_dream_spawn"), \
             patch.object(module, "_dream_notice", return_value=None), \
             patch.object(module, "_soft_remaining", return_value=0), \
             patch.object(module, "expired", return_value=False):
            import time
            event = {"cwd": str(self.cwd), "source": "startup"}
            with patch.object(module, "bounded_context", return_value=""):
                self.assertIsNone(module._handle(event, started_at=time.monotonic()))
            self.assertFalse(self.receipt.exists())
            first = module._handle(event, started_at=time.monotonic())
            context = first["hookSpecificOutput"]["additionalContext"]
            self.assertIn("RUNTIME_UNMEASURED", context)
            self.assertTrue(self.receipt.exists())
            second = module._handle(event, started_at=time.monotonic())
            self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
