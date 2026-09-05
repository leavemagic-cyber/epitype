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
import shutil
import subprocess
import tempfile
import time
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
MARKER_VALUE = "epitype"
MARKER_FIELDS = ("id", "comment")
EVENTS = ("SessionStart", "UserPromptSubmit", "PreCompact", "PreToolUse")
STATE_VERSION = 1
CONFIG_DIRECTORY = ".epitype"
CONFIG_FILENAME = "config.json"
STATE_FILENAME = "install_state.json"
SHIM_STATUS_FILENAME = "shim_status.json"
HOOK_DIRECTORY = "hooks"
BACKUP_INFIX = ".bak_epitype_"
BACKUPS_KEPT = 3
FALLBACK_VAULT = ".epitype-vault"
SHIM_ADAPTER_TOKEN = "__EPITYPE_ADAPTER_FILENAME__"
SHIM_TRACE_ENV = "EPITYPE_SHIM_TRACE"
HOOK_SPECS = {
    "SessionStart": ("sessionstart.py", "sessionstart_hook.py"),
    "UserPromptSubmit": ("recall.py", "recall_hook.py"),
    "PreCompact": ("precompact.py", "precompact_hook.py"),
    "PreToolUse": ("pretooluse.py", "pretooluse_gate.py"),
}
SHIM_NAMES = tuple(shim_name for shim_name, _ in HOOK_SPECS.values())
U12_SHIM_SHA256 = {
    "sessionstart.py": "49932f9dff4d80ebc14045dd619289246afee9ba319bb37f3a786fbdaf746001",
    "recall.py": "06d81067cd22c3a38c74980e13155d9b38eac5393677959d8ec5fedfdfe29085",
    "precompact.py": "22386bf486c9e305d0439fe3cfb814a1e980fe3e96ef88a7769c987e8d416d99",
    "pretooluse.py": "9c2cdb7d145eeba56e532a3ceae639375ae8952960d3f6d56191f79b03673ff2",
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
            raw = raw.replace("{{EPITYPE_HOOKS_ROOT}}", hooks_text)
            raw = raw.replace("{{PYTHON_EXECUTABLE}}", python_text)
            if not codex:
                raw = raw.replace(" --codex", "")
            command["command"] = raw
        result[event] = entry
    return result


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


def _config_bytes(path, vaults, repo_root, preserve_vault_bytes=False):
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
        text = _set_object_member(text, "repo_root", os.fspath(repo_root.resolve()))
        encoded = text.encode("utf-8")
        return (b"\xef\xbb\xbf" if bom else b"") + encoded
    else:
        value = {}
    value["vaults"] = [os.fspath(path) for path in vaults]
    value.setdefault("budget_bytes", 10 * 1024)
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


def _merge_target_state(state, name, path, created_hooks, created_events):
    targets = state.setdefault("targets", {})
    previous = targets.get(name, {})
    previous_events = previous.get("created_events", ())
    targets[name] = {
        "path": os.fspath(path),
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
                if hook_key != "command":
                    if actual_hook.get(hook_key) != hook_value:
                        return False
                    continue
                python, rest = _split_command(actual_hook.get("command"))
                expected_python, expected_rest = _split_command(hook_value)
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


def _doctor(home, dry_run=False, output=sys.stdout, clear_shim_status=False):
    hosts = _detect_hosts(home)
    print("HOSTS: " + (", ".join(hosts) if hosts else "none"), file=output)
    if dry_run:
        records = _read_shim_status(home)
        _report_shim_status(records, output)
        if clear_shim_status:
            _clear_shim_status(home, True, output)
        print("DRY-RUN hooks: SessionStart, UserPromptSubmit, PreCompact, PreToolUse", file=output)
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
        print(f"SHIM RESOLUTION: PASS 4/4 repo_root={repo_root}", file=output)
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
            print(f"REGISTRATION {name}: PASS 4/4", file=output)
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


def _install(home, dry_run=False, apply_billing_guard=False, output=sys.stdout, repo_root=REPO_ROOT):
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

        config_data = _config_bytes(config_path, vaults, repo_root, preserve_vault_bytes)
        if not config_path.exists() or config_path.read_bytes() != config_data:
            print(f"{'DRY-RUN write' if dry_run else 'WRITE'} {config_path}: vaults, repo_root", file=output)
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
            source = path.read_bytes() if path.is_file() else b"{}\n"
            merged, created_hooks, created_events, locations = _merge_hooks(source, entries)
            _merge_target_state(state, name, path, created_hooks, created_events)
            for location in locations:
                print(f"{'DRY-RUN merge' if dry_run else 'MERGE'} {path}: {location}", file=output)
            if merged != source:
                if path.exists():
                    print(f"BACKUP: {_planned_backup(path) if dry_run else 'pending'}", file=output)
                if not dry_run:
                    transaction.write(path, merged)

        if "codex" in hosts:
            _run_billing_guard(home, apply_billing_guard, dry_run, transaction, output, repo_root)

        if dry_run:
            print(f"DRY-RUN write {state_path}: install ownership metadata", file=output)
            print("DRY-RUN health: feed synthetic stdin to four hooks", file=output)
            print("DRY-RUN complete; no files changed.", file=output)
            return 0

        transaction.write(state_path, _state_bytes(state))
        protected_after = {path: path.read_bytes() for path in protected_paths if path.is_file()}
        _assert_native_protection(protected_before, protected_after)
        if _doctor(home, output=output) != 0:
            raise InstallError("post-install doctor failed")

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


def _relocate(home, target, dry_run=False, output=sys.stdout):
    repo_root = _validate_repo_root(target)
    config_path = home / CONFIG_DIRECTORY / CONFIG_FILENAME
    if not config_path.is_file():
        raise InstallError(f"Epitype config is missing: {config_path}")
    config_data = _repo_root_bytes(config_path, repo_root)
    changed = config_path.read_bytes() != config_data
    print(f"RELOCATE TARGET VALID: {repo_root}", file=output)
    if dry_run:
        if changed:
            print(f"DRY-RUN write {config_path}: repo_root", file=output)
        print("DRY-RUN complete; no files changed.", file=output)
        return 0

    transaction = Transaction()
    if changed:
        transaction.write(config_path, config_data)
    doctor_code = _doctor(home, output=output)
    print("RELOCATE REPORT", file=output)
    print(f"REPO ROOT: {repo_root}", file=output)
    print(f"CONFIG: {'CHANGED' if changed else 'UNCHANGED'} {config_path}", file=output)
    for source, backup in transaction.backups:
        print(f"BACKUP: {source} -> {backup}", file=output)
    print("HOST CONFIG: UNCHANGED", file=output)
    print(f"DOCTOR: {'PASS' if doctor_code == 0 else 'FAIL'}", file=output)
    return doctor_code


def _uninstall(home, dry_run=False, output=sys.stdout):
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
            if updated != path.read_bytes():
                if dry_run:
                    print(f"BACKUP: {_planned_backup(path)}", file=output)
                else:
                    transaction.write(path, updated)

        if config_dir.exists():
            print(f"{'DRY-RUN remove' if dry_run else 'REMOVE'} config directory: {config_dir}", file=output)
        if dry_run:
            print("DRY-RUN complete; no files changed.", file=output)
            return 0
        if config_dir.exists():
            shutil.rmtree(config_dir)
        print("UNINSTALL REPORT", file=output)
        for path in transaction.changed:
            print(f"CHANGED: {path}", file=output)
        for source, backup in transaction.backups:
            print(f"BACKUP: {source} -> {backup}", file=output)
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
            shutil.copytree(
                REPO_ROOT,
                old_repo,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"),
            )
            shim_payloads = _shim_payloads(old_repo)
            checks.append((
                "rendered shims are byte-identical to U12",
                set(shim_payloads) == set(U12_SHIM_SHA256)
                and all(
                    hashlib.sha256(payload).hexdigest() == U12_SHIM_SHA256[shim_name]
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
                and "HEALTH PASS 4/4" in first_output.getvalue(),
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
                    ("config", "user.email", "selftest@example.invalid"),
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
                and "HEALTH PASS 4/4" in clear_output.getvalue(),
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
                and "HEALTH FAIL 3/4" in missing_adapter_text
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
                "moved repo relocates through config and all four shims pass",
                relocate_code == 0
                and relocated_config.get("repo_root") == os.fspath(moved_repo.resolve())
                and before_relocate_hosts == (claude.read_bytes(), codex_hooks.read_bytes())
                and all(f"HOOK {event}: PASS" in relocate_text for event in EVENTS)
                and "SHIM RESOLUTION: PASS 4/4" in relocate_text
                and "HEALTH PASS 4/4" in relocate_text
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
    total = 32
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
            return _install(home, dry_run, parsed.apply_billing_guard)
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
