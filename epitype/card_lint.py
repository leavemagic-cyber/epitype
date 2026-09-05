import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8；唯讀 CLI 不落 pyc。
"""Epitype 型別卡片 lint：卡片是表單，每種型別有必填欄位，缺了就不收。

型別由 frontmatter 與路徑依序推斷（decision_key → trigger → 事件卡目錄 → 待辦 →
metadata.type → feedback），必填欄位表在 memspec.CARD_REQUIRED_FIELDS 同源。FAIL 是
「這張卡不能算收下」；WARN 是「收下但有已知缺口」——第一版把既有 371 張缺別名的卡
留在 WARN，否則第一次跑就全紅、沒人看得完。只點名，不改卡。
"""

import argparse
from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time

try:
    from . import memsearch, memspec, scar_census
except ImportError:  # Direct script execution keeps the CLI contract.
    import memsearch
    import memspec
    import scar_census

MAX_CARD_BYTES = 256 * 1024
FAIL = "FAIL"
WARN = "WARN"
# 中文提問喚不回全英文卡（2026-08-13 鏡射裁定事故）；CJK 統一表意文字＋擴充 A＋相容區。
CJK_REGEX = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
# 平面欄位一律由 memspec.frontmatter_fields 供給（U38 同源），這裡只用同一條 key 形狀。
TOP_LEVEL_FIELD = memspec.TOP_LEVEL_FIELD


def _nested_and_lists(front_lines, path):
    """一層巢狀子欄位（"parent.child"）與序列欄位的項數。

    memspec.frontmatter_fields 只吐 top-level scalar，memsearch 只吐 aliases；
    型別判定需要 trigger.tool 與 metadata.type，必填判定需要「序列是否至少一項」。
    這裡不另解析界線或 scalar——界線用 memspec.split_frontmatter 的輸出、值用
    memspec.parse_scalar、flow mapping 用 scar_census 既有的那一份。
    flow 序列只判空／非空（規則只問 ≥1），不假裝數得出項數。
    """
    nested = {}
    counts = {}
    parent = None
    for number, raw_line in enumerate(front_lines, start=2):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        leading = raw_line[: len(raw_line) - len(raw_line.lstrip())]
        if "\t" in leading:
            parent = None
            continue
        if len(leading) == 0:
            parent = None
            match = TOP_LEVEL_FIELD.match(raw_line)
            if match is None:
                continue
            key, raw_value = match.groups()
            value = memspec.strip_inline_comment(raw_value).strip()
            if value in memspec.BLOCK_SCALAR_STYLES:
                continue  # 區塊純量的內縮文字不是子欄位
            if value.startswith("{") and value.endswith("}"):
                try:
                    children = scar_census._parse_flow_mapping(value, path, number)
                except Exception:
                    children = {}
                for child, child_value in children.items():
                    nested.setdefault(f"{key}.{child}", child_value)
                continue
            if value.startswith("[") and value.endswith("]"):
                counts[key] = 1 if value[1:-1].strip() else 0
                continue
            counts[key] = 1 if value else 0
            if not value:
                parent = key
            continue
        if parent is None:
            continue
        if stripped == "-" or stripped.startswith("- "):
            item, _problem = memspec.parse_scalar(stripped[1:])
            if item:
                counts[parent] = counts.get(parent, 0) + 1
            continue
        match = TOP_LEVEL_FIELD.match(stripped)
        if match is not None:
            key, raw_value = match.groups()
            value, _problem = memspec.parse_scalar(raw_value)
            nested.setdefault(f"{parent}.{key}", value)
    return nested, counts


def _card_type(relative, fields, nested):
    """順序判定；結構訊號優先於自報型別，自報的 metadata.type 才是最後手段。"""
    if memspec.DECISION_KEY_FIELD in fields:
        return memspec.CARD_TYPE_DECISION
    trigger_prefix = memspec.TRIGGER_FIELD + "."
    if memspec.TRIGGER_FIELD in fields or any(key.startswith(trigger_prefix) for key in nested):
        return memspec.CARD_TYPE_SCAR
    directories = relative.split("/")[:-1]
    for directory, card_type in memspec.EVENT_CARD_DIRECTORIES:
        if directory in directories:
            return card_type
    declared = nested.get(memspec.METADATA_TYPE_FIELD, "").strip().casefold()
    stem = relative.rsplit("/", 1)[-1]
    stem = stem[:-3] if stem.lower().endswith(".md") else stem
    if (
        declared == memspec.CARD_TYPE_PENDING
        or stem.startswith(memspec.PENDING_NAME_PREFIX)
        or fields.get(memspec.NAME_FIELD, "").startswith(memspec.PENDING_NAME_PREFIX)
    ):
        return memspec.CARD_TYPE_PENDING
    if declared in memspec.CARD_TYPES:
        return declared
    return memspec.DEFAULT_CARD_TYPE


def _has_value(field, fields, nested, counts):
    if field in memspec.CARD_LIST_FIELDS:
        return counts.get(field, 0) >= 1
    if "." in field:
        return bool(nested.get(field, "").strip())
    return bool(fields.get(field, "").strip())


def _expiry_warnings(fields, today):
    """到期欄位不綁型別：協定 §3.5 允許任何卡自己寫 expires_at。"""
    for field in memspec.CARD_EXPIRY_FIELDS:
        raw = fields.get(field, "").strip()
        if not raw:
            continue
        stamped = _as_date(raw)
        if stamped is not None and stamped < today:
            yield WARN, "expired", f"{field}={raw} 已過期（讀取端應視為失效，仍不刪只歸檔）"


def _as_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    match = memspec.PENDING_DATE_REGEX.match(value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _has_date(fields, nested):
    for field in memspec.CARD_DATE_FIELDS:
        source = nested if "." in field else fields
        if memspec.PENDING_DATE_REGEX.search(source.get(field, "")):
            return True
    for field in (memspec.NAME_FIELD, memspec.DESCRIPTION_FIELD):
        if memspec.PENDING_DATE_REGEX.search(fields.get(field, "")):
            return True
    return False


def _check_card(path, relative, today):
    """(型別, findings)。findings 是 (level, rule, reason) 的清單。"""
    fields, problem = memspec.frontmatter_fields(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return memspec.DEFAULT_CARD_TYPE, [(FAIL, "unreadable", f"{type(exc).__name__}")]
    front_lines, closing = memspec.split_frontmatter(text)

    findings = []
    if front_lines is None:
        findings.append((FAIL, "frontmatter", "沒有 frontmatter，型別與必填欄位無法判定"))
        return memspec.DEFAULT_CARD_TYPE, findings
    if closing is None:
        findings.append((FAIL, "frontmatter", "frontmatter 缺少結束界線"))
        return memspec.DEFAULT_CARD_TYPE, findings

    nested, counts = _nested_and_lists(front_lines, path)
    card_type = _card_type(relative, fields, nested)
    if problem:
        level = FAIL if "重複欄位" in problem else WARN
        findings.append((level, "frontmatter", problem))

    status = fields.get(memspec.DECISION_STATUS_FIELD, "").strip()
    if status and status not in memspec.DECISION_STATUS_VALUES:
        findings.append((
            FAIL,
            "status",
            f"status={status} 不在 {'|'.join(memspec.DECISION_STATUS_VALUES)}",
        ))

    for field in memspec.CARD_REQUIRED_FIELDS[card_type]:
        if not _has_value(field, fields, nested, counts):
            findings.append((FAIL, "required", f"缺必填欄位 {field}"))
    for field in memspec.CARD_OPTIONAL_FIELDS[card_type]:
        if field in memspec.CARD_LIST_FIELDS and field in counts and counts[field] == 0:
            findings.append((FAIL, "field-shape", f"{field} 是空序列；選填欄位寫了就要有內容"))

    if card_type == memspec.CARD_TYPE_DECISION:
        stamp = fields.get(memspec.CURRENT_DECISION_AT_FIELD, "").strip()
        if stamp and not memspec.is_iso_date(stamp):
            findings.append((FAIL, "date", f"{memspec.CURRENT_DECISION_AT_FIELD}={stamp} 不是 ISO 日期"))
        decider = fields.get(memspec.DECIDED_BY_FIELD, "").strip()
        if decider and decider not in memspec.DECIDED_BY_VALUES:
            findings.append((FAIL, "decided-by", f"decided_by={decider} 不在 {'|'.join(memspec.DECIDED_BY_VALUES)}"))
        if decider == memspec.OWNER_EXPLICIT_DECIDER and not _has_value(
            memspec.OWNER_QUOTE_FIELD, fields, nested, counts
        ):
            findings.append((FAIL, "required", f"decided_by=owner-explicit 缺 {memspec.OWNER_QUOTE_FIELD}"))
    elif card_type == memspec.CARD_TYPE_GRANT:
        if not _has_value(memspec.GRANT_EXPIRES_FIELD, fields, nested, counts):
            findings.append((WARN, "grant-no-expiry", f"缺 {memspec.GRANT_EXPIRES_FIELD}＝永久授權"))
    elif card_type in memspec.GENERIC_CARD_TYPES:
        if not _has_value(memspec.ALIASES_FIELD, fields, nested, counts):
            findings.append((WARN, "aliases", f"缺 {memspec.ALIASES_FIELD}（同義詞檢索空手）"))
        if not _has_date(fields, nested):
            findings.append((
                FAIL,
                "date",
                f"缺日期（{memspec.LAST_VERIFIED_AT_FIELD}／{memspec.METADATA_MODIFIED_FIELD}／name 或 description 內的 YYYY-MM-DD 任一）",
            ))
        reachable = fields.get(memspec.DESCRIPTION_FIELD, "") + "\n" + "\n".join(
            line.strip().lstrip("-").strip()
            for line in front_lines
            if line.strip().startswith("- ")
        )
        if not CJK_REGEX.search(reachable):
            findings.append((WARN, "no-chinese", "description 與 aliases 都沒有中文字，中文提問喚不回"))

    findings.extend(_expiry_warnings(fields, today))
    return card_type, findings


def scan_vault(vault, today=None, deadline=None):
    """唯讀掃描一個 vault；deadline 是 time.monotonic() 上限，逾時就標記並停手。"""
    vault = Path(vault).resolve()
    today = today or datetime.now(timezone.utc).date()
    cards = []
    by_type = {}
    oversized = 0
    timed_out = False
    for path in memsearch.card_files(vault):
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        try:
            if path.stat().st_size > MAX_CARD_BYTES:
                oversized += 1
                continue
        except OSError:
            continue
        relative = path.relative_to(vault).as_posix()
        try:
            card_type, findings = _check_card(path, relative, today)
        except Exception as exc:  # 一張壞卡不得讓整庫掃描停擺
            card_type, findings = memspec.DEFAULT_CARD_TYPE, [(FAIL, "unreadable", f"{type(exc).__name__}: {exc}")]
        by_type[card_type] = by_type.get(card_type, 0) + 1
        if findings:
            cards.append({
                "path": relative,
                "type": card_type,
                "fail": sum(1 for level, _rule, _reason in findings if level == FAIL),
                "warn": sum(1 for level, _rule, _reason in findings if level == WARN),
                "findings": [{"level": level, "rule": rule, "reason": reason} for level, rule, reason in findings],
            })
    cards.sort(key=lambda item: (-item["fail"], -item["warn"], item["path"]))
    return {
        "vault": str(vault),
        "total": sum(by_type.values()),
        "fail": sum(item["fail"] for item in cards),
        "warn": sum(item["warn"] for item in cards),
        "fail_cards": sum(1 for item in cards if item["fail"]),
        "by_type": {name: by_type[name] for name in memspec.CARD_TYPES if by_type.get(name)},
        "oversized_skipped": oversized,
        "timed_out": timed_out,
        "cards": cards,
    }


def summary_line(vaults, today=None, time_budget=memspec.CARD_LINT_HOOK_BUDGET_SECONDS):
    """SessionStart 的一行，沒有 FAIL 也沒有 WARN 就回 None。

    逾時回 None：一個掃不完的庫給出的數字是半個庫的數字，點名錯的數字比不點名更糟。
    """
    deadline = time.monotonic() + time_budget
    fail = warn = 0
    worst = None
    worst_fail = -1
    for vault in vaults:
        try:
            report = scan_vault(vault, today, deadline)
        except Exception:
            continue
        if report["timed_out"]:
            return None
        fail += report["fail"]
        warn += report["warn"]
        if report["fail"] > worst_fail:
            worst_fail, worst = report["fail"], report["vault"]
    if not (fail or warn) or worst is None:
        return None
    return memspec.CARD_LINT_NOTICE.format(fail=fail, warn=warn, vault=worst)


def _print_report(report, output):
    for card in report["cards"]:
        print(f"{card['path']} [{card['type']}]", file=output)
        for item in card["findings"]:
            print(f"  {item['level']} {item['rule']}: {item['reason']}", file=output)
    by_type = ",".join(f"{name}:{count}" for name, count in report["by_type"].items()) or "-"
    note = f" oversized={report['oversized_skipped']}" if report["oversized_skipped"] else ""
    note += " timed_out=1" if report["timed_out"] else ""
    print(
        f"CARDS total={report['total']} fail={report['fail']} warn={report['warn']} by_type={by_type}{note}",
        file=output,
    )


_FIXTURES = {
    "decision-bad.md": "---\nname: decision-bad\ndescription: 2026-09-01 壞決策卡\ndecision_key: k-bad\nstatus: draft\ncurrent_decision_at: 昨天\ndecided_by: owner-explicit\n---\nbody\n",
    "decision-good.md": "---\nname: decision-good\ndescription: 2026-09-01 好決策卡\ndecision_key: k-good\nstatus: active\ncurrent_decision_at: 2026-09-01\ndecided_by: three-way\naliases:\n  - 好決策\n  - good decision\nvalid_until: 2026-08-01\n---\nbody\n",
    "scar-bad.md": "---\nname: scar-bad\ndescription: 2026-09-01 壞傷疤卡\ntrigger:\n  tool: \"^(Bash)$\"\nmetadata:\n  type: scar\n---\nbody\n",
    "scar-good.md": "---\nname: scar-good\ndescription: 2026-09-01 好傷疤卡\ntrigger: {tool: \"^(Bash)$\", input: \"rm -rf\"}\nadvice: 改用 Write 落檔\nincident: 2026-09-02 三個 session 各踩一次\nvalid_until: 2026-08-01\n---\nbody\n",
    "grants/grant-ok.md": "---\nname: grant-ok\ndescription: owner grant auto-captured 2026-09-02: 你可以繼續\ncaptured_at: 2026-09-02T07:37:47Z\nsession_id: synthetic-session\n---\nbody\n",
    "grants/grant-bad.md": "---\nname: grant-bad\ndescription: owner grant auto-captured 2026-09-02: 缺會期\ncaptured_at: 2026-09-02T07:37:47Z\nexpires_at: 2026-09-03\n---\nbody\n",
    "corrections/correction-bad.md": "---\nname: correction-bad\ncaptured_at: 2026-09-02T07:37:47Z\nsession_id: synthetic-session\n---\nbody\n",
    "rulings/ruling-ok.md": "---\nname: ruling-ok\ndescription: owner ruling auto-captured 2026-09-02: 照舊\ncaptured_at: 2026-09-02T07:37:47Z\nsession_id: synthetic-session\n---\nbody\n",
    "pending-swsetup.md": "---\nname: pending-swsetup\ndescription: 2026-07-22 未辦（owner 自行）\nowner: owner\n---\nbody\n",
    "feedback-bad.md": "---\nname: feedback-bad\ndescription: english only description with no date\n---\nbody\n",
    "feedback-good.md": "---\nname: feedback-good\ndescription: 2026-09-01 中文摘要\naliases:\n  - 別名\nmetadata:\n  type: feedback\n---\nbody\n",
    "reference-dated.md": "---\nname: reference-dated\ndescription: english only reference card\nlast_verified_at: 2026-09-01\naliases:\n  - alias only in english\nmetadata:\n  type: reference\n---\nbody\n",
    "bom-crlf.md": "﻿---\r\nname: bom-crlf\r\ndescription: 2026-09-01 BOM 加 CRLF 的卡\r\naliases:\r\n  - 別名\r\nmetadata:\r\n  type: project\r\n---\r\nbody\r\n",
    "document-end.md": "---\nname: document-end\ndescription: 2026-09-01 以三點結尾的卡\naliases:\n  - 三點\n...\nbody\n",
    "no-boundary.md": "---\nname: no-boundary\ndescription: 2026-09-01 沒有結束界線\n",
    "duplicate-key.md": "---\nname: duplicate-key\ndescription: 2026-09-01 重複欄位\ndescription: 第二個\naliases:\n  - 重複\nmetadata:\n  type: user\n---\nbody\n",
    "habit-empty-aliases.md": "---\nname: habit-empty-aliases\ndescription: 2026-09-01 空別名序列\naliases: []\nmetadata:\n  type: habit\n---\nbody\n",
}


def _findings_of(report, path):
    card = next((item for item in report["cards"] if item["path"] == path), None)
    if card is None:
        return set(), card
    return {(item["level"], item["rule"]) for item in card["findings"]}, card


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-cardlint-") as temp_dir:
            vault = Path(temp_dir).resolve() / "vault"
            vault.mkdir()
            for name, text in _FIXTURES.items():
                target = vault / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text.encode("utf-8"))
            today = date(2026, 9, 6)
            report = scan_vault(vault, today=today)

            rules, card = _findings_of(report, "decision-bad.md")
            checks.append((
                "decision FAIL: status 值域外、日期非 ISO、owner-explicit 缺 owner_quote、缺 aliases",
                card is not None
                and card["type"] == memspec.CARD_TYPE_DECISION
                and {(FAIL, "status"), (FAIL, "date"), (FAIL, "required")} <= rules
                and card["warn"] == 0,
            ))
            rules, card = _findings_of(report, "decision-good.md")
            checks.append((
                "decision WARN only: 齊備但 valid_until 已過期",
                card is not None and rules == {(WARN, "expired")},
            ))

            rules, card = _findings_of(report, "scar-bad.md")
            checks.append((
                "scar FAIL: 缺 trigger.input、advice、incident",
                card is not None
                and card["type"] == memspec.CARD_TYPE_SCAR
                and rules == {(FAIL, "required")}
                and sum(1 for item in card["findings"] if item["level"] == FAIL) == 3,
            ))
            rules, card = _findings_of(report, "scar-good.md")
            checks.append((
                "scar WARN only: flow mapping 的 trigger 讀得到，只剩過期警告",
                card is not None and rules == {(WARN, "expired")},
            ))

            rules, card = _findings_of(report, "grants/grant-ok.md")
            checks.append((
                "事件卡不要求 aliases：grant 欄位齊備就只 WARN 無到期",
                card is not None
                and card["type"] == memspec.CARD_TYPE_GRANT
                and rules == {(WARN, "grant-no-expiry")},
            ))
            rules, card = _findings_of(report, "grants/grant-bad.md")
            checks.append((
                "grant FAIL 缺 session_id；已過期的 expires_at 是 WARN 不是 FAIL",
                card is not None and rules == {(FAIL, "required"), (WARN, "expired")},
            ))
            rules, card = _findings_of(report, "corrections/correction-bad.md")
            checks.append((
                "correction FAIL: 缺 description",
                card is not None and card["type"] == memspec.CARD_TYPE_CORRECTION and rules == {(FAIL, "required")},
            ))
            checks.append((
                "ruling 欄位齊備即無 finding，且不因缺 aliases 被點名",
                _findings_of(report, "rulings/ruling-ok.md")[1] is None
                and report["by_type"].get(memspec.CARD_TYPE_RULING) == 1,
            ))

            rules, card = _findings_of(report, "pending-swsetup.md")
            checks.append((
                "pending FAIL: name 前綴判型，缺 verify 與 exit",
                card is not None
                and card["type"] == memspec.CARD_TYPE_PENDING
                and rules == {(FAIL, "required")}
                and sum(1 for item in card["findings"] if item["level"] == FAIL) == 2,
            ))

            rules, card = _findings_of(report, "feedback-bad.md")
            checks.append((
                "generic FAIL 缺日期；缺 aliases 與全英文只 WARN",
                card is not None
                and card["type"] == memspec.CARD_TYPE_FEEDBACK
                and rules == {(FAIL, "date"), (WARN, "aliases"), (WARN, "no-chinese")},
            ))
            checks.append((
                "generic 齊備（日期＋中文別名）即無 finding",
                _findings_of(report, "feedback-good.md")[1] is None,
            ))
            rules, card = _findings_of(report, "reference-dated.md")
            checks.append((
                "generic WARN only: 有日期有別名但全英文",
                card is not None
                and card["type"] == memspec.CARD_TYPE_REFERENCE
                and rules == {(WARN, "no-chinese")},
            ))

            checks.append((
                "BOM＋CRLF 卡與 `...` 結尾卡都判成乾淨卡",
                _findings_of(report, "bom-crlf.md")[1] is None
                and _findings_of(report, "document-end.md")[1] is None,
            ))
            rules, card = _findings_of(report, "no-boundary.md")
            checks.append((
                "frontmatter 缺結束界線＝FAIL，且不再往下判型別",
                card is not None and rules == {(FAIL, "frontmatter")},
            ))
            rules, card = _findings_of(report, "duplicate-key.md")
            checks.append((
                "重複欄位＝FAIL",
                card is not None and (FAIL, "frontmatter") in rules,
            ))
            rules, card = _findings_of(report, "habit-empty-aliases.md")
            checks.append((
                "選填欄位寫成空序列＝FAIL（不是當成沒寫）",
                card is not None and (FAIL, "field-shape") in rules,
            ))

            checks.append((
                "摘要行帶總數、FAIL／WARN 與 by_type",
                report["total"] == len(_FIXTURES)
                and report["by_type"].get(memspec.CARD_TYPE_DECISION) == 2
                and report["by_type"].get(memspec.CARD_TYPE_SCAR) == 2
                and not report["timed_out"],
            ))

            line = summary_line([vault], today=today)
            checks.append((
                "SessionStart 一行帶 FAIL／WARN 數與最壞的庫",
                isinstance(line, str)
                and "\n" not in line
                and f"FAIL {report['fail']}／WARN {report['warn']}" in line
                and str(vault) in line,
            ))
            checks.append((
                "乾淨庫沒有那一行；逾時也沒有（半個庫的數字不點名）",
                summary_line([vault / "grants" / "nowhere"], today=today) is None
                and summary_line([vault], today=today, time_budget=-1.0) is None,
            ))

            out = io.StringIO()
            code = main(["--strict", "--today", "2026-09-06", os.fspath(vault)], output=out)
            text = out.getvalue()
            checks.append((
                "strict CLI exit 1 並印出摘要行與逐卡 finding",
                code == 1
                and f"CARDS total={len(_FIXTURES)} fail={report['fail']} warn={report['warn']} by_type=" in text
                and "decision-bad.md [decision]" in text
                and "FAIL status:" in text,
            ))
            out = io.StringIO()
            code = main(["--json", "--today", "2026-09-06", os.fspath(vault)], output=out)
            checks.append((
                "非 strict 回 0；--json 是可解析的 JSON",
                code == 0 and json.loads(out.getvalue())["total"] == len(_FIXTURES),
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 21
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
    parser.add_argument("--today", type=date.fromisoformat, default=None, help="ISO date override for reproducible runs")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any card FAILs")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(arguments)
    try:
        report = scan_vault(parsed.vault.expanduser(), parsed.today)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if parsed.json:
        print(json.dumps(report, ensure_ascii=False, indent=1), file=output)
    else:
        _print_report(report, output)
    return 1 if parsed.strict and report["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
