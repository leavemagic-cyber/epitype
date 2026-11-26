"""AI 改壞的規則卡由 Epitype 自己重綁；改到 owner 原話或規則欄位的不綁。

界線：AI 改卡不得靜靜改掉 owner 說過的話。所以只有在 owner 原話、決策、狀態、禁止／執行
欄位、規則本文與 rule_provenance 內容都沒變（只改了正文或其他欄位）時才重綁，並在
current_review 寫明這是改卡後的自動重綁。改到其中任何一項，或判斷不出改了什麼，一律不綁，
只把改了哪裡告訴改卡的 AI。

判斷要有一份審過的基準：綁定裡的逐欄摘要（rule_pipeline.payload_parts，新綁的卡都有），
或掛鉤拿到、且雜湊對得上當時綁定的改前全文。整份 sha256 只能說「有變」，說不出哪一欄變了。
這裡只重綁既有的來源審查；沒有 rule_provenance 的舊卡不自動收編（rule_legacy 的既定界線），
已被取代的卡與 Codex 自己生成的記憶檔照既定界線不動。
"""
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from . import card_io, memspec, rule_pipeline, rule_provenance, rule_review

INTACT = "intact"      # 不是來源卡，或綁定仍然對得上：不用動
SAFE = "safe"          # 只改了正文或其他欄位：可以重綁（不套用時停在這裡）
REBOUND = "rebound"    # 已重綁並寫回
UNSAFE = "unsafe"      # 改到原話或規則欄位：不綁
UNKNOWN = "unknown"    # 判斷不出改了什麼，或重綁後仍不成立：不綁
SKIPPED = "skipped"    # 已被取代的卡：照既定界線不動

_LABELS = {
    rule_pipeline.PART_BODY: "正文",
    rule_pipeline.PART_QUOTED: "正文或其他欄位裡的引號引文",
    rule_pipeline.PART_DOCUMENT: "rule_provenance 內容（原話／規則本文／範圍／作者）",
    memspec.OWNER_QUOTE_FIELD: "owner_quote（owner 原話）",
    "text": "text（規則本文）",
}
# rule_review 只說「哪一欄與審過的內容不符」；把那句話對回欄名。
_REVIEW_DISAGREEMENTS = (
    ("stored quotations disagree", memspec.OWNER_QUOTE_FIELD),
    ("decision_key differs", memspec.DECISION_KEY_FIELD),
    ("text differs", "text"),
)
_PROVENANCE_LINE = re.compile(r"^" + re.escape(rule_provenance.FIELD) + r"\s*:")
_MAX_EDIT_CANDIDATES = 8


@dataclass
class Assessment:
    status: str
    changed: tuple = ()
    reason: str = ""


def label(name):
    return _LABELS.get(name, name)


def labels(names):
    return "、".join(label(name) for name in names) or "（無）"


def _superseded(fields):
    status = str(fields.get(memspec.DECISION_STATUS_FIELD, "")).strip().casefold()
    return status == memspec.SUPERSEDED_DECISION_STATUS or bool(fields.get(memspec.SUPERSEDED_BY_FIELD))


def _external(fields):
    try:
        raw = json.loads(fields[rule_provenance.FIELD]) if isinstance(fields.get(rule_provenance.FIELD), str) else None
    except ValueError:
        return False
    return isinstance(raw, dict) and "ref" in raw


def _advice_description(fields, card_text):
    """description 是守門提示的備援文字時，它就是執行內容，不是說明。"""
    blocks, _body = json.loads(rule_pipeline._meaning(card_text, include_source=False))
    return "description" in rule_pipeline._execution_meaning(fields, blocks)


def _protected(name, advice_description):
    if name == rule_pipeline.PART_BODY:
        return False
    if name in (rule_pipeline.PART_QUOTED, rule_pipeline.PART_DOCUMENT):
        return True
    if name == "description":
        return advice_description
    return rule_pipeline.rule_defining(name)


def _reviewed_state(text):
    """(fields, document) when this text is itself a reviewed, still-bound card; else None."""
    fields, problem = memspec.frontmatter_text(text)
    if problem or rule_provenance.FIELD not in fields:
        return None
    review = rule_review.validate(fields)
    if not review.verified or rule_pipeline.payload_issues(review.document, text):
        return None
    return fields, review.document


def _baseline(document, before_texts):
    """(parts, before fields or None) of the last reviewed state, or None when unknown."""
    binding = document.get("payload_binding")
    if (isinstance(binding, dict) and binding.get("parts_version") == rule_pipeline.PARTS_VERSION
            and isinstance(binding.get("parts"), dict)):
        return binding["parts"], None
    for before in before_texts:
        if not isinstance(before, str):
            continue
        try:
            state = _reviewed_state(before)
        except (ValueError, TypeError, OSError):
            state = None
        if state is not None:
            fields, before_document = state
            return rule_pipeline.payload_parts(before, before_document), (fields, before)
    return None


def assess(card_text, *, before_texts=()):
    """Classify one card's current text. `before_texts` are unverified pre-edit
    candidates; one counts only when it was itself bound when written."""
    fields, problem = memspec.frontmatter_text(card_text)
    if problem or rule_provenance.FIELD not in fields:
        return Assessment(INTACT)
    if _superseded(fields):
        return Assessment(SKIPPED, reason="已被取代的卡不重綁")
    review = rule_review.validate(fields)
    if review.document is None:
        return Assessment(UNKNOWN, reason="rule_provenance 讀不出來")
    document = review.document
    if review.verified and not rule_pipeline.payload_issues(document, card_text):
        return Assessment(INTACT)
    if _external(fields):
        return Assessment(UNKNOWN, reason="rule_provenance 存在外部檔，不在卡內重寫")
    disagreements = [name for marker, name in _REVIEW_DISAGREEMENTS
                     if any(marker in issue for issue in review.issues)]
    baseline = _baseline(document, before_texts)
    if baseline is None:
        if disagreements:
            return Assessment(UNSAFE, tuple(disagreements), "欄位與審過的 rule_provenance 不符")
        why = ("綁定早於逐欄紀錄，也拿不到能驗證的改前內容" if review.verified
               else "rule_provenance 本身不成立：" + "; ".join(review.issues))
        return Assessment(UNKNOWN, reason=why)
    parts, before = baseline
    current = rule_pipeline.payload_parts(card_text, document)
    changed = [name for name in sorted(set(parts) | set(current)) if parts.get(name) != current.get(name)]
    changed = list(dict.fromkeys(disagreements + changed))
    advice = _advice_description(fields, card_text) or (
        before is not None and _advice_description(*before))
    protected = tuple(name for name in changed if _protected(name, advice))
    if protected:
        return Assessment(UNSAFE, protected, "改到原話或規則欄位")
    if not review.verified:
        return Assessment(UNKNOWN, tuple(changed), "rule_provenance 本身不成立：" + "; ".join(review.issues))
    if not changed:
        return Assessment(UNKNOWN, reason="逐欄都沒變，整份綁定卻對不上")
    return Assessment(SAFE, tuple(changed))


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _review_record(document, changed, *, session, edited_at, now):
    record = {"at": now, "by": memspec.RULE_SELF_REPAIR_REVIEWER,
              "kind": "auto-rebind-after-ai-edit",
              "basis": memspec.RULE_SELF_REPAIR_BASIS.format(changed=labels(changed)),
              "changed": list(changed), "quote_and_rule_fields_unchanged": True}
    if session:
        record["session"] = str(session)
    if edited_at is not None:
        record["edited_at"] = _iso(edited_at)
    prior = document.get("current_review")
    if isinstance(prior, dict):
        # 只留上一筆的摘要，不一路巢狀下去。
        record["prior"] = {key: prior[key] for key in ("at", "by", "basis") if key in prior}
    return record


def _with_provenance(card_text, document):
    """The card text with only its rule_provenance line replaced by `document`, or
    None when that line cannot be rewritten in place."""
    lines = card_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != memspec.FRONTMATTER_BOUNDARY:
        return None
    closing = next((index for index in range(1, len(lines)) if lines[index].strip()
                    in (memspec.FRONTMATTER_BOUNDARY, memspec.YAML_DOCUMENT_END)), None)
    if closing is None:
        return None
    hits = [index for index in range(1, closing) if _PROVENANCE_LINE.match(lines[index])]
    if len(hits) != 1:
        return None
    line = lines[hits[0]]
    ending = line[len(line.rstrip("\r\n")):]
    lines[hits[0]] = (rule_provenance.FIELD + ": "
                      + json.dumps(rule_provenance.serialize(document), ensure_ascii=False) + ending)
    text = "".join(lines)
    reread, problem = memspec.frontmatter_text(text)
    if problem or rule_provenance.parse(reread) != document:
        return None
    return text


def rebound_text(card_text, changed, *, session=None, edited_at=None, now=None):
    """The card text with only its rule_provenance line rewritten, or None when that
    line cannot be rewritten in place."""
    fields, _problem = memspec.frontmatter_text(card_text)
    document = copy.deepcopy(rule_provenance.parse(fields))
    now = now or datetime.now(timezone.utc).isoformat()
    record = _review_record(document, changed, session=session, edited_at=edited_at, now=now)
    document["current_review"] = record
    bound = rule_pipeline.bind_payload(document, card_text, reviewer=memspec.RULE_SELF_REPAIR_REVIEWER,
                                       reviewed_at=now, rationale=record["basis"][:200])
    return _with_provenance(card_text, bound)


def repair(path, *, before_texts=(), session=None, apply=True, seconds=5.0, now=None):
    """Assess one card on disk and, in the safe class, rebind it in place.

    The card keeps its modification time: the rebind records a review, it is not
    a new edit. A write that raced in after the read is never overwritten."""
    path = Path(path)
    stat = path.stat()
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    result = assess(text, before_texts=before_texts)
    if result.status != SAFE:
        return result
    new_text = rebound_text(text, result.changed, session=session, edited_at=stat.st_mtime, now=now)
    if new_text is None:
        return Assessment(UNKNOWN, result.changed, "rule_provenance 不是單獨一行，無法原地重寫")
    fields, _problem = memspec.frontmatter_text(new_text)
    check = rule_review.validate(fields, path)
    issues = (list(check.issues) if not check.verified
              else rule_pipeline.payload_issues(check.document, new_text))
    if not issues:
        try:
            issues = rule_pipeline.supersession_issues(fields, path, card_text=new_text, seconds=seconds)
        except rule_pipeline.VerificationDeferred as error:
            return Assessment(UNKNOWN, result.changed, "取代鏈這次沒驗完：" + str(error))
    if issues:
        return Assessment(UNKNOWN, result.changed, "重綁後仍不成立：" + "; ".join(issues))
    if not apply:
        return result
    payload = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + new_text.encode("utf-8")
    try:
        card_io.replace_if_unchanged(path, payload, raw)
    except card_io.CardConflict as error:
        return Assessment(UNKNOWN, result.changed, "重綁時卡片又被改了：" + str(error))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return Assessment(REBOUND, result.changed)


def _has_parts(binding):
    return (binding.get("parts_version") == rule_pipeline.PARTS_VERSION
            and isinstance(binding.get("parts"), dict))


def backfill(path, *, apply=True):
    """仍然有效、但綁定裡還沒有逐欄摘要的卡：只把摘要補進綁定。

    逐欄摘要是 AI 改卡後判斷「改了哪一欄」的基準；它出現之前綁的卡沒有，這種卡被改了
    就只能靠改前內容判斷。綁定仍對得上，代表現在這一版就是審過的那一版，摘要記的正是它。
    卡的內容、審核紀錄（current_review、審核人、時間、理由、整體雜湊）與修改時間都不變，
    補完狀態必須跟補之前一樣。回 True＝補了（乾跑時＝會補）。"""
    path = Path(path)
    stat = path.stat()
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    fields, problem = memspec.frontmatter_text(text)
    if problem or rule_provenance.FIELD not in fields or _superseded(fields) or _external(fields):
        return False
    review = rule_review.validate(fields)
    binding = (review.document or {}).get("payload_binding")
    if not review.verified or not isinstance(binding, dict) or _has_parts(binding):
        return False
    status = rule_pipeline.inspect_card(fields, path, card_text=text).status
    if status not in (rule_provenance.VERIFIED, rule_provenance.REVIEWED):
        return False
    filled = copy.deepcopy(rule_provenance.parse(fields))
    filled["payload_binding"] = dict(filled["payload_binding"], parts_version=rule_pipeline.PARTS_VERSION,
                                     parts=rule_pipeline.payload_parts(text, review.document))
    new_text = _with_provenance(text, filled)
    if new_text is None:
        return False
    new_fields, _problem = memspec.frontmatter_text(new_text)
    if rule_pipeline.inspect_card(new_fields, path, card_text=new_text).status != status:
        return False
    if not apply:
        return True
    payload = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + new_text.encode("utf-8")
    try:
        card_io.replace_if_unchanged(path, payload, raw)
    except card_io.CardConflict:
        return False
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return True


def notice(path, outcome):
    """The line for the AI that just edited the card, or None when there is nothing to say."""
    card = Path(path).name
    if outcome.status == REBOUND:
        return memspec.RULE_SELF_REPAIR_REBOUND_NOTICE.format(card=card, changed=labels(outcome.changed))
    if outcome.status == UNSAFE:
        return memspec.RULE_SELF_REPAIR_UNSAFE_NOTICE.format(card=card, changed=labels(outcome.changed))
    if outcome.status == UNKNOWN:
        return memspec.RULE_SELF_REPAIR_UNKNOWN_NOTICE.format(card=card, why=outcome.reason[:120])
    return None


def _legacy_problem(path, fields, text):
    """Why an unenrolled legacy rule card no longer binds, or None when it still does."""
    if not rule_pipeline.is_rule(fields, fields.get("type", ""), text):
        return None
    check = rule_pipeline.legacy_integrity(fields, path, card_text=text)
    if check is None or check.allowed:
        return None
    return "沒有 rule_provenance 的舊規則卡，不自動收編：" + "; ".join(check.issues)


def sweep(vault, *, apply=False, seconds=2.0):
    """夢的夜間掃描：一庫裡被改壞的規則卡，安全的重綁，其餘列出改了哪裡。

    抓得到掛鉤看不到的修改（Bash／腳本寫檔、別的宿主）。回
    {"rebound": [...], "rebindable": [...], "unrepaired": [{path, changed, reason}], "skipped": n,
     "backfilled": [...], "backfillable": [...], "backfill_failed": [{path, reason}]}。
    仍然有效的卡順手補上逐欄摘要（backfill），每張只補一次。"""
    from . import memsearch
    vault = Path(vault)
    root = vault.resolve()
    result = {"rebound": [], "rebindable": [], "unrepaired": [], "skipped": 0,
              "backfilled": [], "backfillable": [], "backfill_failed": []}
    for path in memsearch.card_files(vault):
        if memspec.codex_native_generated(vault, path):
            continue
        relative = Path(path).relative_to(root).as_posix()
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
            fields, problem = memspec.frontmatter_text(text)
            if problem:
                continue
            if rule_provenance.FIELD not in fields:
                if _superseded(fields):
                    result["skipped"] += 1
                    continue
                why = _legacy_problem(path, fields, text)
                if why:
                    result["unrepaired"].append({"path": relative, "changed": [], "reason": why,
                                                 "status": "legacy"})
                continue
            outcome = repair(path, apply=apply, seconds=seconds)
        except (OSError, UnicodeError, ValueError) as error:
            result["unrepaired"].append({"path": relative, "changed": [],
                                         "reason": f"讀不到或判斷失敗：{type(error).__name__}: {error}"})
            continue
        if outcome.status == INTACT:
            try:
                if backfill(path, apply=apply):
                    result["backfilled" if apply else "backfillable"].append(relative)
            except (OSError, UnicodeError, ValueError) as error:
                # 補不成，卡仍是有效的那一版；下次掃描再補。
                result["backfill_failed"].append({"path": relative, "reason": f"{type(error).__name__}: {error}"})
        elif outcome.status == REBOUND:
            result["rebound"].append(relative)
        elif outcome.status == SAFE:
            result["rebindable"].append(relative)
        elif outcome.status == SKIPPED:
            result["skipped"] += 1
        elif outcome.status in (UNSAFE, UNKNOWN):
            reason = ("改到 " + labels(outcome.changed) if outcome.status == UNSAFE
                      else "判斷不出改了哪些欄位：" + outcome.reason)
            result["unrepaired"].append({"path": relative, "changed": list(outcome.changed),
                                         "reason": reason, "status": outcome.status})
    return result
