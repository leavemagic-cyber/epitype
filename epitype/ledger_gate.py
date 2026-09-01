import sys; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，避免繁中輸出中斷。
"""Epitype 記帳先查證閘與完成宣稱閘共用的證據驗法。"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import threading

from memspec import LOCK_STALE_SECONDS, file_lock


# 2026-09-01 實測事故：帳本宣稱已記錄但 anchor 靜默未命中，導致補丁未套用
# 仍被記成完成；規則：證據未逐條在檔案 bytes 中命中前，任何失敗路徑都不得碰帳本。


def _display_text(value):
    """保留完整證據文字，只逸出換行以維持一條 evidence 一行。"""
    return value.replace("\r", "\\r").replace("\n", "\\n")


def _parse_evidence(raw):
    """以第一個雙冒號切開 Windows 路徑與必須命中的子字串。"""
    path_text, separator, substring = raw.partition("::")
    if not separator or not path_text or not substring:
        return None, "格式必須是 檔路徑::非空子字串"
    return (Path(path_text), substring), None


def _verify_evidence(raw_items):
    """讀取 evidence bytes，回傳全部逐條結果，不在失敗時提早停止。"""
    results = []
    for index, raw in enumerate(raw_items, start=1):
        parsed, parse_error = _parse_evidence(raw)
        if parse_error is not None:
            results.append((index, False, raw, parse_error))
            continue

        evidence_path, substring = parsed
        try:
            payload = evidence_path.read_bytes()
        except FileNotFoundError:
            results.append((index, False, raw, "檔案不存在"))
            continue
        except (OSError, ValueError) as exc:
            results.append(
                (index, False, raw, "檔案無法讀取: " + type(exc).__name__)
            )
            continue

        needle = substring.encode("utf-8", errors="replace")
        if needle in payload:
            results.append((index, True, raw, "bytes 子字串命中"))
        else:
            results.append((index, False, raw, "缺少 bytes 子字串"))
    return results


def _print_evidence_results(results, out):
    """輸出穩定且可供 E2 完成宣稱引用的逐條判定。"""
    if out is None:
        return
    for index, passed, raw, detail in results:
        verdict = "OK" if passed else "FAIL"
        print(
            "EVIDENCE "
            + str(index)
            + " "
            + verdict
            + ": "
            + detail
            + " | "
            + _display_text(raw),
            file=out,
        )


def _append_locked(ledger_path, entry, lock_timeout_seconds):
    """在 memspec 共用鎖內追加完整 UTF-8 帳目，回傳成功與錯誤原因。"""
    try:
        with file_lock(ledger_path, lock_timeout_seconds) as acquired:
            if not acquired:
                return False, "鎖逾時，帳本未寫入"

            prefix = b""
            try:
                if ledger_path.stat().st_size:
                    with ledger_path.open("rb") as existing:
                        existing.seek(-1, os.SEEK_END)
                        if existing.read(1) not in (b"\n", b"\r"):
                            prefix = b"\n"
            except FileNotFoundError:
                pass

            record = entry.encode("utf-8", errors="replace")
            if not record.endswith((b"\n", b"\r")):
                record += b"\n"
            payload = prefix + record

            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(ledger_path, flags, 0o600)
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise OSError("short ledger write")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except (OSError, TypeError, ValueError) as exc:
        return False, "帳本寫入失敗: " + type(exc).__name__
    return True, None


def _run_append(
    ledger,
    entry,
    evidence,
    check_only=False,
    out=sys.stdout,
    err=sys.stderr,
    lock_timeout_seconds=LOCK_STALE_SECONDS,
):
    """執行完整閘門；證據失敗時不呼叫任何帳本檔案操作。"""
    results = _verify_evidence(evidence)
    _print_evidence_results(results, out)
    failed = [result for result in results if not result[1]]
    if failed:
        if err is not None:
            print(
                "REFUSED: "
                + str(len(failed))
                + " 條證據未通過；帳本保證未動",
                file=err,
            )
        return 1

    if check_only:
        if out is not None:
            print("OK: CHECK-ONLY 證據全數通過；帳本未寫入", file=out)
        return 0

    if not entry:
        if err is not None:
            print("REFUSED: entry 不得為空；帳本未寫入", file=err)
        return 1

    ledger_path = Path(ledger)
    appended, error = _append_locked(
        ledger_path,
        entry,
        lock_timeout_seconds,
    )
    if not appended:
        if err is not None:
            print("REFUSED: " + error, file=err)
        return 1

    if out is not None:
        print("OK: 帳目已追加 | ledger=" + os.fspath(ledger_path), file=out)
    return 0


def _quiet_run(*args, **kwargs):
    """selftest 專用：保留回傳碼並攔住預期失敗的診斷文字。"""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = _run_append(*args, out=stdout, err=stderr, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="ledger-gate-") as temp_dir:
            root = Path(temp_dir)

            evidence_ok = root / "evidence-ok.txt"
            evidence_ok.write_bytes(b"prefix PATCH_APPLIED suffix")
            ledger_ok = root / "ledger-ok.md"
            code, _, _ = _quiet_run(
                ledger_ok,
                "第一筆",
                [os.fspath(evidence_ok) + "::PATCH_APPLIED"],
            )
            checks.append(
                (
                    "evidence present appends exact entry",
                    code == 0 and ledger_ok.read_bytes() == "第一筆\n".encode("utf-8"),
                )
            )

            ledger_missing = root / "ledger-missing.md"
            ledger_missing.write_bytes(b"seed\n")
            before_missing = ledger_missing.read_bytes()
            code, _, error = _quiet_run(
                ledger_missing,
                "must-not-append",
                [os.fspath(evidence_ok) + "::NOT_PRESENT"],
            )
            checks.append(
                (
                    "missing evidence refuses with byte-identical ledger",
                    code == 1
                    and ledger_missing.read_bytes() == before_missing
                    and "帳本保證未動" in error,
                )
            )

            evidence_second = root / "evidence-second.txt"
            evidence_second.write_bytes(b"SECOND_OK")
            ledger_multi = root / "ledger-multi.md"
            ledger_multi.write_bytes(b"unchanged\n")
            before_multi = ledger_multi.read_bytes()
            code, output, _ = _quiet_run(
                ledger_multi,
                "must-not-append",
                [
                    os.fspath(evidence_ok) + "::PATCH_APPLIED",
                    os.fspath(evidence_second) + "::MISSING",
                ],
            )
            checks.append(
                (
                    "one failure rejects all multiple evidence",
                    code == 1
                    and ledger_multi.read_bytes() == before_multi
                    and "EVIDENCE 1 OK" in output
                    and "EVIDENCE 2 FAIL" in output,
                )
            )

            ledger_check_only = root / "ledger-check-only.md"
            code, output, _ = _quiet_run(
                ledger_check_only,
                "must-not-append",
                [os.fspath(evidence_ok) + "::PATCH_APPLIED"],
                check_only=True,
            )
            checks.append(
                (
                    "check-only never creates ledger",
                    code == 0
                    and not ledger_check_only.exists()
                    and "OK: CHECK-ONLY" in output,
                )
            )

            long_needle = "E2_FULL_EVIDENCE_" + "x" * 160
            evidence_long = root / "evidence-with-a-long-artifact-name.txt"
            evidence_long.write_text(long_needle, encoding="utf-8")
            raw_long = os.fspath(evidence_long) + "::" + long_needle
            code, output, _ = _quiet_run(
                root / "ledger-long-evidence.md",
                "must-not-append",
                [raw_long],
                check_only=True,
            )
            checks.append(
                (
                    "E2 output preserves full artifact path and substring",
                    code == 0 and raw_long in output and "..." not in output,
                )
            )

            evidence_zh = root / "evidence-zh.bin"
            evidence_zh.write_bytes(
                b"\xff" + "補丁真的存在".encode("utf-8") + b"\xfe"
            )
            code, _, _ = _quiet_run(
                root / "ledger-zh.md",
                "不會寫入",
                [os.fspath(evidence_zh) + "::補丁真的存在"],
                check_only=True,
            )
            checks.append(("Chinese UTF-8 bytes match amid invalid bytes", code == 0))

            ledger_concurrent = root / "ledger-concurrent.md"
            worker_count = 20
            barrier = threading.Barrier(worker_count)
            worker_codes = [None] * worker_count
            entries = [
                "worker-" + str(index).zfill(2) + ":" + str(index) * 1024
                for index in range(worker_count)
            ]

            def append_worker(index):
                try:
                    barrier.wait()
                    worker_codes[index] = _run_append(
                        ledger_concurrent,
                        entries[index],
                        [os.fspath(evidence_ok) + "::PATCH_APPLIED"],
                        out=None,
                        err=None,
                    )
                except threading.BrokenBarrierError:
                    worker_codes[index] = 1

            workers = [
                threading.Thread(target=append_worker, args=(index,))
                for index in range(worker_count)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            concurrent_lines = ledger_concurrent.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
            checks.append(
                (
                    "concurrent appends are complete and untorn",
                    worker_codes == [0] * worker_count
                    and len(concurrent_lines) == worker_count
                    and set(concurrent_lines) == set(entries),
                )
            )

            ledger_locked = root / "ledger-locked.md"
            lock_path = Path(os.fspath(ledger_locked) + ".lock")
            lock_path.write_text("held\n", encoding="ascii")
            code, _, error = _quiet_run(
                ledger_locked,
                "must-not-append",
                [os.fspath(evidence_ok) + "::PATCH_APPLIED"],
                lock_timeout_seconds=0.0,
            )
            checks.append(
                (
                    "lock timeout refuses without hard write",
                    code == 1
                    and not ledger_locked.exists()
                    and "鎖逾時" in error,
                )
            )
    except Exception as exc:
        print(
            "SELFTEST ERROR " + type(exc).__name__ + ": " + str(exc),
            file=sys.stderr,
        )

    total = 8
    passed = sum(bool(ok) for _, ok in checks)
    status = "PASS" if len(checks) == total and passed == total else "FAIL"
    print("SELFTEST " + status + " " + str(passed) + "/" + str(total))
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print("FAILED: " + name, file=sys.stderr)
    return 0 if status == "PASS" else 1


def _build_parser():
    parser = argparse.ArgumentParser(description="Epitype 記帳先查證閘")
    parser.add_argument("--selftest", action="store_true", help="執行內建合成測試")
    subparsers = parser.add_subparsers(dest="command")
    append_parser = subparsers.add_parser("append", help="證據全過後追加帳目")
    append_parser.add_argument("--ledger", required=True, help="帳本路徑")
    append_parser.add_argument("--entry", required=True, help="帳目文字")
    append_parser.add_argument(
        "--evidence",
        required=True,
        action="append",
        help="可重複，格式為 檔路徑::子字串",
    )
    append_parser.add_argument(
        "--check-only",
        action="store_true",
        help="只驗證據，不建立或寫入帳本",
    )
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.selftest:
        if args.command is not None:
            parser.error("--selftest 不得與 append 同用")
        return _selftest()
    if args.command != "append":
        parser.error("必須使用 append 或 --selftest")
    return _run_append(
        args.ledger,
        args.entry,
        args.evidence,
        check_only=args.check_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
