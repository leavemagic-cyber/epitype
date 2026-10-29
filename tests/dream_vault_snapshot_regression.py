"""Nightly restore points use real git repositories in temporary vaults only."""
import sys
sys.dont_write_bytecode = True
from datetime import date
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import dream


class VaultSnapshot(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.today = date(2026, 10, 28)
        self.git("init", "-q")
        for key, value in (("user.name", "Snapshot Test"), ("user.email", "snapshot-fixture@localhost"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            self.git("config", key, value)
        (self.vault / ".gitignore").write_text(
            ".git_snapshot.lock\n_GIT_PENDING\n.epitype/\n", encoding="utf-8")
        self.card = self.vault / "card.md"
        self.card.write_text("original\n", encoding="utf-8")
        (self.vault / "removed.md").write_text("remove me\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")
        self.lock = self.vault / ".git_snapshot.lock"
        self.pending = self.vault / "_GIT_PENDING"

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.vault), *args], check=True, capture_output=True,
            text=True, encoding="utf-8", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()

    def snapshot(self):
        return dream._snapshot_vault(self.vault, self.today)

    def stale(self, path):
        path.write_text("stale", encoding="utf-8")
        old = time.time() - dream.VAULT_SNAPSHOT_STALE_SECONDS - 10
        os.utime(path, (old, old))

    def test_dirty_commits_counts_and_removes_pending_and_own_lock(self):
        self.card.write_text("updated\n", encoding="utf-8")
        (self.vault / "added.md").write_text("new\n", encoding="utf-8")
        (self.vault / "removed.md").unlink()
        self.pending.write_text("retry", encoding="utf-8")
        result = self.snapshot()
        self.assertEqual(result["vault"], str(self.vault))
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["detail"], "dream 2026-10-28 | cards +1 ~1 -1 | by:dream")
        self.assertEqual(self.git("log", "-1", "--format=%s"), result["detail"])
        self.assertEqual(self.git("show", "HEAD:card.md"), "updated")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.lock.exists())

    def test_clean_does_not_commit_and_clears_pending(self):
        before = self.git("rev-parse", "HEAD")
        self.pending.write_text("retry", encoding="utf-8")
        self.assertEqual(self.snapshot()["status"], "clean")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.lock.exists())

    def test_fresh_snapshot_lock_is_busy_and_left_untouched(self):
        self.lock.write_text("other owner", encoding="utf-8")
        before = self.lock.stat().st_mtime_ns
        self.card.write_text("updated\n", encoding="utf-8")
        self.pending.write_text("retry", encoding="utf-8")
        self.assertEqual(self.snapshot()["status"], "busy")
        self.assertEqual(self.lock.read_text(encoding="utf-8"), "other owner")
        self.assertEqual(self.lock.stat().st_mtime_ns, before)
        self.assertTrue(self.pending.exists())
        self.assertEqual(self.git("show", "HEAD:card.md"), "original")

    def test_stale_snapshot_and_index_locks_are_recovered(self):
        self.stale(self.lock)
        index_lock = self.vault / ".git" / "index.lock"
        self.stale(index_lock)
        self.card.write_text("updated\n", encoding="utf-8")
        self.assertEqual(self.snapshot()["status"], "committed")
        self.assertFalse(index_lock.exists())
        self.assertFalse(self.lock.exists())

    def test_fresh_index_lock_fails_without_deleting_it_or_pending(self):
        index_lock = self.vault / ".git" / "index.lock"
        index_lock.write_text("active", encoding="utf-8")
        self.pending.write_text("retry", encoding="utf-8")
        self.card.write_text("updated\n", encoding="utf-8")
        result = self.snapshot()
        self.assertEqual(result["status"], "error")
        self.assertIn("index.lock", result["detail"])
        self.assertEqual(index_lock.read_text(encoding="utf-8"), "active")
        self.assertTrue(self.pending.exists())
        self.assertFalse(self.lock.exists())

    def test_plain_vault_is_not_git(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual(dream._snapshot_vault(plain, self.today)["status"], "not-git")
        self.assertEqual(list(plain.iterdir()), [])

    def test_git_exception_releases_lock(self):
        with patch.object(dream.subprocess, "run", side_effect=FileNotFoundError("synthetic git missing")):
            result = self.snapshot()
        self.assertEqual(result["status"], "error")
        self.assertIn("synthetic git missing", result["detail"])
        self.assertFalse(self.lock.exists())

    def test_commit_nothing_to_commit_clears_pending(self):
        transient = self.vault / "transient.md"
        transient.write_text("transient\n", encoding="utf-8")
        self.pending.write_text("retry", encoding="utf-8")
        original = subprocess.run

        def run(command, **kwargs):
            if command[3] == "add":
                transient.unlink()
            return original(command, **kwargs)

        with patch.object(dream.subprocess, "run", side_effect=run):
            self.assertEqual(self.snapshot()["status"], "clean")
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.lock.exists())

    def test_cleanup_preserves_a_replacement_lock(self):
        replacement = self.vault / "replacement.lock"
        replacement.write_text("new owner", encoding="utf-8")
        original = subprocess.run

        def run(command, **kwargs):
            os.replace(replacement, self.lock)
            return original(command, **kwargs)

        with patch.object(dream.subprocess, "run", side_effect=run):
            self.assertEqual(self.snapshot()["status"], "clean")
        self.assertEqual(self.lock.read_text(encoding="utf-8"), "new owner")

    def test_lock_stays_fresh_during_a_long_git_call(self):
        original = subprocess.run

        def run(command, **kwargs):
            old = time.time() - 2
            os.utime(self.lock, (old, old))
            until = time.monotonic() + 3
            while self.lock.stat().st_mtime <= old and time.monotonic() < until:
                time.sleep(0.01)
            self.assertGreater(self.lock.stat().st_mtime, old)
            self.assertEqual(self.snapshot()["status"], "busy")
            return original(command, **kwargs)

        with patch.object(dream, "VAULT_SNAPSHOT_STALE_SECONDS", 1), \
             patch.object(dream.subprocess, "run", side_effect=run):
            self.assertEqual(self.snapshot()["status"], "clean")
        self.assertFalse(self.lock.exists())

    def test_git_runs_hidden_without_shell_and_with_generous_timeout(self):
        self.card.write_text("updated\n", encoding="utf-8")
        original = subprocess.run
        with patch.object(dream.subprocess, "run", wraps=original) as run:
            self.assertEqual(self.snapshot()["status"], "committed")
        self.assertEqual([call.args[0][3] for call in run.call_args_list], ["status", "add", "commit"])
        for call in run.call_args_list:
            self.assertFalse(call.kwargs.get("shell", False))
            self.assertEqual(call.kwargs["timeout"], 300)
            self.assertEqual(call.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def cli(self, *arguments, vaults=None, views=None):
        pack = self.root / "pack.json"
        report = {"vaults": list(map(str, vaults or [self.vault])), "today": "2026-10-28",
                  "since": "2026-07-30", "sections": [], "next_steps": []}
        with patch.object(dream, "_section_host_sync", return_value={}), \
             patch.object(dream, "_views_module") as view_module, \
             patch.object(dream, "shape_index", return_value={}), \
             patch.object(dream, "build_report", return_value=report), \
             patch.object(dream, "_write_run_state"):
            view_module.return_value.generate.side_effect = views
            code = dream.main(["--json", "--out", str(pack), "--today", "2026-10-28",
                               *arguments, *map(str, vaults or [self.vault])], output=io.StringIO())
        self.assertEqual(code, 0)
        return report, pack

    def test_cli_snapshots_before_rewrites_and_persists_each_result(self):
        self.card.write_text("before views\n", encoding="utf-8")
        plain = self.root / "plain"
        plain.mkdir()

        def views(vault):
            if vault == self.vault:
                self.assertEqual(self.git("show", "HEAD:card.md"), "before views")
                self.card.write_text("after views\n", encoding="utf-8")

        report, pack = self.cli(vaults=[self.vault, plain], views=views)
        self.assertEqual([row["status"] for row in report["vault_snapshots"]], ["committed", "not-git"])
        self.assertEqual(json.loads(pack.read_text(encoding="utf-8"))["vault_snapshots"],
                         report["vault_snapshots"])
        self.assertEqual(self.git("show", "HEAD:card.md"), "before views")
        self.assertEqual(self.card.read_text(encoding="utf-8"), "after views\n")
        self.assertIn(dream.VAULT_SNAPSHOT_HEADING, dream._render_markdown(report))

    def test_cli_failure_is_visible_and_other_vault_still_snapshots(self):
        failed = self.root / "failed"
        failed.mkdir()
        original = dream.subprocess.run

        def run(command, **kwargs):
            if command[2] == str(failed):
                raise OSError("synthetic failure")
            return original(command, **kwargs)

        (failed / ".git").mkdir()
        self.card.write_text("updated\n", encoding="utf-8")
        with patch.object(dream.subprocess, "run", side_effect=run):
            report, _pack = self.cli(vaults=[failed, self.vault])
        self.assertEqual([row["status"] for row in report["vault_snapshots"]], ["error", "committed"])
        markdown = dream._render_markdown(report)
        self.assertIn("synthetic failure", markdown)
        self.assertIn(dream.VAULT_SNAPSHOT_LABELS["error"], markdown)
        self.assertFalse((failed / ".git_snapshot.lock").exists())

    def test_dry_run_never_snapshots(self):
        with patch.object(dream, "_snapshot_vault", side_effect=AssertionError("snapshot in dry run")):
            report, pack = self.cli("--dry-run")
        self.assertEqual(report["vault_snapshots"], [])
        self.assertFalse(pack.exists())


if __name__ == "__main__":
    unittest.main(argv=[argument for argument in sys.argv if argument != "--selftest"])
