import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Stable Epitype hook entrypoint; rendered once per adapter by graft."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess


ADAPTER_FILENAME = "__EPITYPE_ADAPTER_FILENAME__"
SHIM_NAMES = ("sessionstart.py", "recall.py", "precompact.py", "pretooluse.py")
SHIM_REASON_CODES = (
    "config_missing",
    "config_unreadable",
    "repo_root_missing",
    "repo_root_not_dir",
    "adapter_missing",
    "exception",
)
STATUS_FILENAME = "shim_status.json"
EVENT_NAMES = {
    "sessionstart.py": "SessionStart",
    "recall.py": "UserPromptSubmit",
    "precompact.py": "PreCompact",
    "pretooluse.py": "PreToolUse",
}


def _record_telemetry(reason):
    try:
        shim_name = Path(__file__).name
        host = "codex" if "--codex" in sys.argv[1:] else "claude"
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "host": host,
            "event": EVENT_NAMES[shim_name],
            "outcome": "fail-open",
            "hits": 0,
            "injected_bytes": 0,
            "terms": 0,
            "vaults": 0,
            "vault_skipped": 0,
            "ms": 0,
            "reason": "shim-" + reason.replace("_", "-"),
        }
        payload = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        target = Path.home() / ".epitype" / "telemetry.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            os.fspath(target),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except Exception:
        pass


def _record_fail_open(reason):
    _record_telemetry(reason)
    try:
        status_path = Path.home() / ".epitype" / STATUS_FILENAME
        records = {}
        if status_path.is_file():
            try:
                current = json.loads(status_path.read_text(encoding="utf-8"))
                current_records = current.get("shims", {}) if isinstance(current, dict) else {}
                if isinstance(current_records, dict):
                    for name, record in current_records.items():
                        if name not in SHIM_NAMES or not isinstance(record, dict):
                            continue
                        timestamp = record.get("timestamp")
                        reason_value = record.get("reason")
                        if (
                            record.get("shim") == name
                            and reason_value in SHIM_REASON_CODES
                            and isinstance(timestamp, str)
                            and timestamp.endswith("Z")
                        ):
                            try:
                                datetime.fromisoformat(timestamp[:-1] + "+00:00")
                            except ValueError:
                                continue
                            records[name] = {
                                "timestamp": timestamp,
                                "shim": name,
                                "reason": reason_value,
                            }
            except Exception:
                records = {}
        shim_name = Path(__file__).name
        records[shim_name] = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "shim": shim_name,
            "reason": reason,
        }
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps({"version": 1, "shims": records}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def _trace_adapter(adapter, exit_code):
    try:
        trace_value = os.environ.get("EPITYPE_SHIM_TRACE")
        if trace_value is None:
            return
        record = {
            "shim": Path(__file__).name,
            "adapter": os.fspath(adapter),
            "exit": exit_code,
        }
        with Path(trace_value).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def main():
    try:
        config_path = Path.home() / ".epitype" / "config.json"
        if not config_path.is_file():
            _record_fail_open("config_missing")
            return 0
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            _record_fail_open("config_unreadable")
            return 0
        if not isinstance(config, dict):
            _record_fail_open("config_unreadable")
            return 0
        repo_value = config.get("repo_root") if isinstance(config, dict) else None
        if not isinstance(repo_value, str) or not repo_value.strip():
            _record_fail_open("repo_root_missing")
            return 0
        repo_root = Path(repo_value).expanduser()
        if not repo_root.is_dir():
            _record_fail_open("repo_root_not_dir")
            return 0
        adapter = (repo_root / "adapters" / "claude" / ADAPTER_FILENAME).resolve()
        if not adapter.is_file():
            _record_fail_open("adapter_missing")
            return 0
        result = subprocess.run(
            [sys.executable, str(adapter), *sys.argv[1:]],
            check=False,
        )
        _trace_adapter(adapter, result.returncode)
        return result.returncode
    except Exception:
        _record_fail_open("exception")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
