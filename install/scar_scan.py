import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # Keep cp950 consoles deterministic.
"""Scan user transcript messages and write a proposal-only scar report."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from epitype.compact_map import _text_content
from epitype.memspec import COMPACT_MAP_MAX_LINE_BYTES, SCAR_CORRECTION_PATTERNS


SAMPLE_LIMIT = 3
SNIPPET_RADIUS = 80


def _inside_repo(path):
    try:
        path.resolve().relative_to(REPO_ROOT.resolve())
        return True
    except ValueError:
        return False


def _snippet(text, match):
    single_line = " ".join(text.split())
    if len(single_line) <= SNIPPET_RADIUS * 2:
        return single_line
    start = max(0, match.start() - SNIPPET_RADIUS)
    end = min(len(single_line), match.end() + SNIPPET_RADIUS)
    prefix = "…" if start else ""
    suffix = "…" if end < len(single_line) else ""
    return prefix + single_line[start:end] + suffix


def _records(path):
    with path.open("rb") as stream:
        for raw_line in stream:
            if len(raw_line) > COMPACT_MAP_MAX_LINE_BYTES:
                continue
            try:
                item = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(item, dict) or item.get("isSidechain") is True:
                continue
            if item.get("type") != "user":
                continue
            text = _text_content(item.get("message"))
            if text.strip():
                yield text


def scan(directory):
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise NotADirectoryError(os.fspath(directory))
    compiled = [
        (key, label, pattern, re.compile(pattern, re.IGNORECASE | re.UNICODE))
        for key, label, pattern in SCAR_CORRECTION_PATTERNS
    ]
    clusters = {
        key: {
            "key": key,
            "label": label,
            "pattern": pattern,
            "count": 0,
            "samples": [],
        }
        for key, label, pattern, _ in compiled
    }
    files = sorted(directory.rglob("*.jsonl"), key=lambda item: os.path.normcase(os.fspath(item)))
    messages = 0
    for path in files:
        for text in _records(path):
            messages += 1
            normalized = " ".join(text.split())
            for key, _, _, regex in compiled:
                matches = list(regex.finditer(normalized))
                if not matches:
                    continue
                cluster = clusters[key]
                cluster["count"] += len(matches)
                for match in matches:
                    sample = _snippet(normalized, match)
                    if sample not in cluster["samples"] and len(cluster["samples"]) < SAMPLE_LIMIT:
                        cluster["samples"].append(sample)
    matched = [cluster for cluster in clusters.values() if cluster["count"]]
    order = {key: index for index, (key, _, _) in enumerate(SCAR_CORRECTION_PATTERNS)}
    matched.sort(key=lambda item: (-item["count"], order[item["key"]]))
    return {
        "files_scanned": len(files),
        "messages_scanned": messages,
        "clusters": matched,
    }


def _markdown_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_proposal(result):
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Scar card proposals",
        "",
        f"Generated (UTC): {generated}",
        f"Transcript files scanned: {result['files_scanned']}",
        f"User messages scanned: {result['messages_scanned']}",
        "",
        "> Proposal only. No card was written to any memory vault.",
    ]
    if not result["clusters"]:
        lines.extend(("", "No correction-pattern proposals found."))
        return "\n".join(lines) + "\n"

    for cluster in result["clusters"]:
        lines.extend((
            "",
            f"## Pattern: {cluster['label']}",
            "",
            f"Occurrences: {cluster['count']}",
            "",
            "Sample excerpts:",
            "",
        ))
        lines.extend(f"> {_markdown_escape(sample)}" for sample in cluster["samples"])
        lines.extend((
            "",
            "Suggested card draft:",
            "",
            "```yaml",
            f"name: proposed-{cluster['key']}",
            "status: proposal",
            "source: transcript-correction-pattern",
            f"pattern: {cluster['label']}",
            f"occurrences: {cluster['count']}",
            "advice: Review the excerpts, derive a narrow corrective rule, and obtain user approval before filing.",
            "```",
        ))
    return "\n".join(lines) + "\n"


def write_proposal(directory, output_path):
    output_path = Path(output_path).expanduser().resolve()
    if _inside_repo(output_path):
        raise ValueError("proposal output must be outside the Epitype repository")
    result = scan(directory)
    payload = render_proposal(result).encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=output_path.name + ".",
        suffix=".epitype_tmp",
        dir=output_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return result


def _json_line(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _files(root):
    return {path.resolve() for path in root.rglob("*") if path.is_file()}


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-scar-scan-") as temp_dir:
            root = Path(temp_dir).resolve()
            transcripts = root / "transcripts"
            transcripts.mkdir()
            first = transcripts / "first.jsonl"
            first.write_text(
                "\n".join((
                    _json_line({"type": "user", "message": {"content": "又錯了，不是這樣。"}}),
                    _json_line({"type": "user", "message": {"content": [
                        {"type": "text", "text": "再次提醒，"},
                        {"type": "text", "text": "我說過要用合成資料。"},
                    ]}}),
                    _json_line({"type": "user", "message": {"content": "Again, I told you this is wrong. Stop doing that."}}),
                    _json_line({"type": "assistant", "message": {"content": "wrong again"}}),
                    _json_line({"type": "user", "isSidechain": True, "message": {"content": "wrong again stop doing"}}),
                )) + "\n",
                encoding="utf-8",
            )
            second = transcripts / "nested" / "second.jsonl"
            second.parent.mkdir()
            second.write_text(
                _json_line({"type": "user", "message": {"content": "Wrong again."}}) + "\n",
                encoding="utf-8",
            )
            before = _files(root)
            proposal = root / "output" / "scar-proposals.md"
            result = write_proposal(transcripts, proposal)
            by_key = {cluster["key"]: cluster for cluster in result["clusters"]}
            body = proposal.read_text(encoding="utf-8")
            checks.append((
                "Chinese and English correction patterns detected",
                all(key in by_key for key in (
                    "zh-you", "zh-again", "zh-told", "zh-wrong", "zh-not-like-this",
                    "en-again", "en-told", "en-wrong", "en-stop-doing",
                )),
            ))
            checks.append((
                "sidechain and assistant messages skipped",
                result["messages_scanned"] == 4
                and by_key["en-stop-doing"]["count"] == 1,
            ))
            checks.append((
                "same-pattern clusters count occurrences",
                by_key["en-again"]["count"] == 2
                and by_key["en-wrong"]["count"] == 2
                and "Occurrences: 2" in body,
            ))
            after = _files(root)
            checks.append((
                "only the requested proposal file is produced",
                after - before == {proposal.resolve()}
                and "Proposal only" in body
                and "Suggested card draft" in body,
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 4
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, required=True, help="directory containing transcript JSONL files")
    parser.add_argument("--out", type=Path, required=True, help="proposal Markdown path outside the repository")
    return parser


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    parsed = _parser().parse_args(arguments)
    try:
        result = write_proposal(parsed.dir, parsed.out)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"PROPOSAL: {parsed.out.expanduser().resolve()}")
    print(f"CLUSTERS: {len(result['clusters'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
