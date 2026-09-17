import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # Keep cp950 consoles deterministic.
"""Install, inspect, or precisely remove Epitype hook registrations."""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[1]
MARKER_VALUE = "epitype"
MARKER_FIELDS = ("id", "comment")
EVENTS = ("SessionStart", "UserPromptSubmit", "PreCompact", "PreToolUse", "Stop")
STATE_VERSION = 1
CONFIG_DIRECTORY = ".epitype"
CONFIG_FILENAME = "config.json"
STATE_FILENAME = "install_state.json"
SHIM_STATUS_FILENAME = "shim_status.json"
HOOK_DIRECTORY = "hooks"
BACKUP_INFIX = ".bak_epitype_"
BACKUPS_KEPT = 3
FALLBACK_VAULT = ".epitype-vault"
WORK_LEDGER_FILENAME = "_WORK_LEDGER.md"
# 夢的排程常數在這裡再寫一份：安裝器必須在 epitype 套件還不能 import 的機器上跑，
# 所以它不 import memspec（既有的 budget_bytes 也是同樣理由）。漂移由 selftest 逐項
# 比對 memspec 擋下。
DREAM_CONFIG_FIELD = "dream"
DREAM_MODE_FIELD = "mode"
DREAM_INTERVAL_HOURS_FIELD = "interval_hours"
DREAM_AT_FIELD = "at"
DREAM_MODES = ("piggyback", "nightly", "off")
DREAM_DEFAULT_MODE = "piggyback"
DREAM_DEFAULT_INTERVAL_HOURS = 24
DREAM_DEFAULT_AT = "03:30"
DREAM_AT_REGEX = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
DREAM_DIRECTORY = ".epitype"
DREAM_STATE_FILENAME = "dream_state.json"
DREAM_SCHEDULED_FLAG = "--scheduled"
DREAM_MODE_ENV = "EPITYPE_DREAM_MODE"
DREAM_SCRIPT_PARTS = ("epitype", "dream.py")
DREAM_TASK_NAME = r"Epitype\Dream"             # schtasks /TN
DREAM_CRON_MARKER = "# epitype-dream"          # crontab 只認自己這一行
CRONTAB_UNREADABLE_WARN = "WARN nightly dream {action} skipped: crontab -l unreadable, existing crontab left untouched"
SHIM_ADAPTER_TOKEN = "__EPITYPE_ADAPTER_FILENAME__"
SHIM_TRACE_ENV = "EPITYPE_SHIM_TRACE"
HOOK_SPECS = {
    "SessionStart": ("sessionstart.py", "sessionstart_hook.py"),
    "UserPromptSubmit": ("recall.py", "recall_hook.py"),
    "PreCompact": ("precompact.py", "precompact_hook.py"),
    "PreToolUse": ("pretooluse.py", "pretooluse_gate.py"),
    "Stop": ("stop.py", "stop_gate.py"),
}
SHIM_NAMES = tuple(shim_name for shim_name, _ in HOOK_SPECS.values())
# Re-pinned when the Stop shim joined the set: the template's own SHIM_NAMES tuple is
# what lets one shim preserve another's fail-open breadcrumb, so adding stop.py there
# changed every rendered shim's bytes. Any other drift is still a selftest failure.
SHIM_SHA256 = {
    "sessionstart.py": "7a3463ab75febddd012bb8fd0d6b4870775ca1366616d43737cd631c22225792",
    "recall.py": "41602ee307220427decd7158c0bbb77c2463d377039709059f8eb4cdee8f6db3",
    "precompact.py": "d5cd5572658ef2a832a32bc6588a97e7a69711d835593ee5ef6855426a99025b",
    "pretooluse.py": "334da4668c893cb05bdeba142adacd272d63da2fc7dde9b1590e03e9f1916d27",
    "stop.py": "11c7eabf78e710fa8705b8eb8774de11781f0dacfe98e3f2152d85c2d0cef582",
}
SHIM_REASON_CODES = frozenset((
    "config_missing",
    "config_unreadable",
    "repo_root_missing",
    "repo_root_not_dir",
    "adapter_missing",
    "exception",
))
NATIVE_DISABLE_PATTERN = re.compile(
    r"(?:disable(?:d)?[^\r\n]{0,64}(?:memory|recall|history)|"
    r"(?:memory|recall|history)[^\r\n]{0,64}disable(?:d)?)",
    re.IGNORECASE,
)


class InstallError(RuntimeError):
    pass


@dataclass
class JsonMember:
    key: str
    start: int
    end: int
    value: "JsonNode"


@dataclass
class JsonNode:
    kind: str
    start: int
    end: int
    value: object = None
    members: list = field(default_factory=list)
    items: list = field(default_factory=list)


class JsonSpanParser:
    """Small JSON parser that keeps source spans for byte-preserving surgery."""

    def __init__(self, text):
        self.text = text
        self.length = len(text)
        self.decoder = json.JSONDecoder()

    def _space(self, position):
        while position < self.length and self.text[position] in " \t\r\n":
            position += 1
        return position

    def parse(self):
        position = self._space(0)
        node, position = self._value(position)
        if self._space(position) != self.length:
            raise ValueError("trailing JSON content")
        return node

    def _value(self, position):
        if position >= self.length:
            raise ValueError("unexpected end of JSON")
        marker = self.text[position]
        if marker == "{":
            return self._object(position)
        if marker == "[":
            return self._array(position)
        try:
            value, end = self.decoder.raw_decode(self.text, position)
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc
        kind = "string" if isinstance(value, str) else "scalar"
        return JsonNode(kind, position, end, value=value), end

    def _object(self, position):
        start = position
        position = self._space(position + 1)
        members = []
        if position < self.length and self.text[position] == "}":
            return JsonNode("object", start, position + 1, members=members), position + 1
        while True:
            member_start = position
            key_node, position = self._value(position)
            if key_node.kind != "string":
                raise ValueError("JSON object key must be a string")
            position = self._space(position)
            if position >= self.length or self.text[position] != ":":
                raise ValueError("missing JSON object colon")
            position = self._space(position + 1)
            value, position = self._value(position)
            members.append(JsonMember(key_node.value, member_start, value.end, value))
            position = self._space(position)
            if position >= self.length:
                raise ValueError("unterminated JSON object")
            if self.text[position] == "}":
                return JsonNode(
                    "object",
                    start,
                    position + 1,
                    members=members,
                ), position + 1
            if self.text[position] != ",":
                raise ValueError("missing JSON object comma")
            position = self._space(position + 1)

    def _array(self, position):
        start = position
        position = self._space(position + 1)
        items = []
        if position < self.length and self.text[position] == "]":
            return JsonNode("array", start, position + 1, items=items), position + 1
        while True:
            item, position = self._value(position)
            items.append(item)
            position = self._space(position)
            if position >= self.length:
                raise ValueError("unterminated JSON array")
            if self.text[position] == "]":
                return JsonNode("array", start, position + 1, items=items), position + 1
            if self.text[position] != ",":
                raise ValueError("missing JSON array comma")
            position = self._space(position + 1)


def _parse_json(text):
    node = JsonSpanParser(text).parse()
    if node.kind != "object":
        raise InstallError("JSON root must be an object")
    return node


def _member(node, key):
    matches = [item for item in node.members if item.key == key]
    if len(matches) > 1:
        raise InstallError(f"duplicate JSON key is unsafe to edit: {key}")
    return matches[0] if matches else None


def _line_indent(text, position):
    newline = max(text.rfind("\n", 0, position), text.rfind("\r", 0, position))
    prefix = text[newline + 1 : position]
    return prefix if prefix.strip() == "" else ""


def _newline(text):
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    if "\r" in text:
        return "\r"
    return os.linesep


def _render_json(value, pretty, indent, newline):
    if not pretty:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    return rendered.replace("\n", newline + indent)


def _append_object_member(text, node, key, value):
    if node.kind != "object":
        raise InstallError("target JSON value is not an object")
    pretty = "\n" in text or "\r" in text
    newline = _newline(text)
    close_position = node.end - 1
    close_indent = _line_indent(text, close_position)
    if node.members:
        member_indent = _line_indent(text, node.members[0].start) or close_indent + "  "
        position = node.members[-1].value.end
        prefix = "," + (newline + member_indent if pretty else "")
    else:
        member_indent = close_indent + "  "
        position = close_position
        prefix = ""
    rendered = _render_json(value, pretty, member_indent, newline)
    payload = json.dumps(key, ensure_ascii=False) + (": " if pretty else ":") + rendered
    return text[:position] + prefix + payload + text[position:]


def _append_array_item(text, node, value):
    if node.kind != "array":
        raise InstallError("hook event value must be an array")
    pretty = "\n" in text[node.start : node.end] or "\r" in text[node.start : node.end]
    newline = _newline(text)
    close_position = node.end - 1
    close_indent = _line_indent(text, close_position)
    if node.items:
        item_indent = _line_indent(text, node.items[0].start) or close_indent + "  "
        position = node.items[-1].end
        prefix = "," + (newline + item_indent if pretty else "")
    else:
        item_indent = close_indent + "  "
        position = close_position
        prefix = ""
    rendered = _render_json(value, pretty, item_indent, newline)
    return text[:position] + prefix + rendered + text[position:]


def _remove_ranges(text, ranges):
    for start, end in sorted(ranges, reverse=True):
        text = text[:start] + text[end:]
    return text


def _grouped_indices(indices):
    groups = []
    for index in sorted(indices):
        if groups and index == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], index)
        else:
            groups.append((index, index))
    return groups


def _remove_array_items(text, node, indices):
    ranges = []
    count = len(node.items)
    for first, last in _grouped_indices(indices):
        if first == 0 and last == count - 1:
            ranges.append((node.items[first].start, node.items[last].end))
        elif last < count - 1:
            ranges.append((node.items[first].start, node.items[last + 1].start))
        else:
            ranges.append((node.items[first - 1].end, node.items[last].end))
    return _remove_ranges(text, ranges)


def _remove_object_member(text, node, index):
    count = len(node.members)
    if count == 1:
        start = node.members[index].start
        end = node.members[index].end
    elif index < count - 1:
        start = node.members[index].start
        end = node.members[index + 1].start
    else:
        start = node.members[index - 1].value.end
        end = node.members[index].end
    return text[:start] + text[end:]


def _marked(value):
    return isinstance(value, dict) and any(
        value.get(field_name) == MARKER_VALUE for field_name in MARKER_FIELDS
    )


def _replace_array_item(text, node, index, value):
    item = node.items[index]
    pretty = "\n" in text[node.start : node.end] or "\r" in text[node.start : node.end]
    indent = _line_indent(text, item.start)
    rendered = _render_json(value, pretty, indent, _newline(text))
    return text[: item.start] + rendered + text[item.end :]


def _hook_template(codex, hooks_root, repo_root=REPO_ROOT, python_executable=None):
    path = repo_root / "adapters" / "codex" / "hooks_template.json"
    value = json.loads(path.read_bytes().decode("utf-8-sig"))
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        raise InstallError("Codex hook template has no hooks object")
    hooks_text = hooks_root.resolve().as_posix()
    python_text = Path(python_executable or sys.executable).resolve().as_posix()
    if '"' in python_text:
        raise InstallError("Python executable path cannot contain a double quote")
    result = {}
    for event in EVENTS:
        entries = hooks.get(event)
        if not isinstance(entries, list) or len(entries) != 1:
            raise InstallError(f"Codex hook template must contain one {event} entry")
        entry = json.loads(json.dumps(entries[0]))
        entry["id"] = MARKER_VALUE
        commands = entry.get("hooks")
        if not isinstance(commands, list):
            raise InstallError(f"Codex hook template {event} entry is malformed")
        for command in commands:
            raw = command.get("command")
            if not isinstance(raw, str):
                raise InstallError(f"Codex hook template {event} command is malformed")
            raw = _SHIM_PLACEHOLDER.sub(lambda match: _shell_token(hooks_text + match.group(1)), raw)
            raw = raw.replace("{{PYTHON_EXECUTABLE}}", _shell_token(python_text))
            if not codex:
                raw = raw.replace(" --codex", "")
            command["command"] = raw
            command.pop("commandWindows", None)
            if '"' in raw:
                # Codex runs Windows hooks through `cmd.exe /C`, which drops the
                # first and last quote of a line that starts with one; an outer
                # pair of quotes is what survives that rule (2026-09-05: the
                # quoted form silently failed every Codex hook for a day).
                command["commandWindows"] = f'"{raw}"'
        result[event] = entry
    return result


_SHIM_PLACEHOLDER = re.compile(r"\{\{EPITYPE_HOOKS_ROOT\}\}(/\S+)")
_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9_./:\\-]+")


def _shell_token(text):
    """Quote a command token only when a shell needs it: an unquoted path is the
    one form cmd.exe, bash, and sh all read the same way."""
    return text if _PLAIN_TOKEN.fullmatch(text) else f'"{text}"'


def _merge_hooks(raw, entries):
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    json.loads(text)
    root = _parse_json(text)
    hooks_member = _member(root, "hooks")
    created_hooks = hooks_member is None
    created_events = []
    locations = []

    if hooks_member is None:
        text = _append_object_member(
            text,
            root,
            "hooks",
            {event: [entries[event]] for event in EVENTS},
        )
        created_events.extend(EVENTS)
        locations.extend(f"hooks.{event}[id=epitype]" for event in EVENTS)
    else:
        if hooks_member.value.kind != "object":
            raise InstallError("hooks must be a JSON object")
        for event in EVENTS:
            root = _parse_json(text)
            hooks_node = _member(root, "hooks").value
            event_member = _member(hooks_node, event)
            if event_member is None:
                text = _append_object_member(text, hooks_node, event, [entries[event]])
                created_events.append(event)
                locations.append(f"hooks.{event}[id=epitype]")
                continue
            if event_member.value.kind != "array":
                raise InstallError(f"hooks.{event} must be an array")
            values = [json.loads(text[item.start : item.end]) for item in event_member.value.items]
            marked_indices = [index for index, value in enumerate(values) if _marked(value)]
            if marked_indices:
                if len(marked_indices) == 1 and values[marked_indices[0]] == entries[event]:
                    continue
                if len(marked_indices) > 1:
                    text = _remove_array_items(text, event_member.value, marked_indices[1:])
                    root = _parse_json(text)
                    hooks_node = _member(root, "hooks").value
                    event_member = _member(hooks_node, event)
                    values = [json.loads(text[item.start : item.end]) for item in event_member.value.items]
                    marked_indices = [index for index, value in enumerate(values) if _marked(value)]
                text = _replace_array_item(text, event_member.value, marked_indices[0], entries[event])
                locations.append(f"hooks.{event}[id=epitype]")
                continue
            text = _append_array_item(text, event_member.value, entries[event])
            locations.append(f"hooks.{event}[id=epitype]")

    encoded = text.encode("utf-8")
    if bom:
        encoded = b"\xef\xbb\xbf" + encoded
    return encoded, created_hooks, created_events, locations


def _unmerge_hooks(raw, target_state):
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    json.loads(text)
    removed = []

    for event in EVENTS:
        root = _parse_json(text)
        hooks_member = _member(root, "hooks")
        if hooks_member is None or hooks_member.value.kind != "object":
            continue
        event_member = _member(hooks_member.value, event)
        if event_member is None or event_member.value.kind != "array":
            continue
        values = [json.loads(text[item.start : item.end]) for item in event_member.value.items]
        indices = [index for index, value in enumerate(values) if _marked(value)]
        if indices:
            text = _remove_array_items(text, event_member.value, indices)
            removed.append(f"hooks.{event}[id=epitype]")

    for event in target_state.get("created_events", ()):
        root = _parse_json(text)
        hooks_member = _member(root, "hooks")
        if hooks_member is None or hooks_member.value.kind != "object":
            continue
        event_members = hooks_member.value.members
        for index, member in enumerate(event_members):
            if member.key != event:
                continue
            value = json.loads(text[member.value.start : member.value.end])
            if value == []:
                text = _remove_object_member(text, hooks_member.value, index)
            break

    if target_state.get("created_hooks"):
        root = _parse_json(text)
        hooks_members = root.members
        for index, member in enumerate(hooks_members):
            if member.key != "hooks":
                continue
            value = json.loads(text[member.value.start : member.value.end])
            if value == {}:
                text = _remove_object_member(text, root, index)
            break

    encoded = text.encode("utf-8")
    if bom:
        encoded = b"\xef\xbb\xbf" + encoded
    return encoded, removed


def _is_empty_shell(raw):
    """拿掉我們的區塊之後，這個檔還剩下東西嗎。

    只認「頂層物件是空的」這一種，而且讀不動就一律回否——判斷不出來時留著檔案是安全的
    那一邊，刪掉別人的設定不是。
    """
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return False
    return value == {}


def _backup_name(path, timestamp):
    base = path.with_name(path.name + BACKUP_INFIX + timestamp)
    candidate = base
    counter = 2
    while candidate.exists():
        candidate = path.with_name(base.name + f"_{counter}")
        counter += 1
    return candidate


def _prune_backups(path, keep):
    """Every install, vault change, and relocation leaves a backup beside the
    edited file; only the newest few are worth keeping."""
    prefix = path.name + BACKUP_INFIX
    try:
        backups = sorted(
            (item for item in path.parent.iterdir() if item.name.startswith(prefix) and item.is_file()),
            key=lambda item: item.stat().st_mtime,
        )
        for stale in backups[:-keep] if keep > 0 else backups:
            stale.unlink()
    except OSError:
        pass


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".epitype_tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class Transaction:
    def __init__(self):
        self.timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.originals = {}
        self.created_directories = []
        self.backups = []
        self.changed = []

    def mkdir(self, path):
        missing = []
        cursor = path
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            directory.mkdir()
            self.created_directories.append(directory)

    def prepare(self, path):
        if path in self.originals:
            return
        self.originals[path] = path.read_bytes() if path.exists() else None
        if path.exists():
            backup = _backup_name(path, self.timestamp)
            shutil.copy2(path, backup)
            self.backups.append((path, backup))
            _prune_backups(path, keep=BACKUPS_KEPT)

    def write(self, path, data):
        current = path.read_bytes() if path.exists() else None
        if current == data:
            return False
        self.prepare(path)
        self.mkdir(path.parent)
        _atomic_write(path, data)
        if path not in self.changed:
            self.changed.append(path)
        return True

    def remove(self, path):
        """刪掉一個檔，跟 write 一樣先備份、一樣進得了 rollback。"""
        if not path.exists():
            return False
        self.prepare(path)
        path.unlink()
        if path not in self.changed:
            self.changed.append(path)
        return True

    def rollback(self):
        for path, original in reversed(tuple(self.originals.items())):
            if original is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(path, original)
        for directory in reversed(self.created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass


def _host_paths(home):
    return {
        "claude": home / ".claude" / "settings.json",
        "codex": home / ".codex" / "config.toml",
    }


def _detect_hosts(home):
    paths = _host_paths(home)
    return tuple(name for name, path in paths.items() if path.is_file())


def _detect_native_vaults(home, hosts):
    candidates = []
    if "claude" in hosts:
        candidates.extend((home / ".claude" / "memory", home / ".claude" / "memories"))
        projects = home / ".claude" / "projects"
        if projects.is_dir():
            candidates.extend(path / "memory" for path in projects.iterdir() if path.is_dir())
    if "codex" in hosts:
        candidates.append(home / ".codex" / "memories")
    result = []
    seen = set()
    for candidate in candidates:
        if not candidate.is_dir() or not any(path.is_file() for path in candidate.rglob("*.md")):
            continue
        resolved = candidate.resolve()
        key = os.path.normcase(os.fspath(resolved))
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return sorted(result, key=lambda item: os.path.normcase(os.fspath(item)))


def _set_object_member(text, key, value):
    root = _parse_json(text)
    member = _member(root, key)
    if member is None:
        return _append_object_member(text, root, key, value)
    if json.loads(text[member.value.start : member.value.end]) == value:
        return text
    pretty = "\n" in text[root.start : root.end] or "\r" in text[root.start : root.end]
    indent = _line_indent(text, member.start)
    rendered = _render_json(value, pretty, indent, _newline(text))
    return text[: member.value.start] + rendered + text[member.value.end :]


def _existing_config_vaults(path):
    if not path.is_file():
        return [], []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InstallError("Epitype config root must be an object")
    configured = value.get("vaults")
    if not isinstance(configured, list) or not configured:
        return [], []
    valid = []
    stale = []
    for item in configured:
        try:
            exists = isinstance(item, str) and bool(item.strip()) and Path(item).is_dir()
        except (OSError, ValueError):
            exists = False
        (valid if exists else stale).append(item)
    return valid, stale


def _config_bytes(path, vaults, repo_root, preserve_vault_bytes=False, dream=None):
    if path.is_file():
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        value = json.loads(text)
        if not isinstance(value, dict):
            raise InstallError("Epitype config root must be an object")
        _parse_json(text)
        if not preserve_vault_bytes:
            text = _set_object_member(text, "vaults", [os.fspath(item) for item in vaults])
        if "budget_bytes" not in value:
            text = _set_object_member(text, "budget_bytes", 10 * 1024)
        if dream is not None:
            text = _set_object_member(text, DREAM_CONFIG_FIELD, dream)
        text = _set_object_member(text, "repo_root", os.fspath(repo_root.resolve()))
        encoded = text.encode("utf-8")
        return (b"\xef\xbb\xbf" if bom else b"") + encoded
    else:
        value = {}
    value["vaults"] = [os.fspath(path) for path in vaults]
    value.setdefault("budget_bytes", 10 * 1024)
    if dream is not None:
        value[DREAM_CONFIG_FIELD] = dream
    value["repo_root"] = os.fspath(repo_root.resolve())
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _vaults_bytes(path, vaults):
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise InstallError("Epitype config root must be an object")
    text = _set_object_member(text, "vaults", [os.fspath(item) for item in vaults])
    encoded = text.encode("utf-8")
    return (b"\xef\xbb\xbf" if bom else b"") + encoded


def _repo_root_bytes(path, repo_root):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InstallError("Epitype config root must be an object")
    value["repo_root"] = os.fspath(repo_root.resolve())
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _adapter_paths(repo_root):
    return {
        event: repo_root / "adapters" / "claude" / adapter_name
        for event, (_, adapter_name) in HOOK_SPECS.items()
    }


def _validate_repo_root(raw_value, require_adapters=True):
    if isinstance(raw_value, Path):
        repo_root = raw_value.expanduser().resolve()
    elif isinstance(raw_value, str) and raw_value.strip():
        repo_root = Path(raw_value).expanduser().resolve()
    else:
        raise ValueError("config repo_root must be a non-empty path")
    if not repo_root.is_dir():
        raise NotADirectoryError(os.fspath(repo_root))
    if require_adapters:
        missing = [path for path in _adapter_paths(repo_root).values() if not path.is_file()]
        if missing:
            raise FileNotFoundError("missing hook adapters: " + ", ".join(os.fspath(path) for path in missing))
    return repo_root


def _shim_payloads(repo_root):
    template_path = repo_root / "adapters" / "shim_template.py"
    template = template_path.read_text(encoding="utf-8")
    if template.count(SHIM_ADAPTER_TOKEN) != 1:
        raise InstallError("shim template must contain exactly one adapter token")
    return {
        shim_name: template.replace(SHIM_ADAPTER_TOKEN, adapter_name).encode("utf-8")
        for shim_name, adapter_name in HOOK_SPECS.values()
    }


def _load_state(path):
    if not path.is_file():
        return {"version": STATE_VERSION, "targets": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise InstallError("unsupported Epitype install state")
    if not isinstance(value.get("targets"), dict):
        raise InstallError("malformed Epitype install state")
    return value


def _state_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _merge_target_state(state, name, path, created_hooks, created_events,
                        created_file=False):
    targets = state.setdefault("targets", {})
    previous = targets.get(name, {})
    previous_events = previous.get("created_events", ())
    targets[name] = {
        "path": os.fspath(path),
        # 「這個檔本來不存在，是安裝建出來的」。第一次安裝記下來就不再翻面：之後重跑
        # 安裝時檔案當然存在了，照現況記的話這個事實會被自己的安裝洗掉，解除安裝就留
        # 下一個空殼。
        "created_file": bool(previous.get("created_file")) or created_file,
        "created_hooks": bool(previous.get("created_hooks")) or created_hooks,
        "created_events": sorted(set(previous_events) | set(created_events)),
    }


def _protected_diff(before, after):
    violations = []
    for path in sorted(set(before) | set(after), key=lambda item: os.fspath(item)):
        old_raw = before.get(path, b"")
        new_raw = after.get(path, b"")
        try:
            if path.suffix.casefold() == ".json":
                old_value = json.loads(old_raw.decode("utf-8-sig")) if old_raw else {}
                new_value = json.loads(new_raw.decode("utf-8-sig")) if new_raw else {}
            elif path.suffix.casefold() == ".toml":
                old_value = tomllib.loads(old_raw.decode("utf-8-sig")) if old_raw else {}
                new_value = tomllib.loads(new_raw.decode("utf-8-sig")) if new_raw else {}
            else:
                raise ValueError("unsupported protected config format")

            def protected_values(value, prefix=()):
                found = {}
                if not isinstance(value, dict):
                    return found
                for key, item in value.items():
                    path_parts = prefix + (str(key),)
                    if NATIVE_DISABLE_PATTERN.search(str(key)):
                        found[".".join(path_parts)] = item
                    found.update(protected_values(item, path_parts))
                return found

            old_values = protected_values(old_value)
            new_values = protected_values(new_value)
            for key in sorted(set(old_values) | set(new_values)):
                if old_values.get(key, object()) != new_values.get(key, object()):
                    violations.append(f"{path}: native-disable key changed: {key}")
        except (UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError):
            old = old_raw.decode("utf-8", errors="replace").splitlines()
            new = new_raw.decode("utf-8", errors="replace").splitlines()
            for line in difflib.unified_diff(old, new, lineterm=""):
                if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
                    continue
                if NATIVE_DISABLE_PATTERN.search(line[1:]):
                    violations.append(f"{path}: {line}")
    return violations


def _assert_native_protection(before, after):
    violations = _protected_diff(before, after)
    if violations:
        raise InstallError("native-memory protection rejected diff: " + " | ".join(violations))


def _run_billing_guard(home, apply_changes, dry_run, transaction, output, repo_root=REPO_ROOT):
    config = home / ".codex" / "config.toml"
    tool = repo_root / "adapters" / "codex" / "config_guard.py"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    check_result = subprocess.run(
        [sys.executable, os.fspath(tool), "check", "--config", os.fspath(config)],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    print("BILLING GUARD " + ("APPLY" if apply_changes else "CHECK"), file=output)
    if check_result.stdout:
        print(check_result.stdout.rstrip(), file=output)
    if check_result.stderr:
        print(check_result.stderr.rstrip(), file=output)
    if check_result.returncode != 0:
        raise InstallError(f"billing guard exited {check_result.returncode}")
    if apply_changes and dry_run:
        print(f"DRY-RUN apply target: {config}", file=output)
        return
    if not apply_changes:
        return

    # Apply against a temporary copy first. This keeps config_guard as the
    # source-driven implementation while graft owns the one required UTC backup.
    with tempfile.TemporaryDirectory(prefix="epitype-billing-") as temp_dir:
        temporary_config = Path(temp_dir).resolve() / "config.toml"
        temporary_config.write_bytes(config.read_bytes())
        apply_result = subprocess.run(
            [
                sys.executable,
                os.fspath(tool),
                "check",
                "--config",
                os.fspath(temporary_config),
                "--apply",
            ],
            cwd=repo_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if apply_result.returncode != 0:
            if apply_result.stderr:
                print(apply_result.stderr.rstrip(), file=output)
            raise InstallError(f"billing guard apply exited {apply_result.returncode}")
        transaction.write(config, temporary_config.read_bytes())
        print(f"APPLY TARGET: {config}", file=output)


def _marker_count(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    hooks = value.get("hooks", {}) if isinstance(value, dict) else {}
    counts = {}
    for event in EVENTS:
        entries = hooks.get(event, []) if isinstance(hooks, dict) else []
        counts[event] = sum(1 for entry in entries if _marked(entry)) if isinstance(entries, list) else 0
    return counts


_COMMAND_HEAD = re.compile(r'^\s*(?:"([^"]+)"|(\S+))\s+(.*)$')
_PATH_PYTHON_NAMES = ("python", "python3", "py")


def _unwrap_command(command):
    """The cmd.exe-safe form carries one outer pair of quotes; compare what is inside."""
    text = command or ""
    if text.startswith('"') and text.endswith('"') and text.count('"') >= 4:
        return text[1:-1]
    return text


def _split_command(command):
    """(interpreter token, rest) of a registered hook command."""
    match = _COMMAND_HEAD.match(command or "")
    if match is None:
        return None, command
    return match.group(1) or match.group(2), match.group(3).strip()


def _entry_matches(actual, expected):
    """A registration matches when everything but the interpreter token is the
    rendered template and the token names an interpreter that exists: the bare
    PATH name older installs registered, or a resolvable executable. Doctor is
    run from whichever Python is at hand, so requiring the exact path of the
    running interpreter would fail every working install."""
    if not isinstance(actual, dict) or set(actual) != set(expected):
        return False
    for key, value in expected.items():
        if key != "hooks":
            if actual.get(key) != value:
                return False
            continue
        actual_hooks = actual.get("hooks")
        if not isinstance(actual_hooks, list) or len(actual_hooks) != len(value):
            return False
        for actual_hook, expected_hook in zip(actual_hooks, value):
            if not isinstance(actual_hook, dict) or set(actual_hook) != set(expected_hook):
                return False
            for hook_key, hook_value in expected_hook.items():
                if hook_key not in ("command", "commandWindows"):
                    if actual_hook.get(hook_key) != hook_value:
                        return False
                    continue
                python, rest = _split_command(_unwrap_command(actual_hook.get(hook_key)))
                expected_python, expected_rest = _split_command(_unwrap_command(hook_value))
                if python is None or rest != expected_rest:
                    return False
                if python == expected_python or Path(python).name.lower() in _PATH_PYTHON_NAMES:
                    continue
                if not Path(python).is_file():
                    return False
    return True


def _marked_entries(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    hooks = value.get("hooks", {}) if isinstance(value, dict) else {}
    result = {}
    for event in EVENTS:
        entries = hooks.get(event, []) if isinstance(hooks, dict) else []
        result[event] = [entry for entry in entries if _marked(entry)] if isinstance(entries, list) else []
    return result


def _home_environment(home):
    environment = os.environ.copy()
    environment["HOME"] = os.fspath(home)
    environment["USERPROFILE"] = os.fspath(home)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _shim_status_path(home):
    return home / CONFIG_DIRECTORY / SHIM_STATUS_FILENAME


def _read_shim_status(home):
    path = _shim_status_path(home)
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError(f"shim status is not a file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("malformed shim status")
    records = value.get("shims")
    if not isinstance(records, dict) or len(records) > len(SHIM_NAMES):
        raise ValueError("malformed shim status records")
    validated = {}
    for shim_name, record in records.items():
        if shim_name not in SHIM_NAMES or not isinstance(record, dict):
            raise ValueError("malformed shim status record")
        timestamp = record.get("timestamp")
        reason = record.get("reason")
        if (
            record.get("shim") != shim_name
            or reason not in SHIM_REASON_CODES
            or not isinstance(timestamp, str)
            or not timestamp.endswith("Z")
        ):
            raise ValueError("malformed shim status record")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError("malformed shim status timestamp") from exc
        if parsed_timestamp.utcoffset() != timezone.utc.utcoffset(parsed_timestamp):
            raise ValueError("shim status timestamp is not UTC")
        validated[shim_name] = {
            "timestamp": timestamp,
            "shim": shim_name,
            "reason": reason,
        }
    return validated


def _report_shim_status(records, output, previous=None):
    previous = previous or {}
    for shim_name in SHIM_NAMES:
        record = records.get(shim_name)
        if record is None or record == previous.get(shim_name):
            continue
        print(
            f"SHIM FAIL-OPEN SEEN: {shim_name} {record['reason']} {record['timestamp']}",
            file=output,
        )


def _clear_shim_status(home, dry_run, output):
    path = _shim_status_path(home)
    if not path.exists():
        print("SHIM STATUS CLEAR: no records", file=output)
        return
    if dry_run:
        print(f"DRY-RUN remove shim status: {path}", file=output)
        return
    path.unlink()
    print(f"SHIM STATUS CLEARED: {path}", file=output)


def _synthetic_trace_reason(trace_path, shim_name, expected_adapter):
    records = []
    if trace_path.is_file():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("shim") == shim_name:
                records.append(record)
    if not records:
        return "no-trace"
    record = records[-1]
    actual_adapter = record.get("adapter")
    if not isinstance(actual_adapter, str):
        return "adapter-mismatch"
    actual_path = Path(actual_adapter)
    if not actual_path.is_absolute() or (
        os.path.normcase(os.path.normpath(actual_adapter))
        != os.path.normcase(os.path.normpath(os.fspath(expected_adapter.resolve())))
    ):
        return "adapter-mismatch"
    exit_code = record.get("exit")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
        return f"exit={exit_code if isinstance(exit_code, int) else '?'}"
    return None


def _uncommitted_changes(repo_root):
    """Count of tracked files changed in repo_root's working tree; None when it
    is not a git checkout or git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(repo_root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return sum(1 for line in result.stdout.splitlines() if line.strip())


def _synthetic_health(home, repo_root, output):
    scripts = (
        ("SessionStart", "sessionstart.py", {"source": "epitype-doctor"}, ()),
        ("UserPromptSubmit", "recall.py", {"prompt": "synthetic doctor probe"}, ()),
        ("PreCompact", "precompact.py", {"transcript_path": ""}, ("--codex",)),
        ("PreToolUse", "pretooluse.py", {"tool_name": "SyntheticRead", "tool_input": {"path": "synthetic.txt"}}, ()),
        # A Stop probe must never look like a real turn: an empty message reaches the
        # gate, exercises the adapter, and cannot match a card.
        ("Stop", "stop.py", {"stop_hook_active": False, "last_assistant_message": ""}, ()),
    )
    passed = 0
    hooks_root = home / CONFIG_DIRECTORY / HOOK_DIRECTORY
    try:
        allowed = {
            name: float(entry["hooks"][0].get("timeout") or 0)
            for name, entry in _hook_template(False, hooks_root, repo_root).items()
        }
    except (InstallError, OSError, ValueError):
        allowed = {}
    with tempfile.TemporaryDirectory(prefix="epitype-doctor-") as temp_dir:
        root = Path(temp_dir).resolve()
        vault = root / "vault"
        vault.mkdir()
        config = root / "config.json"
        config.write_text(json.dumps({"vaults": [os.fspath(vault)]}), encoding="utf-8")
        environment = _home_environment(home)
        environment["EPITYPE_CONFIG"] = os.fspath(config)
        # 健康檢查是唯讀的：合成的 SessionStart 不得順路起一場背景夢。
        environment[DREAM_MODE_ENV] = "off"
        for name, shim_name, event, arguments in scripts:
            script = hooks_root / shim_name
            trace_path = root / f"{shim_name}.trace"
            environment[SHIM_TRACE_ENV] = os.fspath(trace_path)
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, os.fspath(script), *arguments],
                input=json.dumps(event, ensure_ascii=False),
                cwd=repo_root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=allowed.get(name, 0) + 10,
                check=False,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            expected_adapter = repo_root / "adapters" / "claude" / HOOK_SPECS[name][1]
            reason = _synthetic_trace_reason(trace_path, shim_name, expected_adapter)
            ok = reason is None
            passed += int(ok)
            print(f"HOOK {name}: {'PASS' if ok else 'FAIL'} ({elapsed_ms} ms)", file=output)
            if reason is not None:
                print(f"REASON {name}: {reason}", file=output)
            # A hook that times out at the host fails open without a trace; the
            # only place that shows the margin shrinking is here.
            limit = allowed.get(name, 0)
            if limit and elapsed_ms > limit * 500:
                print(
                    f"WARN {name} used {elapsed_ms} ms of the {limit:g} s the host allows on an empty vault;"
                    " past the limit the host drops the hook silently",
                    file=output,
                )
    print(f"HEALTH {'PASS' if passed == len(scripts) else 'FAIL'} {passed}/{len(scripts)}", file=output)
    return passed == len(scripts)


def _dream_block(existing, mode=None, at=None):
    """設定檔裡的 dream 區塊。沒指定就沿用既有值，再沒有才用預設。"""
    block = dict(existing) if isinstance(existing, dict) else {}
    block[DREAM_MODE_FIELD] = mode or block.get(DREAM_MODE_FIELD) or DREAM_DEFAULT_MODE
    if block[DREAM_MODE_FIELD] not in DREAM_MODES:
        raise InstallError(f"unknown dream mode: {block[DREAM_MODE_FIELD]}")
    interval = block.get(DREAM_INTERVAL_HOURS_FIELD)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        interval = DREAM_DEFAULT_INTERVAL_HOURS
    block[DREAM_INTERVAL_HOURS_FIELD] = interval
    block[DREAM_AT_FIELD] = at or block.get(DREAM_AT_FIELD) or DREAM_DEFAULT_AT
    if not DREAM_AT_REGEX.fullmatch(str(block[DREAM_AT_FIELD])):
        raise InstallError(f"--at must be HH:MM in 24-hour form: {block[DREAM_AT_FIELD]}")
    return block


def _existing_config_dream(path):
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return {}
    block = value.get(DREAM_CONFIG_FIELD) if isinstance(value, dict) else None
    return block if isinstance(block, dict) else {}


def _windowless_python(python, windows=None):
    """Windows 排程用旁邊的 pythonw.exe——凌晨四點跑 python.exe 會閃一個主控台黑窗
    （本機有前科）。stdout/stderr 本來就導進 log，換掉主控台不損失任何輸出；旁邊沒有
    pythonw.exe 就維持原來的直譯器。"""
    windows = os.name == "nt" if windows is None else windows
    if not windows:
        return python
    path = Path(python)
    candidate = path.with_name(path.stem + "w" + path.suffix)
    return os.fspath(candidate) if candidate.is_file() else python


def _dream_command(repo_root, python_executable=None):
    """排程跑的就是 piggyback 起的那一支：夢自己從 config 解出庫與輸出路徑，所以
    這條命令不帶庫路徑，config 改了也不必重註冊。"""
    python = _windowless_python(os.fspath(Path(python_executable or sys.executable).resolve()))
    script = os.fspath((Path(repo_root) / Path(*DREAM_SCRIPT_PARTS)).resolve())
    return [python, script, DREAM_SCHEDULED_FLAG]


def _dream_command_text(command):
    return " ".join(_shell_token(item) for item in command)


def _run_scheduler(argv, stdin_text=None):
    return subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )


def _schedule_argv(repo_root, at, python_executable=None):
    """Windows：schtasks 一行搞定。POSIX：crontab 要先讀再追加，所以這裡只回傳那一行。"""
    command = _dream_command_text(_dream_command(repo_root, python_executable))
    if os.name == "nt":
        return ["schtasks", "/Create", "/SC", "DAILY", "/TN", DREAM_TASK_NAME, "/TR", command, "/ST", at, "/F"]
    hour, _, minute = at.partition(":")
    return [f"{int(minute)} {int(hour)} * * * {command} {DREAM_CRON_MARKER}"]


def _crontab_without_dream(runner):
    """只把已確認不存在的 crontab 當空表；未知讀取錯誤不得觸發替換。"""
    listed = runner(["crontab", "-l"], None)
    body = getattr(listed, "stdout", "") or ""
    if getattr(listed, "returncode", 1) != 0:
        detail = (getattr(listed, "stderr", "") or "").strip()
        if (getattr(listed, "returncode", 1) != 1 or body.strip()
                or not re.fullmatch(r"(?:crontab: )?no crontab for [^\s:]+", detail)):
            return None
    return [line for line in body.splitlines() if not _is_dream_cron(line)]


def _is_dream_cron(line):
    return not line.lstrip().startswith("#") and line.rstrip().endswith(" " + DREAM_CRON_MARKER)


def _register_nightly(repo_root, at, dry_run, output, runner=None, python_executable=None):
    """註冊每日排程並回報成敗；呼叫端負責維持設定與排程一致。"""
    runner = runner or _run_scheduler
    argv = _schedule_argv(repo_root, at, python_executable)
    if os.name == "nt":
        print(f"{'DRY-RUN schedule' if dry_run else 'SCHEDULE'}: {subprocess.list2cmdline(argv)}", file=output)
        if dry_run:
            return True
        result = runner(argv, None)
    else:
        line = argv[0]
        print(f"{'DRY-RUN schedule' if dry_run else 'SCHEDULE'}: crontab + {line}", file=output)
        if dry_run:
            return True
        kept = _crontab_without_dream(runner)
        if kept is None:
            print(CRONTAB_UNREADABLE_WARN.format(action="schedule"), file=output)
            return False
        result = runner(["crontab", "-"], "\n".join([*kept, line]) + "\n")
    if getattr(result, "returncode", 1) != 0:
        detail = (getattr(result, "stderr", "") or "").strip().splitlines()
        print(f"WARN nightly dream schedule failed: {detail[0] if detail else 'unknown error'}", file=output)
        return False
    return True


def _unregister_nightly(dry_run, output, runner=None):
    runner = runner or _run_scheduler
    if os.name == "nt":
        argv = ["schtasks", "/Delete", "/TN", DREAM_TASK_NAME, "/F"]
        print(f"{'DRY-RUN unschedule' if dry_run else 'UNSCHEDULE'}: {subprocess.list2cmdline(argv)}", file=output)
        if dry_run:
            return True
        result = runner(argv, None)
        if getattr(result, "returncode", 1) == 0:
            return True
        # 找不到就是已經沒有了，冪等移除。用 HRESULT 避免依賴 Windows 訊息語言：
        # 0x80070002 是「找不到這個工作」，0x80070003 是「連 Epitype\ 這個工作資料夾都
        # 不存在」——乾淨機器上是後者，只認前者的話第一次安裝就會因為刪不掉一個從來
        # 沒建立過的排程而整個回滾（2026-09-17 實測）。
        query = runner(["schtasks", "/Query", "/TN", DREAM_TASK_NAME, "/HRESULT"], None)
        return (getattr(query, "returncode", 1) & 0xFFFFFFFF) in (0x80070002, 0x80070003)
    print(f"{'DRY-RUN unschedule' if dry_run else 'UNSCHEDULE'}: crontab - {DREAM_CRON_MARKER}", file=output)
    if dry_run:
        return True
    kept = _crontab_without_dream(runner)
    if kept is None:
        print(CRONTAB_UNREADABLE_WARN.format(action="unschedule"), file=output)
        return False
    result = runner(["crontab", "-"], ("\n".join(kept) + "\n") if kept else "")
    return getattr(result, "returncode", 1) == 0


def _dream_governance(vaults):
    """帶工作帳本的那個庫；沒有帳本就用第一個（與 hook 的 governance_vault 同規則）。"""
    paths = [Path(item) for item in vaults if isinstance(item, str) and item.strip()]
    if not paths:
        return None
    for vault in paths:
        if (vault / WORK_LEDGER_FILENAME).is_file():
            return vault
    return paths[0]


def _report_dream(config, output):
    block = config.get(DREAM_CONFIG_FIELD) if isinstance(config, dict) else None
    block = block if isinstance(block, dict) else {}
    mode = block.get(DREAM_MODE_FIELD, DREAM_DEFAULT_MODE)
    at = block.get(DREAM_AT_FIELD, DREAM_DEFAULT_AT)
    interval = block.get(DREAM_INTERVAL_HOURS_FIELD, DREAM_DEFAULT_INTERVAL_HOURS)
    governance = _dream_governance(config.get("vaults") or () if isinstance(config, dict) else ())
    last = "never"
    if governance is not None:
        try:
            state = json.loads((governance / DREAM_DIRECTORY / DREAM_STATE_FILENAME).read_text(encoding="utf-8"))
            last = state.get("completed_at") or "never"
        except (OSError, ValueError):
            last = "never"
    print(f"DREAM: mode={mode} interval_hours={interval} at={at} last={last}", file=output)


def _read_nightly(runner):
    """讀取實際入口；缺少、停用或不明格式都不是已驗證的每日排程。"""
    argv = (["schtasks", "/Query", "/TN", DREAM_TASK_NAME, "/XML"]
            if os.name == "nt" else ["crontab", "-l"])
    result = runner(argv, None)
    if getattr(result, "returncode", 1) != 0:
        raise InstallError("nightly schedule is missing or unreadable")
    body = getattr(result, "stdout", "") or ""
    if os.name == "nt":
        root = ET.fromstring(body)
        actions = root.findall("./{*}Actions/{*}Exec")
        triggers = root.findall("./{*}Triggers/{*}CalendarTrigger")
        if (len(actions) != 1 or len(triggers) != 1
                or len(root.findall("./{*}Actions/*")) != 1
                or len(root.findall("./{*}Triggers/*")) != 1
                or root.findtext("./{*}Settings/{*}Enabled") == "false"
                or triggers[0].findtext("{*}Enabled") == "false"
                or triggers[0].findtext("{*}ScheduleByDay/{*}DaysInterval") != "1"):
            raise InstallError("nightly schedule must have one enabled daily action")
        python = actions[0].findtext("{*}Command") or ""
        args = shlex.split(actions[0].findtext("{*}Arguments") or "", posix=False)
        command = [python.strip('"'), *(item.strip('"') for item in args)]
        boundary = triggers[0].findtext("{*}StartBoundary") or ""
        at = datetime.fromisoformat(boundary).strftime("%H:%M")
        return {"command": command, "at": at}
    lines = body.splitlines()
    own = [index for index, line in enumerate(lines) if _is_dream_cron(line)]
    if len(own) != 1:
        raise InstallError("nightly schedule must contain exactly one Epitype cron entry")
    fields = lines[own[0]][:-len(DREAM_CRON_MARKER)].split(None, 5)
    if (len(fields) != 6 or fields[2:5] != ["*", "*", "*"]
            or not fields[0].isdigit() or not fields[1].isdigit()):
        raise InstallError("nightly cron entry is not a daily schedule")
    at = f"{int(fields[1]):02d}:{int(fields[0]):02d}"
    return {"command": shlex.split(fields[5]), "at": at, "lines": lines, "index": own[0]}


def _check_nightly(config, output, runner, require_script=True):
    state = _read_nightly(runner)
    command = state["command"]
    expected = Path(config["repo_root"]) / Path(*DREAM_SCRIPT_PARTS)
    print(f"DREAM SCHEDULE: target={command!r} at={state['at']}", file=output)
    if (len(command) != 3 or command[2] != DREAM_SCHEDULED_FLAG
            or Path(command[1]).resolve() != expected.resolve()
            or not Path(command[0]).is_file() or (require_script and not expected.is_file())
            or state["at"] != config[DREAM_CONFIG_FIELD].get(DREAM_AT_FIELD, DREAM_DEFAULT_AT)):
        raise InstallError("nightly schedule target, time, or executable does not match config")
    print("DREAM SCHEDULE: PASS", file=output)
    return state


def _replace_nightly_command(expected, command, runner):
    """只改自己的入口，並拒絕覆蓋在讀取後已被改動的入口。"""
    current = _read_nightly(runner)
    if any(current[key] != expected[key] for key in ("command", "at")):
        raise InstallError("nightly schedule changed concurrently; left untouched")
    rendered = _dream_command_text(command)
    if os.name == "nt":
        result = runner(["schtasks", "/Change", "/TN", DREAM_TASK_NAME, "/TR", rendered], None)
    else:
        hour, minute = current["at"].split(":")
        current["lines"][current["index"]] = (
            f"{int(minute)} {int(hour)} * * * {rendered} {DREAM_CRON_MARKER}"
        )
        result = runner(["crontab", "-"], "\n".join(current["lines"]) + "\n")
    if getattr(result, "returncode", 1) != 0:
        raise InstallError("nightly schedule update failed")


def _doctor(home, dry_run=False, output=sys.stdout, clear_shim_status=False, scheduler=None):
    hosts = _detect_hosts(home)
    print("HOSTS: " + (", ".join(hosts) if hosts else "none"), file=output)
    if dry_run:
        records = _read_shim_status(home)
        _report_shim_status(records, output)
        if clear_shim_status:
            _clear_shim_status(home, True, output)
        print("DRY-RUN hooks: " + ", ".join(EVENTS), file=output)
        if not hosts:
            print("DOCTOR FAIL no supported host detected", file=output)
            return 1
        if records:
            print("DOCTOR FAIL fail-open breadcrumb requires --clear-shim-status", file=output)
            return 1
        return 0
    config_path = home / CONFIG_DIRECTORY / CONFIG_FILENAME
    try:
        if clear_shim_status:
            _clear_shim_status(home, False, output)
        initial_records = _read_shim_status(home)
        _report_shim_status(initial_records, output)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        vaults = config.get("vaults") if isinstance(config, dict) else None
        if not isinstance(vaults, list) or not vaults or not all(Path(item).is_dir() for item in vaults):
            raise ValueError("config vaults must be existing directories")
        repo_root = _validate_repo_root(
            config.get("repo_root") if isinstance(config, dict) else None,
            require_adapters=False,
        )
        hooks_root = home / CONFIG_DIRECTORY / HOOK_DIRECTORY
        shim_payloads = _shim_payloads(repo_root)
        for shim_name, expected in shim_payloads.items():
            shim_path = hooks_root / shim_name
            if not shim_path.is_file() or shim_path.read_bytes() != expected:
                raise ValueError(f"shim is missing or stale: {shim_path}")
        print(
            f"SHIM RESOLUTION: PASS {len(shim_payloads)}/{len(SHIM_NAMES)} repo_root={repo_root}",
            file=output,
        )
        dirty = _uncommitted_changes(repo_root)
        if dirty:
            # The live hooks run whatever is in repo_root, committed or not
            # (2026-09-04: an unfinished batch left in the tree ran live for a day).
            print(
                f"WARN repo_root has {dirty} uncommitted change(s): the live hooks run code no gate has passed",
                file=output,
            )
        if not hosts:
            raise ValueError("no supported host detected")
        for name in hosts:
            hook_path = (
                home / ".claude" / "settings.json"
                if name == "claude"
                else home / ".codex" / "hooks.json"
            )
            actual = _marked_entries(hook_path)
            expected = _hook_template(name == "codex", hooks_root, repo_root)
            mismatches = [
                event
                for event in EVENTS
                if len(actual[event]) != 1 or not _entry_matches(actual[event][0], expected[event])
            ]
            if mismatches:
                raise ValueError(f"{name} shim registration mismatch: {', '.join(mismatches)}")
            print(f"REGISTRATION {name}: PASS {len(EVENTS)}/{len(EVENTS)}", file=output)
        _report_dream(config, output)
        if _existing_config_dream(config_path).get(DREAM_MODE_FIELD) == "nightly":
            _check_nightly(config, output, scheduler or _run_scheduler)
        health_ok = _synthetic_health(home, repo_root, output)
        final_records = _read_shim_status(home)
        _report_shim_status(final_records, output, previous=initial_records)
        if final_records:
            print("DOCTOR FAIL fail-open breadcrumb requires --clear-shim-status", file=output)
            return 1
        if not health_ok:
            print("DOCTOR FAIL synthetic hook health", file=output)
            return 1
        return 0
    except Exception as exc:
        print(f"DOCTOR FAIL {type(exc).__name__}: {exc}", file=output)
        return 1


def _planned_backup(path):
    return path.with_name(path.name + BACKUP_INFIX + "<UTC>")


def _install(
    home,
    dry_run=False,
    apply_billing_guard=False,
    output=sys.stdout,
    repo_root=REPO_ROOT,
    dream_mode=None,
    dream_at=None,
    scheduler=None,
):
    repo_root = _validate_repo_root(repo_root)
    hosts = _detect_hosts(home)
    if not hosts:
        raise InstallError("no supported host detected under --home")
    print("HOSTS: " + ", ".join(hosts), file=output)
    config_dir = home / CONFIG_DIRECTORY
    config_path = config_dir / CONFIG_FILENAME
    state_path = config_dir / STATE_FILENAME
    hooks_root = config_dir / HOOK_DIRECTORY
    state = _load_state(state_path)
    native_vaults = _detect_native_vaults(home, hosts)
    configured_vaults, stale_vaults = _existing_config_vaults(config_path)
    fallback = home / FALLBACK_VAULT
    if configured_vaults:
        vaults = configured_vaults
        preserve_vault_bytes = not stale_vaults
        print(f"VAULTS: preserved {len(configured_vaults)} from config", file=output)
        for stale in stale_vaults:
            print(f"VAULTS: stale entry {stale}", file=output)
        print(
            f"VAULTS: detection found {len(native_vaults)}; not adopted (use the resync command)",
            file=output,
        )
    else:
        vaults = native_vaults or [fallback.resolve()]
        preserve_vault_bytes = False
        for stale in stale_vaults:
            print(f"VAULTS: stale entry {stale}", file=output)
    transaction = Transaction()
    protected_paths = [home / ".claude" / "settings.json", home / ".codex" / "config.toml"]
    protected_before = {path: path.read_bytes() for path in protected_paths if path.is_file()}

    try:
        if not configured_vaults and not native_vaults:
            print(f"{'DRY-RUN create' if dry_run else 'CREATE'} empty vault: {fallback}", file=output)
            if not dry_run:
                transaction.mkdir(fallback)
        elif not configured_vaults:
            for vault in native_vaults:
                print(f"NATIVE VAULT: {vault}", file=output)

        previous_dream = _existing_config_dream(config_path)
        dream = _dream_block(previous_dream, dream_mode, dream_at)
        config_data = _config_bytes(config_path, vaults, repo_root, preserve_vault_bytes, dream)
        if not config_path.exists() or config_path.read_bytes() != config_data:
            print(f"{'DRY-RUN write' if dry_run else 'WRITE'} {config_path}: vaults, repo_root, dream", file=output)
            if config_path.exists():
                print(f"BACKUP: {_planned_backup(config_path) if dry_run else 'pending'}", file=output)
            if not dry_run:
                transaction.write(config_path, config_data)

        for shim_name, payload in _shim_payloads(repo_root).items():
            shim_path = hooks_root / shim_name
            if not shim_path.exists() or shim_path.read_bytes() != payload:
                print(f"{'DRY-RUN write' if dry_run else 'WRITE'} stable shim: {shim_path}", file=output)
                if not dry_run:
                    transaction.write(shim_path, payload)

        targets = []
        if "claude" in hosts:
            targets.append(("claude", home / ".claude" / "settings.json", _hook_template(False, hooks_root, repo_root)))
        if "codex" in hosts:
            targets.append(("codex", home / ".codex" / "hooks.json", _hook_template(True, hooks_root, repo_root)))

        for name, path, entries in targets:
            existed = path.is_file()
            source = path.read_bytes() if existed else b"{}\n"
            merged, created_hooks, created_events, locations = _merge_hooks(source, entries)
            _merge_target_state(state, name, path, created_hooks, created_events,
                                created_file=not existed)
            for location in locations:
                print(f"{'DRY-RUN merge' if dry_run else 'MERGE'} {path}: {location}", file=output)
            if merged != source:
                if path.exists():
                    print(f"BACKUP: {_planned_backup(path) if dry_run else 'pending'}", file=output)
                if not dry_run:
                    transaction.write(path, merged)

        if "codex" in hosts:
            _run_billing_guard(home, apply_billing_guard, dry_run, transaction, output, repo_root)

        # nightly 才碰系統排程。改成別的模式時只在「原本就是 nightly」或使用者明講
        # 這次要換模式時反註冊——否則每次安裝都會對一個不存在的排程下刪除指令。
        # 系統排程是整台機器共用的，`--home` 隔離不到它，而且拿家目錄去比也沒用——
        # 沙盒本來就會把 HOME 換掉，所以 `Path.home()` 回傳的就是那個假家目錄。
        # 唯一站得住的判準是這份設定自己的歷史：只有「這個家目錄原本真的是 nightly」
        # 才需要反註冊。裝到一個從來沒排過程的家目錄卻下刪除指令，刪掉的是別人的東西
        # ——2026-09-17 一次隔離驗收就是這樣刪掉真實家目錄那支還在服役的夜間排程，
        # 兩次，而檔案層的隔離檢查完全看不出來。
        if dream[DREAM_MODE_FIELD] == "nightly":
            _register_nightly(repo_root, dream[DREAM_AT_FIELD], dry_run, output, scheduler)
        elif previous_dream.get(DREAM_MODE_FIELD) == "nightly":
            if not _unregister_nightly(dry_run, output, scheduler):
                raise InstallError("nightly dream unschedule failed; installation rolled back")

        if dry_run:
            print(f"DRY-RUN write {state_path}: install ownership metadata", file=output)
            print(f"DRY-RUN health: feed synthetic stdin to {len(EVENTS)} hooks", file=output)
            print("DRY-RUN complete; no files changed.", file=output)
            return 0

        transaction.write(state_path, _state_bytes(state))
        protected_after = {path: path.read_bytes() for path in protected_paths if path.is_file()}
        _assert_native_protection(protected_before, protected_after)
        if _doctor(home, output=output, scheduler=scheduler) != 0:
            raise InstallError("post-install doctor failed")

        # 規則塊與索引同步進宿主自己會載入的那個檔。沒有這一步，使用者寫了卡、產生了
        # 規則，代理卻永遠讀不到——而且看不出少了什麼。裝的時候順手做掉，使用者不必
        # 知道有這個指令。同步失敗不讓安裝失敗：hook 已經裝好、卡片庫已經能用。
        try:
            # 直接跑 `python install/graft.py` 時 sys.path[0] 是 install\，`epitype` 匯
            # 不進來——而那正是文件與安裝器自己印出來的呼叫形式。以前這裡會安靜地跳過
            # 整個傳動軸、照樣回報安裝成功，使用者的 CLAUDE.md 一個字都沒有。
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            from epitype import host_sync

            host_sync.apply(vaults, home=home, output=output)
        except Exception as exc:
            print(f"HOST SYNC SKIPPED: {type(exc).__name__}: {exc}", file=output)

        print("INSTALL REPORT", file=output)
        for path in transaction.changed:
            print(f"CHANGED: {path}", file=output)
        for source, backup in transaction.backups:
            print(f"BACKUP: {source} -> {backup}", file=output)
        print(
            f"REMOVE: {sys.executable} {REPO_ROOT / 'install' / 'graft.py'} uninstall --home {home}",
            file=output,
        )
        return 0
    except Exception:
        if not dry_run:
            transaction.rollback()
        raise


def _resync_vaults(home, dry_run=False, output=sys.stdout):
    hosts = _detect_hosts(home)
    if not hosts:
        raise InstallError("no supported host detected under --home")
    config_path = home / CONFIG_DIRECTORY / CONFIG_FILENAME
    if not config_path.is_file():
        raise InstallError(f"Epitype config is missing: {config_path}")
    value = json.loads(config_path.read_bytes().decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise InstallError("Epitype config root must be an object")
    current = value.get("vaults") if isinstance(value.get("vaults"), list) else []
    detected = _detect_native_vaults(home, hosts)
    fallback = home / FALLBACK_VAULT
    vaults = detected or [fallback.resolve()]
    print("HOSTS: " + ", ".join(hosts), file=output)
    print(f"VAULTS: detection found {len(detected)}; resync requested", file=output)
    current_keys = {
        os.path.normcase(os.path.normpath(item))
        for item in current
        if isinstance(item, str)
    }
    detected_keys = {
        os.path.normcase(os.path.normpath(os.fspath(item)))
        for item in vaults
    }
    for item in current:
        key = os.path.normcase(os.path.normpath(item)) if isinstance(item, str) else None
        if key not in detected_keys:
            print(f"VAULTS: remove {item}", file=output)
    for item in vaults:
        if os.path.normcase(os.path.normpath(os.fspath(item))) not in current_keys:
            print(f"VAULTS: add {item}", file=output)

    config_data = _vaults_bytes(config_path, vaults)
    changed = config_path.read_bytes() != config_data
    if dry_run:
        if not detected and not fallback.exists():
            print(f"DRY-RUN create empty vault: {fallback}", file=output)
        if changed:
            print(f"DRY-RUN write {config_path}: vaults", file=output)
            print(f"BACKUP: {_planned_backup(config_path)}", file=output)
        print("DRY-RUN complete; no files changed.", file=output)
        return 0

    transaction = Transaction()
    try:
        if not detected:
            transaction.mkdir(fallback)
        if changed:
            transaction.write(config_path, config_data)
        print("VAULT RESYNC REPORT", file=output)
        print(f"CONFIG: {'CHANGED' if changed else 'UNCHANGED'} {config_path}", file=output)
        for source, backup in transaction.backups:
            print(f"BACKUP: {source} -> {backup}", file=output)
        return 0
    except Exception:
        transaction.rollback()
        raise


def _relocate(home, target, dry_run=False, output=sys.stdout, scheduler=None):
    repo_root = _validate_repo_root(target)
    config_path = home / CONFIG_DIRECTORY / CONFIG_FILENAME
    if not config_path.is_file():
        raise InstallError(f"Epitype config is missing: {config_path}")
    original = config_path.read_bytes()
    config = json.loads(original.decode("utf-8-sig"))
    config_data = _repo_root_bytes(config_path, repo_root)
    changed = original != config_data
    nightly = _existing_config_dream(config_path).get(DREAM_MODE_FIELD) == "nightly"
    print(f"RELOCATE TARGET VALID: {repo_root}", file=output)
    if dry_run:
        if changed:
            print(f"DRY-RUN write {config_path}: repo_root", file=output)
            if nightly:
                print(f"DRY-RUN update nightly target: {repo_root / Path(*DREAM_SCRIPT_PARTS)}", file=output)
        print("DRY-RUN complete; no files changed.", file=output)
        return 0

    transaction = Transaction()
    runner = scheduler or _run_scheduler
    before = _check_nightly(config, output, runner, require_script=False) if nightly and changed else None
    updated = None
    try:
        if changed:
            if config_path.read_bytes() != original:
                raise InstallError("config changed concurrently; left untouched")
            transaction.write(config_path, config_data)
        if before:
            command = [before["command"][0], os.fspath(repo_root / Path(*DREAM_SCRIPT_PARTS)), DREAM_SCHEDULED_FLAG]
            updated = {"command": command, "at": before["at"]}
            _replace_nightly_command(before, command, runner)
        doctor_code = _doctor(home, output=output, scheduler=scheduler)
        if doctor_code:
            raise InstallError("post-relocate doctor failed")
    except Exception:
        if updated:
            try:
                current = _read_nightly(runner)
                if any(current[key] != before[key] for key in ("command", "at")):
                    _replace_nightly_command(updated, before["command"], runner)
            except Exception as exc:
                print(f"ROLLBACK FAIL schedule: {exc}", file=output)
        if transaction.changed:
            if config_path.read_bytes() == config_data:
                transaction.rollback()
            else:
                print("ROLLBACK FAIL config changed concurrently; left untouched", file=output)
        raise
    print("RELOCATE REPORT", file=output)
    print(f"REPO ROOT: {repo_root}", file=output)
    print(f"CONFIG: {'CHANGED' if changed else 'UNCHANGED'} {config_path}", file=output)
    for source, backup in transaction.backups:
        print(f"BACKUP: {source} -> {backup}", file=output)
    print("HOST CONFIG: UNCHANGED", file=output)
    print(f"DOCTOR: {'PASS' if doctor_code == 0 else 'FAIL'}", file=output)
    return doctor_code


def _uninstall(home, dry_run=False, output=sys.stdout, scheduler=None):
    config_dir = home / CONFIG_DIRECTORY
    state_path = config_dir / STATE_FILENAME
    state = _load_state(state_path) if state_path.is_file() else {"version": STATE_VERSION, "targets": {}}
    config_path = config_dir / CONFIG_FILENAME
    if config_path.is_file():
        config_value = json.loads(config_path.read_text(encoding="utf-8"))
        configured_vaults = config_value.get("vaults", ()) if isinstance(config_value, dict) else ()
        for raw_vault in configured_vaults if isinstance(configured_vaults, list) else ():
            if not isinstance(raw_vault, str):
                continue
            vault = Path(raw_vault).expanduser().resolve()
            try:
                vault.relative_to(config_dir.resolve())
            except ValueError:
                continue
            raise InstallError(
                f"refusing to remove {config_dir}: configured vault would be deleted: {vault}"
            )
    transaction = Transaction()
    targets = {
        "claude": home / ".claude" / "settings.json",
        "codex": home / ".codex" / "hooks.json",
    }
    try:
        for name, path in targets.items():
            if not path.is_file():
                continue
            target_state = state.get("targets", {}).get(name, {})
            updated, removed = _unmerge_hooks(path.read_bytes(), target_state)
            for location in removed:
                print(f"{'DRY-RUN remove' if dry_run else 'REMOVE'} {path}: {location}", file=output)
            # 整個檔都是我們建的、拿掉之後只剩一個空殼，就把檔一起刪掉。宿主檔那邊已經
            # 是這個待遇（整份是我們的就刪），hooks.json 沒有的話，解除安裝之後那句
            # 「沒留下我們的東西」在 codex 這一路就是假的。使用者原本就有的檔永遠不刪。
            if target_state.get("created_file") and _is_empty_shell(updated):
                print(f"{'DRY-RUN remove' if dry_run else 'REMOVE'} {path}"
                      "：這個檔是安裝建出來的，拿掉區塊之後是空的", file=output)
                if not dry_run:
                    transaction.remove(path)
                continue
            if updated != path.read_bytes():
                if dry_run:
                    print(f"BACKUP: {_planned_backup(path)}", file=output)
                else:
                    transaction.write(path, updated)

        # 排程比 config 活得久：留著它會每晚跑一支讀不到 config 的夢。
        if _existing_config_dream(config_path).get(DREAM_MODE_FIELD) == "nightly":
            if not _unregister_nightly(dry_run, output, scheduler):
                raise InstallError("nightly dream unschedule failed; installation preserved")

        if config_dir.exists():
            print(f"{'DRY-RUN remove' if dry_run else 'REMOVE'} config directory: {config_dir}", file=output)
        # 寫進宿主檔的規則塊也要拿回來。不拿的話，那兩個檔會永遠留著一段「由卡片生成、
        # 勿手改」的文字，而生成它的東西已經被刪掉了——每一場都載入一份沒有主人的規則。
        # 寫得進使用者的全域指令檔，就必須拿得回來，而且要在刪掉設定目錄之前做（狀態檔
        # 就在裡面）。
        print(f"{'DRY-RUN remove' if dry_run else 'REMOVE'} host file blocks", file=output)
        if dry_run:
            print("DRY-RUN complete; no files changed.", file=output)
            return 0
        try:
            if str(REPO_ROOT) not in sys.path:
                sys.path.insert(0, str(REPO_ROOT))
            from epitype import host_sync

            host_sync.remove(home=home, output=output)
        except Exception as exc:
            print(f"HOST BLOCK REMOVAL FAILED: {type(exc).__name__}: {exc}", file=output)
            print(
                "  請自行跑：python -m epitype.host_sync --remove"
                "（或手動刪掉 CLAUDE.md／AGENTS.md 裡 EPITYPE 標記之間的區塊）",
                file=output,
            )
        if config_dir.exists():
            shutil.rmtree(config_dir)
        print("UNINSTALL REPORT", file=output)
        for path in transaction.changed:
            print(f"CHANGED: {path}", file=output)
        for source, backup in transaction.backups:
            print(f"BACKUP: {source} -> {backup}", file=output)
        # 安裝時留下的備份也還在使用者目錄裡；只報這次的，那些就成了沒人提過的殘留。
        fresh = {backup for _source, backup in transaction.backups}
        for target in targets.values():
            for earlier in sorted(target.parent.glob(target.name + BACKUP_INFIX + "*")):
                if earlier not in fresh:
                    print(f"EARLIER BACKUP KEPT: {earlier}", file=output)
        print("VAULTS PRESERVED: native vaults and ~/.epitype-vault were not removed.", file=output)
        return 0
    except Exception:
        if not dry_run:
            transaction.rollback()
        raise


def _tree_digest(root):
    rows = []
    for path in sorted(root.rglob("*"), key=lambda item: os.fspath(item)):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            rows.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            rows.append((relative + "/", "directory"))
    return rows


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-graft-") as temp_dir:
            root = Path(temp_dir).resolve()
            old_repo = root / "repo-before-move"
            # Parallel trust tests own transient directories, not installable sources.
            copy_ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache", ".hook-trust-*")
            checks.append((
                "install fixture excludes transient trust directories but preserves sources",
                copy_ignore(str(REPO_ROOT), [".hook-trust-fixture", "adapters", "epitype", "pyproject.toml"])
                == {".hook-trust-fixture"},
            ))
            shutil.copytree(
                REPO_ROOT,
                old_repo,
                ignore=copy_ignore,
            )
            shim_payloads = _shim_payloads(old_repo)
            checks.append((
                "rendered shims are byte-identical to their pins",
                set(shim_payloads) == set(SHIM_SHA256)
                and all(
                    hashlib.sha256(payload).hexdigest() == SHIM_SHA256[shim_name]
                    for shim_name, payload in shim_payloads.items()
                ),
            ))
            home = root / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".codex").mkdir(parents=True)
            claude = home / ".claude" / "settings.json"
            codex_config = home / ".codex" / "config.toml"
            codex_hooks = home / ".codex" / "hooks.json"
            claude_source = (
                b'{\r\n  "permissions": {"deny": ["SyntheticDanger"]},\r\n'
                b'  "hooks": {"SessionStart": [{"id":"existing","hooks":[]}]},\r\n'
                b'  "sentinel": "preserve bytes"\r\n}\r\n'
            )
            codex_source = b'{"hooks":{"PreToolUse":[{"comment":"existing","hooks":[]}]},"keep":true}\n'
            config_source = b'model = "synthetic-unknown"\nmodel_context_window = 999999\n'
            claude.write_bytes(claude_source)
            codex_hooks.write_bytes(codex_source)
            codex_config.write_bytes(config_source)

            first_output = io.StringIO()
            first_code = _install(home, output=first_output, repo_root=old_repo)
            fallback = home / FALLBACK_VAULT
            config_value = json.loads((home / CONFIG_DIRECTORY / CONFIG_FILENAME).read_text(encoding="utf-8"))
            checks.append((
                "both hosts, empty fallback, and doctor",
                first_code == 0
                and fallback.is_dir()
                and not list(fallback.iterdir())
                and config_value.get("vaults") == [os.fspath(fallback.resolve())]
                and config_value.get("repo_root") == os.fspath(old_repo.resolve())
                and all(f"HOOK {event}: PASS" in first_output.getvalue() for event in EVENTS)
                and f"HEALTH PASS {len(EVENTS)}/{len(EVENTS)}" in first_output.getvalue(),
            ))
            claude_value = json.loads(claude.read_text(encoding="utf-8"))
            codex_value = json.loads(codex_hooks.read_text(encoding="utf-8"))
            checks.append((
                "merge preserves permissions and existing hooks",
                claude_value["permissions"]["deny"] == ["SyntheticDanger"]
                and claude_value["hooks"]["SessionStart"][0]["id"] == "existing"
                and codex_value["hooks"]["PreToolUse"][0]["comment"] == "existing"
                and all(_marker_count(claude)[event] == 1 for event in EVENTS)
                and all(_marker_count(codex_hooks)[event] == 1 for event in EVENTS),
            ))
            hooks_root = home / CONFIG_DIRECTORY / HOOK_DIRECTORY
            expected_claude = _hook_template(False, hooks_root, old_repo)
            expected_codex = _hook_template(True, hooks_root, old_repo)
            checks.append((
                "install registers stable shims instead of repo adapters",
                _marked_entries(claude) == {event: [expected_claude[event]] for event in EVENTS}
                and _marked_entries(codex_hooks) == {event: [expected_codex[event]] for event in EVENTS}
                and all(
                    (hooks_root / shim_name).read_bytes() == payload
                    for shim_name, payload in _shim_payloads(old_repo).items()
                )
                and "/adapters/claude/" not in claude.read_text(encoding="utf-8").replace("\\", "/")
                and "/adapters/claude/" not in codex_hooks.read_text(encoding="utf-8").replace("\\", "/"),
            ))
            backups = list((home / ".claude").glob("settings.json.bak_epitype_*"))
            backups += list((home / ".codex").glob("hooks.json.bak_epitype_*"))
            checks.append((
                "UTC backups contain original bytes",
                len(backups) == 2
                and any(path.read_bytes() == claude_source for path in backups)
                and any(path.read_bytes() == codex_source for path in backups),
            ))

            prune_target = home / "prune" / "settings.json"
            prune_target.parent.mkdir(parents=True)
            prune_target.write_text("{}", encoding="utf-8")
            for index in range(BACKUPS_KEPT + 3):
                stale = prune_target.with_name(f"{prune_target.name}{BACKUP_INFIX}2026090{index}T000000Z")
                stale.write_text(str(index), encoding="utf-8")
                os.utime(stale, (1_700_000_000 + index, 1_700_000_000 + index))
            _prune_backups(prune_target, keep=BACKUPS_KEPT)
            remaining = sorted(item.name for item in prune_target.parent.iterdir() if BACKUP_INFIX in item.name)
            checks.append((
                "only the newest backups of an edited file are kept",
                len(remaining) == BACKUPS_KEPT
                and remaining[-1].endswith(f"2026090{BACKUPS_KEPT + 2}T000000Z")
                and prune_target.is_file(),
            ))

            rendered = expected_claude["SessionStart"]
            rendered_command = rendered["hooks"][0]["command"]
            _, rendered_rest = _split_command(rendered_command)

            def variant(command=None, **overrides):
                entry = json.loads(json.dumps(rendered))
                if command is not None:
                    entry["hooks"][0]["command"] = command
                entry["hooks"][0].update(overrides)
                return entry

            checks.append((
                "doctor accepts a PATH interpreter or an existing one, and nothing else about the entry",
                _entry_matches(variant(f"python {rendered_rest}"), rendered)
                and _entry_matches(variant(f'"{sys.executable}" {rendered_rest}'), rendered)
                and not _entry_matches(variant(f'"{home / "missing-python.exe"}" {rendered_rest}'), rendered)
                and not _entry_matches(variant(f'python "{home / "elsewhere.py"}"'), rendered)
                and not _entry_matches(variant(timeout=30), rendered),
            ))

            spaced_python = home / "Program Files" / "Python" / "python.exe"
            spaced = _hook_template(True, hooks_root, old_repo, python_executable=os.fspath(spaced_python))
            spaced_hook = spaced["PreCompact"]["hooks"][0]
            checks.append((
                "hook commands are unquoted when paths allow it, and a quoted command gets a cmd.exe-safe commandWindows",
                '"' not in rendered_command
                and "commandWindows" not in rendered["hooks"][0]
                and spaced_hook["command"].startswith(f'"{spaced_python.resolve().as_posix()}" ')
                and spaced_hook["command"].endswith("/precompact.py --codex")
                and spaced_hook["commandWindows"] == f'"{spaced_hook["command"]}"'
                and _entry_matches(json.loads(json.dumps(spaced["PreCompact"])), spaced["PreCompact"]),
            ))

            checks.append((
                "doctor reports each hook's wall time beside its verdict",
                all(f"HOOK {event}: PASS (" in first_output.getvalue() for event in EVENTS),
            ))

            dirty_repo = home / "dirty-repo"
            dirty_repo.mkdir()
            git_ok = True
            try:
                for arguments in (
                    ("init", "-q"),
                    ("config", "user.email", "selftest"),
                    ("config", "user.name", "selftest"),
                ):
                    subprocess.run(["git", "-C", os.fspath(dirty_repo), *arguments], check=True, capture_output=True, timeout=30)
                (dirty_repo / "tracked.py").write_text("print(1)\n", encoding="utf-8")
                subprocess.run(["git", "-C", os.fspath(dirty_repo), "add", "tracked.py"], check=True, capture_output=True, timeout=30)
                subprocess.run(["git", "-C", os.fspath(dirty_repo), "commit", "-q", "-m", "seed"], check=True, capture_output=True, timeout=30)
                clean_count = _uncommitted_changes(dirty_repo)
                (dirty_repo / "tracked.py").write_text("print(2)\n", encoding="utf-8")
                dirty_count = _uncommitted_changes(dirty_repo)
            except (OSError, subprocess.SubprocessError):
                git_ok = False
            checks.append((
                "doctor can tell a clean checkout from one whose live hooks run uncommitted code",
                (not git_ok)
                or (clean_count == 0 and dirty_count == 1 and _uncommitted_changes(home / "not-a-repo") is None),
            ))

            before_uninstall_dry = _tree_digest(home)
            uninstall_dry_output = io.StringIO()
            uninstall_dry_code = _uninstall(home, dry_run=True, output=uninstall_dry_output)
            checks.append((
                "uninstall dry-run reports without writes",
                uninstall_dry_code == 0
                and before_uninstall_dry == _tree_digest(home)
                and "DRY-RUN remove" in uninstall_dry_output.getvalue()
                and "no files changed" in uninstall_dry_output.getvalue(),
            ))

            installed_claude = claude.read_bytes()
            installed_codex = codex_hooks.read_bytes()
            installed_shims = _tree_digest(hooks_root)
            second_code = _install(home, output=io.StringIO(), repo_root=old_repo)
            checks.append((
                "repeat install is idempotent",
                second_code == 0
                and claude.read_bytes() == installed_claude
                and codex_hooks.read_bytes() == installed_codex
                and _tree_digest(hooks_root) == installed_shims
                and all(_marker_count(claude)[event] == 1 for event in EVENTS)
                and all(_marker_count(codex_hooks)[event] == 1 for event in EVENTS),
            ))

            curated_home = root / "curated-home"
            (curated_home / ".claude" / "memory").mkdir(parents=True)
            curated_settings = curated_home / ".claude" / "settings.json"
            curated_settings_source = b'{"hooks":{},"sentinel":"curated-round-trip"}\n'
            curated_settings.write_bytes(curated_settings_source)
            detected_vault = curated_home / ".claude" / "memory"
            (detected_vault / "detected.md").write_text("detected\n", encoding="utf-8")
            curated_a = root / "curated-a"
            curated_b = root / "curated-b"
            curated_a.mkdir()
            curated_b.mkdir()
            curated_config_dir = curated_home / CONFIG_DIRECTORY
            curated_config_dir.mkdir()
            curated_config = curated_config_dir / CONFIG_FILENAME
            curated_paths = [os.fspath(curated_a.resolve()), os.fspath(curated_b.resolve())]
            curated_config.write_bytes(
                b'{\r\n  "vaults": '
                + json.dumps(curated_paths, ensure_ascii=False, separators=(", ", ": ")).encode("utf-8")
                + b',\r\n  "budget_bytes": 4096\r\n}\r\n'
            )
            curated_before_text = curated_config.read_text(encoding="utf-8")
            curated_before_member = _member(_parse_json(curated_before_text), "vaults")
            curated_vault_bytes = curated_before_text[
                curated_before_member.value.start : curated_before_member.value.end
            ].encode("utf-8")
            curated_output = io.StringIO()
            curated_code = _install(curated_home, output=curated_output, repo_root=old_repo)
            curated_after_text = curated_config.read_text(encoding="utf-8")
            curated_after_member = _member(_parse_json(curated_after_text), "vaults")
            checks.append((
                "repeat install preserves curated vault bytes instead of detected vaults",
                curated_code == 0
                and curated_after_text[
                    curated_after_member.value.start : curated_after_member.value.end
                ].encode("utf-8") == curated_vault_bytes
                and json.loads(curated_after_text)["vaults"] == curated_paths
                and "VAULTS: preserved 2 from config" in curated_output.getvalue()
                and "VAULTS: detection found 1; not adopted (use the resync command)"
                in curated_output.getvalue(),
            ))

            adopt_home = root / "adopt-home"
            (adopt_home / ".claude" / "memory").mkdir(parents=True)
            (adopt_home / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
            adopted_vault = adopt_home / ".claude" / "memory"
            (adopted_vault / "native.md").write_text("native\n", encoding="utf-8")
            (adopt_home / CONFIG_DIRECTORY).mkdir()
            adopt_config = adopt_home / CONFIG_DIRECTORY / CONFIG_FILENAME
            adopt_config.write_text('{"budget_bytes":4096}\n', encoding="utf-8")
            adopt_code = _install(adopt_home, output=io.StringIO(), repo_root=old_repo)
            checks.append((
                "install adopts detection when config has no vaults key",
                adopt_code == 0
                and json.loads(adopt_config.read_text(encoding="utf-8"))["vaults"]
                == [os.fspath(adopted_vault.resolve())],
            ))

            stale_home = root / "stale-home"
            (stale_home / ".claude" / "memory").mkdir(parents=True)
            (stale_home / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
            (stale_home / ".claude" / "memory" / "detected.md").write_text(
                "detected\n",
                encoding="utf-8",
            )
            stale_a = root / "stale-curated-a"
            stale_b = root / "stale-curated-b"
            stale_missing = root / "stale-curated-missing"
            stale_a.mkdir()
            stale_b.mkdir()
            (stale_home / CONFIG_DIRECTORY).mkdir()
            stale_config = stale_home / CONFIG_DIRECTORY / CONFIG_FILENAME
            stale_config.write_text(
                json.dumps({
                    "vaults": [
                        os.fspath(stale_a.resolve()),
                        os.fspath(stale_missing.resolve()),
                        os.fspath(stale_b.resolve()),
                    ]
                }),
                encoding="utf-8",
            )
            stale_output = io.StringIO()
            stale_code = _install(stale_home, output=stale_output, repo_root=old_repo)
            checks.append((
                "one stale curated entry keeps the other entries and rejects detection",
                stale_code == 0
                and json.loads(stale_config.read_text(encoding="utf-8"))["vaults"]
                == [os.fspath(stale_a.resolve()), os.fspath(stale_b.resolve())]
                and f"VAULTS: stale entry {stale_missing.resolve()}" in stale_output.getvalue()
                and "VAULTS: preserved 2 from config" in stale_output.getvalue()
                and "VAULTS: detection found 1; not adopted (use the resync command)"
                in stale_output.getvalue(),
            ))

            before_resync_tree = _tree_digest(curated_home)
            resync_dry_output = io.StringIO()
            resync_dry_code = _resync_vaults(curated_home, dry_run=True, output=resync_dry_output)
            after_resync_dry_tree = _tree_digest(curated_home)
            before_resync_config = curated_config.read_bytes()
            resync_output = io.StringIO()
            resync_code = _resync_vaults(curated_home, output=resync_output)
            resync_backups = list(curated_config.parent.glob(CONFIG_FILENAME + ".bak_epitype_*"))
            checks.append((
                "explicit vault resync previews without writes then backs up and adopts detection",
                resync_dry_code == 0
                and before_resync_tree == after_resync_dry_tree
                and "DRY-RUN complete; no files changed." in resync_dry_output.getvalue()
                and resync_code == 0
                and json.loads(curated_config.read_text(encoding="utf-8"))["vaults"]
                == [os.fspath(detected_vault.resolve())]
                and any(path.read_bytes() == before_resync_config for path in resync_backups)
                and "VAULTS: detection found 1; resync requested" in resync_output.getvalue()
                and "vaults" in _parser().format_help(),
            ))

            hygiene_home = root / "detection-hygiene-home"
            card_candidate = hygiene_home / ".claude" / "projects" / "with-card" / "memory"
            empty_candidate = hygiene_home / ".claude" / "projects" / "empty" / "memory"
            card_candidate.mkdir(parents=True)
            empty_candidate.mkdir(parents=True)
            (card_candidate / "card.md").write_text("card\n", encoding="utf-8")
            checks.append((
                "native detection skips candidate directories with no markdown cards",
                _detect_native_vaults(hygiene_home, ("claude",)) == [card_candidate.resolve()],
            ))

            curated_uninstall_code = _uninstall(curated_home, output=io.StringIO())
            checks.append((
                "curated install and uninstall round trip keeps host bytes exact",
                curated_uninstall_code == 0
                and curated_settings.read_bytes() == curated_settings_source,
            ))

            config_path = home / CONFIG_DIRECTORY / CONFIG_FILENAME
            installed_config = config_path.read_bytes()
            broken_config = json.loads(installed_config)
            broken_config["repo_root"] = os.fspath(root / "missing-repo-root")
            config_path.write_text(
                json.dumps(broken_config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            canary = "SYNTHETIC-CANARY-STRING"
            shim_environment = _home_environment(home)
            shim_environment.pop("EPITYPE_CONFIG", None)
            fail_open_result = subprocess.run(
                [sys.executable, os.fspath(hooks_root / "pretooluse.py")],
                input=json.dumps({"tool_name": "SyntheticRead", "tool_input": {"probe": canary}}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=shim_environment,
                timeout=10,
                check=False,
            )
            status_path = _shim_status_path(home)
            fail_open_status_bytes = status_path.read_bytes()
            fail_open_status = json.loads(fail_open_status_bytes)
            fail_open_record = fail_open_status.get("shims", {}).get("pretooluse.py", {})
            fail_open_doctor_output = io.StringIO()
            fail_open_doctor_code = _doctor(home, output=fail_open_doctor_output)
            checks.append((
                "repo-root fail-open stays silent, records reason, and fails doctor",
                fail_open_result.returncode == 0
                and fail_open_result.stdout == ""
                and fail_open_result.stderr == ""
                and fail_open_record.get("shim") == "pretooluse.py"
                and fail_open_record.get("reason") == "repo_root_not_dir"
                and fail_open_doctor_code == 1
                and "SHIM FAIL-OPEN SEEN: pretooluse.py repo_root_not_dir "
                in fail_open_doctor_output.getvalue()
                and "DOCTOR FAIL" in fail_open_doctor_output.getvalue(),
            ))
            checks.append((
                "shim breadcrumb excludes synthetic event content",
                canary.encode("utf-8") not in fail_open_status_bytes,
            ))
            config_path.write_bytes(installed_config)
            clear_output = io.StringIO()
            clear_code = _doctor(home, output=clear_output, clear_shim_status=True)
            checks.append((
                "doctor clear removes breadcrumb and restores healthy result",
                clear_code == 0
                and not status_path.exists()
                and "SHIM STATUS CLEARED:" in clear_output.getvalue()
                and f"HEALTH PASS {len(EVENTS)}/{len(EVENTS)}" in clear_output.getvalue(),
            ))

            missing_adapter = old_repo / "adapters" / "claude" / "pretooluse_gate.py"
            missing_adapter_bytes = missing_adapter.read_bytes()
            missing_adapter.unlink()
            missing_adapter_output = io.StringIO()
            missing_adapter_code = _doctor(home, output=missing_adapter_output)
            missing_adapter_text = missing_adapter_output.getvalue()
            missing_adapter.write_bytes(missing_adapter_bytes)
            recovered_code = _doctor(
                home,
                output=io.StringIO(),
                clear_shim_status=True,
            )
            checks.append((
                "missing adapter fails hook health for lack of positive trace",
                missing_adapter_code == 1
                and "HOOK PreToolUse: FAIL" in missing_adapter_text
                and "REASON PreToolUse: no-trace" in missing_adapter_text
                and f"HEALTH FAIL {len(EVENTS) - 1}/{len(EVENTS)}" in missing_adapter_text
                and recovered_code == 0,
            ))

            blocked_home = root / "blocked-status-home"
            blocked_config_dir = blocked_home / CONFIG_DIRECTORY
            blocked_config_dir.mkdir(parents=True)
            (blocked_config_dir / CONFIG_FILENAME).write_text(
                json.dumps({"repo_root": os.fspath(root / "also-missing")}),
                encoding="utf-8",
            )
            (blocked_config_dir / SHIM_STATUS_FILENAME).mkdir()
            blocked_environment = _home_environment(blocked_home)
            blocked_environment.pop("EPITYPE_CONFIG", None)
            blocked_result = subprocess.run(
                [sys.executable, os.fspath(hooks_root / "pretooluse.py")],
                input=json.dumps({"tool_name": "SyntheticRead", "tool_input": {"probe": canary}}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=blocked_environment,
                timeout=10,
                check=False,
            )
            checks.append((
                "breadcrumb write failure cannot infect fail-open behavior",
                blocked_result.returncode == 0
                and blocked_result.stdout == ""
                and blocked_result.stderr == "",
            ))

            moved_repo = root / "repo-after-move"
            shutil.move(os.fspath(old_repo), os.fspath(moved_repo))
            before_relocate_hosts = (claude.read_bytes(), codex_hooks.read_bytes())
            relocate_output = io.StringIO()
            relocate_code = _relocate(home, moved_repo, output=relocate_output)
            relocated_config = json.loads(
                (home / CONFIG_DIRECTORY / CONFIG_FILENAME).read_text(encoding="utf-8")
            )
            relocate_text = relocate_output.getvalue()
            checks.append((
                "moved repo relocates through config and every shim passes",
                relocate_code == 0
                and relocated_config.get("repo_root") == os.fspath(moved_repo.resolve())
                and before_relocate_hosts == (claude.read_bytes(), codex_hooks.read_bytes())
                and all(f"HOOK {event}: PASS" in relocate_text for event in EVENTS)
                and f"SHIM RESOLUTION: PASS {len(SHIM_NAMES)}/{len(SHIM_NAMES)}" in relocate_text
                and f"HEALTH PASS {len(EVENTS)}/{len(EVENTS)}" in relocate_text
                and "HOST CONFIG: UNCHANGED" in relocate_text,
            ))

            missing_home = root / "missing-root-home"
            missing_hooks = missing_home / CONFIG_DIRECTORY / HOOK_DIRECTORY
            missing_hooks.mkdir(parents=True)
            for shim_name, payload in _shim_payloads(moved_repo).items():
                (missing_hooks / shim_name).write_bytes(payload)
            (missing_home / CONFIG_DIRECTORY / CONFIG_FILENAME).write_text(
                json.dumps({"vaults": [os.fspath(fallback)]}),
                encoding="utf-8",
            )
            missing_environment = _home_environment(missing_home)
            missing_environment.pop("EPITYPE_CONFIG", None)
            missing_results = [
                subprocess.run(
                    [sys.executable, os.fspath(missing_hooks / shim_name)],
                    input='{"synthetic":true}',
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=missing_environment,
                    timeout=10,
                    check=False,
                )
                for shim_name, _ in HOOK_SPECS.values()
            ]
            checks.append((
                "shims fail open silently when config lacks repo_root",
                all(
                    result.returncode == 0 and result.stdout == "" and result.stderr == ""
                    for result in missing_results
                )
                and set(_read_shim_status(missing_home)) == set(SHIM_NAMES)
                and all(
                    record["reason"] == "repo_root_missing"
                    for record in _read_shim_status(missing_home).values()
                ),
            ))

            card = fallback / "synthetic-card.md"
            card.write_text("synthetic card\n", encoding="utf-8")
            status_path.write_bytes(fail_open_status_bytes)
            uninstall_code = _uninstall(home, output=io.StringIO())
            checks.append((
                "uninstall restores hook files byte-for-byte",
                uninstall_code == 0
                and claude.read_bytes() == claude_source
                and codex_hooks.read_bytes() == codex_source
                and codex_config.read_bytes() == config_source,
            ))
            checks.append((
                "uninstall removes config, shims, and shim status but preserves vault cards",
                not (home / CONFIG_DIRECTORY).exists()
                and not hooks_root.exists()
                and not status_path.exists()
                and card.read_text(encoding="utf-8") == "synthetic card\n",
            ))

            dry_home = root / "dry-home"
            (dry_home / ".claude").mkdir(parents=True)
            dry_settings = dry_home / ".claude" / "settings.json"
            dry_settings.write_text("{\"hooks\":{}}\n", encoding="utf-8")
            before_dry = _tree_digest(dry_home)
            dry_output = io.StringIO()
            dry_code = _install(dry_home, dry_run=True, output=dry_output)
            checks.append((
                "dry-run reports every location without writes",
                dry_code == 0
                and before_dry == _tree_digest(dry_home)
                and all(f"hooks.{event}[id=epitype]" in dry_output.getvalue() for event in EVENTS)
                and "no files changed" in dry_output.getvalue(),
            ))

            # --- 夢的排程：模式寫進 config，nightly 才碰系統排程 ---
            class _FakeScheduler:
                def __init__(self):
                    self.calls = []
                    self.command = []
                    self.at = "03:30"
                    self.cron = ""

                def __call__(self, argv, stdin_text=None):
                    self.calls.append((list(argv), stdin_text))
                    body = ""
                    if argv[:2] == ["schtasks", "/Create"]:
                        self.command = [part.strip('"') for part in shlex.split(argv[argv.index("/TR") + 1], posix=False)]
                        self.at = argv[argv.index("/ST") + 1]
                    elif argv[:2] == ["schtasks", "/Query"]:
                        task = ET.Element("Task")
                        action = ET.SubElement(ET.SubElement(task, "Actions"), "Exec")
                        ET.SubElement(action, "Command").text = self.command[0]
                        ET.SubElement(action, "Arguments").text = _dream_command_text(self.command[1:])
                        trigger = ET.SubElement(ET.SubElement(task, "Triggers"), "CalendarTrigger")
                        ET.SubElement(trigger, "StartBoundary").text = f"2026-01-01T{self.at}:00"
                        ET.SubElement(ET.SubElement(trigger, "ScheduleByDay"), "DaysInterval").text = "1"
                        body = ET.tostring(task, encoding="unicode")
                    elif argv == ["crontab", "-"]:
                        self.cron = stdin_text
                    elif argv == ["crontab", "-l"]:
                        body = self.cron
                    return subprocess.CompletedProcess(list(argv), 0, body, "")

            dream_home = root / "dream-home"
            (dream_home / ".claude").mkdir(parents=True)
            (dream_home / ".claude" / "settings.json").write_text("{\"hooks\":{}}\n", encoding="utf-8")
            dream_dry_output = io.StringIO()
            dream_dry_code = _install(
                dream_home, dry_run=True, output=dream_dry_output,
                dream_mode="nightly", dream_at="04:15", scheduler=_FakeScheduler(),
            )
            dry_text = dream_dry_output.getvalue()
            schedule_marker = f"/ST 04:15" if os.name == "nt" else "15 4 * * *"
            checks.append((
                "install --dream nightly --dry-run shows the exact schedule command and writes nothing",
                dream_dry_code == 0
                and "DRY-RUN schedule" in dry_text
                and schedule_marker in dry_text
                and DREAM_SCHEDULED_FLAG in dry_text
                and os.fspath((REPO_ROOT / Path(*DREAM_SCRIPT_PARTS)).resolve()) in dry_text
                and not (dream_home / CONFIG_DIRECTORY).exists(),
            ))

            windowless_root = root / "pythonbin"
            windowless_root.mkdir()
            console_python = windowless_root / "python.exe"
            console_python.write_text("", encoding="utf-8")
            (windowless_root / "pythonw.exe").write_text("", encoding="utf-8")
            lonely_python = windowless_root / "other.exe"
            lonely_python.write_text("", encoding="utf-8")
            checks.append((
                "Windows 排程改用旁邊的 pythonw.exe（凌晨不閃黑窗）；沒有就維持原來的直譯器",
                _windowless_python(os.fspath(console_python), windows=True)
                == os.fspath(windowless_root / "pythonw.exe")
                and _windowless_python(os.fspath(console_python), windows=False)
                == os.fspath(console_python)
                and _windowless_python(os.fspath(lonely_python), windows=True)
                == os.fspath(lonely_python)
                and (
                    os.name != "nt"
                    or _dream_command(REPO_ROOT, os.fspath(console_python))[0]
                    == os.fspath(windowless_root / "pythonw.exe")
                ),
            ))

            scheduler = _FakeScheduler()
            dream_code = _install(
                dream_home, output=io.StringIO(),
                dream_mode="nightly", dream_at="04:15", scheduler=scheduler,
            )
            dream_config_path = dream_home / CONFIG_DIRECTORY / CONFIG_FILENAME
            dream_config_value = json.loads(dream_config_path.read_text(encoding="utf-8"))
            registrations = [call for call in scheduler.calls
                             if call[0][:2] == ["schtasks", "/Create"] or call[0] == ["crontab", "-"]]
            registered, registered_stdin = registrations[-1] if registrations else ([], None)
            if os.name == "nt":
                registered_ok = (
                    registered[:2] == ["schtasks", "/Create"]
                    and "/F" in registered
                    and registered[registered.index("/SC") + 1] == "DAILY"
                    and registered[registered.index("/TN") + 1] == DREAM_TASK_NAME
                    and registered[registered.index("/ST") + 1] == "04:15"
                    and registered[registered.index("/TR") + 1].endswith(DREAM_SCHEDULED_FLAG)
                )
            else:
                registered_ok = (
                    registered == ["crontab", "-"]
                    and registered_stdin.strip().splitlines()[-1].startswith("15 4 * * *")
                    and registered_stdin.strip().endswith(DREAM_CRON_MARKER)
                )
            checks.append((
                "install --dream nightly records the mode in config and registers one daily task",
                dream_code == 0
                and dream_config_value[DREAM_CONFIG_FIELD]
                == {DREAM_MODE_FIELD: "nightly", DREAM_INTERVAL_HOURS_FIELD: 24, DREAM_AT_FIELD: "04:15"}
                and registered_ok,
            ))

            dream_vault = Path(dream_config_value["vaults"][0])
            (dream_vault / DREAM_DIRECTORY).mkdir(parents=True, exist_ok=True)
            (dream_vault / DREAM_DIRECTORY / DREAM_STATE_FILENAME).write_text(
                json.dumps({"completed_at": "2026-09-06T03:30:00+00:00"}), encoding="utf-8"
            )
            dream_doctor_output = io.StringIO()
            dream_doctor_code = _doctor(dream_home, output=dream_doctor_output, scheduler=scheduler)
            checks.append((
                "doctor reports the dream mode and the last completion time",
                dream_doctor_code == 0
                and "DREAM: mode=nightly interval_hours=24 at=04:15 last=2026-09-06T03:30:00+00:00"
                in dream_doctor_output.getvalue(),
            ))

            off_scheduler = _FakeScheduler()
            off_code = _install(dream_home, output=io.StringIO(), dream_mode="off", scheduler=off_scheduler)
            off_argv = off_scheduler.calls[-1][0] if off_scheduler.calls else []
            off_mode = json.loads(dream_config_path.read_text(encoding="utf-8"))[DREAM_CONFIG_FIELD][DREAM_MODE_FIELD]
            uninstall_scheduler = _FakeScheduler()
            dream_config_value = json.loads(dream_config_path.read_text(encoding="utf-8"))
            dream_config_value[DREAM_CONFIG_FIELD][DREAM_MODE_FIELD] = "nightly"
            dream_config_path.write_text(json.dumps(dream_config_value, indent=2) + "\n", encoding="utf-8")
            uninstall_code = _uninstall(dream_home, output=io.StringIO(), scheduler=uninstall_scheduler)
            uninstall_argv = uninstall_scheduler.calls[-1][0] if uninstall_scheduler.calls else []
            expected_unschedule = ["schtasks", "/Delete", "/TN", DREAM_TASK_NAME, "/F"] if os.name == "nt" else ["crontab", "-"]
            checks.append((
                "switching off, and uninstalling, both unregister the nightly task",
                off_code == 0
                and off_mode == "off"
                and off_argv == expected_unschedule
                and uninstall_code == 0
                and uninstall_argv == expected_unschedule,
            ))

            def _crontab_runner(returncode, stdout, stderr=""):
                return lambda argv, stdin_text=None: subprocess.CompletedProcess(
                    list(argv), returncode, stdout, stderr
                )

            checks.append((
                "crontab -l 非零又印了內容＝讀不完整，放棄註冊而不是把整份 crontab 換掉",
                _crontab_without_dream(_crontab_runner(1, "0 5 * * * backup\n")) is None
                and _crontab_without_dream(_crontab_runner(1, "")) is None
                and _crontab_without_dream(_crontab_runner(1, "", "no crontab for test")) == []
                and _crontab_without_dream(
                    _crontab_runner(0, f"0 5 * * * backup\n1 2 * * * old {DREAM_CRON_MARKER}\n")
                )
                == ["0 5 * * * backup"],
            ))

            if os.fspath(REPO_ROOT) not in sys.path:
                sys.path.insert(0, os.fspath(REPO_ROOT))
            from epitype import memspec as _memspec

            checks.append((
                "the installer's mirrored dream constants still match memspec",
                (DREAM_CONFIG_FIELD, DREAM_MODE_FIELD, DREAM_INTERVAL_HOURS_FIELD, DREAM_AT_FIELD)
                == (
                    _memspec.DREAM_CONFIG_FIELD,
                    _memspec.DREAM_MODE_FIELD,
                    _memspec.DREAM_INTERVAL_HOURS_FIELD,
                    _memspec.DREAM_AT_FIELD,
                )
                and DREAM_MODES == _memspec.DREAM_MODES
                and DREAM_DEFAULT_MODE == _memspec.DREAM_DEFAULT_MODE
                and DREAM_DEFAULT_INTERVAL_HOURS == _memspec.DREAM_DEFAULT_INTERVAL_HOURS
                and DREAM_DEFAULT_AT == _memspec.DREAM_DEFAULT_AT
                and DREAM_DIRECTORY == _memspec.DREAM_DIRECTORY
                and DREAM_STATE_FILENAME == _memspec.DREAM_STATE_FILENAME
                and DREAM_SCHEDULED_FLAG == _memspec.DREAM_SCHEDULED_FLAG
                and DREAM_MODE_ENV == _memspec.DREAM_MODE_ENV
                and WORK_LEDGER_FILENAME == _memspec.WORK_LEDGER_FILENAME
                and DREAM_AT_REGEX.pattern == _memspec.DREAM_AT_PATTERN,
            ))

            legacy_home = root / "legacy-home"
            (legacy_home / ".claude").mkdir(parents=True)
            legacy_settings = legacy_home / ".claude" / "settings.json"
            legacy_hooks = {
                event: [{
                    "id": MARKER_VALUE,
                    "hooks": [{
                        "type": "command",
                        "command": f'python "C:/old-repo/adapters/claude/{adapter_name}"',
                    }],
                }]
                for event, (_, adapter_name) in HOOK_SPECS.items()
            }
            legacy_settings.write_text(
                json.dumps({"hooks": legacy_hooks}, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            legacy_code = _install(legacy_home, output=io.StringIO())
            legacy_root = legacy_home / CONFIG_DIRECTORY / HOOK_DIRECTORY
            legacy_expected = _hook_template(False, legacy_root)
            checks.append((
                "repeat install upgrades legacy absolute adapter registrations",
                legacy_code == 0
                and _marked_entries(legacy_settings)
                == {event: [legacy_expected[event]] for event in EVENTS}
                and "old-repo" not in legacy_settings.read_text(encoding="utf-8"),
            ))

            later_home = root / "later-home"
            (later_home / ".claude").mkdir(parents=True)
            later_settings = later_home / ".claude" / "settings.json"
            later_settings.write_text('{"sentinel":"keep","hooks":{"SessionStart":[]}}\n', encoding="utf-8")
            _install(later_home, output=io.StringIO())
            later_value = json.loads(later_settings.read_text(encoding="utf-8"))
            later_entry = {"id": "later-user", "hooks": [{"type": "command", "command": "synthetic"}]}
            later_value["hooks"]["SessionStart"].append(later_entry)
            later_settings.write_text(
                json.dumps(later_value, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            later_entry_bytes = json.dumps(later_entry, separators=(",", ":")).encode("utf-8")
            _uninstall(later_home, output=io.StringIO())
            later_after = later_settings.read_bytes()
            later_after_value = json.loads(later_after)
            checks.append((
                "uninstall preserves hooks added after installation",
                later_entry_bytes in later_after
                and later_after_value["sentinel"] == "keep"
                and later_after_value["hooks"]["SessionStart"] == [later_entry]
                and not any(
                    _marked(entry)
                    for entries in later_after_value["hooks"].values()
                    for entry in entries
                ),
            ))

            guard_file = root / "guard-settings.json"
            guard_source = b'{"hooks":{}}\n'
            guard_file.write_bytes(guard_source)
            guard_transaction = Transaction()
            guard_transaction.write(guard_file, b'{"hooks":{},"DISABLE_AUTO_MEMORY":true}\n')
            rejected = False
            try:
                _assert_native_protection(
                    {guard_file: guard_source},
                    {guard_file: guard_file.read_bytes()},
                )
            except InstallError:
                rejected = True
                guard_transaction.rollback()
            unchanged_allowed = True
            try:
                _assert_native_protection(
                    {guard_file: b'{"DISABLE_AUTO_MEMORY":true}\n'},
                    {guard_file: b'{"DISABLE_AUTO_MEMORY":true,"hooks":{}}\n'},
                )
            except InstallError:
                unchanged_allowed = False
            checks.append((
                "native-memory disable diff aborts and rolls back",
                rejected and unchanged_allowed and guard_file.read_bytes() == guard_source,
            ))

            nested_home = root / "nested-vault-home"
            nested_config_dir = nested_home / CONFIG_DIRECTORY
            nested_vault = nested_config_dir / "vault"
            nested_vault.mkdir(parents=True)
            nested_card = nested_vault / "synthetic-card.md"
            nested_card.write_text("preserve\n", encoding="utf-8")
            (nested_config_dir / CONFIG_FILENAME).write_text(
                json.dumps({"vaults": [os.fspath(nested_vault.resolve())]}),
                encoding="utf-8",
            )
            nested_rejected = False
            try:
                _uninstall(nested_home, output=io.StringIO())
            except InstallError:
                nested_rejected = True
            checks.append((
                "uninstall refuses a vault nested under config removal",
                nested_rejected and nested_card.read_text(encoding="utf-8") == "preserve\n",
            ))

            native_home = root / "native-home"
            (native_home / ".codex" / "memories").mkdir(parents=True)
            (native_home / ".codex" / "memories" / "native.md").write_text(
                "native\n",
                encoding="utf-8",
            )
            native_config = native_home / ".codex" / "config.toml"
            native_hooks = native_home / ".codex" / "hooks.json"
            native_config.write_text(
                'model = "gpt-5.6-sol"\nmodel_context_window = 300000\nmodel_auto_compact_token_limit = 260000\n',
                encoding="utf-8",
            )
            native_hooks.write_text("{}\n", encoding="utf-8")
            apply_code = _install(native_home, apply_billing_guard=True, output=io.StringIO())
            applied_text = native_config.read_text(encoding="utf-8")
            checks.append((
                "native vault detection and explicit billing apply",
                apply_code == 0
                and not (native_home / FALLBACK_VAULT).exists()
                and "model_context_window = 240000" in applied_text
                and "model_auto_compact_token_limit = 210000" in applied_text,
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 41
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _common_options(parser):
    parser.add_argument("--home", type=Path, default=argparse.SUPPRESS, help="override the user home directory")
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="report every planned edit without writing")


def _parser():
    common = argparse.ArgumentParser(add_help=False)
    _common_options(common)
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", parents=[common], help="merge and verify Epitype hooks")
    install.add_argument("--apply-billing-guard", action="store_true", help="apply, rather than only report, Codex billing guard settings")
    install.add_argument(
        "--dream",
        choices=DREAM_MODES,
        default=None,
        help="offline tidy batch: piggyback on session start (default), a nightly system schedule, or off",
    )
    install.add_argument(
        "--at",
        default=None,
        help=f"HH:MM for --dream nightly (default {DREAM_DEFAULT_AT})",
    )
    commands.add_parser("uninstall", parents=[common], help="remove only Epitype-owned registrations")
    doctor = commands.add_parser("doctor", parents=[common], help="inspect registrations and exercise synthetic hook stdin")
    doctor.add_argument(
        "--clear-shim-status",
        action="store_true",
        help="clear recorded shim fail-open breadcrumbs before running health checks",
    )
    vaults = commands.add_parser("vaults", parents=[common], help="manage the configured vault list")
    vaults.add_argument(
        "--resync",
        action="store_true",
        required=True,
        help="replace the configured vault list with current native detection",
    )
    relocate = commands.add_parser("relocate", parents=[common], help="point stable shims at a moved Epitype repository")
    relocate.add_argument("--to", type=Path, required=True, help="new Epitype repository root")
    return parser


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    parsed = _parser().parse_args(arguments)
    home = getattr(parsed, "home", Path.home()).expanduser().resolve()
    dry_run = getattr(parsed, "dry_run", False)
    try:
        if parsed.command == "install":
            return _install(
                home,
                dry_run,
                parsed.apply_billing_guard,
                dream_mode=parsed.dream,
                dream_at=parsed.at,
            )
        if parsed.command == "uninstall":
            return _uninstall(home, dry_run)
        if parsed.command == "vaults":
            return _resync_vaults(home, dry_run)
        if parsed.command == "relocate":
            return _relocate(home, parsed.to, dry_run)
        return _doctor(home, dry_run, clear_shim_status=parsed.clear_shim_status)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
