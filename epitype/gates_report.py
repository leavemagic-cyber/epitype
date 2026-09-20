import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""把治理 vault 的 `_GATE_LOG.jsonl` 整理成人看得懂的擋下報告。

三種阻擋 kind：動作閘 deny（PreToolUse 卡片命中，日誌行本身沒有 kind 欄）、
stop_block（Stop 閘）、write_block（寫檔閘）。`parse_defect` 是卡片解析失敗，
不是一次阻擋，另外列一行不進 by_label/by_day/by_session/repeat_offenders。
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import tempfile

GATE_LOG_FILENAME = "_GATE_LOG.jsonl"
# stop_note（只提醒、沒擋）刻意不在這裡：它沒有擋下任何東西，算進來會把「擋了幾次」灌水。
# 它照樣出現在「其他種類」那一段，而「從沒響過」看的是所有種類的列，不會因此誤報。
BLOCKING_KINDS = ("deny", "stop_block", "write_block")
BY_CHOICES = ("kind", "decision", "session", "day")
_SECTION_FOR_BY = {"kind": "by_kind", "decision": "by_label", "session": "by_session", "day": "by_day"}
_RELATIVE_SINCE = re.compile(r"^(\d+)d$")
REPEAT_OFFENDER_THRESHOLD = 3
EXPECTED_SELFTESTS = 9


@dataclass(frozen=True)
class Row:
    timestamp: datetime
    kind: str
    rule: str | None
    label: str | None
    session_id: str | None
    reason: str | None
    digest: str | None = None


def parse_since(value, now):
    if value is None:
        return None
    relative = _RELATIVE_SINCE.match(value)
    if relative:
        return now - timedelta(days=int(relative.group(1)))
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"--since 看不懂：{value!r}（要 Nd 或 YYYY-MM-DD）") from None
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


def _parse_timestamp(raw):
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _row_label(record):
    for key in ("decision", "card_path", "card", "filename"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def load_rows(path):
    """回傳 (rows, bad_line_count)。檔案不存在或是空檔一律回傳空結果，不丟例外。"""
    if not path.is_file():
        return [], 0
    rows = []
    bad = 0
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(record, dict):
                bad += 1
                continue
            timestamp = _parse_timestamp(record.get("timestamp"))
            if timestamp is None:
                bad += 1
                continue
            kind = record.get("kind")
            if not isinstance(kind, str) or not kind:
                kind = "deny"
            session_id = record.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                session_id = None
            reason = record.get("reason")
            if not isinstance(reason, str):
                reason = None
            rows.append(
                Row(
                    timestamp=timestamp,
                    kind=kind,
                    rule=record.get("rule") if isinstance(record.get("rule"), str) else None,
                    label=_row_label(record),
                    session_id=session_id,
                    reason=reason,
                    digest=record.get("digest") if isinstance(record.get("digest"), str) else None,
                )
            )
    return rows, bad


def build_report(rows, since=None, top_labels=10, top_sessions=5, recent_per_kind=3):
    kept = [row for row in rows if since is None or row.timestamp >= since]
    blocking = [row for row in kept if row.kind in BLOCKING_KINDS]

    by_kind = Counter(row.kind for row in kept)
    parse_defect_count = by_kind.get("parse_defect", 0)
    other_kinds = {
        kind: count for kind, count in by_kind.items() if kind not in BLOCKING_KINDS and kind != "parse_defect"
    }

    label_counts = Counter()
    label_last_seen = {}
    for row in blocking:
        if row.label is None:
            continue
        label_counts[row.label] += 1
        if row.label not in label_last_seen or row.timestamp > label_last_seen[row.label]:
            label_last_seen[row.label] = row.timestamp
    by_label = [
        {"label": label, "count": count, "last_seen": label_last_seen[label].isoformat()}
        for label, count in sorted(label_counts.items(), key=lambda item: (-item[1], item[0]))[:top_labels]
    ]

    by_day = Counter(row.timestamp.astimezone(timezone.utc).date().isoformat() for row in blocking)

    session_counts = Counter()
    session_last_seen = {}
    for row in blocking:
        if row.session_id is None:
            continue
        session_counts[row.session_id] += 1
        if row.session_id not in session_last_seen or row.timestamp > session_last_seen[row.session_id]:
            session_last_seen[row.session_id] = row.timestamp
    by_session = [
        {"session_id": session_id, "count": count, "last_seen": session_last_seen[session_id].isoformat()}
        for session_id, count in sorted(session_counts.items(), key=lambda item: (-item[1], item[0]))[:top_sessions]
    ]

    recent_by_kind = {}
    for kind in sorted(by_kind):
        matching = sorted((row for row in kept if row.kind == kind), key=lambda row: row.timestamp, reverse=True)
        recent_by_kind[kind] = [
            {
                "timestamp": row.timestamp.isoformat(),
                "kind": row.kind,
                "rule": row.rule,
                "label": row.label,
                "reason": row.reason,
            }
            for row in matching[:recent_per_kind]
        ]

    pair_counts = Counter()
    pair_last_seen = {}
    for row in blocking:
        if row.session_id is None or row.label is None:
            continue
        key = (row.session_id, row.label)
        pair_counts[key] += 1
        if key not in pair_last_seen or row.timestamp > pair_last_seen[key]:
            pair_last_seen[key] = row.timestamp
    repeat_offenders = [
        {
            "session_id": session_id,
            "label": label,
            "count": count,
            "last_seen": pair_last_seen[(session_id, label)].isoformat(),
        }
        for (session_id, label), count in sorted(pair_counts.items(), key=lambda item: (-item[1], item[0]))
        if count >= REPEAT_OFFENDER_THRESHOLD
    ]
    sessions_seen = len(session_counts) > 0

    return {
        "total_rows": len(kept),
        "total_blocked": len(blocking),
        "parse_defect_count": parse_defect_count,
        "other_kinds": other_kinds,
        "by_kind": {kind: by_kind.get(kind, 0) for kind in BLOCKING_KINDS},
        "by_label": by_label,
        "by_day": dict(sorted(by_day.items())),
        "by_session": by_session,
        "sessions_seen": sessions_seen,
        "recent_by_kind": recent_by_kind,
        "repeat_offenders": repeat_offenders,
        "bad_line_count": None,  # filled by caller, which knows load_rows' bad count
    }


def format_report(report, by, since_arg):
    lines = []
    lines.append(f"期間：{since_arg or '（全部）'}")
    lines.append(f"期間內總擋下數：{report['total_blocked']}（deny+stop_block+write_block）")
    lines.append(f"parse_defect（卡片解析失敗，非阻擋事件）：{report['parse_defect_count']} 筆")
    if report["other_kinds"]:
        extra = "、".join(f"{kind}={count}" for kind, count in sorted(report["other_kinds"].items()))
        lines.append(f"未歸類 kind：{extra}")
    lines.append(f"日誌壞行數：{report['bad_line_count']}")

    section_order = ["kind", "decision", "day", "session"]
    section_order.remove(by)
    section_order.insert(0, by)

    for section in section_order:
        if section == "kind":
            lines.append("")
            lines.append("依 kind 分：")
            for kind in BLOCKING_KINDS:
                lines.append(f"  {kind}: {report['by_kind'][kind]}")
        elif section == "decision":
            lines.append("")
            lines.append("依決策卡／傷疤卡分（前 10）：")
            if not report["by_label"]:
                lines.append("  （無）")
            for entry in report["by_label"]:
                lines.append(f"  {entry['label']}: {entry['count']} 筆，最近一次 {entry['last_seen']}")
        elif section == "day":
            lines.append("")
            lines.append("依天分：")
            if not report["by_day"]:
                lines.append("  （無）")
            for day, count in report["by_day"].items():
                lines.append(f"  {day}: {count}")
        elif section == "session":
            lines.append("")
            lines.append("依 session 分（前 5）：")
            if not report["sessions_seen"]:
                lines.append("  （日誌沒有任何 session_id 欄位，無法分 session）")
            else:
                for entry in report["by_session"]:
                    lines.append(f"  {entry['session_id']}: {entry['count']} 筆，最近一次 {entry['last_seen']}")

    lines.append("")
    lines.append("每類各列最近 3 筆：")
    for kind, recent in report["recent_by_kind"].items():
        lines.append(f"  [{kind}]")
        for entry in recent:
            hit = entry["label"] or entry["rule"] or "-"
            detail = f"  {entry['timestamp']} | rule={entry['rule'] or '-'} | 命中={hit}"
            if entry["reason"]:
                detail += f" | reason={entry['reason']}"
            lines.append(detail)

    lines.append("")
    lines.append("判讀提示（同一 session 同一卡連擋 ≥3 次＝疑似誤擋，人工標 TP／FP）：")
    if not report["repeat_offenders"]:
        lines.append("  （無）")
    for entry in report["repeat_offenders"]:
        lines.append(
            f"  session={entry['session_id']} label={entry['label']} count={entry['count']} 最近一次={entry['last_seen']}"
        )
    return "\n".join(lines)


def run_selftest():
    checks = []
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    def synth(offset_hours, kind=None, rule=None, session_id=None, label_field=None, reason=None):
        record = {"timestamp": (now - timedelta(hours=offset_hours)).isoformat()}
        if kind is not None:
            record["kind"] = kind
        if rule is not None:
            record["rule"] = rule
        if session_id is not None:
            record["session_id"] = session_id
        if label_field is not None:
            record.update(label_field)
        if reason is not None:
            record["reason"] = reason
        return json.dumps(record, ensure_ascii=False)

    try:
        with tempfile.TemporaryDirectory(prefix="gates_report_") as temp_dir:
            vault = Path(temp_dir).resolve()
            log_path = vault / GATE_LOG_FILENAME
            fake_lines = [
                synth(1, label_field={"card": "scar-a"}),
                synth(2, label_field={"card": "scar-a"}),
                synth(3, kind="stop_block", rule="forbidden", label_field={"decision": "dec-x"}),
                synth(4, kind="write_block", rule="forbidden", label_field={"decision": "dec-x"}),
                synth(5, kind="parse_defect", label_field={"filename": "bad.md"}, reason="PatternError: boom"),
                synth(30, label_field={"card": "scar-b"}),  # previous UTC day
                "{這一行故意不是合法 JSON",
            ]
            log_path.write_text("\n".join(fake_lines) + "\n", encoding="utf-8")

            rows, bad = load_rows(log_path)
            checks.append(("解析出 6 筆合法列，1 筆壞行", len(rows) == 6 and bad == 1))

            report_all = build_report(rows)
            report_all["bad_line_count"] = bad
            checks.append((
                "三種 kind 計數：deny=3, stop_block=1, write_block=1",
                report_all["by_kind"] == {"deny": 3, "stop_block": 1, "write_block": 1},
            ))
            checks.append(("parse_defect 另計，不算進 total_blocked", report_all["parse_defect_count"] == 1 and report_all["total_blocked"] == 5))

            since_path = vault / "since_check.jsonl"
            since_path.write_text(
                "\n".join((synth(1, label_field={"card": "recent-scar"}), synth(50 * 24, label_field={"card": "old-scar"}))) + "\n",
                encoding="utf-8",
            )
            rows_since, _ = load_rows(since_path)
            report_since = build_report(rows_since, since=parse_since("2d", now))
            checks.append((
                "--since 2d 留下 recent-scar、濾掉 50 天前那筆 old-scar",
                {e["label"] for e in report_since["by_label"]} == {"recent-scar"},
            ))

            checks.append(("--since YYYY-MM-DD 可解析", parse_since("2026-01-01", now) == datetime(2026, 1, 1, tzinfo=timezone.utc)))

            expected_days = {
                (now - timedelta(hours=1)).astimezone(timezone.utc).date().isoformat(),
                (now - timedelta(hours=30)).astimezone(timezone.utc).date().isoformat(),
            }
            checks.append((
                "--by day 依天分鍵值與筆數正確",
                set(report_all["by_day"]) == expected_days
                and report_all["by_day"][(now - timedelta(hours=1)).astimezone(timezone.utc).date().isoformat()] == 4
                and report_all["by_day"][(now - timedelta(hours=30)).astimezone(timezone.utc).date().isoformat()] == 1,
            ))

            json_text = json.dumps(report_all, ensure_ascii=False)
            round_tripped = json.loads(json_text)
            checks.append(("--json 輸出可解析且數字一致", round_tripped["total_blocked"] == 5 and round_tripped["by_kind"]["deny"] == 3))

            missing_report, missing_bad = load_rows(vault / "nope" / GATE_LOG_FILENAME)
            empty_report = build_report(missing_report)
            checks.append(("不存在檔印零不炸", missing_report == [] and missing_bad == 0 and empty_report["total_blocked"] == 0))

        with tempfile.TemporaryDirectory(prefix="gates_report_mix_") as temp_dir2:
            mix_vault = Path(temp_dir2).resolve()
            mix_path = mix_vault / GATE_LOG_FILENAME
            mix_lines = [
                synth(1, kind="write_block", rule="forbidden", session_id="claude-session-alpha", label_field={"decision": "dec-y"}),
                synth(2, kind="write_block", rule="forbidden", session_id="claude-session-alpha", label_field={"decision": "dec-y"}),
                synth(3, kind="write_block", rule="forbidden", session_id="claude-session-alpha", label_field={"decision": "dec-y"}),
                synth(4, kind="stop_block", rule="forbidden", session_id="stopgate-abcdef01", label_field={"decision": "dec-z"}),
                synth(5, kind="stop_block", rule="forbidden", session_id="stopgate-abcdef01", label_field={"decision": "dec-z"}),
                synth(6, label_field={"card": "scar-a"}),  # no session_id at all
            ]
            mix_path.write_text("\n".join(mix_lines) + "\n", encoding="utf-8")
            mix_rows, _ = load_rows(mix_path)
            mix_report = build_report(mix_rows)
            checks.append((
                "混合 host session_id 形狀不同：連擋 >=3 偵測，不同形狀各自計數",
                any(
                    entry["session_id"] == "claude-session-alpha" and entry["count"] == 3
                    for entry in mix_report["repeat_offenders"]
                )
                and all(entry["session_id"] != "stopgate-abcdef01" for entry in mix_report["repeat_offenders"])
                and mix_report["sessions_seen"] is True,
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    status = "PASS" if passed == EXPECTED_SELFTESTS and len(checks) == EXPECTED_SELFTESTS else "FAIL"
    print(f"SELFTEST {status} {passed}/{EXPECTED_SELFTESTS}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def never_fired_lines(vault, rows):
    """武裝了卻從來沒擋過任何東西的規則。**只報不動。**

    為什麼不自動退役：判準分不出「這條規則太窄／情境還沒發生」與「這條規則寫壞了」，
    而 2026-09-17 實跑過自動降級，一條 6 次命中、6 次全擋下、0 漏擋的規則被關了 14 天。
    沒有可靠的成效訊號之前，用數字自動關掉規則就是在重犯同一個錯——所以這裡只列名單，
    讓人自己判。

    從來沒擋過不等於沒用：有些規則存在就是為了讓那件事不要發生。名單是線索，不是判決。
    """
    try:
        from . import compliance
    except ImportError:  # 直接當腳本跑
        import compliance

    fired = {str(getattr(row, "label", "") or "").strip() for row in rows}
    fired.discard("")
    lines = []
    try:
        rules = compliance.armed_rules(vault)
    except Exception as exc:
        return ["讀不到這個庫的武裝規則：%s: %s" % (type(exc).__name__, exc)]
    seen = {}
    for rule in rules:
        seen.setdefault(rule.card, set()).add(rule.kind)
    quiet = sorted(name for name in seen if name not in fired)
    lines.append("武裝規則 %d 條，其中 %d 條從來沒擋過任何東西：" % (len(seen), len(quiet)))
    for name in quiet:
        lines.append("  %s（%s）" % (name, "、".join(sorted(seen[name]))))
    lines.append("只報不動：從來沒擋過不等於沒用，有些規則存在就是為了讓那件事不要發生。")
    return lines


def main(argv=None, output=sys.stdout):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return run_selftest()

    parser = argparse.ArgumentParser(description="治理 vault _GATE_LOG.jsonl 的擋下報告。")
    parser.add_argument("vault", type=Path, help="vault 目錄，或直接指向 _GATE_LOG.jsonl 的路徑")
    parser.add_argument("--since", default=None, help="Nd（例：2d）或 YYYY-MM-DD")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--by", choices=BY_CHOICES, default="kind")
    parser.add_argument("--never-fired", action="store_true",
                        help="列出武裝了卻從來沒擋過任何東西的規則（退役候選，只報不動）")
    parser.add_argument("--selftest", action="store_true")
    parsed = parser.parse_args(arguments)

    if parsed.selftest:
        parser.error("--selftest 不可同時指定 vault")

    target = parsed.vault.expanduser()
    log_path = target if target.suffix == ".jsonl" else target / GATE_LOG_FILENAME

    now = datetime.now(timezone.utc)
    try:
        since = parse_since(parsed.since, now)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    rows, bad = load_rows(log_path)
    report = build_report(rows, since=since)
    report["bad_line_count"] = bad

    if parsed.never_fired:
        vault = log_path.parent if log_path.suffix == ".jsonl" else target
        for line in never_fired_lines(vault, rows):
            print(line, file=output)
        return 0

    if parsed.json:
        print(json.dumps(report, ensure_ascii=False, indent=1), file=output)
    else:
        print(format_report(report, parsed.by, parsed.since), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
