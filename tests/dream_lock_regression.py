import sys; sys.dont_write_bytecode = True
"""Dream lease exclusion and ownership on real processes, synthetic vaults only."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import multiprocessing
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import dream, memspec

# 子行程何時就緒、跑完沒有上限可言：spawn 每次都是新直譯器，run_all 又關掉 bytecode
# （PYTHONDONTWRITEBYTECODE），epitype.dream 每次從原始碼重編（閒置約 1.2 秒），三個
# 一起起、套件又併行時實測 8–10 秒。以前用固定 10／30 秒當「就緒／跑完」的判準，滿載
# 就假紅。現在判準是「子行程還活著就等」；這個上限只防整份套件卡死，不是正確性條件。
HANG_GUARD_SECONDS = 600


def contender(vault, ready, go, finish, results):
    ready.put(os.getpid())
    go.wait()  # 父行程一定會 set（或測試結束時 terminate）；逾時先跑會讓「同時搶」悄悄變成依序搶。
    token = dream.acquire_lock(vault)
    results.put(bool(token))
    finish.wait()  # 擁有者要撐到父行程驗完「別人搶不到」；逾時先放鎖會讓那一題假紅。
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

    def reports(self, channel, processes, count):
        """從 channel 收 count 筆（每個子行程在這條 channel 至多送一筆）。還欠的筆數不超過
        活著的子行程數就一直等；超過了＝有子行程沒送就退出，判失敗。"""
        values = []
        deadline = time.monotonic() + HANG_GUARD_SECONDS
        while len(values) < count:
            try:
                values.append(channel.get(timeout=0.2))
                continue
            except queue.Empty:
                pass
            alive = sum(process.exitcode is None for process in processes)
            if count - len(values) > alive:
                # 退出前送出的那筆已經在管線裡（Queue 退出時會先送完）：再收一次，收不到才算死在半路。
                try:
                    values.append(channel.get(timeout=1))
                    continue
                except queue.Empty:
                    self.fail("child exited before reporting: exit codes "
                              f"{[process.exitcode for process in processes]}")
            if time.monotonic() >= deadline:
                self.fail(f"children alive but silent for {HANG_GUARD_SECONDS}s")
        return values

    def test_dream_preserves_the_callers_explicit_verification_mode(self):
        from epitype import rule_provenance
        seen = []
        with patch.object(dream, '_main', lambda argv, output: seen.append(rule_provenance._UNBOUNDED) or 0):
            self.assertEqual(dream.main(['--scheduled'], output=io.StringIO()), 0)
            self.assertEqual(seen, [False])
            with rule_provenance.unbounded_verification():
                self.assertEqual(dream.main(['--scheduled'], output=io.StringIO()), 0)
                self.assertTrue(rule_provenance._UNBOUNDED)
        self.assertEqual(seen, [False, True])
        self.assertFalse(rule_provenance._UNBOUNDED)

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
        self.reports(ready, processes, len(processes))
        go.set()
        try:
            self.assertEqual(sum(self.reports(results, processes, len(processes))), 1)
            self.assertFalse(dream.acquire_lock(self.vault))
        finally:
            finish.set()
        for process in processes:
            process.join(HANG_GUARD_SECONDS)
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
        self.assertEqual(self.reports(ready, [process], 1), [True])
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
        self.assertEqual(self.reports(ready, [process], 1), [True])
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
            (code, output), = self.reports(results, children, 1)
        children[0].join(HANG_GUARD_SECONDS)
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
            # 完整跑一場夢，滿載時沒有固定上限：逾時會被 spawn 當成起不來而放掉 lease，
            # 回 False，與「子行程先跑完」這個題目無關。只留防卡死的上限。
            out, err = child.communicate(timeout=HANG_GUARD_SECONDS)
            captured.append((argv, child.returncode, out, err))
            return child.pid  # Child already finished and released before this note.
        with patch.dict(os.environ, {memspec.EPITYPE_CONFIG_ENV: str(config)}):
            self.assertTrue(dream.spawn(self.vault, launcher=launcher))
        self.assertEqual(captured[0][1], 0, captured[0][3])
        self.assertIn("DREAM PACK", captured[0][2])
        self.assertIn("--lock-token", captured[0][0])
        self.assertFalse(self.path.exists())
        state = self.path.parent / memspec.DREAM_STATE_FILENAME
        recorded = json.loads(state.read_text(encoding="utf-8"))
        self.assertTrue(recorded["complete"], recorded)

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
