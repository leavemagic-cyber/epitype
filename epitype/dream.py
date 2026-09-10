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
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import posixpath
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid

try:
    from . import (
        alias_batch, card_io, card_lint, decision_lint, gates_report, memsearch, memspec,
        pending_lint,
    )
except ImportError:  # Direct script execution keeps the CLI contract.
    import alias_batch
    import card_io
    import card_lint
    import decision_lint
    import gates_report
    import memsearch
    import memspec
    import pending_lint

EXAMPLE_LIMIT = 10
TIME_BUDGET_ERROR = "time budget exhausted before this section ran"
DEFAULT_EVENT_AGING_DAYS = 90
RECENT_WINDOW_DAYS = 7
DRAFT_DIRNAME = "_drafts"
_EVENT_TYPE_BY_DIR = dict(memspec.EVENT_CARD_DIRECTORIES)  # {"grants": "grant", ...}

# 第 8–12 節（U-K，owner 2026-09-09）：全部只列候選，一個檔都不搬、不改、不刪。夢的
# 這五節回答的是「有什麼東西沒經過確認就留在那裡」，處置一律是人的動作。
POCKET_VAULT_MAX_ROWS = 50
POCKET_VAULT_CANDIDATE = "歸戶候選"
POCKET_VAULT_NOTE = "只列候選：夢不搬、不改、不刪任何口袋庫的檔案"
POCKET_VAULT_COMMAND = "人工判斷每個口袋庫該歸哪一戶（登記進 config 的 vaults，或確認它就該留在原地）；沒有自動 CLI 指令"
DRAFT_AGING_WARN_DAYS = 7
# 第 4 節順手跑的 harvest（U-R2）：只產草稿，所以報告要說的是「這一趟實際多出幾張
# 提案」，不是「掃到幾句話」——沒有新增就是 0，不是「沒跑」。
HARVEST_DRAFTS_NOTE = "本次 harvest 新增 {drafts} 張草稿（掃過 {files} 個 transcript）"
HARVEST_DRY_RUN_NOTE = "本次 harvest 新增 {drafts} 張草稿（--dry-run：只算不寫，掃過 {files} 個 transcript）"
HARVEST_SKIPPED_NOTE = "本次未跑 harvest（呼叫端未給家目錄）：草稿數只反映既有檔案"
HARVEST_ERROR = "harvest（順手收割）失敗，草稿盤點照跑：{detail}"
DRAFT_AGING_STALE_DAYS = 30
DRAFT_OLDEST_ROWS = 5
DRAFT_ROOT_GROUP = "(root)"
DRAFT_AGING_COMMAND = "人工審閱最舊的那幾份 _drafts/**：轉正、歸檔或留著都行，但要有人看過"
MIXED_MAX_PER_VAULT = 50
MIXED_REASON_HEADINGS = "正文有 {count} 個 `## ` 小標"
MIXED_REASON_BYTES = "正文 {bytes} 位元組 > {cap}"
MIXED_REASON_DESCRIPTION = "description {chars} 字元且用「＋」「；」串了多件事"
MIXED_COMMAND = "人工判斷要不要拆成多張卡（一張卡＝一個記憶或規則）；夢不自動拆"
MIXED_SKIP_REVIEWED = "reviewed"
MIXED_SKIP_SUPERSEDED = "superseded"
MIXED_SKIP_FRESH_SPLIT = "fresh_split"
MIXED_SKIPPED_NOTE = "已審過略過 {count} 張"
CAP_UNSET_NOTE = "{field} 未設定，這一項跳過"
CAP_COMMAND = "人工判斷超上限的檔案要精簡還是提高上限；夢不改檔"
# 同一批 core_files 順路多問一句：這個檔跟規則卡重組出來的生成塊還一不一致。
# 只列候選——夢不重生成核心塊，那是本機作業（`epitype core-gen`）。
CAP_DRIFT_KIND = "generated-block-drift"
CAP_DRIFT_COMMAND = "人工判斷生成塊漂移：重跑 epitype core-gen，或查誰手改了生成塊；夢不改檔"
DECISION_CARRIER_FIELDS = ("source", memspec.SUPERSEDED_BY_FIELD, memspec.ALIASES_FIELD)
UNCARRIED_MAX_PER_VAULT = 30
UNCARRIED_EXCERPT_CHARS = 80
# 逐字引用要算承接，重疊的部分至少要有這麼多個字：短重疊（「好」「先對過帳」）在任何
# 兩段中文裡都撞得到，用它當承接證據等於把這一節關掉。
UNCARRIED_QUOTE_MIN_CHARS = 12
# owner_quote 常把好幾段原話串在同一欄（`「B」（Q7）；「…」`），所以要先切開再比對。
# 界線用 unicode 的引號／括號類別判斷（Pi/Pf/Ps/Pe），只有 ASCII 的 " 與 ' 是 Po、
# 類別認不出來，才另外列；不寫死某一種語言的引號。
QUOTE_DELIMITER_CATEGORIES = ("Pi", "Pf", "Ps", "Pe")
QUOTE_DELIMITER_CHARS = "\"'"
UNCARRIED_COMMAND = "人工判斷這句原話該不該升成決策卡（或標 verified: false 降級）；夢不自動升卡"

# 第 15 節（U-P，owner 2026-09-09「應該有回饋檢討機制」）：把四種來源的證據對齊到卡上
# 並數次數。只列候選——不判型別（A／B／C 由檢討場判）、不改卡、不動層、不注入對話。
REVIEW_PACK_SECTION_ID = 15
REVIEW_PACK_TITLE = "檢討包 / review pack"
REVIEW_PACK_MAX_ROWS = 50
REVIEW_PACK_BLOCK_KINDS = (memspec.STOP_GATE_LOG_KIND, memspec.WRITE_GATE_LOG_KIND)
REVIEW_PACK_READY_NOTE = "檢討包達門檻（{count}/{trigger}）"
REVIEW_PACK_BELOW_NOTE = "未達門檻（{count}/{trigger}）"
REVIEW_PACK_NEXT_STEP = (
    "檢討包達門檻（{count}/{trigger}）→ 人工開一場檢討（Claude 整理＋Codex 挑戰，"
    "owner 一包核決）；夢只給候選，不判型別、不改卡、不動層"
)
REVIEW_PACK_COMMAND = "人工逐列判來源、原因與最小修法；沒有對應的自動 CLI 指令"
REVIEW_PACK_UNMAPPED_NOTE = "{count} 則事件沒有 {field} 欄，對不到卡（不強迫每場搜）"
REVIEW_PACK_UNVERIFIED_NOTE = "{count} 則事件 {field}: {value}（待核，不算已核實事故）"
REVIEW_PACK_NO_EXAM_NOTE = "沒有 {filename}，這一節的考題失敗數是「未量」而不是 0"
REVIEW_PACK_EXAM_UNMAPPED_NOTE = "{count} 題失敗但題目沒寫對到哪張卡"


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


def _section_missing_aliases(vaults, today, since_date, config):
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


def _section_card_lint(vaults, today, since_date, config):
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


def _section_pending(vaults, today, since_date, config):
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


def _harvest_drafts(vaults, context):
    """夢順手跑一趟 harvest，只產草稿。

    排在盤點之前，這一趟新產的提案才會被下面的盤點數進去。回傳（新增草稿數、掃過的
    transcript 數、錯誤行）——失敗只回一行錯誤，第 4 節與其餘各節照跑（fail-open）。
    """
    context = context or {}
    home = context.get("home")
    if home is None:
        return None, 0, []          # 沒有家目錄就沒有 transcript 可讀：不跑，並在備註說出來
    deadline = time.monotonic() + memspec.DREAM_HARVEST_BUDGET_SECONDS
    overall = context.get("deadline")
    if overall is not None:
        deadline = min(deadline, overall)
    try:
        # harvest 的 --dry-run 會逐行印 WOULD PROPOSE：那是它自己 CLI 的輸出，夢的報告
        # 只要數字。吞掉 stdout，夢的 --dry-run 才不會把報告和收割日誌混在同一條流裡。
        with redirect_stdout(io.StringIO()):
            counts, _governance, _routed = _harvest_module().harvest(
                home, vaults, drafts_only=True, deadline=deadline,
                dry_run=bool(context.get("dry_run")),
            )
    except Exception as exc:
        return None, 0, [HARVEST_ERROR.format(detail=f"{type(exc).__name__}: {exc}")]
    return counts.get("drafts", 0), counts.get("files", 0), []


def _section_drafts(vaults, today, since_date, config, context=None):
    # 收割先跑、盤點後跑：順序反了的話今晚新產的提案要等明晚才被數到。
    new_drafts, harvested_files, harvest_errors = _harvest_drafts(vaults, context)
    results, errors = _bounded(vaults, _drafts_of)
    errors = [*harvest_errors, *errors]
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
    if new_drafts is None:
        note = HARVEST_SKIPPED_NOTE
    elif (context or {}).get("dry_run"):
        note = HARVEST_DRY_RUN_NOTE.format(drafts=new_drafts, files=harvested_files)
    else:
        note = HARVEST_DRAFTS_NOTE.format(drafts=new_drafts, files=harvested_files)
    return {
        "counts": {
            "total_drafts": len(entries),
            "by_subdir": by_subdir,
            "captured_pending": by_subdir.get(pending_root, 0),
            "harvest_new_drafts": new_drafts,
            "harvest_files": harvested_files,
        },
        "examples": entries[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
        "note": note,
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

    def has_forbidden(path):
        front_lines = stop_gate._decision_frontmatter(path)
        if front_lines is None:
            return False
        values = stop_gate._sequence_fields(
            front_lines, memspec.TOP_LEVEL_FIELD, memspec.split_flow_items
        )
        return bool(values[memspec.FORBIDDEN_FIELD])

    return has_forbidden


def _section_decisions(vaults, today, since_date, config):
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


def _section_event_aging(vaults, today, since_date, config):
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


def _section_recent(vaults, today, since_date, config):
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


# --------------------------------------------------------------------------- section 8


def _projects_roots(vaults):
    """可能裝著口袋庫的 `<家目錄>/.claude/projects` 目錄，以及各自要找的庫目錄名。

    家目錄不寫死：先從每個登記庫的路徑往上找 `.claude/projects` 這一對目錄名——認得
    出來就連庫目錄名（`memory`）也一起從那個登記庫身上讀，換 slug 規則不必改程式。
    一個登記庫都認不出來時（庫登記在別處）才退回 HOME 底下的同一個位置。

    只認這個形狀是刻意的：改成「登記庫的祖父目錄就是根」會讓一個放在 `C:\\a\\b` 的
    庫把 `C:\\` 底下每個目錄都當成專案目錄掃一遍。
    """
    roots = {}
    for vault in vaults:
        path = Path(vault).resolve()
        for parent in path.parents:
            if (
                parent.name == memspec.HOST_PROJECTS_DIRECTORY
                and parent.parent.name == memspec.HOST_STATE_DIRECTORY
                and parent.is_dir()
            ):
                roots.setdefault(parent, set()).add(path.name)
                break
    fallback = Path.home() / memspec.HOST_STATE_DIRECTORY / memspec.HOST_PROJECTS_DIRECTORY
    if fallback.is_dir():
        roots.setdefault(fallback.resolve(), set()).add(memspec.HOST_MEMORY_DIRECTORY)
    return roots


def _pocket_vault_cards(directory):
    """(*.md 卡數, 最新 mtime)——沿用 vault 掃描邊界：`_`／`.` 開頭的路徑段不算。

    只數檔案、不解 frontmatter：這一節要回答的是「這裡有沒有東西沒人管」，一個
    ~200 個專案目錄的家目錄不該為此付整庫解析的錢（夢有十分鐘總預算）。
    """
    count = 0
    newest = None
    pending = [os.fspath(directory)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                listed = list(entries)
        except OSError:
            continue
        for entry in listed:
            if entry.name.startswith(("_", ".")):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                # 連結／junction 一律不進去也不計數，與 memsearch 的 vault 邊界同一條
                # 規則：一個連結形狀的項目不該把目錄外的檔案算成這個庫的卡。
                if memsearch._is_link(entry, info):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif entry.name.lower().endswith(".md"):
                    count += 1
                    newest = info.st_mtime if newest is None else max(newest, info.st_mtime)
            except OSError:
                continue
    return count, newest


def _section_pocket_vaults(vaults, today, since_date, config):
    """未登記卻裝著卡的目錄＝口袋庫；只列歸戶候選，不搬不改。

    owner 2026-09-09：「我不知道會有多少地方在產生非整理的記憶」。登記清單答不了這個
    問題——它只列得出已經知道的庫。所以這一節反過來從磁碟數，再扣掉登記的。
    """
    # 登記清單直接讀設定，不走 configured_vaults()：那個入口為「一個庫都沒登記」丟
    # 例外，而在這裡「沒登記」正是要回答的問題，不是這一節跑不下去的理由。
    registered = {Path(vault).resolve() for vault in vaults}
    raw_registered = config.get(memspec.CONFIG_VAULTS_FIELD)
    for item in (raw_registered if isinstance(raw_registered, list) else ()):
        if not isinstance(item, str) or not item.strip():
            continue
        try:
            registered.add(Path(item).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    errors = []
    found = []
    seen = set()  # 兩個根指到同一個目錄時只算一次。
    roots = _projects_roots(vaults)
    for root, leaves in sorted(roots.items()):
        try:
            with os.scandir(root) as entries:
                projects = sorted(entry.path for entry in entries if entry.is_dir(follow_symlinks=False))
        except OSError as exc:
            errors.append(f"{root}: {type(exc).__name__}: {exc}")
            continue
        for project in projects:
            for leaf in sorted(leaves):
                candidate = Path(project) / leaf
                if not candidate.is_dir():
                    continue
                resolved = candidate.resolve()
                if resolved in registered or resolved in seen:
                    continue
                seen.add(resolved)
                cards, newest = _pocket_vault_cards(resolved)
                if cards < 1:
                    continue
                found.append({
                    "path": str(resolved),
                    "cards": cards,
                    "newest_mtime": datetime.fromtimestamp(newest, tz=timezone.utc).isoformat(timespec="seconds"),
                    "candidate": POCKET_VAULT_CANDIDATE,
                })
    found.sort(key=lambda item: (-item["cards"], item["path"]))
    return {
        "counts": {
            "pocket_vaults": len(found),
            "pocket_cards": sum(item["cards"] for item in found),
            "roots_scanned": len(roots),
        },
        "examples": found[:POCKET_VAULT_MAX_ROWS],
        "commands": [POCKET_VAULT_COMMAND] if found else [],
        "errors": errors,
        "note": POCKET_VAULT_NOTE,
    }


# --------------------------------------------------------------------------- section 9


def _draft_aging_of(vault, today):
    entries = []
    for path in _drafts_of(vault):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        age = (today - datetime.fromtimestamp(stamp, tz=timezone.utc).date()).days
        relative = path.relative_to(vault).as_posix()
        parts = relative.split("/")
        entries.append({
            "vault": str(vault),
            "path": relative,
            "subdir": parts[1] if len(parts) > 2 else DRAFT_ROOT_GROUP,
            "age_days": age,
        })
    return entries


def _section_draft_aging(vaults, today, since_date, config):
    """草稿的年齡分布。第 4 節數的是「有幾份待審」，這一節數的是「積了多久」。

    2026-09-09 實測治理庫積了 370 份草稿沒人管；份數本身不會告訴你那是昨天的一批還是
    半年前就躺在那裡，而後者才是「沒經過確認的東西回不到該在的層」的樣子。
    """
    results, errors = _bounded(vaults, lambda vault: _draft_aging_of(vault, today))
    entries = []
    by_subdir = {}
    for _vault, found in results:
        for item in found:
            entries.append(item)
            bucket = by_subdir.setdefault(item["subdir"], {"total": 0, "over_7": 0, "over_30": 0})
            bucket["total"] += 1
            if item["age_days"] > DRAFT_AGING_WARN_DAYS:
                bucket["over_7"] += 1
            if item["age_days"] > DRAFT_AGING_STALE_DAYS:
                bucket["over_30"] += 1
    entries.sort(key=lambda item: (-item["age_days"], item["vault"], item["path"]))
    over_7 = sum(1 for item in entries if item["age_days"] > DRAFT_AGING_WARN_DAYS)
    over_30 = sum(1 for item in entries if item["age_days"] > DRAFT_AGING_STALE_DAYS)
    return {
        "counts": {
            "total_drafts": len(entries),
            "over_7_days": over_7,
            "over_30_days": over_30,
            "by_subdir": by_subdir,
        },
        "examples": entries[:DRAFT_OLDEST_ROWS],
        "commands": [DRAFT_AGING_COMMAND] if over_7 else [],
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 10


def _body_of(text):
    """卡片正文（frontmatter 之後）；沒有 frontmatter 就整份都是正文。"""
    stripped = text.lstrip("\N{ZERO WIDTH NO-BREAK SPACE}")
    _front, closing = memspec.split_frontmatter(stripped)
    lines = stripped.splitlines()
    return "\n".join(lines if closing is None else lines[closing + 1:])


def _body_heading_count(body):
    """正文裡的 `## ` 小標數；```圍籬內的不算（卡片會貼 markdown 範例）。"""
    count = 0
    fenced = False
    for line in body.splitlines():
        if line.strip().startswith(memspec.GRANT_FENCED_CODE_MARKER):
            fenced = not fenced
            continue
        if not fenced and line.startswith("## "):
            count += 1
    return count


def _mixed_skip_reason(fields, headings, body_bytes):
    """這張卡為什麼不必再列為候選；沒有理由就回 None。

    2026-09-09 U-K3：三個訊號是形狀，人審是判斷，形狀不該壓過判斷。已標
    `mixed_reviewed`（值不看，只看鍵在不在，才不綁任何語言）、已被取代的卡、以及
    剛拆出來就只有一個小標又不超上限的新卡，都已經有人看過，再列一次只是把清單
    變成永遠清不掉的雜訊。新卡若自己長成兩個小標或超上限，那是新卡自己的問題，
    照樣列——這個豁免只赦免 description 那一條。
    """
    if memspec.MIXED_REVIEWED_FIELD in fields:
        return MIXED_SKIP_REVIEWED
    status = fields.get(memspec.DECISION_STATUS_FIELD, "")
    if isinstance(status, str) and status.strip() == memspec.SUPERSEDED_DECISION_STATUS:
        return MIXED_SKIP_SUPERSEDED
    origin = fields.get(memspec.SPLIT_FROM_FIELD, "")
    if (
        isinstance(origin, str)
        and origin.strip()
        and headings < memspec.CARD_MIXED_HEADING_MIN
        and body_bytes <= memspec.CARD_BODY_MIXED_BYTES
    ):
        return MIXED_SKIP_FRESH_SPLIT
    return None


def _mixed_cards_of(vault):
    flagged = []
    skipped = {}
    for path in memsearch.card_files(vault):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        fields, _problem = memspec.frontmatter_text(text)
        body = _body_of(text)
        reasons = []
        headings = _body_heading_count(body)
        body_bytes = len(body.encode("utf-8"))
        if headings >= memspec.CARD_MIXED_HEADING_MIN:
            reasons.append(MIXED_REASON_HEADINGS.format(count=headings))
        if body_bytes > memspec.CARD_BODY_MIXED_BYTES:
            reasons.append(MIXED_REASON_BYTES.format(bytes=body_bytes, cap=memspec.CARD_BODY_MIXED_BYTES))
        description = fields.get(memspec.DESCRIPTION_FIELD, "")
        if (
            len(description) > memspec.CARD_MIXED_DESCRIPTION_MAX_CHARS
            and any(joiner in description for joiner in memspec.CARD_MIXED_DESCRIPTION_JOINERS)
        ):
            reasons.append(MIXED_REASON_DESCRIPTION.format(chars=len(description)))
        if not reasons:
            continue
        # 略過只在「這張本來會被列」時才計數：報告那一行說的是人審從清單上拿掉幾張，
        # 把從來就沒上榜的卡也算進去，那個數字就對不上清單的前後差。
        skip = _mixed_skip_reason(fields, headings, body_bytes)
        if skip is not None:
            skipped[skip] = skipped.get(skip, 0) + 1
            continue
        flagged.append({
            "vault": str(vault),
            "path": path.relative_to(vault).as_posix(),
            "reasons": reasons,
        })
    flagged.sort(key=lambda item: item["path"])
    return flagged, skipped


def _section_mixed_cards(vaults, today, since_date, config):
    """拆卡候選：一張卡看起來裝了不只一件事。夢只列，不拆——拆是人的判斷。"""
    results, errors = _bounded(vaults, _mixed_cards_of)
    examples = []
    by_vault = {}
    total = 0
    skipped_total = 0
    skipped_by_reason = {}
    for vault, (flagged, skipped) in results:
        by_vault[str(vault)] = len(flagged)
        total += len(flagged)
        examples.extend(flagged[:MIXED_MAX_PER_VAULT])
        for reason, count in skipped.items():
            skipped_total += count
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + count
    return {
        "counts": {
            "mixed_cards": total,
            "reviewed_skipped": skipped_total,
            "skipped_by_reason": skipped_by_reason,
            "by_vault": by_vault,
            "listed_cap_per_vault": MIXED_MAX_PER_VAULT,
        },
        "examples": examples,
        "commands": [MIXED_COMMAND] if total else [],
        "note": MIXED_SKIPPED_NOTE.format(count=skipped_total),
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 11


def _positive_int(value):
    """設定裡的位元組上限；不是正整數就當沒設（True 是 int 的子類，要擋掉）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _section_caps(vaults, today, since_date, config):
    """上限＝檢討值＋兩成（owner 2026-09-09）。夢只查有沒有超過，超過只列候選。

    三個鍵都選填，而且缺鍵一律寫一行「未設定」而不是套內建門檻：一個產品猜出來的
    上限被寫成「超上限」，讀的人會以為那是 owner 的判斷。
    """
    notes = []
    entries = []
    errors = []
    index_cap = _positive_int(config.get(memspec.CONFIG_INDEX_CAP_BYTES_FIELD))
    if index_cap is None:
        notes.append(CAP_UNSET_NOTE.format(field=memspec.CONFIG_INDEX_CAP_BYTES_FIELD))
    else:
        for vault in vaults:
            path = Path(vault) / memspec.MEMORY_INDEX_FILENAME
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > index_cap:
                entries.append({
                    "path": str(path), "bytes": size, "cap": index_cap, "over": size - index_cap,
                })
    core_files = config.get(memspec.CONFIG_CORE_FILES_FIELD)
    core_cap = _positive_int(config.get(memspec.CONFIG_CORE_CAP_BYTES_FIELD))
    named = [
        Path(raw).expanduser()
        for raw in (core_files if isinstance(core_files, list) else ())
        if isinstance(raw, str) and raw.strip()
    ]
    if not named:
        notes.append(CAP_UNSET_NOTE.format(field=memspec.CONFIG_CORE_FILES_FIELD))
    elif core_cap is None:
        notes.append(CAP_UNSET_NOTE.format(field=memspec.CONFIG_CORE_CAP_BYTES_FIELD))
    else:
        for path in named:
            try:
                size = path.stat().st_size
            except OSError as exc:
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if size > core_cap:
                entries.append({
                    "path": str(path), "bytes": size, "cap": core_cap, "over": size - core_cap,
                })
    entries.sort(key=lambda item: -item["over"])
    drift = _core_drift(vaults, named)
    return {
        "counts": {"over_cap": len(entries), "unset_keys": len(notes), "drift": len(drift)},
        "examples": entries + drift,
        "commands": ([CAP_COMMAND] if entries else []) + ([CAP_DRIFT_COMMAND] if drift else []),
        "errors": errors,
        "note": "；".join(notes) if notes else None,
    }


def _core_drift(vaults, core_files):
    """生成塊漂移候選：`core_files` 每個檔與規則卡重組出來的核心塊比一次。

    `core_cap_bytes` 沒設也照比——上限與漂移是兩件事。判準與本機的 drift check 同一支
    （`core_gen.drifted`），「無從判斷」（沒有規則卡、讀不到檔）不算漂移，否則沒有規則卡
    的機器每晚都會收到一則固定的假候選。
    """
    if not core_files:
        return []
    try:  # lazy import：核心生成器只有這一節要，夢的其餘各節不為它付 import
        from . import core_gen
    except ImportError:  # Direct script execution keeps the CLI contract.
        import core_gen

    found = []
    for path in core_files:
        try:
            if core_gen.drifted(vaults, path):
                found.append({"path": str(path), "kind": CAP_DRIFT_KIND})
        except Exception:
            continue  # 一個讀不了的檔不得讓整節失敗；缺口由 errors 以外的節照常報
    return found


# --------------------------------------------------------------------------- section 12


def _carry_normalized(text):
    """比對承接用的正規化：先收全形／半形，再只留字母、數字與結合記號。

    去空白、去引號、統一標點是同一個動作——凡不是「字」的字元一律不留，所以不必
    列舉「」『』"" 或任何一種語言的標點，換一種語言的原話走的還是這一條路。
    """
    folded = unicodedata.normalize("NFKC", text)
    return "".join(char for char in folded if unicodedata.category(char)[0] in "LNM").casefold()


def _quote_fragments(raw):
    """一張決策卡 owner_quote 裡夠長的逐字片段：整欄，加上引號界起來的每一段。

    只比對整欄會漏掉最常見的寫法——同一欄串了好幾段原話，每一段各有出處，整欄
    自然不是任何一份原話的子字串。切開之後夾在引號之間的雜訊（`（Q7）；`）也會
    變成片段，但長度不足就進不了集合，不會冒充承接證據。
    """
    pieces = [raw]
    current = []
    for char in raw:
        if unicodedata.category(char) in QUOTE_DELIMITER_CATEGORIES or char in QUOTE_DELIMITER_CHARS:
            if current:
                pieces.append("".join(current))
                current = []
            continue
        current.append(char)
    pieces.append("".join(current))
    normalized = {_carry_normalized(piece) for piece in pieces}
    return {piece for piece in normalized if len(piece) >= UNCARRIED_QUOTE_MIN_CHARS}


def _decision_carriers(vault):
    """(承接文字, owner_quote 逐字片段)：判斷「這句原話有沒有被接住」的兩份證據。

    問的是「有沒有任何一張決策卡接住它」，不必知道是哪一張。第一份是提名——決策卡
    的正文與 source／superseded_by／aliases 值連成一份大字串，提到檔名或 decision_key
    就算承接。第二份是逐字引用：真庫 19 張決策卡全部把原話抄進 owner_quote，多數
    一個檔名都沒寫——只認第一份，FAILURE_MODES §36 寫成那天兩庫的事件卡是 91／53，
    也就是全部被列成「無人承接」。第三條路在事件卡那一端（`carried_by`）。
    """
    chunks = []
    fragments = set()
    for path in memsearch.card_files(vault):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        card_type, fields = card_lint.card_type_of(path.relative_to(vault).as_posix(), text, path)
        if card_type != memspec.CARD_TYPE_DECISION:
            continue
        chunks.append(_body_of(text))
        for field in DECISION_CARRIER_FIELDS:
            value = fields.get(field, "")
            if value:
                chunks.append(value)
        quote = fields.get(memspec.OWNER_QUOTE_FIELD, "")
        if quote:
            fragments |= _quote_fragments(quote)
    return "\n".join(chunks), fragments


def _quoted_verbatim(body, fragments):
    """決策卡的 owner_quote 是否逐字引了這份原話（任一方向的子字串都算）。

    兩個方向都要：決策卡可能只引原話的一段（引用較短），也可能把一句短原話抄進一段
    較長的引文裡（原話較短）。兩邊都得先過 `UNCARRIED_QUOTE_MIN_CHARS` 這道長度地板。
    """
    normalized = _carry_normalized(body)
    if len(normalized) < UNCARRIED_QUOTE_MIN_CHARS:
        return False
    return any(fragment in normalized or normalized in fragment for fragment in fragments)


def _noise_marker_of(body):
    """命中的雜訊樣板（`memspec.EVENT_NOISE_MARKERS`），沒命中就是空字串。

    只標記、不改檔：自動捕捉把跨 CLI 傳輸探針的 payload 整段寫成 ruling，讀起來像
    owner 的裁定，但那不是 owner 說的話。刪不刪、降不降級仍然是人的判斷。
    """
    folded = body.casefold()
    for marker in memspec.EVENT_NOISE_MARKERS:
        if marker.casefold() in folded:
            return marker
    return ""


def _uncarried_quotes_of(vault, carriers):
    carried_text, quote_fragments = carriers
    found = []
    for directory, _card_type in memspec.EVENT_CARD_DIRECTORIES:
        root = Path(vault) / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                continue
            fields, _problem = memspec.frontmatter_text(text)
            verified = fields.get(memspec.VERIFIED_FIELD, "").strip().casefold()
            if verified == memspec.VERIFIED_FALSE:
                continue  # 卡面已經自報「未經人核」，不是這一節的事。
            key = fields.get(memspec.DECISION_KEY_FIELD, "").strip()
            # 比對用完整檔名（帶 .md）與 decision_key，不用去掉副檔名的字根：`carried`
            # 是 `uncarried` 的子字串，用字根比對會把「沒人承接」誤判成「有人承接」，
            # 而這一節寧可多列一份給人看，也不能漏掉一句冒充裁定的原話。
            names = [path.name] + ([key] if key else [])
            if any(name in carried_text for name in names):
                continue
            # 事件卡自報承接者：卡面上寫了 carried_by 就是有人指名接住它，不必再
            # 回頭確認那張決策卡在不在——卡在不在是 `epitype decisions` 的題目。
            if fields.get(memspec.CARRIED_BY_FIELD, "").strip():
                continue
            body = _body_of(text)
            if _quoted_verbatim(body, quote_fragments):
                continue
            flat = " ".join(body.split())
            found.append({
                "vault": str(vault),
                "path": path.relative_to(vault).as_posix(),
                "captured_at": fields.get(memspec.CAPTURED_AT_FIELD, "").strip(),
                "noise": _noise_marker_of(body),
                "excerpt": flat[:UNCARRIED_EXCERPT_CHARS],
            })
    found.sort(key=lambda item: (item["captured_at"], item["path"]), reverse=True)
    return found


def _section_uncarried_quotes(vaults, today, since_date, config):
    """升決策卡候選：沒有任何決策卡承接的原話。

    U-H 之後喚回不再注入 `rulings/`／`corrections/`／`grants/` 的原話檔
    （adapters/claude/recall_hook.py `_event_card`），所以一張沒人承接的原話不再
    自己送到現場——但它也就沒有任何到達路徑了：卡片層若沒人把它寫成決策卡／規則卡，
    那句話等於只留在檔案裡等人搜。這一節列的就是這種缺口，也是移除注入的前置條件。

    承接有三條路，任一成立就不列：決策卡提名（檔名或 decision_key）、決策卡在
    owner_quote 逐字引了它、事件卡自己寫了 `carried_by`。每一列另附「疑似雜訊」欄，
    標出讀起來像裁定、其實是傳輸探針樣板的那幾張——標記而已，夢一個檔都不改。
    """
    results, errors = _bounded(
        vaults, lambda vault: _uncarried_quotes_of(vault, _decision_carriers(vault))
    )
    examples = []
    by_vault = {}
    total = 0
    noise = 0
    for vault, found in results:
        by_vault[str(vault)] = len(found)
        total += len(found)
        noise += sum(1 for item in found if item["noise"])
        examples.extend(found[:UNCARRIED_MAX_PER_VAULT])
    return {
        "counts": {
            "uncarried_quotes": total, "noise_candidates": noise,
            "by_vault": by_vault, "listed_cap_per_vault": UNCARRIED_MAX_PER_VAULT,
        },
        "examples": examples,
        "commands": [UNCARRIED_COMMAND] if total else [],
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 15


def _review_events_of(vault):
    """庫裡每張事件卡的證據列：對到哪張卡、是哪一則事件、什麼時候、核實了沒。

    `matched_card` 是「這一則糾正明確指到哪一條規則」；收斂第 2 條刻意不強迫每場搜，
    所以沒有那一欄就是對不到卡——那種列進「未對到卡」，不會被算到某張卡頭上。
    `event_id` 沒有的舊卡用它的路徑當身分：一張卡至少是一則事件，去重時不能互相蓋掉。
    """
    rows = []
    for path in memsearch.card_files(vault):
        relative = path.relative_to(vault).as_posix()
        if _EVENT_TYPE_BY_DIR.get(relative.split("/", 1)[0]) is None:
            continue
        fields, _problem = memspec.frontmatter_fields(path)
        rows.append({
            "card": fields.get(memspec.MATCHED_CARD_FIELD, "").strip(),
            # 事件身分跨庫比對：event_id 已經含宿主與對話，同一則事件被寫進兩個庫也只
            # 算一次；沒有 event_id 的舊卡退回「這個庫的這個路徑」。
            "event": fields.get(memspec.EVENT_ID_FIELD, "").strip() or f"{vault}::{relative}",
            "date": fields.get(memspec.CAPTURED_AT_FIELD, "").strip()[:10],
            # 只有卡面自報 `verified: false` 才算未核（沒有這一欄的是人手寫的卡）；
            # 判法與第 12 節、喚回端的降級規則同一條。
            "verified": fields.get(memspec.VERIFIED_FIELD, "").strip().casefold()
            != memspec.VERIFIED_FALSE,
        })
    return rows


def _review_blocks_of(vault):
    """`_GATE_LOG.jsonl` 裡 Stop 閘與寫檔閘擋下的列，帶它記到的裁定鍵或卡名。

    日誌的讀法沿用 epitype/gates_report.py（同一份 kind／label 語意），這一節才不會
    對同一個檔算出跟擋下報告不同的數字。
    """
    rows, _bad = gates_report.load_rows(Path(vault) / memspec.GATE_LOG_FILENAME)
    return [
        {
            "card": row.label,
            "date": row.timestamp.astimezone(timezone.utc).date().isoformat(),
        }
        for row in rows
        if row.kind in REVIEW_PACK_BLOCK_KINDS and row.label
    ]


def _exam_results(governance):
    """治理庫裡最近一次考題結果，讀不到就 None（＝未量，不是零失敗）。"""
    path = Path(governance) / memspec.DREAM_DIRECTORY / memspec.EXAM_RESULTS_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    results = value.get("results")
    if not isinstance(value, dict) or not isinstance(results, list):
        return None
    failures = []
    for item in results:
        if not isinstance(item, dict) or item.get("passed") is not False:
            continue
        raw = item.get("cards")
        failures.append({
            "id": str(item.get("id", "")),
            "cards": [
                text.strip() for text in (raw if isinstance(raw, list) else ())
                if isinstance(text, str) and text.strip()
            ],
        })
    return {
        "rules_version": str(value.get("rules_version", "")),
        "date": str(value.get("generated_at", ""))[:10],
        "failures": failures,
    }


def _section_review_pack(vaults, today, since_date, config, sections):
    """檢討包：被證據指到的每張卡一列，加上「有幾件事在等人判」。

    來源四種：事件卡（`matched_card`／`event_id`／`verified`）、閘門紀錄的 stop_block
    與 write_block、考題結果檔的失敗題、以及第 8–12 節的候選計數。

    「待判問題」＝列出來的卡數，因為檢討場是逐列判的：一列＝一張卡＋它身上所有的證據。
    兩種東西刻意不進這個數字，否則同一件事會被算兩次、門檻也會永遠成立——(1) 第 8–12
    節的候選（那五節各自已經有自己的下一步行）；(2) 對不到卡的事件（沒有 `matched_card`
    就沒有列可判，而「這句原話沒有決策卡承接」正是第 12 節在數的東西）。兩者的數字都
    留在 counts 與備註裡，看得到、但不冒充待判項。
    """
    rows = {}
    notes = []

    def row(card):
        return rows.setdefault(card, {
            "card": card, "events": 0, "unverified_events": 0,
            "blocks": 0, "exam_failures": 0, "last_seen": "",
        })

    def touch(entry, date_text):
        if date_text and date_text > entry["last_seen"]:
            entry["last_seen"] = date_text

    event_results, errors = _bounded(vaults, _review_events_of)
    seen = set()
    events = unverified = unmapped = 0
    for _vault, found in event_results:
        for item in found:
            if item["event"] in seen:
                continue  # 同一則事件重送過，或同一句話落在兩個庫：只算一次
            seen.add(item["event"])
            events += 1
            if not item["verified"]:
                unverified += 1
            if not item["card"]:
                unmapped += 1
                continue
            entry = row(item["card"])
            entry["events" if item["verified"] else "unverified_events"] += 1
            touch(entry, item["date"])

    block_results, block_errors = _bounded(vaults, _review_blocks_of)
    errors.extend(block_errors)
    blocks = 0
    for _vault, found in block_results:
        for item in found:
            blocks += 1
            entry = row(item["card"])
            entry["blocks"] += 1
            touch(entry, item["date"])

    exam = _exam_results(governance_vault(vaults))
    exam_failures = exam_unmapped = 0
    for item in (exam or {}).get("failures", ()):
        exam_failures += 1
        if not item["cards"]:
            exam_unmapped += 1
            continue
        for card in item["cards"]:
            entry = row(card)
            entry["exam_failures"] += 1
            touch(entry, exam["date"])

    by_id = {section["id"]: (section.get("counts") or {}) for section in sections}
    candidates = {
        "pocket_vaults": by_id.get(8, {}).get("pocket_vaults", 0),
        "drafts_over_7_days": by_id.get(9, {}).get("over_7_days", 0),
        "mixed_cards": by_id.get(10, {}).get("mixed_cards", 0),
        "over_cap": by_id.get(11, {}).get("over_cap", 0),
        "uncarried_quotes": by_id.get(12, {}).get("uncarried_quotes", 0),
    }

    listed = sorted(
        rows.values(),
        key=lambda item: (-(item["events"] + item["blocks"] + item["exam_failures"]), item["card"]),
    )
    review_items = len(listed)
    trigger = memspec.REVIEW_PACK_TRIGGER
    at_threshold = review_items >= trigger
    notes.append(
        (REVIEW_PACK_READY_NOTE if at_threshold else REVIEW_PACK_BELOW_NOTE).format(
            count=review_items, trigger=trigger
        )
    )
    if unmapped:
        notes.append(REVIEW_PACK_UNMAPPED_NOTE.format(
            count=unmapped, field=memspec.MATCHED_CARD_FIELD))
    if unverified:
        notes.append(REVIEW_PACK_UNVERIFIED_NOTE.format(
            count=unverified, field=memspec.VERIFIED_FIELD, value=memspec.VERIFIED_FALSE))
    if exam is None:
        notes.append(REVIEW_PACK_NO_EXAM_NOTE.format(filename=memspec.EXAM_RESULTS_FILENAME))
    elif exam_unmapped:
        notes.append(REVIEW_PACK_EXAM_UNMAPPED_NOTE.format(count=exam_unmapped))
    return {
        "counts": {
            "cards": len(listed),
            "review_items": review_items,
            "trigger": trigger,
            "at_threshold": at_threshold,
            "events": events,
            "unverified_events": unverified,
            "unmapped_events": unmapped,
            "blocks": blocks,
            "exam_failures": exam_failures,
            "exam_unmapped": exam_unmapped,
            "exam_rules_version": (exam or {}).get("rules_version", ""),
            "dream_candidates": candidates,
            "listed_cap": REVIEW_PACK_MAX_ROWS,
        },
        "examples": listed[:REVIEW_PACK_MAX_ROWS],
        "commands": [REVIEW_PACK_COMMAND] if at_threshold else [],
        "errors": errors,
        "note": "；".join(notes),
    }


# --------------------------------------------------------------------------- 主記憶整形（順路任務）


def _harvest_module():
    """lazy import：只有第 4 節那趟順手收割用得到，別的路徑不為它付 import 的錢。"""
    try:
        from . import harvest
    except ImportError:  # Direct script execution keeps the CLI contract.
        import harvest
    return harvest


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
    (8, "全庫掃描與口袋庫", _section_pocket_vaults),
    (9, "草稿老化", _section_draft_aging),
    (10, "混雜卡（拆卡候選）", _section_mixed_cards),
    (11, "上限檢查", _section_caps),
    (12, "原話無決策卡承接（升決策卡候選）", _section_uncarried_quotes),
)
# 第 15 節不在上面那張表裡：它要讀前面幾節算完的候選數，所以由 build_report 最後跑。
_SECTION_IDS = tuple(section_id for section_id, _title, _fn in _SECTIONS) + (REVIEW_PACK_SECTION_ID,)


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
    # 第 8–12 節一律只列候選：下一步說的是「有幾件事等人判斷」，不是「夢會處理掉」。
    pocket = counts(8)
    if pocket.get("pocket_vaults", 0) > 0:
        steps.append(
            f"未登記的口袋庫 {pocket['pocket_vaults']} 個（共 {pocket.get('pocket_cards', 0)} 張卡）"
            " → 人工判斷歸戶；夢不搬不改"
        )
    aging = counts(9)
    if aging.get("over_7_days", 0) > 0:
        steps.append(
            f"草稿老化：{aging['over_7_days']} 份超過 {DRAFT_AGING_WARN_DAYS} 天、"
            f"{aging.get('over_30_days', 0)} 份超過 {DRAFT_AGING_STALE_DAYS} 天 → 人工審閱 _drafts/**"
        )
    mixed = counts(10)
    if mixed.get("mixed_cards", 0) > 0:
        steps.append(f"拆卡候選 {mixed['mixed_cards']} 張（一張卡＝一個記憶或規則）→ 人工判斷要不要拆")
    caps = counts(11)
    if caps.get("over_cap", 0) > 0:
        steps.append(f"超上限候選 {caps['over_cap']} 個檔案 → 人工判斷精簡或提高上限；夢不改檔")
    if caps.get("unset_keys", 0) > 0:
        steps.append(f"上限檢查有 {caps['unset_keys']} 項未設定（config 缺鍵），這幾項這次沒查")
    if caps.get("drift", 0) > 0:
        steps.append(
            f"生成塊漂移 {caps['drift']} 個檔案（與規則卡重組的結果不一致）"
            " → 人工判斷重生成或查手改；夢不改檔"
        )
    uncarried = counts(12)
    if uncarried.get("uncarried_quotes", 0) > 0:
        noise = uncarried.get("noise_candidates", 0)
        noise_note = f"（其中 {noise} 份疑似傳輸探針雜訊）" if noise else ""
        steps.append(
            f"升決策卡候選 {uncarried['uncarried_quotes']} 份原話會被當裁定端出、卻沒有決策卡承接"
            f"{noise_note} → 人工判斷升卡或降級"
        )
    # 檢討包只在候選滿額時佔一行：未達門檻的數字留在第 15 節裡，不進下一步（也不進
    # SessionStart——開場只讀 state 的 headline 欄，那三個欄位沒有動）。
    review = counts(REVIEW_PACK_SECTION_ID)
    if review.get("at_threshold"):
        steps.append(REVIEW_PACK_NEXT_STEP.format(
            count=review.get("review_items", 0),
            trigger=review.get("trigger", memspec.REVIEW_PACK_TRIGGER),
        ))
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


def build_report(vaults, today=None, since_date=None, deadline=None, shaping=None, config=None,
                 harvest_home=None, dry_run=False):
    today = today or datetime.now(timezone.utc).date()
    since_date = since_date or (today - timedelta(days=DEFAULT_EVENT_AGING_DAYS))
    # 設定讀一次就好：第 8 節要「登記了哪些庫」、第 11 節要三個上限鍵。讀不到就是空
    # 的——盤點本身不該因為設定檔壞掉而整份不跑（那才是 CORE-10 說的靜靜少一節）。
    config = configured_options() if config is None else config

    def run(section_id, title, call):
        # 時限到了就把剩下的節標成略過：背景程序寧可交半份標明缺口的包，也不要
        # 在一個大庫上跑到天亮（CORE-10：缺口要說出來，不是靜靜少一節）。
        if deadline is not None and time.monotonic() >= deadline:
            return {"id": section_id, "title": title, "error": TIME_BUDGET_ERROR}
        try:
            return {"id": section_id, "title": title, "error": None, **call()}
        except Exception as exc:
            return {"id": section_id, "title": title, "error": f"{type(exc).__name__}: {exc}"}

    # 第 4 節在盤點草稿之前順手跑一趟 harvest，所以它多收一個執行脈絡：家目錄（沒有
    # 就不跑）、夢的 --dry-run 透傳、整體時限。其餘各節的簽章不變。
    context = {"home": harvest_home, "dry_run": bool(dry_run), "deadline": deadline}
    sections = [
        run(section_id, title, lambda fn=fn: (
            fn(vaults, today, since_date, config, context) if fn is _section_drafts
            else fn(vaults, today, since_date, config)
        ))
        for section_id, title, fn in _SECTIONS
    ]
    # 檢討包最後跑：它要把第 8–12 節算完的候選數一起列出來當背景。
    sections.append(run(
        REVIEW_PACK_SECTION_ID, REVIEW_PACK_TITLE,
        lambda: _section_review_pack(vaults, today, since_date, config, sections),
    ))
    shaping = list(shaping or ())
    return {
        "vaults": [str(vault) for vault in vaults],
        "today": today.isoformat(),
        "since": since_date.isoformat(),
        "sections": sections,
        "index_shaping": shaping,
        "next_steps": _next_steps(sections, shaping),
    }


def _render_section(lines, section):
    lines.append(f"## {section['id']}. {section['title']}")
    if section.get("error"):
        lines.append(f"（此節失敗：{section['error']}）")
        lines.append("")
        return
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


def _render_markdown(report):
    lines = [f"# Epitype Dream Pack — {report['today']}", ""]
    lines.append("Vaults:")
    for vault in report["vaults"]:
        lines.append(f"- {vault}")
    lines.append(f"事件卡老化門檻（--since）：{report['since']}")
    lines.append("")
    # 第 13、14 節（整形與下一步）在 U-K 就已經佔住那兩個號碼，所以第 15 節接在它們
    # 後面印，號碼才跟閱讀順序一致；盤點節照樣先印。
    for section in report["sections"]:
        if section["id"] != REVIEW_PACK_SECTION_ID:
            _render_section(lines, section)
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

    lines.append("## 14. 夢的下一步")
    for step in report["next_steps"]:
        lines.append(f"- {step}")
    lines.append("")

    for section in report["sections"]:
        if section["id"] == REVIEW_PACK_SECTION_ID:
            _render_section(lines, section)
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
    # 路徑推導同源 memspec：核心生成器讀的必須是同一個設定檔（U-M-a）。
    return memspec.config_path()


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


def configured_options(config_path=None):
    """設定檔整份（讀不到就 {}）——第 8、11 節要的選填鍵都從這裡取。

    `configured_vaults` 會為「沒有登記庫」丟例外，因為那時候夢無事可做；這裡相反：
    設定不存在只代表沒設上限，盤點照跑，缺的鍵由第 11 節寫一行「未設定」。
    """
    return memspec.config_options(config_path)


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
    for section_id in _SECTION_IDS:
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


_SELFTEST_ENV_NAMES = ("HOME", "USERPROFILE", memspec.EPITYPE_CONFIG_ENV)


def _selftest():
    checks = []
    saved_environ = {name: os.environ.get(name) for name in _SELFTEST_ENV_NAMES}
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-dream-") as temp_dir:
            vault = Path(temp_dir).resolve() / "vault"
            vault.mkdir()
            today = date(2026, 9, 6)

            # 第 8 節從家目錄推口袋庫、第 11 節讀設定的上限鍵：兩個入口都要指進這個
            # 暫存目錄，否則自測會去掃 owner 真正的家目錄與真設定（外層 finally 還原）。
            home = Path(temp_dir).resolve() / "home"
            projects = home / memspec.HOST_STATE_DIRECTORY / memspec.HOST_PROJECTS_DIRECTORY
            projects.mkdir(parents=True)
            _write_card(
                projects / "pocket-project" / memspec.HOST_MEMORY_DIRECTORY / "stray.md",
                "---\nname: stray\ndescription: 2026-06-01 synthetic\n---\nbody\n",
            )
            (projects / "empty-project" / memspec.HOST_MEMORY_DIRECTORY).mkdir(parents=True)
            selftest_config = Path(temp_dir).resolve() / "selftest-config.json"
            selftest_config.write_text(
                json.dumps({memspec.CONFIG_VAULTS_FIELD: [os.fspath(vault)]}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.environ["HOME"] = os.fspath(home)
            os.environ["USERPROFILE"] = os.fspath(home)
            os.environ[memspec.EPITYPE_CONFIG_ENV] = os.fspath(selftest_config)

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

            checks.append(("all 12 deterministic sections present with no error", all(
                by_id[i]["error"] is None for i in range(1, 13)
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

            # --- 第 8–12 節：只列候選，一個檔都不動 ---
            # 各自用一個獨立的小庫，才不會讓新題目的資料改動前面七節的數字。
            checks.append(("section 8 lists the unregistered pocket vault, skipping the empty one and the registered vault", (
                by_id[8]["counts"]["pocket_vaults"] == 1
                and by_id[8]["counts"]["pocket_cards"] == 1
                and by_id[8]["examples"][0]["path"] == os.fspath(
                    projects / "pocket-project" / memspec.HOST_MEMORY_DIRECTORY)
                and by_id[8]["examples"][0]["cards"] == 1
                and memspec.is_iso_date(by_id[8]["examples"][0]["newest_mtime"])
                and by_id[8]["examples"][0]["candidate"] == POCKET_VAULT_CANDIDATE
            )))

            drafts_vault = Path(temp_dir).resolve() / "drafts"
            drafts_vault.mkdir()
            draft_ages = {
                "decisions/old.md": date(2026, 1, 1),
                "decisions/middle.md": date(2026, 8, 20),
                "notes/fresh.md": date(2026, 9, 5),
            }
            for relative, stamped in draft_ages.items():
                target = drafts_vault / DRAFT_DIRNAME / relative
                _write_card(target, "draft\n")
                moment = datetime(stamped.year, stamped.month, stamped.day, tzinfo=timezone.utc).timestamp()
                os.utime(target, (moment, moment))
            drafts_by_id = {s["id"]: s for s in build_report([drafts_vault], today=today)["sections"]}
            checks.append(("section 9 ages the drafts, groups them by first-level subdirectory, and names the oldest first", (
                drafts_by_id[9]["counts"]["total_drafts"] == 3
                and drafts_by_id[9]["counts"]["over_7_days"] == 2
                and drafts_by_id[9]["counts"]["over_30_days"] == 1
                and drafts_by_id[9]["counts"]["by_subdir"]["decisions"] == {"total": 2, "over_7": 2, "over_30": 1}
                and drafts_by_id[9]["counts"]["by_subdir"]["notes"] == {"total": 1, "over_7": 0, "over_30": 0}
                and drafts_by_id[9]["examples"][0]["path"] == "_drafts/decisions/old.md"
                and drafts_by_id[9]["examples"][0]["age_days"] == 248
            )))

            mixed_vault = Path(temp_dir).resolve() / "mixed"
            mixed_vault.mkdir()
            _write_card(
                mixed_vault / "two_headings.md",
                "---\nname: two_headings\ndescription: 2026-06-01 synthetic\naliases:\n- a\n---\n"
                "## 第一件事\nbody\n\n## 第二件事\nbody\n",
            )
            _write_card(
                mixed_vault / "fenced_example.md",
                "---\nname: fenced_example\ndescription: 2026-06-01 synthetic\naliases:\n- b\n---\n"
                "```markdown\n## 範例一\n## 範例二\n```\n說明一件事而已\n",
            )
            _write_card(
                mixed_vault / "too_big.md",
                "---\nname: too_big\ndescription: 2026-06-01 synthetic\naliases:\n- c\n---\n"
                + "x" * (memspec.CARD_BODY_MIXED_BYTES + 1) + "\n",
            )
            _write_card(
                mixed_vault / "packed_description.md",
                "---\nname: packed_description\ndescription: 2026-06-01 "
                + "甲乙丙丁" * 45 + "；戊己庚辛\naliases:\n- d\n---\nbody\n",
            )
            # U-K3：人審過的憑證要壓過三個形狀訊號，否則逐張審完的庫下一次照樣被列。
            packed = "2026-06-01 " + "甲乙丙丁" * 45 + "；戊己庚辛"
            _write_card(
                mixed_vault / "reviewed_two_headings.md",
                "---\nname: reviewed_two_headings\ndescription: 2026-06-01 synthetic\n"
                "mixed_reviewed: 2026-09-09-keep\naliases:\n- e\n---\n"
                "## 第一件事\nbody\n\n## 第二件事\nbody\n",
            )
            _write_card(
                mixed_vault / "superseded_big.md",
                "---\nname: superseded_big\ndescription: 2026-06-01 synthetic\nstatus: superseded\n"
                "aliases:\n- f\n---\n" + "x" * (memspec.CARD_BODY_MIXED_BYTES + 1) + "\n",
            )
            _write_card(
                mixed_vault / "fresh_split.md",
                "---\nname: fresh_split\ndescription: " + packed
                + "\nsplit_from: origin.md\naliases:\n- g\n---\n## 一件事\nbody\n",
            )
            _write_card(
                mixed_vault / "split_two_headings.md",
                "---\nname: split_two_headings\ndescription: 2026-06-01 synthetic\n"
                "split_from: origin.md\naliases:\n- h\n---\n"
                "## 第一件事\nbody\n\n## 第二件事\nbody\n",
            )
            _write_card(
                mixed_vault / "unmarked_twin.md",
                "---\nname: unmarked_twin\ndescription: " + packed
                + "\naliases:\n- i\n---\n## 一件事\nbody\n",
            )
            mixed_by_id = {s["id"]: s for s in build_report([mixed_vault], today=today)["sections"]}
            mixed_paths = {item["path"] for item in mixed_by_id[10]["examples"]}
            checks.append(("section 10 flags the three mixed shapes and leaves a fenced markdown example alone", (
                mixed_by_id[10]["counts"]["mixed_cards"] == 5
                and mixed_paths == {"two_headings.md", "too_big.md", "packed_description.md",
                                    "split_two_headings.md", "unmarked_twin.md"}
                and "fenced_example.md" not in mixed_paths
                and any("## " in reason for item in mixed_by_id[10]["examples"]
                        for reason in item["reasons"] if item["path"] == "two_headings.md")
            )))
            checks.append(("section 10 skips the card a human already marked mixed_reviewed", (
                "reviewed_two_headings.md" not in mixed_paths
                and mixed_by_id[10]["counts"]["skipped_by_reason"][MIXED_SKIP_REVIEWED] == 1
            )))
            checks.append(("section 10 skips a superseded card even when its body is over the cap", (
                "superseded_big.md" not in mixed_paths
                and mixed_by_id[10]["counts"]["skipped_by_reason"][MIXED_SKIP_SUPERSEDED] == 1
            )))
            checks.append(("a card just split out is not listed again, but an unmarked twin still is", (
                "fresh_split.md" not in mixed_paths
                and mixed_by_id[10]["counts"]["skipped_by_reason"][MIXED_SKIP_FRESH_SPLIT] == 1
                and "unmarked_twin.md" in mixed_paths
            )))
            checks.append(("a split card that grew two headings of its own is listed again", (
                "split_two_headings.md" in mixed_paths
            )))
            checks.append(("section 10 reports how many cards the human review took off the list", (
                mixed_by_id[10]["counts"]["reviewed_skipped"] == 3
                and mixed_by_id[10]["note"] == MIXED_SKIPPED_NOTE.format(count=3)
            )))

            cap_vault = Path(temp_dir).resolve() / "caps"
            cap_vault.mkdir()
            (cap_vault / memspec.MEMORY_INDEX_FILENAME).write_text("x" * 50, encoding="utf-8")
            core_file = Path(temp_dir).resolve() / "core.md"
            core_file.write_text("y" * 40, encoding="utf-8")
            unset_by_id = {s["id"]: s for s in build_report([cap_vault], today=today, config={})["sections"]}
            checks.append(("section 11 without the config keys says so and judges nothing", (
                unset_by_id[11]["counts"]["over_cap"] == 0
                and unset_by_id[11]["counts"]["unset_keys"] == 2
                and memspec.CONFIG_INDEX_CAP_BYTES_FIELD in unset_by_id[11]["note"]
                and memspec.CONFIG_CORE_FILES_FIELD in unset_by_id[11]["note"]
            )))
            capped_by_id = {s["id"]: s for s in build_report([cap_vault], today=today, config={
                memspec.CONFIG_INDEX_CAP_BYTES_FIELD: 10,
                memspec.CONFIG_CORE_FILES_FIELD: [os.fspath(core_file)],
                memspec.CONFIG_CORE_CAP_BYTES_FIELD: 5,
            })["sections"]}
            capped = {item["path"]: item for item in capped_by_id[11]["examples"]}
            checks.append(("section 11 with the keys set lists both over-cap files with their overage, and changes neither", (
                capped_by_id[11]["counts"]["over_cap"] == 2
                and capped_by_id[11]["counts"]["unset_keys"] == 0
                and capped[os.fspath(cap_vault / memspec.MEMORY_INDEX_FILENAME)]["over"] == 40
                and capped[os.fspath(core_file)]["over"] == 35
                and (cap_vault / memspec.MEMORY_INDEX_FILENAME).read_text(encoding="utf-8") == "x" * 50
                and core_file.read_text(encoding="utf-8") == "y" * 40
            )))

            try:  # 規則卡的合成庫沿用 core_gen 自己那一份，夢不再寫第二套樣板
                from . import core_gen
            except ImportError:  # Direct script execution keeps the CLI contract.
                import core_gen
            drift_vault = core_gen._build_vault(Path(temp_dir).resolve() / "coredrift")
            core_block = Path(temp_dir).resolve() / "core_block.md"
            core_gen.generate([drift_vault], core_block, config={})
            drift_config = {
                memspec.CONFIG_CORE_FILES_FIELD: [os.fspath(core_block)],
                memspec.CONFIG_CORE_CAP_BYTES_FIELD: 1000000,
            }
            clean_11 = {s["id"]: s for s in build_report(
                [drift_vault], today=today, config=drift_config)["sections"]}[11]
            core_block.write_text(
                core_block.read_text(encoding="utf-8") + "hand edit\n", encoding="utf-8", newline="\n"
            )
            drifted_11 = {s["id"]: s for s in build_report(
                [drift_vault], today=today, config=drift_config)["sections"]}[11]
            checks.append(("section 11 reports a hand-edited generated block as a drift candidate and changes neither", (
                clean_11["counts"]["drift"] == 0
                and drifted_11["counts"]["drift"] == 1
                and drifted_11["counts"]["over_cap"] == 0
                and drifted_11["examples"][0]["path"] == os.fspath(core_block)
                and CAP_DRIFT_COMMAND in drifted_11["commands"]
                and core_block.read_text(encoding="utf-8").endswith("hand edit\n")
            )))
            no_rules_11 = {s["id"]: s for s in build_report(
                [cap_vault], today=today, config=drift_config)["sections"]}[11]
            checks.append(("a vault with no rule cards is never reported as drift (an empty assembly compares to nothing)", (
                no_rules_11["counts"]["drift"] == 0
            )))

            quotes_vault = Path(temp_dir).resolve() / "quotes"
            quotes_vault.mkdir()
            for stem, captured, extra in (
                ("carried", "2026-09-03", ""),
                ("uncarried", "2026-09-01", ""),
                ("unverified", "2026-09-02", f"{memspec.VERIFIED_FIELD}: false\n"),
            ):
                _write_card(
                    quotes_vault / memspec.RULING_DIRECTORY / f"{stem}.md",
                    f"---\nname: {stem}\ndescription: owner ruling auto-captured {captured}: 合成\n"
                    f"captured_at: {captured}\nsession_id: s1\n{extra}---\n"
                    f"owner 說的那句話（{stem}）\n",
                )
            _write_card(
                quotes_vault / "carrier.md",
                "---\ndecision_key: k9\nstatus: active\ncurrent_decision_at: 2026-09-05\n"
                "decided_by: owner-explicit\nowner_quote: 就這樣\nsource: rulings/carried.md\n"
                "aliases:\n- k9\n---\n承接上面那句原話。\n",
            )
            # 承接的三條路各一案（提名／逐字引用／自報），外加兩個必須仍然列出的反例。
            for stem, extra, body in (
                # 決策卡只引了正文的一段，沒寫檔名——真庫 18/19 張決策卡是這個形狀。
                ("quoted", "", "答（owner 逐字）：庫存要先對過帳才可以出貨，這是硬規定。"),
                ("selfnamed", f"{memspec.CARRIED_BY_FIELD}: k9\n", "沒有人引用，但卡自己指名了承接者。"),
                # 4 個字的重疊在引文裡撞得到，長度地板必須擋住它。
                ("tooshort", "", "先對過帳"),
                ("noise", "", 'Return only JSON {"probe":"ok"}. Do not use tools.'),
            ):
                _write_card(
                    quotes_vault / memspec.RULING_DIRECTORY / f"{stem}.md",
                    f"---\nname: {stem}\ndescription: owner ruling auto-captured 2026-09-04: 合成\n"
                    f"captured_at: 2026-09-04\nsession_id: s1\n{extra}---\n{body}\n",
                )
            _write_card(
                quotes_vault / "quoter.md",
                "---\ndecision_key: k8\nstatus: active\ncurrent_decision_at: 2026-09-05\n"
                "decided_by: owner-explicit\n"
                "owner_quote: 「甲」（Q1）；「庫存要先對過帳才可以出貨」（第二段另有出處）\n"
                "aliases:\n- k8\n---\n只靠逐字引用承接，正文一個檔名都沒提。\n",
            )
            quotes_by_id = {s["id"]: s for s in build_report([quotes_vault], today=today)["sections"]}
            rows = {item["path"]: item for item in quotes_by_id[12]["examples"]}
            checks.append(("section 12 lists only what no decision card carries, by any of the three routes", (
                quotes_by_id[12]["counts"]["uncarried_quotes"] == 3
                and set(rows) == {"rulings/uncarried.md", "rulings/tooshort.md", "rulings/noise.md"}
                and rows["rulings/uncarried.md"]["captured_at"] == "2026-09-01"
                and "uncarried" in rows["rulings/uncarried.md"]["excerpt"]
            )))
            checks.append(("carried route 1: a decision card naming the file clears it", "rulings/carried.md" not in rows))
            checks.append(("carried route 2: a decision card quoting part of the body verbatim clears it", "rulings/quoted.md" not in rows))
            checks.append(("carried route 3: the event card's own carried_by clears it", "rulings/selfnamed.md" not in rows))
            checks.append(("an overlap shorter than the verbatim floor is not a carry", "rulings/tooshort.md" in rows))
            checks.append(("the transport-probe template is marked as noise and nothing else is", (
                quotes_by_id[12]["counts"]["noise_candidates"] == 1
                and rows["rulings/noise.md"]["noise"] == '{"probe":'
                and rows["rulings/uncarried.md"]["noise"] == ""
                and rows["rulings/tooshort.md"]["noise"] == ""
            )))

            # ── 第 15 節（U-P）：五種來源各一、去重、門檻兩側 ──
            review_vault = Path(temp_dir).resolve() / "review-vault"
            review_vault.mkdir()

            def _event(name, directory, card="", verified=memspec.VERIFIED_TRUE,
                       event_id="", captured="2026-09-05"):
                _write_card(
                    review_vault / directory / f"{name}.md",
                    f"---\nname: {name}\ndescription: owner auto-captured {captured}: synthetic\n"
                    f"{memspec.CAPTURED_AT_FIELD}: {captured}T00:00:00Z\n"
                    f"{memspec.SESSION_FIELD}: review-session\n"
                    + (f"{memspec.EVENT_ID_FIELD}: {event_id}\n" if event_id else "")
                    + (f"{memspec.MATCHED_CARD_FIELD}: {card}\n" if card else "")
                    + f"{memspec.VERIFIED_FIELD}: {verified}\n---\nsynthetic body\n",
                )

            # 同一則事件在兩個庫各留一份／重放一次：event_id 相同，只能算一次。
            _event("correction-20260905-aaaaaaaaaaaa-eventone", memspec.CORRECTION_DIRECTORY,
                   card="decisions/one.md", event_id="eventone1234")
            _event("correction-20260906-bbbbbbbbbbbb-eventone", memspec.CORRECTION_DIRECTORY,
                   card="decisions/one.md", event_id="eventone1234", captured="2026-09-06")
            _event("ruling-20260905-cccccccccccc-eventtwo", memspec.RULING_DIRECTORY,
                   card="decisions/two.md", event_id="eventtwo1234",
                   verified=memspec.VERIFIED_FALSE)
            _event("grant-20260905-dddddddddddd-eventfour", memspec.GRANT_DIRECTORY,
                   event_id="eventfour123")
            _event("grant-20260905-ffffffffffff-eventfive", memspec.GRANT_DIRECTORY,
                   card="decisions/five.md", event_id="eventfive123")
            (review_vault / memspec.GATE_LOG_FILENAME).write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in (
                    {"timestamp": "2026-09-07T01:00:00+00:00",
                     "kind": memspec.STOP_GATE_LOG_KIND, "decision": "decisions/three.md"},
                    {"timestamp": "2026-09-08T01:00:00+00:00",
                     "kind": memspec.WRITE_GATE_LOG_KIND, "decision": "decisions/one.md"},
                    # 動作閘的 deny 不是這一節要數的東西（U-P 只讀 Stop 與寫檔兩種）。
                    {"timestamp": "2026-09-08T02:00:00+00:00", "card": "decisions/deny-only.md"},
                )) + "\n",
                encoding="utf-8",
            )
            exam_results = review_vault / memspec.DREAM_DIRECTORY / memspec.EXAM_RESULTS_FILENAME
            exam_results.parent.mkdir(parents=True, exist_ok=True)
            exam_results.write_text(
                json.dumps({
                    "version": 1, "rules_version": "abcdef123456",
                    "generated_at": "2026-09-09T00:00:00Z",
                    "results": [
                        {"id": "q-pass", "passed": True, "cards": ["decisions/one.md"]},
                        {"id": "q-fail", "passed": False, "cards": ["decisions/four.md"]},
                        {"id": "q-fail-unmapped", "passed": False, "cards": []},
                    ],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            review_before = sorted(
                (path.relative_to(review_vault).as_posix(), path.read_bytes())
                for path in review_vault.rglob("*") if path.is_file()
            )
            review_report = build_report([review_vault], today=today)
            review = {s["id"]: s for s in review_report["sections"]}[REVIEW_PACK_SECTION_ID]
            review_rows = {item["card"]: item for item in review["examples"]}
            checks.append((
                "section 15 lines every source up on the card it points at, one row per card",
                review["error"] is None
                and review["counts"]["events"] == 4
                and review_rows["decisions/one.md"]["events"] == 1
                and review_rows["decisions/one.md"]["blocks"] == 1
                and review_rows["decisions/one.md"]["last_seen"] == "2026-09-08"
                and review_rows["decisions/two.md"] == {
                    "card": "decisions/two.md", "events": 0, "unverified_events": 1,
                    "blocks": 0, "exam_failures": 0, "last_seen": "2026-09-05"}
                and review_rows["decisions/three.md"]["blocks"] == 1
                and review_rows["decisions/four.md"]["exam_failures"] == 1
                and review_rows["decisions/five.md"]["events"] == 1
                and "decisions/deny-only.md" not in review_rows,
            ))
            checks.append((
                "an unverified quote and an event with no matched card are listed, never counted as incidents",
                review["counts"]["unverified_events"] == 1
                and review["counts"]["unmapped_events"] == 1
                and review["counts"]["exam_unmapped"] == 1
                and review["counts"]["exam_rules_version"] == "abcdef123456"
                and memspec.MATCHED_CARD_FIELD in review["note"]
                and memspec.VERIFIED_FALSE in review["note"],
            ))
            checks.append((
                "the trigger fires at five deduplicated rows and says so in the next steps",
                review["counts"]["cards"] == 5
                and review["counts"]["review_items"] == 5
                and review["counts"]["trigger"] == memspec.REVIEW_PACK_TRIGGER
                and review["counts"]["at_threshold"] is True
                and any("檢討包達門檻" in step for step in review_report["next_steps"])
                and review["commands"] == [REVIEW_PACK_COMMAND],
            ))
            review_markdown = _render_markdown(review_report)
            checks.append((
                "the review pack renders as section 15, carries the 8-12 candidates, changes no file",
                review_markdown.index("## 14. 夢的下一步")
                < review_markdown.index(f"## {REVIEW_PACK_SECTION_ID}. {REVIEW_PACK_TITLE}")
                # 第 8–12 節的候選與「對不到卡的事件」都只當背景列出來，不進門檻計數：
                # 那五節各自已經有自己的下一步行，再算一次就是同一件事算兩次（四張已核
                # 事件卡沒有決策卡承接＝第 12 節的四份候選，門檻數字仍然是 5 列）。
                and review["counts"]["dream_candidates"]["uncarried_quotes"] == 4
                and review["counts"]["review_items"] == 5
                and sorted(
                    (path.relative_to(review_vault).as_posix(), path.read_bytes())
                    for path in review_vault.rglob("*") if path.is_file()
                ) == review_before,
            ))

            thin_vault = Path(temp_dir).resolve() / "thin-review-vault"
            (thin_vault / memspec.CORRECTION_DIRECTORY).mkdir(parents=True)
            _write_card(
                thin_vault / memspec.CORRECTION_DIRECTORY / "correction-20260905-eeeeeeeeeeee.md",
                f"---\nname: correction-20260905-eeeeeeeeeeee\ndescription: owner correction 2026-09-05\n"
                f"{memspec.CAPTURED_AT_FIELD}: 2026-09-05T00:00:00Z\n"
                f"{memspec.SESSION_FIELD}: thin\n{memspec.MATCHED_CARD_FIELD}: decisions/one.md\n"
                "---\nbody\n",
            )
            thin_report = build_report([thin_vault], today=today)
            thin = {s["id"]: s for s in thin_report["sections"]}[REVIEW_PACK_SECTION_ID]
            checks.append((
                "below the trigger the pack says how far off it is and adds no next step",
                thin["counts"]["review_items"] == 1
                and thin["counts"]["at_threshold"] is False
                and thin["note"].startswith(
                    REVIEW_PACK_BELOW_NOTE.format(count=1, trigger=memspec.REVIEW_PACK_TRIGGER))
                and memspec.EXAM_RESULTS_FILENAME in thin["note"]
                and not any("檢討包" in step for step in thin_report["next_steps"])
                and thin["commands"] == [],
            ))

            five_report = build_report([vault, drafts_vault, mixed_vault, quotes_vault], today=today, config={
                memspec.CONFIG_INDEX_CAP_BYTES_FIELD: 10,
            })
            five_steps = "\n".join(five_report["next_steps"])
            checks.append(("the next steps carry all five candidate counts, each as a human call", all(
                marker in five_steps for marker in ("口袋庫", "草稿老化", "拆卡候選", "未設定", "升決策卡候選")
            )))
            five_markdown = _render_markdown(five_report)
            checks.append(("the pack renders sections 8-12 and moves shaping to 13 and the next steps to 14", all(
                heading in five_markdown for heading in (
                    "## 8. 全庫掃描與口袋庫", "## 9. 草稿老化", "## 10. 混雜卡（拆卡候選）",
                    "## 11. 上限檢查", "## 12. 原話無決策卡承接（升決策卡候選）",
                    memspec.INDEX_SHAPING_HEADING, "## 14. 夢的下一步",
                )
            )))

            # --since moved before old_grant's captured_at (2025-01-01) clears it as
            # a candidate: only cards captured *before* the cutoff count as aging.
            loose_report = build_report([vault], today=today, since_date=date(2024, 1, 1))
            loose_by_id = {section["id"]: section for section in loose_report["sections"]}
            checks.append(("--since moved earlier than old_grant's date clears the aging candidate", loose_by_id[6]["counts"]["aging_total"] == 0))

            # a broken section function must not take the rest of the report down.
            global _SECTIONS
            saved_sections = _SECTIONS
            def _boom(vaults, today, since_date, config):
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

            # --- U-R2：第 4 節在盤點草稿之前順手跑一趟 harvest（只產草稿）。自帶
            # 家目錄與庫，才不會動到上面每一條對主 fixture 草稿數的斷言。---
            harvest_home = Path(temp_dir).resolve() / "harvest-home"
            harvest_vault = Path(temp_dir).resolve() / "harvest-vault"
            harvest_vault.mkdir()
            harvest_projects = (
                harvest_home / memspec.HOST_STATE_DIRECTORY
                / memspec.HOST_PROJECTS_DIRECTORY / "C--Harvest"
            )
            harvest_projects.mkdir(parents=True)
            # 白名單形狀的授權句：線上捕捉會直接入庫，所以它證明得了 drafts-only 有生效。
            harvest_sentence = "你可以直接改那個測試檔"
            (harvest_projects / "harvest-session.jsonl").write_text(
                json.dumps({
                    "type": "user", "timestamp": "2026-09-05T10:00:00.000Z",
                    "sessionId": "sess-dream-harvest", "cwd": os.fspath(harvest_home),
                    "message": {"role": "user", "content": harvest_sentence},
                }, ensure_ascii=False) + chr(10),
                encoding="utf-8",
            )
            saved_home = (os.environ["HOME"], os.environ["USERPROFILE"])
            try:
                os.environ["HOME"] = os.fspath(harvest_home)
                os.environ["USERPROFILE"] = os.fspath(harvest_home)
                dry_output = io.StringIO()
                dry_harvest_code = main(
                    ["--dry-run", "--today", "2026-09-06", os.fspath(harvest_vault)], output=dry_output
                )
                dry_harvest_text = dry_output.getvalue()
                dry_harvest_files = sorted(
                    path for path in harvest_vault.rglob("*") if path.is_file()
                )
                real_harvest_code = main(
                    ["--today", "2026-09-06", os.fspath(harvest_vault)], output=io.StringIO()
                )
            finally:
                os.environ["HOME"], os.environ["USERPROFILE"] = saved_home
            checks.append(("section 4 harvests on the way; --dry-run counts the draft and writes nothing", (
                dry_harvest_code == 0
                and "本次 harvest 新增 1 張草稿" in dry_harvest_text
                and dry_harvest_files == []
            )))
            harvest_pending = sorted(
                harvest_vault.joinpath(*memspec.CAPTURE_PENDING_SUBPATH).rglob("*.md")
            )
            harvest_pack = (harvest_vault / ".epitype" / "dream_pack_20260906.md").read_text(encoding="utf-8")
            checks.append(("the harvested sentence lands as a proposal only, and the pack says how many", (
                real_harvest_code == 0
                and len(harvest_pending) == 1
                and harvest_sentence in harvest_pending[0].read_text(encoding="utf-8")
                and not (harvest_vault / memspec.GRANT_DIRECTORY).exists()
                and "本次 harvest 新增 1 張草稿" in harvest_pack
                and '"harvest_new_drafts": 1' in harvest_pack
            )))

            # harvest 掛掉只花第 4 節一行 errors：其餘各節照跑，草稿盤點本身也照出數字。
            saved_harvest_module = _harvest_module
            def _broken_harvest_module():
                raise RuntimeError("synthetic harvest failure")
            try:
                globals()["_harvest_module"] = _broken_harvest_module
                broken_harvest_report = build_report([vault], today=today, harvest_home=home)
            finally:
                globals()["_harvest_module"] = saved_harvest_module
            broken_harvest_by_id = {
                section["id"]: section for section in broken_harvest_report["sections"]
            }
            checks.append(("a failing harvest costs section 4 one error line, nothing else", (
                broken_harvest_by_id[4]["error"] is None
                and len(broken_harvest_by_id[4]["errors"]) == 1
                and "harvest" in broken_harvest_by_id[4]["errors"][0]
                and broken_harvest_by_id[4]["counts"]["harvest_new_drafts"] is None
                and broken_harvest_by_id[4]["counts"]["total_drafts"] >= 2
                and broken_harvest_by_id[1]["error"] is None
                and broken_harvest_by_id[2]["counts"]["fail"] >= 1
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
    finally:
        # 家目錄與設定的指向是行程層的，失敗路徑也要還原：留著的話，同一個行程裡
        # 後面跑的東西會對著一個已經被刪掉的暫存目錄找家。
        for name, value in saved_environ.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    passed = sum(bool(ok) for _, ok in checks)
    total = 68
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
            vaults, today=today, since_date=since_date, deadline=deadline, shaping=shaping,
            # 家目錄同源 harvest CLI（沒有 --home 時就是 Path.home()），不另立第二套推導。
            harvest_home=Path.home(), dry_run=parsed.dry_run,
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
