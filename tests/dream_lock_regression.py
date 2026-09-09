import sys; sys.dont_write_bytecode = True
"""Dream lease exclusion and ownership on real processes, synthetic vaults only."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import dream, memspec


def contender(vault, ready, go, finish, results):
    ready.put(os.getpid())
    go.wait(10)
    token = dream.acquire_lock(vault)
    results.put(bool(token))
    finish.wait(10)
    if token:
        dream.release_lock(vault, token)


def guard_holder(vault, ready):
    with dream._lock_guard(vault) as path:
        ready.put(path is not None)
        time.sleep(30)


def lease_holder(vault, ready):
    ready.put(bool(dream.acquire_lock(vault)))
    time.sleep(30)


def scheduled_child(arguments, go, results):
    go.wait(10)
    output = io.StringIO()
    code = dream.main(arguments, output=output)
    results.put((code, output.getvalue()))


class DreamLockRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-dream-lock-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.path = dream.dream_root(self.vault) / memspec.DREAM_LOCK_FILENAME
        self.path.parent.mkdir()
        self.ctx = multiprocessing.get_context("spawn")
        # 第 8 節從家目錄推口袋庫；鎖的行為與它無關，但這份回歸不該順手掃跑測試的人
        # 的真實家目錄（速度與可重現性都會跟著機器跑）。
        home = self.root / "home"
        (home / memspec.HOST_STATE_DIRECTORY / memspec.HOST_PROJECTS_DIRECTORY).mkdir(parents=True)
        for name in ("HOME", "USERPROFILE"):
            patched = patch.dict(os.environ, {name: str(home)})
            patched.start()
            self.addCleanup(patched.stop)

    def stale(self, pid=0):
        self.path.write_text(json.dumps({"pid": pid, "started":
            time.time() - memspec.DREAM_LOCK_STALE_SECONDS - 60}), encoding="utf-8")

    def stop_process(self, process):
        if process.is_alive():
            process.terminate()
        process.join(5)

    def test_two_stale_readers_never_become_two_owners(self):
        self.stale()
        barrier = threading.Barrier(2)
        original = dream._lock_is_stale
        def simultaneous_read(path, now):
            stale = original(path, now)
            try:
                barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass  # An exclusive guard permits only one reader into this region.
            return stale
        with patch.object(dream, "_lock_is_stale", simultaneous_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                tokens = list(pool.map(lambda _: dream.acquire_lock(self.vault), range(2)))
        self.assertEqual(sum(map(bool, tokens)), 1)
        self.assertTrue(dream.release_lock(self.vault, next(filter(None, tokens))))

    def test_cross_process_contenders_have_one_owner(self):
        self.stale()
        ready, results = self.ctx.Queue(), self.ctx.Queue()
        go, finish = self.ctx.Event(), self.ctx.Event()
        processes = [self.ctx.Process(target=contender,
            args=(self.vault, ready, go, finish, results)) for _ in range(3)]
        for process in processes:
            process.start()
            self.addCleanup(self.stop_process, process)
        for _ in processes:
            ready.get(timeout=10)
        go.set()
        try:
            self.assertEqual(sum(results.get(timeout=10) for _ in processes), 1)
            self.assertFalse(dream.acquire_lock(self.vault))
        finally:
            finish.set()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertFalse(self.path.exists())

    def test_live_owner_survives_expiry_and_old_token_cannot_release_successor(self):
        self.stale(os.getpid())
        self.assertFalse(dream.acquire_lock(self.vault))
        self.stale()
        first = dream.acquire_lock(self.vault)
        self.assertTrue(dream.release_lock(self.vault, first))
        second = dream.acquire_lock(self.vault)
        self.assertFalse(dream.release_lock(self.vault, first))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["token"], second)
        self.assertTrue(dream.release_lock(self.vault, second))

    def test_killed_guard_holder_does_not_leave_kernel_lock(self):
        ready = self.ctx.Queue()
        process = self.ctx.Process(target=guard_holder, args=(self.vault, ready))
        process.start()
        self.addCleanup(self.stop_process, process)
        self.assertTrue(ready.get(timeout=10))
        started = time.monotonic()
        self.assertFalse(dream.acquire_lock(self.vault))
        self.assertLess(time.monotonic() - started, 1)
        self.stop_process(process)
        token = dream.acquire_lock(self.vault)
        self.assertTrue(token)
        self.assertTrue(dream.release_lock(self.vault, token))

    def test_handoff_is_single_use_and_late_parent_cannot_rewrite_child(self):
        token = dream.acquire_lock(self.vault, handoff=True)
        self.assertFalse(dream._handoff_lock(self.vault, "wrong-token"))
        self.assertTrue(dream._handoff_lock(self.vault, token))
        self.assertFalse(dream._handoff_lock(self.vault, token))
        self.assertFalse(dream._handoff_lock(self.vault, token, child_pid=4242))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["pid"], os.getpid())
        self.assertTrue(dream.release_lock(self.vault, token))

    def test_unreadable_lease_is_not_a_dead_owner(self):
        self.stale()
        before = self.path.read_bytes()
        with patch.object(dream, "_read_lock", side_effect=PermissionError("synthetic denied")):
            self.assertFalse(dream.acquire_lock(self.vault))
            self.assertFalse(dream.release_lock(self.vault, "unknown"))
            self.assertFalse(dream._handoff_lock(self.vault, "unknown"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_live_then_killed_lease_holder_recovers_only_after_expiry(self):
        ready = self.ctx.Queue()
        process = self.ctx.Process(target=lease_holder, args=(self.vault, ready))
        process.start()
        self.addCleanup(self.stop_process, process)
        self.assertTrue(ready.get(timeout=10))
        later = time.time() + memspec.DREAM_LOCK_STALE_SECONDS + 1
        self.assertFalse(dream.acquire_lock(self.vault, now=later))
        self.stop_process(process)
        self.assertFalse(dream.acquire_lock(self.vault))
        token = dream.acquire_lock(self.vault, now=later)
        self.assertTrue(token)
        self.assertTrue(dream.release_lock(self.vault, token))

    def test_real_spawn_parent_records_pid_before_child_claims(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"vaults": [str(self.vault)]}), encoding="utf-8")
        go, results = self.ctx.Event(), self.ctx.Queue()
        children = []
        def launcher(argv, log_path):
            child = self.ctx.Process(target=scheduled_child, args=(argv[2:], go, results))
            child.start()
            self.addCleanup(self.stop_process, child)
            children.append(child)
            return child.pid
        with patch.dict(os.environ, {memspec.EPITYPE_CONFIG_ENV: str(config)}):
            self.assertTrue(dream.spawn(self.vault, launcher=launcher))
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(value["pid"], children[0].pid)
            self.assertTrue(value["handoff"])
            go.set()
            code, output = results.get(timeout=30)
        children[0].join(10)
        self.assertEqual(children[0].exitcode, 0)
        self.assertEqual(code, 0)
        self.assertIn("DREAM PACK", output)
        self.assertFalse(self.path.exists())

    def test_real_spawn_child_runs_before_parent_pid_note(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"vaults": [str(self.vault)]}), encoding="utf-8")
        captured = []
        def launcher(argv, log_path):
            child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8")
            out, err = child.communicate(timeout=30)
            captured.append((argv, child.returncode, out, err))
            return child.pid  # Child already finished and released before this note.
        with patch.dict(os.environ, {memspec.EPITYPE_CONFIG_ENV: str(config)}):
            self.assertTrue(dream.spawn(self.vault, launcher=launcher))
        self.assertEqual(captured[0][1], 0, captured[0][3])
        self.assertIn("DREAM PACK", captured[0][2])
        self.assertIn("--lock-token", captured[0][0])
        self.assertFalse(self.path.exists())
        state = self.path.parent / memspec.DREAM_STATE_FILENAME
        self.assertTrue(json.loads(state.read_text(encoding="utf-8"))["complete"])

    def test_unverified_lock_held_flag_cannot_run_or_release_another_lease(self):
        token = dream.acquire_lock(self.vault)
        before = self.path.read_bytes()
        config = self.root / "config.json"
        config.write_text(json.dumps({"vaults": [str(self.vault)]}), encoding="utf-8")
        with patch.dict(os.environ, {memspec.EPITYPE_CONFIG_ENV: str(config)}):
            output = io.StringIO()
            self.assertEqual(dream.main(["--scheduled", "--lock-held"], output=output), 0)
        self.assertNotIn("DREAM PACK", output.getvalue())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(dream.release_lock(self.vault, token))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
