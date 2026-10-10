import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""PreToolUse advisory checks, deterministic input repairs, and retained safety.

Write format, attribution uncertainty, resource cleanup and reading volume only
advise. Explicit card on_hit values and native settings remain distinct from
those checks; known other actors' tabs and retained VM tabs are preserved.
"""

from collections import namedtuple
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
import subprocess

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import context_meter
from epitype import control_lifecycle
from epitype import memspec
from epitype import patch_envelope
from epitype import rule_provenance
from _hook_common import (
    isolated_temp_root,
    GATE_LOG_MAX_BYTES,
    append_context,
    append_gate_log,
    claim_meter,
    compile_bounded_regex,
    config_path,
    declared_frontmatter,
    emit,
    encode_payload,
    expired,
    governance_vault,
    load_config,
    notice_marker_directory,
    payload_fits,
    read_event,
    remaining_seconds,
    attribution_seconds,
    recall_marker_directory,
    release_meter,
    scoped_vaults as resolve_vaults,
    run_concurrent,
    run_synthetic,
    sequence_fields,
    trace_hook,
    with_session,
    write_codex_fixture,
    write_config,
)


def _best_effort_audit(callback, *arguments):
    try:
        callback(*arguments)
    except Exception:
        pass


def _quoted(text):
    """引號跟著顯示語言走：中文用「」，英文用 ASCII 雙引號。"""
    return memspec.QUOTE_OPEN + str(text) + memspec.QUOTE_CLOSE


def _deny_value(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _sweep_due(root, now):
    """這一輪該不該掃。掃過就在根目錄留一個時間戳，還沒到間隔就直接跳過。

    2026-09-19 實測：每一次工具呼叫都掃一遍舊標記，光是 stat 就 2,945 次、54 ms——
    而標記只是同一場的去重，掃晚一點沒有任何壞處。"""
    stamp = root / memspec.NOTICE_SWEEP_STAMP
    try:
        if now - stamp.stat().st_mtime < memspec.NOTICE_SWEEP_INTERVAL_SECONDS:
            return False
    except OSError:
        pass
    try:
        root.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        return False
    return True


def _sweep_notice_markers(root, now, keep=None):
    """Markers are a same-session dedupe, not a record: drop the aged-out ones."""
    if not _sweep_due(root, now):
        return
    try:
        for session_directory in root.iterdir():
            if not session_directory.is_dir() or session_directory == keep:
                continue
            empty = True
            for marker in session_directory.iterdir():
                try:
                    if now - marker.stat().st_mtime > memspec.NOTICE_MARKER_TTL_SECONDS:
                        marker.unlink()
                    else:
                        empty = False
                except OSError:
                    empty = False
            if empty:
                session_directory.rmdir()
    except OSError:
        pass


def _notice_marker(session_id, text):
    """True the first time this session sees this notice; False afterwards."""
    # 用到才載入：hashlib 要 6 ms，絕大多數呼叫走不到這裡。
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    directory = notice_marker_directory(session_id)
    root = directory.parent
    try:
        _sweep_notice_markers(root, time.time(), keep=directory)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / digest).open("x", encoding="ascii") as stream:
            stream.write(digest + "\n")
    except FileExistsError:
        return False
    except OSError:
        return True
    return True


def _resolve_target(raw, cwd):
    """One declared path as an absolute path, or None when it cannot be resolved."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        path = Path(raw.strip())
        if not path.is_absolute() and isinstance(cwd, str) and cwd.strip():
            path = Path(cwd) / path
        return path.resolve()
    except (OSError, TypeError, ValueError):
        return None


def _write_target(tool_input, cwd):
    """Absolute path this call is about to write, or None when it names none."""
    for field in memspec.WRITE_GATE_PATH_FIELDS:
        raw = tool_input.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        return _resolve_target(raw, cwd)
    return None


def _edit_items(tool_name, tool_input):
    """(old, new, replace_all) triples this call would apply, in order."""
    if tool_name.casefold() in memspec.WRITE_GATE_MULTI_EDIT_TOOLS:
        raw_edits = tool_input.get(memspec.WRITE_GATE_EDITS_FIELD)
        edits = raw_edits if isinstance(raw_edits, list) else []
    else:
        edits = [tool_input]
    return [
        (
            edit.get(memspec.WRITE_GATE_OLD_FIELD),
            edit.get(memspec.WRITE_GATE_NEW_FIELD),
            bool(edit.get(memspec.WRITE_GATE_REPLACE_ALL_FIELD)),
        )
        for edit in edits
        if isinstance(edit, dict)
    ]


def _prospective_write(tool_name, tool_input, target):
    """(the new text this call adds, the full text the file would then hold).

    The second element is None whenever the result cannot be known exactly — an
    oversized or unreadable file, an `old_string` that is not in the current text.
    A gate that guessed the post-write text would judge a card nobody wrote; the
    new text alone is still checked against the settled rulings."""
    if tool_name.casefold() in memspec.WRITE_GATE_CONTENT_TOOLS:
        content = tool_input.get(memspec.WRITE_GATE_CONTENT_FIELD)
        if not isinstance(content, str):
            return [], None
        return [content], content

    items = _edit_items(tool_name, tool_input)
    additions = [new for _old, new, _all in items if isinstance(new, str) and new]
    if not items or any(
        not isinstance(old, str) or not isinstance(new, str) for old, new, _all in items
    ):
        return additions, None
    try:
        if target.stat().st_size > memspec.WRITE_GATE_MAX_CONTENT_BYTES:
            return additions, None
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return additions, None
    for old, new, replace_all in items:
        if not old or old not in text:
            return additions, None
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return additions, text


# 這幾個欄位裡出現禁語是它們的本分，不是再說一次那句話：`forbidden` 是規則本身，
# `example_blocks` 是「這句一定要擋」的證明——兩者都必須寫得出那句被禁的話。
_EXEMPT_CARD_FIELDS = (memspec.FORBIDDEN_FIELD, memspec.EXAMPLE_BLOCKS_FIELD)


def _audit_literal_only(fragment, text):
    """Recognize quoted old/source data without exempting a proposed instruction.

    Parse literals only, never execute the artifact. All occurrences must belong
    to explicit old/original quotation fields; a neighbouring new instruction
    still gets checked. The eventual card write retains its own source check.
    """
    import ast
    if not fragment or not isinstance(text, str):
        return False
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return False
    source = text.encode('utf-8')
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    ranges = []
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def data_assignment(node):
        # A quoted dict can be data inside a list/dict assigned at module level.
        # Calls, indexing, defaults and return values consume the literal rather
        # than declaring an audit record and retain the ordinary checks.
        parent = parents.get(node)
        while isinstance(parent, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
            node, parent = parent, parents.get(parent)
        if isinstance(parent, ast.Expr) and parent.value is node:
            return (isinstance(parents.get(parent), ast.Module)
                    and all(isinstance(item, ast.Expr) for item in tree.body))
        if not isinstance(parent, (ast.Assign, ast.AnnAssign)) or parent.value is not node:
            return False
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        if not all(isinstance(target, ast.Name) for target in targets):
            return False
        names = {target.id for target in targets}
        if any(isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
               and item.id in names for item in ast.walk(tree)):
            return False
        return isinstance(parents.get(parent), ast.Module)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict) or not data_assignment(node):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value in {
                    'old_literal', 'oldliteral', 'exact_quote', 'original_quote', 'owner_quote'}):
                continue
            # A function call used as a dictionary value still executes. Only
            # a direct string literal is quotation data, never an expression.
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                ranges.append((offsets[value.lineno - 1] + value.col_offset,
                               offsets[value.end_lineno - 1] + value.end_col_offset))
    needle = fragment.encode('utf-8')
    position = source.find(needle)
    found = False
    while position >= 0:
        found = True
        if not any(start <= position and position + len(needle) <= end for start, end in ranges):
            return False
        position = source.find(needle, position + len(needle))
    return found


def _forbidden_rule_edit(decision, target, fragment, texts):
    """True when this write is the ruling itself being edited, not a re-statement.

    2026-09-06 incident: editing the very card that defines a `forbidden` pattern
    was blocked by that pattern, and only got through because the same content is
    allowed on its second attempt. Two exemptions: the target IS the card that
    carries this ruling, or the matched fragment sits inside the frontmatter's own
    `forbidden:` block. Identity is the card's path, never a `decision_key` the
    content declares — a file anywhere could claim any key and walk past the rule.
    Changing the rule is always allowed; re-stating it anywhere else is not."""
    if decision.path is not None:
        try:
            if target.resolve() == Path(decision.path).resolve():
                return True
        except OSError:
            pass
    # 測試檔裡的禁語是樣本，不是主張：一條規則的回歸測試必須寫得出那句被禁的話，否則
    # 這道閘擋掉的第一個東西就是「證明它有效」的那份測試。2026-09-19 真的發生兩次。
    try:
        if any(part.casefold() in memspec.WRITE_GATE_FIXTURE_DIRECTORIES
               for part in target.parts):
            return True
    except (AttributeError, OSError):
        pass
    # 例句的宣告不限卡片格式：替一批卡補例句的那支腳本，資料也是一樣的句子。認的是
    # 「這一行附近宣告了 example_blocks」，而不是檔案長什麼樣。
    # 宣告了例句清單的內容，整份都算樣本——卡片是這樣，替一批卡補例句的那支腳本也是，
    # 而它的資料離宣告很遠。範圍跟「測試目錄整個豁免」同一種取捨：它確實是個洞，但要
    # 鑽這個洞得先在檔案裡寫下 example_blocks 的宣告，而那是看得見、查得到的。
    for text_value in texts:
        if not isinstance(text_value, str) or not fragment:
            continue
        if _audit_literal_only(fragment, text_value):
            return True
        if any(token in text_value for token in memspec.WRITE_GATE_EXAMPLE_MARKERS):
            return True

    for text_value in texts:
        if not isinstance(text_value, str) or not text_value:
            continue
        front_lines, closing = memspec.split_frontmatter(text_value)
        if front_lines is None or closing is None:
            continue
        block = []
        parent = None
        for raw_line in front_lines:
            indented = raw_line[:1].isspace()
            match = None if indented else memspec.TOP_LEVEL_FIELD.match(raw_line)
            if match is not None:
                key, raw_value = match.groups()
                value = memspec.strip_inline_comment(raw_value).strip()
                parent = key
                if key in _EXEMPT_CARD_FIELDS:
                    block.append(value)
                continue
            if indented and parent in _EXEMPT_CARD_FIELDS:
                block.append(raw_line)
        if fragment and any(fragment in line for line in block):
            return True
    return False


def _forbidden_write(event, config, target, additions, prospective, started_at, notices):
    """(vault, decision key, reason) for the first settled ruling this text violates.

    The decision cards, their `forbidden` patterns, and the pattern validator are
    the Stop gate's own: a ruling the model may not restate at the end of a turn is
    the same ruling it may not write into a file, and two readings of one card would
    drift. An unusable pattern is named to the model, never silently dropped."""
    import stop_gate

    for vault in stop_gate._vaults(config, event):
        if expired(started_at):
            return None
        for decision in stop_gate._decisions(vault, started_at, notices, event=event):
            # 管「說出口的話」的裁定不套在寫檔上：白話規則擋的是對 owner 丟機器名稱，
            # 而同一個字寫進程式碼註解或英文提交訊息是正當的。
            if decision.applies_to == memspec.APPLIES_TO_SPEECH:
                continue
            for index, text in enumerate(additions):
                # Defects are collected from the first text only; the same broken
                # pattern repeated once per edit would say nothing new.
                fragment = stop_gate._forbidden_fragment(
                    decision, text, notices if index == 0 else []
                )
                if fragment is None:
                    continue
                if _forbidden_rule_edit(decision, target, fragment, (prospective, text)):
                    continue
                if decision.on_hit == memspec.ON_HIT_NOTE:
                    notices.append(memspec.STOP_NOTE_POLICY.format(
                        advice=decision.advice or decision.quote, decision=decision.key))
                    continue
                return (
                    vault,
                    decision.key,
                    memspec.WRITE_GATE_FORBIDDEN_REASON.format(
                        decision=stop_gate._named(decision),
                        quote=decision.advice or decision.quote,
                        fragment=fragment[: memspec.WRITE_GATE_FRAGMENT_MAX_CHARS],
                    ),
                )
    return None


def _vault_card_path(target, vaults):
    """(vault, vault-relative posix path) when the target is a card of a registered
    vault, by memsearch's own card filter: a '_'/'.' prefixed part, a non-.md name
    and the memory index are not cards, so writing them carries no card contract."""
    for vault in vaults:
        try:
            relative = target.relative_to(vault)
        except ValueError:
            continue
        parts = relative.parts
        if not parts or any(part.startswith(("_", ".")) for part in parts):
            return None
        if not parts[-1].lower().endswith(".md") or parts[-1] == memspec.MEMORY_INDEX_FILENAME:
            return None
        return vault, relative.as_posix()
    return None


def _card_review(relative, text, *, before_text=None, source_path=None):
    """(deny reason, advice line) for the card this write would leave on disk.

    card_lint.check_card is the single reading of the type contract — a second
    required-field table here would let one card pass the gate and fail the lint.
    It reads a path, so the prospective text is staged in the temp directory: the
    vault must not hold a card the model has not actually written yet."""
    from epitype import card_lint

    handle, name = tempfile.mkstemp(prefix="epitype-write-", suffix=".md")
    staging = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        card_type, findings = card_lint.check_card(
            staging, relative, before_text=before_text, source_path=source_path)
    finally:
        try:
            staging.unlink()
        except OSError:
            pass

    fails = [reason for level, _rule, reason in findings if level == card_lint.FAIL]
    warns = [reason for level, _rule, reason in findings if level == card_lint.WARN]
    advice = (
        memspec.WRITE_GATE_CARD_ADVICE.format(
            card_type=card_type, path=relative,
            problems=memspec.PROBLEM_JOINER.join(warns)
        )
        if warns
        else None
    )
    if not fails:
        return None, advice
    problems = memspec.PROBLEM_JOINER.join(fails)
    examples = [
        example
        for field, example in memspec.WRITE_GATE_FIELD_EXAMPLES.items()
        if field in problems
    ]
    reason = memspec.WRITE_GATE_CARD_REASON.format(
        card_type=card_type,
        path=relative,
        problems=problems,
        example=memspec.PROBLEM_JOINER.join(examples[: memspec.GATE_DEFECT_MAX_LINES])
        or memspec.WRITE_GATE_CARD_EXAMPLE_FALLBACK,
    )
    return reason[: memspec.WRITE_GATE_REASON_MAX_CHARS], advice


def _write_marker(session_id, rule, target, text):
    """Same-session dedupe keyed by (rule, file, content digest): a model that
    cannot satisfy a ruling would otherwise be denied the same write forever."""
    # 用到才載入：hashlib 要 6 ms，絕大多數呼叫走不到這裡。
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return _notice_marker(
        session_id, f"{memspec.WRITE_GATE_LOG_KIND}\0{rule}\0{target}\0{digest}"
    )


def _append_write_block(vault, rule, subject, target, started_at, session_id=None):
    """Audit a blocked write by rule, subject, and filename only — the content is
    exactly the material a ruling is about and does not belong in the ledger."""
    append_gate_log(
        vault,
        with_session(
            {
                "kind": "write_note",
                "rule": rule,
                "filename": target.name,
                **subject,
            },
            session_id,
        ),
        started_at,
    )


def _append_recurrence_allow(vault, outcome, relative, target, started_at, session_id=None):
    """Audit a recurrence the gate saw and let through, in the write_block row's shape
    and with the same limit: rule, card and outcome, never the text."""
    append_gate_log(
        vault,
        with_session(
            {
                "kind": memspec.RECURRENCE_GATE_ALLOW_LOG_KIND,
                "rule": memspec.RECURRENCE_GATE_RULE,
                "filename": target.name,
                "card_path": relative,
                "outcome": outcome,
            },
            session_id,
        ),
        started_at,
    )


def _card_lines(text):
    """A card's lines as the recurrence rule counts them: BOM dropped, right-stripped,
    split exactly like memspec.split_frontmatter so the indexes line up."""
    return [line.rstrip() for line in text.lstrip("﻿").splitlines()]


def _field_spans(text, fields):
    """({field: its non-blank frontmatter lines}, indexes of every line inside those fields).

    The top-level reading every gate uses: a field is its `key:` line plus the indented
    lines under it. A key under `metadata:` is that field too (the shared reading moves
    it to the top level), so the indexes go through the layout's origins to land on the
    card's own lines. A key nested under any other parent is not the field — the gates
    do not read it, and card_lint already FAILs that card as disarmed."""
    blocks, inside = {}, set()
    front, closing, origins, _hoisted = memspec.frontmatter_layout(text)
    if front is None or closing is None:
        return blocks, inside
    parent = None
    for origin, raw_line in zip(origins, front):
        index = origin + 1
        line = raw_line.rstrip()
        if not line.strip() or line[:1].isspace():
            if parent in fields:
                inside.add(index)
                if line.strip():
                    blocks[parent] += (line,)
            continue
        match = memspec.TOP_LEVEL_FIELD.match(line)
        parent = match.group(1) if match else None
        if parent in fields:
            inside.add(index)
            blocks[parent] = blocks.get(parent, ()) + (line,)
    return blocks, inside


def _line_delta(before, after):
    """(lines only `after` has, lines only `before` has), counted as multisets so a
    duplicated line is still one addition."""
    from collections import Counter

    remaining = Counter(before)
    added = []
    for line in after:
        if remaining[line] > 0:
            remaining[line] -= 1
        else:
            added.append(line)
    return added, list(remaining.elements())


def _incident_line(line):
    """True when the line narrates this rule being broken again.

    A marker alone is not a narrative: real vaults say 「免得以後再犯」「防再犯規則」 as
    ordinary words. Two shapes count — a marker and a date on the same line, or a bold
    or heading line that opens with the marker. A marker right after a preventive word
    never counts, and neither does a conditional one (「再犯時先補 forbidden」): that is
    the rule's own text about what to do next time. 「時／的話」 right after the marker is
    checked before the date, so 「2026-09-25 再犯時先補…」 is still a rule; the cost is that
    a narrative phrased 「那天又犯時…」 passes. Otherwise a date right before the marker
    (「2026-09-25 又犯就…」) names a day it happened, and a trailing 就／則 does not excuse it."""
    hits = []
    for hit in memspec.RECURRENCE_GATE_MARKER_REGEX.finditer(line):
        window = line[max(0, hit.start() - memspec.RECURRENCE_GATE_PREVENTIVE_WINDOW):hit.start()]
        if memspec.RECURRENCE_GATE_PREVENTIVE_REGEX.search(window) is not None:
            continue
        if memspec.RECURRENCE_GATE_CONDITIONAL_AFTER_REGEX.match(line, hit.end()) is not None:
            continue
        if memspec.RECURRENCE_GATE_DATED_MARKER_REGEX.search(window) is not None:
            hits.append(hit)
            continue
        if memspec.RECURRENCE_GATE_CONDITIONAL_BEFORE_REGEX.search(window) is not None:
            continue
        hits.append(hit)
    if not hits:
        return False
    if memspec.RECURRENCE_GATE_DATE_REGEX.search(line) is not None:
        return True
    emphasis = memspec.RECURRENCE_GATE_EMPHASIS_REGEX.match(line)
    if emphasis is None:
        return False
    lead = memspec.RECURRENCE_GATE_LEAD_REGEX.match(line, emphasis.end())
    start = lead.end() if lead is not None else emphasis.end()
    return any(hit.start() == start for hit in hits)


def _recurrence_review(card, target, additions, prospective, patch, started_at, session_id):
    """Deny value when this write narrates a recurrence into a behaviour card that it
    leaves exactly as unable to block as before; None otherwise (docs/FAILURE_MODES.md
    §45). `card` is the (vault, relative path) the target already resolved to; `patch`
    is the envelope entry when the write is a Codex Update File.

    Judged only on an existing card with frontmatter. A new card, a project/reference/
    decision card, and a named-tool write whose post-write text cannot be known are not
    this rule's business. A patch is applied to the card on disk, so it is judged on the
    same post-write text as a Write or Edit of the same content; a patch that cannot be
    applied writes nothing in Codex either, and passes."""
    if prospective is not None:
        new_text = prospective
    elif patch is not None:
        new_text = "\n".join(str(run) for run in additions)
    else:
        return None
    if memspec.RECURRENCE_GATE_MARKER_REGEX.search(new_text) is None:
        return None
    try:
        if not target.is_file() or target.stat().st_size > memspec.WRITE_GATE_MAX_CONTENT_BYTES:
            return None
        before = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    front, closing = memspec.split_frontmatter(before)
    if front is None or closing is None or expired(started_at):
        return None
    if prospective is None:
        prospective = patch_envelope.apply_update(patch, before)

    from epitype import card_lint

    vault, relative = card
    kinds = [card_lint.card_type_of(relative, before)[0]]
    if prospective is not None:
        kinds.append(card_lint.card_type_of(relative, prospective)[0])
    card_type = next((kind for kind in kinds if kind in memspec.RECURRENCE_GATE_CARD_TYPES), None)
    if card_type is None:
        return None
    if prospective is None:
        # 補丁套不上磁碟上的卡，Codex 自己也會失敗、什麼都不會寫出去：放行，只在新增行
        # 真的有事故敘述時記一筆，讓這次繞過在稽核裡看得見。
        if any(
            _incident_line(line.rstrip())
            for run in additions
            for line in str(run).splitlines()
        ):
            _best_effort_audit(
                _append_recurrence_allow, vault, memspec.RECURRENCE_GATE_OUTCOME_UNAPPLIABLE,
                relative, target, started_at, session_id)
        return None

    exempt_fields = memspec.RECURRENCE_GATE_EXEMPT_FIELDS
    old_lines = _card_lines(before)
    old_blocks, old_inside = _field_spans(before, exempt_fields)
    new_lines = _card_lines(prospective)
    new_blocks, new_inside = _field_spans(prospective, exempt_fields)
    # 只有新增或改動算武裝；把欄位刪掉是拆防線，不是裝防線。
    arming = any(
        new_blocks.get(field) and new_blocks[field] != old_blocks.get(field)
        for field in memspec.RECURRENCE_GATE_ARMING_FIELDS
    )
    added_free, removed_free = _line_delta(
        [line for index, line in enumerate(old_lines) if index not in old_inside],
        [line for index, line in enumerate(new_lines) if index not in new_inside],
    )
    added_exempt, _removed = _line_delta(
        [line for index, line in enumerate(old_lines) if index in old_inside],
        [line for index, line in enumerate(new_lines) if index in new_inside],
    )

    incidents = [line for line in added_free if _incident_line(line)]
    # 淨增才算：改一個錯字的舊事故行是一刪一增，不是又記了一次。
    if len(incidents) <= sum(1 for line in removed_free if _incident_line(line)):
        if any(_incident_line(line) for line in added_exempt):
            _best_effort_audit(
                _append_recurrence_allow, vault, memspec.RECURRENCE_GATE_OUTCOME_EXEMPT_FIELDS,
                relative, target, started_at, session_id)
        return None
    if arming:
        _best_effort_audit(
            _append_recurrence_allow, vault, memspec.RECURRENCE_GATE_OUTCOME_ARMING,
            relative, target, started_at, session_id)
        return None
    if not _write_marker(session_id, memspec.RECURRENCE_GATE_RULE, target, new_text):
        _best_effort_audit(
            _append_recurrence_allow, vault, memspec.RECURRENCE_GATE_OUTCOME_REPEAT,
            relative, target, started_at, session_id)
        return None
    _best_effort_audit(
        _append_write_block,
        vault,
        memspec.RECURRENCE_GATE_RULE,
        {"card_path": relative},
        target,
        started_at,
        session_id,
    )
    armed = memspec.SLASH_JOINER.join((
        memspec.FORBIDDEN_FIELD,
        memspec.REQUIRE_WHEN_FIELD + "+" + memspec.REQUIRE_TEXT_FIELD,
        memspec.ACTION_GUARD_TOOL_FIELD + "+" + memspec.ACTION_GUARD_ALL_OF_FIELD,
        memspec.TURN_CHECK_FIELD,
    ))
    reason = memspec.RECURRENCE_GATE_REASON.format(
        path=relative,
        card_type=card_type,
        fragment=incidents[0].strip()[: memspec.WRITE_GATE_FRAGMENT_MAX_CHARS],
        armed=armed,
        unenforceable=memspec.UNENFORCEABLE_FIELD,
    )
    return _deny_value(reason[: memspec.WRITE_GATE_REASON_MAX_CHARS])


# scope_reference/source_note are said only when this guard's own conditions match
# the call; emitting them at discovery put every card's reference into every call.
_Guard = namedtuple("_Guard", "card tool substrings advice path requires unless when write_check advise_tool advise_fragments advise_text sequence audience on_hit scope_reference source_note", defaults=((), "", "note", (), ""))


def _read_guard(path):
    """One card's action guard, a defect string, or None when the card declares none.

    Validation is strict in the fail-open direction: a card whose substrings are too
    short, too many, or missing enforces nothing and says so, because dropping the
    bad items instead would leave fewer required fragments and therefore a guard that
    matches *more* than its author wrote."""
    from epitype import rule_pipeline

    path = Path(path)
    try:
        card_text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return f"[{path.name}] guard source UNVERIFIED: full card unreadable ({type(exc).__name__})"
    front, _closing = memspec.split_frontmatter(card_text)
    fields, problem = memspec.frontmatter_text(card_text)
    if problem:
        return f"[{path.name}] guard source INVALID: {problem}"
    if not any(key in fields for key in (
            memspec.ACTION_GUARD_TOOL_FIELD, "advise_tool", "require_tool_before", memspec.WRITE_CHECK_FIELD)):
        return None
    try:
        projected = rule_pipeline.projection(
            fields, path, legacy_text=fields.get("guard_advice") or fields.get("description", ""),
            card_text=card_text)
    except (ValueError, OSError, UnicodeError) as exc:
        return f"[{path.name}] guard source INVALID: {exc}"
    if projected["expired"]:
        return None
    if fields.get(memspec.DECISION_STATUS_FIELD, "").strip() in memspec.STOP_GATE_SILENT_STATUSES:
        return None

    def finish(entry):
        # Advice and its authority always come from this same fresh card read.
        rendered = rule_pipeline.render_projection(projected, card_path=path)
        rendered += "\n" + memspec.RULE_SOURCE_AUTHOR_LABEL + projected["authorship"]
        entry["source_scope"] = projected["document"].get("scope")
        entry["source_status"] = projected["provenance"]["status"]
        entry["source_render"] = rendered
        entry["source_issues"] = projected["provenance"].get("issues", [])
        # Explicit card demotion leaves unrelated retained safety guards unchanged.
        entry["on_hit"] = fields.get("on_hit", "note").strip().casefold()
        if projected['text'].strip():
            entry["advice"] = "\n".join(filter(None, (entry.get("advice", ""), rendered)))
        if entry.get("advise_text"):
            entry["advise_text"] += "\n" + rendered
        return entry

    def one_line(value):
        return " ".join(str(value or "").split())

    name = one_line(fields.get(memspec.NAME_FIELD)) or path.stem
    tool = one_line(fields.get(memspec.ACTION_GUARD_TOOL_FIELD))
    write_check = one_line(fields.get(memspec.WRITE_CHECK_FIELD))
    if not tool and not write_check and fields.get('require_tool_before') is not None:
        from epitype import action_sequence
        items=sequence_fields(front,('require_tool_before',))['require_tool_before']
        try:
            items=[item for item in items if action_sequence.rule(item) is not None]
        except ValueError as exc:
            return memspec.ACTION_GUARD_DEFECT.format(card=name,field='require_tool_before',reason=str(exc))
        if not items: return None
        return finish({'card':name,'sequence':items,'audience':one_line(fields.get('audience')),
                'advice':one_line(fields.get('description'))[:240],
                'expires':one_line(fields.get(memspec.VALID_UNTIL_FIELD))})
    if not tool and not write_check and fields.get("advise_tool"):
        fragments = sequence_fields(front, ("advise_all_of",))["advise_all_of"]
        if any(not isinstance(item, str) or not item.strip() for item in fragments):
            return memspec.ACTION_GUARD_DEFECT.format(
                card=name, field="advise_all_of", reason="提醒片段必須是非空字串")
        return finish({"card": name, "advise_tool": one_line(fields["advise_tool"]),
                "advise_fragments": fragments,
                "advise_text": one_line(fields.get("advise_text") or fields.get("description"))[:240],
                "expires": one_line(fields.get(memspec.VALID_UNTIL_FIELD))})
    if write_check and write_check not in memspec.WRITE_CHECK_VALUES:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name, field=memspec.WRITE_CHECK_FIELD,
            reason=f"不支援 {write_check!r}",
        )
    if not tool:
        if write_check:
            return finish({
                "card": name, "tool": "", "substrings": [], "advice": "",
                "requires": [], "unless": [], "when": [], "write_check": write_check,
                "expires": one_line(
                    fields.get(memspec.VALID_UNTIL_FIELD) or fields.get(memspec.GRANT_EXPIRES_FIELD)
                ),
            })
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name, field=memspec.ACTION_GUARD_TOOL_FIELD,
            reason=memspec.ACTION_GUARD_TOOL_EMPTY_REASON,
        )
    declared = sequence_fields(
        front,
        (
            memspec.ACTION_GUARD_ALL_OF_FIELD,
            memspec.ACTION_GUARD_REQUIRES_FIELD,
            memspec.ACTION_GUARD_UNLESS_FIELD,
            memspec.ACTION_GUARD_WHEN_FIELD,
        ),
    )
    substrings = declared[memspec.ACTION_GUARD_ALL_OF_FIELD]
    requires = declared[memspec.ACTION_GUARD_REQUIRES_FIELD]
    unless_items = declared[memspec.ACTION_GUARD_UNLESS_FIELD]
    when_items = declared[memspec.ACTION_GUARD_WHEN_FIELD]
    problem = None
    field_name = re.compile(memspec.ACTION_GUARD_FIELD_NAME_PATTERN + r"\Z")
    unless = []
    for item in unless_items:
        name_part, _, value_part = str(item).partition("=")
        # 逃生口寫壞就整張卡不生效。反過來（忽略壞掉的那一條）會讓守衛擋得比作者寫的更多，
        # 而擋過頭的那一方沒有人會來報案——被擋的人只會換個寫法繞過去。
        if not field_name.match(name_part.strip()) or not value_part.strip():
            problem = memspec.ACTION_GUARD_PAIR_SYNTAX_REASON.format(
                field=memspec.ACTION_GUARD_UNLESS_FIELD, item=item
            )
            break
        unless.append((name_part.strip(), value_part.strip()))
    if problem:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name, field=memspec.ACTION_GUARD_UNLESS_FIELD, reason=problem
        )
    when = []
    for item in when_items:
        name_part, _, value_part = str(item).partition("=")
        if not field_name.match(name_part.strip()) or not value_part.strip():
            problem = memspec.ACTION_GUARD_PAIR_SYNTAX_REASON.format(
                field=memspec.ACTION_GUARD_WHEN_FIELD, item=item
            )
            break
        values = tuple(
            piece.strip().casefold()
            for piece in value_part.split(memspec.ACTION_GUARD_WHEN_ALTERNATIVE)
            if piece.strip()
        )
        if not values:
            problem = memspec.ACTION_GUARD_PAIR_NO_VALUE_REASON.format(
                field=memspec.ACTION_GUARD_WHEN_FIELD, item=item
            )
            break
        when.append((name_part.strip(), values))
    if problem:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name, field=memspec.ACTION_GUARD_WHEN_FIELD, reason=problem
        )
    if len(when) > memspec.ACTION_GUARD_MAX_WHEN:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name,
            field=memspec.ACTION_GUARD_WHEN_FIELD,
            reason=memspec.ACTION_GUARD_WHEN_TOO_MANY_REASON.format(
                count=len(when), limit=memspec.ACTION_GUARD_MAX_WHEN),
        )
    for item in requires:
        if not field_name.match(str(item).strip()):
            return memspec.ACTION_GUARD_DEFECT.format(
                card=name,
                field=memspec.ACTION_GUARD_REQUIRES_FIELD,
                reason=memspec.ACTION_GUARD_NOT_A_FIELD_REASON.format(item=item),
            )
    if len(requires) > memspec.ACTION_GUARD_MAX_REQUIRES:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name,
            field=memspec.ACTION_GUARD_REQUIRES_FIELD,
            reason=memspec.ACTION_GUARD_REQUIRES_TOO_MANY_REASON.format(
                count=len(requires), limit=memspec.ACTION_GUARD_MAX_REQUIRES),
        )
    if not substrings and (requires or when):
        # 欄位型的守衛不需要字面片段：它問的是「少了什麼」或「哪兩個欄位配在一起」，
        # 兩者都沒有字面可以比對。
        return finish({
            "card": name,
            "tool": tool,
            "substrings": [],
            "advice": one_line(
                fields.get(memspec.ACTION_GUARD_ADVICE_FIELD)
                or fields.get(memspec.DESCRIPTION_FIELD)
            ),
            "requires": [str(item).strip() for item in requires],
            "unless": [list(pair) for pair in unless],
            "when": [[pair[0], list(pair[1])] for pair in when],
            "write_check": write_check,
            # 到期日只存不判（同下）。漏了這一行的話，欄位型守衛的 valid_until 會被
            # 靜靜忽略——卡片寫了期限、閘永遠不會停。
            "expires": one_line(
                fields.get(memspec.VALID_UNTIL_FIELD) or fields.get(memspec.GRANT_EXPIRES_FIELD)
            ),
        })
    if not substrings:
        problem = memspec.ACTION_GUARD_NO_CONDITION_REASON.format(
            all_of=memspec.ACTION_GUARD_ALL_OF_FIELD,
            requires=memspec.ACTION_GUARD_REQUIRES_FIELD,
            when=memspec.ACTION_GUARD_WHEN_FIELD,
        )
    elif len(substrings) > memspec.ACTION_GUARD_MAX_SUBSTRINGS:
        problem = memspec.ACTION_GUARD_TOO_MANY_FRAGMENTS_REASON.format(
            limit=memspec.ACTION_GUARD_MAX_SUBSTRINGS)
    elif (
        len(substrings) == 1
        and len(substrings[0]) < memspec.ACTION_GUARD_LONE_FRAGMENT_MIN_CHARS
    ):
        problem = memspec.ACTION_GUARD_LONE_FRAGMENT_REASON.format(
            limit=memspec.ACTION_GUARD_LONE_FRAGMENT_MIN_CHARS)
    if problem:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name, field=memspec.ACTION_GUARD_ALL_OF_FIELD, reason=problem
        )
    advice = one_line(
        fields.get(memspec.ACTION_GUARD_ADVICE_FIELD)
        or fields.get(memspec.DESCRIPTION_FIELD)
    )
    return finish({
        "card": name,
        "tool": tool,
        "substrings": list(substrings),
        "advice": advice,
        "requires": [str(item).strip() for item in requires],
        "unless": [list(pair) for pair in unless],
        # 欄位組合條件在這一支也要帶上。2026-09-20 Codex 審查抓到：只有「沒有字面片段」
        # 那一支序列化了 when，於是一張同時寫了字面與欄位條件的卡，欄位那半在讀卡時就
        # 消失，結果變成「只要文字命中就擋」——比作者寫的寬。
        "when": [[pair[0], list(pair[1])] for pair in when],
        "write_check": write_check,
        # 到期日只存不判：卡片不動也會過期，而這裡的結果會進快取。
        "expires": one_line(
            fields.get(memspec.VALID_UNTIL_FIELD) or fields.get(memspec.GRANT_EXPIRES_FIELD)
        ),
    })


def _guard_cache(vault):
    return vault / memspec.FTS_INDEX_DIRECTORY / memspec.ACTION_GUARD_CACHE_FILENAME


def warm_guard_cache(vaults, started_at, deadline=None):
    """Read every card's guard status once, inside `deadline` (a time.monotonic() cap).

    A tool call may only re-read a bounded slice of a vault, so on a cold cache a
    guard sitting past that slice is not enforced yet — the gate under-enforces
    silently, which is the failure this whole mechanism exists to remove. SessionStart
    has budget a tool call does not, so it pays the discovery cost once per session
    and every later call reads a warm cache.

    Warming is pure optimisation, so the deadline is a hard stop, checked between
    vaults and between cards inside a vault. A truncated pass writes nothing at all —
    the cache is all-or-nothing, so a vault that cannot be walked inside the deadline
    starts cold again next session and this segment buys nothing there (measured
    2026-09-22: the 463-card governance vault finishes in 1.02 s, so it does converge
    on this machine). Truncating changes how fast the cache finishes warming, never
    whether a guard is enforced — the gate itself reads no less and skips nothing,
    because what it never wrote it also never claims to know.
    Without a deadline the segment takes its own default budget rather than running
    until the host's timeout: `started_at` bounds the host's patience, not this
    segment's share of it.
    """
    if deadline is None:
        deadline = time.monotonic() + memspec.SESSIONSTART_WARM_GUARD_BUDGET_SECONDS
    for vault in vaults:
        if expired(started_at) or time.monotonic() >= deadline:
            return
        try:
            _guards(vault, started_at, [], cap=_WARM_CAP, deadline=deadline)
        except Exception:
            continue


_WARM_CAP = 1 << 30
# 欄位形狀增加 write_check；版本不對就整份重讀。
# 6：包在 `metadata:` 底下的守衛欄位照頂層讀，舊快取把那些卡記成「沒有守衛」。
_GUARD_CACHE_VERSION = 8


def _guards(vault, started_at, defects, cap=None, deadline=None, *, event=None, advisories=None,
            match_first=False):
    """Every usable guard in one vault, discovered through a manifest cache.

    The cache locates candidates; it never supplies executable evidence. Every
    known positive is read in full on each call, including its original source.
    Changed and rotating negative candidates have a separate discovery budget.
    A warm call without an actual event only discovers guards, never enforces.
    match_first=True returns ADVISORY-scope guards too, carrying their reference,
    and defers every per-card note to the caller's own trigger match."""
    from epitype import cardscan, rule_context

    def timed_out():
        return expired(started_at) or (deadline is not None and time.monotonic() >= deadline)

    def timeout_notice():
        defects.append(f"[{Path(vault).name}] guard source UNVERIFIED: verification budget exhausted; next check: retry this vault's actual guards")

    vault = Path(vault).resolve()
    try:
        scan = cardscan.scan_vault(Path(vault).resolve())
    except Exception as exc:
        # 這個庫的規則這一次一條都沒生效。安靜回空的話，外面看起來就像「這裡沒有
        # 規則」——而那正是沒有人會去查的那個答案。
        defects.append(memspec.GATE_VAULT_UNREADABLE_NOTICE.format(
            gate=memspec.GATE_NAME_ACTION, vault=Path(vault).name,
            reason=type(exc).__name__))
        return []
    manifest, paths = {}, {}
    for card_path, path, mtime_ns, size, ctime_ns in scan:
        if timed_out():
            timeout_notice()
            return []
        # 走訪已經帶回這三項；再 stat 一次只為了 inode 與裝置編號，而 Windows 的
        # 目錄列表根本不給那兩項（實測回 0），等於每張卡多付一次系統呼叫換兩個零。
        manifest[card_path] = [mtime_ns, size, ctime_ns]
        paths[card_path] = path

    cache_path = _guard_cache(vault)
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        # 版本沒對上就整份重讀。以前這裡不看版本，於是舊格式的項目會缺新欄位、而清單
        # 又只在檔案變動時才重讀——新加的判斷（例如到期）對既有的卡永遠不會生效。
        if isinstance(loaded, dict) and loaded.get("version") != _GUARD_CACHE_VERSION:
            loaded = None
        old_manifest = loaded.get("manifest") if isinstance(loaded, dict) else None
        cached = loaded.get("guards") if isinstance(loaded, dict) else None
        cursor = loaded.get("cursor") if isinstance(loaded, dict) else ""
        if not isinstance(old_manifest, dict) or not isinstance(cached, dict):
            old_manifest, cached, cursor = {}, {}, ""
        if not isinstance(cursor, str):
            cursor = ""
    except (OSError, ValueError):
        old_manifest, cached, cursor = {}, {}, ""

    old_cursor = cursor
    known = {key: value for key, value in cached.items() if key in paths}
    verified = {key: old_manifest.get(key) for key in known}
    changed = [key for key in paths if key not in cached or old_manifest.get(key) != manifest[key]]
    negatives = sorted(key for key in paths if key in cached and key not in changed and not known.get(key))
    rotated = [key for key in negatives if key > cursor] + [key for key in negatives if key <= cursor]
    budget = memspec.ACTION_GUARD_MAX_CARDS_PER_VAULT if cap is None else cap
    positives = sorted(key for key, value in known.items() if value)
    discovery = [key for key in changed + rotated if key not in positives][:budget]
    fresh = {}
    for card_path in positives + discovery:
        if timed_out():
            timeout_notice()
            return []
        try:
            known[card_path] = _read_guard(paths[card_path])
        except Exception as exc:
            known[card_path] = f"[{card_path}] guard source UNVERIFIED: fresh read failed ({type(exc).__name__})"
        fresh[card_path] = known[card_path]
        verified[card_path] = manifest[card_path]
        if card_path in negatives:
            cursor = card_path
    if timed_out():
        timeout_notice()
        return []
    if len([key for key in changed if key not in positives]) > len(discovery):
        defects.append(f"[{vault.name}] guard discovery UNVERIFIED: new/changed candidates remain; next check: continue bounded discovery")

    if verified != old_manifest or known != cached or cursor != old_cursor:
        staging = cache_path.with_name(f".{cache_path.name}.tmp-{os.getpid()}")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            staging.write_text(
                json.dumps(
                    {"version": _GUARD_CACHE_VERSION, "manifest": verified,
                     "guards": known, "cursor": cursor},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(staging, cache_path)
        except OSError as exc:
            defects.append(f"[{vault.name}] guard locator cache unreadable/unwritable ({type(exc).__name__}); fresh results only")
        finally:
            # 寫失敗留下的暫存檔沒人會回頭清，而檔名帶 pid，每次失敗都是新的一個。
            try:
                staging.unlink()
            except OSError:
                pass

    found = []
    for card_path in sorted(fresh):
        entry = fresh[card_path]
        if isinstance(entry, str):
            defects.append(entry)
        elif isinstance(entry, dict) and (
            entry.get("substrings") or entry.get("requires") or entry.get("when")
            or entry.get("write_check") or entry.get("advise_tool") or entry.get("sequence")
        ):
            if memspec.card_expired(entry.get("expires")):
                # 過期的守衛不再攔人；時限型的規則要自己停下來，不能靠人記得去拔。
                continue
            if event is None:
                continue
            on_hit = entry.get("on_hit", "note")
            advice = entry.get("advice", "")
            source_note = ""
            if entry.get("source_status") != "VERIFIED":
                source_note = f"[{card_path}] guard source UNVERIFIED: " + "; ".join(entry.get("source_issues") or ["legacy source not verified"])
                if not match_first:
                    defects.append(source_note)
                # CORE-08/CORE-25: only an owner-verified source may deny; an
                # unverified card's on_hit: block is shown as a reminder.
                if on_hit == "block":
                    on_hit = "note"
                    advice = "\n".join(filter(None, (advice, memspec.ACTION_GUARD_UNVERIFIED_BLOCK_NOTE)))
            scope = entry.get("source_scope")
            scope_reference = ()
            if scope is not None:
                state, reason = rule_context.applicability(scope, event)
                if state == "OUTSIDE":
                    continue
                if state == "ADVISORY":
                    reference = "限定參考（本場範圍未確認）：" + entry.get("source_render", "")
                    if not match_first:
                        defects.append(f"[{card_path}] guard scope ADVISORY: {reason}\n{reference}")
                        if advisories is not None:
                            advisories.append((paths[card_path], entry.get("card", card_path), reference))
                        continue
                    scope_reference = (f"[{card_path}] guard scope ADVISORY: {reason}\n{reference}", reference)
            found.append(
                _Guard(
                    entry.get("card", card_path),
                    entry.get("tool", ""),
                    tuple(item for item in (entry.get("substrings") or ()) if isinstance(item, str)),
                    advice,
                    vault / card_path,
                    tuple(item for item in (entry.get("requires") or ()) if isinstance(item, str)),
                    tuple(
                        (pair[0], pair[1])
                        for pair in (entry.get("unless") or ())
                        if isinstance(pair, (list, tuple)) and len(pair) == 2
                    ),
                    tuple(
                        (pair[0], tuple(str(value).casefold() for value in pair[1]))
                        for pair in (entry.get("when") or ())
                        if isinstance(pair, (list, tuple)) and len(pair) == 2
                        and isinstance(pair[1], (list, tuple)) and pair[1]
                    ),
                    entry.get("write_check", ""),
                    entry.get("advise_tool", ""),
                    tuple(entry.get("advise_fragments") or ()),
                    entry.get("advise_text", ""),
                    tuple(entry.get("sequence") or ()),
                    entry.get("audience", ""),
                    on_hit,
                    scope_reference,
                    source_note,
                )
            )
    return found


def _guard_matched(guard, defects, advisories):
    """Say a guard's deferred notes once its own trigger matched this call.

    True when the guard is only an ADVISORY-scope reference: the caller then stops,
    because a reference is shown but never enforced."""
    # One guard can match through its advise and its tool trigger in the same call.
    if guard.source_note and guard.source_note not in defects:
        defects.append(guard.source_note)
    if not guard.scope_reference:
        return False
    line, reference = guard.scope_reference
    defects.append(line)
    if advisories is not None:
        advisories.append((guard.path, guard.card, reference))
    return True


def _action_text(tool_input):
    """Every acting string the call carries, joined — the haystack a guard reads.

    Nothing is parsed: a guard asks whether its fragments all appear in what this
    call actually does, which is the one question a string check can answer honestly
    about a shell command. The only fields left out are the ones that cannot act —
    `description` is prose written for the owner's permission card, and its words
    were enough to fake a hit (see ACTION_GUARD_IGNORED_FIELDS)."""
    if not isinstance(tool_input, dict):
        return ""
    parts = [
        value
        for key, value in tool_input.items()
        if isinstance(value, str) and key not in memspec.ACTION_GUARD_IGNORED_FIELDS
    ]
    return "\n".join(parts)[: memspec.ACTION_GUARD_HAYSTACK_MAX_CHARS]


def _guard_review(event, tool_name, tool_input, config, started_at, defects, advisories=None):
    """(deny value, ()) when a scar card's fragments all appear in this call.

    Owner 2026-09-16 lifted §34's "cards may not carry an action condition". What is
    restored is only the literal form: every fragment must be present, as plain text.
    Semantic judgement and genuinely irreversible actions remain the host's native
    rules, exactly as §34 left them."""
    # 用到才載入：hashlib 要 6 ms，絕大多數呼叫走不到這裡。
    import hashlib

    haystack = _action_text(tool_input)
    # 必填欄位型的守衛問的是「少了什麼」，所以呼叫沒有任何可比對的字串時它仍然要判——
    # 只有連 tool_input 都不是個欄位集合時，這道閘才真的沒有東西可問。
    if not haystack and not isinstance(tool_input, dict):
        return None
    # 刻意不看 `stop_hook_active`。那是 Stop 閘為了避免自己重入才讀的旗標，動作守衛
    # 沒有重入問題（它不寫東西，拒絕也不會再觸發自己）。照著讀的話，Stop 閘擋下之後
    # 的那一段續跑，工具守衛整個是關的——而那正是我被逼著換做法、最可能亂動手的時刻。
    folded_tool = tool_name.casefold()
    for vault in resolve_vaults(config, event):
        if expired(started_at):
            defects.append("guard source UNVERIFIED: verification budget exhausted before vault read")
            return None
        # 自動降級已經拿掉（判準分不出「規則太寬」與「我一直犯這條」，2026-09-17
        # 實跑：一條 6 次命中、6 次全擋下、0 漏擋的規則被關了 14 天）。那個函式現在
        # 永遠回空集合，而為了拿一個空集合，每一次工具呼叫都要多載 8.3 ms 的模組。
        demoted = frozenset()
        for guard in _guards(vault, started_at, defects, event=event, advisories=advisories,
                             match_first=True):
            if guard.sequence:
                from epitype import action_receipts, action_sequence, tool_sequence
                if not tool_sequence.applies(guard.audience,event): continue
                actions=action_receipts.projected(event)
                mutations={item.partition('=>')[2].strip()[1:] for item in guard.sequence}
                if any(action.kind in mutations for action in actions):
                    if _guard_matched(guard, defects, advisories):
                        continue
                    facts=action_receipts.read(event,action_receipts.database_path(config_path()))
                    start=max((max(f.started,f.finished) for f in facts),default=0)+1
                    # This is a proposed operation, not persisted execution evidence.
                    proposed=action_receipts.Fact('__proposed__',start,start+1,True,tuple(actions))
                    judgments=action_sequence.check(guard.sequence,(*facts,proposed),{'__proposed__'},
                        covered=action_receipts.coverage(event,action_receipts.database_path(config_path())))
                    safety = _sequence_preflight(event, actions, started_at, defects,
                        read_files=any(j.verdict != 'allow' for j in judgments))
                    if safety is not None:
                        return safety
                    missing=next((j for j in judgments if j.verdict=='block'),None)
                    if missing:
                        _best_effort_audit(append_gate_log,vault,with_session({
                            'kind':'action_note','rule':'tool_sequence','card':guard.card,
                            'card_path':str(guard.path.relative_to(vault)),'tool':tool_name,
                            'digest':hashlib.sha256(haystack.encode('utf-8',errors='replace')).hexdigest()[:16]},
                            event.get('session_id')),started_at)
                        if advisories is not None:
                            advisories.append((guard.path, guard.card,
                                f'查核提醒：{missing.reason}。已自動核對可讀取的現況；{guard.advice}'))
                    for allowed in (j for j in judgments if j.verdict=='allow'):
                        _best_effort_audit(append_gate_log,vault,with_session({
                            'kind':'sequence_allow','rule':'tool_sequence','card':guard.card,
                            'card_path':str(guard.path.relative_to(vault)),'tool':tool_name,
                            'target_digest':hashlib.sha256(allowed.target.encode('utf-8')).hexdigest()[:16],
                            'tool_use_id':event.get('tool_use_id')},event.get('session_id')),started_at)
                    for unknown in (j for j in judgments if j.verdict=='unverified'):
                        defects.append(f'[{guard.card}] UNVERIFIED: {unknown.reason}')
                        if advisories is not None and guard.advice:
                            advisories.append((guard.path,guard.card,f'未驗證：{unknown.reason}。{guard.advice}'))
                continue
            if (guard.advise_text
                    and memspec.action_guard_tool_matches(guard.advise_tool, folded_tool)
                    and all(fragment in haystack for fragment in guard.advise_fragments)):
                if _guard_matched(guard, defects, advisories):
                    continue
                if advisories is not None:
                    advisories.append((guard.path, guard.card, guard.advise_text))
            if not guard.tool:
                continue
            if not memspec.action_guard_tool_matches(guard.tool, folded_tool) or guard.card in demoted:
                continue
            if guard.substrings and not all(
                fragment in haystack for fragment in guard.substrings
            ):
                continue
            fields = tool_input if isinstance(tool_input, dict) else {}
            if any(
                str(fields.get(name, "")).strip() == value for name, value in guard.unless
            ):
                continue
            # 欄位組合型的條件：全部成立才算命中。一組不成立就整張卡跳過，不是「部分命中」。
            if guard.when and not all(
                str(fields.get(name, "") or "").strip().casefold() in values
                for name, values in guard.when
            ):
                continue
            missing = [
                name for name in guard.requires if not str(fields.get(name, "") or "").strip()
            ]
            if guard.requires and not missing:
                continue
            if _guard_matched(guard, defects, advisories):
                continue
            if missing:
                reason = memspec.ACTION_GUARD_REQUIRES_REASON.format(
                    card=guard.card,
                    tool=tool_name,
                    fields=memspec.LIST_JOINER.join(_quoted(name) for name in missing),
                    advice=guard.advice,
                )
            elif not guard.substrings and guard.when:
                reason = memspec.ACTION_GUARD_WHEN_REASON.format(
                    card=guard.card,
                    tool=tool_name,
                    pairs=memspec.PAIR_JOINER.join(
                        f"{name}={str(fields.get(name, '') or '').strip()}"
                        for name, _values in guard.when
                    ),
                    advice=guard.advice,
                )
            else:
                fragments = memspec.LIST_JOINER.join(
                    _quoted(fragment[: memspec.ACTION_GUARD_FRAGMENT_MAX_CHARS])
                    for fragment in guard.substrings
                )
                reason = memspec.ACTION_GUARD_REASON.format(
                    card=guard.card, tool=tool_name, fragments=fragments, advice=guard.advice
                )
            advisory_hit = (guard.on_hit != "block" or (missing == ["tabId"]
                and tool_name == control_lifecycle.CHROME + "navigate"))
            _best_effort_audit(
                append_gate_log,
                vault,
                with_session(
                    {
                        "kind": "action_note" if advisory_hit else memspec.ACTION_GUARD_LOG_KIND,
                        "rule": memspec.ACTION_GUARD_RULE,
                        "card": guard.card,
                        "card_path": str(guard.path.relative_to(vault)),
                        "tool": tool_name,
                        # 夜間重放靠它分辨「這張卡放行了」與「同一次呼叫被別張卡擋下」。
                        "digest": hashlib.sha256(
                            haystack.encode("utf-8", errors="replace")
                        ).hexdigest()[:16],
                    },
                    event.get("session_id"),
                ),
                started_at,
            )
            if advisory_hit:
                if advisories is not None:
                    advisories.append((guard.path, guard.card,
                                       memspec.ACTION_GUARD_NOTE.format(advice=guard.advice)))
                else:
                    defects.append(memspec.ACTION_GUARD_NOTE.format(advice=reason))
                continue
            # Explicit retained safety cards continue to return deny.
            return _deny_value(reason)
    return None


def _sequence_preflight(event, actions, started_at, notices, *, read_files=False):
    """Run necessary existing prerequisite checks without inventing action authority."""
    for action in actions:
        target = Path(action.target)
        if action.kind == "file.change" and read_files:
            sensitive = (target.name.casefold().startswith(".env")
                or target.suffix.casefold() in (".pem", ".key", ".p12", ".pfx")
                or any(part.casefold() in (".ssh", ".aws", ".gnupg") for part in target.parts)
                or re.search(r"(?:credential|secret|token)", target.name, re.I)
                or target.name.casefold() in ("rclone.conf", "brain.json", "council_snapshot.json"))
            if sensitive:
                notices.append("未自動讀取可能含密鑰／憑證或生產狀態的目標：" + str(target)
                               + "；保留宿主原生安全設定，由 owner 明確指示處理。")
                continue
            try:
                if target.is_file():
                    read_bytes = 0
                    with target.open("rb") as stream:
                        while not expired(started_at):
                            chunk = stream.read(65536)
                            if not chunk:
                                break
                            read_bytes += len(chunk)
                    complete = read_bytes == target.stat().st_size
                    notices.append(f"已自動讀取改檔目標 {target} 的舊內容：{read_bytes} bytes，"
                                   + ("完整檔案；未回傳原文。" if complete else "本次只讀部分，餘段未驗證。"))
            except OSError as exc:
                notices.append(f"改檔前讀取未驗證：{target}（{type(exc).__name__}）；由寫檔工具回報目標錯誤。")
        elif action.kind == "git.main_update":
            try:
                checked = subprocess.run(["git", "-C", str(target), "status", "--porcelain=v1"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=max(.05, min(1.0, remaining_seconds(started_at))), check=False)
            except (OSError, subprocess.TimeoutExpired):
                return _deny_value(f"無法確認 main checkout {target} 的現況；先保留現有 Git 狀態。")
            if checked.returncode:
                return _deny_value(f"main checkout {target} 的 status 失敗；先保留現有 Git 狀態。")
            if checked.stdout.strip():
                return _deny_value(f"main checkout {target} 有未提交變更；先隔離或回報，不能覆蓋他人的工作。")
            notices.append(f"已自動核對 main checkout {target}：工作樹乾淨。")
        elif action.kind == "directory.delete":
            # Worktree inspection is a prerequisite, never permission for permanent deletion.
            if action.repository:
                try:
                    checked = subprocess.run(["git", "-C", action.repository, "worktree", "list", "--porcelain"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=max(.05, min(1.0, remaining_seconds(started_at))), check=False)
                    if checked.returncode == 0:
                        notices.append(f"已自動查核 {action.repository} 的 worktree 清單；這不構成刪除授權。")
                except (OSError, subprocess.TimeoutExpired):
                    notices.append("worktree 查核未驗證，目標保持原狀。")
            return _deny_value(f"永久刪除目錄 {target} 仍受安全界線保護；前置查核不等於刪除授權。")
    return None


def _encoding_bom(head):
    """PowerShell 5.1 認得的 BOM：UTF-8、UTF-16 LE、UTF-16 BE 都讓它正確判碼。"""
    return (head.startswith(b"\xef\xbb\xbf")
            or head.startswith(b"\xff\xfe")
            or head.startswith(b"\xfe\xff"))


def _ps_encoding_safe(text):
    """寫入後的內容對 PowerShell 5.1 是否安全。

    純 ASCII 時 UTF-8 與 cp950 的位元組完全相同，編碼無關、不需 BOM；否則首字元必須是
    BOM（UTF-8 或 UTF-16 都以 `\ufeff` 開頭）才能標明非 ANSI 編碼。"""
    return text.startswith("\ufeff") or text.isascii()


# 宿主原生設定：卡片是正本，`epitype sync` 渲染出來的鍵不准直接改（見 epitype/host_native.py）。
# 檔名先比對，對上了才讀狀態檔——絕大多數呼叫連那一次讀檔都不付。
_NATIVE_BASENAMES = frozenset(name.casefold() for name in (
    memspec.HOST_NATIVE_CLAUDE_USER_SETTINGS,
    memspec.HOST_NATIVE_CLAUDE_PROJECT_SETTINGS,
    memspec.HOST_NATIVE_CODEX_CONFIG,
    memspec.HOST_NATIVE_CODEX_RULES_FILENAME,
))
# 殼層判斷只給真的會執行指令的工具：Grep 的樣式裡剛好有 `rm ` 不是在寫檔。
_NATIVE_NON_SHELL_TOOLS = frozenset((
    "read", "glob", "grep", "webfetch", "websearch", "agent", "task", "ls", "notebookread",
))
_NATIVE_CLAUDE, _NATIVE_CODEX_CONFIG, _NATIVE_CODEX_RULES = (
    "claude_settings", "codex_config", "codex_rules")


def _native_state():
    """{normcase 路徑: 紀錄}；讀不到就是沒有被管理的鍵（放行）。與 host_native.state_path 同一處。"""
    path = Path.home() / ".epitype" / memspec.HOST_NATIVE_STATE_FILENAME
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    targets = loaded.get("targets") if isinstance(loaded, dict) else None
    return targets if isinstance(targets, dict) else {}


def _native_protected(record):
    """[(顯示名, 取值路徑, 清單項目或 None, 卡片, 欄位, 殼層比對字, 卡片要的值)]：
    這個檔裡被管理的每一個鍵。清單項目（deny 規則）的「值」是它在不在，卡片要的是在。"""
    kind = record.get("kind")
    items = []
    if kind == _NATIVE_CLAUDE:
        for key, entry in (record.get("env") or {}).items():
            items.append((f"env.{key}", ("env", key), None, (entry or {}).get("card"),
                          memspec.HOST_CLAUDE_ENV_FIELD, key, (entry or {}).get("value")))
        for rule, entry in (record.get("deny") or {}).items():
            items.append((f"permissions.deny[{rule}]", ("permissions", "deny"), rule,
                          (entry or {}).get("card"), memspec.HOST_CLAUDE_DENY_FIELD, rule, True))
    elif kind == _NATIVE_CODEX_CONFIG:
        for dotted, entry in (record.get("keys") or {}).items():
            items.append((dotted, tuple(dotted.split(".")), None, (entry or {}).get("card"),
                          memspec.HOST_CODEX_CONFIG_FIELD, dotted.rsplit(".", 1)[-1],
                          (entry or {}).get("value")))
    return items


def _native_value(parsed, path, item):
    node = parsed
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if item is not None:
        return isinstance(node, list) and item in node
    return node


def _native_parse(kind, text):
    """解析設定檔內容；解析不了回 None。"""
    try:
        if not text.strip():
            return {}
        if kind == _NATIVE_CLAUDE:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        import tomllib

        return tomllib.loads(text)
    except (ValueError, TypeError):
        return None


def _native_reason(path, name, card, field):
    return _deny_value(memspec.HOST_NATIVE_GUARD_REASON.format(
        path=path, key=name, card=card or "?", field=field))


def _native_file_review(target, after, deleting=False):
    """這次寫入改到了 Epitype 管理的鍵嗎？改到就擋，其餘放行。

    只擋「這次寫入讓被管理的鍵變了、而且變的不是卡片要的值」：檔案本來就漂移、這次寫的
    是別的鍵，照樣放行（修漂移是 `epitype sync` 的事，不該讓使用者其他的設定改不了）。
    寫入後的內容推不出來（Edit 的舊字串不在檔裡）就放行：那次寫入在宿主那一端也會失敗。"""
    if target.name.casefold() not in _NATIVE_BASENAMES:
        return None
    record = _native_state().get(os.path.normcase(os.fspath(target)))
    if not isinstance(record, dict):
        return None
    kind = record.get("kind")
    if kind == _NATIVE_CODEX_RULES:
        cards = sorted(set((record.get("rules") or {}).values()))
        return _native_reason(target, memspec.HOST_NATIVE_CODEX_RULES_FILENAME,
                              memspec.LIST_JOINER.join(cards), memspec.HOST_CODEX_RULES_FIELD)
    protected = _native_protected(record)
    if not protected:
        return None
    try:
        before_text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        before_text = ""
    before = _native_parse(kind, before_text) or {}
    if deleting:
        after_doc = {}
    elif after is None:
        return None
    else:
        after_doc = _native_parse(kind, after.lstrip("﻿"))
    for name, path, item, card, field, _token, wanted in protected:
        was = _native_value(before, path, item)
        # 寫完解析不了＝那個鍵等於被整份拿掉。
        now = None if after_doc is None else _native_value(after_doc, path, item)
        if now != was and now != wanted:
            return _native_reason(target, name, card, field)
    return None


def _native_shell_path(raw, cwd):
    """殼層寫的一個路徑，解析成與狀態表同一種寫法（normcase 的絕對路徑）；解析不了回 None。"""
    text = raw.strip()
    folded = text.casefold()
    for prefix in memspec.HOST_NATIVE_GUARD_HOME_PREFIXES:
        if folded == prefix or folded.startswith((prefix + "/", prefix + "\\")):
            text = os.fspath(Path.home()) + text[len(prefix):]
            break
    if os.name == "nt":
        # Git Bash 的 /c/<路徑> 就是 C:/<路徑>
        drive = re.match(r"^/([A-Za-z])(/.*)?$", text)
        if drive:
            text = drive.group(1) + ":" + (drive.group(2) or "/")
    try:
        path = Path(text)
        if not path.is_absolute():
            if not isinstance(cwd, str) or not cwd.strip():
                return None
            path = Path(cwd) / path
        return os.path.normcase(os.path.abspath(path))
    except (OSError, ValueError):
        return None


def _native_redirects(text, cwd):
    """[(目標的完整路徑, 是不是附加)]：這段殼層文字裡每一個輸出導向。"""
    found = []
    for match in re.finditer(memspec.HOST_NATIVE_GUARD_REDIRECT_REGEX, text):
        raw = next((group for group in match.groups() if group), None)
        target = _native_shell_path(raw, cwd) if raw else None
        if target is not None:
            found.append((target, match.group(0).startswith(">>")))
    return found


def _native_shell_review(text, state, cwd=None):
    """殼層這一側：同一次呼叫裡有被管理的檔名、被管理的鍵、一個寫入動作，三個都在才擋。

    讀（cat、grep、Get-Content，含 `2>/dev/null`）、`epitype sync`、複製出去當備份都放行。
    整份覆寫（`> 那個檔`）不必點名鍵：它會把被管理的鍵一起抹掉。"""
    if re.search(memspec.HOST_NATIVE_GUARD_SYNC_REGEX, text, re.IGNORECASE):
        return None
    folded = text.casefold()
    writes = re.search(memspec.HOST_NATIVE_GUARD_WRITE_REGEX, text, re.IGNORECASE) is not None
    redirects = None
    for path_key, record in state.items():
        if not isinstance(record, dict):
            continue
        name = Path(path_key).name
        if name.casefold() not in folded:
            continue
        if redirects is None:
            redirects = _native_redirects(text, cwd)
        managed = os.path.normcase(os.path.abspath(path_key))
        hits = [append for target, append in redirects if target == managed]
        if not writes and not hits:
            continue
        if record.get("kind") == _NATIVE_CODEX_RULES:
            cards = sorted(set((record.get("rules") or {}).values()))
            return _native_reason(path_key, memspec.HOST_NATIVE_CODEX_RULES_FILENAME,
                                  memspec.LIST_JOINER.join(cards), memspec.HOST_CODEX_RULES_FIELD)
        protected = _native_protected(record)
        overwrite = any(not append for append in hits)
        for display, _path, _item, card, field, token, _wanted in protected:
            if overwrite or token.casefold() in folded:
                return _native_reason(path_key, display, card, field)
    return None


def _native_review(event, tool_name, tool_input):
    """代理直接改 Epitype 管理的宿主原生鍵時擋下：Edit／Write、Codex 的 apply_patch 封套、殼層。"""
    if not isinstance(tool_input, dict):
        return None
    cwd = event.get("cwd")
    if tool_name.casefold() in memspec.WRITE_GATE_TOOL_NAMES:
        target = _write_target(tool_input, cwd)
        if target is None or target.name.casefold() not in _NATIVE_BASENAMES:
            return None
        _additions, prospective = _prospective_write(tool_name, tool_input, target)
        return _native_file_review(target, prospective)
    pieces = []
    for key, value in tool_input.items():
        if key in memspec.ACTION_GUARD_IGNORED_FIELDS:
            continue
        if isinstance(value, str):
            pieces.append(value)
        elif isinstance(value, (list, tuple)):
            pieces.extend(item for item in value if isinstance(item, str))
    text = "\n".join(pieces)
    folded = text.casefold()
    if not any(name in folded for name in _NATIVE_BASENAMES):
        return None
    state = _native_state()
    if not state:
        return None
    if patch_envelope.BEGIN_MARKER in text:
        for entry in patch_envelope.parse(text):
            target = _resolve_target(entry.path, cwd)
            if target is None or target.name.casefold() not in _NATIVE_BASENAMES:
                continue
            if entry.op == "delete":
                deny = _native_file_review(target, None, deleting=True)
            elif entry.op == "add":
                deny = _native_file_review(target, patch_envelope.full_content(entry))
            else:
                try:
                    before = target.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeError):
                    before = None
                after = None if before is None else patch_envelope.apply_update(entry, before)
                deny = _native_file_review(target, after)
            if deny is not None:
                return deny
        return None
    if tool_name.casefold() in _NATIVE_NON_SHELL_TOOLS:
        return None
    return _native_shell_review(text, state, cwd)


def _write_check_review(event, config, target, prospective, patch, started_at, *, diagnostics=None):
    """Check a card-declared file invariant before a structured PowerShell write.

    .ps1／.psm1／.psd1 都適用同一條 PS 5.1 編碼規則。"""
    if target.suffix.casefold() not in memspec.WRITE_CHECK_PS_SUFFIXES:
        return None
    diagnostics = diagnostics if diagnostics is not None else []
    rules = []
    for vault in resolve_vaults(config, event):
        if expired(started_at):
            diagnostics.append("write-check source UNVERIFIED: verification budget exhausted before vault read")
            return None
        rules.extend(
            (vault, guard) for guard in _guards(vault, started_at, diagnostics, event=event,
                                                match_first=True)
            if guard.write_check == memspec.WRITE_CHECK_PS51_UTF8_BOM
        )
    if not rules:
        return None

    try:
        with target.open("rb") as stream:
            existing_bom = _encoding_bom(stream.read(4))
    except FileNotFoundError:
        existing_bom = False
    except OSError:
        return None  # The host write will report an unreadable target itself.

    if prospective is None and patch is not None and patch.op == "update" and existing_bom:
        # Codex apply_patch preserves an existing BOM when changing a later line.
        # patch_envelope.apply_update strips that BOM for text matching, so using
        # its result here would reject safe edits. An explicit first-line BOM
        # removal is different and must still be denied.
        removes_bom = any(
            hunk.old and hunk.old[0].startswith("\ufeff")
            and (not hunk.new or not hunk.new[0].startswith("\ufeff"))
            for hunk in (patch.hunks or ())
        )
        if not removes_bom:
            return None
        # BOM 被明確移除：移除後只要不含非 ASCII，PowerShell 5.1 仍讀得對；否則要擋。
        # apply_update 會把 BOM 連同定位一起剝掉、對不上 hunk，無法用來判這一種，故直接
        # 讀移除 BOM 後的內容來判。讀不到就當作不安全（原本有 BOM，現在拿掉了）。
        try:
            remainder = target.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            remainder = None
        if remainder is not None and remainder.isascii():
            return None
        existing_bom = False
    if prospective is None and patch is not None and patch.op == "update":
        try:
            if target.stat().st_size <= memspec.WRITE_GATE_MAX_CONTENT_BYTES:
                before = target.read_text(encoding="utf-8")
                prospective = patch_envelope.apply_update(patch, before)
        except (OSError, UnicodeError):
            pass
    if prospective is not None:
        safe = _ps_encoding_safe(prospective)
    else:
        # When the post-write text is unknown, a file already carrying a BOM
        # keeps its existing safety evidence. A BOM-less file cannot claim it.
        safe = existing_bom
    if safe:
        return None
    enforcing = [(vault, guard) for vault, guard in rules if not guard.scope_reference]
    vault, guard = (enforcing or rules)[0]
    if _guard_matched(guard, diagnostics, None):
        return None  # ADVISORY scope: its reference is now in diagnostics, nothing enforced
    reason = memspec.WRITE_CHECK_REASON.format(card=guard.card, path=target) + "\n" + guard.advice
    _best_effort_audit(
        append_gate_log, vault,
        with_session({"kind": "write_note", "rule": "write_check", "card": guard.card,
                      "target": str(target)}, event.get("session_id")),
        started_at,
    )
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": memspec.WRITE_NOTE_ENCODING_LABEL + reason}}


def _review_one_target(event, config, target, additions, prospective, started_at, session_id,
                       patch=None):
    """Collect write findings as advice without blocking an authorized update.

Unknown prospective content stays unknown. The card publisher remains responsible
for exact source validation; writing a draft is not publishing verified authority.
"""

    def oversized(text):
        return len(text.encode("utf-8", errors="replace")) > memspec.WRITE_GATE_MAX_CONTENT_BYTES

    notices = []
    if target.suffix.casefold() == ".md":
        from epitype import owner_rules
        try:
            before = target.read_text(encoding="utf-8-sig") if target.is_file() else ""
            after = prospective
            if after is None and patch is not None:
                after = patch_envelope.apply_update(patch, before)
            if after is not None or additions:
                reason = rule_provenance.claim_violation(
                    after if after is not None else "\n".join(additions),
                    event.get("transcript_path"), previous=before if after is not None else None,
                    require_citations=True, base=target.parent,
                    seconds=attribution_seconds(started_at), hard_deadline=True,
                    card_metadata=bool(config) and _vault_card_path(target, resolve_vaults(config, event)) is not None,
                    notices=notices)
                if reason:
                    notices.append(memspec.WRITE_NOTE_ATTRIBUTION_LABEL + reason)
                if event.get("stop_hook_active") or expired(started_at):
                    return None, notices
                reason = (owner_rules.violation(before, after, resolve_vaults(config, event))
                          if after is not None else None)
                if reason:
                    _best_effort_audit(append_gate_log, governance_vault(config),
                        with_session({"kind": "write_note", "rule": "owner_rulings",
                                      "target": str(target)}, session_id), started_at)
                    notices.append(memspec.WRITE_NOTE_OWNER_RULINGS_LABEL + reason)
        except (OSError, UnicodeError, ValueError) as exc:
            # Failure to reconstruct the old file does not exempt new claims.
            # Ordinary undecidable text retains the existing write-gate contract.
            candidate = prospective if isinstance(prospective, str) else "\n".join(additions)
            reason = rule_provenance.claim_violation(
                candidate, event.get("transcript_path"), require_citations=True,
                base=target.parent, seconds=attribution_seconds(started_at),
                hard_deadline=True, notices=notices)
            if reason:
                notices.append(memspec.WRITE_NOTE_ATTRIBUTION_LABEL + reason)
            if (owner_rules.BEGIN in str(prospective or "")
                    or owner_rules.BEGIN in locals().get("before", "")):
                notices.append(f"交接裁定段無法驗證：{type(exc).__name__}；本次寫入放行，待同步正本。")
    if event.get("stop_hook_active"):
        return None, notices
    if patch is not None and patch.op == "delete":
        # Owner ruling removal was checked above; deletion creates no card content.
        return None, notices
    # 編碼守衛只看 BOM 與 ASCII，成本與內容大小無關，因此不隨大檔豁免：>256KB 的
    # .ps1/.psm1/.psd1 若沒有 BOM 且含非 ASCII，PowerShell 5.1 一樣會用 cp950 讀壞。
    write_check = _write_check_review(event, config, target, prospective, patch, started_at,
                                    diagnostics=notices)
    if write_check is not None:
        notices.append(write_check["hookSpecificOutput"]["additionalContext"])

    if any(oversized(text) for text in additions):
        return None, notices
    if prospective is not None and oversized(prospective):
        prospective = None
    found = _forbidden_write(
        event, config, target, additions, prospective, started_at, notices
    )
    if found is not None:
        vault, decision_key, reason = found
        if not _write_marker(
            session_id, memspec.WRITE_GATE_FORBIDDEN_RULE, target, "\0".join(additions)
        ):
            return None, notices
        _best_effort_audit(
            _append_write_block,
            vault,
            memspec.WRITE_GATE_FORBIDDEN_RULE,
            {"decision": decision_key},
            target,
            started_at,
            session_id,
        )
        notices.append(memspec.WRITE_NOTE_FORBIDDEN_LABEL + reason[: memspec.WRITE_GATE_REASON_MAX_CHARS])

    if expired(started_at):
        return None, notices
    card = _vault_card_path(target, resolve_vaults(config, event))
    if card is None:
        return None, notices
    # 這條自己壞掉，不能連帶讓卡片契約那條也不判，所以單獨接住。
    try:
        recurrence = _recurrence_review(
            card, target, additions, prospective, patch, started_at, session_id
        )
    except Exception:
        recurrence = None
    if recurrence is not None:
        notices.append(memspec.WRITE_NOTE_RECURRENCE_LABEL + recurrence["hookSpecificOutput"]["permissionDecisionReason"])

    if prospective is None or expired(started_at):
        return None, notices
    vault, relative = card
    reason, advice = _card_review(relative, prospective,
                                 before_text=locals().get("before", ""), source_path=target)
    if reason is None:
        if advice:
            notices.append(advice)
        return None, notices
    if not _write_marker(session_id, memspec.WRITE_GATE_CARD_RULE, target, prospective):
        return None, notices
    _best_effort_audit(
        _append_write_block,
        vault,
        memspec.WRITE_GATE_CARD_RULE,
        {"card_path": relative},
        target,
        started_at,
        session_id,
    )
    notices.append(memspec.WRITE_NOTE_CARD_LABEL + reason)
    return None, notices


def _envelope_text(tool_input):
    """這次呼叫輸入裡的自由文字，接成一段拿去找封套。

    為什麼掃全部的字串欄位而不是某個指名的欄位：這台機器上的 Codex 沒有 `apply_patch`
    工具，封套是夾在 `exec` 的程式文字裡送進來的，而那次呼叫進到這裡時工具名已經變成
    `Bash`。欄位名不可靠，封套的邊界才可靠。"""
    pieces = []
    for value in tool_input.values():
        if isinstance(value, str):
            pieces.append(value)
        elif isinstance(value, (list, tuple)):
            pieces.extend(item for item in value if isinstance(item, str))
    return "\n".join(pieces)


def _envelope_review(event, tool_input, config, started_at):
    """(deny value, advice lines) for the `*** Begin Patch` envelopes in this call.

    docs/FAILURE_MODES.md §11 kept diff-shaped writes out of `WRITE_GATE_TOOL_NAMES`
    because a diff's context and removed lines would match `forbidden` patterns the
    write never adds. That reason stands, and the tool-name list is unchanged: what
    is judged here is only the added lines, per target file, so the objection no
    longer applies. Add File carries its whole content and is judged by both rules;
    Update File cannot be reconstructed from a patch, so the card contract does not
    judge it at all. A file that cannot be judged — unresolvable path, oversized
    content, a malformed envelope — is left unknown while the rest are still judged,
    because one undecidable file must neither pass nor block the others."""
    text = _envelope_text(tool_input)
    if patch_envelope.BEGIN_MARKER not in text:
        return None, []
    cwd = event.get("cwd")
    session_id = event.get("session_id")
    notices = []
    for entry in patch_envelope.parse(text):
        if expired(started_at):
            break
        target = _resolve_target(entry.path, cwd)
        if target is None:
            continue
        if entry.op == "delete":
            if target.suffix.casefold() == ".md":
                deny, advice = _review_one_target(
                    event, config, target, [], "", started_at, session_id, patch=entry)
                # A deletion's findings are advice too; dropping them hides the removal.
                notices.extend(advice)
                if deny is not None:
                    return deny, []
            continue
        if not entry.additions and target.suffix.casefold() not in (".ps1", ".md"):
            continue
        deny, advice = _review_one_target(
            event, config, target, list(entry.additions),
            patch_envelope.full_content(entry), started_at, session_id,
            patch=entry,
        )
        notices.extend(advice)
        if deny is not None:
            return deny, []
    return None, notices


def _literal_python_writes(command, cwd):
    """Recognize bounded literal Markdown writes, never execute the submitted code.

    Path literals, aliases, UTF-8 read_text, string addition and replace preserve
    an exact prospective document. Dynamic control flow stays outside this shape.
    A read/query containing authority words is not a file write.
    """
    import ast
    import shlex

    if len(command.encode('utf-8')) > memspec.WRITE_GATE_MAX_CONTENT_BYTES:
        return []
    sources = [match[3] for match in re.finditer(
        r'(?m)^\s*python(?:3(?:\.\d+)?)?(?:\.exe)?\s+-\s+<<\s*([\x27"]?)(\w+)\1\s*\n'
        r'([\s\S]*?)^\2\s*$', command)]
    try:
        words = shlex.split(command)
        for i, word in enumerate(words[:-2]):
            if re.fullmatch(r'python(?:3(?:\.\d+)?)?(?:\.exe)?', Path(word).name, re.IGNORECASE) and words[i+1] == '-c':
                sources.append(words[i+2])
    except ValueError:
        pass
    writes = []
    for source in sources:
        if len(source.encode('utf-8')) > memspec.WRITE_GATE_MAX_CONTENT_BYTES:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue
        values = {}
        constructors, modules = set(), set()

        def value(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, int)):
                return node.value
            if isinstance(node, ast.Name):
                return values.get(node.id)
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
                operand = value(node.operand)
                return -operand if isinstance(operand, int) else None
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left, right = value(node.left), value(node.right)
                if isinstance(left, str) and isinstance(right, str):
                    joined = left+right
                    return joined if len(joined.encode('utf-8')) <= memspec.WRITE_GATE_MAX_CONTENT_BYTES else None
            if not isinstance(node, ast.Call):
                return None
            name = node.func.id if isinstance(node.func, ast.Name) else None
            path_constructor = (name in constructors or isinstance(node.func, ast.Attribute)
                and node.func.attr == 'Path' and isinstance(node.func.value, ast.Name)
                and node.func.value.id in modules)
            if path_constructor and len(node.args) == 1:
                path = value(node.args[0])
                return _resolve_target(path, cwd) if isinstance(path, str) else None
            if isinstance(node.func, ast.Attribute):
                owner = value(node.func.value)
                if isinstance(owner, Path) and owner.suffix.casefold() == '.md' and node.func.attr == 'read_text':
                    try:
                        if owner.stat().st_size <= memspec.WRITE_GATE_MAX_CONTENT_BYTES:
                            return owner.read_text(encoding='utf-8-sig')
                    except (OSError, UnicodeError):
                        pass
                if isinstance(owner, str) and node.func.attr == 'replace' and len(node.args) in (2, 3):
                    args = [value(arg) for arg in node.args]
                    if all(isinstance(arg, str) for arg in args[:2]) and (len(args) == 2 or isinstance(args[2], int)):
                        # Bounding the result avoids a literal expansion consuming the hook.
                        count = owner.count(args[0]) if len(args) == 2 or args[2] < 0 else min(owner.count(args[0]), args[2])
                        growth = max(0, len(args[1].encode('utf-8'))-len(args[0].encode('utf-8')))
                        if len(owner.encode('utf-8'))+count*growth <= memspec.WRITE_GATE_MAX_CONTENT_BYTES:
                            return owner.replace(*args)
            return None

        for statement in tree.body:
            if isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
                                      ast.With, ast.AsyncWith, ast.Match)):
                # A conditional can rebind either the target or imported constructors.
                # Never carry assumed literal identities across unknown control flow.
                break
            if isinstance(statement, ast.ImportFrom) and statement.module == 'pathlib':
                constructors.update(alias.asname or alias.name for alias in statement.names if alias.name == 'Path')
            if isinstance(statement, ast.Import):
                modules.update(alias.asname or alias.name for alias in statement.names if alias.name == 'pathlib')
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                constructors.discard(statement.name)
                modules.discard(statement.name)
            if isinstance(statement, ast.Assign):
                computed = value(statement.value)
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        values[target.id] = computed
                        constructors.discard(target.id)
                        modules.discard(target.id)
            if not isinstance(statement, (ast.Expr, ast.Assign)) or not isinstance(statement.value, ast.Call):
                continue
            call = statement.value
            if not isinstance(call.func, ast.Attribute) or call.func.attr != 'write_text' or not call.args:
                continue
            target = value(call.func.value)
            if not isinstance(target, Path) or target.suffix.casefold() != '.md':
                continue
            content = value(call.args[0])
            additions = ([content] if isinstance(content, str) else [node.value for node in ast.walk(call.args[0])
                if isinstance(node, ast.Constant) and isinstance(node.value, str)])
            writes.append((target, additions, content if isinstance(content, str) else None))
    return writes


def _python_write_review(event, tool_input, config, started_at):
    notices = []
    for target, additions, prospective in _literal_python_writes(_envelope_text(tool_input), event.get('cwd')):
        deny, advice = _review_one_target(event, config, target, additions, prospective,
                                         started_at, event.get('session_id'))
        notices.extend(advice)
        if deny is not None:
            return deny, []
    return None, notices


def _write_review(event, tool_name, tool_input, config, started_at):
    """(deny value, advice lines) for a call about to write file content.

    A named write tool declares its target and its new text in structured fields.
    Other calls are scanned for patch envelopes and literal Python Markdown writes.
    A non-file tool without either recognized write shape
    proceeds untouched. Source admission also runs during Stop rewrites; existing
    ordinary card checks retain their re-entry exemption. Opaque bash
    redirections never reach this gate at all (docs/FAILURE_MODES.md §11)."""
    if not isinstance(tool_input, dict):
        return None, []
    if tool_name.casefold() in memspec.WRITE_GATE_TOOL_NAMES:
        target = _write_target(tool_input, event.get("cwd"))
        if target is None:
            return None, []
        additions, prospective = _prospective_write(tool_name, tool_input, target)
        return _review_one_target(
            event, config, target, additions, prospective,
            started_at, event.get("session_id"),
        )
    if expired(started_at):
        return None, []
    deny, advice = _envelope_review(event, tool_input, config, started_at)
    if deny is not None:
        return deny, advice
    python_deny, python_advice = _python_write_review(event, tool_input, config, started_at)
    return python_deny, advice+python_advice


def _context_meter_line(event, config, started_at):
    """(用量計那一行, 標記目錄, 標記名, 用量)，或 None。

    標記放在喚回標記目錄：PreCompact 清喚回標記時一起清掉，壓縮後重新武裝。
    子代理的呼叫由 context_meter.notice 擋掉——提醒被子代理吃掉，主線就永遠收不到。"""
    if config is None or expired(started_at):
        return None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        directory = recall_marker_directory(session_id)
        found = context_meter.notice(event, governance_vault(config), directory)
    except Exception:
        return None
    if found is None:
        return None
    return found[0], directory, found[1], found[2]


def _midturn_scan(lines, checked):
    """(還沒量過的文字段 [(id, 文字)] 依時間先後, 是否已碰到停止點)。

    量過的段永遠在前、沒量過的在後，所以倒著走碰到第一個量過的段就能停。
    工具回傳記在 user 那一側但不結束回合；isMeta／壓縮摘要也不是真人提問。"""
    found = []
    for raw in reversed(lines):
        # 先用位元組篩掉不可能相關的列再解析：工具回傳列可達數 MB。文字裡的引號一定被跳脫，
        # 所以帶引號的 "tool_result" 只會是結構上的型別值，而它在列首附近。
        if b'"text"' not in raw and b'"user"' not in raw:
            continue
        if b'"tool_result"' in raw[:4096]:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        if isinstance(row.get("payload"), dict):
            # Codex rollout：這裡只認 Claude 的紀錄形狀；不停下來的話每次都要讀滿上限。
            found.reverse()
            return found, True
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if row.get("type") == "user":
            if row.get("isMeta") or row.get("isCompactSummary"):
                continue
            if isinstance(content, list) and any(
                    isinstance(block, dict) and block.get("type") == "tool_result"
                    for block in content):
                continue
            if isinstance(content, (str, list)):
                found.reverse()
                return found, True
            continue
        if row.get("type") != "assistant" or not isinstance(content, list):
            continue
        row_id = str(row.get("uuid") or "")
        for index in range(len(content) - 1, -1, -1):
            block = content[index]
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str) or not text.strip() or not row_id:
                continue
            block_id = f"{row_id}:{index}"
            if block_id in checked:
                found.reverse()
                return found, True
            found.append((block_id, text))
    found.reverse()
    return found, False


def _midturn_texts(transcript_path, checked, started_at):
    """這一回合還沒量過的助理文字段。只讀檔尾：對話檔可以超過 150 MB。"""
    path = Path(str(transcript_path))
    size = path.stat().st_size
    span = memspec.CONTEXT_METER_TAIL_FIRST_BYTES
    with path.open("rb") as stream:
        while True:
            start = max(0, size - span)
            stream.seek(start)
            lines = stream.read(size - start).split(b"\n")
            if start:
                lines = lines[1:]
            found, stopped = _midturn_scan(lines, checked)
            if (stopped or not start or span >= memspec.STOP_GATE_TURN_TAIL_BYTES
                    or expired(started_at)):
                return found
            span = min(span * 4, memspec.STOP_GATE_TURN_TAIL_BYTES)


def _midturn_state(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    if not isinstance(state, dict):
        return [], []
    return ([str(item) for item in state.get("checked") or ()],
            [str(item) for item in state.get("pending") or ()])


def _save_midturn_state(path, checked, pending):
    # 並行的工具呼叫各有一個 hook 行程：先寫暫存再換名，讀的一方不會讀到半個檔。
    keep = memspec.TURN_LANGUAGE_MIDTURN_KEEP_IDS
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"checked": checked[-keep:], "pending": pending[-keep:]}),
                         encoding="utf-8")
    os.replace(temporary, path)


def _owner_language_decisions(config, event, started_at):
    """回合結束閘認得的 owner_language 裁定，卡片照它的規矩從檔案重讀。

    找卡沿用回合結束閘自己的裁定快取（它每回合結束都更新）：整庫盤點加輪替重驗實測一次
    約 450 ms，而這裡在工具呼叫之前跑。快取不在或版本不合才退回整庫載入。"""
    import stop_gate
    from epitype import tool_sequence

    found = []
    for vault in stop_gate._vaults(config, event):
        vault = Path(vault).resolve()
        _manifest, cached, _cursor = stop_gate._read_cache(
            vault / memspec.FTS_INDEX_DIRECTORY / stop_gate._DECISION_CACHE_FILENAME)
        if cached:
            decisions = [
                stop_gate._decision_from_ruling(vault, card_path, ruling, event=event, defects=[])
                for card_path, ruling in sorted(cached.items())
                if isinstance(ruling, dict) and ruling.get(stop_gate._KEY)
                and ruling.get(memspec.TURN_CHECK_FIELD) == memspec.TURN_CHECK_OWNER_LANGUAGE]
        else:
            decisions = stop_gate._decisions(vault, started_at, [], event=event)
        found.extend(
            decision for decision in decisions
            if decision is not None
            and decision.turn_check == memspec.TURN_CHECK_OWNER_LANGUAGE
            and decision.applies_to != memspec.APPLIES_TO_WRITE
            and tool_sequence.applies(decision.audience, event))
    return found


def _midturn_language_line(event, config, started_at, fits):
    """工具之間寫給 owner 的文字不是卡片指定的語言，回一行提醒；其餘一律 None。

    只提醒、不擋。量法、門檻與目標語言都取自回合結束閘的 owner_language 卡。每段文字最多
    提醒一次；外文字母還不夠判的段留著，跟下一段合起來量。任何例外都不輸出。"""
    try:
        if (config is None or started_at is None or expired(started_at)
                or event.get("agent_id") or event.get("agentId")):
            return None
        session_id = event.get("session_id")
        transcript = event.get("transcript_path")
        if not (isinstance(session_id, str) and session_id
                and isinstance(transcript, str) and transcript):
            return None
        directory = notice_marker_directory(session_id)
        state_path = directory / memspec.TURN_LANGUAGE_MIDTURN_STATE_FILE
        checked, pending = _midturn_state(state_path)
        found = _midturn_texts(transcript, set(checked), started_at)
        ids = [block_id for block_id, _text in found]
        if not ids or set(ids) <= set(pending) or expired(started_at):
            return None
        import stop_gate

        measured, chosen = None, None
        for decision in _owner_language_decisions(config, event, started_at):
            measured = stop_gate._owner_language_measure(
                decision, "\n\n".join(text for _id, text in found), [])
            if measured is not None:
                chosen = decision
                break
        if expired(started_at):
            return None
        directory.mkdir(parents=True, exist_ok=True)
        if measured is None:
            _save_midturn_state(state_path, checked + ids, [])
            return None
        share, foreign, minimum, language = measured
        if share >= minimum:
            _save_midturn_state(state_path, checked + ids, [])
            return None
        if foreign < memspec.TURN_LANGUAGE_MIN_FOREIGN_LETTERS:
            _save_midturn_state(state_path, checked, ids)
            return None
        _save_midturn_state(state_path, checked + ids, [])
        template = memspec.TURN_LANGUAGE_MIDTURN_NOTICE
        room = memspec.TURN_LANGUAGE_MIDTURN_MAX_CHARS - len(
            template.format(language=language, share=share, advice=""))
        advice = " ".join(str(chosen.advice or "").split())
        if len(advice) > room:
            advice = advice[:room - 1] + "…" if room > 1 else ""
        line = template.format(language=language, share=share, advice=advice)
        if not fits(line) or not _notice_marker(session_id, "midturn-language:" + ids[0]):
            return None
        return line
    except Exception:
        return None


def _finish_advice(claims, emitted):
    import hashlib
    from epitype import rule_context
    for session, marker, path, vault in claims:
        try:
            if emitted:
                rule_context.record(vault, session, path.relative_to(vault))
            else:
                digest = hashlib.sha256(marker.encode('utf-8')).hexdigest()[:16]
                (notice_marker_directory(session) / digest).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    claims.clear()


def _notice_target(event):
    """What this call acts on, for the same-session notice dedupe.

    A finding repeated on another file or another call target is new information;
    keying the dedupe on the words alone hid it from the second target."""
    tool = str(event.get("tool_name") or "")
    fields = event.get("tool_input")
    fields = fields if isinstance(fields, dict) else {}
    for name in memspec.NOTICE_TARGET_FIELDS:
        value = fields.get(name)
        if value not in (None, "", [], ()):
            return tool + "\0" + name + "=" + json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return tool + "\0" + json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)


def _allow_context(event, notices=(), config=None, started_at=None, meter_claims=None, advisories=(), advice_claims=None):
    """Context for a call the gate lets through: the write gate's own advice and
    the rulings whose `forbidden` pattern could not be compiled, each named once
    per session — a ruling that silently stopped being enforced is the failure the
    gate exists to prevent.

    The context meter's line rides last and only when the whole context still fits
    the budget. It is not added here: it goes into `meter_claims` as
    (directory, marker, tokens, line), and main claims the marker right before the
    output and adds the line only if this process won the claim — Codex runs several
    tool calls at once, and every one of their hooks sees "not said yet". A claim
    whose output then fails is released, so the line is tried again on the next call.
    `meter_claims` is None for in-process callers, which get no meter line."""
    lines = []
    session_id = event.get("session_id")
    target = _notice_target(event)
    for notice in list(dict.fromkeys(notices))[: memspec.GATE_DEFECT_MAX_LINES]:
        if _notice_marker(session_id, notice + "\0" + target):
            lines.append(notice)
    shown = 0
    for path, card, excerpt in advisories:
        if shown == 2:
            break
        line = memspec.ACTION_GUARD_ADVISORY_LINE.format(card=card, excerpt=excerpt)
        marker = "tool-advice:" + str(path.resolve()) + "\0" + target
        if (payload_fits("PreToolUse", "\n".join([*lines, line]),
                         config[memspec.CONFIG_BUDGET_BYTES_FIELD])
                and _notice_marker(session_id, marker)):
            lines.append(line)
            shown += 1
            try:
                from epitype import rule_context
                for vault in resolve_vaults(config, event):
                    if path.resolve().is_relative_to(vault.resolve()):
                        if advice_claims is None:
                            rule_context.record(vault, session_id, path.relative_to(vault))
                        else:
                            advice_claims.append((session_id, marker, path, vault))
                        break
            except (OSError, ValueError):
                pass
    try:
        if config is not None and started_at is not None and not expired(started_at):
            lifecycle_path = control_lifecycle.database_path(config_path())
            lifecycle_note = control_lifecycle.reminder(
                event, lifecycle_path, "tool", claim=False
            )
            if lifecycle_note:
                candidate = "\n".join([*lines, lifecycle_note])
                if payload_fits("PreToolUse", candidate,
                                config[memspec.CONFIG_BUDGET_BYTES_FIELD]):
                    claimed = control_lifecycle.reminder(event, lifecycle_path, "tool")
                    if claimed:
                        lines.append(claimed)
                else:
                    print("Epitype 瀏覽器收尾提醒超出本次 hook 預算；下次工具呼叫仍會重試。",
                          file=sys.stderr)
    except Exception as exc:
        if config_path().exists():
            print(memspec.GATE_DEGRADED_NOTICE.format(
                gate="control lifecycle", reason=type(exc).__name__), file=sys.stderr)
    if meter_claims is not None:
        language_line = _midturn_language_line(
            event, config, started_at,
            lambda line: payload_fits("PreToolUse", "\n".join([*lines, line]),
                                      config[memspec.CONFIG_BUDGET_BYTES_FIELD]))
        if language_line:
            lines.append(language_line)
        meter = _context_meter_line(event, config, started_at)
        if meter is not None and payload_fits(
                "PreToolUse", "\n".join([*lines, meter[0]]),
                config[memspec.CONFIG_BUDGET_BYTES_FIELD]):
            meter_claims.append((meter[1], meter[2], meter[3], meter[0]))
    if not lines:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(lines)}}


def _read_state_path(vault, session_id):
    session = "".join(char for char in str(session_id or "") if char.isalnum() or char in "-_")
    if not session:
        return None
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY / "reads" / (session + ".json")


def _waste_review(event, tool_name, tool_input, config, started_at):
    """Reading volume/repetition gives advice without overriding the current task."""
    # 用到才載入：hashlib 要 6 ms，絕大多數呼叫走不到這裡。
    import hashlib

    if tool_name.casefold() not in memspec.READ_WASTE_TOOLS:
        return None, None
    if not isinstance(tool_input, dict):
        return None, None
    raw = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    try:
        target = Path(raw)
        info = target.stat()
    except OSError:
        return None, None

    # 有界讀取不只有 offset／limit 一種寫法：PDF 給的是頁碼範圍，筆記本給的是格子。
    # 2026-09-19 這道閘上線半小時就誤擋了一次帶頁碼範圍的 PDF 讀取——擋的理由是
    # 「整檔拉進來」，而那次呼叫本來就只要六頁。
    bounds = [tool_input.get(field) for field in memspec.READ_WASTE_BOUND_FIELDS]
    bounded = any(str(value or "").strip() for value in bounds)
    if info.st_size >= memspec.READ_WASTE_BIG_FILE_BYTES and not bounded:
        return None, memspec.READ_WASTE_LARGE_NOTICE.format(path=raw, size=info.st_size)

    # 「同一段」的身分要含全部的範圍欄位，不能只有 offset／limit。2026-09-20 Codex
    # 審查抓到：同一份 PDF 連續讀 1-3、4-6、7-9 頁，第三次會被擋，理由還錯稱這一段
    # 已經讀過——那三次讀的根本是不同頁。
    digest = hashlib.sha256(
        "|".join(str(part) for part in (
            [os.path.normcase(os.path.abspath(raw)), info.st_mtime_ns, info.st_size] + bounds
        )).encode("utf-8")
    ).hexdigest()[:16]

    vault = governance_vault(config, for_write=True)
    state_path = _read_state_path(vault, event.get("session_id"))
    if state_path is None:
        return None, None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    count = int(state.get(digest, 0)) + 1
    state[digest] = count
    if len(state) > memspec.READ_WASTE_STATE_MAX_ENTRIES:
        state = dict(list(state.items())[-memspec.READ_WASTE_STATE_MAX_ENTRIES:])
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        staging = state_path.with_name("." + state_path.name + ".tmp-%d" % os.getpid())
        staging.write_text(json.dumps(state), encoding="utf-8")
        os.replace(staging, state_path)
    except OSError:
        pass

    if count > 1 + memspec.READ_WASTE_FREE_REPEATS:
        return None, memspec.READ_WASTE_RANGE_COUNT_NOTICE.format(path=raw, count=count)
    if count > 1:
        return None, memspec.READ_WASTE_REPEAT_NOTICE.format(count=count, path=raw)
    return None, None


def _handle_review(event, started_at, defects=None, meter_claims=None, advice_claims=None):
    """Review source-bound dispatches, writes, then existing action guards."""
    defects = [] if defects is None else defects
    # Dispatch prose is another authority-writing surface. Do not inspect read
    # queries or arbitrary shell strings as if they were new instructions.
    # Findings ride in this call's additionalContext: stderr never reaches the model.
    dispatch_notes = []
    dispatch_name = str(event.get("tool_name", "")).casefold()
    if any(name in dispatch_name for name in ("task", "agent", "send_message")):
        dispatch = event.get("tool_input", {})
        if isinstance(dispatch, dict):
            for key in ("prompt", "message", "instructions"):
                found = []
                reason = rule_provenance.claim_violation(
                    dispatch.get(key), event.get("transcript_path"), require_citations=True,
                    base=event.get("cwd"), seconds=attribution_seconds(started_at),
                    hard_deadline=True, notices=found)
                dispatch_notes.extend(memspec.DISPATCH_NOTE_ATTRIBUTION_LABEL + line
                                      for line in (*found, *filter(None, (reason,))))
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    config = load_config(started_at)
    if config is None:
        # Configuration expiry cannot silently waive a recognized Markdown
        # claim. Reuse reconstruction/admission; all other write guards keep
        # their expired contract. This path never runs card or state mutations.
        # Past the deadline main emits no context, so stderr is the only channel left.
        defects.extend(dispatch_notes)
        try:
            value, notices = _write_review(event, tool_name, event.get("tool_input"), {}, started_at)
            defects.extend(notices)
            return value
        except Exception as exc:
            defects.append(f"write admission UNVERIFIED: expired review failed ({type(exc).__name__}); "
                           "next check: inspect this exact tool call")
            return None
    advisories = []
    try:
        guard_value = _guard_review(
            event, tool_name, event.get("tool_input"), config, started_at, defects, advisories
        )
        for step_tool, step_input in memspec.nested_tool_calls(tool_name, event.get("tool_input")):
            if guard_value is not None:
                break
            guard_value = _guard_review(event, step_tool, step_input, config, started_at, defects, advisories)
    except Exception as exc:
        defects.append(f"guard source UNVERIFIED: review failed ({type(exc).__name__}); next check: inspect this exact tool call")
        guard_value = None
    if guard_value is not None:
        return guard_value
    try:
        native_value = _native_review(event, tool_name, event.get("tool_input"))
    except Exception:
        native_value = None
    if native_value is not None:
        _best_effort_audit(
            append_gate_log, governance_vault(config, for_write=True),
            with_session({"kind": memspec.WRITE_GATE_LOG_KIND, "rule": "host_native",
                          "tool": tool_name}, event.get("session_id")), started_at)
        return native_value
    try:
        write_value, notices = _write_review(
            event, tool_name, event.get("tool_input"), config, started_at
        )
    except Exception as exc:
        write_value, notices = None, ()
        defects.append(f"write admission UNVERIFIED: review failed ({type(exc).__name__}); "
                       "next check: inspect this exact tool call")
    if write_value is not None:
        return write_value
    # Authority uncertainty is labelled and shown in this call's context.
    notices = tuple(dispatch_notes) + tuple(
        memspec.WRITE_NOTE_ATTRIBUTION_LABEL + notice if notice.startswith('[owner-attribution]')
        else notice for notice in notices)
    try:
        waste_value, waste_notice = _waste_review(
            event, tool_name, event.get("tool_input"), config, started_at
        )
    except Exception:
        waste_value, waste_notice = None, None
    if waste_value is not None:
        return waste_value
    if waste_notice:
        notices = tuple(notices) + (waste_notice,)
    control_value, control_notice = _control_review(event, tool_name, started_at)
    if control_value is not None:
        return control_value
    if control_notice:
        notices = tuple(notices) + (control_notice,)
    return _allow_context(event, notices, config, started_at, meter_claims, advisories, advice_claims)


def _control_review(event, tool_name, started_at):
    """Record keep declarations and let their CLI deliver a normal result."""
    if expired(started_at):
        return None, None
    try:
        path = control_lifecycle.database_path(config_path())
        request = control_lifecycle.keep_request(event)
        if request is not None:
            accepted, message = control_lifecycle.declare_keep(event, path, *request)
            return None, message
    except Exception as exc:
        if config_path().exists():
            print(memspec.GATE_DEGRADED_NOTICE.format(
                gate="control lifecycle", reason=type(exc).__name__), file=sys.stderr)
    return None, None


def _prepare_input(event, started_at, notices, repairs=None):
    """Repair deterministic inputs, preserving the host's normal permission flow.

`repairs` collects (file name, notice) for each BOM added, so a repair the host
cannot receive can be withdrawn and named instead."""
    payload = event.get("tool_input")
    if not isinstance(payload, dict):
        return None
    name = str(event.get("tool_name", ""))
    calls = [(name, payload), *memspec.nested_tool_calls(name, payload)]
    for tool, fields in calls:
        if tool == control_lifecycle.CHROME + "navigate":
            target, reason = control_lifecycle.navigation_target(
                event, control_lifecycle.database_path(config_path()), fields.get("tabId"), fields.get("url"))
            if reason:
                if reason.startswith(memspec.LIFECYCLE_SAFETY_PREFIX):
                    return _deny_value(reason)
                notices.append(reason)
            if fields.get("tabId") is None and target is not None:
                fields["tabId"] = int(target) if target.isdigit() else target
                notices.append(f"已自動選本場空閒分頁 {target} 導頁。")
        if tool.casefold() in memspec.WRITE_GATE_CONTENT_TOOLS:
            target = _write_target(fields, event.get("cwd"))
            text = fields.get(memspec.WRITE_GATE_CONTENT_FIELD)
            if (target is not None and target.suffix.casefold() in memspec.WRITE_CHECK_PS_SUFFIXES
                    and isinstance(text, str) and not _ps_encoding_safe(text)):
                fields[memspec.WRITE_GATE_CONTENT_FIELD] = "\ufeff" + text
                notice = f"已為 {target.name} 自動保留 PowerShell 5.1 可辨識的 UTF-8 BOM。"
                notices.append(notice)
                if repairs is not None:
                    repairs.append((target.name, notice))
                _verify_ps_syntax(fields[memspec.WRITE_GATE_CONTENT_FIELD], started_at, notices)
    return None


def _verify_ps_syntax(text, started_at, notices):
    """Parse a temporary script with Windows PowerShell; never execute its content."""
    if os.name != "nt" or expired(started_at):
        notices.append("PowerShell 解析未驗證：本次環境或查核時間不足；寫入照常放行。")
        return
    handle, name = tempfile.mkstemp(prefix="epitype-ps-parse-", suffix=".ps1")
    staging = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        quoted = str(staging).replace("'", "''")
        command = "$tokens=$null; $errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('" + quoted + "',[ref]$tokens,[ref]$errors); if($errors.Count){exit 1}"
        checked = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, timeout=max(.05, min(1.0, remaining_seconds(started_at))), check=False)
        notices.append("已用 Windows PowerShell 解析寫入內容。" if checked.returncode == 0
                       else "PowerShell 解析發現語法問題，請修正文法；本次寫入照常放行。")
    except (OSError, subprocess.TimeoutExpired) as exc:
        notices.append(f"PowerShell 解析未驗證（{type(exc).__name__}）；本次寫入照常放行。")
    finally:
        staging.unlink(missing_ok=True)


def _handle(event, started_at, defects=None, meter_claims=None, advice_claims=None):
    notices = [] if defects is None else defects
    prepared = deepcopy(event)
    repairs = []
    denied = _prepare_input(prepared, started_at, notices, repairs)
    if denied is not None:
        return denied
    changed = prepared.get("tool_input") != event.get("tool_input")
    value = _handle_review(prepared, started_at, notices, meter_claims, advice_claims)
    if changed and (value or {}).get("hookSpecificOutput", {}).get("permissionDecision") != "deny":
        if value is None:
            value = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
        # updatedInput replaces the whole input. Omitting permissionDecision preserves native checks.
        value["hookSpecificOutput"]["updatedInput"] = prepared["tool_input"]
        if repairs and len(encode_payload(value).encode("utf-8")) > memspec.HOOK_MAX_OUTPUT_BYTES:
            # emit() refuses a payload over the hard limit, which would drop the repair
            # and every note with it while the unrepaired write goes ahead. The review
            # above saw the repaired text, so the missing BOM is named here instead.
            del value["hookSpecificOutput"]["updatedInput"]
            for name, notice in repairs:
                notices[:] = [line for line in notices if line != notice]
                notices.append(f"未能為 {name} 自動補 BOM：補上後的輸入超過 hook 輸出上限。")
                value = append_context(value, "PreToolUse", memspec.WRITE_NOTE_ENCODING_LABEL
                                       + memspec.WRITE_NOTE_BOM_TOO_LARGE.format(name=name))
    return value


def _selftest():
    checks = []
    from epitype import card_lint
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    from rule_source_fixtures import source_bound_card
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-gate-") as temp_dir:
            root = Path(temp_dir).resolve()
            vault = root / "vault"
            vault.mkdir()
            # 2026-09-09 U-J：卡片就算還寫著 trigger，這道閘也不再讀它、不再攔任何動作。
            (vault / "retired-trigger.md").write_text(
                "---\n"
                "name: synthetic-safety\n"
                "trigger:\n"
                "  tool: ^Bash$\n"
                "  input: remove target\n"
                "advice: Use the read-only alternative.\n"
                "---\n"
                "Synthetic card body.\n",
                encoding="utf-8",
            )
            config = root / "config.json"
            write_config(config, [vault])
            log_path = vault / memspec.GATE_LOG_FILENAME

            retired = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}},
                config,
            )
            checks.append((
                "a card that still declares trigger denies nothing and writes no audit row",
                retired.returncode == 0
                and not retired.stdout.strip()
                and not retired.stderr.strip()
                and not log_path.exists(),
            ))

            # 這四條原本各由一張 trigger 卡攔下；現在一律由宿主原生規則負責
            # （Claude permissions.deny／Codex execpolicy），本 hook 不表意見。
            host_rule_cases = (
                ("heredoc backslash", "python - <<'PY'\np = 'C:\\Users\\x'\nPY\n"),
                ("host process", "taskkill /IM claude.exe /F"),
                ("destructive git", "git reset --hard"),
                ("credential read", "Get-Content .env"),
            )
            host_rule_silent = True
            for _name, command in host_rule_cases:
                result = run_synthetic(
                    Path(__file__),
                    {"tool_name": "Bash", "tool_input": {"command": command}},
                    config,
                )
                host_rule_silent = host_rule_silent and result.returncode == 0 and not result.stdout.strip()
            checks.append((
                "shell command classification is gone: the four retired scars produce no decision",
                host_rule_silent and not log_path.exists(),
            ))

            broken_card = vault / "broken-trigger.md"
            broken_card.write_text(
                "---\nname: broken-trigger\ntrigger:\n  tool: [\n  input: remove\n"
                "advice: never used.\n---\n",
                encoding="utf-8",
            )
            broken = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}, "session_id": uuid.uuid4().hex},
                config,
            )
            checks.append((
                "an unparsable card is no longer a gate defect: nothing is denied, named, or audited",
                broken.returncode == 0
                and not broken.stdout.strip()
                and not log_path.exists(),
            ))

            rotate_vault = root / "rotate-vault"
            rotate_vault.mkdir()
            rotate_log = rotate_vault / memspec.GATE_LOG_FILENAME
            rotate_log.write_text("x" * (GATE_LOG_MAX_BYTES + 1024) + "\n", encoding="utf-8")
            append_gate_log(rotate_vault, {"card": "rotate-check"}, time.monotonic())
            rotated_path = rotate_vault / (memspec.GATE_LOG_FILENAME + ".1")
            rotated_rows = [
                json.loads(line)
                for line in rotate_log.read_text(encoding="utf-8").splitlines()
                if line
            ]
            checks.append(
                (
                    "a gate log past GATE_LOG_MAX_BYTES rotates to .1 before the next row lands",
                    rotated_path.is_file()
                    and rotated_path.stat().st_size > GATE_LOG_MAX_BYTES
                    and len(rotated_rows) == 1
                    and rotated_rows[0].get("card") == "rotate-check",
                )
            )

            # The shared `forbidden` pattern validator (Stop gate and rule A both
            # compile through it): a pattern that can backtrack exponentially would
            # hang the hook, and a hung hook is a bypass.
            def regex_accepted(pattern):
                try:
                    compile_bounded_regex(pattern)
                except (ValueError, re.error):
                    return False
                return True

            checks.append((
                "adjacent repetitions, bounded groups, and long patterns are accepted",
                regex_accepted(r"git\s+add\s+(?:-A|--all|\.)\s*$")
                and regex_accepted(r"rm(?:\s+-\w+)?\s+-rf\b")
                and regex_accepted(r"(?:\s+-\w+(?:\s+\S+)?)?\s*>")
                and regex_accepted("|".join(f"(?:token{index}\\s*)" for index in range(60))),
            ))
            checks.append((
                "repeated alternations, nested repetitions, and backreferences are rejected",
                not regex_accepted(r"(?:ab|cd)+$")
                and not regex_accepted(r"(?:\s+\S+)*x")
                and not regex_accepted(r"(a)\1")
                and not regex_accepted("a" * (memspec.FORBIDDEN_REGEX_MAX_CHARS + 1)),
            ))

            # Owner 2026-09-09 (§30): the narration meter is gone. A transcript
            # whose turn is full of mid-run prose must produce no context at all.
            def transcript_rows(*rows):
                path = root / f"transcript-{uuid.uuid4().hex}.jsonl"
                path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
                return os.fspath(path)

            def assistant(kind, value):
                block = {"type": "text", "text": value} if kind == "text" else {"type": "tool_use", "id": value, "name": "Bash", "input": {}}
                return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}

            prompt_row = {"type": "user", "message": {"role": "user", "content": "修一下"}}
            result_row = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]}}
            narrated = transcript_rows(
                prompt_row,
                assistant("text", "先看檔案。"),
                assistant("tool_use", "t1"),
                result_row,
                assistant("text", "那次失敗是我的路徑錯，改成 C:/… 重跑一次。"),
            )
            quiet = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "synthetic.txt"}, "transcript_path": narrated, "session_id": uuid.uuid4().hex},
                config,
            )
            checks.append((
                "prose between tool calls adds no context: the narration meter is gone",
                quiet.returncode == 0 and not quiet.stdout.strip() and not quiet.stderr.strip(),
            ))

            # 這兩項在自己的暫存根裡跑。原本寫死使用者真正的 `%TEMP%`，於是併行跑整套
            # 時別的行程會在「拿掉時間戳」與「呼叫」之間把時間戳重新建起來，清掃就不會
            # 發生，這一項假敗（2026-09-22 實測：8 次跑出現 3 次紅）。而且每跑一次都在
            # 使用者的暫存目錄留下 `aged-*`／`sweep-*`（同日實測堆了 2,302 個項目）。
            # 隔離之後驗的還是同一段清掃程式：`temp_root()` 每次呼叫都重讀環境變數。
            with tempfile.TemporaryDirectory(prefix="epitype-sweep-") as sweep_home:
                saved_temp = {name: os.environ.get(name) for name in ("TMPDIR", "TEMP", "TMP")}
                for name in saved_temp:
                    os.environ[name] = sweep_home
                try:
                    sweep_root = Path(sweep_home) / memspec.NOTICE_MARKER_DIRECTORY
                    aged_session = sweep_root / ("aged-" + uuid.uuid4().hex)
                    aged_session.mkdir(parents=True, exist_ok=True)
                    aged_marker = aged_session / "0123456789abcdef"
                    aged_marker.write_text("aged\n", encoding="ascii")
                    aged_time = time.time() - memspec.NOTICE_MARKER_TTL_SECONDS - 60
                    os.utime(aged_marker, (aged_time, aged_time))
                    # 清掃改成有間隔的（每次呼叫都掃一遍舊標記，實測 2,945 次 stat、54 ms）。
                    # 把時間戳拿掉就代表「這一輪該掃了」，掃的行為本身照舊要驗。
                    try:
                        (sweep_root / memspec.NOTICE_SWEEP_STAMP).unlink()
                    except OSError:
                        pass
                    _notice_marker("sweep-" + uuid.uuid4().hex, "synthetic notice")
                    checks.append(
                        (
                            "aged notice markers are swept instead of accumulating",
                            not aged_marker.exists() and not aged_session.exists(),
                        )
                    )

                    second_session = sweep_root / ("aged2-" + uuid.uuid4().hex)
                    second_session.mkdir(parents=True, exist_ok=True)
                    second_marker = second_session / "0123456789abcdef"
                    second_marker.write_text("aged\n", encoding="ascii")
                    os.utime(second_marker, (aged_time, aged_time))
                    _notice_marker("sweep-" + uuid.uuid4().hex, "another notice")
                    checks.append(
                        (
                            "剛掃過就不再掃：標記是同一場的去重，不是每次呼叫都要付的代價",
                            second_marker.exists(),
                        )
                    )
                finally:
                    for name, value in saved_temp.items():
                        if value is None:
                            os.environ.pop(name, None)
                        else:
                            os.environ[name] = value

            miss = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "synthetic.txt"}},
                config,
            )
            checks.append(
                (
                    "a call that writes no file content allows silently",
                    miss.returncode == 0 and not miss.stdout and not miss.stderr,
                )
            )

            write_root = root / "write-gate"
            write_vault = write_root / "vault"
            write_vault.mkdir(parents=True)
            (write_vault / "mirror.md").write_text(
                "---\nname: 虛擬盤鏡像裁定\ndescription: 2026-08-13 虛擬盤與實盤參數一致\n"
                f"{memspec.DECISION_KEY_FIELD}: virtual-mirrors-live\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-08-13\n"
                f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 虛擬必須鏡像實盤\n"
                f"{memspec.ALIASES_FIELD}: [虛擬盤, 鏡像實盤]\n"
                f"{memspec.FORBIDDEN_FIELD}: [兩套參數]\n---\nbody\n",
                encoding="utf-8",
            )
            (write_vault / "retired.md").write_text(
                "---\nname: 舊制\ndescription: 2026-01-01 已作廢\n"
                f"{memspec.DECISION_KEY_FIELD}: retired-write-rule\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.SUPERSEDED_DECISION_STATUS}\n"
                f"{memspec.SUPERSEDED_BY_FIELD}: mirror.md\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-01-01\n"
                f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 舊制原話\n"
                f"{memspec.ALIASES_FIELD}: [退休甲, 退休乙]\n"
                f"{memspec.FORBIDDEN_FIELD}: [退休禁詞]\n---\nbody\n",
                encoding="utf-8",
            )
            write_config_path = root / "write-config.json"
            write_config(write_config_path, [write_vault])

            def write_call(tool_name, tool_input, session=None, extra=None):
                # Positive card fixtures carry their own original synthetic record.
                # Preserve undecidable inputs and the exact recurrence/guard paths.
                if isinstance(tool_input, dict):
                    tool_input = dict(tool_input)
                    target = _write_target(tool_input, os.fspath(write_root))
                    if tool_name.casefold() in memspec.WRITE_GATE_TOOL_NAMES and target is not None:
                        _additions, prospective = _prospective_write(tool_name, tool_input, target)
                        if (prospective is not None and target.parent == write_vault
                                and target.suffix.casefold() == ".md"):
                            from epitype import rule_pipeline
                            card_type, fields = card_lint.card_type_of(target.name, prospective, target)
                            if rule_pipeline.is_rule(fields, card_type, prospective):
                                bound = source_bound_card(prospective, target,
                                    source_root=write_root / "source-fixtures", case_id=target.stem)
                                if tool_name.casefold() == "write":
                                    tool_input["content"] = bound
                                else:
                                    before = target.read_text(encoding="utf-8")
                                    if tool_name.casefold() == "multiedit":
                                        tool_input["edits"] = [*tool_input["edits"], {
                                            "old_string": "---\n", "new_string": bound.splitlines(True)[0]
                                            + bound.splitlines(True)[1]}]
                                    else:
                                        tool_input.update(old_string=before, new_string=bound, replace_all=False)
                    elif isinstance(tool_input.get("command"), str):
                        command = tool_input["command"]
                        entries = patch_envelope.parse(command)
                        if len(entries) == 1 and entries[0].op == "update":
                            entry = entries[0]
                            target = _resolve_target(entry.path, os.fspath(write_root))
                            if target is not None and target.parent == write_vault:
                                try:
                                    before = target.read_text(encoding="utf-8-sig")
                                except (OSError, UnicodeError):
                                    before = None
                                prospective = patch_envelope.apply_update(entry, before) if before is not None else None
                                if prospective is not None:
                                    from epitype import rule_pipeline
                                    card_type, fields = card_lint.card_type_of(target.name, prospective, target)
                                    if rule_pipeline.is_rule(fields, card_type, prospective):
                                        bound = source_bound_card(prospective, target,
                                            source_root=write_root / "source-fixtures", case_id=target.stem,
                                            host="codex")
                                        first_name = before.splitlines()[1]
                                        hunk = "@@\n ---\n+" + bound.splitlines()[1] + "\n " + first_name + "\n"
                                        marker = f"*** Update File: {entry.path}\n"
                                        tool_input["command"] = command.replace(marker, marker + hunk, 1)
                event = {
                    "session_id": session or f"write-{uuid.uuid4().hex}",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "cwd": os.fspath(write_root),
                }
                event.update(extra or {})
                # The event carries cwd, and every ancestor of a cwd is looked up
                # as a slug under home: without this the case would read the test
                # machine's own native vaults.
                result = run_synthetic(
                    Path(__file__),
                    event,
                    write_config_path,
                    environment={
                        "HOME": os.fspath(write_root),
                        "USERPROFILE": os.fspath(write_root),
                    },
                )
                value = json.loads(result.stdout) if result.stdout.strip() else {}
                return result, value.get("hookSpecificOutput", {})

            # 寫檔的三條檢查（既有裁定 forbidden、卡片契約、再犯敘述）只提醒：放行（不帶
            # permissionDecision），提醒行帶各自的標籤。放行的對照組要看標籤沒出現，
            # 否則「不判」與「判了但只提醒」分不出來。
            forbidden_note = memspec.WRITE_NOTE_FORBIDDEN_LABEL
            card_note = memspec.WRITE_NOTE_CARD_LABEL
            recurrence_note = memspec.WRITE_NOTE_RECURRENCE_LABEL

            def noted(out, label, *fragments):
                context = out.get("additionalContext", "")
                return ("permissionDecision" not in out and label in context
                        and all(fragment in context for fragment in fragments))

            def quiet(out, label):
                return (out.get("permissionDecision") != "deny"
                        and label not in out.get("additionalContext", ""))

            forbidden_write, forbidden_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "plan.txt"),
                    "content": "我打算讓虛擬盤用兩套參數各自最佳化。",
                },
            )
            checks.append((
                "Write 的內容命中現行裁定的 forbidden 就提醒並放行，提醒帶裁定鍵、日期、裁定說明與命中片段",
                forbidden_write.returncode == 0
                and noted(forbidden_out, forbidden_note, "virtual-mirrors-live，2026-08-13",
                          "虛擬盤與實盤參數一致", "兩套參數"),
            ))
            write_log = write_vault / memspec.GATE_LOG_FILENAME
            write_rows = [
                json.loads(line)
                for line in write_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ] if write_log.is_file() else []
            checks.append((
                "命中的寫入以 write_note 入帳，只記規則、裁定鍵與檔名，不記內容",
                any(
                    row.get("kind") == "write_note"
                    and row.get("rule") == memspec.WRITE_GATE_FORBIDDEN_RULE
                    and row.get("decision") == "virtual-mirrors-live"
                    and row.get("filename") == "plan.txt"
                    and "兩套參數" not in json.dumps(row, ensure_ascii=False)
                    for row in write_rows
                ),
            ))

            edit_target = write_root / "notes.txt"
            edit_target.write_text("原本這裡寫著舊做法。\n", encoding="utf-8")
            _edit_result, edit_out = write_call(
                "Edit",
                {
                    "file_path": os.fspath(edit_target),
                    "old_string": "舊做法",
                    "new_string": "兩套參數",
                },
            )
            _multi_result, multi_out = write_call(
                "MultiEdit",
                {
                    "file_path": os.fspath(edit_target),
                    "edits": [
                        {"old_string": "原本", "new_string": "現在"},
                        {"old_string": "舊做法", "new_string": "兩套參數"},
                    ],
                },
            )
            checks.append((
                "Edit 的 new_string 與 MultiEdit 其中一項命中 forbidden 都提醒",
                noted(edit_out, forbidden_note) and noted(multi_out, forbidden_note),
            ))
            _stale_result, stale_out = write_call(
                "Edit",
                {
                    "file_path": os.fspath(edit_target),
                    "old_string": "這個字串不在檔案裡",
                    "new_string": "無害替代文字",
                },
            )
            checks.append((
                "Edit 找不到 old_string 時不判寫入後內容，放行",
                _stale_result.returncode == 0 and not _stale_result.stdout.strip(),
            ))

            _superseded_result, superseded_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "old.txt"),
                    "content": "退休禁詞照舊寫進來。",
                },
            )
            checks.append((
                "superseded 的決策卡不再擋寫入",
                _superseded_result.returncode == 0 and not superseded_out,
            ))

            # 2026-09-06 事故：改「定義 forbidden 的那張卡」時被自己的 forbidden 擋住。
            _own_result, own_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "mirror.md"),
                    "content": "---\nname: 虛擬盤鏡像裁定\ndescription: 2026-08-13 虛擬盤與實盤參數一致\n"
                    f"{memspec.DECISION_KEY_FIELD}: virtual-mirrors-live\n"
                    f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                    f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-08-13\n"
                    f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                    f"{memspec.OWNER_QUOTE_FIELD}: 虛擬必須鏡像實盤\n"
                    f"{memspec.ALIASES_FIELD}: [虛擬盤, 鏡像實盤]\n"
                    f"{memspec.FORBIDDEN_FIELD}: [兩套參數]\n---\n這條裁定禁的就是兩套參數。\n",
                },
            )
            checks.append((
                "改的就是定義那條 forbidden 的決策卡：命中自己的禁詞不提醒",
                _own_result.returncode == 0 and quiet(own_out, forbidden_note),
            ))

            _other_result, other_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "other-rule.md"),
                    "content": "---\nname: 別的裁定\ndescription: 2026-09-06 另一條裁定\n"
                    f"{memspec.DECISION_KEY_FIELD}: k-other\n"
                    f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                    f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-09-06\n"
                    f"{memspec.DECIDED_BY_FIELD}: three-way\n"
                    f"{memspec.ALIASES_FIELD}: [別甲, 別乙]\n---\n就讓虛擬盤用兩套參數各自最佳化。\n",
                },
            )
            _plain_result, plain_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "plan2.txt"),
                    "content": "之後一律改用兩套參數。",
                },
            )
            checks.append((
                "豁免只認那張卡：別的決策卡與一般檔案寫同一句仍然提醒",
                noted(other_out, forbidden_note) and noted(plain_out, forbidden_note),
            ))

            # 2026-09-06 對抗審：宣告同一個 decision_key 就能讓任何檔案繞過規則 A。
            _forged_result, forged_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "plan3.txt"),
                    "content": "---\nname: 假冒\ndescription: 不是那張卡\n"
                    f"{memspec.DECISION_KEY_FIELD}: virtual-mirrors-live\n"
                    "---\n就讓虛擬盤用兩套參數各自最佳化。\n",
                },
            )
            checks.append((
                "vault 外的檔案自稱同一個 decision_key 不算在改那張卡，照樣提醒",
                noted(forged_out, forbidden_note),
            ))

            _block_result, block_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "new-rule.md"),
                    "content": "---\nname: 新規則\ndescription: 2026-09-06 把裸名詞改寫成句形\n"
                    f"{memspec.DECISION_KEY_FIELD}: k-newrule\n"
                    f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                    f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-09-06\n"
                    f"{memspec.DECIDED_BY_FIELD}: three-way\n"
                    f"{memspec.ALIASES_FIELD}: [新甲, 新乙]\n"
                    f"{memspec.FORBIDDEN_FIELD}:\n"
                    "  - (建議|要不要|是否|應該).{0,12}(納入|採用|改成)兩套參數\n---\nbody\n",
                },
            )
            checks.append((
                "禁詞落在寫入內容自己的 forbidden 區塊裡＝正在改規則，放行",
                _block_result.returncode == 0 and quiet(block_out, forbidden_note),
            ))

            _outside_result, outside_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(root / "outside-any-vault.md"),
                    "content": "---\nname: 沒有必填欄位的卡\n---\nbody\n",
                },
            )
            checks.append((
                "落在所有已登記 vault 之外的 .md 不做規則 B",
                _outside_result.returncode == 0 and not _outside_result.stdout.strip(),
            ))

            _card_result, card_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "new-decision.md"),
                    "content": "---\nname: 新裁定\ndescription: 2026-09-06 只寫了一半\n"
                    f"{memspec.DECISION_KEY_FIELD}: k-new\n---\nbody\n",
                },
            )
            card_reason = card_out.get("additionalContext", "")
            checks.append((
                "寫進 vault 的決策卡缺必填欄位就提醒並放行，提醒列出缺哪些欄位並附可照抄的一行範例",
                noted(card_out, card_note)
                and all(
                    field in card_reason
                    for field in (
                        memspec.DECISION_STATUS_FIELD,
                        memspec.CURRENT_DECISION_AT_FIELD,
                        memspec.DECIDED_BY_FIELD,
                        memspec.ALIASES_FIELD,
                    )
                )
                and memspec.WRITE_GATE_FIELD_EXAMPLES[memspec.DECISION_STATUS_FIELD] in card_reason,
            ))
            card_rows = [
                json.loads(line)
                for line in write_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            checks.append((
                "規則 B 的攔阻入帳記 card_path，不記卡片內容",
                any(
                    row.get("rule") == memspec.WRITE_GATE_CARD_RULE
                    and row.get("card_path") == "new-decision.md"
                    for row in card_rows
                ),
            ))

            _warn_result, warn_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "reference-dated.md"),
                    "content": "---\nname: reference-dated\ndescription: english only reference card\n"
                    f"{memspec.LAST_VERIFIED_AT_FIELD}: 2026-09-01\n"
                    "metadata:\n  type: reference\n---\nbody\n",
                },
            )
            checks.append((
                "WARN 級只在 additionalContext 提示，不擋寫入",
                _warn_result.returncode == 0
                and "permissionDecision" not in warn_out
                and memspec.WRITE_GATE_CARD_ADVICE[:6] in warn_out.get("additionalContext", "")
                and "reference-dated.md" in warn_out.get("additionalContext", "")
                and memspec.ALIASES_FIELD in warn_out.get("additionalContext", "")
                and "沒有中文字" not in warn_out.get("additionalContext", ""),
            ))

            _derived_result, derived_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "body-dated.md"),
                    "content": "---\nname: body-dated\ndescription: 欄位無日期、正文有\n"
                    f"{memspec.ALIASES_FIELD}: [正文日期]\n"
                    "metadata:\n  type: feedback\n---\n2026-08-15 那天的紀錄\n",
                },
            )
            checks.append((
                "正文有日期、frontmatter 沒有的卡不擋：規則 B 用的是 card_lint 同一條日期判定",
                _derived_result.returncode == 0
                and "permissionDecision" not in derived_out
                and memspec.CARD_DATE_SOURCE_BODY in derived_out.get("additionalContext", ""),
            ))

            repeat_session = "write-repeat-" + uuid.uuid4().hex
            repeat_input = {
                "file_path": os.fspath(write_root / "again.txt"),
                "content": "還是兩套參數。",
            }
            _first_result, first_out = write_call("Write", repeat_input, session=repeat_session)
            second_result, second_out = write_call("Write", repeat_input, session=repeat_session)
            checks.append((
                "同 session 同規則同檔案同內容只提醒一次，第二次安靜放行",
                noted(first_out, forbidden_note)
                and second_result.returncode == 0
                and not second_out,
            ))

            _codex_result, codex_out = write_call(
                "write_file",
                {
                    "path": os.fspath(write_root / "codex.txt"),
                    "content": "改成兩套參數再說。",
                },
                extra={"transcript_path": os.fspath(root / "codex-synthetic.jsonl")},
            )
            checks.append((
                "Codex 形狀的檔案寫入工具走同一道檢查（同樣提醒）",
                _codex_result.returncode == 0
                and noted(codex_out, forbidden_note),
            ))

            oversized_content = "兩套參數" + "填充" * 70000
            _big_result, big_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "big.txt"),
                    "content": oversized_content,
                },
            )
            checks.append((
                "超過內容上限就放行（fail-open，不在 hook deadline 內跑大字串）",
                len(oversized_content.encode("utf-8")) > memspec.WRITE_GATE_MAX_CONTENT_BYTES
                and _big_result.returncode == 0
                and not big_out,
            ))

            (write_vault / "broken-forbidden.md").write_text(
                "---\nname: 壞禁詞\ndescription: 2026-09-06 禁詞正則寫壞了\n"
                f"{memspec.DECISION_KEY_FIELD}: k-broken-forbidden\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-09-06\n"
                f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 這條的禁詞寫壞了\n"
                f"{memspec.ALIASES_FIELD}: [壞甲, 壞乙]\n"
                f"{memspec.FORBIDDEN_FIELD}: ['(a+)+$']\n---\nbody\n",
                encoding="utf-8",
            )
            _broken_result, broken_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "probe.txt"),
                    "content": "a" * 24,
                },
            )
            checks.append((
                "無法使用的 forbidden 正則被忽略而非擋下，並向模型點名",
                _broken_result.returncode == 0
                and "permissionDecision" not in broken_out
                and "k-broken-forbidden" in broken_out.get("additionalContext", ""),
            ))

            # U64: 規則 A 走 stop_gate._forbidden_fragment，引號豁免同一處修好兩邊都有。
            quoted_write_result, quoted_write_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "check_forbidden.py"),
                    "content": 'FORBIDDEN_PHRASE = "兩套參數"\nassert FORBIDDEN_PHRASE not in reply\n',
                },
            )
            checks.append((
                "forbidden 落在寫入內容的引號字串字面值內是引用／驗證腳本，不擋",
                quoted_write_result.returncode == 0
                and quiet(quoted_write_out, forbidden_note),
            ))

            bare_write_result, bare_write_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "check_forbidden2.py"),
                    "content": 'FORBIDDEN_PHRASE = "兩套參數"\n# 這裡直接寫兩套參數，沒加引號。\n',
                },
            )
            checks.append((
                "同一份內容除了引號內的引用還有裸禁詞，裸的那份照樣提醒",
                bare_write_result.returncode == 0
                and noted(bare_write_out, forbidden_note),
            ))

            # 規則再犯（2026-09-25 titan）：往已存在的行為卡追加事故敘述、卻沒動任何武裝欄位，擋；
            # 同時在武裝、新卡、非行為卡、一般用語與判不準的輸入，全部放行。
            recurrence_line = "**再犯（2026-09-25，titan 台指期休市事故）**：Run 區塊又拆成兩條指令。"
            recurrence_body = "Run 按鈕區塊必須是一條完整的 PowerShell 指令。\n"

            def recurrence_card(name, card_type="feedback", extra=""):
                return (
                    f"---\nname: {name}\n"
                    "description: 2026-09-01 Run 按鈕區塊只能是一條指令\n"
                    "aliases:\n  - Run 按鈕\n"
                    "unenforceable: 判斷型，訊息裡沒有可比對的字面訊號\n"
                    f"{extra}metadata:\n  type: {card_type}\n---\n{recurrence_body}"
                )

            def recurrence_rows():
                return [
                    json.loads(line)
                    for line in write_log.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ] if write_log.is_file() else []

            def append_incident(path, text=recurrence_line, session=None, extra=None):
                return write_call(
                    "Edit",
                    {
                        "file_path": os.fspath(path),
                        "old_string": recurrence_body,
                        "new_string": recurrence_body + "\n" + text + "\n",
                    },
                    session=session,
                    extra=extra,
                )

            run_card = write_vault / "feedback-run-button.md"
            run_card.write_text(recurrence_card("feedback-run-button"), encoding="utf-8")
            recurrence_denied, recurrence_out = append_incident(run_card)
            checks.append((
                "往擋不住的 feedback 卡追加「再犯（日期）…」就提醒並放行，提醒寫出三種處理與事故經過不進卡",
                recurrence_denied.returncode == 0
                and noted(recurrence_out, recurrence_note, "feedback-run-button.md",
                          memspec.FORBIDDEN_FIELD, memspec.UNENFORCEABLE_FIELD,
                          "修 Epitype 本身", "不寫進卡片", "台指期休市")
                and recurrence_body in run_card.read_text(encoding="utf-8")
                and recurrence_line not in run_card.read_text(encoding="utf-8"),
            ))
            checks.append((
                "再犯提醒以 write_note 入帳，只記規則與卡名，不記內容",
                any(
                    row.get("kind") == "write_note"
                    and row.get("rule") == memspec.RECURRENCE_GATE_RULE
                    and row.get("card_path") == "feedback-run-button.md"
                    and "台指期" not in json.dumps(row, ensure_ascii=False)
                    for row in recurrence_rows()
                ),
            ))

            armed_card = write_vault / "feedback-armed.md"
            armed_card.write_text(recurrence_card("feedback-armed"), encoding="utf-8")
            armed_result, armed_out = write_call(
                "MultiEdit",
                {
                    "file_path": os.fspath(armed_card),
                    "edits": [
                        {
                            "old_string": "metadata:\n",
                            "new_string": "forbidden:\n  - 'Run 區塊.{0,20}兩條指令'\nmetadata:\n",
                        },
                        {
                            "old_string": recurrence_body,
                            "new_string": recurrence_body + "\n" + recurrence_line + "\n",
                        },
                    ],
                },
            )
            checks.append((
                "同一段追加但同時新增 forbidden（正在武裝）就放行，並以 write_allow 入帳",
                armed_result.returncode == 0
                and quiet(armed_out, recurrence_note)
                and any(
                    row.get("kind") == memspec.RECURRENCE_GATE_ALLOW_LOG_KIND
                    and row.get("card_path") == "feedback-armed.md"
                    and row.get("outcome") == memspec.RECURRENCE_GATE_OUTCOME_ARMING
                    and "台指期" not in json.dumps(row, ensure_ascii=False)
                    for row in recurrence_rows()
                ),
            ))

            fresh_card = write_vault / "feedback-fresh.md"
            fresh_result, fresh_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(fresh_card),
                    "content": recurrence_card("feedback-fresh") + "\n" + recurrence_line + "\n",
                },
            )
            project_card = write_vault / "project-titan.md"
            project_card.write_text(recurrence_card("project-titan", "project"), encoding="utf-8")
            project_result, project_out = append_incident(project_card)
            checks.append((
                "新建的卡含「再犯」、以及 project 卡追加再犯敘述，都放行",
                fresh_result.returncode == 0
                and quiet(fresh_out, recurrence_note)
                and project_result.returncode == 0
                and quiet(project_out, recurrence_note),
            ))

            envelope_card = write_vault / "feedback-codex.md"
            envelope_card.write_text(recurrence_card("feedback-codex"), encoding="utf-8")
            envelope_command = "\n".join((
                "apply_patch <<'EOF'",
                patch_envelope.BEGIN_MARKER,
                f"*** Update File: {os.fspath(envelope_card)}",
                "@@",
                " " + recurrence_body.rstrip("\n"),
                "+",
                "+" + recurrence_line,
                patch_envelope.END_MARKER,
                "EOF",
            ))
            envelope_result, envelope_out = write_call("Bash", {"command": envelope_command})
            checks.append((
                "Codex 形狀（exec 夾 patch 封套）往同一種卡追加再犯敘述，同樣提醒",
                envelope_result.returncode == 0
                and noted(envelope_out, recurrence_note, "feedback-codex.md", "不寫進卡片"),
            ))

            ordinary = []
            ordinary_owner_words = "驗證程式片段太大問題，免得以後再犯。"
            ordinary_source = write_root / "ordinary-owner.jsonl"
            ordinary_source.write_text(json.dumps({
                "type": "user", "sessionId": "ordinary-owner", "uuid": "original-owner",
                "timestamp": "2026-06-18T00:00:00Z",
                "message": {"content": ordinary_owner_words}}, ensure_ascii=False) + "\n", encoding="utf-8")
            for index, text in enumerate((
                "owner 2026-06-18：「驗證程式片段太大問題，免得以後再犯。」",
                "## 我最大的錯（永遠別再犯）",
                "這是行為層的防再犯規則，2026-09-20 起適用。",
                "- 不要再犯沒有對於各商品有不同判斷方式（2026-07-12）",
            )):
                ordinary_card = write_vault / f"feedback-ordinary-{index}.md"
                ordinary_card.write_text(
                    recurrence_card(f"feedback-ordinary-{index}"), encoding="utf-8")
                ordinary.append(append_incident(ordinary_card, text,
                    extra={"transcript_path": os.fspath(ordinary_source)}))
            checks.append((
                "「免得以後再犯」「永遠別再犯」「防再犯」「不要再犯」是一般用語，不是事故敘述，放行",
                all(
                    result.returncode == 0 and quiet(out, recurrence_note)
                    for result, out in ordinary
                ),
            ))

            conditional = []
            for index, text in enumerate((
                "2026-09-25 起的處理規則：再犯時先補 forbidden。",
                "If this recurs after 2026-09-25, add a forbidden pattern before anything else.",
                "- 如果 2026-10-01 之後又犯，就改修 Epitype 本身。",
                "2026-09-25 再犯時先補 forbidden。",
                "2026-09-25 再犯的話先補 forbidden。",
            )):
                conditional_card = write_vault / f"feedback-conditional-{index}.md"
                conditional_card.write_text(
                    recurrence_card(f"feedback-conditional-{index}"), encoding="utf-8")
                conditional.append(append_incident(conditional_card, text))
            checks.append((
                "條件句（「再犯時先補 forbidden」「如果…又犯，就」「if this recurs」，日期緊貼標記但後接「時／的話」亦同）是規則正文，放行",
                all(
                    result.returncode == 0 and quiet(out, recurrence_note)
                    for result, out in conditional
                ),
            ))

            headline = []
            for index, text in enumerate((
                "**再犯（titan 台指期休市事故）**：Run 區塊又拆成兩條指令。",
                "## 第二次犯同一件事",
                "3. **（titan）又犯**：同一件事。",
            )):
                headline_card = write_vault / f"feedback-headline-{index}.md"
                headline_card.write_text(
                    recurrence_card(f"feedback-headline-{index}"), encoding="utf-8")
                headline.append(append_incident(headline_card, text))
            checks.append((
                "沒有日期但以再犯標記開頭的粗體或標題行照樣提醒；標記不在開頭的粗體不提醒",
                noted(headline[0][1], recurrence_note)
                and noted(headline[1][1], recurrence_note)
                and quiet(headline[2][1], recurrence_note),
            ))

            # 同一段內容走 Write（整份覆寫既有卡）、Edit、Codex 補丁三條路，結論必須一致。
            # 卡上已經有 forbidden：補丁新增的內縮行不能因為「也許在武裝」就被放過。
            first_item = "  - '貼出兩段.{0,5}指令'"
            second_item = "  - '分成.{0,3}兩個區塊'"
            indented_incident = "    **再犯（2026-09-25）**：Run 區塊又拆成兩條指令。"
            paths_card = write_vault / "feedback-paths.md"
            paths_text = recurrence_card(
                "feedback-paths", extra=f"{memspec.FORBIDDEN_FIELD}:\n{first_item}\n")
            paths_card.write_text(paths_text, encoding="utf-8")

            def three_paths(arm):
                armed_text = paths_text.replace(
                    first_item + "\n", first_item + "\n" + second_item + "\n") if arm else paths_text
                written = armed_text.replace(
                    recurrence_body, recurrence_body + "\n" + indented_incident + "\n")
                edits = [{"old_string": recurrence_body,
                          "new_string": recurrence_body + "\n" + indented_incident + "\n"}]
                hunks = []
                if arm:
                    edits.insert(0, {"old_string": first_item + "\n",
                                     "new_string": first_item + "\n" + second_item + "\n"})
                    hunks += ["@@", " " + memspec.FORBIDDEN_FIELD + ":", " " + first_item,
                              "+" + second_item]
                hunks += ["@@", " " + recurrence_body.rstrip("\n"), "+", "+" + indented_incident]
                command = "\n".join(
                    ["apply_patch <<'EOF'", patch_envelope.BEGIN_MARKER,
                     f"*** Update File: {os.fspath(paths_card)}", *hunks,
                     patch_envelope.END_MARKER, "EOF"])
                return [
                    write_call("Write", {"file_path": os.fspath(paths_card), "content": written}),
                    write_call("MultiEdit", {"file_path": os.fspath(paths_card), "edits": edits}),
                    write_call("Bash", {"command": command}),
                ]

            unarmed_paths = three_paths(False)
            checks.append((
                "已有 forbidden 的卡正文追加內縮再犯行：Write 整份覆寫、Edit、補丁三條路都提醒",
                all(
                    result.returncode == 0
                    and noted(out, recurrence_note, "不寫進卡片")
                    for result, out in unarmed_paths
                ),
            ))
            armed_paths = three_paths(True)
            checks.append((
                "同一段內容但在既有 forbidden 底下多補一項：三條路都放行",
                all(
                    result.returncode == 0 and quiet(out, recurrence_note)
                    for result, out in armed_paths
                ),
            ))

            dated_card = write_vault / "feedback-dated-then.md"
            dated_card.write_text(recurrence_card("feedback-dated-then"), encoding="utf-8")
            _dated_result, dated_out = append_incident(
                dated_card, "2026-09-25 又犯就表示閘門失效。")
            checks.append((
                "日期緊貼標記、後面單獨接「就」不算條件句：「2026-09-25 又犯就表示閘門失效。」照樣提醒",
                noted(dated_out, recurrence_note),
            ))

            def patch_command(card_path, *hunk_lines):
                return "\n".join((
                    "apply_patch <<'EOF'", patch_envelope.BEGIN_MARKER,
                    f"*** Update File: {os.fspath(card_path)}", *hunk_lines,
                    patch_envelope.END_MARKER, "EOF"))

            # 檔裡是 en dash，補丁寫 ASCII 連字號：Codex 最寬鬆那一輪對得上，這裡也要對得上，
            # 否則會拿「套不上」去判一個 Codex 實際會寫出去的武裝修改。
            dash_old = "  - 'run–two.{0,3}x'"
            dash_new = "  - 'run-three.{0,3}x'"
            dash_card = write_vault / "feedback-dash.md"
            dash_text = recurrence_card(
                "feedback-dash", extra=f"{memspec.FORBIDDEN_FIELD}:\n{dash_old}\n")
            dash_card.write_text(dash_text, encoding="utf-8")
            _dash_write, dash_write_out = write_call("Write", {
                "file_path": os.fspath(dash_card),
                "content": dash_text.replace(dash_old, dash_new) + "\n" + recurrence_line + "\n",
            })
            _dash_patch, dash_patch_out = write_call("Bash", {"command": patch_command(
                dash_card, "@@", " " + memspec.FORBIDDEN_FIELD + ":",
                "-" + dash_old.replace("–", "-"), "+" + dash_new,
                "@@", " " + recurrence_body.rstrip("\n"), "+", "+" + recurrence_line)})
            checks.append((
                "既有 forbidden 的 run–two（en dash）改成 run-three 並追加事故：補丁與完整寫入同樣放行",
                dash_write_out.get("permissionDecision") != "deny"
                and dash_patch_out.get("permissionDecision") != "deny"
                # 兩條路各記一筆 arming_changed：補丁那條是套出全文後判定的，不是湊巧放過。
                and sum(
                    row.get("card_path") == "feedback-dash.md"
                    and row.get("outcome") == memspec.RECURRENCE_GATE_OUTCOME_ARMING
                    for row in recurrence_rows()
                ) == 2,
            ))

            lost_card = write_vault / "feedback-lost-anchor.md"
            lost_card.write_text(recurrence_card("feedback-lost-anchor"), encoding="utf-8")
            _lost_result, lost_out = write_call("Bash", {"command": patch_command(
                lost_card, "@@ 這一行不在卡裡", "+", "+" + recurrence_line)})
            checks.append((
                "補丁的 @@ 定位行找不到（Codex 也套不上）：放行，並記 patch_unappliable",
                lost_out.get("permissionDecision") != "deny"
                and any(
                    row.get("card_path") == "feedback-lost-anchor.md"
                    and row.get("outcome") == memspec.RECURRENCE_GATE_OUTCOME_UNAPPLIABLE
                    for row in recurrence_rows()
                ),
            ))

            eof_card = write_vault / "feedback-eof.md"
            eof_card.write_text(recurrence_card("feedback-eof") + "tail line\n", encoding="utf-8")
            _eof_miss, eof_miss_out = write_call("Bash", {"command": patch_command(
                eof_card, "@@", " " + recurrence_body.rstrip("\n"), "+" + recurrence_line,
                "*** End of File")})
            _eof_hit, eof_hit_out = write_call("Bash", {"command": patch_command(
                eof_card, "@@", " tail line", "+" + recurrence_line, "*** End of File")})
            checks.append((
                "End of File 段落只比檔尾：上下文在前面的行就是套不上而不判，落在檔尾才照判而提醒",
                quiet(eof_miss_out, recurrence_note)
                and noted(eof_hit_out, recurrence_note),
            ))

            bom_card = write_vault / "feedback-bom.md"
            bom_card.write_bytes(
                "﻿".encode("utf-8") + recurrence_card("feedback-bom").encode("utf-8"))
            _bom_result, bom_out = append_incident(bom_card)
            checks.append((
                "帶 BOM 的既有卡追加再犯敘述照樣提醒",
                noted(bom_out, recurrence_note),
            ))

            crlf_card = write_vault / "feedback-crlf.md"
            crlf_card.write_bytes(
                recurrence_card("feedback-crlf").replace("\n", "\r\n").encode("utf-8"))
            _crlf_edit, crlf_edit_out = append_incident(crlf_card)
            crlf_command = "\n".join((
                "apply_patch <<'EOF'", patch_envelope.BEGIN_MARKER,
                f"*** Update File: {os.fspath(crlf_card)}", "@@",
                " " + recurrence_body.rstrip("\n"), "+", "+" + recurrence_line,
                patch_envelope.END_MARKER, "EOF"))
            _crlf_patch, crlf_patch_out = write_call("Bash", {"command": crlf_command})
            checks.append((
                "CRLF 的既有卡追加再犯敘述，Edit 與補丁都提醒",
                noted(crlf_edit_out, recurrence_note)
                and noted(crlf_patch_out, recurrence_note),
            ))

            quoted_card = write_vault / "feedback-quoted.md"
            quoted_text = (
                '---\nname: "feedback-quoted"\n'
                'description: "2026-09-01 Run 按鈕區塊只能是一條指令"\n'
                'aliases:\n  - "Run 按鈕"\n'
                'unenforceable: "判斷型，訊息裡沒有可比對的字面訊號"\n'
                f'metadata:\n  type: "feedback"\n---\n{recurrence_body}'
            )
            quoted_card.write_text(quoted_text, encoding="utf-8")
            _quoted_denied, quoted_deny_out = append_incident(quoted_card)
            _quoted_armed, quoted_arm_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(quoted_card),
                    "content": quoted_text.replace(
                        "metadata:", f"{memspec.FORBIDDEN_FIELD}: ['貼出兩段.{{0,5}}指令']\nmetadata:"
                    ) + "\n" + recurrence_line + "\n",
                },
            )
            checks.append((
                "欄位值加引號的卡照樣判成 feedback：追加就提醒，同時補上 flow 形式的 forbidden 就不提醒",
                noted(quoted_deny_out, recurrence_note)
                and quiet(quoted_arm_out, recurrence_note),
            ))

            exempt_card = write_vault / "feedback-exempt.md"
            exempt_card.write_text(recurrence_card("feedback-exempt"), encoding="utf-8")
            exempt_result, exempt_out = write_call(
                "Edit",
                {
                    "file_path": os.fspath(exempt_card),
                    "old_string": "unenforceable: 判斷型，訊息裡沒有可比對的字面訊號\n",
                    "new_string": (
                        "unenforceable: 判斷型；2026-09-25 又犯一次，字面訊號仍然抓不到\n"
                    ),
                },
            )
            checks.append((
                "新增的再犯字樣只在 unenforceable 欄位裡就放行，並記 exempt_fields_only",
                exempt_result.returncode == 0
                and exempt_out.get("permissionDecision") != "deny"
                and any(
                    row.get("card_path") == "feedback-exempt.md"
                    and row.get("outcome") == memspec.RECURRENCE_GATE_OUTCOME_EXEMPT_FIELDS
                    for row in recurrence_rows()
                ),
            ))

            repeat_card = write_vault / "feedback-repeat.md"
            repeat_card.write_text(recurrence_card("feedback-repeat"), encoding="utf-8")
            repeat_session = f"recurrence-{uuid.uuid4().hex}"
            _first_result, first_repeat = append_incident(repeat_card, session=repeat_session)
            second_result, second_repeat = append_incident(repeat_card, session=repeat_session)
            checks.append((
                "同一場同一份內容第二次送來不再提醒，但以 repeat 入帳看得見",
                noted(first_repeat, recurrence_note)
                and second_result.returncode == 0
                and quiet(second_repeat, recurrence_note)
                and any(
                    row.get("card_path") == "feedback-repeat.md"
                    and row.get("outcome") == memspec.RECURRENCE_GATE_OUTCOME_REPEAT
                    for row in recurrence_rows()
                ),
            ))

            broken_card = write_vault / "feedback-broken.md"
            broken_card.write_bytes(b"---\nname: broken\n\xff\xfe\n---\nbody\n")
            broken_inputs = (
                ("Edit", {"file_path": os.fspath(broken_card), "old_string": "body\n",
                          "new_string": "body\n" + recurrence_line + "\n"}),
                ("Edit", {"file_path": os.fspath(run_card), "old_string": recurrence_body,
                          "new_string": 7}),
                ("Edit", {"file_path": os.fspath(run_card), "old_string": "不存在的原文",
                          "new_string": recurrence_line}),
                ("Bash", {"command": patch_envelope.BEGIN_MARKER + "\n*** Update File: "
                          + os.fspath(run_card) + "\n+" + recurrence_line}),
                ("Edit", [recurrence_line]),
            )
            broken_results = [write_call(name, tool_input) for name, tool_input in broken_inputs]
            checks.append((
                "判不準的輸入（壞編碼的卡、非字串、原文不在檔裡、沒收尾的封套、非物件輸入）一律放行",
                all(
                    result.returncode == 0 and out.get("permissionDecision") != "deny"
                    for result, out in broken_results
                ),
            ))

            # 用量計：放行路徑跨過 0.97T 附一行含交接檔路徑的提醒，同一個壓縮週期只說一次；
            # 子代理的呼叫不說、不寫標記，之後主線照樣收得到。
            from epitype import compact_map

            meter_vault = root / "meter-vault"
            meter_vault.mkdir()
            meter_config = root / "meter-config.json"
            write_config(meter_config, [meter_vault])
            meter_options = json.loads(meter_config.read_text(encoding="utf-8"))
            meter_options[memspec.CONTEXT_METER_CONFIG_FIELD] = {
                memspec.CONTEXT_METER_OVERRIDE_FIELD: 100000}
            meter_config.write_text(json.dumps(meter_options), encoding="utf-8")
            meter_transcript = root / "meter.jsonl"
            meter_transcript.write_text(json.dumps({"type": "assistant", "message": {"usage": {
                "input_tokens": 92000, "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 5000}}}) + "\n", encoding="utf-8")

            def meter_call(session_id, **extra):
                result = run_synthetic(
                    Path(__file__),
                    {"tool_name": "Bash", "tool_input": {"command": "echo hi"},
                     "session_id": session_id, "transcript_path": str(meter_transcript), **extra},
                    meter_config,
                )
                context = ""
                if result.stdout.strip():
                    context = json.loads(result.stdout)["hookSpecificOutput"].get("additionalContext", "")
                return result, context

            main_handoff = os.fspath(
                compact_map.handoff_destination(meter_vault, "meter-main", meter_transcript))
            first_meter, first_context = meter_call("meter-main")
            second_meter, second_context = meter_call("meter-main")
            checks.append((
                "at 0.98 of the threshold the first allowed call carries the handoff line, the next one nothing",
                first_meter.returncode == 0
                and "permissionDecision" not in first_meter.stdout
                and main_handoff in first_context
                and "98k" in first_context
                and second_meter.returncode == 0
                and not second_meter.stdout.strip()
                and not second_context,
            ))
            sub_handoff = os.fspath(
                compact_map.handoff_destination(meter_vault, "meter-sub", meter_transcript))
            by_agent, agent_context = meter_call("meter-sub", agent_id="agent-1")
            agent_left_no_marker = not (
                recall_marker_directory("meter-sub") / memspec.CONTEXT_METER_MARKER).exists()
            after_agent, after_context = meter_call("meter-sub")
            checks.append((
                "a subagent's call says nothing and claims nothing, so the main thread still gets the line",
                by_agent.returncode == 0
                and not by_agent.stdout.strip()
                and agent_left_no_marker
                and sub_handoff in after_context,
            ))

            # Codex 形狀的 PreToolUse（rollout 在 .codex/sessions、帶 model 與 turn_id）：到了
            # 壓縮點－H 放行時附 Codex 的那一行，輸出是 Codex 認得的 additionalContext（沒有
            # permissionDecision），同一個週期不再說；Claude 的全域覆寫（100000）不套上 Codex。
            codex_rollout, codex_env = write_codex_fixture(root, "codex-meter", 170000)

            def codex_meter_call():
                result = run_synthetic(
                    Path(__file__),
                    {"hook_event_name": "PreToolUse", "session_id": "codex-meter", "turn_id": "t1",
                     "transcript_path": os.fspath(codex_rollout), "cwd": os.fspath(root),
                     "model": "gpt-x", "permission_mode": "default", "tool_name": "Bash",
                     "tool_use_id": "call-1", "tool_input": {"command": "echo hi"}},
                    meter_config, environment=codex_env,
                )
                value = json.loads(result.stdout) if result.stdout.strip() else {}
                return result, value.get("hookSpecificOutput", {})

            codex_first, codex_first_out = codex_meter_call()
            codex_second, codex_second_out = codex_meter_call()
            codex_handoff = os.fspath(
                compact_map.handoff_destination(meter_vault, "codex-meter", codex_rollout))
            checks.append((
                "a Codex-shaped PreToolUse at limit-H carries Codex's line as additionalContext once",
                codex_first.returncode == 0
                and set(codex_first_out) == {"hookEventName", "additionalContext"}
                and codex_first_out["additionalContext"] == memspec.CONTEXT_METER_CODEX_NOTICE.format(
                    cur=170, left=40, path=codex_handoff)
                and codex_second.returncode == 0
                and not codex_second.stdout.strip(),
            ))

            # 並行：Codex 一次並行好幾個工具呼叫。六個 hook 行程同時看到「還沒說過」，只有
            # 搶到標記的那一個說；其餘各記一行 meter-lost（至少一行＝真的並行過）。
            race_rollout, race_env = write_codex_fixture(root, "codex-race", 170000)
            race_event = {"hook_event_name": "PreToolUse", "session_id": "codex-race", "turn_id": "t1",
                          "transcript_path": os.fspath(race_rollout), "cwd": os.fspath(root),
                          "model": "gpt-x", "permission_mode": "default", "tool_name": "Bash",
                          "tool_use_id": "call-1", "tool_input": {"command": "echo hi"}}
            race_runs = run_concurrent(
                Path(__file__), race_event, meter_config, 6, environment=race_env,
                clock=memspec.HOOK_CLOCK_FROZEN)
            race_line = memspec.CONTEXT_METER_CODEX_NOTICE.format(
                cur=170, left=40,
                path=os.fspath(compact_map.handoff_destination(meter_vault, "codex-race", race_rollout)))
            trace_file = meter_config.parent / memspec.CONTEXT_METER_TRACE_FILENAME

            def traced(session):
                if not trace_file.is_file():
                    return []
                return [json.loads(line)["outcome"] for line in trace_file.read_text(encoding="utf-8").splitlines()
                        if f'"session":"{session}"' in line and '"event":"PreToolUse"' in line]

            race_outcomes = traced("codex-race")

            def context_of(stdout):
                value = json.loads(stdout) if stdout.strip() else {}
                return value.get("hookSpecificOutput", {}).get("additionalContext", "").splitlines()
            checks.append((
                "six concurrent Codex PreToolUse hooks at limit-H: exactly one says the line, the others lose the claim",
                all(code == 0 for code, _out, _err in race_runs)
                and sum(race_line in context_of(out) for _code, out, _err in race_runs) == 1
                and race_outcomes.count("meter-emitted") == 1
                and race_outcomes.count("meter-lost") >= 1
                and (recall_marker_directory("codex-race") / memspec.CONTEXT_METER_MARKER).is_file(),
            ))

            # 搶到卻沒送出去（輸出失敗）：放掉標記、記一行 meter-released，下一次照說。
            lost_rollout, lost_env = write_codex_fixture(root, "codex-unsent", 170000)
            lost_event = {**race_event, "session_id": "codex-unsent", "transcript_path": os.fspath(lost_rollout)}
            run_concurrent(Path(__file__), lost_event, meter_config, 1, environment=lost_env,
                           settle_seconds=0, close_stdout=True, clock=memspec.HOOK_CLOCK_FROZEN)
            unsent_marker = (recall_marker_directory("codex-unsent") / memspec.CONTEXT_METER_MARKER).exists()
            unsent_outcomes = traced("codex-unsent")
            unsent_retry = run_synthetic(
                Path(__file__), lost_event, meter_config, environment=lost_env,
                clock=memspec.HOOK_CLOCK_FROZEN)
            checks.append((
                "a claimed line whose output fails is released (traced) and said on the next call",
                not unsent_marker and "meter-released" in unsent_outcomes
                and "meter-emitted" not in unsent_outcomes
                and memspec.CONTEXT_METER_CODEX_NOTICE.format(
                    cur=170, left=40,
                    path=os.fspath(compact_map.handoff_destination(meter_vault, "codex-unsent", lost_rollout)))
                in context_of(unsent_retry.stdout),
            ))

            missing_config = root / "missing-config.json"
            missing = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}},
                missing_config,
            )
            checks.append(
                (
                    "missing config infra failure allows silently",
                    missing.returncode == 0
                    and not missing.stdout
                    and not missing.stderr,
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 56
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _best_effort_record(event):
    """附記這次呼叫碰到的檔名。壞了就算了：這是紀錄，不是關卡，不能讓它擋住工作。"""
    try:
        from epitype import opened

        config = load_config(_STARTED_AT)
        if config is None:
            return
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict):
            return
        def joined(fields):
            return " ".join(
                str(value)
                for name, value in tool_input.items()
                if name in fields and isinstance(value, (str, int, float, list, tuple))
            )

        weak_fields = tuple(
            name for name in memspec.OPENED_TARGET_FIELDS
            if name not in memspec.OPENED_STRONG_FIELDS
        )
        opened.record(
            governance_vault(config, for_write=True),
            event.get("session_id", event.get("sessionId")),
            joined(weak_fields),
            joined(memspec.OPENED_STRONG_FIELDS),
        )
    except Exception:
        pass


def main():
    if "--selftest" in sys.argv[1:]:
        with isolated_temp_root():
            return _selftest()
    defects = []
    meter_claims = []
    advice_claims = []
    won = []
    event = None
    try:
        event = read_event(sys.stdin)
        if event.get("hook_event_name") == "PostToolUse":
            from epitype import action_receipts
            action_receipts.finish(event, action_receipts.database_path(config_path()))
            # 截圖、點擊這類每步都觸發的操控改不了帳本：在讀設定、開 SQLite 之前就回去，
            # 否則每個瀏覽器動作都白付一次設定載入。
            if not control_lifecycle.is_tracked_tool(event.get("tool_name")):
                return 0
            try:
                config = load_config(_STARTED_AT)
                if config is not None:
                    note = control_lifecycle.observe(
                        event, control_lifecycle.database_path(config_path())
                    )
                    if note:
                        emit({"hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": note,
                        }})
            except Exception as exc:
                if config_path().exists():
                    print(memspec.GATE_DEGRADED_NOTICE.format(
                        gate="PostToolUse", reason=type(exc).__name__), file=sys.stderr)
            return 0
        from epitype import action_receipts
        if config_path().exists():
            event=action_receipts.refresh(event,action_receipts.database_path(config_path()))
        value = _handle(event, _STARTED_AT, defects, meter_claims, advice_claims)
        updated = (value or {}).get("hookSpecificOutput", {}).get("updatedInput")
        if isinstance(updated, dict):
            event = {**event, "tool_input": updated}
        # 這一次呼叫碰到哪些檔，附記給回合閘用：說「我查過某個檔」的時候，那個檔名
        # 必須在這裡出現過。擋下的呼叫不記——那個檔根本沒被打開。
        if value is None or value.get("hookSpecificOutput", {}).get(
                "permissionDecision") != "deny":
            _best_effort_record(event)
            from epitype import action_receipts
            if config_path().exists():
                action_receipts.begin(event, action_receipts.database_path(config_path()))
        # A guard card that stopped being enforced is the silent failure §34 warned
        # about, so it is named on stderr rather than swallowed.
        for line in defects[: memspec.GATE_DEFECT_MAX_LINES]:
            print(line, file=sys.stderr)
        if value is not None or meter_claims:
            output = (value or {}).get("hookSpecificOutput", {})
            is_deny = output.get("permissionDecision") == "deny"
            if is_deny or isinstance(updated, dict) or not expired(_STARTED_AT):
                lost = []
                if meter_claims and not is_deny:
                    # 先搶、搶到才附：並行的 hook 行程只有一個說。搶到卻沒印出去的在下面放掉。
                    value, won, lost = claim_meter(value, "PreToolUse", meter_claims)
                if value is not None:
                    emit(value)
                    sys.stdout.flush()
                    _finish_advice(advice_claims, True)
                emitted, won = won, []
                _trace_meter(event, emitted, lost)
    except Exception as exc:
        # 這裡是最後一道：設定檔壞了、記憶庫讀不到、程式本身有 bug，全都走這一圈。
        # 仍然 fail-open（不擋住工作），但裝了卻用不了的時候一定要講一句——安靜退場
        # 跟「沒有東西要擋」在外面看起來一模一樣，而這正是本專案最不能容忍的那種壞掉。
        # 沒有設定檔則照舊安靜：那代表這個專案根本沒在用 Epitype，不是壞掉。
        try:
            if config_path().exists():
                print(memspec.GATE_DEGRADED_NOTICE.format(
                    gate=memspec.GATE_NAME_ACTION,
                    reason=type(exc).__name__), file=sys.stderr)
        except Exception:
            pass
    if won:
        release_meter(won)
        _trace_meter(event, (), (), won)
    _finish_advice(advice_claims, False)
    return 0


def _trace_meter(event, emitted, lost, released=()):
    """用量提醒的去向各記一行：說了、並行時沒搶到、搶到卻沒送出去而放掉。"""
    for outcome, items in (("meter-emitted", emitted), ("meter-lost", lost),
                           ("meter-released", released)):
        for _directory, _marker, tokens in items:
            trace_hook("PreToolUse", event, _STARTED_AT, outcome=outcome, tokens=tokens)


if __name__ == "__main__":
    raise SystemExit(main())
