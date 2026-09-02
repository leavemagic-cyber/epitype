import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Shared fail-open mechanics for Claude hook adapters."""

import json
import os
from pathlib import Path
import re
import subprocess

from epitype import memspec

NATIVE_PROJECTS_SUBPATH = (".claude", "projects")
_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9]")


def expired(started_at):
    import time

    return time.monotonic() - started_at >= memspec.HOOK_TIMEOUT_SECONDS


def read_event(stream):
    value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("hook input must be a JSON object")
    return value


def load_config(started_at):
    if expired(started_at):
        return None
    configured = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".epitype" / "config.json"
    )
    raw = path.read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("config must be an object")

    raw_vaults = value.get(memspec.CONFIG_VAULTS_FIELD)
    if not isinstance(raw_vaults, list) or not raw_vaults:
        raise ValueError("config vaults must be a non-empty list")
    vaults = []
    for item in raw_vaults:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("vault paths must be non-empty strings")
        vault = Path(item).expanduser().resolve()
        if not vault.is_dir():
            raise NotADirectoryError(str(vault))
        vaults.append(vault)

    raw_budget = value.get(
        memspec.CONFIG_BUDGET_BYTES_FIELD,
        memspec.HOOK_DEFAULT_BUDGET_BYTES,
    )
    if isinstance(raw_budget, bool) or not isinstance(raw_budget, int):
        raise ValueError("budget_bytes must be a positive integer")
    budget = raw_budget
    if budget <= 0:
        raise ValueError("budget_bytes must be a positive integer")
    return {
        memspec.CONFIG_VAULTS_FIELD: vaults,
        memspec.CONFIG_BUDGET_BYTES_FIELD: min(
            budget,
            memspec.HOOK_DEFAULT_BUDGET_BYTES,
        ),
    }


def _holds_cards(vault):
    try:
        if (vault / memspec.MEMORY_INDEX_FILENAME).is_file():
            return True
        return any(
            item.suffix.lower() == ".md" and not item.name.startswith("_")
            for item in vault.iterdir()
        )
    except OSError:
        return False


def native_cwd_vaults(cwd, home=None):
    """Claude Code auto-creates one memory directory per cwd slug; cards written
    there must be recallable without editing config, so the cwd and each ancestor
    join the vault list whenever their directory already holds an index or a card.
    Empty auto-created shells are skipped so no index is planted in them."""
    if not isinstance(cwd, str) or not cwd.strip():
        return []
    projects = (home or Path.home()).joinpath(*NATIVE_PROJECTS_SUBPATH)
    try:
        start = Path(cwd)
        bases = (start, *start.parents)
    except (TypeError, ValueError):
        return []
    found = []
    for base in bases:
        texts = {str(base)}
        try:
            texts.add(str(base.resolve()))
        except OSError:
            pass
        for text in sorted(texts):
            candidate = projects / _SLUG_PATTERN.sub("-", text) / "memory"
            if candidate.is_dir() and _holds_cards(candidate):
                resolved = candidate.resolve()
                if resolved not in found:
                    found.append(resolved)
    return found


def resolve_vaults(config, event, home=None):
    """Closest native cwd vault first, then the configured vaults, deduplicated."""
    vaults = native_cwd_vaults(event.get("cwd") if isinstance(event, dict) else None, home)
    for vault in config[memspec.CONFIG_VAULTS_FIELD]:
        if vault not in vaults:
            vaults.append(vault)
    return vaults


def payload(event_name, context):
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }


def encode_payload(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def payload_fits(event_name, context, context_budget):
    if len(context.encode("utf-8")) > context_budget:
        return False
    encoded = encode_payload(payload(event_name, context)).encode("utf-8")
    return len(encoded) <= memspec.HOOK_MAX_OUTPUT_BYTES


def bounded_context(event_name, pieces, context_budget, required_first=False):
    selected = []
    for piece in pieces:
        if not isinstance(piece, str) or not piece:
            continue
        candidate = "\n".join(selected + [piece])
        if payload_fits(event_name, candidate, context_budget):
            selected.append(piece)
        elif required_first and not selected:
            return None
    return "\n".join(selected) if selected else None


def emit(value):
    encoded = encode_payload(value)
    if len(encoded.encode("utf-8")) > memspec.HOOK_MAX_OUTPUT_BYTES:
        raise ValueError("hook output exceeds the hard byte limit")
    print(encoded)


def run_synthetic(script, event, config_path, arguments=(), environment=None):
    environment = {**os.environ, **(environment or {})}
    environment[memspec.EPITYPE_CONFIG_ENV] = os.fspath(config_path)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, os.fspath(script), *arguments],
        input=json.dumps(event, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=10,
        check=False,
    )


def write_config(path, vaults, budget=memspec.HOOK_DEFAULT_BUDGET_BYTES):
    value = {
        memspec.CONFIG_VAULTS_FIELD: [os.fspath(item) for item in vaults],
        memspec.CONFIG_BUDGET_BYTES_FIELD: budget,
    }
    path.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding="utf-8",
    )
