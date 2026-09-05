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
    run_synthetic,
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
    previous_repeat = False
    for opcode, argument in items:
        if opcode in _REPEAT_OPS:
            if inside_repeat or previous_repeat:
                raise ValueError("trigger regex has ambiguous repetition")
            _validate_regex_tree(argument[2], inside_repeat=True)
            previous_repeat = True
            continue
        previous_repeat = False
        if opcode == _re_constants.BRANCH:
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
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        if not isinstance(decoded, str):
            raise ValueError("frontmatter scalar must be text")
        return decoded
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
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
    text = path.read_text(encoding="utf-8")
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in ("---", "..."):
            return lines[1:index]
    raise ValueError("unterminated frontmatter")


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
    component = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or "nosession"))[:128] or "nosession"
    root = Path(tempfile.gettempdir()) / memspec.NARRATION_MARKER_DIRECTORY
    directory = root / component
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
    for vault in config[memspec.CONFIG_VAULTS_FIELD]:
        for path in memsearch.card_files(vault):
            if expired(started_at):
                return None
            try:
                card = _parse_trigger_card(path)
            except Exception as exc:
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
        return _narration_context(event, started_at)
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
    total = 46
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
