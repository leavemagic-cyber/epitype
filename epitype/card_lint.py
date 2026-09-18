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
    """決策卡 forbidden 序列的字串項；序列的讀法與別的欄位同源（memspec）。"""
    return memspec.sequence_items(front_lines, memspec.FORBIDDEN_FIELD)


def _is_bare_term(item):
    """裸名詞＝沒有動詞、夠短、沒有正則元字元；三個都成立才算。

    2026-09-06 實測：forbidden 寫成裸名詞時，「為什麼不採用 X」這種說明也被擋，
    因為擋的是名字而不是再提議的動作。"""
    if any(character in memspec.FORBIDDEN_REGEX_METACHARACTERS for character in item):
        return False
    if len(item) > memspec.FORBIDDEN_BARE_TERM_MAX_CHARS:
        return False
    return not any(verb in item for verb in memspec.FORBIDDEN_VERB_HINTS)


def _disarmed_findings(fields, nested):
    """閘門只讀頂層欄位，所以被包進下一層的閘門欄位＝這張卡什麼都不擋。

    2026-09-16：三張剛寫好的裁定卡，欄位全部被包進 `metadata:` 底下一層，卡片外觀完
    全正常、體檢也過，實測 8 個案例一個都沒擋。假裝武裝的卡比沒有卡更糟——後者至少
    不會讓人以為有防護。所以這裡判 FAIL，不是 WARN。"""
    findings = []
    for key in sorted(nested):
        parent, _, child = key.partition(".")
        if child in memspec.CARD_GATE_FIELDS and child not in fields:
            findings.append((
                FAIL,
                "disarmed-field",
                memspec.CARD_DISARMED_REASON.format(field=child, parent=parent),
            ))
    return findings


def _arming_findings(fields, counts, card_type, today):
    """feedback 卡必須武裝，或明講它綁不住。

    2026-09-16 實查：通用庫 400 張卡只有 11 張帶 forbidden，而那 11 張全是產品自己的
    設計決策——170 張記錄 owner 行為糾正的 feedback 卡，武裝數是 0。卡是被規範的那一
    方寫的，不綁自己的寫法永遠比較省事，所以這個選擇不能留給寫卡的人默默做。

    存量卡 WARN（讓數字看得見而不是一次判掉幾百張），裁定日之後建立的卡 FAIL。"""
    # 只認 feedback 的話，兩行 `metadata: type: habit` 就整條繞過去了，而文件寫的是
    # 「與卡片型別無關」。行為卡的型別不只一種，所以按型別收窄的那一份名單才是正本。
    if card_type not in memspec.CARD_ARMING_TYPES:
        return []
    if fields.get(memspec.UNENFORCEABLE_FIELD, "").strip():
        return []
    for field in memspec.CARD_ARMING_FIELDS:
        if fields.get(field, "").strip() or counts.get(field):
            return []
    cutoff = _as_date(memspec.CARD_ARMING_REQUIRED_FROM)
    stamps = [
        _as_date(fields.get(field, "").strip()[:10])
        for field in memspec.CARD_DATE_FIELDS
        if fields.get(field, "").strip()
    ]
    stamps = [stamp for stamp in stamps if stamp is not None]
    level = FAIL if stamps and max(stamps) >= cutoff else WARN
    return [(
        level,
        "unarmed",
        memspec.CARD_UNARMED_REASON.format(
            armed="／".join(memspec.CARD_ARMING_FIELDS),
            unenforceable=memspec.UNENFORCEABLE_FIELD,
        ),
    )]


def _require_findings(fields):
    """`require_when` 與 `require_text` 成對才有意義。

    只寫條件沒寫要求＝什麼都不會被檢查；只寫要求沒寫條件＝閘不知道何時該檢查。
    兩種都是「卡片看起來有規定、實際什麼都不管」，與巢狀欄位同一種靜默失效。"""
    when = fields.get(memspec.REQUIRE_WHEN_FIELD, "").strip()
    text = fields.get(memspec.REQUIRE_TEXT_FIELD, "").strip()
    if bool(when) == bool(text):
        return []
    present, missing = (
        (memspec.REQUIRE_WHEN_FIELD, memspec.REQUIRE_TEXT_FIELD)
        if when
        else (memspec.REQUIRE_TEXT_FIELD, memspec.REQUIRE_WHEN_FIELD)
    )
    return [(
        FAIL,
        "require-pair",
        memspec.CARD_REQUIRE_PAIR_REASON.format(present=present, missing=missing),
    )]


def _pattern_findings(fields, front_lines):
    """樣式編不編得起來、工具名認不認得——不驗的話，卡片壞了也看不出來。

    2026-09-17 對抗審查實測：`forbidden: "a(b"`（括號沒關）、`guard_tool: Shell`（不存在
    的工具）、以及超過長度上限的樣式，三張卡片體檢全部 `fail=0 warn=0`，而三張都是零
    攔截。卡片看起來正常、實際什麼都不擋，正是這套東西最該杜絕的那一種失敗。
    """
    findings = []
    patterns = list(memspec.sequence_items(front_lines, memspec.FORBIDDEN_FIELD))
    for field in (memspec.REQUIRE_WHEN_FIELD, memspec.REQUIRE_TEXT_FIELD):
        value = fields.get(field, "").strip()
        if value:
            patterns.append(value)
    for pattern in patterns:
        problem = memspec.pattern_problem(pattern)
        if problem:
            # WARN 而不是 FAIL：閘的正本行為是退回逐字比對，那張卡照樣在擋。判死的話，
            # 一張 forbidden 寫 Windows 路徑（`C:\Users\…` 的 `\U` 編不起來）的卡會被
            # 寫檔閘直接拒絕，而使用者根本沒有別的欄位可以說「我本來就是要逐字比對」。
            findings.append((
                WARN, "pattern",
                memspec.CARD_PATTERN_BROKEN_REASON.format(
                    pattern=pattern[:60], reason=problem),
            ))
    tool = fields.get(memspec.ACTION_GUARD_TOOL_FIELD, "").strip()
    if tool and tool.casefold() not in memspec.ACTION_GUARD_KNOWN_TOOLS:
        findings.append((
            WARN, "guard-tool",
            memspec.CARD_GUARD_TOOL_UNKNOWN_REASON.format(
                tool=tool, known="、".join(sorted(memspec.ACTION_GUARD_KNOWN_TOOLS))),
        ))
    return findings


def _guard_findings(fields, front_lines):
    """動作守衛欄位的可用性（owner 2026-09-16 解除 §34 的動作條件禁令）。

    §34 對 `trigger:` 的第三個理由是「卡片寫錯會靜默失效」。這裡就是那個理由的答案：
    守衛寫壞由體檢當場判 FAIL，不會等到該擋的時候才發現沒擋。"""
    if memspec.ACTION_GUARD_TOOL_FIELD not in fields:
        return []
    findings = []
    if not fields.get(memspec.ACTION_GUARD_TOOL_FIELD, "").strip():
        findings.append((
            FAIL, "guard", f"{memspec.ACTION_GUARD_TOOL_FIELD} 是空的，這一道守不到任何工具"
        ))
    items = memspec.sequence_items(front_lines, memspec.ACTION_GUARD_ALL_OF_FIELD)
    requires = memspec.sequence_items(front_lines, memspec.ACTION_GUARD_REQUIRES_FIELD)
    if len(requires) > memspec.ACTION_GUARD_MAX_REQUIRES:
        findings.append((
            FAIL,
            "guard",
            f"{memspec.ACTION_GUARD_REQUIRES_FIELD} {len(requires)} 個，"
            f"超過上限 {memspec.ACTION_GUARD_MAX_REQUIRES}",
        ))
    if not items and not requires:
        # 兩種守衛各自成立：比對字面片段的，和問「呼叫少了哪個欄位」的。只認前者會讓
        # 照規範寫的後者被判不合格——而那正是 2026-09-19 修掉的那種互斥規範。
        findings.append((
            FAIL,
            "guard",
            f"缺 {memspec.ACTION_GUARD_ALL_OF_FIELD} 的字面片段，"
            f"也缺 {memspec.ACTION_GUARD_REQUIRES_FIELD} 的必填欄位：守衛卡至少要有一種條件",
        ))
    elif not items:
        pass
    elif len(items) > memspec.ACTION_GUARD_MAX_SUBSTRINGS:
        findings.append((
            FAIL, "guard", f"片段 {len(items)} 個，超過上限 {memspec.ACTION_GUARD_MAX_SUBSTRINGS}"
        ))
    elif len(items) == 1 and len(items[0]) < memspec.ACTION_GUARD_LONE_FRAGMENT_MIN_CHARS:
        findings.append((
            FAIL,
            "guard",
            f"只有一個片段「{items[0]}」且短於 {memspec.ACTION_GUARD_LONE_FRAGMENT_MIN_CHARS} 個字，"
            "會擋掉整類工具；停用整類工具是宿主原生規則的事，請再加一個片段把條件收窄",
        ))
    return findings


def _decider_findings(fields, nested, counts):
    """`decided_by` 的值域與 owner-explicit 的原話要求。

    決策卡與規則卡共用同一份：規則卡的「誰決定」問的是同一件事，兩處各寫一份的話，
    同一個 `owner-explicit` 會在一種卡上要原話、在另一種卡上不要。
    """
    decider = fields.get(memspec.DECIDED_BY_FIELD, "").strip()
    if decider and decider not in memspec.DECIDED_BY_VALUES:
        yield (
            FAIL,
            "decided-by",
            f"{memspec.DECIDED_BY_FIELD}={decider} 不在 {'|'.join(memspec.DECIDED_BY_VALUES)}",
        )
    if decider == memspec.OWNER_EXPLICIT_DECIDER and not _has_value(
        memspec.OWNER_QUOTE_FIELD, fields, nested, counts
    ):
        yield (
            FAIL,
            "required",
            f"{memspec.DECIDED_BY_FIELD}=owner-explicit 缺 {memspec.OWNER_QUOTE_FIELD}",
        )


def _rule_findings(fields, front_lines):
    """規則卡自己的幾項：住哪一層、給哪些宿主、核准日期、規則原句的長度與行數。

    `text` 會被生成器逐位元組抄進核心塊，所以形狀不合的卡在這裡就是 FAIL——生成器
    在組裝時才發現，代價是整個核心塊生不出來。
    """
    layer = fields.get(memspec.RULE_LAYER_FIELD, "").strip()
    if layer and layer not in memspec.RULE_LAYERS:
        yield (FAIL, "layer", memspec.RULE_LAYER_REASON.format(
            field=memspec.RULE_LAYER_FIELD, value=layer, allowed="|".join(memspec.RULE_LAYERS)))
    approved_at = fields.get(memspec.RULE_APPROVED_AT_FIELD, "").strip()
    if approved_at and not memspec.is_iso_date(approved_at):
        yield (FAIL, "date", f"{memspec.RULE_APPROVED_AT_FIELD}={approved_at} 不是 ISO 日期")
    order = fields.get(memspec.RULE_ORDER_FIELD, "").strip()
    if order and _as_int(order) is None:
        yield (FAIL, "field-shape", memspec.RULE_ORDER_NOT_INTEGER_REASON.format(
            field=memspec.RULE_ORDER_FIELD, value=order))
    text = fields.get(memspec.RULE_TEXT_FIELD, "")
    size = len(text.encode("utf-8"))
    if size > memspec.RULE_TEXT_MAX_BYTES:
        yield (FAIL, "rule-text", memspec.RULE_TEXT_TOO_LONG_REASON.format(
            field=memspec.RULE_TEXT_FIELD, size=size, limit=memspec.RULE_TEXT_MAX_BYTES))
    if "\n" in text:
        yield (FAIL, "field-shape", memspec.RULE_TEXT_MULTILINE_REASON.format(
            field=memspec.RULE_TEXT_FIELD))
    # 宿主區：缺 hosts＝共用（不是「還沒填」），所以只檢查寫了的那些卡。值域外的名字
    # 生成得出一個沒有任何下游會讀的區；底線帶 hosts 則是把兩邊都要的一條只給一邊。
    hosts = memspec.sequence_items(front_lines or (), memspec.RULE_HOSTS_FIELD)
    unknown = [host for host in hosts if host not in memspec.RULE_HOSTS]
    if unknown:
        yield (FAIL, "hosts", memspec.RULE_HOSTS_REASON.format(
            field=memspec.RULE_HOSTS_FIELD, value="／".join(unknown),
            allowed="|".join(memspec.RULE_HOSTS)))
    if hosts and layer == memspec.RULE_LAYER_FLOOR:
        yield (FAIL, "hosts-layer", memspec.RULE_HOSTS_ON_FLOOR_REASON.format(
            layer=memspec.RULE_LAYER_FLOOR, field=memspec.RULE_HOSTS_FIELD,
            value="／".join(hosts)))


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _indented_alias_count(front_lines):
    """排在下一層的別名有幾個。

    只數這一個鍵，而且只在體檢裡用來把「缺別名」跟「別名位置不對」分開——共用的
    `sequence_items` 維持只認頂層，動它會改掉規則卡 `forbidden`／`hosts` 的讀法。"""
    if not front_lines:
        return 0
    collecting, found = False, 0
    for raw in front_lines:
        stripped = raw.strip()
        if not stripped or stripped == raw:
            collecting = False
            continue
        if stripped.startswith("-"):
            if collecting and stripped[1:].strip():
                found += 1
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        collecting = key.strip() == memspec.ALIASES_FIELD
        if collecting and value.strip():
            # 行列式（`aliases: [a, b]`）只要判「有沒有」，數幾個不影響任何決定。
            found += 1
            collecting = False
    return found


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

    findings.extend(_disarmed_findings(fields, nested))
    findings.extend(_guard_findings(fields, front_lines))
    findings.extend(_pattern_findings(fields, front_lines))
    findings.extend(_require_findings(fields))
    findings.extend(_arming_findings(fields, counts, card_type, today))

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

    if card_type in (memspec.CARD_TYPE_DECISION, memspec.CARD_TYPE_RULE):
        findings.extend(_decider_findings(fields, nested, counts))
    if card_type == memspec.CARD_TYPE_DECISION:
        stamp = fields.get(memspec.CURRENT_DECISION_AT_FIELD, "").strip()
        if stamp and not memspec.is_iso_date(stamp):
            findings.append((FAIL, "date", f"{memspec.CURRENT_DECISION_AT_FIELD}={stamp} 不是 ISO 日期"))
        for item in _forbidden_items(front_lines):
            if _is_bare_term(item):
                findings.append((WARN, "forbidden-bare-term", memspec.FORBIDDEN_BARE_TERM_REASON.format(
                    term=item, example=memspec.FORBIDDEN_BARE_TERM_EXAMPLE.format(term=item))))
    elif card_type == memspec.CARD_TYPE_RULE:
        findings.extend(_rule_findings(fields, front_lines))
    elif card_type == memspec.CARD_TYPE_GRANT:
        if not _has_value(memspec.GRANT_EXPIRES_FIELD, fields, nested, counts):
            findings.append((
                INFO,
                "grant-permanent",
                f"缺 {memspec.GRANT_EXPIRES_FIELD}＝永久授權；owner 可能就是要它永久有效",
            ))
    elif card_type in memspec.GENERIC_CARD_TYPES:
        if not _has_value(memspec.ALIASES_FIELD, fields, nested, counts):
            # 宿主重排過的卡片，別名會在下一層。搜尋 2026-09-19 起讀得到它，所以這裡不能
            # 再報「缺」——一邊找得到、一邊說沒有，就是同一天修掉的那種互斥規範。
            indented = _indented_alias_count(front_lines)
            if indented:
                findings.append((
                    INFO,
                    "aliases-indented",
                    f"{memspec.ALIASES_FIELD} 有 {indented} 個但被排在下一層（宿主寫入器會這樣重排）；"
                    "搜尋讀得到，頂層仍是規範位置",
                ))
            else:
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
    """SessionStart 的一行，沒有任何 FAIL 就回 None。

    2026-09-09（§35）：WARN 不再開口。WARN 是「這張卡可以更好」，那是夢的清單；
    FAIL 是「喚回端會端出半真的卡」，那才是本場要有人動手的事。WARN 的數字仍附在
    同一行裡，因為要修 FAIL 的人本來就會一起看。
    """
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
    if not fail or worst is None:
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
    # 規則卡：合成規則，不是任何人的真規則（產品不內建行為守則文字）。
    "rule-floor.md": "---\nname: rule-floor\ndescription: 2026-09-09 合成底線規則卡\n"
    "layer: floor\nsection: alpha\norder: 1\ntext: Synthetic floor sentence for the fixtures.\n"
    "decided_by: three-way\napproved_by: synthetic-pair\napproved_at: 2026-09-09\n"
    "aliases:\n  - 合成規則\n  - synthetic rule\nmetadata:\n  type: rule\n---\nbody\n",
    "rule-bad.md": "---\nname: rule-bad\ndescription: 2026-09-09 壞規則卡\n"
    "layer: nowhere\nsection: alpha\norder: 甲\ntext: " + "x" * (400 + 1) + "\n"
    "decided_by: owner-explicit\napproved_by: synthetic-pair\napproved_at: 昨天\n"
    "aliases:\n  - 壞規則\nmetadata:\n  type: rule\n---\nbody\n",
    # 宿主區（U-R3）：hosts 值域內＋常駐層是乾淨的；值域外的名字與底線層帶 hosts 都是 FAIL。
    "rule-hosts.md": "---\nname: rule-hosts\ndescription: 2026-09-10 合成宿主區規則卡\n"
    "layer: resident\nsection: alpha\norder: 2\ntext: Synthetic host-only sentence for the fixtures.\n"
    "decided_by: three-way\napproved_by: synthetic-pair\napproved_at: 2026-09-10\n"
    "hosts:\n  - claude\n  - codex\n"
    "aliases:\n  - 宿主區規則\n  - host zone rule\nmetadata:\n  type: rule\n---\nbody\n",
    "rule-hosts-bad.md": "---\nname: rule-hosts-bad\ndescription: 2026-09-10 宿主區寫壞的規則卡\n"
    "layer: floor\nsection: alpha\norder: 3\ntext: Synthetic floor sentence that must stay shared.\n"
    "decided_by: three-way\napproved_by: synthetic-pair\napproved_at: 2026-09-10\n"
    "hosts: [claude, gemini]\n"
    "aliases:\n  - 壞宿主區\nmetadata:\n  type: rule\n---\nbody\n",
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
    # 行為卡二選一：武裝，或明講綁不住。這張走後者，示範那個出口長什麼樣。
    "feedback-good.md": "---\nname: feedback-good\ndescription: 2026-09-01 中文摘要\naliases:\n  - 別名\nunenforceable: 判斷型，訊息裡沒有可比對的字面訊號\nmetadata:\n  type: feedback\n---\nbody\n",
    "reference-dated.md": "---\nname: reference-dated\ndescription: english only reference card\nlast_verified_at: 2026-09-01\naliases:\n  - alias only in english\nmetadata:\n  type: reference\n---\nbody\n",
    "bom-crlf.md": "﻿---\r\nname: bom-crlf\r\ndescription: 2026-09-01 BOM 加 CRLF 的卡\r\naliases:\r\n  - 別名\r\nmetadata:\r\n  type: project\r\n---\r\nbody\r\n",
    "document-end.md": "---\nname: document-end\ndescription: 2026-09-01 以三點結尾的卡\naliases:\n  - 三點\nunenforceable: 判斷型\n...\nbody\n",
    "no-boundary.md": "---\nname: no-boundary\ndescription: 2026-09-01 沒有結束界線\n",
    "duplicate-key.md": "---\nname: duplicate-key\ndescription: 2026-09-01 重複欄位\ndescription: 第二個\naliases:\n  - 重複\nmetadata:\n  type: user\n---\nbody\n",
    "habit-empty-aliases.md": "---\nname: habit-empty-aliases\ndescription: 2026-09-01 空別名序列\naliases: []\nmetadata:\n  type: habit\n---\nbody\n",
    "body-dated.md": "---\nname: body-dated\ndescription: 欄位無日期、正文有\naliases:\n  - 正文日期\nmetadata:\n  type: reference\n---\n2026-08-15 那天的紀錄\n",
    "name-20260814.md": "---\nname: name-20260814\ndescription: 只有檔名帶日期\naliases:\n  - 檔名日期\nmetadata:\n  type: project\n---\nbody\n",
    "bom-crlf-nodate.md": "﻿---\r\nname: bom-crlf-nodate\r\ndescription: BOM 加 CRLF 且欄位無日期\r\naliases:\r\n  - 無日期\r\nmetadata:\r\n  type: user\r\n---\r\n2026-08-16 正文日期\r\n",
    "stale-date-field.md": "---\nname: stale-date-field\ndescription: 欄位在但值不是日期\nlast_verified_at: 未知\naliases:\n  - 壞日期\nmetadata:\n  type: habit\n---\n2026-08-17 正文日期\n",
    "project-closed.md": "---\nname: project-closed\ndescription: 2026-09-09 已結案的專案\nstatus: closed\nclosed_at: 2026-09-09\nclosed_by: claude\naliases:\n  - 結案專案\nmetadata:\n  type: project\n---\nbody\n",
    "feedback-closed.md": "---\nname: feedback-closed\ndescription: 2026-09-09 把專案狀態寫到回饋卡上\nstatus: closed\naliases:\n  - 錯層級\nunenforceable: 判斷型\nmetadata:\n  type: feedback\n---\nbody\n",
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

            checks.append((
                "rule 卡欄位齊備即無 finding，且不因缺日期欄位被點名（approved_at 就是它的日期）",
                _findings_of(report, "rule-floor.md")[1] is None
                and report["by_type"].get(memspec.CARD_TYPE_RULE) == 4,
            ))
            rules, card = _findings_of(report, "rule-bad.md")
            checks.append((
                "rule FAIL：layer 值域外、order 非整數、text 超上限、approved_at 非 ISO、"
                "owner-explicit 缺 owner_quote",
                card is not None
                and card["type"] == memspec.CARD_TYPE_RULE
                and rules == {
                    (FAIL, "layer"), (FAIL, "field-shape"), (FAIL, "rule-text"),
                    (FAIL, "date"), (FAIL, "required"),
                }
                and str(memspec.RULE_TEXT_MAX_BYTES) in _reason_of(report, "rule-bad.md", "rule-text")
                and memspec.OWNER_QUOTE_FIELD in _reason_of(report, "rule-bad.md", "required"),
            ))

            checks.append((
                "rule hosts 值域內＋常駐層＝無 finding：宿主區卡不因為多了一個欄位被點名",
                _findings_of(report, "rule-hosts.md")[1] is None,
            ))
            rules, card = _findings_of(report, "rule-hosts-bad.md")
            checks.append((
                "rule FAIL：hosts 值域外的宿主名、以及底線層不得帶 hosts",
                card is not None
                and card["type"] == memspec.CARD_TYPE_RULE
                and rules == {(FAIL, "hosts"), (FAIL, "hosts-layer")}
                and "gemini" in _reason_of(report, "rule-hosts-bad.md", "hosts")
                and memspec.RULE_LAYER_FLOOR
                in _reason_of(report, "rule-hosts-bad.md", "hosts-layer"),
            ))

            rules, card = _findings_of(report, "scar-bad.md")
            checks.append((
                "scar FAIL: 缺 advice、incident；殘留的 trigger 只是已停用欄位的 WARN",
                card is not None
                and card["type"] == memspec.CARD_TYPE_SCAR
                and rules == {(FAIL, "required"), (WARN, "deprecated-field"), (WARN, "unarmed")}
                and sum(1 for item in card["findings"] if item["level"] == FAIL) == 2,
            ))
            rules, card = _findings_of(report, "scar-good.md")
            checks.append((
                "scar WARN only: 自報型別＋advice＋incident 齊備；過期警告，外加還沒表態擋不擋",
                card is not None and rules == {(WARN, "expired"), (WARN, "unarmed")},
            ))
            rules, card = _findings_of(report, "trigger-retired.md")
            checks.append((
                "U-J：只宣告 trigger 的卡不再被判成 scar；退役欄位不算武裝，所以同時還欠一則 unarmed",
                card is not None
                and card["type"] == memspec.CARD_TYPE_FEEDBACK
                and rules == {(WARN, "deprecated-field"), (WARN, "unarmed")}
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
                card is not None and card["type"] == memspec.CARD_TYPE_CORRECTION
                and rules == {(FAIL, "required"), (WARN, "unarmed")},
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
                "generic FAIL 缺日期；缺 aliases 與未武裝各一則 WARN，全英文只 INFO",
                card is not None
                and card["type"] == memspec.CARD_TYPE_FEEDBACK
                and rules == {(FAIL, "date"), (WARN, "aliases"), (WARN, "unarmed"), (INFO, "no-chinese")}
                and card["warn"] == 2,
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
                and report["fail"] > 0
                and f"FAIL {report['fail']}／WARN {report['warn']}" in line
                and str(vault) in line,
            ))
            checks.append((
                "乾淨庫沒有那一行；逾時也沒有（半個庫的數字不點名）",
                summary_line([vault / "grants" / "nowhere"], today=today) is None
                and summary_line([vault], today=today, time_budget=-1.0) is None,
            ))
            # §35：只有 WARN 的庫在開場不出聲——WARN 是夢的清單，不是本場要動手的事。
            warn_vault = Path(temp_dir).resolve() / "warn-only"
            warn_vault.mkdir()
            (warn_vault / "body-dated.md").write_bytes(_FIXTURES["body-dated.md"].encode("utf-8"))
            warn_report = scan_vault(warn_vault, today=today)
            checks.append((
                "只有 WARN 的庫在 SessionStart 不出聲（FAIL 才開口）",
                warn_report["fail"] == 0
                and warn_report["warn"] > 0
                and summary_line([warn_vault], today=today) is None,
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
                git_card is not None and git_rules == {(FAIL, "date"), (WARN, "unarmed")},
            ))
            if _git_commit_fixture(git_vault, "2026-08-01T00:00:00"):
                git_report = scan_vault(git_vault, today=today)
                git_rules, git_card = _findings_of(git_report, "git-dated.md")
                checks.append((
                    "vault 是 git repo 時，首次提交日推得日期＝WARN date-derived",
                    git_card is not None
                    and git_rules == {(WARN, "date-derived"), (WARN, "unarmed")}
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
    total = 47
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
