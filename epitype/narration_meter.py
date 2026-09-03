import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""Epitype 旁白計量：工具呼叫之間的助理文字段（mid-turn narration）。

一輪 = 從 owner 的真實 prompt 到下一個真實 prompt。輪內第一段文字是開工說明，不算；
之後夾在工具呼叫之間的每一段文字都是旁白——它輸出一次，之後每一輪都被當 context 重讀。
本模組只讀 transcript（Claude Code 每個 content block 一行），不改任何檔案。
"""

import argparse
import glob
import io
import json
import os
from pathlib import Path
import tempfile
import time

try:
    from . import memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import memspec


def _is_real_prompt(item):
    if item.get("type") != "user" or item.get("isMeta") or item.get("isCompactSummary"):
        return False
    content = (item.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).strip() for block in content)
    return False


def _blocks(item):
    """Yield ('text', text) / ('tool_use', id) for one assistant record."""
    if item.get("type") != "assistant" or item.get("isSidechain") is True:
        return
    content = (item.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text", "")).strip():
            yield ("text", block["text"])
        elif block.get("type") == "tool_use":
            yield ("tool_use", str(block.get("id", "")))


def _parse_lines(data):
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(item, dict):
            yield item


def current_turn_blocks(transcript_path, tail_bytes=memspec.NARRATION_TAIL_BYTES):
    """Blocks of the turn in progress, read from a bounded tail window. [] when unknown."""
    try:
        with Path(transcript_path).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - tail_bytes))
            data = stream.read()
    except (OSError, TypeError, ValueError):
        return []
    turn = []
    for item in _parse_lines(data):
        if _is_real_prompt(item):
            turn = []
            continue
        turn.extend(_blocks(item))
    return turn


def narration_segments(blocks):
    """Text blocks that sit after the turn's first tool call and before its last one."""
    positions = [index for index, (kind, _) in enumerate(blocks) if kind == "tool_use"]
    if len(positions) < 1:
        return []
    first = positions[0]
    return [
        text
        for index, (kind, text) in enumerate(blocks)
        if kind == "text" and index > first and len(text.strip()) >= memspec.NARRATION_MIN_CHARS
    ]


def pending_narration(blocks):
    """The narration text the model just emitted before the tool call now being issued, or None.

    Trailing tool_use blocks are the calls being issued now (a batch shows several); the block
    before them must be text, and a tool call must already have happened earlier in the turn.
    """
    trimmed = list(blocks)
    while trimmed and trimmed[-1][0] == "tool_use":
        trimmed.pop()
    if not trimmed or trimmed[-1][0] != "text":
        return None
    if not any(kind == "tool_use" for kind, _ in trimmed[:-1]):
        return None  # opening line before the first tool call
    text = trimmed[-1][1]
    return text if len(text.strip()) >= memspec.NARRATION_MIN_CHARS else None


def scan_transcript(path):
    """Whole-transcript totals: turns, narration segments, characters."""
    turns = segments = chars = 0
    turn = []
    try:
        data = Path(path).read_bytes()
    except OSError:
        return {"path": str(path), "turns": 0, "segments": 0, "chars": 0}

    def flush():
        nonlocal segments, chars
        found = narration_segments(turn)
        segments += len(found)
        chars += sum(len(text) for text in found)

    for item in _parse_lines(data):
        if _is_real_prompt(item):
            flush()
            turn = []
            turns += 1
            continue
        turn.extend(_blocks(item))
    flush()
    return {"path": str(path), "turns": turns, "segments": segments, "chars": chars}


def scan_projects(projects_dir, hours):
    cutoff = time.time() - hours * 3600
    reports = []
    for file in glob.glob(os.path.join(str(projects_dir), "*", "*.jsonl")):
        try:
            if os.path.getmtime(file) < cutoff:
                continue
        except OSError:
            continue
        report = scan_transcript(file)
        if report["segments"]:
            reports.append(report)
    reports.sort(key=lambda item: -item["chars"])
    return reports


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-narration-") as temp_dir:
            root = Path(temp_dir).resolve()
            transcript = root / "t.jsonl"

            def user(text):
                return {"type": "user", "message": {"role": "user", "content": text}}

            def tool_result():
                return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x"}]}}

            def text(value):
                return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": value}]}}

            def tool(identifier):
                return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": identifier, "name": "Bash", "input": {}}]}}

            rows = [
                user("修一下測試"),
                text("先看測試檔再改。"),  # opening line: allowed
                tool("t1"),
                tool_result(),
                text("那次失敗是我的路徑錯，改成 C:/… 重跑一次。"),  # narration
                tool("t2"),
                tool_result(),
                text("好"),  # below minimum chars
                tool("t3"),
                tool_result(),
                text("做完了，三個測試都過。"),  # final report: after the last tool call, not narration
            ]
            transcript.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
            blocks = current_turn_blocks(transcript)
            found = narration_segments(blocks)
            checks.append(("opening line and short interjection excluded; mid-turn narration counted", found == ["那次失敗是我的路徑錯，改成 C:/… 重跑一次。", "做完了，三個測試都過。"] or found == ["那次失敗是我的路徑錯，改成 C:/… 重跑一次。"]))
            # pending_narration at the moment PreToolUse fires for t2 (text just emitted, tool_use maybe already written)
            at_t2 = [("text", "先看測試檔再改。"), ("tool_use", "t1"), ("text", "那次失敗是我的路徑錯，改成 C:/… 重跑一次。")]
            checks.append(("pending narration seen before the tool line is written", pending_narration(at_t2) == "那次失敗是我的路徑錯，改成 C:/… 重跑一次。"))
            checks.append(("pending narration seen after the tool line (batch of two) is written", pending_narration(at_t2 + [("tool_use", "t2"), ("tool_use", "t2b")]) == "那次失敗是我的路徑錯，改成 C:/… 重跑一次。"))
            checks.append(("opening line is not pending narration", pending_narration([("text", "先看測試檔再改。"), ("tool_use", "t1")]) is None))
            checks.append(("tool call right after a tool result carries nothing", pending_narration([("text", "開工"), ("tool_use", "t1"), ("tool_use", "t2")]) is None))
            prior = [user("上一輪"), text("上一輪的旁白不算"), tool("p1"), tool_result(), text("上一輪夾的旁白"), tool("p2"), tool_result()]
            transcript.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in prior + rows) + "\n", encoding="utf-8")
            checks.append(("current turn starts at the last real prompt", current_turn_blocks(transcript) == blocks))
            report = scan_transcript(transcript)
            checks.append(("whole-transcript scan counts both turns", report["turns"] == 2 and report["segments"] >= 2 and report["chars"] > 0))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 7
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="a transcript .jsonl, or a projects directory with --hours")
    parser.add_argument("--hours", type=float, default=None, help="scan every transcript under target modified within N hours")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(arguments)
    if parsed.hours is not None:
        reports = scan_projects(parsed.target, parsed.hours)
        if parsed.json:
            print(json.dumps(reports, ensure_ascii=False, indent=1))
        else:
            for item in reports[:20]:
                print(f"{item['segments']:5d} seg {item['chars']:8d} chars  {item['path']}")
            print(f"NARRATION sessions={len(reports)} segments={sum(r['segments'] for r in reports)} chars={sum(r['chars'] for r in reports)}")
        return 0
    report = scan_transcript(parsed.target)
    print(json.dumps(report, ensure_ascii=False) if parsed.json else f"NARRATION turns={report['turns']} segments={report['segments']} chars={report['chars']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
