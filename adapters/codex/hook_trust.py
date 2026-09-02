import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # Keep output deterministic on cp950 consoles.
"""Report whether Codex will actually run the Epitype hooks registered in hooks.json.

Codex only executes a hook after the user has trusted it; trust lives in
config.toml as ``[hooks.state.'<hooks.json>:<event>:<group>:<index>']`` with a
``trusted_hash``. A registration that exists in hooks.json but has no trust
record is silently skipped by Codex ("N hooks need review before they can
run"), so a registration-only doctor reports green while recall never fires.
This check turns that invisible state into a hard failure.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib


MARKER_VALUE = "epitype"
MARKER_FIELDS = ("id", "comment")
SEEN_FILENAME = "codex_hook_trust_seen.json"
TRUSTED = "TRUSTED"
UNTRUSTED = "UNTRUSTED"
DISABLED = "DISABLED"
MODIFIED = "MODIFIED"
REVIEW_HINT = (
    "Codex skips untrusted hooks. In the Codex TUI run /hooks (Desktop app: "
    "the hooks review panel), approve the epitype entries, then rerun this check."
)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _snake(event):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def _hook_digest(hook):
    return hashlib.sha256(
        json.dumps(hook, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _is_marked(group):
    return any(group.get(field) == MARKER_VALUE for field in MARKER_FIELDS)


def _epitype_positions(hooks_path):
    value = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
    events = value.get("hooks") if isinstance(value, dict) else None
    if not isinstance(events, dict):
        raise ValueError("hooks.json has no hooks object")
    positions = []
    for event, groups in events.items():
        if not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict) or not _is_marked(group):
                continue
            hooks = group.get("hooks")
            if not isinstance(hooks, list):
                continue
            for hook_index, hook in enumerate(hooks):
                if isinstance(hook, dict):
                    key = f"{hooks_path}:{_snake(event)}:{group_index}:{hook_index}"
                    positions.append((event, key, _hook_digest(hook)))
    return positions


def _trust_states(config_path):
    value = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    hooks = value.get("hooks") if isinstance(value, dict) else None
    state = hooks.get("state") if isinstance(hooks, dict) else None
    return state if isinstance(state, dict) else {}


def _load_seen(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _classify(record, digest, seen_record):
    if not isinstance(record, dict) or not record.get("trusted_hash"):
        return UNTRUSTED
    if record.get("enabled") is False:
        return DISABLED
    if (
        isinstance(seen_record, dict)
        and seen_record.get("trusted_hash") == record.get("trusted_hash")
        and seen_record.get("hook_digest") not in (None, digest)
    ):
        return MODIFIED
    return TRUSTED


def run_check(home, output=sys.stdout, seen_path=None):
    hooks_path = (home / ".codex" / "hooks.json").resolve()
    config_path = home / ".codex" / "config.toml"
    if not hooks_path.is_file() or not config_path.is_file():
        print("CODEX TRUST: SKIP no codex hooks.json/config.toml", file=output)
        return 0
    positions = _epitype_positions(hooks_path)
    if not positions:
        print("CODEX TRUST: SKIP no epitype registrations", file=output)
        return 0
    states = _trust_states(config_path)
    seen_path = seen_path or home / ".epitype" / SEEN_FILENAME
    seen = _load_seen(seen_path)
    verdicts = []
    for event, key, digest in positions:
        record = states.get(key)
        verdict = _classify(record, digest, seen.get(key))
        verdicts.append(verdict)
        print(f"CODEX TRUST {event} {':'.join(key.rsplit(':', 3)[1:])}: {verdict}", file=output)
        if verdict == TRUSTED:
            seen[key] = {"trusted_hash": record.get("trusted_hash"), "hook_digest": digest}
    failing = sum(verdict != TRUSTED for verdict in verdicts)
    if failing:
        print(f"CODEX TRUST: FAIL {failing}/{len(verdicts)} not runnable. {REVIEW_HINT}", file=output)
        return 1
    try:
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps(seen, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
    print(f"CODEX TRUST: PASS {len(verdicts)}/{len(verdicts)}", file=output)
    return 0


def _fixture(root, states, hook_command="python x.py"):
    codex = root / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    hooks = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "other.exe"}]},
                {"hooks": [{"type": "command", "command": hook_command, "timeout": 3}], "id": MARKER_VALUE},
            ],
            "PreCompact": [
                {"hooks": [{"type": "command", "command": hook_command + " --codex"}], "comment": MARKER_VALUE}
            ],
        }
    }
    (codex / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    hooks_path = (codex / "hooks.json").resolve()
    lines = ["model = \"synthetic\"", "", "[hooks.state]", ""]
    for suffix, record in states.items():
        key = f"{hooks_path}:{suffix}"  # TOML literal string: backslashes stay single.
        lines.append(f"[hooks.state.'{key}']")
        for field, value in record.items():
            rendered = json.dumps(value) if isinstance(value, str) else str(value).lower()
            lines.append(f"{field} = {rendered}")
        lines.append("")
    (codex / "config.toml").write_text("\n".join(lines), encoding="utf-8")
    return hooks_path


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix=".hook-trust-", dir=_REPO_ROOT) as temp_dir:
            root = Path(temp_dir)
            both = {
                "user_prompt_submit:1:0": {"trusted_hash": "sha256:aa"},
                "pre_compact:0:0": {"trusted_hash": "sha256:bb"},
            }

            home = root / "untrusted"
            hooks_path = _fixture(home, {"user_prompt_submit:0:0": {"trusted_hash": "sha256:00"}})
            out = io.StringIO()
            code = run_check(home, out)
            text = out.getvalue()
            checks.append((
                "registered but untrusted hooks fail with review hint",
                code == 1 and text.count(UNTRUSTED) == 2 and "FAIL 2/2" in text and "/hooks" in text,
            ))
            checks.append((
                "key follows codex <path>:<snake_event>:<group>:<index> layout",
                _epitype_positions(hooks_path)[0][1] == f"{hooks_path}:user_prompt_submit:1:0"
                and _epitype_positions(hooks_path)[1][1] == f"{hooks_path}:pre_compact:0:0",
            ))

            home = root / "trusted"
            _fixture(home, both)
            out = io.StringIO()
            code = run_check(home, out)
            seen_file = home / ".epitype" / SEEN_FILENAME
            checks.append((
                "trusted hooks pass and record their digests",
                code == 0 and "PASS 2/2" in out.getvalue() and len(_load_seen(seen_file)) == 2,
            ))

            _fixture(home, both, hook_command="python relocated.py")
            out = io.StringIO()
            code = run_check(home, out)
            checks.append((
                "hook edited after trust reports MODIFIED",
                code == 1 and out.getvalue().count(MODIFIED) == 2,
            ))

            home = root / "disabled"
            disabled = {key: {**record, "enabled": False} for key, record in both.items()}
            _fixture(home, disabled)
            out = io.StringIO()
            code = run_check(home, out)
            checks.append(("disabled trust records fail", code == 1 and out.getvalue().count(DISABLED) == 2))

            home = root / "nocodex"
            home.mkdir()
            out = io.StringIO()
            checks.append(("missing codex host skips cleanly", run_check(home, out) == 0 and "SKIP" in out.getvalue()))

            cp950_environment = os.environ.copy()
            cp950_environment["PYTHONUTF8"] = "0"
            cp950_environment["PYTHONIOENCODING"] = "cp950"
            cp950_environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, os.fspath(Path(__file__)), "check", "--home", os.fspath(root / "untrusted")],
                capture_output=True,
                env=cp950_environment,
                timeout=10,
                check=False,
            )
            checks.append((
                "cp950 console remains UTF-8 safe",
                result.returncode == 1 and UNTRUSTED in result.stdout.decode("utf-8", errors="replace"),
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 7
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="report Codex trust state of epitype hooks")
    check.add_argument("--home", type=Path, default=Path.home(), help="home directory (default: ~)")
    return parser


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    parsed = _parser().parse_args(arguments)
    try:
        return run_check(parsed.home.expanduser().resolve())
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
