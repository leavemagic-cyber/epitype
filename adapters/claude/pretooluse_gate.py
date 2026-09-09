import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude PreToolUse adapter for the fail-open write-content gate.

2026-09-09 (owner, docs/FAILURE_MODES.md §34): the scar-card `trigger:`
interception is gone. Irreversible actions are the host's own native rules
(Claude `permissions.deny`, Codex `execpolicy`); what stays here is the gate over
the content a call is about to write, which no host rule can express."""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import memspec
from _hook_common import (
    GATE_LOG_MAX_BYTES,
    append_gate_log,
    compile_bounded_regex,
    emit,
    expired,
    load_config,
    notice_marker_directory,
    read_event,
    resolve_vaults,
    run_synthetic,
    with_session,
    write_config,
)


def _best_effort_audit(callback, *arguments):
    try:
        callback(*arguments)
    except Exception:
        pass


def _deny_value(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _sweep_notice_markers(root, now, keep=None):
    """Markers are a same-session dedupe, not a record: drop the aged-out ones."""
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


def _write_target(tool_input, cwd):
    """Absolute path this call is about to write, or None when it names none."""
    for field in memspec.WRITE_GATE_PATH_FIELDS:
        raw = tool_input.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            path = Path(raw)
            if not path.is_absolute() and isinstance(cwd, str) and cwd.strip():
                path = Path(cwd) / path
            return path.resolve()
        except (OSError, TypeError, ValueError):
            return None
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
                if key == memspec.FORBIDDEN_FIELD:
                    block.append(value)
                continue
            if indented and parent == memspec.FORBIDDEN_FIELD:
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
        for decision in stop_gate._decisions(vault, started_at):
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
                return (
                    vault,
                    decision.key,
                    memspec.WRITE_GATE_FORBIDDEN_REASON.format(
                        decision=stop_gate._named(decision),
                        quote=decision.quote,
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


def _card_review(relative, text):
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
        card_type, findings = card_lint.check_card(staging, relative)
    finally:
        try:
            staging.unlink()
        except OSError:
            pass

    fails = [reason for level, _rule, reason in findings if level == card_lint.FAIL]
    warns = [reason for level, _rule, reason in findings if level == card_lint.WARN]
    advice = (
        memspec.WRITE_GATE_CARD_ADVICE.format(
            card_type=card_type, path=relative, problems="；".join(warns)
        )
        if warns
        else None
    )
    if not fails:
        return None, advice
    problems = "；".join(fails)
    examples = [
        example
        for field, example in memspec.WRITE_GATE_FIELD_EXAMPLES.items()
        if field in problems
    ]
    reason = memspec.WRITE_GATE_CARD_REASON.format(
        card_type=card_type,
        path=relative,
        problems=problems,
        example="；".join(examples[: memspec.GATE_DEFECT_MAX_LINES])
        or "見 docs/ARCHITECTURE.md §Card types and required fields",
    )
    return reason[: memspec.WRITE_GATE_REASON_MAX_CHARS], advice


def _write_marker(session_id, rule, target, text):
    """Same-session dedupe keyed by (rule, file, content digest): a model that
    cannot satisfy a ruling would otherwise be denied the same write forever."""
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
                "kind": memspec.WRITE_GATE_LOG_KIND,
                "rule": rule,
                "filename": target.name,
                **subject,
            },
            session_id,
        ),
        started_at,
    )


def _write_review(event, tool_name, tool_input, config, started_at):
    """(deny value, advice lines) for a call about to write file content.

    Rule A: new content that re-states what the owner already ruled out is blocked
    against the Stop gate's decision cards. Rule B: a card written into a registered
    vault must satisfy card_lint's contract for its own type — FAIL blocks, WARN only
    advises. Anything else proceeds untouched: a non-file tool, a re-entrant hook run,
    a path outside every vault, content past the size cap, or a post-write text that
    cannot be known exactly. Bash redirections never reach this gate at all
    (docs/FAILURE_MODES.md §11)."""
    if tool_name.casefold() not in memspec.WRITE_GATE_TOOL_NAMES:
        return None, []
    if not isinstance(tool_input, dict) or event.get("stop_hook_active"):
        return None, []
    target = _write_target(tool_input, event.get("cwd"))
    if target is None or expired(started_at):
        return None, []
    additions, prospective = _prospective_write(tool_name, tool_input, target)

    def oversized(text):
        return len(text.encode("utf-8", errors="replace")) > memspec.WRITE_GATE_MAX_CONTENT_BYTES

    if any(oversized(text) for text in additions):
        return None, []
    if prospective is not None and oversized(prospective):
        prospective = None

    notices = []
    session_id = event.get("session_id")
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
        return _deny_value(reason[: memspec.WRITE_GATE_REASON_MAX_CHARS]), []

    if prospective is None or expired(started_at):
        return None, notices
    card = _vault_card_path(target, resolve_vaults(config, event))
    if card is None:
        return None, notices
    vault, relative = card
    reason, advice = _card_review(relative, prospective)
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
    return _deny_value(reason), []


def _allow_context(event, notices=()):
    """Context for a call the gate lets through: the write gate's own advice and
    the rulings whose `forbidden` pattern could not be compiled, each named once
    per session — a ruling that silently stopped being enforced is the failure the
    gate exists to prevent."""
    lines = []
    session_id = event.get("session_id")
    for notice in list(dict.fromkeys(notices))[: memspec.GATE_DEFECT_MAX_LINES]:
        if _notice_marker(session_id, notice):
            lines.append(notice)
    if not lines:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(lines)}}


def _handle(event, started_at):
    """The gate's whole decision: only a call about to write file content can be
    denied, and only against the owner's own settled rulings and the card
    contract. Every other tool call is the host's native rules to judge, never
    this hook's (owner 2026-09-09, docs/FAILURE_MODES.md §34)."""
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    config = load_config(started_at)
    if config is None:
        return None
    try:
        write_value, notices = _write_review(
            event, tool_name, event.get("tool_input"), config, started_at
        )
    except Exception:
        write_value, notices = None, ()
    if write_value is not None:
        return write_value
    return _allow_context(event, notices)


def _selftest():
    checks = []
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

            sweep_root = Path(tempfile.gettempdir()) / memspec.NOTICE_MARKER_DIRECTORY
            aged_session = sweep_root / ("aged-" + uuid.uuid4().hex)
            aged_session.mkdir(parents=True, exist_ok=True)
            aged_marker = aged_session / "0123456789abcdef"
            aged_marker.write_text("aged\n", encoding="ascii")
            aged_time = time.time() - memspec.NOTICE_MARKER_TTL_SECONDS - 60
            os.utime(aged_marker, (aged_time, aged_time))
            _notice_marker("sweep-" + uuid.uuid4().hex, "synthetic notice")
            checks.append(
                (
                    "aged notice markers are swept instead of accumulating",
                    not aged_marker.exists() and not aged_session.exists(),
                )
            )

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

            forbidden_write, forbidden_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "plan.txt"),
                    "content": "我打算讓虛擬盤用兩套參數各自最佳化。",
                },
            )
            forbidden_reason = forbidden_out.get("permissionDecisionReason", "")
            checks.append((
                "Write 的內容命中現行裁定的 forbidden 就擋，理由帶裁定鍵、日期、owner 原話與命中片段",
                forbidden_write.returncode == 0
                and forbidden_out.get("permissionDecision") == "deny"
                and "virtual-mirrors-live，2026-08-13" in forbidden_reason
                and "虛擬必須鏡像實盤" in forbidden_reason
                and "兩套參數" in forbidden_reason,
            ))
            write_log = write_vault / memspec.GATE_LOG_FILENAME
            write_rows = [
                json.loads(line)
                for line in write_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ] if write_log.is_file() else []
            checks.append((
                "擋下的寫入以 write_block 入帳，只記規則、裁定鍵與檔名，不記內容",
                any(
                    row.get("kind") == memspec.WRITE_GATE_LOG_KIND
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
                "Edit 的 new_string 與 MultiEdit 其中一項命中 forbidden 都擋",
                edit_out.get("permissionDecision") == "deny"
                and multi_out.get("permissionDecision") == "deny",
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
                "改的就是定義那條 forbidden 的決策卡：命中自己的禁詞不擋",
                _own_result.returncode == 0 and own_out.get("permissionDecision") != "deny",
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
                "豁免只認那張卡：別的決策卡與一般檔案寫同一句仍然擋",
                other_out.get("permissionDecision") == "deny"
                and plain_out.get("permissionDecision") == "deny",
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
                "vault 外的檔案自稱同一個 decision_key 不算在改那張卡，照擋",
                forged_out.get("permissionDecision") == "deny",
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
                _block_result.returncode == 0 and block_out.get("permissionDecision") != "deny",
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
            card_reason = card_out.get("permissionDecisionReason", "")
            checks.append((
                "寫進 vault 的決策卡缺必填欄位就擋，理由列出缺哪些欄位並附可照抄的一行範例",
                card_out.get("permissionDecision") == "deny"
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
                "同 session 同規則同檔案同內容只擋一次，AI 修不動時不會無限卡死",
                first_out.get("permissionDecision") == "deny"
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
                "Codex 形狀的檔案寫入工具走同一道閘",
                _codex_result.returncode == 0
                and codex_out.get("permissionDecision") == "deny",
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
                and "permissionDecision" not in quoted_write_out,
            ))

            bare_write_result, bare_write_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "check_forbidden2.py"),
                    "content": 'FORBIDDEN_PHRASE = "兩套參數"\n# 這裡直接寫兩套參數，沒加引號。\n',
                },
            )
            checks.append((
                "同一份內容除了引號內的引用還有裸禁詞，裸的那份照擋",
                bare_write_result.returncode == 0
                and bare_write_out.get("permissionDecision") == "deny",
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
    total = 30
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main():
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    try:
        event = read_event(sys.stdin)
        value = _handle(event, _STARTED_AT)
        if value is not None:
            output = value.get("hookSpecificOutput", {})
            is_deny = output.get("permissionDecision") == "deny"
            if is_deny or not expired(_STARTED_AT):
                emit(value)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
