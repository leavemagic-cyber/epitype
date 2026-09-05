import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude PreToolUse adapter for fail-open, card-driven safety advice."""

from datetime import datetime, timezone
import ast
import hashlib
import json
import os
from pathlib import Path
import re
from re import _constants as _re_constants
from re import _parser as _re_parser
import shlex
import tempfile
import uuid
from typing import NamedTuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import memsearch, memspec, narration_meter
from _hook_common import (
    emit,
    encode_payload,
    expired,
    load_config,
    read_event,
    resolve_vaults,
    run_synthetic,
    session_component,
    write_config,
)

_TRIGGER_KEYS = (
    memspec.TRIGGER_TOOL_FIELD,
    memspec.TRIGGER_INPUT_FIELD,
    memspec.TRIGGER_MATCH_FIELD,
)
SHELL_TOOL_NAMES = frozenset(
    ("bash", "powershell", "sh", "cmd", "shell", "shell_command", "exec_command")
)
_REPEAT_OPS = frozenset(
    (
        _re_constants.MAX_REPEAT,
        _re_constants.MIN_REPEAT,
        _re_constants.POSSESSIVE_REPEAT,
    )
)
_GROUPREF_OPS = frozenset(
    (
        _re_constants.GROUPREF,
        _re_constants.GROUPREF_EXISTS,
        _re_constants.GROUPREF_IGNORE,
        _re_constants.GROUPREF_LOC_IGNORE,
        _re_constants.GROUPREF_UNI_IGNORE,
    )
)


def _validate_regex_tree(items, inside_repeat=False):
    """Reject the shapes that can backtrack exponentially on a crafted tool input:
    a repetition or an alternation nested inside an unbounded repetition, and
    backreferences. A hung gate is killed by the host and the call proceeds, so
    such a card would be a bypass. Adjacent repetitions (`\\s+\\S+`) and anything
    under a bounded `?` stay accepted: at worst polynomial, and real cards use
    them; the rejection itself is surfaced by the caller, never silent."""
    for opcode, argument in items:
        if opcode in _REPEAT_OPS:
            unbounded = argument[1] > 1
            if unbounded and inside_repeat:
                raise ValueError("trigger regex nests one repetition inside another")
            _validate_regex_tree(argument[2], inside_repeat=inside_repeat or unbounded)
        elif opcode == _re_constants.BRANCH:
            if inside_repeat:
                raise ValueError("trigger regex repeats an alternation")
            for branch in argument[1]:
                _validate_regex_tree(branch, inside_repeat=inside_repeat)
        elif opcode == _re_constants.SUBPATTERN:
            _validate_regex_tree(argument[-1], inside_repeat=inside_repeat)
        elif opcode in (_re_constants.ASSERT, _re_constants.ASSERT_NOT):
            _validate_regex_tree(argument[1], inside_repeat=inside_repeat)
        elif opcode == getattr(_re_constants, "ATOMIC_GROUP", object()):
            _validate_regex_tree(argument, inside_repeat=inside_repeat)
        elif opcode in _GROUPREF_OPS:
            raise ValueError("trigger regex backreferences are not supported")


def _compile_trigger_regex(pattern):
    if len(pattern) > memspec.TRIGGER_REGEX_MAX_CHARS:
        raise ValueError("trigger regex exceeds the length limit")
    parsed = _re_parser.parse(pattern, 0)
    _validate_regex_tree(parsed)
    return re.compile(pattern)


def _scalar(raw):
    value, problem = memspec.parse_scalar(raw)
    if problem:
        raise ValueError(problem)
    return value


def _inline_items(body):
    items = []
    current = []
    quote = None
    escaped = False
    for character in body:
        if quote is not None:
            current.append(character)
            if escaped:
                escaped = False
            elif character == "\\" and quote == '"':
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in ('"', "'"):
            quote = character
            current.append(character)
        elif character == ",":
            items.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if quote is not None:
        raise ValueError("unterminated quoted trigger value")
    items.append("".join(current).strip())
    return [item for item in items if item]


def _inline_mapping(raw):
    value = raw.strip()
    if not (value.startswith("{") and value.endswith("}")):
        raise ValueError("trigger must be a mapping")
    result = {}
    for item in _inline_items(value[1:-1]):
        if ":" not in item:
            raise ValueError("malformed trigger mapping")
        key, raw_value = item.split(":", 1)
        key = key.strip()
        if key not in _TRIGGER_KEYS:
            raise ValueError("unknown trigger field")
        if key in result:
            raise ValueError("duplicate trigger field")
        result[key] = _scalar(raw_value)
    return result


def _frontmatter(path):
    lines, closing = memspec.split_frontmatter(path.read_text(encoding="utf-8"))
    if lines is not None and closing is None:
        raise ValueError("unterminated frontmatter")
    return lines


_TRIGGER_CACHE_FILENAME = "gate_triggers.json"
_TRIGGER_CACHE_VERSION = 1


def _declares_trigger(path):
    lines = _frontmatter(path)
    return lines is not None and any(
        ":" in line and line[:1] not in " \t" and line.split(":", 1)[0].strip() == memspec.TRIGGER_FIELD
        for line in lines
    )


def _write_trigger_cache(cache_path, manifest, triggers):
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        staging = cache_path.with_name(f".{cache_path.name}.tmp-{os.getpid()}")
        staging.write_text(
            json.dumps(
                {"version": _TRIGGER_CACHE_VERSION, "manifest": manifest, "triggers": triggers},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(staging, cache_path)
    except OSError:
        pass


def _trigger_card_paths(vault):
    """Cards whose frontmatter declares a trigger.

    A manifest cache under the vault's index directory means a tool call re-reads
    only the cards that changed since the previous call, not every card in the
    vault; an unreadable or stale cache falls back to classifying the changed
    cards and is rewritten. Cards whose frontmatter cannot be parsed stay listed
    so the parse defect is still audited by the caller."""
    vault = Path(vault).resolve()
    scan = memsearch.scan_cards(vault)
    manifest = {card_path: [mtime_ns, size] for card_path, _, mtime_ns, size in scan}
    paths = {card_path: path for card_path, path, _, _ in scan}
    cache_path = vault / memspec.FTS_INDEX_DIRECTORY / _TRIGGER_CACHE_FILENAME
    cached = {}
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and loaded.get("version") == _TRIGGER_CACHE_VERSION:
            cached = loaded
    except (OSError, ValueError):
        pass
    old_manifest = cached.get("manifest")
    if not isinstance(old_manifest, dict):
        old_manifest = {}
    old_triggers = set(cached.get("triggers") or ())
    if manifest == old_manifest:
        return [paths[card_path] for card_path in sorted(old_triggers) if card_path in paths]
    triggers = []
    for card_path, path, _, _ in scan:
        if old_manifest.get(card_path) == manifest[card_path]:
            if card_path in old_triggers:
                triggers.append(card_path)
            continue
        try:
            declares = _declares_trigger(path)
        except OSError:
            continue
        except ValueError:
            declares = True
        if declares:
            triggers.append(card_path)
    _write_trigger_cache(cache_path, manifest, triggers)
    return [paths[card_path] for card_path in triggers]


def _parse_trigger_card(path):
    lines = _frontmatter(path)
    if lines is None:
        return None

    fields = {}
    trigger = {}
    trigger_seen = False
    current = None
    advice_style = None
    advice_lines = []
    problems = []

    def finish_advice():
        nonlocal advice_style, advice_lines
        if advice_style is not None:
            separator = "\n" if advice_style.startswith("|") else " "
            fields[memspec.ADVICE_FIELD] = separator.join(advice_lines).strip()
        advice_style = None
        advice_lines = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            if advice_style is not None and not stripped:
                advice_lines.append("")
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" \t"))
        if indent == 0:
            finish_advice()
            current = None
            if ":" not in raw_line:
                problems.append("malformed top-level frontmatter")
                continue
            key, raw_value = raw_line.split(":", 1)
            key = key.strip()
            value = raw_value.strip()
            if key == memspec.TRIGGER_FIELD:
                if trigger_seen:
                    problems.append("duplicate trigger field")
                    continue
                trigger_seen = True
                current = memspec.TRIGGER_FIELD
                if value:
                    try:
                        trigger.update(_inline_mapping(value))
                    except ValueError as exc:
                        problems.append(str(exc))
            elif key == memspec.ADVICE_FIELD:
                if value in ("|", ">", "|-", ">-", "|+", ">+"):
                    advice_style = value
                    current = memspec.ADVICE_FIELD
                else:
                    try:
                        fields[key] = _scalar(raw_value)
                    except (ValueError, json.JSONDecodeError) as exc:
                        problems.append(str(exc))
            elif key in ("name", memspec.DECISION_STATUS_FIELD):
                try:
                    fields[key] = _scalar(raw_value)
                except (ValueError, json.JSONDecodeError) as exc:
                    problems.append(str(exc))
            continue

        if current == memspec.TRIGGER_FIELD:
            if ":" not in stripped:
                problems.append("malformed nested trigger field")
                continue
            key, raw_value = stripped.split(":", 1)
            key = key.strip()
            if key not in _TRIGGER_KEYS:
                problems.append("unknown trigger field")
                continue
            if key in trigger:
                problems.append("duplicate trigger field")
                continue
            try:
                trigger[key] = _scalar(raw_value)
            except (ValueError, json.JSONDecodeError) as exc:
                problems.append(str(exc))
        elif current == memspec.ADVICE_FIELD and advice_style is not None:
            advice_lines.append(stripped)

    finish_advice()
    if not trigger_seen:
        return None
    if problems:
        raise ValueError(problems[0])

    tool_pattern = trigger.get(memspec.TRIGGER_TOOL_FIELD, "")
    input_pattern = trigger.get(memspec.TRIGGER_INPUT_FIELD, "")
    match_mode = trigger.get(memspec.TRIGGER_MATCH_FIELD, "")
    advice = fields.get(memspec.ADVICE_FIELD, "").strip()
    if not tool_pattern or not input_pattern or not advice:
        raise ValueError("trigger cards require tool, input, and advice")
    if match_mode not in (
        "",
        memspec.TRIGGER_COMMAND_MATCH,
        memspec.TRIGGER_FULLTEXT_MATCH,
    ):
        raise ValueError("trigger match must be command or fulltext when present")
    if (
        fields.get(memspec.DECISION_STATUS_FIELD, "").strip()
        == memspec.SUPERSEDED_DECISION_STATUS
    ):
        return None
    tool_regex = _compile_trigger_regex(tool_pattern)
    input_regex = _compile_trigger_regex(input_pattern)
    return {
        "path": path.resolve(),
        "name": fields.get("name", "").strip() or path.stem,
        "advice": advice,
        "match_mode": match_mode,
        "tool_regex": tool_regex,
        "input_regex": input_regex,
    }


class _CommandParseError(ValueError):
    pass


class _ShellToken(NamedTuple):
    value: str
    quoted: bool


class _CommandCandidate(NamedTuple):
    text: str
    executable_end: int
    wrapper: bool = False


def _uses_command_matching(tool_name, match_mode):
    return match_mode == memspec.TRIGGER_COMMAND_MATCH or (
        not match_mode and tool_name.casefold() in SHELL_TOOL_NAMES
    )


_HEREDOC_TOKEN = re.compile(
    r"\"(?:\\.|[^\"\\])*\"|'[^']*'|"
    r"<<(?P<tabs>-)?[ \t]*(?P<quote>['\"]?)(?P<name>[A-Za-z0-9_.:+-]+)(?P=quote)"
)


def _heredoc_markers(line):
    return [
        (match.group("name"), bool(match.group("tabs")))
        for match in _HEREDOC_TOKEN.finditer(line)
        if match.group("name") and not line.startswith("<<<", match.start())
    ]


def _without_heredoc_bodies(command):
    output = []
    pending = []
    for line in command.splitlines(keepends=True):
        if pending:
            delimiter, strip_tabs = pending[0]
            candidate = line.rstrip("\r\n")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            if candidate == delimiter:
                pending.pop(0)
            continue
        output.append(line)
        pending.extend(_heredoc_markers(line.rstrip("\r\n")))
    if pending:
        raise _CommandParseError("unterminated heredoc")
    return "".join(output)


def _shell_segments(command):
    command = _without_heredoc_bodies(command)
    lexer = shlex.shlex(command, posix=False, punctuation_chars="|&;\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        raw_tokens = list(lexer)
    except ValueError as exc:
        raise _CommandParseError("invalid shell token syntax") from exc
    segments, current, comment = [], [], False
    for raw in raw_tokens:
        if raw == "\n" or raw and set(raw) <= {"|", "&", ";"}:
            if current:
                segments.append(current)
            current, comment = [], False
            continue
        quoted = len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'")
        if not quoted and raw.startswith("#"):
            comment = True
        if comment:
            continue
        value = raw[1:-1] if quoted else raw
        shell_substitution = "$(" in value or "`" in value
        if (not quoted and (shell_substitution or any(mark in value for mark in "(){}"))) or (
            quoted and raw[0] == '"' and shell_substitution
        ):
            raise _CommandParseError("unsupported shell grouping")
        current.append(_ShellToken(value, quoted))
    if current:
        segments.append(current)
    return segments


def _executable_name(value):
    name = re.split(r"[\\/]", value)[-1]
    return re.sub(r"(?i)\.(?:exe|com|cmd|bat|ps1|sh|py)$", "", name)


def _python_embedded_commands(code, depth):
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise _CommandParseError("invalid Python command string") from exc
    commands = []
    execution_calls = {"system", "popen", "run", "call", "check_call", "check_output", "exec", "eval"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else (
            function.id if isinstance(function, ast.Name) else ""
        )
        if name.lower() not in execution_calls:
            continue
        argument, value = node.args[0], None
        if isinstance(argument, ast.Constant):
            value = argument.value
        elif isinstance(argument, (ast.List, ast.Tuple)):
            values = [
                item.value for item in argument.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            value = " ".join(values) if len(values) == len(argument.elts) else None
        if isinstance(value, str):
            nested = _python_embedded_commands if name.lower() in ("exec", "eval") else _command_candidates
            commands.extend(nested(value, depth + 1))
    return commands


def _as_wrapper_candidates(candidates):
    return [
        _CommandCandidate(candidate.text, candidate.executable_end, True)
        for candidate in candidates
    ]


def _command_candidates(command, depth=0):
    if depth > 8:
        raise _CommandParseError("command wrapper nesting too deep")
    candidates = []
    for tokens in _shell_segments(command):
        index = 0
        while index < len(tokens):
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index].value):
                index += 1
                continue
            executable = _executable_name(tokens[index].value).lower()
            if executable in ("&", "call", "command", "exec", "nohup"):
                index += 1
                continue
            if executable == "env":
                index += 1
                while index < len(tokens) and (
                    tokens[index].value.startswith("-")
                    or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index].value)
                ):
                    index += 1
                continue
            if executable == "sudo":
                index += 1
                value_options = {"-u", "--user", "-g", "--group", "-h", "--host"}
                while index < len(tokens) and tokens[index].value.startswith("-"):
                    option = tokens[index].value.split("=", 1)[0]
                    index += 1
                    if option in value_options and index < len(tokens):
                        index += 1
                continue
            break
        if index >= len(tokens):
            continue

        executable_token = tokens[index]
        executable = _executable_name(executable_token.value)
        lowered = executable.lower()
        arguments = tokens[index + 1 :]
        wrapper_flags = {
            "cmd": {"/c", "/k"},
            "powershell": {"-c", "-command"},
            "pwsh": {"-c", "-command"},
            "bash": {"-c"},
            "sh": {"-c"},
            "zsh": {"-c"},
        }
        if lowered in wrapper_flags:
            for flag_index, token in enumerate(arguments):
                if token.value.lower() in wrapper_flags[lowered]:
                    payload = " ".join(
                        item.value for item in arguments[flag_index + 1 :]
                    )
                    if not payload:
                        raise _CommandParseError("shell wrapper lacks command string")
                    candidates.extend(
                        _as_wrapper_candidates(_command_candidates(payload, depth + 1))
                    )
                    break

        if lowered == "py" or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", lowered):
            for flag_index, token in enumerate(arguments):
                if token.value.lower() == "-c":
                    if flag_index + 1 >= len(arguments):
                        raise _CommandParseError("Python wrapper lacks command string")
                    candidates.extend(
                        _as_wrapper_candidates(
                            _python_embedded_commands(
                                arguments[flag_index + 1].value, depth
                            )
                        )
                    )
                    break

        script_flags = {
            "node": {"-e", "--eval"},
            "ruby": {"-e"},
            "perl": {"-e"},
        }
        if lowered in script_flags:
            for flag_index, token in enumerate(arguments):
                if token.value.lower() in script_flags[lowered]:
                    if flag_index + 1 >= len(arguments):
                        raise _CommandParseError("interpreter lacks command string")
                    candidates.extend(
                        _as_wrapper_candidates(
                            _python_embedded_commands(
                                arguments[flag_index + 1].value, depth
                            )
                        )
                    )
                    break

        visible_tokens = []
        executable_end = 0
        if not executable_token.quoted:
            visible_tokens.append(executable)
            executable_end = len(executable)
        visible_tokens.extend(
            token.value
            for token in arguments
            if not token.quoted and not token.value.startswith((">", "<"))
        )
        candidate = " ".join(visible_tokens).strip()
        if candidate:
            candidates.append(_CommandCandidate(candidate, executable_end))
    return candidates


def _command_match_position(input_regex, candidates):
    for candidate in candidates:
        found = input_regex.search(candidate.text)
        if found is None:
            continue
        if candidate.wrapper:
            return "wrapper"
        if candidate.executable_end and found.start() < candidate.executable_end:
            return "executable"
        return "argument"
    return None


def _append_gate_log(vault, row, started_at):
    target = vault / memspec.GATE_LOG_FILENAME
    remaining = memspec.HOOK_TIMEOUT_SECONDS - (time.monotonic() - started_at)
    if remaining <= 0:
        raise TimeoutError("hook deadline reached")
    value = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **row,
    }
    with memspec.file_lock(target, min(0.25, remaining)) as acquired:
        if not acquired:
            raise OSError("gate audit lock unavailable")
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _append_audit(
    vault, tool_name, card_name, started_at, fallback=None, position=None
):
    row = {"tool": tool_name, "card": card_name}
    if fallback is not None:
        row["fallback"] = fallback
    if position is not None:
        row["position"] = position
    _append_gate_log(
        vault,
        row,
        started_at,
    )


def _append_parse_defect(vault, path, error, started_at):
    _append_gate_log(
        vault,
        {
            "kind": "parse_defect",
            "filename": path.name,
            "reason": f"{type(error).__name__}: {error}",
        },
        started_at,
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


def _bounded_deny(card):
    source = f" [{card['path']}]"
    advice = card["advice"]
    value = _deny_value(advice + source)
    if len(encode_payload(value).encode("utf-8")) <= memspec.HOOK_MAX_OUTPUT_BYTES:
        return value

    # Preserve the card source while trimming only the user-authored advice.
    # JSON escaping is variable-width, so size the final serialized payload.
    if len(encode_payload(_deny_value(source.strip())).encode("utf-8")) > memspec.HOOK_MAX_OUTPUT_BYTES:
        source = " [Epitype trigger]"
    low = 0
    high = len(advice)
    best = _deny_value("Epitype trigger matched." + source)
    while low <= high:
        middle = (low + high) // 2
        candidate = _deny_value(advice[:middle].rstrip() + "…" + source)
        if len(encode_payload(candidate).encode("utf-8")) <= memspec.HOOK_MAX_OUTPUT_BYTES:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _sweep_narration_markers(root, now, keep=None):
    """Markers are a same-session dedupe, not a record: drop the aged-out ones."""
    try:
        for session_directory in root.iterdir():
            if not session_directory.is_dir() or session_directory == keep:
                continue
            empty = True
            for marker in session_directory.iterdir():
                try:
                    if now - marker.stat().st_mtime > memspec.NARRATION_MARKER_TTL_SECONDS:
                        marker.unlink()
                    else:
                        empty = False
                except OSError:
                    empty = False
            if empty:
                session_directory.rmdir()
    except OSError:
        pass


def _narration_marker(session_id, text):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    root = Path(tempfile.gettempdir()) / memspec.NARRATION_MARKER_DIRECTORY
    directory = root / session_component(session_id)
    try:
        _sweep_narration_markers(root, time.time(), keep=directory)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / digest).open("x", encoding="ascii") as stream:
            stream.write(digest + "\n")
    except FileExistsError:
        return False
    except OSError:
        return True
    return True


def _narration_context(event, started_at):
    """One bounded line when the model narrated between tool calls (owner 2026-09-03).

    Never touches permissionDecision: the tool still goes through the normal
    permission path; the text only tells the model what it just paid for.
    """
    if expired(started_at):
        return None
    blocks = narration_meter.current_turn_blocks(event.get("transcript_path"))
    text = narration_meter.pending_narration(blocks)
    if text is None:
        return None
    if not _narration_marker(event.get("session_id"), text):
        return None  # a batch of tool calls after one narration is flagged once
    segments = len(narration_meter.narration_segments(blocks))
    context = (
        f"{memspec.NARRATION_PREFIX} {len(text.strip())} 字（本輪第 {max(segments, 1)} 段）："
        f"{memspec.NARRATION_ADVICE}"
    )
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}}


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


def _forbidden_write(event, config, additions, started_at, notices):
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
        example="；".join(examples[: memspec.GATE_DEFECT_MAX_LINES]) or "見 docs/ARCHITECTURE.md 卡片型別表",
    )
    return reason[: memspec.WRITE_GATE_REASON_MAX_CHARS], advice


def _write_marker(session_id, rule, target, text):
    """Same-session dedupe keyed by (rule, file, content digest): a model that
    cannot satisfy a ruling would otherwise be denied the same write forever."""
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return _narration_marker(
        session_id, f"{memspec.WRITE_GATE_LOG_KIND}\0{rule}\0{target}\0{digest}"
    )


def _append_write_block(vault, rule, subject, target, started_at):
    """Audit a blocked write by rule, subject, and filename only — the content is
    exactly the material a ruling is about and does not belong in the ledger."""
    _append_gate_log(
        vault,
        {
            "kind": memspec.WRITE_GATE_LOG_KIND,
            "rule": rule,
            "filename": target.name,
            **subject,
        },
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
    found = _forbidden_write(event, config, additions, started_at, notices)
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
    )
    return _deny_value(reason), []


def _allow_context(event, started_at, defects, notices=()):
    """Context for a call the gate lets through: trigger cards it could not use
    are named once per session (a scar that silently stopped applying is the
    failure the gate exists to prevent), then the write gate's own advice, then
    any narration notice."""
    lines = []
    session_id = event.get("session_id")
    for name, reason in defects[: memspec.GATE_DEFECT_MAX_LINES]:
        notice = memspec.GATE_DEFECT_NOTICE.format(name=name, reason=reason)
        if _narration_marker(session_id, notice):
            lines.append(notice)
    for notice in list(dict.fromkeys(notices))[: memspec.GATE_DEFECT_MAX_LINES]:
        if _narration_marker(session_id, notice):
            lines.append(notice)
    narration = _narration_context(event, started_at)
    if narration is not None:
        lines.append(narration["hookSpecificOutput"]["additionalContext"])
    if not lines:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(lines)}}


def _handle(event, started_at):
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    tool_input = event.get("tool_input")
    tool_input_text = json.dumps(
        tool_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    config = load_config(started_at)
    if config is None:
        return None

    cards = []
    defects = []
    for vault in config[memspec.CONFIG_VAULTS_FIELD]:
        if expired(started_at):
            return None
        try:
            trigger_paths = _trigger_card_paths(vault)
        except OSError:
            continue
        for path in trigger_paths:
            if expired(started_at):
                return None
            try:
                card = _parse_trigger_card(path)
            except Exception as exc:
                defects.append((path.stem, f"{type(exc).__name__}: {exc}"))
                _best_effort_audit(
                    _append_parse_defect, vault, path, exc, started_at
                )
                continue
            if card is not None:
                cards.append((vault, card))

    match = None
    command_state = None
    for vault, card in cards:
        if not card["tool_regex"].search(tool_name):
            continue
        fallback = None
        position = None
        if _uses_command_matching(tool_name, card["match_mode"]):
            if command_state is None:
                command = tool_input.get("command") if isinstance(tool_input, dict) else None
                try:
                    if not isinstance(command, str):
                        raise _CommandParseError("command input is not text")
                    command_state = (_command_candidates(command), False)
                except _CommandParseError:
                    command_state = ((), True)
            candidates, used_fallback = command_state
            if used_fallback:
                input_matches = card["input_regex"].search(tool_input_text) is not None
                fallback = "fulltext"
            else:
                position = _command_match_position(card["input_regex"], candidates)
                input_matches = position is not None
        else:
            input_matches = card["input_regex"].search(tool_input_text) is not None
        if input_matches:
            match = (vault, card, fallback, position)
            break
    if match is None:
        # Scar cards first: they are the owner's own trigger regexes and cheaper
        # than reading every decision card, so the write gate only sees calls no
        # card already stopped.
        try:
            write_value, notices = _write_review(
                event, tool_name, tool_input, config, started_at
            )
        except Exception:
            write_value, notices = None, ()
        if write_value is not None:
            return write_value
        return _allow_context(event, started_at, defects, notices)
    vault, card, fallback, position = match
    value = _bounded_deny(card)
    _best_effort_audit(
        _append_audit,
        vault,
        tool_name,
        card["name"],
        started_at,
        fallback,
        position,
    )
    return value


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-gate-") as temp_dir:
            root = Path(temp_dir).resolve()
            vault = root / "vault"
            vault.mkdir()
            card = vault / "safe-alternative.md"
            card.write_text(
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

            hit = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}},
                config,
            )
            value = json.loads(hit.stdout) if hit.stdout.strip() else {}
            output = value.get("hookSpecificOutput", {})
            reason = output.get("permissionDecisionReason", "")
            checks.append(
                (
                    "matching card denies with advice",
                    hit.returncode == 0
                    and output.get("permissionDecision") == "deny"
                    and "Use the read-only alternative." in reason
                    and str(card.resolve()) in reason,
                )
            )

            log_path = vault / memspec.GATE_LOG_FILENAME
            log_rows = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line
            ] if log_path.is_file() else []
            checks.append(
                (
                    "audit row persisted",
                    len(log_rows) == 1
                    and log_rows[0].get("tool") == "Bash"
                    and log_rows[0].get("card") == "synthetic-safety",
                )
            )

            audit_lock = Path(os.fspath(log_path) + ".lock")
            audit_lock.write_text("synthetic-busy\n", encoding="ascii")
            busy_audit = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}},
                config,
            )
            audit_lock.unlink(missing_ok=True)
            busy_value = json.loads(busy_audit.stdout) if busy_audit.stdout.strip() else {}
            checks.append((
                "matching deny survives a busy audit lock",
                busy_audit.returncode == 0
                and busy_value.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
            ))

            oversized_vault = root / "oversized-vault"
            oversized_vault.mkdir()
            (oversized_vault / "oversized.md").write_text(
                "---\nname: oversized\ntrigger: {tool: ^Read$, input: dangerous-target}\n"
                + "advice: " + ("安全替代方案" * 3000) + "\n---\n",
                encoding="utf-8",
            )
            oversized_config = root / "oversized-config.json"
            write_config(oversized_config, [oversized_vault])
            oversized = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "dangerous-target"}},
                oversized_config,
            )
            oversized_value = json.loads(oversized.stdout) if oversized.stdout.strip() else {}
            checks.append((
                "oversized advice is truncated without cancelling the deny",
                oversized.returncode == 0
                and oversized_value.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
                and len(oversized.stdout.encode("utf-8")) <= memspec.HOOK_MAX_OUTPUT_BYTES + 1,
            ))

            decision_vault = root / "decision-vault"
            decision_vault.mkdir()
            (decision_vault / "old.md").write_text(
                "---\nname: retired gate\nstatus: superseded\nsuperseded_by: current.md\n"
                "trigger: {tool: ^Read$, input: retired-target}\nadvice: OLD RULE\n...\n",
                encoding="utf-8",
            )
            (decision_vault / "current.md").write_text(
                "---\nname: current gate\nstatus: active\n"
                "trigger: {tool: ^Read$, input: current-target}\nadvice: CURRENT RULE\n...\n",
                encoding="utf-8",
            )
            decision_config = root / "decision-config.json"
            write_config(decision_config, [decision_vault])
            retired = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "retired-target"}},
                decision_config,
            )
            current = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "current-target"}},
                decision_config,
            )
            current_value = json.loads(current.stdout) if current.stdout.strip() else {}
            checks.append((
                "only the active decision card can gate and YAML end markers agree",
                retired.returncode == 0
                and not retired.stdout.strip()
                and current.returncode == 0
                and current_value.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
            ))

            unsafe_card = root / "unsafe-regex.md"
            unsafe_card.write_text(
                "---\nname: unsafe regex\ntrigger: {tool: ^Read$, input: '(a+)+$'}\n"
                "advice: Never evaluate catastrophic backtracking.\n---\n",
                encoding="utf-8",
            )
            try:
                _parse_trigger_card(unsafe_card)
                unsafe_rejected = False
            except ValueError:
                unsafe_rejected = True
            checks.append(("ambiguous repeated regex is rejected before matching", unsafe_rejected))

            def regex_accepted(pattern):
                try:
                    _compile_trigger_regex(pattern)
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
                and not regex_accepted("a" * (memspec.TRIGGER_REGEX_MAX_CHARS + 1)),
            ))

            cache_vault = root / "cache-vault"
            cache_vault.mkdir()
            for index in range(40):
                (cache_vault / f"plain-{index:02d}.md").write_text(
                    f"---\nname: plain {index}\ndescription: no trigger here\n---\nbody {index}\n",
                    encoding="utf-8",
                )
            (cache_vault / "guard.md").write_text(
                "---\nname: guard\ntrigger: {tool: ^Bash$, input: 'cachedeny'}\n"
                "advice: cached trigger card denies.\n---\n",
                encoding="utf-8",
            )
            cache_config = root / "cache-config.json"
            write_config(cache_config, [cache_vault])
            parse_calls = []
            original_parse = _parse_trigger_card
            globals()["_parse_trigger_card"] = lambda path: parse_calls.append(path) or original_parse(path)
            previous_config = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
            os.environ[memspec.EPITYPE_CONFIG_ENV] = os.fspath(cache_config)
            try:
                def gate(command, session="cache-session"):
                    return _handle(
                        {"session_id": session, "tool_name": "Bash", "tool_input": {"command": command}},
                        time.monotonic(),
                    )

                def decision(value):
                    return (value or {}).get("hookSpecificOutput", {}).get("permissionDecision")

                first = decision(gate("echo cachedeny"))
                first_parses = len(parse_calls)
                cache_file = cache_vault / memspec.FTS_INDEX_DIRECTORY / _TRIGGER_CACHE_FILENAME
                second = decision(gate("echo cachedeny"))
                checks.append((
                    "trigger cards come from a manifest cache: only trigger cards are parsed per call",
                    first == "deny" and second == "deny" and first_parses == 1
                    and len(parse_calls) == 2 and cache_file.is_file(),
                ))
                (cache_vault / "late.md").write_text(
                    "---\nname: late\ntrigger: {tool: ^Bash$, input: 'latedeny'}\n"
                    "advice: a card added later denies too.\n---\n",
                    encoding="utf-8",
                )
                late = decision(gate("echo latedeny"))
                (cache_vault / "guard.md").write_text(
                    "---\nname: guard\ndescription: trigger removed\n---\n", encoding="utf-8"
                )
                removed = decision(gate("echo cachedeny"))
                checks.append((
                    "a changed manifest re-reads only the changed cards: added trigger denies, removed one allows",
                    late == "deny" and removed is None,
                ))
                cache_file.write_text("{not json", encoding="utf-8")
                corrupted = decision(gate("echo latedeny"))
                checks.append((
                    "an unreadable trigger cache is rebuilt, never trusted",
                    corrupted == "deny" and json.loads(cache_file.read_text(encoding="utf-8"))["version"] == _TRIGGER_CACHE_VERSION,
                ))
                (cache_vault / "broken-trigger.md").write_text(
                    "---\nname: broken-trigger\ntrigger: {tool: ^Bash$, input: '(a+)+$'}\n"
                    "advice: never used.\n---\n",
                    encoding="utf-8",
                )
                defect_session = f"defect-{uuid.uuid4().hex}"
                noticed = gate("echo harmless", session=defect_session)
                noticed_text = (noticed or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
                repeated = gate("echo harmless", session=defect_session)
                checks.append((
                    "an unusable trigger card is named to the model once per session, not silently skipped",
                    "broken-trigger" in noticed_text
                    and memspec.GATE_DEFECT_NOTICE[:12] in noticed_text
                    and decision(noticed) is None
                    and repeated is None,
                ))
            finally:
                globals()["_parse_trigger_card"] = original_parse
                if previous_config is None:
                    os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None)
                else:
                    os.environ[memspec.EPITYPE_CONFIG_ENV] = previous_config

            def transcript_rows(*rows):
                path = root / f"narration-{uuid.uuid4().hex}.jsonl"
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
            narration_session = "narration-" + uuid.uuid4().hex
            flagged = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "synthetic.txt"}, "transcript_path": narrated, "session_id": narration_session},
                config,
            )
            flagged_value = json.loads(flagged.stdout) if flagged.stdout.strip() else {}
            flagged_output = flagged_value.get("hookSpecificOutput", {})
            checks.append(
                (
                    "narration between tool calls is named without touching the permission decision",
                    flagged.returncode == 0
                    and flagged_output.get("additionalContext", "").startswith(memspec.NARRATION_PREFIX)
                    and memspec.NARRATION_ADVICE in flagged_output.get("additionalContext", "")
                    and "permissionDecision" not in flagged_output,
                )
            )
            repeat = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "synthetic.txt"}, "transcript_path": narrated, "session_id": narration_session},
                config,
            )
            checks.append(("same narration is flagged once per session", repeat.returncode == 0 and not repeat.stdout.strip()))
            opening = transcript_rows(prompt_row, assistant("text", "先看檔案再改，這是開工說明。"))
            clean = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "synthetic.txt"}, "transcript_path": opening, "session_id": uuid.uuid4().hex},
                config,
            )
            checks.append(("opening line before the first tool call is not narration", clean.returncode == 0 and not clean.stdout.strip()))
            deny_first = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}, "transcript_path": narrated, "session_id": uuid.uuid4().hex},
                config,
            )
            deny_value = json.loads(deny_first.stdout) if deny_first.stdout.strip() else {}
            checks.append(
                (
                    "a matching card still denies ahead of narration context",
                    deny_value.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
                    and "additionalContext" not in deny_value.get("hookSpecificOutput", {}),
                )
            )

            sweep_root = Path(tempfile.gettempdir()) / memspec.NARRATION_MARKER_DIRECTORY
            aged_session = sweep_root / ("aged-" + uuid.uuid4().hex)
            aged_session.mkdir(parents=True, exist_ok=True)
            aged_marker = aged_session / "0123456789abcdef"
            aged_marker.write_text("aged\n", encoding="ascii")
            aged_time = time.time() - memspec.NARRATION_MARKER_TTL_SECONDS - 60
            os.utime(aged_marker, (aged_time, aged_time))
            run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "synthetic.txt"}, "transcript_path": narrated, "session_id": uuid.uuid4().hex},
                config,
            )
            checks.append(
                (
                    "aged narration markers are swept instead of accumulating",
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
                    "no match allows silently",
                    miss.returncode == 0 and not miss.stdout and not miss.stderr,
                )
            )

            (vault / "broken.md").write_text(
                "---\n"
                "name: broken-synthetic\n"
                "trigger:\n"
                "  tool: [\n"
                "  input: remove\n"
                "advice: Alternative.\n"
                "---\n",
                encoding="utf-8",
            )
            broken = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}},
                config,
            )
            broken_value = json.loads(broken.stdout) if broken.stdout.strip() else {}
            broken_output = broken_value.get("hookSpecificOutput", {})
            checks.append(
                (
                    "bad card is isolated from matching good card",
                    broken.returncode == 0
                    and broken_output.get("permissionDecision") == "deny"
                    and "Use the read-only alternative."
                    in broken_output.get("permissionDecisionReason", ""),
                )
            )

            log_rows = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            checks.append(
                (
                    "bad card records parse defect",
                    any(
                        row.get("kind") == "parse_defect"
                        and row.get("filename") == "broken.md"
                        and row.get("reason")
                        for row in log_rows
                    ),
                )
            )

            regex_vault = root / "regex-vault"
            regex_vault.mkdir()
            regex_card = regex_vault / "regex-synthetic.md"
            regex_card.write_text(
                "---\n"
                "name: regex-synthetic\n"
                "trigger:\n"
                "  tool: ^Read$\n"
                '  input: "auth\\.json"\n'
                "advice: Keep credential material unread.\n"
                "---\n",
                encoding="utf-8",
            )
            regex_config = root / "regex-config.json"
            write_config(regex_config, [regex_vault])
            regex_hit = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "auth.json"}},
                regex_config,
            )
            regex_value = (
                json.loads(regex_hit.stdout) if regex_hit.stdout.strip() else {}
            )
            regex_output = regex_value.get("hookSpecificOutput", {})
            checks.append(
                (
                    "Read path card keeps fulltext matching",
                    regex_hit.returncode == 0
                    and regex_output.get("permissionDecision") == "deny"
                    and "Keep credential material unread."
                    in regex_output.get("permissionDecisionReason", ""),
                )
            )

            command_vault = root / "command-vault"
            command_vault.mkdir()
            card_specs = (
                ("argument-marker", "", r"x37c7c1b07e7f", "Block the synthetic argument marker."),
                ("kill-host", "", r"(?i)(?:taskkill\b.*\b(?:claude|codex)(?:\.exe)?\b|Stop-Process\b.*\b(?:claude|codex)\b)", "Keep the host process running."),
                ("destructive-git", "", r"(?i)git\b.*\breset\b.*--hard\b", "Preserve the working tree."),
                ("credential-read", "match: fulltext, ", r"(?i)(?:\.env|auth\.json)", "Keep credential material unread."),
                ("heredoc-backslash", "match: fulltext, ", r"(?s)<<.*\\\\", "Keep backslashes out of heredoc input."),
            )
            for name, match_field, pattern, advice in card_specs:
                (command_vault / f"{name}.md").write_text(
                    f"---\nname: {name}\ntrigger: {{tool: ^(?:Bash|Shell|shell_command)$, {match_field}input: '{pattern}'}}\nadvice: {advice}\n---\n",
                    encoding="utf-8",
                )
            command_config = root / "command-config.json"
            write_config(command_config, [command_vault])

            def command_result(command, tool_name="Bash"):
                result = run_synthetic(
                    Path(__file__),
                    {"tool_name": tool_name, "tool_input": {"command": command}},
                    command_config,
                )
                value = json.loads(result.stdout) if result.stdout.strip() else {}
                decision = value.get("hookSpecificOutput", {}).get("permissionDecision")
                return result, decision

            command_cases = (
                ("Python heredoc prose allows", "python - <<'PY'\npayload = {'lesson': 'taskkill /IM claude.exe'}\nprint(payload)\nPY\n", None),
                ("cat heredoc prose allows", "cat <<'EOF'\ntaskkill /IM claude.exe\nEOF\n", None),
                ("quoted command mention allows", 'echo "taskkill /IM claude.exe"', None),
                ("Python prose write allows", 'python -c "open(\'ledger.txt\', \'w\').write(\'taskkill /IM claude.exe\')"', None),
                ("interpreter -e prose allows", 'node -e "console.log(\'taskkill /IM claude.exe\')"', None),
                ("comment mention allows", "echo safe # taskkill /IM claude.exe", None),
                ("fourth unquoted argument denies", "run prohibited synthetic action x37c7c1b07e7f", "deny"),
                ("double-quoted argument marker allows", 'echo "prose x37c7c1b07e7f"', None),
                ("single-quoted argument marker allows", "echo 'prose x37c7c1b07e7f'", None),
                ("executable position denies", "taskkill /IM claude.exe", "deny"),
                ("unquoted codex taskkill arguments deny", "taskkill /IM codex.exe /F", "deny"),
                ("later Stop-Process name argument denies", "Stop-Process -Id 4242 -Name codex", "deny"),
                ("Stop-Process comment mention allows", "Stop-Process -Id 4242 # codex", None),
                ("second chained segment denies", "echo safe && taskkill /IM claude.exe", "deny"),
                ("cmd wrapper denies", 'cmd /c "taskkill /IM claude.exe"', "deny"),
                ("cmd wrapper with codex denies", 'cmd /c "taskkill /IM codex.exe"', "deny"),
                ("PowerShell wrapper denies", 'powershell -Command "Stop-Process -Name claude"', "deny"),
                ("bash wrapper denies", "bash -c 'taskkill /IM claude.exe'", "deny"),
                ("Python os.system wrapper denies", 'python -c "import os; os.system(\'taskkill /IM claude.exe\')"', "deny"),
                ("interpreter -e exec wrapper denies", 'node -e "exec(\'taskkill /IM claude.exe\')"', "deny"),
                ("path and extension normalization denies", r"C:\Windows\System32\taskkill.exe /IM claude.exe", "deny"),
                ("env assignment and sudo wrapper deny", "MODE=safe sudo taskkill /IM claude.exe", "deny"),
                ("destructive git original case denies", "git reset --hard", "deny"),
                ("credential path original case denies", "Get-Content .env", "deny"),
                ("explicit fulltext denies a prose mention", 'echo "auth.json"', "deny"),
                ("explicit fulltext sees heredoc backslashes", "python - <<'PY'\np = 'C:\\Users\\x'\nPY\n", "deny"),
            )
            for name, command, expected in command_cases:
                result, decision = command_result(command)
                checks.append((name, result.returncode == 0 and decision == expected))

            alias_results = [
                command_result('echo "taskkill /IM claude.exe"', tool_name)[1]
                for tool_name in ("Shell", "shell_command")
            ]
            checks.append((
                "documented shell aliases use command-aware quoted-text handling",
                alias_results == [None, None],
            ))

            malformed, malformed_decision = command_result(
                'echo "taskkill /IM claude.exe'
            )
            checks.append((
                "unbalanced quote falls back and denies",
                malformed.returncode == 0 and malformed_decision == "deny",
            ))
            command_log = command_vault / memspec.GATE_LOG_FILENAME
            command_rows = [
                json.loads(line)
                for line in command_log.read_text(encoding="utf-8").splitlines()
                if line
            ]
            checks.append(("fallback audit is explicit", any(
                row.get("card") == "kill-host" and row.get("fallback") == "fulltext"
                for row in command_rows
            )))
            checks.append(("audit identifies all command positions", {
                "executable", "argument", "wrapper"
            }.issubset({row.get("position") for row in command_rows})))

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
                result = run_synthetic(Path(__file__), event, write_config_path)
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
                    f"{memspec.ALIASES_FIELD}: [alias only in english]\n"
                    "metadata:\n  type: reference\n---\nbody\n",
                },
            )
            checks.append((
                "WARN 級只在 additionalContext 提示，不擋寫入",
                _warn_result.returncode == 0
                and "permissionDecision" not in warn_out
                and memspec.WRITE_GATE_CARD_ADVICE[:6] in warn_out.get("additionalContext", "")
                and "reference-dated.md" in warn_out.get("additionalContext", ""),
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
    total = 65
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
