"""Nightly relocation contract; all homes and schedulers are synthetic."""
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install import graft


class Platform:
    def __init__(self, name):
        self.name = name

    def __getattr__(self, name):
        return getattr(os, name)


class Scheduler:
    def __init__(self):
        self.calls, self.command = [], []
        self.at, self.cron, self.fail_change = "03:30", "", False
        self.other_setting = "preserve me"
        self.extra_trigger = self.extra_action = False

    def __call__(self, argv, stdin_text=None):
        self.calls.append((argv, stdin_text))
        body, code = "", 0
        if argv[:2] in (["schtasks", "/Create"], ["schtasks", "/Change"]):
            if self.fail_change and argv[1] == "/Change":
                code = 1
            else:
                self.command = [part.strip('"') for part in shlex.split(argv[argv.index("/TR") + 1], posix=False)]
                if "/ST" in argv:
                    self.at = argv[argv.index("/ST") + 1]
        elif argv[:2] == ["schtasks", "/Query"]:
            task = ET.Element("Task", xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task")
            action = ET.SubElement(ET.SubElement(task, "Actions"), "Exec")
            ET.SubElement(action, "Command").text = self.command[0]
            ET.SubElement(action, "Arguments").text = graft._dream_command_text(self.command[1:])
            trigger = ET.SubElement(ET.SubElement(task, "Triggers"), "CalendarTrigger")
            ET.SubElement(trigger, "StartBoundary").text = f"2026-01-01T{self.at}:00"
            ET.SubElement(ET.SubElement(trigger, "ScheduleByDay"), "DaysInterval").text = "1"
            if self.extra_trigger:
                ET.SubElement(task.find("Triggers"), "EventTrigger")
            if self.extra_action:
                ET.SubElement(task.find("Actions"), "ComHandler")
            body = ET.tostring(task, encoding="unicode")
        elif argv == ["crontab", "-l"]:
            body = self.cron
        elif argv == ["crontab", "-"]:
            self.cron = stdin_text
        return subprocess.CompletedProcess(argv, code, body, "synthetic failure" if code else "")


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="schedule-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.home, self.old, self.new = root / "home", root / "old repo", root / "new repo"
        for repo in (self.old, self.new):
            shutil.copytree(graft.REPO_ROOT, repo, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude/settings.json").write_text("{}", encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch.object(graft, "os", Platform("nt")).start()
        patch.object(graft, "_synthetic_health", return_value=True).start()
        self.scheduler = Scheduler()
        graft._install(self.home, repo_root=self.old, dream_mode="nightly", scheduler=self.scheduler, output=io.StringIO())
        self.config = self.home / ".epitype/config.json"
        self.original = self.config.read_bytes()
        self.old_command = list(self.scheduler.command)

    def relocate(self):
        return graft._relocate(self.home, self.new, scheduler=self.scheduler, output=io.StringIO())

    def test_relocate_updates_actual_target_preserves_hosts_and_settings(self):
        settings = (self.home / ".claude/settings.json").read_bytes()
        shims = {path.name: path.read_bytes() for path in (self.home / ".epitype/hooks").glob("*.py")}
        self.assertEqual(self.relocate(), 0)
        self.assertEqual(Path(self.scheduler.command[1]), self.new / "epitype/dream.py")
        self.old.rename(self.old.with_name("old-moved"))
        self.assertEqual(graft._doctor(self.home, output=io.StringIO(), scheduler=self.scheduler), 0)
        self.assertEqual(settings, (self.home / ".claude/settings.json").read_bytes())
        self.assertEqual(shims, {path.name: path.read_bytes() for path in (self.home / ".epitype/hooks").glob("*.py")})
        self.assertEqual(self.scheduler.other_setting, "preserve me")

    def test_scheduler_failure_restores_config(self):
        self.scheduler.fail_change = True
        with self.assertRaises(graft.InstallError):
            self.relocate()
        self.assertEqual(self.original, self.config.read_bytes())
        self.assertEqual(self.scheduler.command, self.old_command)

    def test_relocation_after_old_directory_already_moved(self):
        self.old.rename(self.old.with_name("old-moved"))
        self.assertEqual(self.relocate(), 0)
        self.assertEqual(Path(self.scheduler.command[1]), self.new / "epitype/dream.py")

    def test_doctor_failure_restores_both(self):
        with patch.object(graft, "_doctor", return_value=1), self.assertRaises(graft.InstallError):
            self.relocate()
        self.assertEqual(self.original, self.config.read_bytes())
        self.assertEqual(self.scheduler.command, self.old_command)

    def test_update_that_writes_then_reports_failure_is_rolled_back(self):
        def runner(argv, stdin=None):
            result = self.scheduler(argv, stdin)
            if argv[:2] == ["schtasks", "/Change"] and str(self.new) in argv[-1]:
                return subprocess.CompletedProcess(argv, 1, "", "late failure")
            return result
        with self.assertRaises(graft.InstallError):
            graft._relocate(self.home, self.new, scheduler=runner, output=io.StringIO())
        self.assertEqual(self.original, self.config.read_bytes())
        self.assertEqual(self.scheduler.command, self.old_command)

    def test_rollback_preserves_later_owner_work(self):
        def changed_doctor(*args, **kwargs):
            self.config.write_bytes(b'{"owner":"later"}')
            self.scheduler.command = ["owner", "later", "job"]
            return 1
        with patch.object(graft, "_doctor", side_effect=changed_doctor), self.assertRaises(graft.InstallError):
            self.relocate()
        self.assertEqual(self.config.read_bytes(), b'{"owner":"later"}')
        self.assertEqual(self.scheduler.command, ["owner", "later", "job"])

    def test_doctor_rejects_wrong_time_missing_target_and_wrong_arguments(self):
        for command, at in ((self.old_command, "12:34"),
                            ([self.old_command[0], "missing.py", "--scheduled"], "03:30"),
                            (self.old_command + ["--unexpected"], "03:30")):
            self.scheduler.command, self.scheduler.at = command, at
            self.assertEqual(graft._doctor(self.home, output=io.StringIO(), scheduler=self.scheduler), 1)

    def test_doctor_rejects_additional_trigger_or_action(self):
        self.scheduler.extra_trigger = True
        self.assertEqual(graft._doctor(self.home, output=io.StringIO(), scheduler=self.scheduler), 1)
        self.scheduler.extra_trigger, self.scheduler.extra_action = False, True
        self.assertEqual(graft._doctor(self.home, output=io.StringIO(), scheduler=self.scheduler), 1)

    def test_dry_run_changes_neither_config_nor_schedule(self):
        self.scheduler.calls.clear()
        self.assertEqual(graft._relocate(self.home, self.new, dry_run=True, scheduler=self.scheduler, output=io.StringIO()), 0)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.scheduler.calls, [])


class PosixRelocationTests(unittest.TestCase):
    def test_update_and_rollback_preserve_other_jobs_and_reject_conflict(self):
        scheduler = Scheduler()
        marker = graft.DREAM_CRON_MARKER
        scheduler.cron = f"0 1 * * * backup\n30 3 * * * /python /old/dream.py --scheduled {marker}\n"
        with patch.object(graft, "os", Platform("posix")):
            before = graft._read_nightly(scheduler)
            scheduler.cron += "0 2 * * * later-job\n"
            command = ["/python", "/new/dream.py", "--scheduled"]
            graft._replace_nightly_command(before, command, scheduler)
            updated = graft._read_nightly(scheduler)
            scheduler.cron += "0 4 * * * latest-job\n"
            graft._replace_nightly_command(updated, before["command"], scheduler)
            self.assertIn("/old/dream.py", scheduler.cron)
            for name in ("backup", "later-job", "latest-job"):
                self.assertIn(name, scheduler.cron)
            unchanged = scheduler.cron
            with self.assertRaises(graft.InstallError):
                graft._replace_nightly_command(updated, command, scheduler)
            self.assertEqual(scheduler.cron, unchanged)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
