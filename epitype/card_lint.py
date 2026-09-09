import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8；唯讀/寫入 CLI 不落 pyc。
"""Epitype 型別卡片 lint：卡片是表單，每種型別有必填欄位，缺了就不收。

型別由 frontmatter 與路徑依序推斷（decision_key → 事件卡目錄 → 待辦 →
metadata.type → feedback），必填欄位表在 memspec.CARD_REQUIRED_FIELDS 同源。FAIL 是
「這張卡不能算收下」；WARN 是「收下但有已知缺口」——第一版把既有 371 張缺別名的卡
留在 WARN，否則第一次跑就全紅、沒人看得完。INFO 是「這不是給 owner 的決定題」
（授權卡沒有到期日＝永久有效；卡沒有中文＝本場 AI 自己補），只在 --verbose 與審核包
出現，不進 SessionStart 的 WARN 數。預設只點名不改卡；唯一會動筆的是 --fix-dates，把推得的日期寫成一行
last_verified_at:，寫什麼都先由 --dry-run 印出來。
"""

import argparse
from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
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
# owner 2026-09-06:「沒有有效期限可能是 owner 希望永久有效」——那不是缺口，是選擇。
# INFO 只在 --verbose 與審核包出現，不進 SessionStart 那一行的 WARN 數。
INFO = "INFO"
# 中文提問喚不回全英文卡（2026-08-13 鏡射裁定事故）；CJK 統一表意文字＋擴充 A＋相容區。
CJK_REGEX = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
# 平面欄位一律由 memspec.frontmatter_fields 供給（U38 同源），這裡只用同一條 key 形狀。
TOP_LEVEL_FIELD = memspec.TOP_LEVEL_FIELD


def _nested_and_lists(front_lines, path):
    """一層巢狀子欄位（"parent.child"）與序列欄位的項數。

    memspec.frontmatter_fields 只吐 top-level scalar，memsearch 只吐 aliases；
    型別判定需要 metadata.type，必填判定需要「序列是否至少一項」。
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


def _forbidden_items(front_lines):
    """決策卡 forbidden 序列的字串項，block 與 flow 兩種寫法都讀得到。

    flow 裡未加引號的逗號本來就分項（閘門的切法也是這樣），所以帶 `{0,12}` 的句形
    要寫成 block 或加引號——切錯只會少報一條 WARN，不會多擋任何一次寫入。
    """
    items = []
    parent = None
    for raw_line in front_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1].isspace():
            if parent and stripped.startswith("- "):
                item, _problem = memspec.parse_scalar(stripped[1:])
                if item:
                    items.append(item)
            continue
        parent = None
        match = TOP_LEVEL_FIELD.match(raw_line)
        if match is None:
            continue
        key, raw_value = match.groups()
        if key != memspec.FORBIDDEN_FIELD:
            continue
        value = memspec.strip_inline_comment(raw_value).strip()
        if value.startswith("[") and value.endswith("]"):
            for piece in value[1:-1].split(","):
                item, _problem = memspec.parse_scalar(piece)
                if item:
                    items.append(item)
        elif value and value not in memspec.BLOCK_SCALAR_STYLES:
            item, _problem = memspec.parse_scalar(raw_value)
            if item:
                items.append(item)
        else:
            parent = key
    return items


def _is_bare_term(item):
    """裸名詞＝沒有動詞、夠短、沒有正則元字元；三個都成立才算。

    2026-09-06 實測：forbidden 寫成裸名詞時，「為什麼不採用 X」這種說明也被擋，
    因為擋的是名字而不是再提議的動作。"""
    if any(character in memspec.FORBIDDEN_REGEX_METACHARACTERS for character in item):
        return False
    if len(item) > memspec.FORBIDDEN_BARE_TERM_MAX_CHARS:
        return False
    return not any(verb in item for verb in memspec.FORBIDDEN_VERB_HINTS)


def _card_type(relative, fields, nested):
    """順序判定；結構訊號優先於自報型別，自報的 metadata.type 才是最後手段。"""
    if memspec.DECISION_KEY_FIELD in fields:
        return memspec.CARD_TYPE_DECISION
    # 2026-09-09 U-J：`trigger:` 不再是結構訊號——那條攔截路徑整條拆了，卡片還留著這個
    # 欄位也只是舊寫法。傷疤卡現在只由 metadata.type 自報。
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


def card_type_of(relative, text, path=None):
    """(型別, 平面欄位)——已讀進來的一份卡片內容判型別的公開入口。

    視圖生成器必須跟 lint 判成同一個型別，否則同一張卡在目錄裡分到 A 區、在
    lint 裡按 B 型的必填欄位檢查。`_card_type` 仍是唯一實作，這裡只是不必再讀一次檔
    的入口（`_check_card` 走的是同一個函式）。
    """
    fields, _problem = memspec.frontmatter_text(text)
    front_lines, _closing = memspec.split_frontmatter(text)
    nested = {} if front_lines is None else _nested_and_lists(front_lines, path)[0]
    return _card_type(relative, fields, nested), fields


def _declares_field(field, fields, nested, counts):
    """卡片有沒有寫下這個欄位——空值、巢狀子欄位、序列都算寫了。

    必填欄位問的是「有沒有內容」，已停用欄位問的是「在不在」：`trigger:` 底下掛著
    子欄位、或寫成空值等著接續行，都是同一個要點名的舊寫法。"""
    prefix = field + "."
    return (
        field in fields
        or field in counts
        or any(key == field or key.startswith(prefix) for key in nested)
    )


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


def _body_text(text, closing):
    return "\n".join(text.lstrip("﻿").splitlines()[closing + 1:])[
        : memspec.CARD_DATE_BODY_SCAN_CHARS
    ]


def _iso_of(match):
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def _derived_date(fields, relative, body):
    """(來源, ISO 日期) 或 None。欄位沒寫日期不等於這張卡沒有日期。

    git 首提日不在這裡：那要開行程，只有這三處都落空的卡才值得付（_apply_git_dates）。
    """
    for match in memspec.CARD_DATE_BODY_REGEX.finditer(body):
        stamped = _iso_of(match)
        if stamped:
            return memspec.CARD_DATE_SOURCE_BODY, stamped
    for text in (fields.get(memspec.NAME_FIELD, ""), relative.rsplit("/", 1)[-1]):
        for match in memspec.CARD_DATE_COMPACT_REGEX.finditer(text):
            stamped = _iso_of(match)
            if stamped:
                return memspec.CARD_DATE_SOURCE_NAME, stamped
    return None


def _git_first_commit_dates(vault, paths, budget=memspec.CARD_DATE_GIT_BUDGET_SECONDS):
    """vault 相對路徑 → 該檔首次被加入的提交日；不是 git repo 或 git 不在就回 {}。

    一個行程問完 `paths` 全部；逐檔 `--follow` 是每張卡一個行程。pathspec 收窄到真的
    缺日期的那幾張：實測 303 張卡的庫，全庫走訪 1.3 秒、只問 3 張 0.5 秒，而
    SessionStart 全部的庫加起來只有 2 秒。`core.quotepath=false` 讓中文檔名原樣輸出，
    否則整庫的中文卡全部對不上。
    """
    if budget <= 0 or not paths:
        return {}
    pathspec = list(paths)
    if sum(len(item) for item in pathspec) > memspec.CARD_DATE_GIT_PATHSPEC_MAX_CHARS:
        pathspec = ["."]  # 命令列長度有上限，多到裝不下就整庫走訪
    common = ["git", "-c", "core.quotepath=false", "-C", os.fspath(vault)]
    try:
        prefix = subprocess.run(
            common + ["rev-parse", "--show-prefix"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=budget,
        )
        if prefix.returncode != 0:
            return {}
        log = subprocess.run(
            common + ["log", "--reverse", "--diff-filter=A", "--name-only",
                      "--date=short", "--format=%x00%ad", "--"] + pathspec,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=budget,
        )
        if log.returncode != 0:
            return {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    root = prefix.stdout.strip()
    dates = {}
    stamped = ""
    for line in log.stdout.splitlines():
        if line.startswith("\0"):
            stamped = line[1:].strip()
            continue
        name = line.strip()
        if not name or not stamped:
            continue
        if root:
            if not name.startswith(root):
                continue
            name = name[len(root):]
        dates.setdefault(name, stamped)
    return dates


def _apply_git_dates(vault, raw_cards, budget):
    """第二趟：只對前三處都落空的卡問 git，問到就把那條 FAIL 降成 WARN date-derived。"""
    wanted = [
        card for card in raw_cards
        if any(level == FAIL and rule == "date" for level, rule, _reason in card["findings"])
    ]
    if not wanted:
        return
    dates = _git_first_commit_dates(vault, [card["path"] for card in wanted], budget)
    for card in wanted:
        stamped = dates.get(card["path"])
        if not stamped:
            continue
        card["derived"] = (memspec.CARD_DATE_SOURCE_GIT, stamped)
        card["findings"] = [
            (WARN, "date-derived", memspec.CARD_DATE_DERIVED_REASON.format(
                field=memspec.LAST_VERIFIED_AT_FIELD,
                source=memspec.CARD_DATE_SOURCE_GIT,
                date=stamped,
            ))
            if (level, rule) == (FAIL, "date") else (level, rule, reason)
            for level, rule, reason in card["findings"]
        ]


def _check_card(path, relative, today):
    """(型別, findings, 推得的日期)。findings 是 (level, rule, reason) 的清單。"""
    fields, problem = memspec.frontmatter_fields(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return memspec.DEFAULT_CARD_TYPE, [(FAIL, "unreadable", f"{type(exc).__name__}")], None
    front_lines, closing = memspec.split_frontmatter(text)

    findings = []
    if front_lines is None:
        findings.append((FAIL, "frontmatter", "沒有 frontmatter，型別與必填欄位無法判定"))
        return memspec.DEFAULT_CARD_TYPE, findings, None
    if closing is None:
        findings.append((FAIL, "frontmatter", "frontmatter 缺少結束界線"))
        return memspec.DEFAULT_CARD_TYPE, findings, None

    nested, counts = _nested_and_lists(front_lines, path)
    card_type = _card_type(relative, fields, nested)
    derived = None
    if problem:
        level = FAIL if "重複欄位" in problem else WARN
        findings.append((level, "frontmatter", problem))

    allowed = memspec.card_status_values(card_type)
    status = fields.get(memspec.DECISION_STATUS_FIELD, "").strip()
    if status and status not in allowed:
        findings.append((
            FAIL,
            "status",
            f"status={status} 不在 {'|'.join(allowed)}（{card_type} 型）",
        ))

    for field in memspec.DEPRECATED_CARD_FIELDS:
        if _declares_field(field, fields, nested, counts):
            findings.append((
                WARN, "deprecated-field", memspec.DEPRECATED_FIELD_REASON.format(field=field)
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
        for item in _forbidden_items(front_lines):
            if _is_bare_term(item):
                findings.append((WARN, "forbidden-bare-term", memspec.FORBIDDEN_BARE_TERM_REASON.format(
                    term=item, example=memspec.FORBIDDEN_BARE_TERM_EXAMPLE.format(term=item))))
    elif card_type == memspec.CARD_TYPE_GRANT:
        if not _has_value(memspec.GRANT_EXPIRES_FIELD, fields, nested, counts):
            findings.append((
                INFO,
                "grant-permanent",
                f"缺 {memspec.GRANT_EXPIRES_FIELD}＝永久授權；owner 可能就是要它永久有效",
            ))
    elif card_type in memspec.GENERIC_CARD_TYPES:
        if not _has_value(memspec.ALIASES_FIELD, fields, nested, counts):
            findings.append((
                WARN,
                "aliases",
                f"缺 {memspec.ALIASES_FIELD}（同義詞檢索空手）→ "
                "python epitype/alias_batch.py export <vault>，審核 suggested 後 apply",
            ))
        if not _has_date(fields, nested):
            derived = _derived_date(fields, relative, _body_text(text, closing))
            if derived is None:
                findings.append((FAIL, "date", memspec.CARD_DATE_MISSING_REASON.format(
                    fields="／".join(memspec.CARD_DATE_FIELDS))))
            else:
                findings.append((WARN, "date-derived", memspec.CARD_DATE_DERIVED_REASON.format(
                    field=memspec.LAST_VERIFIED_AT_FIELD, source=derived[0], date=derived[1])))
        reachable = fields.get(memspec.DESCRIPTION_FIELD, "") + "\n" + "\n".join(
            line.strip().lstrip("-").strip()
            for line in front_lines
            if line.strip().startswith("- ")
        )
        if not CJK_REGEX.search(reachable):
            findings.append((
                INFO,
                "no-chinese",
                "description 與 aliases 都沒有中文字，中文提問喚不回；"
                "本場 AI 自主補中文別名即可（owner 2026-09-06 裁定：不必問 owner）",
            ))

    findings.extend(_expiry_warnings(fields, today))
    return card_type, findings, derived


def check_card(path, relative, today=None):
    """單張卡的內容檢查，回 (型別, findings)——與 scan_vault 逐卡走的是同一條路徑。

    寫檔內容閘要在落盤前對「寫入後的內容」跑同一套規則；沒有這個入口的話，閘門就得
    自己再寫一份必填欄位判定，同一張卡兩端會判成不同結果。`relative` 是卡在 vault 內
    的相對路徑（型別判定要看目錄與檔名），可以與 `path` 指向的實體檔不同。

    git 首提日不在這條路徑上：閘門看的是還沒落盤的內容，磁碟上沒有那個檔可查。
    """
    card_type, findings, _derived = _check_card(
        path, relative, today or datetime.now(timezone.utc).date()
    )
    return card_type, findings


def _shorten(text, vault):
    prefix = os.fspath(vault)
    return text.replace(prefix + os.sep, "").replace(prefix, "")


def _vault_findings(vault, relatives):
    """庫層級的四件事——一張一張看不出來的那些。

    決策唯一性（同一 decision_key 只准一張 active）與取代鏈（目標存在、指向相同
    decision_key、鏈不循環、走得到現行卡）**不在這裡重寫**：那一份實作在
    decision_lint 規則 1／2，同一條規則兩份實作就會有兩個答案。這裡把它的結論折進
    同一份報告，另外做兩個真正新的檢查：目錄漏卡、搜尋器漏卡。

    兩個漏卡是 WARN 不是 FAIL：修法是機械重生，不是「這張卡不能收」。
    """
    try:  # lazy import：hook 熱路徑（scan_vaults）不走這條，不為它付 import
        from . import decision_lint, views
    except ImportError:  # Direct script execution keeps the CLI contract.
        import decision_lint
        import views

    findings = []
    try:
        decisions = decision_lint.lint_vault(vault)
    except Exception as exc:
        findings.append((WARN, "decisions", f"決策 lint 無法完成：{type(exc).__name__}: {exc}"))
    else:
        for level, items in ((FAIL, decisions.failures), (WARN, decisions.warnings)):
            for item in items:
                findings.append((
                    level,
                    "decisions",
                    f"規則{item.rule} {item.reason}｜{_shorten(item.path_text, vault)}",
                ))

    managed = set(relatives)
    try:
        listed = views.listed_paths(vault)
    except Exception:
        listed = None
    if listed is None:
        findings.append((WARN, "views", memspec.VIEWS_MISSING_REASON.format(
            directory=memspec.VIEWS_DIRECTORY, vault=vault)))
    else:
        missing = sorted(managed - listed)
        if missing:
            findings.append((WARN, "views", memspec.VIEWS_STALE_REASON.format(
                count=len(missing), cards="／".join(missing[:3]), vault=vault)))

    try:
        indexed = memsearch.indexed_card_paths(vault)
    except Exception:
        indexed = None
    if indexed is None:
        findings.append((WARN, "search-index", memspec.SEARCH_INDEX_MISSING_REASON.format(vault=vault)))
    else:
        missing = sorted(managed - indexed)
        if missing:
            findings.append((WARN, "search-index", memspec.SEARCH_INDEX_STALE_REASON.format(
                count=len(missing), cards="／".join(missing[:3]), vault=vault)))
    return findings


def scan_vault(vault, today=None, deadline=None, deep=False):
    """唯讀掃描一個 vault；deadline 是 time.monotonic() 上限，逾時就標記並停手。

    `deep` 另跑庫層級檢查（決策唯一性／取代鏈／目錄漏卡／索引漏卡）：那是第二趟
    全庫走訪，開場那一行付不起，所以 hook 走的 `scan_vaults` 維持不開。
    """
    vault = Path(vault).resolve()
    today = today or datetime.now(timezone.utc).date()
    raw_cards = []
    by_type = {}
    oversized = 0
    timed_out = False
    relatives = []
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
        relatives.append(relative)
        try:
            card_type, findings, derived = _check_card(path, relative, today)
        except Exception as exc:  # 一張壞卡不得讓整庫掃描停擺
            card_type, findings, derived = (
                memspec.DEFAULT_CARD_TYPE,
                [(FAIL, "unreadable", f"{type(exc).__name__}: {exc}")],
                None,
            )
        by_type[card_type] = by_type.get(card_type, 0) + 1
        if findings:
            raw_cards.append({"path": relative, "type": card_type, "findings": findings, "derived": derived})
    budget = memspec.CARD_DATE_GIT_BUDGET_SECONDS
    if deadline is not None:
        budget = min(budget, deadline - time.monotonic())
    _apply_git_dates(vault, raw_cards, budget)
    cards = [
        {
            "path": card["path"],
            "type": card["type"],
            "fail": sum(1 for level, _rule, _reason in card["findings"] if level == FAIL),
            "warn": sum(1 for level, _rule, _reason in card["findings"] if level == WARN),
            "info": sum(1 for level, _rule, _reason in card["findings"] if level == INFO),
            "derived_date": card["derived"][1] if card["derived"] else None,
            "findings": [
                {"level": level, "rule": rule, "reason": reason}
                for level, rule, reason in card["findings"]
            ],
        }
        for card in raw_cards
    ]
    cards.sort(key=lambda item: (-item["fail"], -item["warn"], item["path"]))
    # 逾時的庫不跑庫層級檢查：半個庫的納管清單會把沒掃到的卡全報成「目錄漏卡」。
    vault_findings = _vault_findings(vault, relatives) if deep and not timed_out else []
    return {
        "vault": str(vault),
        "total": sum(by_type.values()),
        "fail": sum(item["fail"] for item in cards)
        + sum(1 for level, _rule, _reason in vault_findings if level == FAIL),
        "warn": sum(item["warn"] for item in cards)
        + sum(1 for level, _rule, _reason in vault_findings if level == WARN),
        "info": sum(item["info"] for item in cards),
        "fail_cards": sum(1 for item in cards if item["fail"]),
        "by_type": {name: by_type[name] for name in memspec.CARD_TYPES if by_type.get(name)},
        "oversized_skipped": oversized,
        "timed_out": timed_out,
        "cards": cards,
        "vault_findings": [
            {"level": level, "rule": rule, "reason": reason}
            for level, rule, reason in vault_findings
        ],
    }


def scan_vaults(vaults, today=None, time_budget=memspec.CARD_LINT_HOOK_BUDGET_SECONDS):
    """開場那幾行共用的一趟掃描——掃兩趟就是同一份預算付兩次。

    逾時回 None：一個掃不完的庫給出的數字是半個庫的數字，點名錯的數字比不點名更糟。
    """
    deadline = time.monotonic() + time_budget
    reports = []
    for vault in vaults:
        try:
            report = scan_vault(vault, today, deadline)
        except Exception:
            continue
        if report["timed_out"]:
            return None
        reports.append(report)
    return reports


def summary_line(vaults, today=None, time_budget=memspec.CARD_LINT_HOOK_BUDGET_SECONDS, reports=None):
    """SessionStart 的一行，沒有 FAIL 也沒有 WARN 就回 None。"""
    if reports is None:
        reports = scan_vaults(vaults, today, time_budget)
    if reports is None:
        return None
    fail = warn = 0
    worst = None
    worst_fail = -1
    for report in reports:
        fail += report["fail"]
        warn += report["warn"]
        if report["fail"] > worst_fail:
            worst_fail, worst = report["fail"], report["vault"]
    if not (fail or warn) or worst is None:
        return None
    return memspec.CARD_LINT_NOTICE.format(fail=fail, warn=warn, vault=worst)


def _cursor_path(governance):
    return Path(governance) / memspec.FTS_INDEX_DIRECTORY / memspec.CARD_NO_CHINESE_CURSOR_FILENAME


def _read_cursor(governance):
    try:
        value = json.loads(_cursor_path(governance).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_cursor(governance, last):
    path = _cursor_path(governance)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({memspec.CARD_NO_CHINESE_CURSOR_FIELD: last}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # 游標寫不出去只是下一場重複點名，不值得讓開場少一行


def no_chinese_cards(reports):
    """沒有中文的卡，`<庫>\n<庫內相對路徑>` 一張一個鍵；排序固定，游標才有意義。"""
    keys = []
    for report in reports or ():
        for card in report["cards"]:
            if any(item["rule"] == "no-chinese" for item in card["findings"]):
                keys.append(f"{report['vault']}\n{card['path']}")
    keys.sort()
    return keys


def no_chinese_line(reports, governance, limit=memspec.CARD_NO_CHINESE_PER_SESSION):
    """本場順手補中文別名的卡，一行。

    owner 2026-09-06 裁定：缺中文別名不是給 owner 的決定題，AI 自主翻譯就好。所以
    這一行不是警告而是派工，而派工要輪替——游標記上次列到哪，否則每一場都點同三張，
    第四張以後永遠輪不到。
    """
    keys = no_chinese_cards(reports)
    if not keys:
        return None
    last = _read_cursor(governance).get(memspec.CARD_NO_CHINESE_CURSOR_FIELD)
    start = keys.index(last) + 1 if last in keys else 0
    chosen = (keys[start:] + keys[:start])[:limit]
    _write_cursor(governance, chosen[-1])
    return memspec.CARD_NO_CHINESE_LINE.format(
        limit=limit, cards="｜".join(key.split("\n", 1)[1] for key in chosen)
    )


def _print_report(report, output, verbose=False):
    for item in report.get("vault_findings") or ():
        print(f"  {item['level']} {item['rule']}: {item['reason']}", file=output)
    for card in report["cards"]:
        items = [item for item in card["findings"] if verbose or item["level"] != INFO]
        if not items:
            continue
        print(f"{card['path']} [{card['type']}]", file=output)
        for item in items:
            print(f"  {item['level']} {item['rule']}: {item['reason']}", file=output)
    by_type = ",".join(f"{name}:{count}" for name, count in report["by_type"].items()) or "-"
    note = f" oversized={report['oversized_skipped']}" if report["oversized_skipped"] else ""
    note += f" info={report['info']}" if report["info"] else ""
    note += " timed_out=1" if report["timed_out"] else ""
    print(
        f"CARDS total={report['total']} fail={report['fail']} warn={report['warn']} by_type={by_type}{note}",
        file=output,
    )


def _fix_dates(report, dry_run, output):
    """把推得的日期寫成 last_verified_at:。只新增一行——別的欄位、別的位元組不動。

    寫檔原語沿用 alias_batch 那一份（BOM／CRLF 原樣、tmp＋os.replace），這裡再寫一份
    就會出現兩種「安全寫回」。lazy import：hook 熱路徑不為了寫回付這個 import。
    """
    try:
        from . import alias_batch
    except ImportError:  # Direct script execution keeps the CLI contract.
        import alias_batch

    vault = Path(report["vault"])
    written = skipped = 0
    for card in report["cards"]:
        stamped = card.get("derived_date")
        if not stamped:
            continue
        target = vault / card["path"]
        raw = alias_batch._read_card_raw(target)
        # 欄位已經在（值不是日期，所以才推得日期）時再插一行就是重複欄位＝FAIL：
        # 那是把 WARN 修成 FAIL。這種卡留給 owner 自己改那一行。
        already = memspec.LAST_VERIFIED_AT_FIELD in memspec.frontmatter_fields(target)[0]
        if raw is None or already:
            skipped += 1
            continue
        bom, _text, lines_with_ends, closing = raw
        terminator = alias_batch._default_terminator(lines_with_ends)
        line = f"{memspec.LAST_VERIFIED_AT_FIELD}: {stamped}"
        if dry_run:
            print(f"WOULD-FIX {card['path']} +{line}", file=output)
            continue
        lines = list(lines_with_ends)
        lines[closing:closing] = [line + terminator]
        alias_batch._write_card(target, bom, "".join(lines), expected=_text)
        print(f"FIX {card['path']} +{line}", file=output)
        written += 1
    print(f"FIX-DATES{' dry-run' if dry_run else ''} written={written} skipped={skipped}", file=output)


_FIXTURES = {
    "decision-bad.md": "---\nname: decision-bad\ndescription: 2026-09-01 壞決策卡\ndecision_key: k-bad\nstatus: draft\ncurrent_decision_at: 昨天\ndecided_by: owner-explicit\n---\nbody\n",
    "decision-good.md": "---\nname: decision-good\ndescription: 2026-09-01 好決策卡\ndecision_key: k-good\nstatus: active\ncurrent_decision_at: 2026-09-01\ndecided_by: three-way\naliases:\n  - 好決策\n  - good decision\nvalid_until: 2026-08-01\n---\nbody\n",
    "decision-bare.md": "---\nname: decision-bare\ndescription: 2026-09-06 forbidden 四種寫法\n"
    "decision_key: k-bare\nstatus: active\ncurrent_decision_at: 2026-09-06\ndecided_by: three-way\n"
    "aliases:\n  - 裸名詞\n  - bare term\nforbidden:\n  - 兩套參數\n"
    "  - (建議|要不要|是否|應該).{0,12}(納入|採用|改成)兩套參數\n  - 建議改成兩套參數\n"
    "  - 這串裸名詞剛好超過八個字\n---\nbody\n",
    "scar-bad.md": "---\nname: scar-bad\ndescription: 2026-09-01 壞傷疤卡\ntrigger:\n  tool: \"^(Bash)$\"\nmetadata:\n  type: scar\n---\nbody\n",
    "scar-good.md": "---\nname: scar-good\ndescription: 2026-09-01 好傷疤卡\nadvice: 改用 Write 落檔\nincident: 2026-09-02 三個 session 各踩一次\nvalid_until: 2026-08-01\nmetadata:\n  type: scar\n---\nbody\n",
    # 2026-09-09 U-J：只剩 trigger 的舊卡不再被判成傷疤卡，欄位本身只點名一次。
    "trigger-retired.md": "---\nname: trigger-retired\ndescription: 2026-09-09 只留著舊 trigger 的卡\naliases:\n  - 舊攔截欄位\ntrigger: {tool: \"^(Bash)$\", input: \"rm -rf\"}\nmetadata:\n  type: feedback\n---\nbody\n",
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
    "body-dated.md": "---\nname: body-dated\ndescription: 欄位無日期、正文有\naliases:\n  - 正文日期\nmetadata:\n  type: reference\n---\n2026-08-15 那天的紀錄\n",
    "name-20260814.md": "---\nname: name-20260814\ndescription: 只有檔名帶日期\naliases:\n  - 檔名日期\nmetadata:\n  type: project\n---\nbody\n",
    "bom-crlf-nodate.md": "﻿---\r\nname: bom-crlf-nodate\r\ndescription: BOM 加 CRLF 且欄位無日期\r\naliases:\r\n  - 無日期\r\nmetadata:\r\n  type: user\r\n---\r\n2026-08-16 正文日期\r\n",
    "stale-date-field.md": "---\nname: stale-date-field\ndescription: 欄位在但值不是日期\nlast_verified_at: 未知\naliases:\n  - 壞日期\nmetadata:\n  type: habit\n---\n2026-08-17 正文日期\n",
    "project-closed.md": "---\nname: project-closed\ndescription: 2026-09-09 已結案的專案\nstatus: closed\nclosed_at: 2026-09-09\nclosed_by: claude\naliases:\n  - 結案專案\nmetadata:\n  type: project\n---\nbody\n",
    "feedback-closed.md": "---\nname: feedback-closed\ndescription: 2026-09-09 把專案狀態寫到回饋卡上\nstatus: closed\naliases:\n  - 錯層級\nmetadata:\n  type: feedback\n---\nbody\n",
}


def _findings_of(report, path):
    card = next((item for item in report["cards"] if item["path"] == path), None)
    if card is None:
        return set(), card
    return {(item["level"], item["rule"]) for item in card["findings"]}, card


def _reason_of(report, path, rule):
    _rules, card = _findings_of(report, path)
    if card is None:
        return ""
    return next((item["reason"] for item in card["findings"] if item["rule"] == rule), "")


def _git_commit_fixture(vault, stamp):
    """在 temp vault 建一個單提交的 git repo；git 不在就回 False，測試改驗降級路徑。"""
    environment = dict(os.environ)
    address = "epitype@" + "example" + ".invalid"
    environment.update({
        "GIT_AUTHOR_NAME": "epitype",
        "GIT_COMMITTER_NAME": "epitype",
        "GIT_AUTHOR_EMAIL": address,
        "GIT_COMMITTER_EMAIL": address,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
    })
    try:
        for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
            done = subprocess.run(
                ["git", "-C", os.fspath(vault)] + command,
                capture_output=True, text=True, env=environment, timeout=30,
            )
            if done.returncode != 0:
                return False
    except (OSError, subprocess.SubprocessError):
        return False
    return True


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
                "scar FAIL: 缺 advice、incident；殘留的 trigger 只是已停用欄位的 WARN",
                card is not None
                and card["type"] == memspec.CARD_TYPE_SCAR
                and rules == {(FAIL, "required"), (WARN, "deprecated-field")}
                and sum(1 for item in card["findings"] if item["level"] == FAIL) == 2,
            ))
            rules, card = _findings_of(report, "scar-good.md")
            checks.append((
                "scar WARN only: 自報型別＋advice＋incident 齊備，只剩過期警告",
                card is not None and rules == {(WARN, "expired")},
            ))
            rules, card = _findings_of(report, "trigger-retired.md")
            checks.append((
                "U-J：只宣告 trigger 的卡不再被判成 scar，欄位本身只換來一則 WARN",
                card is not None
                and card["type"] == memspec.CARD_TYPE_FEEDBACK
                and rules == {(WARN, "deprecated-field")}
                and memspec.TRIGGER_FIELD in _reason_of(report, "trigger-retired.md", "deprecated-field"),
            ))

            rules, card = _findings_of(report, "grants/grant-ok.md")
            checks.append((
                "事件卡不要求 aliases：grant 缺到期日只是 INFO（可能就是要永久有效），不計入 WARN",
                card is not None
                and card["type"] == memspec.CARD_TYPE_GRANT
                and rules == {(INFO, "grant-permanent")}
                and card["warn"] == 0
                and card["info"] == 1,
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
                "generic FAIL 缺日期；缺 aliases 是 WARN，全英文只 INFO",
                card is not None
                and card["type"] == memspec.CARD_TYPE_FEEDBACK
                and rules == {(FAIL, "date"), (WARN, "aliases"), (INFO, "no-chinese")}
                and card["warn"] == 1,
            ))
            checks.append((
                "六個來源都沒有才 FAIL，訊息說明找過哪些來源",
                all(
                    part in _reason_of(report, "feedback-bad.md", "date")
                    for part in (memspec.LAST_VERIFIED_AT_FIELD, "正文", "git 首次提交")
                ),
            ))
            checks.append((
                "缺別名的 WARN 指向離線別名批次，不是只點名",
                "alias_batch.py export" in _reason_of(report, "feedback-bad.md", "aliases")
                and "apply" in _reason_of(report, "feedback-bad.md", "aliases"),
            ))
            checks.append((
                "沒中文不再問 owner：INFO 明寫本場 AI 自主補（owner 2026-09-06 裁定）",
                "自主補中文別名" in _reason_of(report, "feedback-bad.md", "no-chinese")
                and "請 owner 決定" not in _reason_of(report, "feedback-bad.md", "no-chinese"),
            ))
            rules, card = _findings_of(report, "body-dated.md")
            checks.append((
                "正文第一個日期推得＝WARN date-derived，不是 FAIL",
                card is not None
                and rules == {(WARN, "date-derived")}
                and card["derived_date"] == "2026-08-15"
                and memspec.CARD_DATE_SOURCE_BODY in _reason_of(report, "body-dated.md", "date-derived"),
            ))
            rules, card = _findings_of(report, "name-20260814.md")
            checks.append((
                "檔名裡的 YYYYMMDD 也算推得",
                card is not None
                and rules == {(WARN, "date-derived")}
                and card["derived_date"] == "2026-08-14",
            ))
            checks.append((
                "generic 齊備（日期＋中文別名）即無 finding",
                _findings_of(report, "feedback-good.md")[1] is None,
            ))
            rules, card = _findings_of(report, "reference-dated.md")
            checks.append((
                "generic INFO only: 有日期有別名但全英文，不進 WARN 數",
                card is not None
                and card["type"] == memspec.CARD_TYPE_REFERENCE
                and rules == {(INFO, "no-chinese")}
                and card["warn"] == 0,
            ))

            checks.append((
                "status 的值域按型別：專案卡收得下 closed（結案＝目錄位置）",
                _findings_of(report, "project-closed.md")[1] is None,
            ))
            rules, card = _findings_of(report, "feedback-closed.md")
            checks.append((
                "同一個 closed 寫在回饋卡上＝FAIL，理由指名型別（不然視圖會分錯層級）",
                card is not None
                and rules == {(FAIL, "status")}
                and f"（{memspec.CARD_TYPE_FEEDBACK} 型）"
                in _reason_of(report, "feedback-closed.md", "status"),
            ))

            rules, card = _findings_of(report, "decision-bare.md")
            checks.append((
                "forbidden 寫成裸名詞＝WARN forbidden-bare-term，理由給再提議的句形範例",
                card is not None
                and rules == {(WARN, "forbidden-bare-term")}
                and "兩套參數" in _reason_of(report, "decision-bare.md", "forbidden-bare-term")
                and "(建議|要不要|是否|應該).{0,12}(納入|採用|改成)兩套參數"
                in _reason_of(report, "decision-bare.md", "forbidden-bare-term"),
            ))
            checks.append((
                "同一張卡的句形、帶動詞、過長三項都不算裸名詞：只報那一條",
                card is not None and card["warn"] == 1,
            ))

            # 沒中文的卡不是 owner 的決定題，是本場的順手任務；每場輪替換人。
            hook_reports = scan_vaults([vault], today=today)
            cursor_home = Path(temp_dir).resolve() / "cursor-home"
            first = no_chinese_line(hook_reports, cursor_home, limit=1)
            second = no_chinese_line(hook_reports, cursor_home, limit=1)
            third = no_chinese_line(hook_reports, cursor_home, limit=1)
            checks.append((
                "沒中文的卡列成一行順手任務，帶庫內相對路徑",
                [key.split("\n", 1)[1] for key in no_chinese_cards(hook_reports)]
                == ["feedback-bad.md", "reference-dated.md"]
                and isinstance(first, str)
                and "feedback-bad.md" in first
                and "≤1 張" in first,
            ))
            checks.append((
                "游標讓每場輪替：第二場換下一張，繞完一圈再回到第一張",
                "reference-dated.md" in second
                and "feedback-bad.md" not in second
                and third == first
                and no_chinese_line([], cursor_home) is None,
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
                and report["by_type"].get(memspec.CARD_TYPE_DECISION) == 3
                and report["by_type"].get(memspec.CARD_TYPE_SCAR) == 2
                and not report["timed_out"],
            ))

            deep = scan_vault(vault, today=today, deep=True)
            deep_rules = {(item["level"], item["rule"]) for item in deep["vault_findings"]}
            vault_fail = sum(1 for item in deep["vault_findings"] if item["level"] == FAIL)
            vault_warn = sum(1 for item in deep["vault_findings"] if item["level"] == WARN)
            out = io.StringIO()
            main(["--deep", "--today", "2026-09-06", os.fspath(vault)], output=out)
            checks.append((
                "--deep 加庫層級檢查：目錄漏卡／索引漏卡各一則，決策規則折進同一份報告而非另寫一套",
                {(WARN, "views"), (WARN, "search-index")} <= deep_rules
                and (FAIL, "decisions") in deep_rules
                and vault_fail > 0
                and deep["fail"] == report["fail"] + vault_fail
                and deep["warn"] == report["warn"] + vault_warn
                and report["vault_findings"] == []
                and "WARN search-index:" in out.getvalue(),
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
            checks.append((
                "SessionStart 那一行的 WARN 數不含 INFO",
                report["info"] >= 1
                and isinstance(line, str)
                and f"WARN {report['warn']}" in line
                and f"WARN {report['warn'] + report['info']}" not in line,
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
            plain = text
            out = io.StringIO()
            main(["--verbose", "--today", "2026-09-06", os.fspath(vault)], output=out)
            verbose = out.getvalue()
            checks.append((
                "INFO 只在 --verbose 逐卡列出，摘要行以 info= 另計",
                "grant-permanent" not in plain
                and f"{INFO} grant-permanent:" in verbose
                and f"info={report['info']}" in plain,
            ))

            git_vault = Path(temp_dir).resolve() / "gitvault"
            git_vault.mkdir()
            (git_vault / "git-dated.md").write_text(
                "---\nname: git-dated\ndescription: 哪裡都沒有日期\naliases:\n"
                "  - 靠版本控制\nmetadata:\n  type: habit\n---\nbody\n",
                encoding="utf-8",
            )
            git_report = scan_vault(git_vault, today=today)
            git_rules, git_card = _findings_of(git_report, "git-dated.md")
            checks.append((
                "非 git repo 的庫：四來源皆無就是 FAIL",
                git_card is not None and git_rules == {(FAIL, "date")},
            ))
            if _git_commit_fixture(git_vault, "2026-08-01T00:00:00"):
                git_report = scan_vault(git_vault, today=today)
                git_rules, git_card = _findings_of(git_report, "git-dated.md")
                checks.append((
                    "vault 是 git repo 時，首次提交日推得日期＝WARN date-derived",
                    git_card is not None
                    and git_rules == {(WARN, "date-derived")}
                    and git_card["derived_date"] == "2026-08-01"
                    and memspec.CARD_DATE_SOURCE_GIT
                    in _reason_of(git_report, "git-dated.md", "date-derived"),
                ))
            else:
                checks.append((
                    "git 不可用：_git_first_commit_dates 回空 dict，判定退回 FAIL 而不是爆掉",
                    _git_first_commit_dates(git_vault, ["git-dated.md"]) == {}
                    and git_rules == {(FAIL, "date")},
                ))

            before = (vault / "bom-crlf-nodate.md").read_bytes()
            out = io.StringIO()
            main(["--fix-dates", "--dry-run", "--today", "2026-09-06", os.fspath(vault)], output=out)
            dry = out.getvalue()
            checks.append((
                "--fix-dates --dry-run 只列出要寫的行，一個位元組都不動",
                "WOULD-FIX bom-crlf-nodate.md" in dry
                and f"{memspec.LAST_VERIFIED_AT_FIELD}: 2026-08-16" in dry
                and "written=0" in dry
                and (vault / "bom-crlf-nodate.md").read_bytes() == before,
            ))
            out = io.StringIO()
            main(["--fix-dates", "--today", "2026-09-06", os.fspath(vault)], output=out)
            after = (vault / "bom-crlf-nodate.md").read_bytes()
            checks.append((
                "--fix-dates 只新增一行 last_verified_at，BOM 與 CRLF 原樣保留",
                after.startswith("﻿".encode("utf-8"))
                and b"\r\nlast_verified_at: 2026-08-16\r\n---\r\n" in after
                and after.count(b"\n") == before.count(b"\n") + 1
                and b"\n" not in after.replace(b"\r\n", b"")
                and "written=3 skipped=1" in out.getvalue(),
            ))
            stale = (vault / "stale-date-field.md").read_text(encoding="utf-8")
            checks.append((
                "欄位已存在但值不是日期的卡不寫回：補一行會變成重複欄位 FAIL",
                stale.count(f"{memspec.LAST_VERIFIED_AT_FIELD}:") == 1
                and "未知" in stale,
            ))
            fixed = scan_vault(vault, today=today)
            checks.append((
                "寫回後只剩不該寫的那張還是 date-derived，FAIL 數與型別判定不變",
                [
                    entry["path"]
                    for entry in fixed["cards"]
                    if any(item["rule"] == "date-derived" for item in entry["findings"])
                ] == ["stale-date-field.md"]
                and fixed["fail"] == report["fail"]
                and fixed["by_type"] == report["by_type"],
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 42
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
    parser.add_argument("--verbose", action="store_true", help="also list INFO findings")
    parser.add_argument("--deep", action="store_true",
                        help="also run the vault-level checks: decision uniqueness, supersession chains, view and search-index coverage")
    parser.add_argument("--fix-dates", action="store_true", help="write derived dates back as last_verified_at")
    parser.add_argument("--dry-run", action="store_true", help="with --fix-dates: name the writes, change nothing")
    parsed = parser.parse_args(arguments)
    try:
        report = scan_vault(parsed.vault.expanduser(), parsed.today, deep=parsed.deep)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if parsed.fix_dates:
        try:
            _fix_dates(report, parsed.dry_run, output)
        except OSError as exc:
            print(f"FIX-DATES ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        return 0
    if parsed.json:
        print(json.dumps(report, ensure_ascii=False, indent=1), file=output)
    else:
        _print_report(report, output, parsed.verbose)
    return 1 if parsed.strict and report["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
