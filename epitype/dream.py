import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8；唯讀 CLI 不落 pyc。
"""Epitype 夢——離線整理審核包的確定性盤點入口。

不呼叫任何模型。「醒時」（hook）已經有的確定性檢查（別名匯出、卡片型別 lint、殭屍
待辦、草稿、決策鏈、事件卡老化）在這裡各跑一次唯讀盤點，彙整成一份審核
包（Markdown 或 JSON），讓 owner 或子代理一眼看到今晚該整理什麼、該跑哪個既有指令
——這裡本身不套用任何建議，套用一律由列出的指令另外執行。

排程有三種模式（設定在 config 的 dream 區塊）：piggyback（SessionStart 順路起一個
脫鉤的低優先權背景程序）、nightly（graft 註冊系統排程）、off。三者跑的都是同一條
命令 `dream.py --scheduled`——庫與輸出路徑由這裡自己從 config 解出，所以換庫不必
重註冊排程。盤點本身唯讀，寫的是 <治理 vault>/.epitype/ 的 pack／state／lock／log，
外加兩個順路任務：重生 `_views/`，以及把 MEMORY.md 允許段以外、目錄已承載的卡片
連結行搬進 `_drafts/index_pruned/`（原文照搬、不刪；`--dry-run` 只印不改）。
"""

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import posixpath
import subprocess
import sys
import tempfile
import threading
import time
import uuid

try:
    from . import alias_batch, card_io, card_lint, decision_lint, memsearch, memspec, pending_lint
except ImportError:  # Direct script execution keeps the CLI contract.
    import alias_batch
    import card_io
    import card_lint
    import decision_lint
    import memsearch
    import memspec
    import pending_lint

EXAMPLE_LIMIT = 10
TIME_BUDGET_ERROR = "time budget exhausted before this section ran"
DEFAULT_EVENT_AGING_DAYS = 90
RECENT_WINDOW_DAYS = 7
DRAFT_DIRNAME = "_drafts"
_EVENT_TYPE_BY_DIR = dict(memspec.EVENT_CARD_DIRECTORIES)  # {"grants": "grant", ...}


# --------------------------------------------------------------------------- helpers


def _bounded(vaults, fn):
    """跑一個逐 vault 的函式；單一 vault 出錯只跳過那個 vault，其餘照跑。"""
    results = []
    errors = []
    for vault in vaults:
        try:
            results.append((vault, fn(vault)))
        except Exception as exc:
            errors.append(f"{vault}: {type(exc).__name__}: {exc}")
    return results, errors


def _iso_date_of(value):
    if not value or not memspec.is_iso_date(value):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- section 1


def _section_missing_aliases(vaults, today, since_date):
    results, errors = _bounded(vaults, lambda v: alias_batch._export_candidates(v, True, None))
    entries = []
    for vault, candidates in results:
        for item in candidates:
            entries.append({"vault": str(vault), "card_path": item["card_path"], "aliases_now": item["aliases_now"]})
    entries.sort(key=lambda item: (item["vault"], item["card_path"]))
    commands = [f'epitype aliases export "{vault}"' for vault in vaults] if entries else []
    return {
        "counts": {"missing_aliases": len(entries)},
        "examples": entries[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 2


def _section_card_lint(vaults, today, since_date):
    results, errors = _bounded(vaults, lambda v: card_lint.scan_vault(v, today))
    fail = warn = fail_cards = 0
    cards = []
    for vault, report in results:
        fail += report["fail"]
        warn += report["warn"]
        fail_cards += report["fail_cards"]
        for card in report["cards"]:
            cards.append({"vault": str(vault), "path": card["path"], "fail": card["fail"], "warn": card["warn"]})
    cards.sort(key=lambda item: (-item["fail"], -item["warn"], item["path"]))
    commands = [f'epitype cards "{vault}"' for vault in vaults] if (fail or warn) else []
    return {
        "counts": {"fail": fail, "warn": warn, "fail_cards": fail_cards},
        "examples": cards[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 3


def _section_pending(vaults, today, since_date):
    results, errors = _bounded(vaults, lambda v: pending_lint.scan_vault(v, today=today))
    zombie_cards = zombie_lines = oldest = 0
    entries = []
    for vault, report in results:
        zombie_cards += report["zombie_cards"]
        zombie_lines += report["zombie_lines"]
        oldest = max(oldest, report["oldest_days"])
        for card in report["cards"]:
            entries.append({"vault": str(vault), "path": card["path"], "oldest_days": card["oldest_days"]})
    entries.sort(key=lambda item: -item["oldest_days"])
    commands = [f'epitype pending "{vault}"' for vault in vaults] if zombie_lines else []
    return {
        "counts": {"zombie_cards": zombie_cards, "zombie_lines": zombie_lines, "oldest_days": oldest},
        "examples": entries[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 4


def _drafts_of(vault):
    root = Path(vault) / DRAFT_DIRNAME
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.md"))


def _section_drafts(vaults, today, since_date):
    results, errors = _bounded(vaults, _drafts_of)
    by_subdir = {}
    entries = []
    for vault, paths in results:
        for path in paths:
            relative = path.relative_to(vault).as_posix()
            parts = relative.split("/")
            subdir = parts[1] if len(parts) > 2 else "(root)"
            by_subdir[subdir] = by_subdir.get(subdir, 0) + 1
            entries.append({"vault": str(vault), "path": relative})
    commands = []
    pending_root = memspec.CAPTURE_PENDING_SUBPATH[-1]
    pruned_root = memspec.INDEX_PRUNED_SUBPATH[-1]
    # 整形移出的行不是捕捉草稿，`--reevaluate` 對它沒有意義（那條路問的是「今天的規則
    # 還會不會捕捉這句話」）；它只是人要看的紀錄，所以不觸發任何建議指令。
    replayable = {pending_root, pruned_root}
    for vault, paths in results:
        if any(replayable.isdisjoint(path.parts) for path in paths):
            commands.append(
                f'python epitype/harvest.py --reevaluate "{Path(vault) / DRAFT_DIRNAME / "decisions"}" [--apply]'
            )
        # 捕捉提案不走 --reevaluate：那條路是「今天的規則還會不會捕捉」，而提案被扣住
        # 的原因正是它會被捕捉；轉正是人看過、改 verified 的動作（owner 2026-09-09 Q5「C」）。
        if any(pending_root in path.parts for path in paths):
            commands.append(memspec.CAPTURE_PENDING_REVIEW_COMMAND.format(
                path=Path(vault).joinpath(*memspec.CAPTURE_PENDING_SUBPATH)
            ))
    return {
        "counts": {
            "total_drafts": len(entries),
            "by_subdir": by_subdir,
            "captured_pending": by_subdir.get(pending_root, 0),
        },
        "examples": entries[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 5


def _stop_gate_forbidden_reader():
    """A callable (path) -> bool: does this card declare a non-empty `forbidden`?

    memspec.frontmatter_fields (what decision_lint.Card.fields carries) only returns
    flat top-level scalars, so a `forbidden:` written as a YAML block list (or an
    inline `[a, b]`) reads back as "" and would show up here as a false
    "missing_forbidden". adapters/claude/stop_gate.py already has to solve exactly
    this (its own gate rules on `forbidden`), so this reuses that reader instead of
    writing a third YAML-list parser.
    """
    repo_root = Path(__file__).resolve().parents[1]
    claude_adapter_dir = repo_root / "adapters" / "claude"
    for extra in (str(repo_root), str(claude_adapter_dir)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import stop_gate
    from pretooluse_gate import _inline_items

    def has_forbidden(path):
        front_lines = stop_gate._decision_frontmatter(path)
        if front_lines is None:
            return False
        values = stop_gate._sequence_fields(front_lines, memspec.TOP_LEVEL_FIELD, _inline_items)
        return bool(values[memspec.FORBIDDEN_FIELD])

    return has_forbidden


def _section_decisions(vaults, today, since_date):
    results, errors = _bounded(vaults, decision_lint.lint_vault)
    has_forbidden_reader = _stop_gate_forbidden_reader()
    active = superseded = 0
    missing = []
    for vault, report in results:
        for card in report.cards:
            status = card.fields.get(memspec.DECISION_STATUS_FIELD, "").strip()
            if status == memspec.ACTIVE_DECISION_STATUS:
                active += 1
                has_quote = bool(card.fields.get(memspec.OWNER_QUOTE_FIELD, "").strip())
                has_forbidden = has_forbidden_reader(card.path)
                if not has_quote or not has_forbidden:
                    try:
                        relative = card.path.relative_to(vault).as_posix()
                    except ValueError:
                        relative = str(card.path)
                    missing.append({
                        "vault": str(vault),
                        "path": relative,
                        "missing_owner_quote": not has_quote,
                        "missing_forbidden": not has_forbidden,
                    })
            elif status == memspec.SUPERSEDED_DECISION_STATUS:
                superseded += 1
    missing.sort(key=lambda item: item["path"])
    commands = [f'epitype decisions "{vault}"' for vault in vaults] if missing else []
    return {
        "counts": {"active": active, "superseded": superseded, "missing_owner_or_forbidden": len(missing)},
        "examples": missing[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 6


def _event_cards_of(vault):
    found = []
    for path in memsearch.card_files(vault):
        relative = path.relative_to(vault).as_posix()
        top_dir = relative.split("/", 1)[0]
        card_type = _EVENT_TYPE_BY_DIR.get(top_dir)
        if card_type is None:
            continue
        fields, _problem = memspec.frontmatter_fields(path)
        found.append((relative, card_type, fields.get(memspec.CAPTURED_AT_FIELD, "").strip()))
    return found


def _section_event_aging(vaults, today, since_date):
    results, errors = _bounded(vaults, _event_cards_of)
    counts_by_type = {card_type: 0 for card_type in _EVENT_TYPE_BY_DIR.values()}
    aging_by_type = {card_type: 0 for card_type in _EVENT_TYPE_BY_DIR.values()}
    candidates = []
    for vault, found in results:
        for relative, card_type, captured in found:
            counts_by_type[card_type] += 1
            captured_date = _iso_date_of(captured)
            if captured_date is not None and captured_date < since_date:
                aging_by_type[card_type] += 1
                candidates.append({
                    "vault": str(vault),
                    "path": relative,
                    "type": card_type,
                    "captured_at": captured,
                })
    candidates.sort(key=lambda item: item["captured_at"])
    total_aging = sum(aging_by_type.values())
    commands = ["人工複核候選事件卡，決定是否歸檔（保留，不刪）；沒有對應的自動 CLI 指令"] if total_aging else []
    return {
        "counts": {"by_type": counts_by_type, "aging_by_type": aging_by_type, "aging_total": total_aging},
        "examples": candidates[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 7


def _git_added_dates(vault):
    """{相對 posix 路徑: 最早新增日期}；不是 git repo 或指令不可用時回傳 {}。"""
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(vault), "log", "--diff-filter=A", "--name-only", "--format=C\t%ad", "--date=short"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    added = {}
    current = None
    for raw_line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if raw_line.startswith("C\t"):
            current = raw_line.split("\t", 1)[1].strip()
            continue
        line = raw_line.strip()
        if not line or current is None:
            continue
        # git log walks newest commit first; keep the OLDEST add date per path.
        added[line] = min(added[line], current) if line in added else current
    return added


def _recent_cards_of(vault):
    git_dates = _git_added_dates(vault)
    entries = []
    for path in memsearch.card_files(vault):
        relative = path.relative_to(vault).as_posix()
        fields, _problem = memspec.frontmatter_fields(path)
        captured_date = _iso_date_of(fields.get(memspec.CAPTURED_AT_FIELD, "").strip())
        source = "captured_at" if captured_date is not None else None
        if captured_date is None and relative in git_dates:
            try:
                captured_date = date.fromisoformat(git_dates[relative])
                source = "git"
            except ValueError:
                captured_date = None
        if captured_date is None:
            try:
                captured_date = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
                source = "mtime"
            except OSError:
                continue
        entries.append((relative, captured_date, source))
    return entries


def _section_recent(vaults, today, since_date):
    results, errors = _bounded(vaults, _recent_cards_of)
    recent = []
    for vault, entries in results:
        for relative, added_date, source in entries:
            if (today - added_date).days <= RECENT_WINDOW_DAYS:
                recent.append({"vault": str(vault), "path": relative, "date": added_date.isoformat(), "source": source})
    recent.sort(key=lambda item: item["date"], reverse=True)
    return {
        "counts": {"recent_7d": len(recent)},
        "examples": recent[:EXAMPLE_LIMIT],
        "commands": [],
        "errors": errors,
    }


# --------------------------------------------------------------------------- 主記憶整形（順路任務）


def _views_module():
    """lazy import：夢的盤點路徑不為生成器付錢，整形與順路重生共用這一處。"""
    try:
        from . import views
    except ImportError:  # Direct script execution keeps the CLI contract.
        import views
    return views


def _allowed_index_section(title):
    key = " ".join(str(title or "").split()).casefold()
    return any(key == allowed.casefold() for allowed in memspec.INDEX_ALLOWED_SECTIONS)


def _index_card_targets(line):
    """這一行指到的卡片（vault 相對 posix 路徑）；沒有 `](….md)` 就回空清單。"""
    targets = []
    for raw in memspec.INDEX_CARD_LINK_REGEX.findall(line):
        target = raw.split("#", 1)[0].replace("\\", "/")
        for character, encoded in memspec.MARKDOWN_LINK_ESCAPES.items():
            target = target.replace(encoded, character)
        if target.endswith(".md"):
            targets.append(posixpath.normpath(target))
    return targets


def _index_stat(path):
    """(mtime_ns, size)——讀後與寫前各取一次；中間變了就放棄本次整形。"""
    info = path.stat()
    return (info.st_mtime_ns, info.st_size)


def _split_index(text, listed):
    """(留下的行, 要搬的 [(段標題, 原文行)], 留下但視圖沒列的連結行)。

    段標題＝最近一個 `##` 以上的標題（`#` 是檔名標題，之後算「不在任何段」）。允許段
    內一律不動：那是手寫區，動它就等於生成器去跟其他寫者搶同一份檔案。第一個 `##`
    之前的前言區同樣不動：短入口的標題行與說明行本來就可能帶連結。

    判斷段落前要先跳過檔首 BOM——帶 BOM 的第一行首字元不是 `#`，第一個標題會認不出
    來，整份檔就被當成「不在任何段」而全部可搬。BOM 只影響判斷：`raw` 一律原文進
    keep，寫回的位元組不因這個判斷而改變。
    """
    keep, moved, unlisted_lines = [], [], []
    section = None
    seen_section = False
    fenced = False
    for position, raw in enumerate(text.splitlines(keepends=True)):
        stripped = (raw.lstrip("\N{ZERO WIDTH NO-BREAK SPACE}") if position == 0 else raw).strip()
        if stripped.startswith("```"):
            fenced = not fenced
            keep.append(raw)
            continue
        if not fenced and stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            section = stripped.lstrip("#").strip() if level >= 2 else None
            seen_section = seen_section or level >= 2
            keep.append(raw)
            continue
        if fenced or not seen_section or (section is not None and _allowed_index_section(section)):
            keep.append(raw)
            continue
        targets = _index_card_targets(raw)
        if not targets:
            keep.append(raw)
            continue
        missing = [target for target in targets if target not in listed]
        if missing:
            # 視圖沒列的連結不搬：那可能是剛寫好、還沒生成目錄的新卡，搬走就真的不見了。
            unlisted_lines.append({"section": section, "line": stripped, "missing": missing})
            keep.append(raw)
            continue
        moved.append((section, raw))
    return keep, moved, unlisted_lines


def _append_pruned(vault, today, moved, stamp):
    """把移出的行原文照搬進 `_drafts/index_pruned/YYYYMMDD.md`（附時間、來源段、原因）。

    先寫這裡再改 MEMORY.md：換名寫入若在最後一步被拒，行仍然兩邊都在，下次夢會把它
    再記一次——這是紀錄檔，多一筆各自帶自己的時間戳，比漏一筆安全。

    照搬＝逐位元組，連原本的行尾（CRLF 就是 CRLF）一起；也不去重：同一行出現在不同
    段是兩件事，兩筆都要留得下來。所以這裡用二進位附加，不讓文字模式改寫行尾。
    """
    path = Path(vault).joinpath(*memspec.INDEX_PRUNED_SUBPATH) / f"{today.strftime('%Y%m%d')}.md"
    try:
        existing = path.read_bytes()
    except OSError:
        existing = b""
    block = [] if existing else [memspec.INDEX_PRUNED_TITLE + "\n", "\n"]
    for section, raw in moved:
        block.append(memspec.INDEX_PRUNED_ENTRY_NOTE.format(
            stamp=stamp,
            source=memspec.MEMORY_INDEX_FILENAME,
            section=section or memspec.INDEX_PRUNED_SECTION_NONE,
            reason=memspec.INDEX_PRUNED_REASON,
        ) + "\n")
        block.append(raw)
        if not raw.endswith(("\n", "\r")):
            # 檔尾那一行原本就沒有換行；補一個純粹是不讓下一筆註解黏上去。
            block.append("\n")
        block.append("\n")
    written = len(moved)
    if written:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            stream.write("".join(block).encode("utf-8"))
    return path, written


def shape_index(vault, today, apply=True, stamp=None):
    """把 MEMORY.md 允許段以外、目錄已經承載的卡片連結行搬進 `_drafts/index_pruned/`。

    主記憶會自己長回來（宿主「存卡後在 MEMORY.md 加一行」的預設、別場直接編輯），
    事前用寫檔閘擋會連合法的手寫連結一起擋掉，所以改成夜裡低頻、受控、小範圍的事後
    整形（Codex 2026-09-09 (c)）：讀→記 mtime＋大小→改→寫前再比→換名寫入（帶原內容
    比對）→再讀核對，任一步對不上就整份放棄並記一行，絕不硬寫。搬走的行不刪，原文
    留在 `_drafts/`。
    """
    vault = Path(vault)
    index_path = vault / memspec.MEMORY_INDEX_FILENAME
    result = {
        "vault": str(vault), "status": "clean", "moved": 0, "kept": 0,
        "moved_examples": [], "kept_examples": [], "pruned_path": None, "reason": None,
    }
    try:
        original = index_path.read_bytes()
        before = _index_stat(index_path)
    except OSError:
        result["status"] = "no-index"
        result["reason"] = memspec.INDEX_SHAPING_NO_INDEX
        return result
    try:
        text = original.decode("utf-8")
    except UnicodeError as exc:
        result["status"] = "error"
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result

    listed = _views_module().listed_paths(vault)
    if listed is None:
        # 目錄讀不到就沒有「已被承載」的證據，整形沒有判準——不猜，報一行。
        result["status"] = "no-views"
        result["reason"] = memspec.INDEX_SHAPING_NO_VIEWS.format(
            directory=memspec.VIEWS_DIRECTORY, vault=vault
        )
        return result

    keep, moved, unlisted_lines = _split_index(text, listed)
    result["moved"] = len(moved)
    result["kept"] = len(unlisted_lines)
    result["moved_examples"] = [line.strip() for _section, line in moved[:EXAMPLE_LIMIT]]
    result["kept_examples"] = unlisted_lines[:EXAMPLE_LIMIT]
    if not moved:
        return result
    if not apply:
        result["status"] = "would-move"
        return result

    try:
        if _index_stat(index_path) != before:
            result["status"] = "abandoned"
            result["reason"] = memspec.INDEX_SHAPING_RACE_REASON
            return result
        stamp = stamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        pruned_path, _written = _append_pruned(vault, today, moved, stamp)
        result["pruned_path"] = str(pruned_path)
        payload = "".join(keep).encode("utf-8")
        card_io.replace_if_unchanged(index_path, payload, original)
    except card_io.CardConflict as exc:
        result["status"] = "abandoned"
        result["reason"] = memspec.INDEX_SHAPING_CONFLICT_REASON.format(error=exc)
        return result
    except OSError as exc:
        result["status"] = "error"
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result

    if index_path.read_bytes() != payload:
        result["status"] = "readback-mismatch"
        result["reason"] = memspec.INDEX_SHAPING_READBACK_REASON
        return result
    result["status"] = "moved"
    return result


# --------------------------------------------------------------------------- report assembly


_SECTIONS = (
    (1, "缺別名卡", _section_missing_aliases),
    (2, "卡片型別檢查 FAIL／WARN", _section_card_lint),
    (3, "殭屍待辦", _section_pending),
    (4, "草稿待審", _section_drafts),
    (5, "裁定鏈", _section_decisions),
    (6, "事件卡老化", _section_event_aging),
    (7, "最近 7 天新增卡數", _section_recent),
)


def _next_steps(sections, shaping=()):
    by_id = {section["id"]: section for section in sections}

    def counts(section_id):
        section = by_id.get(section_id) or {}
        return section.get("counts") or {}

    steps = []
    card = counts(2)
    if card.get("fail", 0) > 0:
        steps.append(f"卡片型別檢查有 FAIL {card['fail']} 筆，先修 → epitype cards <vault>")
    alias = counts(1)
    if alias.get("missing_aliases", 0) > 20:
        steps.append(f"缺別名卡 {alias['missing_aliases']} 張超過門檻，跑別名批次 → epitype aliases export <vault>")
    draft = counts(4)
    if draft.get("total_drafts", 0) > 0:
        held = draft.get("captured_pending", 0)
        held_note = f"（其中捕捉提案 {held} 份，未核不得當依據）" if held else ""
        steps.append(f"草稿待審 {draft['total_drafts']} 份{held_note} → 人工審閱 _drafts/**")
    pending = counts(3)
    if pending.get("zombie_cards", 0) > 0:
        steps.append(f"殭屍待辦 {pending['zombie_lines']} 行／{pending['zombie_cards']} 卡 → epitype pending <vault>")
    decision = counts(5)
    if decision.get("missing_owner_or_forbidden", 0) > 0:
        steps.append(f"active 決策卡缺 owner_quote／forbidden 共 {decision['missing_owner_or_forbidden']} 張 → epitype decisions <vault>")
    event = counts(6)
    if event.get("aging_total", 0) > 0:
        steps.append(f"事件卡老化候選 {event['aging_total']} 張 → 人工複核是否歸檔（不刪）")
    kept = sum(item.get("kept", 0) for item in shaping)
    if kept:
        steps.append(memspec.INDEX_SHAPING_KEPT_STEP.format(count=kept))
    abandoned = sum(1 for item in shaping if item.get("status") == "abandoned")
    if abandoned:
        steps.append(memspec.INDEX_SHAPING_ABANDONED_STEP.format(count=abandoned))
    if any(section.get("error") or section.get("errors") for section in sections):
        steps.append("盤點未完成：先查看失敗／略過的節與 vault，重跑後才能確認其餘待處理項。")
    if not steps:
        steps.append("目前沒有需要今晚整理的項目。")
    return steps


def build_report(vaults, today=None, since_date=None, deadline=None, shaping=None):
    today = today or datetime.now(timezone.utc).date()
    since_date = since_date or (today - timedelta(days=DEFAULT_EVENT_AGING_DAYS))
    sections = []
    for section_id, title, fn in _SECTIONS:
        # 時限到了就把剩下的節標成略過：背景程序寧可交半份標明缺口的包，也不要
        # 在一個大庫上跑到天亮（CORE-10：缺口要說出來，不是靜靜少一節）。
        if deadline is not None and time.monotonic() >= deadline:
            sections.append({"id": section_id, "title": title, "error": TIME_BUDGET_ERROR})
            continue
        try:
            data = fn(vaults, today, since_date)
            sections.append({"id": section_id, "title": title, "error": None, **data})
        except Exception as exc:
            sections.append({"id": section_id, "title": title, "error": f"{type(exc).__name__}: {exc}"})
    shaping = list(shaping or ())
    return {
        "vaults": [str(vault) for vault in vaults],
        "today": today.isoformat(),
        "since": since_date.isoformat(),
        "sections": sections,
        "index_shaping": shaping,
        "next_steps": _next_steps(sections, shaping),
    }


def _render_markdown(report):
    lines = [f"# Epitype Dream Pack — {report['today']}", ""]
    lines.append("Vaults:")
    for vault in report["vaults"]:
        lines.append(f"- {vault}")
    lines.append(f"事件卡老化門檻（--since）：{report['since']}")
    lines.append("")
    for section in report["sections"]:
        lines.append(f"## {section['id']}. {section['title']}")
        if section.get("error"):
            lines.append(f"（此節失敗：{section['error']}）")
            lines.append("")
            continue
        lines.append("counts: " + json.dumps(section.get("counts", {}), ensure_ascii=False))
        for error in section.get("errors") or ():
            lines.append(f"（部分 vault 略過：{error}）")
        examples = section.get("examples") or []
        if examples:
            lines.append(f"examples (前 {len(examples)} 筆):")
            for item in examples:
                lines.append(f"- {item}")
        for command in section.get("commands") or ():
            lines.append(f"建議指令：{command}")
        note = section.get("note")
        if note:
            lines.append(f"備註：{note}")
        lines.append("")
    lines.append(memspec.INDEX_SHAPING_HEADING)
    shaping = report.get("index_shaping") or ()
    for item in shaping:
        lines.append("- " + memspec.INDEX_SHAPING_LINE.format(
            vault=item["vault"], status=item["status"], moved=item["moved"], kept=item["kept"],
            detail=item.get("reason") or item.get("pruned_path") or memspec.VIEWS_MISSING,
        ))
        for example in item.get("moved_examples") or ():
            lines.append(f"  - 移出：{example}")
        for example in item.get("kept_examples") or ():
            lines.append(f"  - 留下（視圖未列 {example['missing']}）：{example['line']}")
    if not shaping:
        lines.append(memspec.VIEWS_EMPTY_SECTION)
    lines.append("")

    lines.append("## 9. 夢的下一步")
    for step in report["next_steps"]:
        lines.append(f"- {step}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 排程與狀態


def dream_root(governance):
    """夢的檔案只落在這裡：pack、state、lock、log 全在 <治理 vault>/.epitype/ 內。"""
    return Path(governance).expanduser().resolve() / memspec.DREAM_DIRECTORY


def governance_vault(vaults):
    """帶工作帳本的那個庫；沒有帳本就用第一個（與 hook 的 governance_vault 同規則）。"""
    paths = [Path(vault) for vault in vaults]
    for vault in paths:
        if (vault / memspec.WORK_LEDGER_FILENAME).is_file():
            return vault
    return paths[0]


def log(governance, message):
    """夢的例外只寫這裡：開場不吵、hook 不受影響，事後查得到。"""
    try:
        root = dream_root(governance)
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (root / memspec.DREAM_LOG_FILENAME).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _epoch_of(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def due(state, interval_hours=memspec.DREAM_DEFAULT_INTERVAL_HOURS, now=None):
    """沒跑過，或距上次完成超過 interval_hours，才輪到這一場順路做。"""
    state = state or {}
    completed = state.get(memspec.DREAM_STATE_COMPLETED_EPOCH_FIELD)
    if isinstance(completed, bool) or not isinstance(completed, (int, float)):
        completed = _epoch_of(state.get(memspec.DREAM_STATE_COMPLETED_FIELD))
    if completed is None:
        return True
    return ((time.time() if now is None else now) - completed) >= interval_hours * 3600


def _lock_is_stale(path, now):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        started = value.get("started") if isinstance(value, dict) else None
    except (OSError, ValueError):
        started = None
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        try:
            started = path.stat().st_mtime
        except OSError:
            return False
    return (now - started) >= memspec.DREAM_LOCK_STALE_SECONDS


_LOCK_THREAD_GUARD = threading.Lock()


@contextmanager
def _lock_guard(governance):
    """OS 鎖只保護 lease 的讀改寫；guard 永不刪除／換檔，避免不同 inode 各自上鎖。
    非阻塞取得，最多等 0.1 秒；程序死亡由 kernel 釋放，不另做 stale 搶鎖。"""
    if not _LOCK_THREAD_GUARD.acquire(timeout=0.1):
        yield None
        return
    handle = None
    try:
        root = dream_root(governance)
        root.mkdir(parents=True, exist_ok=True)
        path = root / memspec.DREAM_LOCK_FILENAME
        handle = os.open(str(path) + ".guard", os.O_CREAT | os.O_RDWR, 0o600)
        if os.name == "nt":
            import msvcrt
            acquire = lambda: msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        deadline = time.monotonic() + 0.1
        while True:
            try:
                acquire()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
    except (OSError, ImportError):
        if handle is not None:
            os.close(handle)
        _LOCK_THREAD_GUARD.release()
        yield None
        return
    try:
        yield path
    finally:
        try:
            unlock()
        finally:
            os.close(handle)
            _LOCK_THREAD_GUARD.release()


def _read_lock(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _write_lock(path, value):
    staging = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        staging.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def acquire_lock(governance, now=None, pid=None, handoff=False):
    """取得後回傳本輪 token；只有逾時且持有人已死的 lease 才能接管。"""
    now = time.time() if now is None else now
    with _lock_guard(governance) as path:
        if path is None:
            return None
        try:
            value = _read_lock(path)
            owner = value.get("pid")
            alive = (isinstance(owner, int) and not isinstance(owner, bool)
                     and memspec._process_is_alive(owner))
            if path.exists() and (alive or not _lock_is_stale(path, now)):
                return None
            token = uuid.uuid4().hex
            _write_lock(path, {
                "pid": os.getpid() if pid is None else pid, "token": token,
                "started": now, "handoff": handoff,
                "started_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            })
            return token
        except OSError:
            return None


def release_lock(governance, token):
    with _lock_guard(governance) as path:
        if path is None:
            return False
        try:
            value = _read_lock(path)
            if value.get("token") != token or value.get("pid") != os.getpid():
                return False
            path.unlink()
            return True
        except OSError:
            return False


def _handoff_lock(governance, token, child_pid=None):
    """父程序只補 pending lease 的 PID；子程序憑 token 接手一次，之後父程序不可改寫。"""
    with _lock_guard(governance) as path:
        if path is None:
            return False
        try:
            value = _read_lock(path)
            if not token or value.get("token") != token or not value.get("handoff"):
                return False
            if child_pid is not None and value.get("pid") != os.getpid():
                return False
            value["pid"] = os.getpid() if child_pid is None else child_pid
            if child_pid is None:
                value["handoff"] = False
            _write_lock(path, value)
            return True
        except OSError:
            return False


def launch_argv(python_executable=None, script=None):
    """排程與 piggyback 共用的唯一命令形。不用 `-m`：排程器無法保證 cwd 或
    PYTHONPATH 帶得到 repo，而腳本路徑在哪都一樣讀得到。庫與輸出路徑由
    `--scheduled` 自己從 config 解出，呼叫端不必重述一遍。"""
    return [
        str(python_executable or sys.executable),
        os.fspath(Path(script or __file__).resolve()),
        memspec.DREAM_SCHEDULED_FLAG,
    ]


def _launch(argv, log_path):
    """脫鉤、低優先權、輸出全導進 dream.log 的背景程序；回傳 pid。"""
    options = {}
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        )
    else:
        options["start_new_session"] = True
        if hasattr(os, "nice"):
            options["preexec_fn"] = lambda: os.nice(memspec.DREAM_NICE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as stream:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **options,
        )
    return process.pid


def spawn(governance, python_executable=None, launcher=None, now=None):
    """起一個脫鉤的背景夢，不等它。已有未逾時的 lock 就不起；起不來只寫 log。"""
    token = acquire_lock(governance, now, handoff=True)
    if not token:
        return False
    argv = launch_argv(python_executable)
    if not argv[0]:
        log(governance, "spawn skipped: no python executable")
        release_lock(governance, token)
        return False
    try:
        pid = (launcher or _launch)(
            [*argv, memspec.DREAM_LOCK_HELD_FLAG, "--lock-token", token],
            dream_root(governance) / memspec.DREAM_LOG_FILENAME,
        )
    except Exception as exc:
        log(governance, f"spawn failed: {type(exc).__name__}: {exc}")
        release_lock(governance, token)
        return False
    _handoff_lock(governance, token, child_pid=pid)
    return True


def _config_path():
    configured = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
    return Path(configured).expanduser() if configured else Path.home() / ".epitype" / "config.json"


def configured_vaults(config_path=None):
    """登記的庫（只留存在的目錄）。夢只讀 vaults：跑不跑由排程器與 hook 決定。"""
    value = json.loads(Path(config_path or _config_path()).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("config root must be an object")
    raw = value.get(memspec.CONFIG_VAULTS_FIELD)
    vaults = [
        Path(item).expanduser().resolve()
        for item in (raw if isinstance(raw, list) else ())
        if isinstance(item, str) and item.strip()
    ]
    vaults = [vault for vault in vaults if vault.is_dir()]
    if not vaults:
        raise ValueError("config lists no existing vault")
    return vaults


# 開場那一行的欄位名同源 memspec；這裡只說每個名字取哪一節的哪個數字。
_HEADLINE_SOURCES = {
    "card_fail": (2, "fail"),
    "missing_aliases": (1, "missing_aliases"),
    "drafts": (4, "total_drafts"),
}


def _headline(report):
    """開場那一行要的數字；其餘各節數字整包留在 state 的 sections 裡。"""
    counts = {section["id"]: (section.get("counts") or {}) for section in report["sections"]}
    headline = {}
    for field in memspec.DREAM_HEADLINE_FIELDS:
        section_id, key = _HEADLINE_SOURCES.get(field, (None, None))
        headline[field] = counts.get(section_id, {}).get(key, 0)
    return headline


def _report_errors(report):
    """保留每節及逐庫錯誤；沒有執行的檢查不能折算成零問題。"""
    sections = {section["id"]: section for section in report["sections"]}
    errors = {}
    for section_id, _title, _fn in _SECTIONS:
        section = sections.get(section_id)
        if section is None:
            errors[str(section_id)] = {"error": "section missing", "errors": []}
        elif section.get("error") or section.get("errors"):
            errors[str(section_id)] = {
                "error": section.get("error"), "errors": section.get("errors") or [],
            }
    return errors


# --------------------------------------------------------------------------- selftest


def _write_card(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-dream-") as temp_dir:
            vault = Path(temp_dir).resolve() / "vault"
            vault.mkdir()
            today = date(2026, 9, 6)

            # section 1: one card with no aliases, one with plenty.
            _write_card(
                vault / "feedback" / "no_alias.md",
                "---\nname: no_alias\ndescription: 2026-06-01 synthetic feedback\n---\nbody\n",
            )
            _write_card(
                vault / "feedback" / "has_alias.md",
                "---\nname: has_alias\ndescription: 2026-06-01 synthetic feedback\naliases:\n- a\n- b\n---\nbody\n",
            )

            # section 2: a FAIL card (no frontmatter) and a clean one.
            _write_card(vault / "feedback" / "broken.md", "no frontmatter here\n")
            _write_card(
                vault / "feedback" / "clean.md",
                "---\nname: clean\ndescription: 2026-06-01 中文 synthetic\naliases:\n- x\n---\nbody\n",
            )

            # section 3: an overdue pending line.
            _write_card(
                vault / "feedback" / "plan.md",
                "---\nname: plan\ndescription: 2026-06-01 synthetic（待 owner 決策項）\naliases:\n- plan\n---\n"
                "- 2026-07-01 待辦：跑掉這行\n",
            )

            # section 4: two drafts in one subdirectory.
            _write_card(vault / "_drafts" / "decisions" / "d1.md", "draft one\n")
            _write_card(vault / "_drafts" / "decisions" / "d2.md", "draft two\n")

            # section 5: one active decision missing owner_quote, one clean superseded.
            _write_card(
                vault / "missing_quote.md",
                "---\ndecision_key: k1\nstatus: active\ncurrent_decision_at: 2026-06-01\n"
                "decided_by: ai-autonomous\naliases:\n- k1\n---\nbody\n",
            )
            _write_card(
                vault / "old_decision.md",
                "---\ndecision_key: k1\nstatus: superseded\nsuperseded_by: missing_quote.md\n"
                "current_decision_at: 2026-05-01\ndecided_by: ai-autonomous\naliases:\n- k1\n---\nbody\n",
            )
            # section 5 (fix): forbidden written as a YAML block list must not be
            # misread as missing — memspec.frontmatter_fields only returns flat
            # scalars, so this active card would false-positive without the
            # stop_gate sequence reader.
            _write_card(
                vault / "block_forbidden.md",
                "---\ndecision_key: k2\nstatus: active\ncurrent_decision_at: 2026-06-01\n"
                "decided_by: ai-autonomous\nowner_quote: 就這樣\nforbidden:\n  - 不要這樣做\n"
                "aliases:\n- k2\n---\nbody\n",
            )

            # section 6 + 7: one old grant (aging candidate), one fresh grant (recent).
            _write_card(
                vault / "grants" / "old_grant.md",
                "---\nname: old_grant\ndescription: synthetic\ncaptured_at: 2025-01-01\nsession_id: s1\n---\nbody\n",
            )
            _write_card(
                vault / "grants" / "new_grant.md",
                "---\nname: new_grant\ndescription: synthetic\ncaptured_at: 2026-09-05\nsession_id: s1\n---\nbody\n",
            )

            # backdate everything so the mtime fallback in section 7 doesn't
            # pick up "just written by this test" as "recent" — only
            # new_grant.md's explicit captured_at should land in the window.
            old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
            for md_path in vault.rglob("*.md"):
                os.utime(md_path, (old_ts, old_ts))

            report = build_report([vault], today=today)
            by_id = {section["id"]: section for section in report["sections"]}

            checks.append(("all 7 deterministic sections present with no error", all(
                by_id[i]["error"] is None for i in range(1, 8)
            )))
            checks.append(("section 1 counts the alias-less card only", by_id[1]["counts"]["missing_aliases"] == 1
                and by_id[1]["examples"][0]["card_path"] == "feedback/no_alias.md"))
            checks.append(("section 2 sees the frontmatter-less FAIL card", by_id[2]["counts"]["fail"] >= 1
                and any(item["path"] == "feedback/broken.md" for item in by_id[2]["examples"])))
            checks.append(("section 3 counts the overdue pending line", by_id[3]["counts"]["zombie_lines"] == 1))
            checks.append(("section 4 counts both drafts under their subdirectory", by_id[4]["counts"]["total_drafts"] == 2
                and by_id[4]["counts"]["by_subdir"].get("decisions") == 2))
            checks.append(("section 5 flags the active card missing owner_quote and counts superseded", (
                by_id[5]["counts"]["active"] == 2
                and by_id[5]["counts"]["superseded"] == 1
                and by_id[5]["counts"]["missing_owner_or_forbidden"] == 1
                and by_id[5]["examples"][0]["path"] == "missing_quote.md"
            )))
            checks.append(("section 5 does not flag a block-list forbidden as missing", not any(
                item["path"] == "block_forbidden.md" for item in by_id[5]["examples"]
            )))
            checks.append(("section 6 flags the old grant as an aging candidate, not the new one", (
                by_id[6]["counts"]["by_type"]["grant"] == 2
                and by_id[6]["counts"]["aging_total"] == 1
                and by_id[6]["examples"][0]["path"] == "grants/old_grant.md"
            )))
            checks.append(("section 7 counts the fresh grant as recent, not the old one", (
                by_id[7]["counts"]["recent_7d"] == 1
                and by_id[7]["examples"][0]["path"] == "grants/new_grant.md"
            )))
            checks.append(("no section counts AI commitments any more (owner 2026-09-09)", not any(
                "commitment" in key
                for section in report["sections"]
                for key in (section.get("counts") or {})
            )))

            # --since moved before old_grant's captured_at (2025-01-01) clears it as
            # a candidate: only cards captured *before* the cutoff count as aging.
            loose_report = build_report([vault], today=today, since_date=date(2024, 1, 1))
            loose_by_id = {section["id"]: section for section in loose_report["sections"]}
            checks.append(("--since moved earlier than old_grant's date clears the aging candidate", loose_by_id[6]["counts"]["aging_total"] == 0))

            # a broken section function must not take the rest of the report down.
            global _SECTIONS
            saved_sections = _SECTIONS
            def _boom(vaults, today, since_date):
                raise RuntimeError("boom")
            try:
                _SECTIONS = tuple((sid, title, _boom if sid == 4 else fn) for sid, title, fn in _SECTIONS)
                broken_report = build_report([vault], today=today)
            finally:
                _SECTIONS = saved_sections
            broken_by_id = {section["id"]: section for section in broken_report["sections"]}
            checks.append(("a broken section fails alone; siblings still report their numbers", (
                broken_by_id[4]["error"] is not None
                and broken_by_id[1]["error"] is None
                and broken_by_id[2]["error"] is None
                and broken_by_id[1]["counts"]["missing_aliases"] == 1
            )))

            # next steps surface the pending/draft/decision findings deterministically.
            checks.append(("next steps name the overdue pending line and the drafts", any(
                "殭屍待辦" in step for step in report["next_steps"]
            ) and any("草稿待審" in step for step in report["next_steps"])))

            # --dry-run prints to the given stream and writes nothing to disk.
            import io
            out = io.StringIO()
            code = main(["--dry-run", "--today", "2026-09-06", os.fspath(vault)], output=out)
            dream_dir = vault / ".epitype"
            existing_before = set(dream_dir.glob("dream_pack_*.md")) if dream_dir.is_dir() else set()
            checks.append(("--dry-run exits 0, prints the report, writes no pack file", (
                code == 0 and "Epitype Dream Pack" in out.getvalue() and existing_before == set()
            )))

            # default --out path and --json both work and agree on the numbers.
            views_current = vault / memspec.VIEWS_DIRECTORY / memspec.VIEWS_CURRENT_FILENAME
            views_dry_run_absent = not views_current.exists()
            code = main(["--today", "2026-09-06", os.fspath(vault)], output=io.StringIO())
            default_out = vault / ".epitype" / "dream_pack_20260906.md"
            checks.append(("default run writes the dated pack under <vault>/.epitype", code == 0 and default_out.is_file()))
            checks.append(("the run rebuilds the reading views on the way; --dry-run writes none", (
                views_dry_run_absent
                and views_current.is_file()
                and (vault / memspec.VIEWS_DIRECTORY / memspec.VIEWS_HISTORY_DIRECTORY
                     / memspec.VIEWS_CLOSED_FILENAME).is_file()
            )))

            custom_out = Path(temp_dir).resolve() / "custom_pack.json"
            out2 = io.StringIO()
            code = main(["--today", "2026-09-06", "--out", os.fspath(custom_out), "--json", os.fspath(vault)], output=out2)
            checks.append(("--out redirects the write target", code == 0 and custom_out.is_file()))
            parsed = json.loads(custom_out.read_text(encoding="utf-8"))
            parsed_by_id = {section["id"]: section for section in parsed["sections"]}
            checks.append(("--json output parses and matches the in-process counts", (
                parsed_by_id[6]["counts"] == by_id[6]["counts"]
                and parsed_by_id[3]["counts"] == by_id[3]["counts"]
            )))

            # --- 排程與狀態 ---
            gov = Path(temp_dir).resolve() / "gov"
            (gov / memspec.DREAM_DIRECTORY).mkdir(parents=True)
            state_file = gov / memspec.DREAM_DIRECTORY / memspec.DREAM_STATE_FILENAME
            md_out = gov / memspec.DREAM_DIRECTORY / memspec.DREAM_PACK_FILENAME
            js_out = gov / memspec.DREAM_DIRECTORY / memspec.DREAM_PACK_JSON_FILENAME
            code = main([
                "--today", "2026-09-06", "--out", os.fspath(md_out), "--json-out", os.fspath(js_out),
                os.fspath(vault),
            ], output=io.StringIO())
            written_state = json.loads(state_file.read_text(encoding="utf-8"))
            checks.append(("--json-out writes the JSON pack beside --out and both parse", (
                code == 0
                and md_out.is_file()
                and json.loads(js_out.read_text(encoding="utf-8"))["today"] == "2026-09-06"
            )))
            checks.append(("the run writes completion time, per-section counts and elapsed into dream_state.json", (
                memspec.is_iso_date(written_state[memspec.DREAM_STATE_COMPLETED_FIELD][:10])
                and written_state[memspec.DREAM_STATE_HEADLINE_FIELD]["card_fail"] >= 1
                and written_state[memspec.DREAM_STATE_HEADLINE_FIELD]["drafts"] == 2
                and written_state[memspec.DREAM_STATE_SECTIONS_FIELD]["3"]["zombie_lines"] == 1
                and isinstance(written_state[memspec.DREAM_STATE_ELAPSED_FIELD], float)
                and written_state[memspec.DREAM_STATE_PACK_FIELD] == os.fspath(md_out)
            )))

            # 時限是自己計時的：預算耗盡後剩下的節標成略過，而不是靜靜少一節。
            budget_report = build_report([vault], today=today, deadline=time.monotonic() - 1)
            checks.append(("an exhausted time budget marks every remaining section, and the default budget is 10 minutes", (
                memspec.DREAM_BUDGET_SECONDS == 600
                and all(section["error"] == TIME_BUDGET_ERROR for section in budget_report["sections"])
            )))

            # 已活過時間窗的程序仍是持有人；時間經過本身不授權搶鎖。
            first = acquire_lock(gov)
            second = acquire_lock(gov)
            stale_now = time.time() + memspec.DREAM_LOCK_STALE_SECONDS + 1
            checks.append(("one dream at a time; a live owner is protected even after the stale window", (
                first and not second and not acquire_lock(gov, now=stale_now)
            )))
            release_lock(gov, first)
            next_token = acquire_lock(gov)
            release_lock(gov, next_token)
            checks.append(("a released lock frees the next dream", (
                not (gov / memspec.DREAM_DIRECTORY / memspec.DREAM_LOCK_FILENAME).exists()
                and next_token
            )))

            checks.append(("due() is true when no dream ever finished and false inside the interval", (
                due({}, 24)
                and not due({memspec.DREAM_STATE_COMPLETED_FIELD:
                             datetime.now(timezone.utc).isoformat(timespec="seconds")}, 24)
                and due({memspec.DREAM_STATE_COMPLETED_FIELD: "2026-09-01T00:00:00+00:00"}, 24,
                        now=datetime(2026, 9, 6, tzinfo=timezone.utc).timestamp())
            )))

            launched = []
            spawned = spawn(gov, launcher=lambda argv, log_path: launched.append((argv, log_path)) or 4242)
            lock_body = json.loads((gov / memspec.DREAM_DIRECTORY / memspec.DREAM_LOCK_FILENAME).read_text(encoding="utf-8"))
            checks.append(("spawn takes the lock, records the child pid, and runs the one scheduled command form", (
                spawned
                and launched[0][0][1:] == [os.fspath(Path(__file__).resolve()),
                                           memspec.DREAM_SCHEDULED_FLAG, memspec.DREAM_LOCK_HELD_FLAG,
                                           "--lock-token", lock_body["token"]]
                and launched[0][1].name == memspec.DREAM_LOG_FILENAME
                and lock_body["pid"] == 4242
            )))
            checks.append(("a second spawn while the lock is held starts nothing", (
                not spawn(gov, launcher=lambda argv, log_path: launched.append((argv, log_path)) or 1)
                and len(launched) == 1
            )))
            # 假 launcher 沒有子程序可釋放；只清除這份 fixture 的 lease。
            (gov / memspec.DREAM_DIRECTORY / memspec.DREAM_LOCK_FILENAME).unlink()

            def _refuse(argv, log_path):
                raise OSError("no such file")

            refused = spawn(gov, launcher=_refuse)
            dream_log = (gov / memspec.DREAM_DIRECTORY / memspec.DREAM_LOG_FILENAME).read_text(encoding="utf-8")
            checks.append(("a launcher that fails leaves no lock behind and says so only in dream.log", (
                not refused
                and "spawn failed: OSError" in dream_log
                and not (gov / memspec.DREAM_DIRECTORY / memspec.DREAM_LOCK_FILENAME).exists()
            )))

            # --scheduled 自己從 config 解出登記庫、治理庫與輸出路徑，跑完釋放 lock。
            scheduled_config = Path(temp_dir).resolve() / "scheduled-config.json"
            scheduled_config.write_text(json.dumps({
                memspec.CONFIG_VAULTS_FIELD: [os.fspath(vault), os.fspath(gov)],
            }, ensure_ascii=False), encoding="utf-8")
            (gov / memspec.WORK_LEDGER_FILENAME).write_text("ledger\n", encoding="utf-8")
            saved_env = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
            os.environ[memspec.EPITYPE_CONFIG_ENV] = os.fspath(scheduled_config)
            try:
                token = acquire_lock(gov, handoff=True)
                scheduled_code = main([memspec.DREAM_SCHEDULED_FLAG, memspec.DREAM_LOCK_HELD_FLAG,
                                       "--lock-token", token,
                                       "--today", "2026-09-06"], output=io.StringIO())
                lock_released = not (gov / memspec.DREAM_DIRECTORY / memspec.DREAM_LOCK_FILENAME).exists()
                scheduled_state = state_file.read_text(encoding="utf-8")
                token = acquire_lock(gov)  # 假裝另一場夢正在跑
                blocked_code = main([memspec.DREAM_SCHEDULED_FLAG, "--today", "2026-09-06"], output=io.StringIO())
                blocked_state = state_file.read_text(encoding="utf-8")
            finally:
                release_lock(gov, token)
                if saved_env is None:
                    os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None)
                else:
                    os.environ[memspec.EPITYPE_CONFIG_ENV] = saved_env
            checks.append(("--scheduled resolves the ledger holder as governance and writes pack, JSON and state there", (
                scheduled_code == 0
                and json.loads(js_out.read_text(encoding="utf-8"))["vaults"] == [os.fspath(vault), os.fspath(gov)]
                and json.loads(scheduled_state)[memspec.DREAM_STATE_PACK_FIELD] == os.fspath(md_out)
            )))
            checks.append(("--lock-held releases the caller's lock at exit; a held lock makes the next scheduled run a no-op", (
                lock_released and blocked_code == 0 and blocked_state == scheduled_state
            )))
            checks.append(("狀態檔換名寫入：讀得到完整 JSON，旁邊不留 .tmp", (
                json.loads(scheduled_state)[memspec.DREAM_STATE_COMPLETED_FIELD]
                and not state_file.with_name(state_file.name + ".tmp").exists()
            )))

            # 2026-09-06 覆審：設定階段的例外走 `return 2`，繞過釋放 lock 的 finally，
            # 呼叫端交接過來的 lock 就留在原地擋掉之後每一場夢。
            saved_dream_root = dream_root
            root_calls = []

            def _dream_root_fails_after_handoff(governance):
                root_calls.append(governance)
                if len(root_calls) == 2:
                    raise OSError("governance path unavailable")
                return saved_dream_root(governance)

            os.environ[memspec.EPITYPE_CONFIG_ENV] = os.fspath(scheduled_config)
            try:
                token = acquire_lock(gov, handoff=True)
                globals()["dream_root"] = _dream_root_fails_after_handoff
                setup_code = main(
                    [memspec.DREAM_SCHEDULED_FLAG, memspec.DREAM_LOCK_HELD_FLAG,
                     "--lock-token", token],
                    output=io.StringIO(),
                )
            finally:
                globals()["dream_root"] = saved_dream_root
                release_lock(gov, token)
                if saved_env is None:
                    os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None)
                else:
                    os.environ[memspec.EPITYPE_CONFIG_ENV] = saved_env
            checks.append(("設定階段拋例外：回 2，交接來的 lock 仍然釋放", (
                setup_code == 2
                and len(root_calls) == 3
                and not (gov / memspec.DREAM_DIRECTORY / memspec.DREAM_LOCK_FILENAME).exists()
            )))

            # --- 主記憶整形（順路任務）---
            shaped = Path(temp_dir).resolve() / "shaped"
            shaped.mkdir()
            for stem in ("moved-one", "moved-two"):
                _write_card(
                    shaped / f"{stem}.md",
                    f"---\nname: {stem}\ndescription: 2026-09-01 synthetic\naliases:\n- {stem}\n---\nbody\n",
                )
            _views_module().generate(shaped, stamp="2026-09-09T00:00Z")
            index_path = shaped / memspec.MEMORY_INDEX_FILENAME
            # 三段短入口（兩段各帶一行合法的手寫連結）＋一段允許段以外、被塞進三行卡片
            # 連結：兩行的卡目錄承載得到，第三行指到還沒成為卡的檔案。
            index_text = (
                "# 短入口\n"
                "\n"
                "## 習慣與偏好\n"
                "- [moved-one](moved-one.md)\n"
                "\n"
                "## 找不到就搜\n"
                "- 先 memsearch，讀索引不算查過記憶。\n"
                "\n"
                "## 索引卡\n"
                "- [moved-two](moved-two.md)\n"
                "\n"
                "## 環境陷阱\n"
                "- [moved-one](moved-one.md)\n"
                "- [moved-two](moved-two.md)\n"
                "- [還沒生成目錄的新卡](pending-card.md)\n"
            )
            index_path.write_bytes(index_text.encode("utf-8"))
            pruned_dir = shaped.joinpath(*memspec.INDEX_PRUNED_SUBPATH)
            pruned_file = pruned_dir / "20260906.md"

            dry_out = io.StringIO()
            dry_code = main(["--dry-run", "--today", "2026-09-06", os.fspath(shaped)], output=dry_out)
            checks.append(("--dry-run 只印「會搬幾行」：MEMORY.md 一個位元組沒動，index_pruned 不存在", (
                dry_code == 0
                and index_path.read_bytes() == index_text.encode("utf-8")
                and not pruned_dir.exists()
                and "would-move｜搬出 2 行｜留下 1 行" in dry_out.getvalue()
            )))

            shaped_out = io.StringIO()
            shaped_code = main(["--today", "2026-09-06", os.fspath(shaped)], output=shaped_out)
            after_first = index_path.read_text(encoding="utf-8")
            pruned_text = pruned_file.read_text(encoding="utf-8")
            checks.append(("整形只搬允許段以外、目錄已承載的行；三段短入口一字不動", (
                shaped_code == 0
                and after_first == index_text.replace(
                    "- [moved-one](moved-one.md)\n- [moved-two](moved-two.md)\n"
                    "- [還沒生成目錄的新卡](pending-card.md)\n",
                    "- [還沒生成目錄的新卡](pending-card.md)\n",
                )
            )))
            checks.append(("移出的行原文照搬進 _drafts/index_pruned/YYYYMMDD.md，附時間、來源段與原因", (
                "- [moved-one](moved-one.md)" in pruned_text
                and "- [moved-two](moved-two.md)" in pruned_text
                and "「環境陷阱」" in pruned_text
                and memspec.INDEX_PRUNED_REASON in pruned_text
                and "pending-card" not in pruned_text
            )))

            second_pack = (shaped / ".epitype" / "dream_pack_20260906.md").read_text(encoding="utf-8")
            checks.append(("視圖沒列的那行留在 MEMORY.md，並列進夢報告的整形節", (
                "- [還沒生成目錄的新卡](pending-card.md)" in after_first
                and memspec.INDEX_SHAPING_HEADING in second_pack
                and "moved｜搬出 2 行｜留下 1 行" in second_pack
                and "pending-card.md" in second_pack.split(memspec.INDEX_SHAPING_HEADING, 1)[1]
                and memspec.INDEX_SHAPING_KEPT_STEP.format(count=1) in second_pack
            )))

            main(["--today", "2026-09-06", os.fspath(shaped)], output=io.StringIO())
            checks.append(("再跑一次沒有東西可搬：MEMORY.md 與 index_pruned 都不再變動", (
                index_path.read_text(encoding="utf-8") == after_first
                and pruned_file.read_text(encoding="utf-8") == pruned_text
            )))

            # 競爭情境：讀完之後、寫入之前檔案被別的寫者改動 → 整份放棄，一行都不搬。
            index_path.write_bytes(index_text.encode("utf-8"))
            saved_index_stat = _index_stat
            stat_calls = []

            def _index_stat_races(path):
                stat_calls.append(path)
                if len(stat_calls) == 2:
                    with open(path, "a", encoding="utf-8", newline="\n") as stream:
                        stream.write("- 別場 session 這時候加了一行\n")
                return saved_index_stat(path)

            race_out = io.StringIO()
            try:
                globals()["_index_stat"] = _index_stat_races
                main(["--today", "2026-09-06", os.fspath(shaped)], output=race_out)
            finally:
                globals()["_index_stat"] = saved_index_stat
            race_pack = (shaped / ".epitype" / "dream_pack_20260906.md").read_text(encoding="utf-8")
            checks.append(("寫入前 mtime 變了就整份放棄：行留在原地、index_pruned 沒長、報告記一行", (
                len(stat_calls) == 2
                and "- [moved-one](moved-one.md)\n- [moved-two](moved-two.md)\n"
                in index_path.read_text(encoding="utf-8")
                and pruned_file.read_text(encoding="utf-8") == pruned_text
                and memspec.INDEX_SHAPING_RACE_REASON in race_pack
                and memspec.INDEX_SHAPING_ABANDONED_STEP.format(count=1) in race_pack
            )))

            def _shaping_vault(name, stems, payload):
                """開一個只為整形用的小庫：幾張卡、一份目錄、一份指定位元組的 MEMORY.md。"""
                built = Path(temp_dir).resolve() / name
                built.mkdir()
                for stem in stems:
                    _write_card(
                        built / f"{stem}.md",
                        f"---\nname: {stem}\ndescription: 2026-09-01 synthetic\naliases:\n- {stem}\n---\nbody\n",
                    )
                _views_module().generate(built, stamp="2026-09-09T00:00Z")
                (built / memspec.MEMORY_INDEX_FILENAME).write_bytes(payload)
                return built, built / memspec.MEMORY_INDEX_FILENAME

            # 檔首 BOM：`## 索引卡` 前面多一個 BOM，第一行首字元就不是 `#`，第一個標題
            # 認不出來 → 整份檔被當成「不在任何段」，允許段裡的手寫行會被搬走。
            bom_bytes = "\ufeff## 索引卡\n- [bom-one](bom-one.md)\n- [bom-two](bom-two.md)\n".encode("utf-8")
            bom_vault, bom_index = _shaping_vault("bom", ("bom-one", "bom-two"), bom_bytes)
            main(["--today", "2026-09-06", os.fspath(bom_vault)], output=io.StringIO())
            checks.append(("檔首 BOM 不影響判段：允許段的兩行連結一個位元組沒動，index_pruned 也沒開", (
                bom_index.read_bytes() == bom_bytes
                and not bom_vault.joinpath(*memspec.INDEX_PRUNED_SUBPATH).exists()
            )))

            # 前言區（第一個 `##` 之前）：短入口的標題行與說明行本來就可能帶連結，一律
            # 不搬；同一份檔裡允許段以外的行照搬，證明這道保護沒有把整形關掉。
            pre_bytes = (
                "# 短入口\n"
                "- [pre-one](pre-one.md)\n"
                "\n"
                "## 環境陷阱\n"
                "- [pre-two](pre-two.md)\n"
            ).encode("utf-8")
            pre_vault, pre_index = _shaping_vault("preamble", ("pre-one", "pre-two"), pre_bytes)
            main(["--today", "2026-09-06", os.fspath(pre_vault)], output=io.StringIO())
            pre_pruned = (pre_vault.joinpath(*memspec.INDEX_PRUNED_SUBPATH) / "20260906.md").read_text(encoding="utf-8")
            checks.append(("第一個 `##` 之前的前言連結行不搬；同檔允許段以外的行照搬", (
                pre_index.read_bytes() == pre_bytes.replace(b"- [pre-two](pre-two.md)\n", b"")
                and "- [pre-two](pre-two.md)" in pre_pruned
                and "pre-one" not in pre_pruned
            )))

            # 紀錄檔＝原文照搬：CRLF 檔搬出的行連 \r\n 一起進 index_pruned；同一行出現在
            # 兩個段就是兩件事，去重會讓其中一段的證據消失。
            crlf_bytes = (
                "# t\r\n\r\n## 環境陷阱\r\n- [dup](dup-one.md)\r\n\r\n## 另一段\r\n- [dup](dup-one.md)\r\n"
            ).encode("utf-8")
            crlf_vault, crlf_index = _shaping_vault("verbatim", ("dup-one",), crlf_bytes)
            main(["--today", "2026-09-06", os.fspath(crlf_vault)], output=io.StringIO())
            crlf_pruned = (crlf_vault.joinpath(*memspec.INDEX_PRUNED_SUBPATH) / "20260906.md").read_bytes()
            checks.append(("index_pruned 逐位元組照搬：CRLF 行尾原樣留著，同一行分屬兩段就記兩筆", (
                crlf_pruned.count(b"- [dup](dup-one.md)\r\n") == 2
                and "「環境陷阱」".encode("utf-8") in crlf_pruned
                and "「另一段」".encode("utf-8") in crlf_pruned
                and crlf_index.read_bytes() == "# t\r\n\r\n## 環境陷阱\r\n\r\n## 另一段\r\n".encode("utf-8")
            )))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 40
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


# --------------------------------------------------------------------------- CLI


def main(argv=None, output=sys.stdout):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vaults", nargs="*", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--json-out", type=Path, default=None, help="除了 --out 之外，再把 JSON 報告寫到這裡")
    parser.add_argument("--state", type=Path, default=None,
                         help=f"完成後寫入的狀態檔；預設 <--out 目錄>/{memspec.DREAM_STATE_FILENAME}")
    parser.add_argument("--since", type=date.fromisoformat, default=None,
                         help=f"事件卡老化門檻 ISO 日期；預設今天往前 {DEFAULT_EVENT_AGING_DAYS} 天")
    parser.add_argument("--today", type=date.fromisoformat, default=None, help="ISO date override for reproducible runs")
    parser.add_argument("--dry-run", action="store_true", help="只印到 stdout，不寫檔")
    parser.add_argument(memspec.DREAM_SCHEDULED_FLAG, action="store_true",
                         help="排程／順路模式：自己從 config 解出登記庫與 .epitype 輸出路徑")
    parser.add_argument(memspec.DREAM_LOCK_HELD_FLAG, action="store_true",
                         help="接手呼叫端的 lock，必須同時提供 --lock-token")
    parser.add_argument("--lock-token", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--time-budget-seconds", type=float, default=memspec.DREAM_BUDGET_SECONDS,
                         help="自己計時的總時限，逾時剩下的節標成略過")
    parsed = parser.parse_args(arguments)

    started = time.monotonic()
    deadline = started + parsed.time_budget_seconds if parsed.time_budget_seconds > 0 else None
    governance = None
    lock_token = None
    out_path = parsed.out.resolve() if parsed.out else None
    json_out_path = parsed.json_out.resolve() if parsed.json_out else None
    state_path = parsed.state.resolve() if parsed.state else None
    try:
        try:
            if parsed.scheduled:
                vaults = configured_vaults()
                governance = governance_vault(vaults)
                if parsed.lock_held:
                    if not _handoff_lock(governance, parsed.lock_token):
                        log(governance, "skipped: lock handoff unavailable or no longer owned")
                        return 0
                    lock_token = parsed.lock_token
                root = dream_root(governance)
                out_path = out_path or root / memspec.DREAM_PACK_FILENAME
                json_out_path = json_out_path or root / memspec.DREAM_PACK_JSON_FILENAME
                state_path = state_path or root / memspec.DREAM_STATE_FILENAME
                if not lock_token:
                    lock_token = acquire_lock(governance)
                    if not lock_token:
                        log(governance, "skipped: another dream holds the lock")
                        return 0
            else:
                if not parsed.vaults:
                    parser.error("vaults are required unless " + memspec.DREAM_SCHEDULED_FLAG + " is given")
                vaults = [memsearch._resolve_vault(raw) for raw in parsed.vaults]
        except SystemExit:
            raise
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

        today = parsed.today or datetime.now(timezone.utc).date()
        since_date = parsed.since or (today - timedelta(days=DEFAULT_EVENT_AGING_DAYS))
        # 順路重生閱讀目錄：夜間整理已經在走每一個庫，而生成本身是「輸入指紋沒變就
        # 不寫」。失敗不影響審核包——目錄過期還有 card_lint --deep 的漏卡檢查會報。
        # 必須排在整形之前：整形的判準是「目錄已經承載這張卡」，判準本身不能是舊的。
        if not parsed.dry_run:
            views = _views_module()
            for vault in vaults:
                try:
                    views.generate(vault)
                except Exception:
                    continue
        shaping = []
        for vault in vaults:
            try:
                shaping.append(shape_index(vault, today, apply=not parsed.dry_run))
            except Exception as exc:
                shaping.append({
                    "vault": str(vault), "status": "error", "moved": 0, "kept": 0,
                    "moved_examples": [], "kept_examples": [], "pruned_path": None,
                    "reason": f"{type(exc).__name__}: {exc}",
                })
        report = build_report(
            vaults, today=today, since_date=since_date, deadline=deadline, shaping=shaping
        )
        rendered = json.dumps(report, ensure_ascii=False, indent=1)
        content = rendered if parsed.json else _render_markdown(report)

        if parsed.dry_run:
            print(content, file=output)
            return 0

        out_path = out_path or (vaults[0] / memspec.DREAM_DIRECTORY / f"dream_pack_{today.strftime('%Y%m%d')}.md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        if json_out_path is not None:
            json_out_path.parent.mkdir(parents=True, exist_ok=True)
            json_out_path.write_text(rendered, encoding="utf-8")
        _write_run_state(
            state_path or (out_path.parent / memspec.DREAM_STATE_FILENAME),
            report,
            out_path,
            time.monotonic() - started,
        )
        print(f"DREAM PACK {out_path}", file=output)
        return 0
    finally:
        if governance is not None and lock_token:
            release_lock(governance, lock_token)


def _write_run_state(path, report, out_path, elapsed):
    """本次嘗試收尾時間、各節數字、完整性——開場與排程共用這一份。
    部分交件仍按本次收尾時間節流，避免故障庫在每場開場被重啟；complete 才表示查完。
    notified_at 沿用舊值：那是上一場的通知紀錄，比對的是新的 completed_at。"""
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    now = datetime.now(timezone.utc)
    errors = _report_errors(report)
    value = {
        memspec.DREAM_STATE_COMPLETED_FIELD: now.isoformat(timespec="seconds"),
        memspec.DREAM_STATE_COMPLETED_EPOCH_FIELD: round(now.timestamp(), 3),
        memspec.DREAM_STATE_DATE_FIELD: report["today"],
        memspec.DREAM_STATE_ELAPSED_FIELD: round(elapsed, 3),
        memspec.DREAM_STATE_PACK_FIELD: os.fspath(out_path),
        memspec.DREAM_STATE_HEADLINE_FIELD: _headline(report),
        memspec.DREAM_STATE_COMPLETE_FIELD: not errors,
        memspec.DREAM_STATE_ERRORS_FIELD: errors,
        memspec.DREAM_STATE_SECTIONS_FIELD: {
            str(section["id"]): section.get("counts") or {} for section in report["sections"]
        },
        memspec.DREAM_STATE_NOTIFIED_FIELD: (previous or {}).get(memspec.DREAM_STATE_NOTIFIED_FIELD)
        if isinstance(previous, dict)
        else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先寫旁邊再換名：截斷式寫入被砍在中間（機器休眠、程序被 kill）會留下半個 JSON，
    # 而開場那一行與「距上次多久」只讀這一份，讀不動就當夢從沒跑過。
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(staging, path)


if __name__ == "__main__":
    raise SystemExit(main())
