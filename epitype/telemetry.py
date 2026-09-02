import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]
"""Privacy-bounded execution evidence for Epitype hooks."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time


TELEMETRY_FILENAME = "telemetry.jsonl"
HEARTBEAT_DIRECTORY = "heartbeat"
TEST_HOME_ENV = "EPITYPE_TEST_HOME"
MAX_BYTES = 512 * 1024
KEEP_BYTES = 256 * 1024
HEARTBEAT_SECONDS = 60.0
FIELDS = (
    "ts",
    "host",
    "event",
    "outcome",
    "hits",
    "injected_bytes",
    "terms",
    "vaults",
    "vault_skipped",
    "ms",
    "reason",
)
HOSTS = frozenset(("claude", "codex"))
OUTCOMES = frozenset(("hit", "miss", "deny", "allow", "fail-open", "error", "timeout"))
REASONS = frozenset(
    (
        "adapter-error",
        "auto-remedied",
        "card-deny",
        "config",
        "context-injected",
        "detector-error",
        "detector-timeout",
        "exception",
        "hook-error",
        "hook-timeout",
        "invalid-event",
        "map-written",
        "no-context",
        "no-hit",
        "no-index",
        "no-match",
        "shim-adapter-missing",
        "shim-config-missing",
        "shim-config-unreadable",
        "shim-exception",
        "shim-repo-root-missing",
        "shim-repo-root-not-dir",
        "timeout",
    )
)


def runtime_home():
    override = os.environ.get(TEST_HOME_ENV)
    return Path(override).expanduser() if override else Path.home()


def state_root(home=None):
    return Path(home if home is not None else runtime_home()) / ".epitype"


def telemetry_path(home=None):
    return state_root(home) / TELEMETRY_FILENAME


def heartbeat_path(host, event, home=None):
    return state_root(home) / HEARTBEAT_DIRECTORY / f"{host}.{event}"


def host_from_argv(arguments):
    return "codex" if "--codex" in arguments else "claude"


def utc_timestamp(now=None):
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _nonnegative_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validated_record(record):
    record = dict(record)
    record.setdefault("vault_skipped", 0)
    if len(record) != len(FIELDS) or set(record) != set(FIELDS):
        raise ValueError("telemetry fields are not the fixed schema")
    if record["host"] not in HOSTS:
        raise ValueError("unknown telemetry host")
    if record["outcome"] not in OUTCOMES:
        raise ValueError("unknown telemetry outcome")
    if record["reason"] not in REASONS:
        raise ValueError("unknown telemetry reason")
    if not isinstance(record["event"], str) or not record["event"].replace("-", "").isalnum():
        raise ValueError("invalid telemetry event")
    if not isinstance(record["ts"], str) or not record["ts"].endswith("Z"):
        raise ValueError("invalid telemetry timestamp")
    for name in ("hits", "injected_bytes", "terms", "vaults", "vault_skipped", "ms"):
        _nonnegative_integer(record[name], name)
    return record


def append(
    host,
    event,
    outcome,
    *,
    hits=0,
    injected_bytes=0,
    terms=0,
    vaults=0,
    vault_skipped=0,
    ms=0,
    reason,
    home=None,
    now=None,
):
    """Append one schema-checked JSON object with one O_APPEND os.write call.

    This is deliberately best-effort. Evidence failure must never change a hook's
    decision or stdout contract.
    """
    try:
        record = _validated_record(
            {
                "ts": utc_timestamp(now),
                "host": host,
                "event": event,
                "outcome": outcome,
                "hits": hits,
                "injected_bytes": injected_bytes,
                "terms": terms,
                "vaults": vaults,
                "vault_skipped": vault_skipped,
                "ms": ms,
                "reason": reason,
            }
        )
        payload = (
            json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        target = telemetry_path(home)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            os.fspath(target),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            written = os.write(descriptor, payload)
            return written == len(payload)
        finally:
            os.close(descriptor)
    except Exception:
        return False


def heartbeat(host, event, *, home=None, now=None):
    """Stat once and touch only when the prior signal is older than 60 seconds."""
    try:
        target = heartbeat_path(host, event, home)
        current = time.time() if now is None else float(now)
        try:
            modified = target.stat().st_mtime
        except FileNotFoundError:
            modified = None
        if modified is not None and current - modified <= HEARTBEAT_SECONDS:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)
        if now is not None:
            os.utime(target, (current, current))
        return True
    except Exception:
        return False


def rotate(*, home=None):
    """Keep complete tail rows after the log exceeds its bounded size."""
    target = telemetry_path(home)
    try:
        size = target.stat().st_size
        if size <= MAX_BYTES:
            return False
        with target.open("rb") as stream:
            start = max(0, size - KEEP_BYTES)
            stream.seek(start)
            tail = stream.read(KEEP_BYTES)
        if start:
            newline = tail.find(b"\n")
            tail = tail[newline + 1 :] if newline >= 0 else b""
        final_newline = tail.rfind(b"\n")
        tail = tail[: final_newline + 1] if final_newline >= 0 else b""
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=target.name + ".",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(tail)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return True
    except Exception:
        return False


def read_records(*, home=None):
    target = telemetry_path(home)
    records = []
    try:
        with target.open("r", encoding="ascii") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        continue
                    record = _validated_record(value)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                records.append(record)
    except OSError:
        pass
    return records
