import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8；唯讀/寫入 CLI 不落 pyc。
"""Epitype 目錄生成器：把卡片欄位機械攤成兩份可瀏覽的目錄。

三個閱讀層級（2026-09-09 Claude↔Codex 收斂）：`MEMORY.md` 是**純手寫**短入口、
`_views/current.md` 是現用卡、`_views/history/closed.md` 是已結案／已取代。本模組
只寫後兩份——生成器碰 `MEMORY.md` 就會加入它的多寫者競爭（原生自動記憶也在追加），
而「甲讀舊檔→乙追加→甲依舊檔重寫」正是區塊標記擋不住的那種遺失。

分層＝改一個欄位，不搬檔案：卡片正本永遠留在原處，連結才穩定。狀態按型別解讀
（`_state_of`）；`closed` 只影響目錄位置，喚回照樣搜得到（memsearch 只排除
`superseded` 並轉向繼任卡）。掃描範圍與型別判定都不自己造一套：範圍用
`memsearch.scan_cards`，型別用 `card_lint.card_type_of`，否則同一張卡會在目錄裡
分到 A 區、在 lint 裡按 B 型檢查。純 stdlib、不叫模型、不含任何行為守則文字。
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

try:
    from . import card_lint, memsearch, memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import card_lint
    import memsearch
    import memspec

# 指紋只在同一個生成格式內比較才有意義：改了版面卻沿用舊指紋＝永遠不重寫。
FORMAT_VERSION = "1"
DEFAULT_LOCK_TIMEOUT = 5.0

STATE_CURRENT = "current"
STATE_CLOSED = "closed"
STATE_REVIEW = "review"

# 需要狀態才算「已確認現用」的型別。缺欄位的卡不得以「不是 closed」冒充現用
# （收斂第 3 條）——它進待複查段，由收尾的人／AI 依證據補欄位。
STATE_REQUIRED_TYPES = (memspec.CARD_TYPE_DECISION, memspec.CARD_TYPE_PROJECT)

# markdown 連結只有這幾個字元會把目標吃掉；其餘（含中文檔名）保持原樣才讀得懂。
_LINK_ESCAPES = {" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E"}


def _state_of(card_type, fields):
    status = fields.get(memspec.DECISION_STATUS_FIELD, "").strip()
    if status == memspec.SUPERSEDED_DECISION_STATUS:
        return STATE_CLOSED
    if card_type == memspec.CARD_TYPE_PROJECT and status == memspec.CLOSED_CARD_STATUS:
        return STATE_CLOSED
    if card_type in STATE_REQUIRED_TYPES and status != memspec.ACTIVE_DECISION_STATUS:
        return STATE_REVIEW
    return STATE_CURRENT


def _text(value, limit=None):
    cleaned = " ".join(str(value or "").split())
    if limit is not None and len(cleaned) > limit:
        return cleaned[:limit] + memspec.VIEWS_ELLIPSIS
    return cleaned


def _label(fields, relative):
    # `]` 在連結文字裡會提早關閉標籤，整行連結就斷了；卡名本身不動，只動這一份顯示。
    name = _text(fields.get(memspec.NAME_FIELD, "")) or Path(relative).stem
    return name.replace("[", "(").replace("]", ")")


def _link(relative, depth):
    target = "".join(_LINK_ESCAPES.get(character, character) for character in relative)
    return "../" * depth + target


def _note_of(card_type, fields):
    status = fields.get(memspec.DECISION_STATUS_FIELD, "").strip()
    if status == memspec.SUPERSEDED_DECISION_STATUS:
        return memspec.VIEWS_SUPERSEDED_NOTE.format(
            target=_text(fields.get(memspec.SUPERSEDED_BY_FIELD, "")) or memspec.VIEWS_MISSING
        )
    if card_type == memspec.CARD_TYPE_PROJECT and status == memspec.CLOSED_CARD_STATUS:
        return memspec.VIEWS_CLOSED_NOTE.format(
            stamp=_text(fields.get(memspec.CLOSED_AT_FIELD, "")) or memspec.VIEWS_MISSING,
            who=_text(fields.get(memspec.CLOSED_BY_FIELD, "")) or memspec.VIEWS_MISSING,
        )
    return ""


def collect(vault):
    """(解析後的庫, 納管卡的分類結果, 輸入指紋)。

    指紋按掃描順序吃相對路徑＋mtime＋大小：卡沒動就不重寫，重寫只是把 generated
    時間換掉，卻讓每個讀者的 diff 都變髒。
    """
    vault = Path(vault).resolve()
    if not vault.is_dir():
        raise NotADirectoryError(f"vault path is not a directory: {vault}")
    digest = hashlib.sha256()
    digest.update(f"{FORMAT_VERSION}\0".encode("utf-8"))
    entries = []
    for relative, path, mtime_ns, size in memsearch.scan_cards(vault):
        digest.update(f"{relative}\0{mtime_ns}\0{size}\0".encode("utf-8"))
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            text = ""
        card_type, fields = card_lint.card_type_of(relative, text, path)
        entries.append({
            "path": relative,
            "type": card_type,
            "state": _state_of(card_type, fields),
            "name": _label(fields, relative),
            "description": _text(
                fields.get(memspec.DESCRIPTION_FIELD, ""), memspec.VIEWS_DESCRIPTION_CHARS
            ),
            "decision_key": _text(fields.get(memspec.DECISION_KEY_FIELD, "")),
            "decided_at": _text(fields.get(memspec.CURRENT_DECISION_AT_FIELD, "")),
            "note": _note_of(card_type, fields),
        })
    return vault, entries, digest.hexdigest()


def _of(entries, state, card_type=None):
    return [
        entry for entry in entries
        if entry["state"] == state and (card_type is None or entry["type"] == card_type)
    ]


def _card_line(entry, depth):
    return memspec.VIEWS_CARD_LINE.format(
        name=entry["name"], link=_link(entry["path"], depth), description=entry["description"]
    )


def _header(title, stamp, total):
    return [title, "", memspec.VIEWS_GENERATED_LINE.format(stamp=stamp, total=total), ""]


def render_current(entries, stamp):
    """現用卡。決策段列**全部** active 決策：開場注入只挑有 forbidden 或近 30 天、
    每庫 ≤12 條，那不是完整替代品，所以短入口指過來的必須是全量（收斂第 5 條）。"""
    decisions = _of(entries, STATE_CURRENT, memspec.CARD_TYPE_DECISION)
    review = _of(entries, STATE_REVIEW)
    listed = _of(entries, STATE_CURRENT) + review
    lines = _header(memspec.VIEWS_CURRENT_TITLE, stamp, len(listed))

    lines.append(memspec.VIEWS_DECISION_HEADING.format(count=len(decisions)))
    if decisions:
        lines.extend(
            memspec.VIEWS_DECISION_LINE.format(
                name=entry["name"],
                link=_link(entry["path"], 1),
                key=entry["decision_key"] or memspec.VIEWS_MISSING,
                date=entry["decided_at"] or memspec.VIEWS_MISSING,
                description=entry["description"],
            )
            for entry in decisions
        )
    else:
        lines.append(memspec.VIEWS_EMPTY_SECTION)
    lines.append("")

    for card_type in memspec.CARD_TYPES:
        if card_type == memspec.CARD_TYPE_DECISION:
            continue
        group = _of(entries, STATE_CURRENT, card_type)
        if not group:
            continue  # 小庫不必生成空分類
        lines.append(memspec.VIEWS_TYPE_HEADING.format(type=card_type, count=len(group)))
        lines.extend(_card_line(entry, 1) for entry in group)
        lines.append("")

    lines.append(memspec.VIEWS_REVIEW_HEADING.format(count=len(review)))
    if review:
        lines.extend(
            memspec.VIEWS_NOTE_LINE.format(
                name=entry["name"],
                link=_link(entry["path"], 1),
                note=entry["type"],
                description=entry["description"],
            )
            for entry in review
        )
    else:
        lines.append(memspec.VIEWS_EMPTY_SECTION)
    return "\n".join(lines) + "\n"


def render_closed(entries, stamp):
    """已結案／已取代。多一層目錄，所以連結多一個 `../`。"""
    closed = _of(entries, STATE_CLOSED)
    lines = _header(memspec.VIEWS_CLOSED_TITLE, stamp, len(closed))
    for card_type in memspec.CARD_TYPES:
        group = _of(entries, STATE_CLOSED, card_type)
        if not group:
            continue
        lines.append(memspec.VIEWS_TYPE_HEADING.format(type=card_type, count=len(group)))
        lines.extend(
            memspec.VIEWS_NOTE_LINE.format(
                name=entry["name"],
                link=_link(entry["path"], 2),
                note=entry["note"] or memspec.VIEWS_MISSING,
                description=entry["description"],
            )
            for entry in group
        )
        lines.append("")
    if not closed:
        lines.append(memspec.VIEWS_EMPTY_SECTION)
    return "\n".join(lines) + "\n"


def view_paths(vault):
    root = Path(vault) / memspec.VIEWS_DIRECTORY
    return (
        root / memspec.VIEWS_CURRENT_FILENAME,
        root / memspec.VIEWS_HISTORY_DIRECTORY / memspec.VIEWS_CLOSED_FILENAME,
    )


def _fingerprint_path(vault):
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY / memspec.VIEWS_FINGERPRINT_FILENAME


def _read_fingerprint(vault):
    try:
        value = json.loads(_fingerprint_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value.get(memspec.VIEWS_FINGERPRINT_FIELD) if isinstance(value, dict) else None


def _write_fingerprint(vault, fingerprint):
    path = _fingerprint_path(vault)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({memspec.VIEWS_FINGERPRINT_FIELD: fingerprint}) + "\n", encoding="utf-8"
        )
    except OSError:
        pass  # 指紋寫不出去只是下一次多寫一遍，不值得讓視圖不生成


def _lock_target(vault):
    """鎖檔放系統 temp：`_views/` 是生成器獨占的輸出，鎖不該在庫裡多留一個檔案，
    而同一台機器上同一個庫的兩個生成程序必須看到同一把鎖。"""
    key = hashlib.sha256(os.fspath(vault).casefold().encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / (memspec.VIEWS_LOCK_PREFIX + key)


def _write(path, text):
    """先寫旁邊再換名：截斷式寫入被砍在中間會留下半份目錄，而讀者無從分辨。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + f".tmp{os.getpid()}")
    staging.write_text(text, encoding="utf-8", newline="\n")
    os.replace(staging, path)


def generate(vault, force=False, lock_timeout=DEFAULT_LOCK_TIMEOUT, stamp=None):
    vault, entries, fingerprint = collect(vault)
    current_path, closed_path = view_paths(vault)
    result = {
        "vault": str(vault),
        "status": "written",
        "total": len(entries),
        "current": len(_of(entries, STATE_CURRENT)),
        "closed": len(_of(entries, STATE_CLOSED)),
        "review": len(_of(entries, STATE_REVIEW)),
        "decisions": len(_of(entries, STATE_CURRENT, memspec.CARD_TYPE_DECISION)),
        "paths": {"current": str(current_path), "closed": str(closed_path)},
    }
    with memspec.file_lock(_lock_target(vault), lock_timeout) as held:
        if not held:
            result["status"] = "lock-busy"
            return result
        unchanged = (
            not force
            and _read_fingerprint(vault) == fingerprint
            and current_path.is_file()
            and closed_path.is_file()
        )
        if unchanged:
            result["status"] = "unchanged"
            return result
        stamp = stamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        _write(current_path, render_current(entries, stamp))
        _write(closed_path, render_closed(entries, stamp))
        _write_fingerprint(vault, fingerprint)
    return result


def listed_paths(vault):
    """兩份視圖列出的卡（vault 相對 posix 路徑）——lint 的「目錄漏卡」用這一份。

    只認 `- [...](...)` 那一種行，且把連結還原成 vault 相對路徑；讀不到的視圖回 None，
    讓呼叫端把「沒有目錄」與「目錄漏卡」報成兩件事。
    """
    current_path, closed_path = view_paths(vault)
    found = set()
    for path, depth in ((current_path, 1), (closed_path, 2)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        prefix = "../" * depth
        for line in text.splitlines():
            if not line.startswith("- [") or "](" not in line:
                continue
            target = line.split("](", 1)[1].split(")", 1)[0]
            if not target.startswith(prefix):
                continue
            target = target[len(prefix):]
            for character, encoded in _LINK_ESCAPES.items():
                target = target.replace(encoded, character)
            found.add(target)
    return found


_FIXTURES = {
    "project-open.md": "---\nname: project-open\ndescription: 2026-09-01 還在跑的專案\naliases:\n  - 專案\nmetadata:\n  type: project\n---\nbody\n",
    "project-done.md": "---\nname: project-done\ndescription: 2026-09-01 已結案的專案\nstatus: closed\nclosed_at: 2026-09-09\nclosed_by: claude\naliases:\n  - 結案\nmetadata:\n  type: project\n---\nbody\n",
    "decision-live.md": "---\nname: decision-live\ndescription: 2026-09-01 現行裁定\ndecision_key: k-one\nstatus: active\ncurrent_decision_at: 2026-09-01\ndecided_by: three-way\naliases:\n  - 裁定\n---\nbody\n",
    "decision-old.md": "---\nname: decision-old\ndescription: 2026-08-01 被取代的裁定\ndecision_key: k-one\nstatus: superseded\nsuperseded_by: decision-live.md\ncurrent_decision_at: 2026-08-01\ndecided_by: three-way\naliases:\n  - 舊裁定\n---\nbody\n",
    "feedback-plain.md": "---\nname: feedback-plain\ndescription: 2026-09-01 一般回饋，永遠現用\naliases:\n  - 回饋\nmetadata:\n  type: feedback\n---\nbody\n",
    "grants/grant-one.md": "---\nname: grant-one\ndescription: owner grant auto-captured 2026-09-02: 你可以繼續\ncaptured_at: 2026-09-02T07:37:47Z\nsession_id: synthetic\n---\nbody\n",
}


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-views-") as temp_dir:
            vault = Path(temp_dir).resolve() / "vault"
            vault.mkdir()
            for name, text in _FIXTURES.items():
                target = vault / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text.encode("utf-8"))

            first = generate(vault, stamp="2026-09-09T00:00Z")
            current_path, closed_path = view_paths(vault)
            current = current_path.read_text(encoding="utf-8")
            closed = closed_path.read_text(encoding="utf-8")
            checks.append((
                "兩份視圖都生成，納管卡數＝現用＋結案＋待複查",
                first["status"] == "written"
                and current_path.is_file()
                and closed_path.is_file()
                and first["total"] == len(_FIXTURES)
                and first["current"] + first["closed"] + first["review"] == first["total"],
            ))
            checks.append((
                "行數合計＝納管卡數（每張卡剛好出現在一份視圖的一行）",
                sum(
                    1
                    for text in (current, closed)
                    for line in text.splitlines()
                    if line.startswith("- [")
                ) == len(_FIXTURES),
            ))
            checks.append((
                "project closed 與 decision superseded 落結案；一般回饋不套結案",
                "- [project-done](../../project-done.md)" in closed
                and "- [decision-old](../../decision-old.md)" in closed
                and "closed 2026-09-09 by claude" in closed
                and "superseded_by: decision-live.md" in closed
                and "- [feedback-plain](../feedback-plain.md)" in current,
            ))
            checks.append((
                "決策段列出 active 決策，帶 key｜日期｜一行",
                "- [decision-live](../decision-live.md) — k-one｜2026-09-01｜" in current
                and "decision-old" not in current,
            ))
            checks.append((
                "缺 status 的專案卡進待複查，不冒充已確認現用",
                memspec.VIEWS_REVIEW_HEADING.format(count=1) in current
                and "- [project-open](../project-open.md) — project｜" in current
                and first["review"] == 1,
            ))
            checks.append((
                "連結相對路徑從視圖所在目錄解得開（含子目錄的事件卡）",
                all(
                    (current_path.parent / target).resolve().is_file()
                    for target in ("../feedback-plain.md", "../grants/grant-one.md")
                )
                and (closed_path.parent / "../../project-done.md").resolve().is_file(),
            ))

            second = generate(vault, stamp="2026-09-09T01:00Z")
            checks.append((
                "輸入指紋沒變就不重寫（generated 時間不變）",
                second["status"] == "unchanged"
                and current_path.read_text(encoding="utf-8") == current,
            ))
            forced = generate(vault, force=True, stamp="2026-09-09T02:00Z")
            checks.append((
                "--force 無視指紋重寫",
                forced["status"] == "written"
                and "2026-09-09T02:00Z" in current_path.read_text(encoding="utf-8"),
            ))
            (vault / "feedback-new.md").write_text(
                "---\nname: feedback-new\ndescription: 2026-09-09 新卡\naliases:\n  - 新卡\n---\nbody\n",
                encoding="utf-8",
            )
            third = generate(vault, stamp="2026-09-09T03:00Z")
            checks.append((
                "新增一張卡就重寫，新卡出現在現用視圖",
                third["status"] == "written"
                and third["total"] == len(_FIXTURES) + 1
                and "- [feedback-new](../feedback-new.md)" in current_path.read_text(encoding="utf-8"),
            ))

            with memspec.file_lock(_lock_target(vault), 5.0) as held:
                busy = generate(vault, force=True, lock_timeout=0.0) if held else None
            checks.append((
                "同庫並行：拿不到鎖就回 lock-busy，不寫半份視圖",
                held and busy is not None and busy["status"] == "lock-busy",
            ))

            listed = listed_paths(vault)
            checks.append((
                "listed_paths 還原成 vault 相對路徑，兩份視圖合起來蓋住每張納管卡",
                listed == {relative for relative, _p, _m, _s in memsearch.scan_cards(vault)},
            ))

            empty = Path(temp_dir).resolve() / "empty"
            empty.mkdir()
            blank = generate(empty, stamp="2026-09-09T00:00Z")
            checks.append((
                "空庫也生成兩份視圖，段落寫「無」而不是消失",
                blank["status"] == "written"
                and blank["total"] == 0
                and memspec.VIEWS_EMPTY_SECTION in view_paths(empty)[1].read_text(encoding="utf-8"),
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 12
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
    parser.add_argument("vaults", nargs="+", type=Path)
    parser.add_argument("--force", action="store_true", help="rewrite even when the input fingerprint is unchanged")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(arguments)

    results = []
    code = 0
    for raw in parsed.vaults:
        try:
            results.append(generate(raw.expanduser(), force=parsed.force))
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            code = 2
    if parsed.json:
        print(json.dumps(results, ensure_ascii=False, indent=1), file=output)
    else:
        for item in results:
            print(
                "VIEWS {status} total={total} current={current} closed={closed} "
                "review={review} decisions={decisions} vault={vault}".format(**item),
                file=output,
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
