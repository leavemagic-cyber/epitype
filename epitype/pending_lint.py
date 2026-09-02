import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""Epitype 殭屍待辦 lint：卡片裡有入口沒出口的待辦行。

一行是殭屍待辦，若它同時滿足：帶待辦標記（memspec.PENDING_MARKER_PATTERN）、沒有
收尾字樣（PENDING_CLOSED_PATTERN）、沒有可跑的 ``verify:``、而且年齡超過
PENDING_MAX_AGE_DAYS——年齡取行內 ISO 日期，沒有才退回卡片 mtime。只點名，不改卡。
"""

import argparse
from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile

try:
    from . import memsearch, memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import memsearch
    import memspec

MAX_CARD_BYTES = 256 * 1024


def _line_age_days(line, mtime, today):
    match = memspec.PENDING_DATE_REGEX.search(line)
    if match:
        try:
            stamped = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return (today - stamped).days, "line"
        except ValueError:
            pass
    modified = datetime.fromtimestamp(mtime, tz=timezone.utc).date()
    return (today - modified).days, "mtime"


def _is_pending(line):
    return (
        memspec.PENDING_MARKER_REGEX.search(line) is not None
        and memspec.PENDING_CLOSED_REGEX.search(line) is None
        and memspec.PENDING_VERIFY_MARKER not in line
    )


def scan_vault(vault, max_age_days=memspec.PENDING_MAX_AGE_DAYS, today=None):
    vault = Path(vault).resolve()
    today = today or datetime.now(timezone.utc).date()
    cards = []
    for path in memsearch.card_files(vault):
        try:
            stat = path.stat()
            if stat.st_size > MAX_CARD_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        zombies = []
        in_frontmatter = False
        for number, line in enumerate(text.splitlines(), 1):
            # Frontmatter describes the card; only body lines can be to-do items.
            if line.strip() == "---" and (number == 1 or in_frontmatter):
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter or not _is_pending(line):
                continue
            age, source = _line_age_days(line, stat.st_mtime, today)
            if age >= max_age_days:
                zombies.append({"line": number, "age_days": age, "date_source": source, "text": line.strip()[:160]})
        if zombies:
            cards.append({
                "path": path.relative_to(vault).as_posix(),
                "oldest_days": max(item["age_days"] for item in zombies),
                "lines": zombies,
            })
    cards.sort(key=lambda item: (-item["oldest_days"], item["path"]))
    return {
        "vault": str(vault),
        "max_age_days": max_age_days,
        "zombie_cards": len(cards),
        "zombie_lines": sum(len(item["lines"]) for item in cards),
        "oldest_days": max((item["oldest_days"] for item in cards), default=0),
        "cards": cards,
    }


def summary_line(vaults, max_age_days=memspec.PENDING_MAX_AGE_DAYS, today=None):
    """One bounded line for SessionStart, or None when nothing is overdue."""
    lines = cards = oldest = 0
    worst = None
    for vault in vaults:
        try:
            report = scan_vault(vault, max_age_days, today)
        except OSError:
            continue
        lines += report["zombie_lines"]
        cards += report["zombie_cards"]
        if report["oldest_days"] > oldest:
            oldest, worst = report["oldest_days"], report["vault"]
    if not lines:
        return None
    return (
        f"⏳ 殭屍待辦 {lines} 行／{cards} 卡（最舊 {oldest} 天，逾 {max_age_days} 天未收尾）"
        f"→ python epitype/pending_lint.py \"{worst}\""
    )


def _print_report(report, output):
    for card in report["cards"]:
        print(f"{card['path']} oldest={card['oldest_days']}d", file=output)
        for item in card["lines"]:
            print(f"  L{item['line']} {item['age_days']}d({item['date_source']}) {item['text']}", file=output)
    print(
        f"PENDING zombies={report['zombie_lines']} cards={report['zombie_cards']} "
        f"oldest={report['oldest_days']}d max_age={report['max_age_days']}d",
        file=output,
    )


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-pending-") as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            today = date(2026, 9, 2)
            (vault / "plan.md").write_text(
                "---\nname: plan\ndescription: 2026-06-01 synthetic plan（待 owner 決策項）\n---\n"
                "- 2026-07-22 未辦（owner 自行）：SWSetup\n"
                "- 2026-09-01 待辦：明天再看\n"
                "- 2026-07-01 待辦：跑 verify: python check.py\n"
                "- ~~2026-07-01 未辦：舊項~~ 2026-09-02 作廢\n"
                "- 2026-06-01 ⏳ 等 owner 決定 ✅ 已完成\n",
                encoding="utf-8",
            )
            old_mtime = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
            (vault / "undated.md").write_text(
                "---\nname: undated\ndescription: synthetic\n---\nTODO 沒日期的待辦\n",
                encoding="utf-8",
            )
            os.utime(vault / "undated.md", (old_mtime, old_mtime))
            (vault / "_views.md").write_text("---\nname: v\ndescription: v\n---\n未辦 私有視圖\n", encoding="utf-8")
            (vault / memspec.MEMORY_INDEX_FILENAME).write_text("# index\n未辦 索引行\n", encoding="utf-8")

            report = scan_vault(vault, today=today)
            plan = next((card for card in report["cards"] if card["path"] == "plan.md"), None)
            checks.append((
                "dated overdue pending line is a zombie; fresh line is not",
                plan is not None
                and [item["line"] for item in plan["lines"]] == [5]
                and plan["lines"][0]["age_days"] == 42
                and plan["lines"][0]["date_source"] == "line",
            ))
            checks.append((
                "verify: line and closed lines are exempt",
                plan is not None and all(item["line"] not in (7, 8, 9) for item in plan["lines"]),
            ))
            undated = next((card for card in report["cards"] if card["path"] == "undated.md"), None)
            checks.append((
                "undated line falls back to card mtime age",
                undated is not None
                and undated["lines"][0]["date_source"] == "mtime"
                and undated["lines"][0]["age_days"] == 32,
            ))
            checks.append((
                "index, underscore files and frontmatter are never scanned",
                all(card["path"] not in ("_views.md", memspec.MEMORY_INDEX_FILENAME) for card in report["cards"])
                and all(item["line"] != 3 for item in plan["lines"])
                and report["zombie_cards"] == 2
                and report["zombie_lines"] == 2,
            ))
            line = summary_line([vault], today=today)
            checks.append((
                "session-start summary is one line naming counts and the worst vault",
                isinstance(line, str)
                and "\n" not in line
                and "2 行／2 卡" in line
                and "最舊 42 天" in line
                and str(vault.resolve()) in line,
            ))
            checks.append((
                "clean vault yields no summary",
                summary_line([vault], max_age_days=10_000, today=today) is None,
            ))
            out = io.StringIO()
            code = main(["--strict", "--today", "2026-09-02", os.fspath(vault)], output=out)
            checks.append((
                "strict CLI exits 1 and prints the zombie lines",
                code == 1 and "plan.md oldest=42d" in out.getvalue() and "PENDING zombies=2" in out.getvalue(),
            ))
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


def main(argv=None, output=sys.stdout):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault", type=Path)
    parser.add_argument("--max-age-days", type=int, default=memspec.PENDING_MAX_AGE_DAYS)
    parser.add_argument("--today", type=date.fromisoformat, default=None, help="ISO date override for reproducible runs")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any zombie line exists")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(arguments)
    try:
        report = scan_vault(parsed.vault.expanduser(), parsed.max_age_days, parsed.today)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if parsed.json:
        print(json.dumps(report, ensure_ascii=False, indent=1), file=output)
    else:
        _print_report(report, output)
    return 1 if parsed.strict and report["zombie_lines"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
