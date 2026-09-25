import sys; sys.dont_write_bytecode = True  # 載入時不 reconfigure stdout：輸出失敗之後才會載入這支（記 meter-released），reconfigure 會先 flush 卡住的輸出而丟例外，那一行就記不下來。
"""壓縮前後掛鉤的追蹤：每次一行 JSON，追查「壓縮後地圖與交接沒交回來」與用量提醒。

這裡的程式跑在掛鉤裡，所以：只追加一行、不讀任何大檔、任何錯誤都吞掉——追蹤壞了絕不能
改變掛鉤的結果。檔案超過上限就換名成 `.1`（只留一份舊的），過了到期日自己停（見 memspec
的 CONTEXT_METER_TRACE 段）。檔放在設定檔旁：設定檔不在的時候也要記得下「設定不在」。
"""

from collections import deque
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import time

try:
    from . import memspec
except ImportError:  # 直接當腳本跑。
    import memspec

_SESSION_MAX_CHARS = 128
_FIELD_MAX_CHARS = 200


def trace_path():
    return memspec.config_path().parent / memspec.CONTEXT_METER_TRACE_FILENAME


def until(options=None):
    """追蹤開到哪一天（UTC、當天含）。設定裡的日期壞了就當沒設：壞值不該讓追蹤永遠開著。"""
    options = memspec.config_options() if options is None else options
    section = options.get(memspec.CONTEXT_METER_CONFIG_FIELD) if isinstance(options, dict) else None
    value = section.get(memspec.CONTEXT_METER_TRACE_UNTIL_FIELD) if isinstance(section, dict) else None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    return date.fromisoformat(memspec.CONTEXT_METER_TRACE_UNTIL)


def enabled(now=None, options=None):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).date() <= until(options)


def host_of(event, codex=False):
    """哪個宿主。PreCompact 由 `--codex` 旗標決定；其餘看 transcript 檔名（Codex 是
    `rollout-*.jsonl`）——只為了分組，不讀檔尾。"""
    if codex:
        return memspec.CONTEXT_METER_HOST_CODEX
    transcript = event.get("transcript_path") if isinstance(event, dict) else None
    if isinstance(transcript, str) and os.path.basename(transcript.strip()).startswith("rollout-"):
        return memspec.CONTEXT_METER_HOST_CODEX
    return memspec.CONTEXT_METER_HOST_CLAUDE


def _clip(value):
    if isinstance(value, str):
        return value[:_FIELD_MAX_CHARS]
    if isinstance(value, (list, tuple)):
        return [_clip(item) for item in list(value)[:8]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_FIELD_MAX_CHARS]


def _room(target, incoming):
    """這一行寫得下嗎。寫進去會超過上限就先把現檔換名成 `.1`（蓋掉更舊的那份）；換名失敗、
    或這一行本身就比上限大，回 False——上限是硬的，寧可不記。"""
    cap = memspec.CONTEXT_METER_TRACE_MAX_BYTES
    if incoming > cap:
        return False
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if size + incoming <= cap:
        return True
    try:
        os.replace(target, target.with_name(target.name + ".1"))
    except OSError:
        return False
    return True


def record(event_name, event=None, started_at=None, codex=False, path=None, now=None, options=None,
           **fields):
    """追加一行；寫了回 True。永不丟例外、永不輸出。

    設定檔所在的目錄不在就不寫（不替沒裝 Epitype 的機器建目錄）。換名與追加在同一把短鎖
    裡：Windows 的 O_APPEND 是「先移到檔尾再寫」，並行的幾個 hook 會寫在同一個位移、互相
    蓋掉（2026-09-26 實測 6 個並行行程只留下 5 行）。等不到鎖就不寫這一行：少一行比寫出
    交錯、壞掉的 JSON 好。上限是硬的：換名失敗而檔已經放不下這一行，也不寫。

    鎖是旁邊一個不刪的 `.mutex` 檔上的作業系統鎖（Windows 位元組鎖、POSIX flock），不用
    memspec.file_lock：那把鎖每次建檔＋fsync＋刪檔，實測一次 16 ms，是這一行本身的上百倍。
    作業系統鎖在行程結束時自動放掉，沒有殭屍鎖要清。"""
    try:
        now = now or datetime.now(timezone.utc)
        if not enabled(now, options):
            return False
        session = event.get("session_id", event.get("sessionId")) if isinstance(event, dict) else None
        row = {
            "at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "host": host_of(event, codex),
            "session": session[:_SESSION_MAX_CHARS] if isinstance(session, str) else None,
            "event": event_name,
            "ms": round((time.monotonic() - started_at) * 1000) if started_at is not None else None,
        }
        row.update({key: _clip(value) for key, value in fields.items()})
        line = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        target = Path(path) if path is not None else trace_path()
        if not target.parent.is_dir():
            return False
        binary = getattr(os, "O_BINARY", 0)
        mutex = os.open(target.with_name(target.name + ".mutex"), os.O_RDWR | os.O_CREAT | binary, 0o600)
        try:
            if not _acquire(mutex, time.monotonic() + memspec.CONTEXT_METER_TRACE_LOCK_SECONDS):
                return False
            try:
                if not _room(target, len(line)):
                    return False
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | binary, 0o600)
                try:
                    os.write(descriptor, line)
                finally:
                    os.close(descriptor)
            finally:
                _release(mutex)
        finally:
            os.close(mutex)
        return True
    except Exception:
        return False


def _acquire(descriptor, deadline):
    """在 mutex 檔的第 0 個位元組上拿獨占鎖，等到 deadline 為止；拿不到回 False。"""
    try:
        if os.name == "nt":
            import msvcrt

            def attempt():
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            def attempt():
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                attempt()
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.005)
    except Exception:
        return False


def _release(descriptor):
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except Exception:
        pass


def new_run():
    """一次掛鉤呼叫的編號：開始行與結束行帶同一個，CLI 用它配對。不載入 uuid（每次呼叫都付）。"""
    return os.urandom(4).hex()


def paired(lines):
    """把 phase=start／exit 的兩行依 run 配成一列，其餘行原樣。回要印的字串，順序照檔案。

    有開始沒結束＝掛鉤被宿主砍掉、當掉，或還在跑；連開始都沒有＝宿主根本沒叫它（或砍在
    直譯器啟動、載入模組的那一段）。只配對傳進來的這幾行：結束行的開始行若在視窗外，標成
    只有結束。"""
    rows = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        rows.append((line, value if isinstance(value, dict) else {}))
    exits = {row.get("run") for _line, row in rows if row.get("phase") == "exit" and row.get("run")}
    started = set()
    shown = []
    for line, row in rows:
        phase, run = row.get("phase"), row.get("run")
        if phase == "start" and run:
            started.add(run)
            if run not in exits:
                shown.append("[START ONLY: killed, crashed or still running] " + line)
        elif phase == "exit" and run:
            shown.append(("[start+exit] " if run in started else "[exit only] ") + line)
        else:
            shown.append(line)
    return shown


def last(count, path=None):
    """最後 count 行，舊的在前（先換名的舊檔、再現檔）。兩個檔都有上限，所以整檔讀可以。"""
    target = Path(path) if path is not None else trace_path()
    lines = deque(maxlen=max(0, int(count)))
    for candidate in (target.with_name(target.name + ".1"), target):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines.extend(line for line in text.splitlines() if line.strip())
    return list(lines)


def _selftest():
    from datetime import timedelta
    import tempfile

    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-meter-trace-") as temp_dir:
            root = Path(temp_dir).resolve()
            target = root / memspec.CONTEXT_METER_TRACE_FILENAME
            now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
            event = {"session_id": "s" * 400, "transcript_path": "/x/.codex/sessions/rollout-1.jsonl"}

            wrote = record("PreCompact", event, time.monotonic(), codex=True, path=target, now=now,
                           options={}, outcome="ok", map=True, lines=["map"])
            row = json.loads(target.read_text(encoding="utf-8"))
            checks.append(("one JSON line with time, host, session, event, elapsed ms and the fields",
                           wrote and row["at"] == "2026-09-26T12:00:00.000Z" and row["host"] == "codex"
                           and len(row["session"]) == _SESSION_MAX_CHARS and row["event"] == "PreCompact"
                           and isinstance(row["ms"], int) and row["outcome"] == "ok"
                           and row["map"] is True and row["lines"] == ["map"]))
            claude = record("SessionStart", {"transcript_path": "/p/abc.jsonl"}, None, path=target,
                            now=now, options={})
            checks.append(("host from the transcript name; no started_at gives ms null",
                           claude and json.loads(last(1, target)[0])["host"] == "claude"
                           and json.loads(last(1, target)[0])["ms"] is None))

            # 到期：預設日期當天還寫、隔天不寫；設定可以延長，也可以提早；壞值＝沒設。
            default_until = date.fromisoformat(memspec.CONTEXT_METER_TRACE_UNTIL)
            last_day = datetime.combine(default_until, datetime.min.time(), timezone.utc).replace(hour=23)
            day_after = last_day + timedelta(hours=2)
            expired_target = root / "expired.jsonl"
            extended = {memspec.CONTEXT_METER_CONFIG_FIELD: {memspec.CONTEXT_METER_TRACE_UNTIL_FIELD: "2099-01-01"}}
            shortened = {memspec.CONTEXT_METER_CONFIG_FIELD: {memspec.CONTEXT_METER_TRACE_UNTIL_FIELD: "2026-01-01"}}
            garbage = {memspec.CONTEXT_METER_CONFIG_FIELD: {memspec.CONTEXT_METER_TRACE_UNTIL_FIELD: "soon"}}
            checks.append(("tracing stops after its expiry date unless config extends it",
                           record("x", event, path=expired_target, now=last_day, options={})
                           and not record("x", event, path=expired_target, now=day_after, options={})
                           and not record("x", event, path=expired_target, now=day_after, options=garbage)
                           and record("x", event, path=expired_target, now=day_after, options=extended)
                           and not record("x", event, path=expired_target, now=now, options=shortened)
                           and len(last(10, expired_target)) == 2))

            # 上限：超過就換名成 .1，只留一份舊的；總量不超過兩份上限。
            capped = root / "capped.jsonl"
            rotated = capped.with_name(capped.name + ".1")
            near_full = ("{}\n" * ((memspec.CONTEXT_METER_TRACE_MAX_BYTES - 64) // 3)).encode("ascii")
            capped.write_bytes(near_full)
            record("x", event, path=capped, now=now, options={}, round=1)
            first_rotation = rotated.stat().st_size == len(near_full) and len(last(9, capped)) >= 1
            record("x", event, path=capped, now=now, options={}, round=2)
            kept_small = not rotated.read_bytes().endswith(b'"round":2}\n')
            capped.write_bytes(near_full.replace(b"{}", b"[]"))
            record("x", event, path=capped, now=now, options={}, round=3)
            siblings = sorted(item.name for item in root.iterdir() if item.name.startswith("capped"))
            checks.append(("size cap rotates to one .1 file (replacing the older one) and never grows past it",
                           first_rotation and kept_small
                           and capped.stat().st_size <= memspec.CONTEXT_METER_TRACE_MAX_BYTES
                           and rotated.read_bytes().startswith(b"[]")
                           and siblings == ["capped.jsonl", "capped.jsonl.1", "capped.jsonl.mutex"]
                           and json.loads(last(1, capped)[0])["round"] == 3))

            # 失敗不外漏：目標是目錄、父目錄不在、欄位不能序列化——都回 False、不丟例外。
            as_directory = root / "is-a-directory"
            as_directory.mkdir()
            missing_parent = root / "nope" / "trace.jsonl"
            checks.append(("a trace failure returns False and never raises",
                           record("x", event, path=as_directory, now=now, options={}) is False
                           and record("x", event, path=missing_parent, now=now, options={}) is False
                           and not missing_parent.parent.exists()
                           and record("x", event, path=target, now=now, options={}, odd=object()) is True
                           and record("x", "not-a-dict", path=target, now="not-a-time", options={}) is False))
            checks.append(("last() on a missing file is empty", last(5, root / "absent.jsonl") == []))

            # 等不到鎖就不寫（不換名、不追加）；換名失敗而放不下，也不寫——上限是硬的。
            held = root / "held.jsonl"
            held.write_bytes(b'{"kept":1}\n')
            holder = os.open(held.with_name(held.name + ".mutex"), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
            try:
                held_lock = _acquire(holder, time.monotonic() + 1)
                started = time.monotonic()
                skipped = record("x", event, path=held, now=now, options={})
                waited = time.monotonic() - started
            finally:
                if held_lock:
                    _release(holder)
                os.close(holder)
            stuck = root / "stuck.jsonl"
            stuck_bytes = b"z" * (memspec.CONTEXT_METER_TRACE_MAX_BYTES - 10) + b"\n"
            stuck.write_bytes(stuck_bytes)
            stuck.with_name(stuck.name + ".1").mkdir()  # 換名目標是目錄：換名一定失敗
            over_cap = record("x", event, path=stuck, now=now, options={})
            checks.append(("no lock within the wait means no line; a failed rotation over the cap writes nothing",
                           held_lock and skipped is False
                           and waited >= memspec.CONTEXT_METER_TRACE_LOCK_SECONDS * 0.9
                           and held.read_bytes() == b'{"kept":1}\n'
                           and not held.with_name(held.name + ".1").exists()
                           and over_cap is False and stuck.read_bytes() == stuck_bytes
                           and record("x", event, path=held, now=now, options={}) is True))

            # 配對：同一個 run 的開始與結束併成一列；只有開始的醒目標出；只有結束的、其他行照印。
            sample = [
                json.dumps({"event": "PreCompact", "phase": "start", "run": "a1"}),
                json.dumps({"event": "PreCompact", "phase": "start", "run": "b2"}),
                json.dumps({"event": "PreToolUse", "outcome": "meter-emitted"}),
                json.dumps({"event": "PreCompact", "phase": "exit", "run": "a1", "outcome": "ok"}),
                json.dumps({"event": "SessionStart", "phase": "exit", "run": "c3", "outcome": "ok"}),
                "not json",
            ]
            shown = paired(sample)
            checks.append(("start/exit lines pair by run; an unmatched start stands out",
                           shown == [
                               "[START ONLY: killed, crashed or still running] " + sample[1],
                               sample[2],
                               "[start+exit] " + sample[3],
                               "[exit only] " + sample[4],
                               "not json",
                           ] and len(new_run()) == 8 and new_run() != new_run()))

            # 並行：8 條執行緒各寫 25 行（各自開檔、各自拿鎖），一行都不少、沒有一行被蓋壞。
            import threading

            shared = root / "shared.jsonl"

            def burst(worker):
                for index in range(25):
                    record("x", event, path=shared, now=now, options={}, worker=worker, index=index)

            threads = [threading.Thread(target=burst, args=(worker,)) for worker in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            written = [json.loads(line) for line in shared.read_text(encoding="utf-8").splitlines()]
            checks.append(("concurrent writers each keep every line intact",
                           len(written) == 200
                           and len({(row["worker"], row["index"]) for row in written}) == 200))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 9
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(_selftest() if "--selftest" in sys.argv[1:] else 2)
