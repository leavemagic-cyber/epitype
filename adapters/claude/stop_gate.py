import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Stop adapter shared by both hosts: a turn that re-opens a settled ruling is blocked.

2026-09-05 事故：owner 已裁「虛擬必須鏡像實盤」的事，被同一場 session 重新端成選項
問 owner（owner：「為什麼還是會發生這種錯誤？」）。SessionStart 注入裁定、
UserPromptSubmit 喚回裁定，兩者都只是「說給模型聽」；回合結束前沒有任何一道閘去比
對模型剛說出口的話。這支 adapter 就是那道閘：命中決策卡的 `forbidden` 就擋；把已裁定
的事再問一次 owner 也擋，並要求照裁定改寫。

Fail-open by construction: any exception, an expired deadline, or a missing config
lets the turn end. A hook that could hang or crash the turn would be worse than the
error it guards against.
"""

from collections import namedtuple
import codecs
import hashlib
import json
import os
from pathlib import Path
import re

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import memspec
from _hook_common import (
    append_gate_log,
    clear_recall_markers,
    compile_bounded_regex,
    compile_pattern_or_literal,
    declared_frontmatter,
    emit,
    expired,
    governance_vault,
    load_config,
    native_cwd_vaults,
    read_event,
    recall_marker_directory,
    run_synthetic,
    sequence_fields,
    with_session,
    write_config,
)

_TERMINATORS = re.escape(memspec.STOP_GATE_SENTENCE_TERMINATORS)
# One sentence with its terminator, or the trailing fragment that has none.
_SENTENCE_REGEX = re.compile(f"[^{_TERMINATORS}]*[{_TERMINATORS}]|[^{_TERMINATORS}]+$")
_WHITESPACE_REGEX = re.compile(r"\s+")
_FORBIDDEN_RULE = "forbidden"
_QUESTION_RULE = "question"
_DECISION_CACHE_FILENAME = "stop_decisions.json"
# The cached entry is discovery only — whether this card declares a ruling at all.
# Every authority the gate acts on is re-read from this turn's bytes below, so a
# ruling that gained fields (require_when/require_text/advice) needs no version bump.
_DECISION_CACHE_VERSION = 3
_KEY = "key"
_DECIDED_AT = "decided_at"
_QUOTE = "quote"

_REQUIRE_RULE = "require"
_REQUIRE_WHEN = "require_when"
_REQUIRE_TEXT = "require_text"
_ADVICE = "advice"

_Decision = namedtuple(
    "_Decision",
    "key decided_at quote forbidden aliases path decided_by require_when require_text advice",
)


def _one_line(value):
    return " ".join(str(value or "").split())


def _normalized(text):
    """Whitespace dropped and case folded, so an alias matches however it is spaced.

    別名去空白比對：`SESSION_RESUME` 與 `SESSION RESUME`、`Stop Hook` 與 `stop hook`
    是同一個名字，模型換個寫法就繞過閘門的話這道閘等於不存在。"""
    return _WHITESPACE_REGEX.sub("", str(text or "")).casefold()


def _decision_frontmatter(path):
    """Frontmatter of a card this gate rules on: a decision, or any card that arms.

    Arming must not depend on the card's type. card_lint tells the author of a
    behaviour card to add `forbidden` or `require_when`; if only decision-keyed cards
    were read, following that instruction would enforce nothing — the same silent
    disarming this gate exists to prevent, arriving through the lint's own advice
    (2026-09-16: six freshly armed behaviour cards were inert for exactly this
    reason)."""
    for field in (
        memspec.DECISION_KEY_FIELD,
        memspec.FORBIDDEN_FIELD,
        memspec.REQUIRE_WHEN_FIELD,
    ):
        lines = declared_frontmatter(
            path, field, memspec.STOP_GATE_FRONTMATTER_MAX_BYTES
        )
        if lines is not None:
            return lines
    return None


def _read_decision(path):
    """The active ruling one card carries, as plain JSON values, or None.

    Status is re-read from the card, never from the index: a stale index would pin a
    ruling the owner has already superseded, and this gate blocks on what it reads.

    U38: reads via memspec.frontmatter_fields/memspec.TOP_LEVEL_FIELD directly —
    the same primitives decision_lint._parse_frontmatter wraps — so this gate no
    longer pays decision_lint's argparse/dataclasses import even deferred."""
    front_lines = _decision_frontmatter(path)
    if front_lines is None:
        return None
    fields, _problem = memspec.frontmatter_text("---\n" + "\n".join(front_lines) + "\n---\n")
    key = _one_line(fields.get(memspec.DECISION_KEY_FIELD))
    status = _one_line(fields.get(memspec.DECISION_STATUS_FIELD))
    if key:
        # A ruling speaks only while it is the current one.
        if status != memspec.ACTIVE_DECISION_STATUS:
            return None
    else:
        # A behaviour card arms without carrying a ruling of its own; it is named by
        # its own name, and anything retired or superseded stops speaking.
        key = _one_line(fields.get(memspec.NAME_FIELD))
        if not key or status in memspec.STOP_GATE_SILENT_STATUSES:
            return None
    sequences = sequence_fields(front_lines, (memspec.ALIASES_FIELD, memspec.FORBIDDEN_FIELD))
    decided_by = _one_line(fields.get(memspec.DECIDED_BY_FIELD))
    quote = _one_line(fields.get(memspec.OWNER_QUOTE_FIELD))
    if not quote and decided_by != memspec.OWNER_EXPLICIT_DECIDER:
        quote = _one_line(fields.get(memspec.DESCRIPTION_FIELD))
    return {
        _KEY: key,
        memspec.DECIDED_BY_FIELD: decided_by,
        _DECIDED_AT: _one_line(fields.get(memspec.CURRENT_DECISION_AT_FIELD)),
        _QUOTE: quote[: memspec.STOP_GATE_QUOTE_MAX_CHARS],
        memspec.FORBIDDEN_FIELD: sequences[memspec.FORBIDDEN_FIELD],
        memspec.ALIASES_FIELD: sequences[memspec.ALIASES_FIELD],
        _REQUIRE_WHEN: _one_line(fields.get(memspec.REQUIRE_WHEN_FIELD)),
        _REQUIRE_TEXT: _one_line(fields.get(memspec.REQUIRE_TEXT_FIELD)),
        _ADVICE: _one_line(fields.get(memspec.DESCRIPTION_FIELD))[
            : memspec.STOP_GATE_QUOTE_MAX_CHARS
        ],
    }


def _read_cache(cache_path):
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}, ""
    if not isinstance(loaded, dict) or loaded.get("version") not in (2, _DECISION_CACHE_VERSION):
        return {}, {}, ""
    manifest = loaded.get("manifest")
    rulings = loaded.get("decisions")
    if not isinstance(manifest, dict) or not isinstance(rulings, dict):
        return {}, {}, ""
    cursor = loaded.get("cursor")
    return manifest, rulings, cursor if isinstance(cursor, str) else ""


def _write_cache(cache_path, manifest, rulings, cursor=""):
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        staging = cache_path.with_name(f".{cache_path.name}.tmp-{os.getpid()}")
        staging.write_text(
            json.dumps(
                {"version": _DECISION_CACHE_VERSION, "manifest": manifest, "decisions": rulings, "cursor": cursor},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(staging, cache_path)
    except OSError:
        pass


def _decisions(vault, started_at, defects=None):
    """Cache discovery only; every returned authority comes from this turn's bytes.

    Refresh at most one cap of discovery cards and one cap of known decisions.
    Negative entries rotate, because timestamps/size/identity cannot prove that a
    non-decision's contents never changed. Unread work retains its old signature.
    """
    from epitype import memsearch

    defects = [] if defects is None else defects
    vault = Path(vault).resolve()
    try:
        scan = memsearch.scan_cards(vault)
    except Exception:
        return []
    manifest, paths = {}, {}
    for card_path, path, mtime_ns, size in scan:
        if expired(started_at):
            return []
        try:
            info = path.stat()
        except OSError:
            continue
        manifest[card_path] = [mtime_ns, size, info.st_ctime_ns, info.st_dev, info.st_ino]
        paths[card_path] = path
    cache_path = vault / memspec.FTS_INDEX_DIRECTORY / _DECISION_CACHE_FILENAME
    old_manifest, cached, cursor = _read_cache(cache_path)
    old_cursor = cursor
    rulings = {key: value for key, value in cached.items() if key in paths}
    verified = {key: old_manifest.get(key) for key in rulings}
    changed = [key for key in paths if key not in cached or old_manifest.get(key) != manifest[key]]
    negatives = sorted(key for key in paths if key in cached and key not in changed
                       and not (isinstance(cached[key], dict) and cached[key].get(_KEY)))
    rotated = [key for key in negatives if key > cursor] + [key for key in negatives if key <= cursor]
    refreshed = set()
    cap = memspec.STOP_GATE_MAX_CARDS_PER_VAULT
    for card_path in (changed + rotated)[:cap]:
        if expired(started_at):
            break
        try:
            rulings[card_path] = _read_decision(paths[card_path])
        except Exception:
            rulings[card_path] = None
        refreshed.add(card_path)
        verified[card_path] = manifest[card_path]
        if card_path in negatives:
            cursor = card_path

    found = []
    candidates = [key for key in sorted(rulings) if isinstance(rulings[key], dict) and rulings[key].get(_KEY)]
    # 上限只管「找」，不管「擋」。貴的是掃整個庫去分辨哪些卡帶著裁定——那件事有快取，
    # 找過就不必再找；已經找出來的卡再讀一次 frontmatter 很便宜。以前連執行都砍到上限，
    # 於是卡片一多，排在後面的就默默不生效，而且要靠「先被放行過一次」才拿得到優先權，
    # 是個死結（2026-09-17 對抗審查實測：5000 張卡裡 500 張武裝，每回合只有 12 張生效）。
    # 排序仍然有用：命中多的先讀，超時的時候先保住最會攔到東西的那些。
    candidates.sort(key=_priority_key(vault, rulings))
    for index, card_path in enumerate(candidates):
        if expired(started_at):
            # 少檢查幾張卡一定要出聲。以前逾時就靜靜回傳已經找到的部分，於是「這回合
            # 沒擋」跟「這回合沒檢查完」長得一模一樣。
            defects.append(memspec.STOP_GATE_INCOMPLETE_DEFECT.format(
                checked=index, total=len(candidates), vault=vault.name))
            break
        if card_path not in refreshed:
            try:
                rulings[card_path] = _read_decision(paths[card_path])
            except Exception:
                rulings[card_path] = None
            verified[card_path] = manifest[card_path]
        ruling = rulings[card_path]
        if expired(started_at) or not isinstance(ruling, dict) or not ruling.get(_KEY):
            continue
        found.append(
            _Decision(
                _one_line(ruling.get(_KEY)),
                _one_line(ruling.get(_DECIDED_AT)),
                _one_line(ruling.get(_QUOTE))[: memspec.STOP_GATE_QUOTE_MAX_CHARS],
                tuple(_strings(ruling.get(memspec.FORBIDDEN_FIELD))),
                tuple(_strings(ruling.get(memspec.ALIASES_FIELD))),
                vault / card_path,
                _one_line(ruling.get(memspec.DECIDED_BY_FIELD)),
                _one_line(ruling.get(_REQUIRE_WHEN)),
                _one_line(ruling.get(_REQUIRE_TEXT)),
                _one_line(ruling.get(_ADVICE)),
            )
        )
    if verified != old_manifest or rulings != cached or cursor != old_cursor:
        _write_cache(cache_path, verified, rulings, cursor)
    return [] if expired(started_at) else found


def _priority_key(vault, rulings):
    """Order candidates by how often each ruling has actually fired lately.

    Fail-quiet on purpose: without the nightly health file every card scores the
    same and the order stays alphabetical, which is what it was before."""
    try:
        from epitype import compliance

        ranks = compliance.priority_rank(vault)
    except Exception:
        ranks = {}

    def key(card_path):
        ruling = rulings.get(card_path) or {}
        name = ruling.get(_KEY) if isinstance(ruling, dict) else None
        return ranks.get(name, (0, "")) + (card_path,)

    return key


def _strings(value):
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def _quoted_spans(message):
    """Character ranges of `message` that cite something rather than propose it:
    the owner's own quoting conventions (memspec.STOP_GATE_QUOTE_TEXT_REGEX) plus a
    Markdown blockquote line in full.

    U64: a Stop-gate report that cites a blocked phrase as evidence, or a
    write-gate script that defines it as a string literal, is not the model
    re-opening the ruling. Spans are merged so a hit is judged against one
    contiguous range rather than accidentally straddling two adjacent ones."""
    if not memspec.STOP_GATE_QUOTE_MASK_ENABLED:
        return []
    spans = [match.span() for match in memspec.STOP_GATE_QUOTE_TEXT_REGEX.finditer(message)]
    spans.extend(match.span() for match in memspec.STOP_GATE_BLOCKQUOTE_LINE_REGEX.finditer(message))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _exceptions(vault):
    """Fragments the nightly review found produce a block that changed nothing.

    Fail-quiet: with no health file every pattern enforces its full range, which is
    what it did before the review existed."""
    try:
        from epitype import compliance

        return compliance.exceptions_for(vault)
    except Exception:
        return {}


def _demoted(vault):
    """Cards the nightly rehearsal found fire too often across history to block.

    A rule that matches more than one turn in ten is not describing a mistake any
    more, it is describing how the model writes. Counting continues; blocking stops
    until the rate comes back down, so the demotion undoes itself."""
    try:
        from epitype import compliance

        return compliance.demoted_cards(vault)
    except Exception:
        return set()


def _forbidden_fragment(decision, message, defects, excepted=frozenset()):
    """The matched fragment of the first usable `forbidden` pattern that fires
    outside a quoted citation (U64: see _quoted_spans) — a hit fully inside a
    quoted span is a citation, not a restatement; one outside still blocks,
    including a second, unquoted occurrence of a pattern already cited once.

    A pattern the shared validator rejects is dropped and named on stderr, never
    silently: an unusable pattern is a ruling that stopped being enforced, and the
    turn still ends rather than being blocked by a card nobody can fix."""
    quoted = _quoted_spans(message)
    for pattern in decision.forbidden:
        # A pattern that will not compile used to be dropped, which left the card
        # looking armed and enforcing nothing. It now falls back to matching the text
        # literally — narrower than any working pattern, so it cannot over-block —
        # and says so, because a rule quietly behaving differently is the failure
        # this gate exists to remove.
        regex, repaired = compile_pattern_or_literal(pattern)
        if regex is None or repaired:
            defects.append(
                memspec.STOP_GATE_PATTERN_DEFECT.format(
                    decision=decision.key,
                    pattern=_one_line(pattern)[: memspec.STOP_GATE_FRAGMENT_MAX_CHARS],
                    reason="改用逐字比對" if repaired else "無法使用，這一條沒有生效",
                )
            )
        if regex is None:
            continue
        for found in regex.finditer(message):
            if any(start <= found.start() and found.end() <= end for start, end in quoted):
                continue
            matched = _one_line(found.group(0))
            if matched in excepted:
                # 夜間回饋認定這一串字擋了等於沒擋（擋完我照原樣重送），所以它自動放行。
                # 放行的是這一串字，不是整條樣式，而且會過期。
                continue
            return matched or _one_line(pattern)
    return None


def _requirement_gap(decision, message, defects):
    """The trigger fragment when this turn owes the card's required text and lacks it.

    Whole classes of rule are "do this first", and whether I did it is invisible from
    outside. The conversion is to require that doing it leaves a mark in the message:
    the rule stops asking for the unobservable act and asks for the sentence that
    reports it. Omission then becomes checkable, and a fabricated mark is no longer a
    skipped step but a false statement, which the honesty floor already governs.

    A pattern the shared validator rejects is dropped and named on stderr: a
    requirement nobody can fix must not block every turn forever."""
    if not decision.require_when or not decision.require_text:
        return None
    patterns = {}
    for field, pattern in (
        (memspec.REQUIRE_WHEN_FIELD, decision.require_when),
        (memspec.REQUIRE_TEXT_FIELD, decision.require_text),
    ):
        try:
            patterns[field] = compile_bounded_regex(pattern)
        except Exception as exc:
            defects.append(
                memspec.STOP_GATE_PATTERN_DEFECT.format(
                    decision=decision.key,
                    pattern=_one_line(pattern)[: memspec.STOP_GATE_FRAGMENT_MAX_CHARS],
                    reason=f"{field}: {type(exc).__name__}: {exc}",
                )
            )
            return None
    quoted = _quoted_spans(message)

    def outside_quotes(regex):
        for found in regex.finditer(message):
            if any(start <= found.start() and found.end() <= end for start, end in quoted):
                continue
            return found
        return None

    triggered = outside_quotes(patterns[memspec.REQUIRE_WHEN_FIELD])
    if triggered is None:
        return None
    if outside_quotes(patterns[memspec.REQUIRE_TEXT_FIELD]) is not None:
        return None
    return _one_line(triggered.group(0))[: memspec.STOP_GATE_FRAGMENT_MAX_CHARS]


def _is_question(sentence):
    text = sentence.strip()
    return text.endswith(memspec.STOP_GATE_QUESTION_ENDINGS) or any(
        marker in text for marker in memspec.STOP_GATE_QUESTION_MARKERS
    )


def _asks_again(decision, message):
    """True when one question sentence names this same ruling by two of its aliases.

    Two aliases of one card, not one: a single shared word ("虛擬") appears in
    unrelated questions, while「虛擬盤」and「鏡像實盤」together in a question is the
    settled subject being put back to the owner. Aliases belonging to different cards
    never add up."""
    # Only a sourced, explicit owner ruling can make a question "already decided".
    if decision.decided_by != memspec.OWNER_EXPLICIT_DECIDER or not decision.quote:
        return False
    aliases = tuple(
        alias
        for alias in dict.fromkeys(_normalized(item) for item in decision.aliases)
        if len(alias) >= memspec.STOP_GATE_MIN_ALIAS_CHARS
    )
    if len(aliases) < memspec.STOP_GATE_ALIAS_HITS:
        return False
    for match in _SENTENCE_REGEX.finditer(message):
        sentence = match.group(0)
        if not sentence.strip() or not _is_question(sentence):
            continue
        haystack = _normalized(sentence)
        if sum(1 for alias in aliases if alias in haystack) >= memspec.STOP_GATE_ALIAS_HITS:
            return True
    return False


def _named(decision):
    if decision.decided_at:
        return f"{decision.key}，{decision.decided_at}"
    return decision.key


def _message_digest(message):
    """Short digest of a turn's text — the same value the nightly replay computes."""
    return hashlib.sha256(str(message).encode("utf-8", errors="replace")).hexdigest()[:16]


def _claim_marker(session_id, decision_key, message):
    """One block per (session, decision, message); the marker lives in the recall
    marker directory so PreCompact's clear_recall_markers drops it with the rest.

    A marker that cannot be written must not silence the ruling, so a filesystem
    error claims the block rather than swallowing it."""
    if not session_id:
        return True
    digest = hashlib.sha256(
        (str(decision_key) + "\0" + str(message)).encode("utf-8", errors="replace")
    ).hexdigest()
    try:
        directory = recall_marker_directory(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / (memspec.STOP_GATE_MARKER_PREFIX + digest)
        with marker.open("x", encoding="ascii") as stream:
            stream.write(digest + "\n")
    except FileExistsError:
        return False
    except OSError:
        return True
    return True


def _audit(config, decision_key, rule, started_at, session_id=None, digest=""):
    """The blocked message's digest goes on the row, never the message.

    The nightly replay has to tell "this ruling let something through" from "the turn
    was stopped by a different ruling and rewritten" — the gate reports one verdict
    per turn and stops, so without the digest every second violation in a blocked
    message reads as a miss."""
    try:
        row = {
            "kind": memspec.STOP_GATE_LOG_KIND,
            "decision": decision_key,
            "rule": rule,
            "digest": digest,
        }
        append_gate_log(
            governance_vault(config, for_write=True),
            with_session(row, session_id),
            started_at,
        )
    except Exception:
        pass


def _vaults(config, event):
    """The cwd's own vault(s) first, then the governance vault holding the ledger."""
    vaults = list(native_cwd_vaults(event.get("cwd") if isinstance(event, dict) else None))
    governance = governance_vault(config)
    if governance not in vaults:
        vaults.append(governance)
    return vaults


def _handle(event, started_at, defects):
    # The host re-runs Stop after a block; blocking that run again would loop forever.
    if event.get("stop_hook_active"):
        return None
    message = event.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        return None
    message = message[: memspec.STOP_GATE_MESSAGE_MAX_CHARS]
    config = load_config(started_at)
    if config is None:
        return None
    return _verdict(event, message, config, started_at, defects)


def _verdict(event, message, config, started_at, defects):
    decisions = []
    for vault in _vaults(config, event):
        if expired(started_at):
            return None
        decisions.extend(_decisions(vault, started_at, defects))
    if not decisions:
        return None

    # A forbidden hit outranks a repeated question: the model already said the thing
    # the owner ruled out, which is the harder violation of the two.
    verdicts = []
    excepted = {}
    demoted = set()
    for vault in _vaults(config, event):
        for card, fragments in _exceptions(vault).items():
            excepted.setdefault(card, set()).update(fragments)
        demoted |= _demoted(vault)
    decisions = [decision for decision in decisions if decision.key not in demoted]
    for decision in decisions:
        fragment = _forbidden_fragment(
            decision, message, defects, excepted.get(decision.key, frozenset())
        )
        if fragment is not None:
            verdicts.append(
                (
                    decision,
                    _FORBIDDEN_RULE,
                    memspec.STOP_GATE_FORBIDDEN_REASON.format(
                        decision=_named(decision),
                        quote=decision.quote,
                        fragment=fragment[: memspec.STOP_GATE_FRAGMENT_MAX_CHARS],
                    ),
                )
            )
            break
    if not verdicts:
        for decision in decisions:
            trigger = _requirement_gap(decision, message, defects)
            if trigger is None:
                continue
            verdicts.append(
                (
                    decision,
                    _REQUIRE_RULE,
                    memspec.STOP_GATE_REQUIRE_REASON.format(
                        decision=_named(decision),
                        trigger=trigger,
                        expected=f"「{decision.require_text}」所指的內容",
                        advice=decision.advice,
                    ),
                )
            )
            break
    if not verdicts:
        for decision in decisions:
            if not _asks_again(decision, message):
                continue
            template = (
                memspec.STOP_GATE_QUESTION_REASON
                if decision.decided_at
                else memspec.STOP_GATE_QUESTION_REASON_UNDATED
            )
            verdicts.append(
                (
                    decision,
                    _QUESTION_RULE,
                    template.format(decided_at=decision.decided_at, quote=decision.quote),
                )
            )
            break
    if not verdicts:
        return None

    decision, rule, reason = verdicts[0]
    session_id = event.get("session_id", event.get("sessionId"))
    if not _claim_marker(session_id, decision.key, message):
        return None
    _audit(config, decision.key, rule, started_at, session_id, _message_digest(message))
    return {"decision": "block", "reason": reason}


def _selftest():
    import tempfile
    import uuid

    checks = []
    sessions = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-stopgate-") as temp_dir:
            root = Path(temp_dir).resolve()
            vault = root / "vault"
            vault.mkdir()
            (vault / "mirror.md").write_text(
                "---\n"
                "name: 虛擬盤鏡像裁定\n"
                "description: 虛擬盤與實盤參數一致\n"
                f"{memspec.DECISION_KEY_FIELD}: virtual-mirrors-live\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-08-13\n"
                f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 虛擬必須鏡像實盤\n"
                f"{memspec.ALIASES_FIELD}:\n  - 虛擬盤\n  - 鏡像實盤\n  - virtual mirror\n"
                f"{memspec.FORBIDDEN_FIELD}:\n  - 虛擬盤(?:先)?用不同參數\n  - 兩套參數\n"
                "---\nbody\n",
                encoding="utf-8",
            )
            (vault / "schedule.md").write_text(
                "---\n"
                "name: 排程裁定\n"
                "description: 排程由 owner 決定\n"
                f"{memspec.DECISION_KEY_FIELD}: schedule-owner-only\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-08-20\n"
                f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 排程只由我改\n"
                f"{memspec.ALIASES_FIELD}: [排程甲, 排程乙]\n"
                "---\nbody\n",
                encoding="utf-8",
            )
            (vault / "retired.md").write_text(
                "---\n"
                "name: 舊制\n"
                "description: 已作廢\n"
                f"{memspec.DECISION_KEY_FIELD}: retired-rule\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.SUPERSEDED_DECISION_STATUS}\n"
                f"{memspec.SUPERSEDED_BY_FIELD}: mirror.md\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-01-01\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 舊制原話\n"
                f"{memspec.ALIASES_FIELD}: [退休甲, 退休乙]\n"
                f"{memspec.FORBIDDEN_FIELD}: [退休禁詞]\n"
                "---\nbody\n",
                encoding="utf-8",
            )
            config = root / "config.json"
            write_config(config, [vault])

            def run(message, session=None, extra=None):
                session_id = session or f"stopgate-{uuid.uuid4().hex}"
                if session_id not in sessions:
                    sessions.append(session_id)
                event = {
                    "session_id": session_id,
                    "hook_event_name": "Stop",
                    "stop_hook_active": False,
                    "last_assistant_message": message,
                }
                event.update(extra or {})
                # An event carrying cwd makes every ancestor look for a same-slug
                # native vault under home; without this the run would pull the
                # test machine's real vaults into a synthetic case.
                result = run_synthetic(
                    Path(__file__),
                    event,
                    config,
                    environment={"HOME": os.fspath(root), "USERPROFILE": os.fspath(root)},
                )
                value = json.loads(result.stdout) if result.stdout.strip() else {}
                return result, value, session_id

            forbidden_result, forbidden_value, forbidden_session = run(
                "我建議虛擬盤先用不同參數跑一週再說。"
            )
            log_rows = [
                json.loads(line)
                for line in (vault / memspec.GATE_LOG_FILENAME).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            checks.append((
                "a forbidden phrase is blocked and audited as stop_block",
                forbidden_result.returncode == 0
                and forbidden_value.get("decision") == "block"
                and "虛擬必須鏡像實盤" in forbidden_value.get("reason", "")
                and "virtual-mirrors-live，2026-08-13" in forbidden_value.get("reason", "")
                and "虛擬盤先用不同參數" in forbidden_value.get("reason", "")
                and any(
                    row.get("kind") == memspec.STOP_GATE_LOG_KIND
                    and row.get("decision") == "virtual-mirrors-live"
                    and row.get("rule") == _FORBIDDEN_RULE
                    for row in log_rows
                ),
            ))
            checks.append((
                "the stop_block audit row carries the event's session_id",
                any(
                    row.get("kind") == memspec.STOP_GATE_LOG_KIND
                    and row.get("decision") == "virtual-mirrors-live"
                    and row.get("session_id") == forbidden_session
                    for row in log_rows
                ),
            ))
            checks.append((
                "the blocking payload carries decision and reason and nothing else",
                set(forbidden_value) == {"decision", "reason"}
                and len(forbidden_result.stdout.encode("utf-8")) <= memspec.HOOK_MAX_OUTPUT_BYTES
                and not forbidden_result.stderr,
            ))

            # U64: citing a forbidden phrase as evidence is not re-proposing it.
            quoted_result, quoted_value, _ = run(
                "實測表：「虛擬盤先用不同參數」是被擋下的例子，僅供舉例說明。"
            )
            checks.append((
                "forbidden 落在中文引號「」內是引用，不算再提議",
                quoted_result.returncode == 0
                and not quoted_result.stdout.strip()
                and not quoted_value,
            ))

            backtick_result, backtick_value, _ = run(
                "程式碼片段：`兩套參數` 只是這裡的變數命名範例，不是提議。"
            )
            checks.append((
                "forbidden 落在反引號內是引用，不算再提議",
                backtick_result.returncode == 0
                and not backtick_result.stdout.strip()
                and not backtick_value,
            ))

            blockquote_result, blockquote_value, _ = run(
                "引用回顧：\n> 虛擬盤先用不同參數跑一週再說\n以上是先前被擋下的話，現在已經照裁定改了。"
            )
            checks.append((
                "forbidden 整行落在 > 引用行內是引用，不算再提議",
                blockquote_result.returncode == 0
                and not blockquote_result.stdout.strip()
                and not blockquote_value,
            ))

            table_result, table_value, _ = run(
                "| 案例 | 結果 |\n| --- | --- |\n| 「虛擬盤先用不同參數」 | 擋下 |\n"
            )
            checks.append((
                "forbidden 落在表格儲存格的引號內是引用，不算再提議",
                table_result.returncode == 0
                and not table_result.stdout.strip()
                and not table_value,
            ))

            mixed_result, mixed_value, _ = run(
                "owner 說過「虛擬盤先用不同參數」不行，但我還是想虛擬盤先用不同參數看看。"
            )
            checks.append((
                "同訊息一次加引號、一次沒加：沒加引號那次仍照擋",
                mixed_result.returncode == 0
                and mixed_value.get("decision") == "block"
                and "虛擬必須鏡像實盤" in mixed_value.get("reason", "")
                and "虛擬盤先用不同參數" in mixed_value.get("reason", ""),
            ))

            question_result, question_value, _ = run(
                "先講結論。虛擬盤要不要改成鏡像實盤，還是維持現狀？"
            )
            checks.append((
                "a question naming the ruling by two of its aliases is blocked",
                question_result.returncode == 0
                and question_value.get("decision") == "block"
                and question_value.get("reason", "").startswith("此事 owner 已於 2026-08-13 裁定：")
                and "不得再問" in question_value.get("reason", ""),
            ))

            statement_result, statement_value, _ = run(
                "已依裁定把虛擬盤設成鏡像實盤，參數表已同步。"
            )
            checks.append((
                "a statement that merely names the aliases is left alone",
                statement_result.returncode == 0
                and not statement_result.stdout.strip()
                and not statement_value,
            ))

            split_result, split_value, _ = run(
                "虛擬盤跟排程甲要不要一起處理？"
            )
            two_alias_result, two_alias_value, _ = run(
                "排程甲跟排程乙要不要一起處理？"
            )
            checks.append((
                "one alias from each of two active cards in one question is not a hit,"
                " while two aliases of one card are",
                split_result.returncode == 0
                and not split_value
                and two_alias_value.get("decision") == "block"
                and "排程只由我改" in two_alias_value.get("reason", ""),
            ))

            active_event = {
                "session_id": "stopgate-active",
                "hook_event_name": "Stop",
                "stop_hook_active": True,
                "last_assistant_message": "我建議虛擬盤先用不同參數跑一週再說。",
            }
            active_result = run_synthetic(Path(__file__), active_event, config)
            checks.append((
                "stop_hook_active is never blocked again, so the host cannot loop",
                active_result.returncode == 0
                and not active_result.stdout.strip()
                and not active_result.stderr,
            ))

            retired_result, retired_value, _ = run(
                "這裡用退休禁詞，另外退休甲跟退休乙要不要恢復？"
            )
            checks.append((
                "a superseded card neither forbids nor answers for the owner",
                retired_result.returncode == 0 and not retired_value,
            ))

            repeat_message = "我建議虛擬盤先用不同參數跑一週再說。"
            _first, first_value, repeat_session = run(repeat_message)
            _second, second_value, _ = run(repeat_message, session=repeat_session)
            markers = sorted(
                path.name
                for path in recall_marker_directory(repeat_session).iterdir()
                if path.is_file()
            )
            checks.append((
                "the same message is blocked once per session, from the recall marker directory",
                first_value.get("decision") == "block"
                and not second_value
                and len(markers) == 1
                and markers[0].startswith(memspec.STOP_GATE_MARKER_PREFIX),
            ))
            clear_recall_markers(repeat_session)
            checks.append((
                "PreCompact's marker sweep covers the stop marker",
                not recall_marker_directory(repeat_session).exists(),
            ))

            codex_result, codex_value, _ = run(
                "我建議虛擬盤先用不同參數跑一週再說。",
                extra={
                    "transcript_path": os.fspath(root / "synthetic.jsonl"),
                    "cwd": os.fspath(root),
                },
            )
            checks.append((
                "the Codex-shaped event blocks through the same adapter",
                codex_result.returncode == 0
                and set(codex_value) == {"decision", "reason"}
                and codex_value.get("decision") == "block",
            ))

            # A cache that outlived the card it summarised would silently stop
            # enforcing a ruling the owner just tightened.
            cache_path = vault / memspec.FTS_INDEX_DIRECTORY / _DECISION_CACHE_FILENAME
            cached_before = json.loads(cache_path.read_text(encoding="utf-8"))
            mirror = vault / "mirror.md"
            mirror.write_text(
                mirror.read_text(encoding="utf-8").replace("  - 兩套參數\n", "  - 兩套參數\n  - 分開調參\n"),
                encoding="utf-8",
            )
            edited_result, edited_value, _ = run("我打算分開調參，兩邊各自最佳化。")
            checks.append((
                "the manifest cache is written and a rewritten card is re-read",
                cached_before.get("version") == _DECISION_CACHE_VERSION
                and len(cached_before.get("manifest") or ()) >= 3
                and edited_result.returncode == 0
                and edited_value.get("decision") == "block"
                and "分開調參" in edited_value.get("reason", ""),
            ))

            plain_vault = root / "plain-vault"
            plain_vault.mkdir()
            (plain_vault / "note.md").write_text(
                "---\nname: note\ndescription: 2026-09-01 沒有裁定的卡\n---\nbody\n",
                encoding="utf-8",
            )
            plain_config = root / "plain-config.json"
            write_config(plain_config, [plain_vault])
            plain_result = run_synthetic(
                Path(__file__),
                {
                    "session_id": "stopgate-plain",
                    "stop_hook_active": False,
                    "last_assistant_message": "虛擬盤要不要改成鏡像實盤？",
                },
                plain_config,
            )
            checks.append((
                "a vault with no decision card emits nothing and exits 0",
                plain_result.returncode == 0
                and not plain_result.stdout
                and not plain_result.stderr,
            ))

            bad_config = root / "bad-config.json"
            bad_config.write_text("{broken", encoding="utf-8")
            bad_result = run_synthetic(
                Path(__file__),
                {
                    "session_id": "stopgate-bad",
                    "stop_hook_active": False,
                    "last_assistant_message": "我建議虛擬盤先用不同參數跑一週再說。",
                },
                bad_config,
            )
            checks.append((
                "bad config fails open silently",
                bad_result.returncode == 0
                and not bad_result.stdout
                and not bad_result.stderr,
            ))

            evil_vault = root / "evil-vault"
            evil_vault.mkdir()
            (evil_vault / "evil.md").write_text(
                "---\n"
                "name: 指數正則\n"
                "description: 2026-09-06 壞正則\n"
                f"{memspec.DECISION_KEY_FIELD}: evil-regex\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-09-06\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 壞正則不得生效\n"
                f"{memspec.ALIASES_FIELD}: [唯一別名]\n"
                f"{memspec.FORBIDDEN_FIELD}: ['(a+)+b']\n"
                "---\nbody\n",
                encoding="utf-8",
            )
            evil_config = root / "evil-config.json"
            write_config(evil_config, [evil_vault])
            evil_result = run_synthetic(
                Path(__file__),
                {
                    "session_id": "stopgate-evil",
                    "stop_hook_active": False,
                    "last_assistant_message": "aaaaaaaaaaaaaaaaaaaaaaaac 這行不該被擋。",
                },
                evil_config,
            )
            checks.append((
                "an exponential forbidden pattern is dropped, named on stderr, and blocks nothing",
                evil_result.returncode == 0
                and not evil_result.stdout.strip()
                and "evil-regex" in evil_result.stderr
                and memspec.STOP_GATE_PATTERN_DEFECT.split("{", 1)[0] in evil_result.stderr,
            ))

            # Owner 2026-09-09 (§30): Stop keeps the decision gate and writes no
            # commitment ledger. A turn whose tail is a promise still blocks on the
            # forbidden phrase, and leaves nothing behind in the vault.
            ledger = vault / memspec.FTS_INDEX_DIRECTORY / "commitments.jsonl"
            before = ledger.stat().st_mtime_ns if ledger.exists() else None
            promise = "我等一下會把參數表補上。"
            blocked_result, blocked_value, _ = run(
                "我建議虛擬盤先用不同參數跑一週再說。" + promise
            )
            after = ledger.stat().st_mtime_ns if ledger.exists() else None
            checks.append((
                "a promise in the turn's tail still blocks and writes no ledger",
                blocked_result.returncode == 0
                and blocked_value.get("decision") == "block"
                and before == after,
            ))

            blown = time.monotonic() - memspec.HOOK_TIMEOUT_SECONDS - 1
            checks.append((
                "期限在 marker 寫下之後才到：block 照樣送出，不會只留帳不擋",
                _emits({"decision": "block", "reason": "x"}, blown)
                and not _emits(None, time.monotonic())
                and not _emits({"decision": "approve"}, blown),
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        for session_id in sessions:
            clear_recall_markers(session_id)

    passed = sum(bool(ok) for _, ok in checks)
    total = 22
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _emits(value, started_at):
    """A block reaches the host even past the deadline.

    `_verdict` claims the same-session dedupe marker and appends the audit row
    before it returns, so a deadline crossed in between used to drop the block
    while leaving the ruling on the ledger and unable to fire again this session
    (2026-09-06 review). The action gate's `main` has the same shape."""
    return value is not None and (
        value.get("decision") == "block" or not expired(started_at)
    )


def main():
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    defects = []
    try:
        event = read_event(sys.stdin)
        value = _handle(event, _STARTED_AT, defects)
        for line in defects[: memspec.GATE_DEFECT_MAX_LINES]:
            print(line, file=sys.stderr)
        if _emits(value, _STARTED_AT):
            emit(value)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
