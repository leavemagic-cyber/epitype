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


def matched_sentence(prompt, trigger):
    for sentence in memspec.GRANT_SENTENCE_SPLIT_REGEX.split(prompt):
        candidate = sentence.strip()
        if candidate and trigger.search(without_quoted_text(candidate)):
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


def capture_owner_sentence(prompt, vault, event, started_at, kind, replay=None):
    directory_name, trigger, label = CAPTURE_KINDS[kind]
    owner_utterance, _reason = is_owner_utterance(prompt, trigger)
    if not owner_utterance or expired(started_at):
        return None
    sentence = matched_sentence(prompt, trigger)
    if sentence is None:
        return None
    return write_capture(
        vault, directory_name, kind, grant_digest(sentence), label, sentence, event, started_at, replay=replay
    )


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
        summary = one_line(summary if summary is not None else body)[: memspec.CAPTURE_SUMMARY_CHARS]
        provenance = "".join(
            f"{key}: {one_line(value)}\n" for key, value in (replay.fields if replay is not None else ())
        )
        card = (
            "---\n"
            f"name: {name}\n"
            f"description: {label} {stamp[:10]}: {summary}\n"
            f"{memspec.SCOPE_FIELD}: governance-core\n"
            f"captured_at: {stamp}\n"
            f"cwd: {one_line(event.get('cwd'))}\n"
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


def ruling_body(prompt, question):
    """(body, summary) when the owner's answer follows an explicit request for a
    ruling, else None. The question-window rule lives here so the online hook and
    the offline replay judge the same pair the same way."""
    if not _answer_shape(prompt) or not question:
        return None
    flat = one_line(question)
    # Quoted phrases are the assistant talking *about* requests (「請你裁決」in a
    # report), not making one: a match inside quotes does not count. A live
    # request sits at the end of the turn; keep only the window around it.
    quoted = [(m.start(), m.end()) for m in memspec.RULING_QUOTED_TEXT_REGEX.finditer(flat)]
    matches = [
        m for m in memspec.RULING_QUESTION_REGEX.finditer(flat)
        if not any(start <= m.start() < end for start, end in quoted)
    ]
    if not matches or matches[-1].start() < len(flat) - memspec.RULING_QUESTION_TAIL_CHARS:
        return None
    hit = matches[-1]
    window = memspec.RULING_QUESTION_WINDOW_CHARS
    asked = flat[max(0, hit.start() - window): hit.end() + window].strip()
    answer = prompt.strip()
    return f"問（助理）：{asked}\n答（owner 逐字）：{answer}", answer


def capture_ruling(prompt, vault, event, started_at, question=None, replay=None):
    """The owner's answer to a question the agent explicitly put to them.

    question is supplied by the offline replay, which already holds the preceding
    assistant turn; online it is read from the transcript tail as before — after
    the cheap shape checks, so an ordinary prompt still costs no transcript read.
    """
    if expired(started_at) or not _answer_shape(prompt):
        return None
    if question is None:
        question = last_assistant_text(event.get("transcript_path"))
    found = ruling_body(prompt, question)
    if found is None:
        return None
    body, answer = found
    return write_capture(
        vault, memspec.RULING_DIRECTORY, "ruling", grant_digest(prompt), "owner ruling auto-captured", body, event, started_at,
        summary=answer,
        replay=replay,
    )
