import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude PreToolUse adapter for fail-open, card-driven safety advice."""

from datetime import datetime, timezone
import ast
import json
import os
from pathlib import Path
import re
import shlex
import tempfile

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import memspec, telemetry
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
        if line.strip() == "---":
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
            elif key == "name":
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
    if match_mode not in ("", memspec.TRIGGER_COMMAND_MATCH):
        raise ValueError("trigger match must be command when present")
    tool_regex = re.compile(tool_pattern)
    input_regex = re.compile(input_pattern)
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
        current.append(value)
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


def _command_candidates(command, depth=0):
    if depth > 8:
        raise _CommandParseError("command wrapper nesting too deep")
    candidates = []
    for tokens in _shell_segments(command):
        index = 0
        while index < len(tokens):
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
                index += 1
                continue
            executable = _executable_name(tokens[index]).lower()
            if executable in ("&", "call", "command", "exec", "nohup"):
                index += 1
                continue
            if executable == "env":
                index += 1
                while index < len(tokens) and (
                    tokens[index].startswith("-")
                    or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index])
                ):
                    index += 1
                continue
            if executable == "sudo":
                index += 1
                value_options = {"-u", "--user", "-g", "--group", "-h", "--host"}
                while index < len(tokens) and tokens[index].startswith("-"):
                    option = tokens[index].split("=", 1)[0]
                    index += 1
                    if option in value_options and index < len(tokens):
                        index += 1
                continue
            break
        if index >= len(tokens):
            continue

        executable = _executable_name(tokens[index])
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
            for flag_index, value in enumerate(arguments):
                if value.lower() in wrapper_flags[lowered]:
                    payload = " ".join(arguments[flag_index + 1 :])
                    if not payload:
                        raise _CommandParseError("shell wrapper lacks command string")
                    candidates.extend(_command_candidates(payload, depth + 1))
                    break

        if lowered == "py" or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", lowered):
            for flag_index, value in enumerate(arguments):
                if value.lower() == "-c":
                    if flag_index + 1 >= len(arguments):
                        raise _CommandParseError("Python wrapper lacks command string")
                    candidates.extend(
                        _python_embedded_commands(arguments[flag_index + 1], depth)
                    )
                    break

        script_flags = {
            "node": {"-e", "--eval"},
            "ruby": {"-e"},
            "perl": {"-e"},
        }
        if lowered in script_flags:
            for flag_index, value in enumerate(arguments):
                if value.lower() in script_flags[lowered]:
                    if flag_index + 1 >= len(arguments):
                        raise _CommandParseError("interpreter lacks command string")
                    candidates.extend(
                        _python_embedded_commands(arguments[flag_index + 1], depth)
                    )
                    break

        visible_arguments = [value for value in arguments if not value.startswith((">", "<"))]
        candidates.append(" ".join((executable, *visible_arguments)).strip())
    return candidates


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


def _append_audit(vault, tool_name, card_name, started_at, fallback=None):
    row = {"tool": tool_name, "card": card_name}
    if fallback is not None:
        row["fallback"] = fallback
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


def _handle(event, started_at, metrics=None):
    metrics = metrics if metrics is not None else {}
    metrics.update(vaults=0, vault_skipped=0, fail_open=False, outcome="allow", reason="no-match")
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
    try:
        config = load_config(started_at)
    except Exception:
        metrics.update(fail_open=True, outcome="error", reason="config")
        return None
    if config is None:
        metrics.update(fail_open=True, outcome="timeout", reason="timeout")
        return None
    metrics["vaults"] = len(config[memspec.CONFIG_VAULTS_FIELD])

    cards = []
    for vault in config[memspec.CONFIG_VAULTS_FIELD]:
        for path in sorted(vault.rglob("*.md"), key=lambda item: str(item).casefold()):
            if expired(started_at):
                metrics.update(fail_open=True, outcome="timeout", reason="timeout")
                return None
            try:
                card = _parse_trigger_card(path)
            except Exception as exc:
                _append_parse_defect(vault, path, exc, started_at)
                continue
            if card is not None:
                cards.append((vault, card))

    match = None
    command_state = None
    for vault, card in cards:
        if not card["tool_regex"].search(tool_name):
            continue
        fallback = None
        if card["match_mode"] == memspec.TRIGGER_COMMAND_MATCH:
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
                input_matches = any(
                    card["input_regex"].match(candidate) for candidate in candidates
                )
        else:
            input_matches = card["input_regex"].search(tool_input_text) is not None
        if input_matches:
            match = (vault, card, fallback)
            break
    if match is None:
        return None
    if expired(started_at):
        metrics.update(fail_open=True, outcome="timeout", reason="timeout")
        return None

    vault, card, fallback = match
    _append_audit(vault, tool_name, card["name"], started_at, fallback)
    if expired(started_at):
        metrics.update(fail_open=True, outcome="timeout", reason="timeout")
        return None
    reason = f"{card['advice']} [{card['path']}]"
    value = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    if len(encode_payload(value).encode("utf-8")) > memspec.HOOK_MAX_OUTPUT_BYTES:
        metrics.update(fail_open=True, outcome="error", reason="exception")
        return None
    metrics["reason"] = "card-deny"
    return value


def _process(event, started_at, host, *, home=None):
    metrics = {}
    value = _handle(event, started_at, metrics)
    elapsed_ms = max(0, int((time.monotonic() - started_at) * 1000))
    if value is not None:
        telemetry.append(
            host,
            "PreToolUse",
            "deny",
            hits=1,
            injected_bytes=len(encode_payload(value).encode("utf-8")),
            vaults=metrics["vaults"],
            ms=elapsed_ms,
            reason="card-deny",
            home=home,
        )
        return value, False
    if metrics["fail_open"]:
        telemetry.append(
            host,
            "PreToolUse",
            metrics["outcome"],
            vaults=metrics["vaults"],
            vault_skipped=metrics["vault_skipped"],
            ms=elapsed_ms,
            reason=metrics["reason"],
            home=home,
        )
        return None, False
    return None, telemetry.heartbeat(host, "PreToolUse", home=home)


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-gate-") as temp_dir:
            root = Path(temp_dir)
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
                    "quoted regex scalar participates in matching",
                    regex_hit.returncode == 0
                    and regex_output.get("permissionDecision") == "deny"
                    and "Keep credential material unread."
                    in regex_output.get("permissionDecisionReason", ""),
                )
            )

            command_vault = root / "command-vault"
            command_vault.mkdir()
            card_specs = (
                ("kill-host", "match: command, ", r"(?i)(?:taskkill\b.*\bclaude(?:\.exe)?\b|Stop-Process\b.*\bclaude\b)", "Keep the host process running."),
                ("destructive-git", "match: command, ", r"(?i)git\b.*\breset\b.*--hard\b", "Preserve the working tree."),
                ("credential-read", "", r"(?i)(?:\.env|auth\.json)", "Keep credential material unread."),
            )
            for name, match_field, pattern, advice in card_specs:
                (command_vault / f"{name}.md").write_text(
                    f"---\nname: {name}\ntrigger: {{tool: ^Bash$, {match_field}input: '{pattern}'}}\nadvice: {advice}\n---\n",
                    encoding="utf-8",
                )
            command_config = root / "command-config.json"
            write_config(command_config, [command_vault])

            def command_result(command):
                result = run_synthetic(
                    Path(__file__),
                    {"tool_name": "Bash", "tool_input": {"command": command}},
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
                ("executable position denies", "taskkill /IM claude.exe", "deny"),
                ("second chained segment denies", "echo safe && taskkill /IM claude.exe", "deny"),
                ("cmd wrapper denies", 'cmd /c "taskkill /IM claude.exe"', "deny"),
                ("PowerShell wrapper denies", 'powershell -Command "Stop-Process -Name claude"', "deny"),
                ("bash wrapper denies", "bash -c 'taskkill /IM claude.exe'", "deny"),
                ("Python os.system wrapper denies", 'python -c "import os; os.system(\'taskkill /IM claude.exe\')"', "deny"),
                ("interpreter -e exec wrapper denies", 'node -e "exec(\'taskkill /IM claude.exe\')"', "deny"),
                ("path and extension normalization denies", r"C:\Windows\System32\taskkill.exe /IM claude.exe", "deny"),
                ("env assignment and sudo wrapper deny", "MODE=safe sudo taskkill /IM claude.exe", "deny"),
                ("destructive git original case denies", "git reset --hard", "deny"),
                ("credential path original case denies", "Get-Content .env", "deny"),
            )
            for name, command, expected in command_cases:
                result, decision = command_result(command)
                checks.append((name, result.returncode == 0 and decision == expected))

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

            missing_config = root / "missing-config.json"
            missing = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}},
                missing_config,
            )
            missing_records = telemetry.read_records(home=root / "synthetic-home")
            checks.append(
                (
                    "missing config infra failure allows silently",
                    missing.returncode == 0
                    and not missing.stdout
                    and not missing.stderr
                    and missing_records[-1]["outcome"] == "error"
                    and missing_records[-1]["reason"] == "config",
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 26
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main():
    arguments = sys.argv[1:]
    if "--selftest" in arguments:
        return _selftest()
    host = telemetry.host_from_argv(arguments)
    try:
        event = read_event(sys.stdin)
        value, _ = _process(event, _STARTED_AT, host)
        if value is not None and not expired(_STARTED_AT):
            emit(value)
        elif value is not None:
            telemetry.append(
                host,
                "PreToolUse",
                "timeout",
                ms=max(0, int((time.monotonic() - _STARTED_AT) * 1000)),
                reason="timeout",
            )
    except Exception:
        telemetry.append(
            host,
            "PreToolUse",
            "error",
            ms=max(0, int((time.monotonic() - _STARTED_AT) * 1000)),
            reason="exception",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
