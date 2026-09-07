import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，避免繁中輸出在程式進入點就中斷。
"""owner 原話捕捉核心：線上 hook 與離線回放（harvest）共用同一份規則。

規則若在兩處各寫一份，回放出來的歷史卡就會和今天線上寫的卡不同標準；故觸發、
憑證遮罩、digest 去重、frontmatter 欄位一律只在這裡實作一次。
"""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

try:
    from . import memsearch, memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import memsearch
    import memspec


# 寫入結果分類：回放要能分辨「新寫」與「早就有了」，線上不看這個值。
STATUS_WRITTEN = "written"
STATUS_DUPLICATE = "duplicate"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"


class Replay:
    """離線回放的來源與去向：stamp 是原話當時的時間、fields 是額外的 frontmatter
    來源欄、status 回報寫入結果。reindex 預設關閉，因為回放結束才建一次索引，
    每張卡建一次會把一次大整理變成數千次索引重建。"""

    __slots__ = ("stamp", "fields", "reindex", "status")

    def __init__(self, stamp=None, fields=(), reindex=False):
        self.stamp = stamp
        self.fields = tuple(fields)
        self.reindex = reindex
        self.status = None


def _report(replay, status):
    if replay is not None:
        replay.status = status


def one_line(value):
    return " ".join(str(value or "").split())


def without_quoted_text(prompt):
    return memspec.GRANT_QUOTED_TEXT_REGEX.sub("", prompt)


def expired(started_at):
    """Hook 逾時閘。started_at is None = 離線回放，不受 hook 預算限制。"""
    return started_at is not None and time.monotonic() - started_at >= memspec.HOOK_TIMEOUT_SECONDS


def is_owner_utterance(prompt, trigger=memspec.GRANT_TRIGGER_REGEX):
    """Reject non-owner and ambiguous prompt shapes before trigger matching."""
    if not isinstance(prompt, str) or not prompt.strip():
        return False, "empty-prompt"
    folded = prompt.casefold()
    if any(marker in folded for marker in memspec.GRANT_REJECT_MARKERS):
        return False, "system-injected-marker"
    if memspec.GRANT_FENCED_CODE_MARKER in prompt:
        return False, "fenced-code"
    if memspec.GRANT_LEADING_TAG_REGEX.search(prompt):
        return False, "tagged-block"
    if len(prompt) > memspec.GRANT_MAX_CHARS:
        return False, "over-max-chars"
    if len(memspec.GRANT_NEWLINE_REGEX.findall(prompt)) > memspec.GRANT_MAX_NEWLINES:
        return False, "too-many-newlines"

    quoted = memspec.GRANT_QUOTED_TEXT_REGEX.findall(prompt)
    if (
        any(trigger.search(value) for value in quoted)
        and not trigger.search(without_quoted_text(prompt))
    ):
        return False, "quoted-trigger-only"
    return True, "owner-utterance-shape"


# Grants and corrections are the two owner sentences that must survive the
# session they were said in; both take the same source-checked capture path.
CAPTURE_KINDS = {
    "grant": (memspec.GRANT_DIRECTORY, memspec.GRANT_TRIGGER_REGEX, "owner grant auto-captured"),
    "correction": (
        memspec.CORRECTION_DIRECTORY,
        memspec.CORRECTION_TRIGGER_REGEX,
        "owner correction auto-captured",
    ),
}


def owner_reply(text):
    """owner 自己說的那半。

    owner 常貼一段助理原文再用 `<-`／`<=`／`《` 接自己的話；標記前那半是助理的字，
    拿它當觸發詞或長度證據等於讓助理替 owner 作證（實測 7 例助理長段分析被存成
    owner 事件卡）。標記後沒有字時整句都是引文，回空字串。
    """
    matches = list(memspec.CAPTURE_REPLY_MARKER_REGEX.finditer(text or ""))
    if not matches:
        return (text or "").strip()
    return text[matches[-1].end():].strip()


def _segments(text):
    """子句層取證：反問子句丟掉，其餘保留原樣（含結尾標點，句首型觸發詞才認得）。

    問號子句不整句丟——真裁定常把反問嵌在多子句裡（「不是!只有…是標準合約…這樣
    了解嗎?」）；問號子句再按逗號切，只丟帶疑問詞的那半。
    """
    kept = []
    parts = memspec.CAPTURE_CLAUSE_SPLIT_REGEX.split(text)
    for index in range(0, len(parts), 2):
        clause = parts[index] + (parts[index + 1] if index + 1 < len(parts) else "")
        if not clause.strip():
            continue
        if memspec.CAPTURE_CLAUSE_QUESTION_REGEX.search(clause):
            kept.extend(
                piece for piece in memspec.CAPTURE_SUBCLAUSE_SPLIT_REGEX.split(clause)
                if piece.strip() and not memspec.CAPTURE_QUESTION_WORD_REGEX.search(piece)
            )
        else:
            kept.append(clause)
    return kept


def evidence_text(text):
    """The part of an owner utterance that may prove a decision, veto phrases removed.

    Veto phrases are deleted rather than dropping the whole segment: 「不要再跟我說
    沒有資料」 is a standing rule that merely contains a communication-style phrase,
    while 「白話跟我說明」 is nothing but the phrase and must not survive.
    """
    stripped = (memspec.CAPTURE_VETO_REGEX.sub("", piece) for piece in _segments(owner_reply(text)))
    return " ".join(piece for piece in stripped if piece.strip())


def is_decisive(evidence):
    """True when the evidence says what to do (or not do), or names a standing rule.

    Restating a standing rule (「我說過了，執行到完」) is itself the decision, so the
    standing table counts here too — without it a third of the measured real
    corrections read as content-free.
    """
    return bool(
        memspec.CAPTURE_DECISIVE_REGEX.search(evidence)
        or memspec.CAPTURE_STANDING_REGEX.search(evidence)
        # 「你可以用到 7 個核心」的授權句只有觸發詞本身是證據；觸發詞不算決定性的話，
        # 具體範圍的真授權會整批掉出去。
        or memspec.GRANT_TRIGGER_REGEX.search(evidence)
        # 同理，「你搞錯了，那欄是給 A 用的，不是給 B」的決定就在糾正詞本身。
        or memspec.CORRECTION_TRIGGER_REGEX.search(evidence)
    )


def looks_generated(text):
    """True when the candidate has the shape of assistant prose, not an owner line."""
    body = one_line(text)
    if len(body) > memspec.CAPTURE_OWNER_MAX_CHARS:
        return True
    digits = sum(character.isdigit() for character in body)
    return (
        len(body) >= memspec.CAPTURE_DIGIT_WINDOW_CHARS
        and digits * memspec.CAPTURE_DIGIT_WINDOW_CHARS
        >= memspec.CAPTURE_DIGIT_MAX_PER_WINDOW * len(body)
    )


def hollow_answer(text):
    """A one-word acknowledgement with no scope: pinned on recall, says nothing."""
    reply = owner_reply(text)
    return len(reply) < memspec.CAPTURE_ACK_MIN_CHARS and not memspec.CAPTURE_STANDING_REGEX.search(reply)


def candidate_shape(prompt):
    """(ok, reason) for the shape checks every kind shares, before any trigger."""
    reply = owner_reply(prompt)
    if not reply:
        return False, "quoted-assistant-text-only"
    if looks_generated(reply):
        return False, "assistant-prose-shape"
    if hollow_answer(prompt):
        return False, "scopeless-acknowledgement"
    if not is_decisive(evidence_text(prompt)):
        return False, "no-decisive-clause"
    return True, "decisive-owner-clause"


def matched_sentence(prompt, trigger):
    """The sentence to store: the trigger has to sit in that sentence's own evidence.

    Searching the raw sentence is what let a trigger inside a question, an empty
    acknowledgement, or pasted assistant text write a card (2026-09-06 measurement).
    """
    for sentence in memspec.GRANT_SENTENCE_SPLIT_REGEX.split(prompt):
        candidate = sentence.strip()
        if not candidate:
            continue
        evidence = evidence_text(without_quoted_text(candidate))
        if trigger.search(evidence) and is_decisive(evidence):
            return candidate
    return None


def grant_digest(sentence):
    return hashlib.sha256(one_line(sentence).encode("utf-8")).hexdigest()[:12]


def existing_capture(vault, directory_name, kind, digest):
    """同 digest 的既有卡（去重規則的唯一實作），沒有就 None。同一句話寫兩張卡＝
    喚回時兩條佔位，所以線上與回放必須問同一個問題。"""
    try:
        return next(iter(sorted((vault / directory_name).glob(f"{kind}-*-{digest}.md"))), None)
    except OSError:
        return None


def capture_owner_sentence(prompt, vault, event, started_at, kind, replay=None, question=None):
    found = _classify_event(prompt, event, started_at, question)
    if found is None or found[0] != kind:
        return None
    return _capture_classified(found, vault, event, started_at, replay)


def write_capture(vault, directory_name, kind, digest, label, body, event, started_at, summary=None, replay=None):
    # A captured card is persistent, indexed, and re-injected later: credential-shaped
    # text never earns that (adversarial review 2026-09-03 #5). The sentence still
    # exists in the transcript; Epitype simply does not copy it into the vault.
    if memspec.CAPTURE_REJECT_REGEX.search(body):
        _report(replay, STATUS_REJECTED)
        return None
    directory = vault / directory_name
    try:
        if existing_capture(vault, directory_name, kind, digest) is not None:
            _report(replay, STATUS_DUPLICATE)
            return None
        directory.mkdir(parents=True, exist_ok=True)
        # 回放時用原話當時的時間，否則整批歷史卡會全部標成今天，日期就不再是證據。
        stamp = (replay.stamp if replay is not None else None) or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        name = f"{kind}-{stamp[:10].replace('-', '')}-{digest}"
        target = directory / f"{name}.md"
        # The description is what recall injects; it must carry the owner's words,
        # not just a label, or the model has to open the file to learn anything.
        summary = one_line(summary if summary is not None else body)
        provenance = "".join(
            f"{key}: {one_line(value)}\n" for key, value in (replay.fields if replay is not None else ())
        )
        card = (
            "---\n"
            f"name: {name}\n"
            f"description: {label} {stamp[:10]}: {summary}\n"
            f"{memspec.SCOPE_FIELD}: governance-core\n"
            f"captured_at: {stamp}\n"
            # cwd 是落點的證據，也是事後歸戶（capture_route）唯一能依據的來源專案。
            f"{memspec.CWD_FIELD}: {one_line(event.get('cwd'))}\n"
            f"session_id: {one_line(event.get('session_id', event.get('sessionId')))}\n"
            f"{provenance}"
            "---\n"
            f"{body}\n"
        )
        with memspec.file_lock(target, memspec.GRANT_LOCK_SECONDS) as locked:
            if not locked or target.exists():
                _report(replay, STATUS_DUPLICATE if target.exists() else STATUS_ERROR)
                return None
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(card, encoding="utf-8")
            os.replace(temporary, target)
        _report(replay, STATUS_WRITTEN)
        # The staleness grace window would hide the new card from the very next
        # prompt; a captured owner sentence must be recallable immediately. When
        # the index lock is taken by another hook, age the index instead so the
        # next reader rebuilds it.
        if (replay.reindex if replay is not None else True) and not expired(started_at):
            elapsed = 0.0 if started_at is None else time.monotonic() - started_at
            remaining = memspec.HOOK_TIMEOUT_SECONDS - elapsed
            wait = max(0.0, min(memspec.GRANT_LOCK_SECONDS, remaining - 0.5))
            if memsearch.build_index(vault, lock_timeout=wait).get("status") != "built":
                memsearch.mark_stale(vault)
        return target
    except (OSError, sqlite3.Error):
        _report(replay, STATUS_ERROR)
        return None


def assistant_text(item):
    """Text of one transcript record if it is an assistant turn (Claude or Codex shape)."""
    if not isinstance(item, dict) or item.get("isSidechain") is True:
        return None
    if item.get("type") == "assistant":
        message = item.get("message")
    elif item.get("type") == "response_item":
        message = item.get("payload")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
    else:
        return None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") in ("text", "output_text") and isinstance(block.get("text"), str)
    )


def last_assistant_text(transcript_path):
    """Last assistant turn in the transcript tail, or None. Reads a bounded window only."""
    try:
        path = Path(transcript_path)
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - memspec.RULING_TAIL_BYTES))
            data = stream.read()
    except (OSError, TypeError, ValueError):
        return None
    turn = []
    for raw in reversed(data.split(b"\n")):
        try:
            item = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        text = assistant_text(item)
        if text is None:
            if turn and item.get("type") in ("user", "response_item"):
                break
            continue
        if text.strip():
            turn.append(text)
    return "".join(reversed(turn)) if turn else None


def _answer_shape(prompt):
    owner_utterance, _reason = is_owner_utterance(prompt, memspec.NEVER_MATCH_REGEX)
    return owner_utterance and len(prompt.strip()) >= memspec.RULING_MIN_ANSWER_CHARS


def ruling_question(question):
    """The window around an explicit request for a decision, or None.

    Quoted phrases are the assistant talking *about* requests (「請你裁決」in a
    report), not making one: a match inside quotes does not count. A live
    request sits at the end of the turn; keep only the window around it.
    """
    if not question:
        return None
    flat = one_line(question)
    quoted = [(m.start(), m.end()) for m in memspec.RULING_QUOTED_TEXT_REGEX.finditer(flat)]
    matches = [
        m for m in memspec.RULING_QUESTION_REGEX.finditer(flat)
        if not any(start <= m.start() < end for start, end in quoted)
    ]
    if not matches or matches[-1].start() < len(flat) - memspec.RULING_QUESTION_TAIL_CHARS:
        return None
    hit = matches[-1]
    window = memspec.RULING_QUESTION_WINDOW_CHARS
    return flat[max(0, hit.start() - window): hit.end() + window].strip()


def ruling_body(prompt, question):
    """(body, summary) when the owner's own sentence carries a ruling, else None.

    2026-09-06: the assistant's request alone used to be the whole trigger, so any
    next prompt — a bare question included — became a ruling (27 of 94 measured
    rulings were pure questions). Now the owner's sentence must itself hold a
    decisive clause, and the ruling stands either because the assistant did ask for
    a decision or because the sentence carries standing scope (以後／一律／不用問我).
    """
    if not _answer_shape(prompt):
        return None
    shaped, _reason = candidate_shape(prompt)
    if not shaped:
        return None
    asked = ruling_question(question)
    answer = prompt.strip()
    if asked:
        return f"問（助理）：{asked}\n答（owner 逐字）：{answer}", answer
    evidence = evidence_text(prompt)
    # No request on the wire: the sentence has to carry the ruling on its own —
    # either standing scope (以後／一律／我說過), or several decisive clauses, which
    # is what separates a rule the owner is laying down from a one-off order
    # (「直接刪」 has one decisive word and nothing else; a ruling states a shape).
    if memspec.CAPTURE_STANDING_REGEX.search(evidence):
        return answer, answer
    if (
        len(memspec.CAPTURE_DECISIVE_REGEX.findall(evidence)) >= memspec.CAPTURE_RULING_MIN_DECISIVE
        and len(one_line(evidence)) >= memspec.CAPTURE_RULING_MIN_CHARS
    ):
        return answer, answer
    return None


def classify(prompt, question=None):
    """(kind, body, summary, digest) for the one card this utterance earns, or None.

    Online capture, offline replay, and the precision harness must judge a sentence
    identically; a second copy of this ordering is how the replayed history stopped
    matching what the live hook writes.

    2026-09-06: a correction trigger (不要再／etc.) sitting inside the owner's answer
    to the assistant's own decision question was always filed as correction, so
    「就用第二案，以後都不要再問這件事」 answering 「請你確認要用哪個」 never reached
    ruling_body. correction only yields to ruling when the assistant's prior turn
    itself asked for a decision (ruling_question) — no question on the wire keeps
    correction first, so an owner-initiated correction like 「不是！只有第一種算正式
    合約…」 said with no request pending stays correction.
    """
    for kind in ("correction", "grant"):
        directory, trigger, _label = CAPTURE_KINDS[kind]
        owner_utterance, _reason = is_owner_utterance(prompt, trigger)
        if not owner_utterance:
            continue
        shaped, _reason = candidate_shape(prompt)
        if not shaped:
            continue
        sentence = matched_sentence(prompt, trigger)
        if sentence is not None:
            if kind == "correction" and ruling_question(question) is not None:
                found = ruling_body(prompt, question)
                if found is not None:
                    body, summary = found
                    return "ruling", body, summary, prompt
            return kind, sentence, None, sentence
    owner_utterance, _reason = is_owner_utterance(prompt, memspec.NEVER_MATCH_REGEX)
    if owner_utterance:
        found = ruling_body(prompt, question)
        if found is not None:
            body, summary = found
            return "ruling", body, summary, prompt
    return None


def _classify_event(prompt, event, started_at, question=None):
    """Resolve context before choosing the one card; reject noise before tail I/O."""
    if expired(started_at) or not is_owner_utterance(prompt, memspec.NEVER_MATCH_REGEX)[0]:
        return None
    if not candidate_shape(prompt)[0]:
        return None
    if question is None:
        question = last_assistant_text(event.get("transcript_path"))
    if expired(started_at):
        return None
    return classify(prompt, question)


def _capture_classified(found, vault, event, started_at, replay):
    kind, body, summary, digest_source = found
    if kind == "ruling":
        directory, label = memspec.RULING_DIRECTORY, "owner ruling auto-captured"
    else:
        directory, _trigger, label = CAPTURE_KINDS[kind]
    return write_capture(
        vault, directory, kind, grant_digest(digest_source), label, body, event, started_at,
        summary=summary, replay=replay,
    )


def capture_event(prompt, vault, event, started_at, question=None, replay=None):
    """Capture one event using the same prompt + question decision as replay."""
    found = _classify_event(prompt, event, started_at, question)
    return _capture_classified(found, vault, event, started_at, replay) if found is not None else None


def capture_ruling(prompt, vault, event, started_at, question=None, replay=None):
    """Compatibility entry point; classification includes the preceding question."""
    found = _classify_event(prompt, event, started_at, question)
    if found is None or found[0] != "ruling":
        return None
    return _capture_classified(found, vault, event, started_at, replay)
