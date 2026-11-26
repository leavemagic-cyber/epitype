import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Stable Epitype hook entrypoint; rendered once per adapter by graft."""

import json
import os


ADAPTER_FILENAME = "__EPITYPE_ADAPTER_FILENAME__"
SHIM_NAMES = ("sessionstart.py", "recall.py", "precompact.py", "pretooluse.py", "stop.py")
SHIM_REASON_CODES = (
    "config_missing",
    "config_unreadable",
    "repo_root_missing",
    "repo_root_not_dir",
    "adapter_missing",
    "exception",
)
STATUS_FILENAME = "shim_status.json"
# 寫檔後的快速出口用：memspec／capture_route 的副本（這支墊片不能 import epitype），
# 對不上時 shim_dispatch_regression 會紅。
FAST_EXIT_ADAPTER = "pretooluse_gate.py"
FAST_WRITE_TOOLS = frozenset((
    "write", "write_file", "create_file", "edit", "edit_file", "str_replace_editor",
    "multiedit", "multi_edit", "apply_edits"))
FAST_PATH_FIELDS = ("file_path", "path", "filePath")
FAST_CONFIG_ENV = "EPITYPE_CONFIG"
FAST_CONFIG_VAULTS = "vaults"
FAST_NATIVE_PROJECTS = (".claude", "projects")
FAST_NATIVE_MEMORY = "memory"
FAST_INDEX_NAME = "MEMORY.md"


def _post_write_off_vault(raw):
    """True：這是 PostToolUse 的寫檔事件，而且目標一定不是記憶庫卡片。

    寫檔後的事件每改一次檔就觸發一次，整套閘門光載入就要一秒；改的不是卡片就不該付。
    這裡只用標準庫比對路徑：不是 .md、是索引檔、不在任何設定庫或
    ~/.claude/projects/*/memory 底下、或落在 _／. 開頭的目錄，才算一定不是卡片。
    讀不懂、判不準一律回 False，交給閘門自己判斷。
    """
    try:
        event = json.loads(raw.lstrip("\ufeff"))
        if not isinstance(event, dict) or event.get("hook_event_name") != "PostToolUse":
            return False
        if str(event.get("tool_name") or "").casefold() not in FAST_WRITE_TOOLS:
            return False
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict):
            return False
        target = next((value.strip() for value in (tool_input.get(name) for name in FAST_PATH_FIELDS)
                       if isinstance(value, str) and value.strip()), None)
        if target is None:
            return True
        cwd = event.get("cwd")
        if not os.path.isabs(target) and isinstance(cwd, str) and cwd.strip():
            target = os.path.join(cwd, target)
        targets = {os.path.abspath(target), os.path.realpath(target)}
        if not any(path.lower().endswith(".md") and os.path.basename(path) != FAST_INDEX_NAME
                   for path in targets):
            return True
        home = os.path.expanduser("~")
        config_file = os.environ.get(FAST_CONFIG_ENV) or os.path.join(home, ".epitype", "config.json")
        with open(os.path.expanduser(config_file), encoding="utf-8-sig") as stream:
            config = json.load(stream)
        vaults = config.get(FAST_CONFIG_VAULTS) if isinstance(config, dict) else None
        if not isinstance(vaults, list):
            return False

        def forms(path):
            path = os.path.expanduser(path)
            return {os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.realpath(path))}

        def inside(path, root):
            if os.path.splitdrive(path)[0] != os.path.splitdrive(root)[0]:
                return None
            relative = os.path.relpath(path, root)
            parts = relative.split(os.sep)
            return None if relative == os.curdir or parts[0] == os.pardir else parts

        def card_parts(parts):
            return bool(parts) and not any(part.startswith(("_", ".")) for part in parts)

        vault_roots = set().union(*(forms(vault) for vault in vaults if isinstance(vault, str) and vault.strip()))
        projects = forms(os.path.join(home, *FAST_NATIVE_PROJECTS))
        for path in set().union(*(forms(path) for path in targets)):
            if any(card_parts(inside(path, root)) for root in vault_roots):
                return False
            for root in projects:
                parts = inside(path, root) or []
                if len(parts) > 2 and parts[1] == os.path.normcase(FAST_NATIVE_MEMORY) and card_parts(parts[2:]):
                    return False
        return True
    except Exception:
        return False


def _record_fail_open(reason):
    try:
        from datetime import datetime, timezone
        from pathlib import Path

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
        from pathlib import Path

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


def _run_adapter(adapter):
    import runpy
    import traceback

    # Match direct-script imports without starting a second Python process.
    previous_argv = sys.argv
    previous_path = sys.path[:]
    sys.argv = [os.fspath(adapter), *previous_argv[1:]]
    sys.path.insert(0, os.fspath(adapter.parent))
    try:
        runpy.run_path(os.fspath(adapter), run_name="__main__")
        return 0
    except SystemExit as error:
        if error.code is None:
            return 0
        if isinstance(error.code, int):
            return error.code
        print(error.code, file=sys.stderr)
        return 1
    except Exception:
        # An adapter crash must stay nonzero, never become a shim fail-open.
        traceback.print_exc()
        return 1
    finally:
        sys.argv = previous_argv
        sys.path[:] = previous_path


def main():
    try:
        # 這段只用 os.path：寫檔後的快速出口要在 0.2 秒內結束，pathlib 光載入就要三十毫秒。
        config_path = os.path.join(os.path.expanduser("~"), ".epitype", "config.json")
        if not os.path.isfile(config_path):
            _record_fail_open("config_missing")
            return 0
        try:
            with open(config_path, encoding="utf-8") as stream:
                config = json.loads(stream.read())
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
        repo_root = os.path.expanduser(repo_value)
        if not os.path.isdir(repo_root):
            _record_fail_open("repo_root_not_dir")
            return 0
        adapter = os.path.realpath(os.path.join(repo_root, "adapters", "claude", ADAPTER_FILENAME))
        if not os.path.isfile(adapter):
            _record_fail_open("adapter_missing")
            return 0
        if ADAPTER_FILENAME == FAST_EXIT_ADAPTER:
            import io

            raw = sys.stdin.read()
            if _post_write_off_vault(raw):
                return 0
            sys.stdin = io.StringIO(raw)
        from pathlib import Path

        adapter = Path(adapter)
        exit_code = _run_adapter(adapter)
        _trace_adapter(adapter, exit_code)
        return exit_code
    except Exception:
        _record_fail_open("exception")
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
