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

try:
    from ._native_hooks import hook_states as _native_hook_states
except ImportError:
    from _native_hooks import hook_states as _native_hook_states


MARKER_VALUE = "epitype"
MARKER_FIELDS = ("id", "comment")
SEEN_FILENAME = "codex_hook_trust_seen.json"
TRUSTED = "TRUSTED"
UNTRUSTED = "UNTRUSTED"
DISABLED = "DISABLED"
MODIFIED = "MODIFIED"
UNVERIFIED = "UNVERIFIED"
REVIEW_HINT = (
    "Codex skips untrusted hooks. In the Codex TUI run /hooks (Desktop app: "
    "the hooks review panel), approve the epitype entries, then rerun this check."
)
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 事件清單的正本在安裝器那一份：註冊是它做的，這裡只是查註冊有沒有被信任。兩邊各寫
# 一份的下場 2026-09-22 已經發生過——安裝器加了 SubagentStop，這裡沒加，於是每一台正
# 常安裝的機器都被這支報成 unexpected。安裝器不能 import memspec（它要在 epitype 套件
# 還不能 import 的機器上跑），所以共用常數搬不進 memspec；由查核端去讀正本才是單一來源。
from install.graft import EVENTS as REQUIRED_EVENTS


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
    # Nothing checked is not a pass: exit 1 with a verdict that says so, never
    # a "skip" that reads as green.
    if not hooks_path.is_file() or not config_path.is_file():
        print("CODEX TRUST: UNVERIFIED no codex hooks.json/config.toml", file=output)
        return 1
    positions = _epitype_positions(hooks_path)
    if not positions:
        print("CODEX TRUST: UNVERIFIED no epitype registrations", file=output)
        return 1
    counts = {event: 0 for event in REQUIRED_EVENTS}
    unexpected = []
    for event, _key, _digest in positions:
        if event in counts:
            counts[event] += 1
        else:
            unexpected.append(event)
    missing = [event for event, count in counts.items() if count == 0]
    duplicate = [event for event, count in counts.items() if count > 1]
    if missing or duplicate or unexpected:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if duplicate:
            details.append("duplicate=" + ",".join(duplicate))
        if unexpected:
            details.append("unexpected=" + ",".join(sorted(set(unexpected))))
        print("CODEX TRUST: FAIL registration set " + " ".join(details), file=output)
        return 1
    states = _trust_states(config_path)
    seen_path = seen_path or home / ".epitype" / SEEN_FILENAME
    seen = _load_seen(seen_path)
    native = {}
    if any(_classify(states.get(key), digest, seen.get(key)) == TRUSTED
           for _event, key, digest in positions):
        try:
            native = _native_hook_states(home)
        except Exception:
            print("CODEX TRUST: UNVERIFIED native hook inventory unavailable", file=output)
    verdicts = []
    for event, key, digest in positions:
        record = states.get(key)
        verdict = _classify(record, digest, seen.get(key))
        if verdict == TRUSTED:
            current = native.get(key, {})
            verdict = current.get("trustStatus", "").upper()
            if verdict not in (TRUSTED, UNTRUSTED, DISABLED, MODIFIED):
                verdict = UNVERIFIED
            if verdict == TRUSTED:
                if current.get("enabled") is not True:
                    verdict = DISABLED
                elif current.get("currentHash") != record.get("trusted_hash"):
                    verdict = MODIFIED
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


FIXTURE_SECOND_GROUP_EVENT = "UserPromptSubmit"


def _fixture_key(hooks_path, event):
    group = 1 if event == FIXTURE_SECOND_GROUP_EVENT else 0
    return "%s:%s:%d:0" % (hooks_path, _snake(event), group)


def _fixture(root, states, hook_command="python x.py"):
    codex = root / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    # 每個必備事件都註冊一次，清單跟著正本走：寫死五個事件的夾具，正是 SubagentStop
    # 被漏掉那次沒有被任何測試抓到的原因。
    registrations = {}
    for event in REQUIRED_EVENTS:
        entry = {"type": "command", "command": hook_command + " --" + _snake(event)}
        # 兩種標記欄位都要驗得到，PreCompact 用 comment。
        group = {"hooks": [entry], ("comment" if event == "PreCompact" else "id"): MARKER_VALUE}
        if event == FIXTURE_SECOND_GROUP_EVENT:
            # 前面先擺一個不是 epitype 的群組：位置索引得算對，不能假設永遠是 0。
            entry["timeout"] = 10
            registrations[event] = [{"hooks": [{"type": "command", "command": "other.exe"}]}, group]
        else:
            registrations[event] = [group]
    hooks = {"hooks": registrations}
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
    from unittest.mock import patch

    def fixture_native(home):
        states = _trust_states(home / ".codex" / "config.toml")
        return {key: {"trustStatus": "trusted", "enabled": True,
                      "currentHash": states.get(key, {}).get("trusted_hash")}
                for _event, key, _digest in _epitype_positions(home / ".codex" / "hooks.json")}

    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix=".hook-trust-", dir=_REPO_ROOT) as temp_dir, \
             patch(__name__ + "._native_hook_states", side_effect=fixture_native):
            root = Path(temp_dir).resolve()
            every_event = {
                _fixture_key("", event).lstrip(":"): {"trusted_hash": "sha256:t%d" % number}
                for number, event in enumerate(REQUIRED_EVENTS)
            }
            expected = len(REQUIRED_EVENTS)

            home = root / "untrusted"
            hooks_path = _fixture(home, {"user_prompt_submit:0:0": {"trusted_hash": "sha256:00"}})
            out = io.StringIO()
            code = run_check(home, out)
            text = out.getvalue()
            checks.append((
                "registered but untrusted hooks fail with review hint",
                code == 1
                and text.count(UNTRUSTED) == expected
                and f"FAIL {expected}/{expected}" in text
                and "/hooks" in text,
            ))
            checks.append((
                "key follows codex <path>:<snake_event>:<group>:<index> layout",
                {item[1] for item in _epitype_positions(hooks_path)}
                == {_fixture_key(hooks_path, event) for event in REQUIRED_EVENTS},
            ))
            from install import graft as _graft

            checks.append((
                "required events ARE the installer's own list, SubagentStop included",
                REQUIRED_EVENTS is _graft.EVENTS and "SubagentStop" in REQUIRED_EVENTS,
            ))
            checks.append((
                "a fully registered host reports no unexpected event",
                "unexpected=" not in text,
            ))

            home = root / "trusted"
            _fixture(home, every_event)
            out = io.StringIO()
            code = run_check(home, out)
            seen_file = home / ".epitype" / SEEN_FILENAME
            checks.append((
                "trusted hooks pass and record their digests",
                code == 0
                and f"PASS {expected}/{expected}" in out.getvalue()
                and len(_load_seen(seen_file)) == expected,
            ))

            for status, native_hash, expected_verdict in (
                ("modified", "sha256:new", MODIFIED),
                ("trusted", "sha256:unmatched", MODIFIED),
                ("", "", UNVERIFIED),
            ):
                report = {key: {"trustStatus": status, "enabled": True, "currentHash": native_hash}
                          for _event, key, _digest in _epitype_positions(home / ".codex" / "hooks.json")}
                out = io.StringIO()
                with patch(__name__ + "._native_hook_states", return_value=report):
                    code = run_check(home, out)
                checks.append(("native inventory overrides cached trust: " + expected_verdict,
                               code == 1 and out.getvalue().count(expected_verdict) == expected))
            out = io.StringIO()
            with patch(__name__ + "._native_hook_states", side_effect=OSError("fixture unavailable")):
                code = run_check(home, out)
            checks.append(("unavailable native inventory cannot pass",
                           code == 1 and "PASS" not in out.getvalue() and UNVERIFIED in out.getvalue()))

            _fixture(home, every_event, hook_command="python relocated.py")
            out = io.StringIO()
            code = run_check(home, out)
            checks.append((
                "hook edited after trust reports MODIFIED",
                code == 1 and out.getvalue().count(MODIFIED) == expected,
            ))

            home = root / "disabled"
            disabled = {key: {**record, "enabled": False} for key, record in every_event.items()}
            _fixture(home, disabled)
            out = io.StringIO()
            code = run_check(home, out)
            checks.append((
                "disabled trust records fail",
                code == 1 and out.getvalue().count(DISABLED) == expected,
            ))

            home = root / "incomplete"
            _fixture(home, every_event)
            incomplete_hooks = home / ".codex" / "hooks.json"
            incomplete_value = json.loads(incomplete_hooks.read_text(encoding="utf-8"))
            del incomplete_value["hooks"]["PreToolUse"]
            del incomplete_value["hooks"]["Stop"]
            incomplete_hooks.write_text(json.dumps(incomplete_value), encoding="utf-8")
            out = io.StringIO()
            checks.append((
                "missing required registrations fail instead of a partial PASS",
                run_check(home, out) == 1
                and "missing=PreToolUse,Stop" in out.getvalue(),
            ))

            home = root / "nocodex"
            home.mkdir()
            out = io.StringIO()
            checks.append(("missing codex host is not a runnable success", run_check(home, out) == 1 and "UNVERIFIED" in out.getvalue()))

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
    total = 14
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
