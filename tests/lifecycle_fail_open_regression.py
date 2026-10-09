"""Narrow fault injection for A37/A39; replaces all stateful work before calling main."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from types import SimpleNamespace

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "adapters" / "claude"))
import precompact_hook
import sessionstart_hook
import pretooluse_gate
from epitype import memspec


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# memory_lint.py and session_episodic_log.py are the host's own hooks, not files this repo
# ships. They are checked where they are installed: EPITYPE_HOST_HOOKS_DIR, else the
# current home's .claude/hooks. Under run_all's fixture home and in CI they are absent, and
# the summary line names each one that was not checked instead of passing silently.
HOST_HOOKS = Path(os.environ.get("EPITYPE_HOST_HOOKS_DIR") or Path.home() / ".claude" / "hooks")


def fault(*args, **kwargs):
    raise TimeoutError("fixture deadline exhausted")


def main():
    count = 0
    unchecked = []
    original_stdin, original_argv = sys.stdin, sys.argv
    output, errors = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(output), redirect_stderr(errors), tempfile.TemporaryDirectory(prefix="epitype-life-") as directory:
            root = Path(directory)
            for module in (precompact_hook, sessionstart_hook):
                module._handle = fault
                module.trace_hook = lambda *args, **kwargs: None
                module.trace_run = lambda: "fixture"
                module.read_event = lambda *args: {"session_id": "fixture-a39"}
                sys.stdin = io.StringIO("{}")
                sys.argv = [module.__file__]
                assert module.main() == 0
                count += 1
            if (HOST_HOOKS / "memory_lint.py").is_file():
                memory = load(HOST_HOOKS / "memory_lint.py", "fixture_memory_lint")
                memory._start_candidates = lambda *args: [root]
                memory._resolve_memory_dir = lambda *args: root
                memory.run_lint = fault
                sys.stdin = SimpleNamespace(isatty=lambda: False, buffer=io.BytesIO(b'{}'))
                assert memory.main([]) == 0
                count += 1
            else:
                unchecked.append("memory_lint.py")
            if (HOST_HOOKS / "session_episodic_log.py").is_file():
                episodic = load(HOST_HOOKS / "session_episodic_log.py", "fixture_episodic")
                episodic.run_log = fault
                sys.stdin = SimpleNamespace(isatty=lambda: False, buffer=io.BytesIO(b'{"hook_event_name":"SessionEnd"}'))
                assert episodic.main([]) == 0
                count += 1
            else:
                unchecked.append("session_episodic_log.py")
            timestamp = datetime.now(timezone.utc).isoformat()
            (root / memspec.GATE_LOG_FILENAME).write_text("\n".join(json.dumps({
                "kind": memspec.ACTION_GUARD_LOG_KIND, "timestamp": timestamp,
                "card": "advisory-fixture"}) for _ in range(memspec.GUARD_REPEAT_NOTICE_THRESHOLD)), encoding="utf-8")
            pretooluse_gate._guards = lambda *args, **kwargs: [SimpleNamespace(card="advisory-fixture", advice="fixture advice", on_hit="note")]
            lines = sessionstart_hook._repeat_guard_notices([root], None)
            assert len(lines) == 1 and "目前僅提醒" in lines[0] and "非授權或新增禁令" in lines[0]
            count += 1
            assert '"decision": "block"' not in output.getvalue() and '"permissionDecision": "deny"' not in output.getvalue()
            count += 1
    finally:
        sys.stdin, sys.argv = original_stdin, original_argv
    print(f"PASS {count}: lifecycle timeout faults return zero; historical notices stay advisory; live state was not touched")
    if unchecked:
        print(f"NOT CHECKED (host hooks not installed at {HOST_HOOKS}): " + ", ".join(unchecked))


if __name__ == "__main__":
    main()
