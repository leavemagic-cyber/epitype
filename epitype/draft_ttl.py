# -*- coding: utf-8 -*-
"""草稿有時限：放著沒人用就自己過期，不必有人去審。

為什麼要這樣改：自動捕捉每天都在產草稿，而「等人審」的隊伍只會長不會短——2026-09-19
盤點時 45 份放超過七天，而且那個數字每天都在往上。owner 的原話是「如果正常運行 epitype，
還會一直有草稿積壓，那就會太浪費 token，而且對其他使用者很不人性化」。

一個永遠審不完的隊伍，實際效果等於沒有人在審；差別只在它每天還要佔掉報表一行、佔掉
一個人的注意力。所以隊伍改成有時限的暫存：七天之內沒有被用到就自動標成過期。

三件事刻意這樣定：

1. **檔案不刪**，只在 frontmatter 寫上過期與理由。協定允許歸檔、不允許刪除；而且真的
   有用的那一份，之後要撈回來得撈得到。
2. **只過期自動捕捉的草稿**。人手寫的提案是有人刻意放進來的，不該被時間吃掉。
3. **過期是可逆的**：把 `triaged` 改回空的，它就回到待審。
"""

import argparse
import io
import sys
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from . import memspec
except ImportError:  # 直接當腳本跑
    import memspec

DRAFT_DIRNAME = "_drafts"


def _as_date(value):
    text = str(value or "").strip()[:10]
    if len(text) != 10:
        return None
    try:
        return date(int(text[:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def _age_days(fields, path, today):
    """這份草稿幾天大了。日期取 frontmatter，沒有才退回檔案時間。"""
    for field in memspec.DRAFT_AGE_FIELDS:
        stamped = _as_date(fields.get(field))
        if stamped is not None:
            return (today - stamped).days
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
    except OSError:
        return 0
    return (today - modified).days


def _is_candidate(fields):
    """待審、而且是自動捕捉來的。"""
    triaged = str(fields.get(memspec.DRAFT_TRIAGED_FIELD) or "").strip().lower()
    if triaged and triaged not in memspec.DRAFT_TRIAGED_PENDING_VALUES:
        return False
    provenance = str(fields.get(memspec.PROVENANCE_FIELD) or "").strip().lower()
    return provenance in memspec.DRAFT_AUTO_PROVENANCE


def _stamp(path, today):
    """把過期欄位寫進 frontmatter。已經有 triaged 的不動。"""
    try:
        text = io.open(path, encoding="utf-8").read()
    except OSError:
        return False
    lines = text.split("\n")
    if not lines or lines[0].strip() != memspec.FRONTMATTER_BOUNDARY:
        return False
    try:
        closing = lines.index(memspec.FRONTMATTER_BOUNDARY, 1)
    except ValueError:
        return False
    block = [
        "%s: %s" % (memspec.DRAFT_TRIAGED_FIELD, memspec.DRAFT_TRIAGED_EXPIRED),
        "%s: %s" % (memspec.DRAFT_TRIAGED_AT_FIELD, today.isoformat()),
        "%s: %s" % (memspec.DRAFT_TRIAGED_BY_FIELD, memspec.DRAFT_TTL_ACTOR),
        "%s: %s" % (memspec.DRAFT_TRIAGE_NOTE_FIELD, memspec.DRAFT_TTL_NOTE.format(
            days=memspec.DRAFT_TTL_DAYS)),
    ]
    # 同名欄位先拿掉（hold 改成過期時會有），免得一張卡出現兩個 triaged。
    head = [line for line in lines[1:closing]
            if not any(line.startswith(name + ":") for name in (
                memspec.DRAFT_TRIAGED_FIELD, memspec.DRAFT_TRIAGED_AT_FIELD,
                memspec.DRAFT_TRIAGED_BY_FIELD, memspec.DRAFT_TRIAGE_NOTE_FIELD))]
    rebuilt = [lines[0]] + head + block + lines[closing:]
    try:
        io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(rebuilt))
    except OSError:
        return False
    return True


def expire_stale(vault, today=None, days=None, apply=False):
    """把放太久沒人用的自動草稿標成過期。回傳報告 dict。"""
    vault = Path(vault)
    today = today or datetime.now(timezone.utc).date()
    days = memspec.DRAFT_TTL_DAYS if days is None else days
    root = vault / DRAFT_DIRNAME
    report = {"vault": str(vault), "days": days, "expired": 0, "kept": 0,
              "examples": [], "applied": bool(apply)}
    if not root.is_dir():
        return report
    for path in sorted(root.rglob("*.md")):
        try:
            fields, _problem = memspec.frontmatter_fields(path)
        except (OSError, UnicodeError):
            continue
        if not _is_candidate(fields):
            continue
        age = _age_days(fields, path, today)
        if age < days:
            report["kept"] += 1
            continue
        if apply and not _stamp(path, today):
            continue
        report["expired"] += 1
        if len(report["examples"]) < memspec.DRAFT_TTL_EXAMPLE_LIMIT:
            report["examples"].append({
                "path": path.relative_to(vault).as_posix(),
                "age_days": age,
                "description": str(fields.get(memspec.DESCRIPTION_FIELD) or "")[:120],
            })
    return report


def find(vault, needle, limit=None):
    """在草稿裡找一個詞。過期不等於消失——要讓它「不用審」，就得讓它「還找得到」。

    不走全文索引：索引刻意跳過底線開頭的目錄（那是隱私邊界），而草稿正住在那裡。
    草稿是純文字，直接掃就好，反正只有人在問的時候才跑。"""
    vault = Path(vault)
    root = vault / DRAFT_DIRNAME
    limit = memspec.DRAFT_FIND_LIMIT if limit is None else limit
    hits = []
    if not root.is_dir() or not str(needle or "").strip():
        return hits
    folded = str(needle).casefold()
    for path in sorted(root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if folded not in text.casefold():
            continue
        fields, _problem = memspec.frontmatter_fields(path)
        hits.append({
            "path": path.relative_to(vault).as_posix(),
            "triaged": str(fields.get(memspec.DRAFT_TRIAGED_FIELD) or "").strip() or "(待審)",
            "description": str(fields.get(memspec.DESCRIPTION_FIELD) or "")[:160],
        })
        if len(hits) >= limit:
            break
    return hits


def _selftest():
    import tempfile

    checks = []
    today = date(2026, 9, 19)

    def vault_with(text, name="captured_pending/20260901/c.md"):
        root = Path(tempfile.mkdtemp(prefix="epitype-ttl-"))
        target = root / DRAFT_DIRNAME / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return root, target

    old_capture = ("---\nname: c\ndescription: owner correction auto-captured\n"
                   "captured_at: 2026-09-01T10:00:00Z\nprovenance: auto-captured\n"
                   "verified: false\n---\nbody\n")
    root, target = vault_with(old_capture)
    report = expire_stale(root, today=today, apply=True)
    fields, _ = memspec.frontmatter_fields(target)
    checks.append(("放超過七天的自動草稿會過期",
                   report["expired"] == 1
                   and fields.get(memspec.DRAFT_TRIAGED_FIELD) == memspec.DRAFT_TRIAGED_EXPIRED,
                   report))

    fresh = old_capture.replace("2026-09-01", "2026-09-18")
    root, _target = vault_with(fresh)
    report = expire_stale(root, today=today, apply=True)
    checks.append(("還沒滿七天的不動", report["expired"] == 0 and report["kept"] == 1, report))

    handwritten = old_capture.replace("provenance: auto-captured\n", "")
    root, target = vault_with(handwritten)
    report = expire_stale(root, today=today, apply=True)
    fields, _ = memspec.frontmatter_fields(target)
    checks.append(("人手寫的提案不會被時間吃掉",
                   report["expired"] == 0 and not fields.get(memspec.DRAFT_TRIAGED_FIELD),
                   report))

    judged = old_capture.replace("verified: false\n", "verified: false\ntriaged: retired\n")
    root, _target = vault_with(judged)
    report = expire_stale(root, today=today, apply=True)
    checks.append(("已經判過的不再碰", report["expired"] == 0, report))

    held = old_capture.replace("verified: false\n", "verified: false\ntriaged: hold\n")
    root, target = vault_with(held)
    report = expire_stale(root, today=today, apply=True)
    text = target.read_text(encoding="utf-8")
    checks.append(("hold 也會到期，而且只留一個 triaged 欄位",
                   report["expired"] == 1 and text.count("triaged:") == 1, report))

    root, target = vault_with(old_capture)
    report = expire_stale(root, today=today, apply=False)
    fields, _ = memspec.frontmatter_fields(target)
    checks.append(("只算不改的那一種真的沒改到檔案",
                   report["expired"] == 1 and not fields.get(memspec.DRAFT_TRIAGED_FIELD),
                   report))

    root, target = vault_with(old_capture)
    expire_stale(root, today=today, apply=True)
    body = target.read_text(encoding="utf-8")
    checks.append(("原文照留、只是多了幾個欄位", "body" in body and "auto-captured" in body, None))

    # 過期了還要找得到——不然「不用審」等於「丟掉」。
    hits = find(root, "auto-captured")
    checks.append(("過期的草稿仍然搜得到，而且看得出它已經過期",
                   len(hits) == 1 and hits[0]["triaged"] == memspec.DRAFT_TRIAGED_EXPIRED,
                   hits))
    checks.append(("找不到的詞就回空", find(root, "這個詞不存在於任何草稿") == [], None))

    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        if not ok:
            print("FAIL", name, detail)
    print("SELFTEST %s %d/%d" % ("PASS" if passed == len(checks) else "FAIL",
                                 passed, len(checks)))
    return 0 if passed == len(checks) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="草稿到期：放太久沒人用的自動草稿標成過期")
    parser.add_argument("vaults", nargs="*")
    parser.add_argument("--days", type=int, default=memspec.DRAFT_TTL_DAYS)
    parser.add_argument("--apply", action="store_true", help="真的寫進檔案（預設只算不改）")
    parser.add_argument("--find", metavar="詞", help="在草稿裡找一個詞（過期的也找得到）")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.vaults:
        print("要給至少一個記憶庫路徑", file=sys.stderr)
        return 2
    code = 0
    for raw in args.vaults:
        vault = Path(raw)
        if not vault.is_dir():
            print("找不到這個記憶庫：%s" % raw, file=sys.stderr)
            code = 2
            continue
        if args.find:
            hits = find(vault, args.find)
            for item in hits:
                print("  [%s] %s\n      %s" % (item["triaged"], item["path"], item["description"]))
            print("DRAFT_FIND vault=%s needle=%s hits=%d" % (vault.name, args.find, len(hits)))
            continue
        report = expire_stale(vault, days=args.days, apply=args.apply)
        for item in report["examples"]:
            print("  %s（%d 天）%s" % (item["path"], item["age_days"], item["description"]))
        print("DRAFT_TTL vault=%s expired=%d kept=%d days=%d applied=%s"
              % (vault.name, report["expired"], report["kept"], report["days"],
                 report["applied"]))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
