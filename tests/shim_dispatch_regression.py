"""Stable shims must preserve gate results without a second interpreter."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from install import graft


class ShimDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="epitype-shim-dispatch-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.adapter = self.root / "repo" / "adapters" / "claude" / "pretooluse_gate.py"
        self.adapter.parent.mkdir(parents=True)
        config = self.home / ".epitype" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"repo_root": str(self.root / "repo")}), encoding="utf-8")
        self.shim = self.home / ".epitype" / "hooks" / "pretooluse.py"
        self.shim.parent.mkdir()
        self.shim.write_bytes(graft._shim_payloads(REPO)["pretooluse.py"])
        self.entry_pid = self.root / "entry-pid.txt"
        self.entry = self.root / "entry.py"
        self.entry.write_text(
            "import os, pathlib, runpy, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "sys.argv = sys.argv[2:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n", encoding="utf-8")
        self.environment = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
        self.environment["EPITYPE_SHIM_TRACE"] = str(self.root / "trace.jsonl")

    def invoke(self, source, arguments=(), payload=""):
        self.adapter.write_text(source, encoding="utf-8")
        with subprocess.Popen(
            [sys.executable, str(self.entry), str(self.entry_pid), str(self.shim), *arguments], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", env=self.environment,
        ) as child:
            stdout, stderr = child.communicate(payload, timeout=10)
            # Windows venv launchers have a different outer PID from Python.
            return int(self.entry_pid.read_text()), child.returncode, stdout, stderr

    def test_same_pid_preserves_imports_arguments_and_input(self):
        (self.adapter.parent / "shim_fixture_helper.py").write_text("VALUE = 'sibling'\n", encoding="utf-8")
        pid, code, stdout, stderr = self.invoke(
            "import json, os, sys\nfrom shim_fixture_helper import VALUE\n"
            "print(json.dumps([os.getpid(), sys.argv, sys.stdin.read(), VALUE]))\n",
            ("--fixture", "value"), "fixture input",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), [pid, [str(self.adapter), "--fixture", "value"], "fixture input", "sibling"])
        self.assertEqual(stderr, "")

    def test_denial_code_and_output_are_preserved(self):
        _, code, stdout, stderr = self.invoke("import sys\nprint('deny')\nprint('reason', file=sys.stderr)\nraise SystemExit(2)\n")
        self.assertEqual((code, stdout.strip(), stderr.strip()), (2, "deny", "reason"))
        trace = json.loads((self.root / "trace.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(trace["exit"], 2)

    def test_adapter_crash_does_not_fail_open(self):
        _, code, stdout, stderr = self.invoke("raise RuntimeError('fixture crash')\n")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("RuntimeError: fixture crash", stderr)
        self.assertFalse((self.home / ".epitype" / "shim_status.json").exists())

    def test_string_exit_is_reported_as_failure(self):
        _, code, stdout, stderr = self.invoke("raise SystemExit('fixture refusal')\n")
        self.assertEqual((code, stdout, stderr.strip()), (1, "", "fixture refusal"))

    def test_missing_adapter_keeps_existing_fail_open_status(self):
        import runpy
        with patch.dict(os.environ, self.environment), patch("pathlib.Path.home", return_value=self.home):
            module = runpy.run_path(str(self.shim), run_name="shim_fixture")
            self.assertEqual(module["main"](), 0)
        status = json.loads((self.home / ".epitype" / "shim_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["shims"]["pretooluse.py"]["reason"], "adapter_missing")

    def post_write(self, target, tool="Edit"):
        """寫檔後事件；假閘門被叫到就把收到的輸入原樣印出來。"""
        vault = self.root / "vault"
        (vault / "nested").mkdir(parents=True, exist_ok=True)
        config = self.home / ".epitype" / "config.json"
        config.write_text(json.dumps({"repo_root": str(self.root / "repo"), "vaults": [str(vault)]}), encoding="utf-8")
        self.environment.pop("EPITYPE_CONFIG", None)
        payload = json.dumps({"hook_event_name": "PostToolUse", "session_id": "s", "cwd": str(self.root),
                              "tool_name": tool, "tool_input": {"file_path": str(target)}})
        _, code, stdout, stderr = self.invoke("import sys\nprint(sys.stdin.read(), end='')\n", payload=payload)
        self.assertEqual((code, stderr), (0, ""))
        return payload, stdout

    def test_post_write_off_vault_exits_before_the_gate_loads(self):
        vault = self.root / "vault"
        for target in (self.root / "src" / "app.py", self.root / "design" / "STATE.md",
                       vault / "_ledger" / "row.md", vault / ".git" / "note.md", vault / "MEMORY.md",
                       vault / "data.json"):
            with self.subTest(target=target.name):
                _payload, stdout = self.post_write(target)
                self.assertEqual(stdout, "")
                self.assertFalse((self.root / "trace.jsonl").exists())

    def test_post_write_on_a_card_reaches_the_gate_with_input_intact(self):
        vault = self.root / "vault"
        for target, tool in ((vault / "card.md", "Edit"), (vault / "nested" / "card.MD", "Write"),
                             (self.home / ".claude" / "projects" / "C--work" / "memory" / "card.md", "MultiEdit")):
            with self.subTest(target=target.name, tool=tool):
                payload, stdout = self.post_write(target, tool)
                self.assertEqual(stdout, payload)

    def test_other_events_reach_the_gate_with_input_intact(self):
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                              "tool_input": {"file_path": str(self.root / "app.py")}, "note": "\u4e2d\u6587"},
                             ensure_ascii=False)
        _, code, stdout, stderr = self.invoke("import sys\nprint(sys.stdin.read(), end='')\n", payload=payload)
        self.assertEqual((code, stdout, stderr), (0, payload, ""))

    def test_fast_exit_copies_match_their_sources(self):
        import runpy
        from epitype import capture_route, memspec
        with patch.dict(os.environ, self.environment):
            module = runpy.run_path(str(self.shim), run_name="shim_fixture")
        self.assertEqual(module["FAST_WRITE_TOOLS"], memspec.WRITE_GATE_TOOL_NAMES)
        self.assertEqual(module["FAST_PATH_FIELDS"], memspec.WRITE_GATE_PATH_FIELDS)
        self.assertEqual(module["FAST_CONFIG_ENV"], memspec.EPITYPE_CONFIG_ENV)
        self.assertEqual(module["FAST_CONFIG_VAULTS"], memspec.CONFIG_VAULTS_FIELD)
        self.assertEqual(module["FAST_NATIVE_PROJECTS"], capture_route.NATIVE_PROJECTS_SUBPATH)
        self.assertEqual(module["FAST_NATIVE_MEMORY"], capture_route.NATIVE_MEMORY_DIRNAME)
        self.assertEqual(module["FAST_INDEX_NAME"], memspec.MEMORY_INDEX_FILENAME)
        self.assertEqual(module["FAST_EXIT_ADAPTER"], graft.HOOK_SPECS["PostToolUse"][1])

    def test_rendered_shims_match_installer_pins(self):
        payloads = graft._shim_payloads(REPO)
        self.assertEqual(set(payloads), set(graft.SHIM_SHA256))
        self.assertEqual({name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}, graft.SHIM_SHA256)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
