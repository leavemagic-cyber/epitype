#!/usr/bin/env python3
"""讀取 Codex rollout JSONL，顯示最後一筆當前與累計 token 使用量。"""

import argparse
import contextlib
import io
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import tempfile
from pathlib import Path


# 2026-09-01 實測事故：相鄰欄位混用會把整場累計誤報為當前 context 用量；
# 規則：只採已觀察的 event_msg/token_count schema，當前用量讀取
# payload.info.last_token_usage.total_tokens，整場累計讀取
# payload.info.total_token_usage.total_tokens，視窗讀取 payload.info.model_context_window。


def _plain_int(value):
    """bool 是 int 的子類，但不能當作 token 數字。"""
    return isinstance(value, int) and not isinstance(value, bool)


def parse_rollout(path):
    """回傳 (reading, problem, bad_count, bad_line_samples)。"""
    bad_count = 0
    bad_lines = []
    token_event_count = 0
    last_payload = None
    last_line = 0

    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                bad_count += 1
                if len(bad_lines) < 5:
                    bad_lines.append(line_number)
                continue

            if not isinstance(record, dict) or record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            token_event_count += 1
            last_payload = payload
            last_line = line_number

    diagnostics = (bad_count, tuple(bad_lines))
    if last_payload is None:
        return None, "找不到可解析的 event_msg / token_count 事件。", *diagnostics

    # 「最後一筆」依檔案順序；它若缺欄位，就明說失敗，不偷用較早事件。
    info = last_payload.get("info")
    if not isinstance(info, dict):
        return None, f"最後一筆 token_count（第 {last_line} 行）的 payload.info 不是物件。", *diagnostics
    current_usage = info.get("last_token_usage")
    if not isinstance(current_usage, dict):
        problem = f"最後一筆 token_count（第 {last_line} 行）缺少 payload.info.last_token_usage 物件。"
        return None, problem, *diagnostics

    current_tokens = current_usage.get("total_tokens")
    total_usage = info.get("total_token_usage")
    cumulative_tokens = None
    if total_usage is not None:
        if not isinstance(total_usage, dict):
            problem = f"最後一筆 token_count（第 {last_line} 行）的 total_token_usage 不是物件。"
            return None, problem, *diagnostics
        cumulative_tokens = total_usage.get("total_tokens")
        if not _plain_int(cumulative_tokens) or cumulative_tokens < 0:
            problem = f"最後一筆 token_count（第 {last_line} 行）的累計 total_tokens 不是非負整數。"
            return None, problem, *diagnostics
    window = info.get("model_context_window")
    if not _plain_int(current_tokens) or current_tokens < 0:
        problem = f"最後一筆 token_count（第 {last_line} 行）的當前 total_tokens 不是非負整數。"
        return None, problem, *diagnostics
    if not _plain_int(window) or window <= 0:
        problem = f"最後一筆 token_count（第 {last_line} 行）的 model_context_window 不是正整數。"
        return None, problem, *diagnostics

    reading = (current_tokens, cumulative_tokens, window, last_line, token_event_count)
    return reading, None, *diagnostics


def print_report(report):
    reading, problem, bad_count, bad_lines = report
    if bad_count:
        lines = ", ".join(str(number) for number in bad_lines)
        suffix = "（僅列前 5 行）" if bad_count > 5 else ""
        print(f"WARNING: {bad_count} 行 JSON 無法解析；行號 {lines}{suffix}。", file=sys.stderr)
    if reading is None:
        print(f"UNAVAILABLE: {problem}", file=sys.stderr)
        return 1

    current_tokens, cumulative_tokens, window, _line, _count = reading
    print(f"current_context_tokens: {current_tokens}")
    if cumulative_tokens is not None:
        print(f"cumulative_total_tokens: {cumulative_tokens}")
    print(f"model_context_window: {window}")
    if current_tokens > window:
        print(
            "usage_percent: ⚠ 資料異常："
            f"current_context_tokens ({current_tokens}) 超過 model_context_window ({window})"
        )
        return 1
    print(f"usage_percent: {current_tokens * 100.0 / window:.2f}%")
    return 0


def _fake_event(current, window, cumulative=None):
    info = {
        "last_token_usage": {"total_tokens": current},
        "model_context_window": window,
    }
    if cumulative is not None:
        info["total_token_usage"] = {"total_tokens": cumulative}
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": info,
        },
    }


def run_selftest():
    # 累計刻意遠大於當前與視窗；百分比只能採用當前 context 用量。
    fake_lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "fake"}}),
        json.dumps(_fake_event(40, 100, cumulative=4_000)),
        "{這一行故意不是合法 JSON",
        json.dumps(_fake_event(75, 200, cumulative=5_000)),
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="token_meter_") as temp_dir:
            fake_path = Path(temp_dir).resolve() / "fake_rollout.jsonl"
            fake_path.write_text("\n".join(fake_lines) + "\n", encoding="utf-8")
            report = parse_rollout(fake_path)
        expected = ((75, 5_000, 200, 4, 2), None, 1, (3,))
        if report != expected:
            print(f"SELFTEST FAIL: expected {expected!r}, got {report!r}")
            return 1
        normal_output = io.StringIO()
        normal_error = io.StringIO()
        with contextlib.redirect_stdout(normal_output), contextlib.redirect_stderr(normal_error):
            normal_exit = print_report(report)
        normal_text = normal_output.getvalue()
        expected_lines = (
            "current_context_tokens: 75",
            "cumulative_total_tokens: 5000",
            "usage_percent: 37.50%",
        )
        if normal_exit != 0 or any(line not in normal_text for line in expected_lines):
            print(f"SELFTEST FAIL: 累計≠當前案例輸出錯誤：{normal_text!r}")
            return 1

        anomalous = ((201, 9_999, 200, 1, 1), None, 0, ())
        anomalous_output = io.StringIO()
        with contextlib.redirect_stdout(anomalous_output):
            anomalous_exit = print_report(anomalous)
        anomalous_text = anomalous_output.getvalue()
        if anomalous_exit == 0 or "usage_percent: ⚠ 資料異常：" not in anomalous_text:
            print(f"SELFTEST FAIL: 超窗案例未正確回報資料異常：{anomalous_text!r}")
            return 1
        print(anomalous_text, end="")
    except Exception as exc:  # 自測也必須明說失敗，不吞例外。
        print(f"SELFTEST FAIL: {type(exc).__name__}: {exc}")
        return 1
    print("SELFTEST PASS")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="顯示 Codex rollout 最後一筆 token_count 的當前用量、累計用量、視窗與佔用率。"
    )
    parser.add_argument("rollout", nargs="?", help="rollout JSONL 檔案路徑")
    parser.add_argument("--selftest", action="store_true", help="執行內建合成資料自測")
    args = parser.parse_args()

    if args.selftest:
        if args.rollout:
            parser.error("--selftest 不可同時指定 rollout 路徑")
        return run_selftest()
    if not args.rollout:
        parser.error("請指定 rollout JSONL 路徑，或使用 --selftest")

    path = Path(args.rollout)
    if not path.is_file():
        print(f"UNAVAILABLE: 檔案不存在或不是一般檔案：{path}", file=sys.stderr)
        return 1
    try:
        return print_report(parse_rollout(path))
    except (OSError, UnicodeError) as exc:
        print(f"UNAVAILABLE: 無法讀取 {path}：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
