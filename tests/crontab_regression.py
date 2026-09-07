"""Crontab failures preserve both existing jobs and the installation."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install import graft


class Platform:
    name = "posix"

    def __getattr__(self, name):
        return getattr(os, name)


class CrontabTests(unittest.TestCase):
    def test_windows_missing_task_is_idempotent_but_other_errors_fail(self):
        for code, expected in ((0x80070002, True), (-2147024894, True), (5, False)):
            def runner(argv, stdin=None):
                return subprocess.CompletedProcess(argv, code if "/HRESULT" in argv else 1, "", "localized error")
            windows = Platform()
            windows.name = "nt"
            with patch.object(graft, "os", windows):
                self.assertEqual(graft._unregister_nightly(False, io.StringIO(), runner), expected)

    def test_read_failures_never_submit_replacement(self):
        for detail, body, code in (("Permission denied", "", 1), ("", "", 1),
                                   ("unknown locale", "", 1), ("no crontab for test: error", "", 1),
                                   ("no crontab for test", "", 2),
                                   ("no crontab for test", "partial job\n", 1)):
            calls = []
            def runner(argv, stdin=None):
                calls.append((argv, stdin))
                return subprocess.CompletedProcess(argv, code, body, detail)
            with self.subTest(detail=detail, body=body, code=code), patch.object(graft, "os", Platform()):
                self.assertFalse(graft._register_nightly(graft.REPO_ROOT, "03:30", False, io.StringIO(), runner))
                self.assertFalse(graft._unregister_nightly(False, io.StringIO(), runner))
                self.assertEqual(calls, [(["crontab", "-l"], None)] * 2)

    def test_absent_and_existing_tables_preserve_unrelated_jobs(self):
        for detail in ("no crontab for test", "crontab: no crontab for test"):
            runner = lambda argv, stdin: subprocess.CompletedProcess(argv, 1, "", detail)
            self.assertEqual(graft._crontab_without_dream(runner), [])
        other = ['MAILTO=test@example.invalid', '0 5 * * * backup',
                 '# owner note # epitype-dream', '0 6 * * * echo "# epitype-dream"']
        body = "\n".join([*other, f"30 3 * * * old {graft.DREAM_CRON_MARKER}"]) + "\n"
        runner = lambda argv, stdin: subprocess.CompletedProcess(argv, 0, body, "")
        self.assertEqual(graft._crontab_without_dream(runner), other)

    def test_uninstall_read_error_preserves_config_and_host_registration(self):
        with tempfile.TemporaryDirectory(prefix="cron-uninstall-") as temporary:
            home = Path(temporary).resolve()
            (home / ".claude").mkdir()
            settings = home / ".claude/settings.json"
            settings.write_text('{"hooks":{}}', encoding="utf-8")
            graft._install(home, repo_root=graft.REPO_ROOT, output=io.StringIO())
            config = home / ".epitype/config.json"
            import json
            value = json.loads(config.read_text(encoding="utf-8"))
            value["dream"]["mode"] = "nightly"
            config.write_text(json.dumps(value), encoding="utf-8")
            original, host = config.read_bytes(), settings.read_bytes()
            runner = lambda argv, stdin: subprocess.CompletedProcess(argv, 1, "", "Permission denied")
            with patch.object(graft, "os", Platform()), self.assertRaises(graft.InstallError):
                graft._uninstall(home, output=io.StringIO(), scheduler=runner)
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(settings.read_bytes(), host)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
