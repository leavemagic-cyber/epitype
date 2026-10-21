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
        self.environment = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
        self.environment["EPITYPE_SHIM_TRACE"] = str(self.root / "trace.jsonl")

    def invoke(self, source, arguments=(), payload=""):
        self.adapter.write_text(source, encoding="utf-8")
        with subprocess.Popen(
            [sys.executable, str(self.shim), *arguments], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", env=self.environment,
        ) as child:
            stdout, stderr = child.communicate(payload, timeout=10)
            return child.pid, child.returncode, stdout, stderr

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

    def test_rendered_shims_match_installer_pins(self):
        payloads = graft._shim_payloads(REPO)
        self.assertEqual(set(payloads), set(graft.SHIM_SHA256))
        self.assertEqual({name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}, graft.SHIM_SHA256)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
