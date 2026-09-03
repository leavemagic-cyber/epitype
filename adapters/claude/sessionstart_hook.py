import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude SessionStart adapter for slim index and work-ledger injection."""

import json
import os
from pathlib import Path
import re
import tempfile

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import memspec, pending_lint
from _hook_common import (
    bounded_context,
    emit,
    expired,
    load_config,
    native_cwd_vaults,
    payload,
    read_event,
    resolve_vaults,
    run_synthetic,
    write_config,
)


def _handle(event, started_at):
    config = load_config(started_at)
    if config is None:
        return None
    budget = config[memspec.CONFIG_BUDGET_BYTES_FIELD]
    pieces = []
    # Session start carries the cwd's own vault(s) plus the governance vault;
    # another project's index and ledger are noise here and were crowding the
    # budget. Recall still reaches that project's cards by content.
    # The governance vault is the one holding the working ledger, not whichever
    # path sorted first: the installer sorts vaults alphabetically, so position
    # carries no meaning, and a cwd vault that also appears in the configured
    # list must never be dropped (adversarial review 2026-09-03 #3, #7).
    resolved = resolve_vaults(config, event)
    configured = config[memspec.CONFIG_VAULTS_FIELD]
    native = native_cwd_vaults(event.get("cwd") if isinstance(event, dict) else None)
    governance = next(
        (vault for vault in configured if (vault / memspec.WORK_LEDGER_FILENAME).is_file()),
        None,
    )
    if governance is None:
        vaults = resolved  # no ledger anywhere: keep the pre-1.1.0 behaviour
    else:
        vaults = [vault for vault in resolved if vault in native or vault == governance]

    # One line, first, so the budget cannot drop it: pending items with an entry
    # and no exit are exactly what resurfaces as wrong memory later.
    if not expired(started_at):
        overdue = pending_lint.summary_line(vaults)
        if overdue:
            pieces.append(overdue)

    for vault in vaults:
        if expired(started_at):
            return None
        index_path = vault / memspec.MEMORY_INDEX_FILENAME
        if index_path.is_file():
            body = index_path.read_text(encoding="utf-8")
            slim = memspec.slim_index(
                body,
                min(memspec.SESSIONSTART_INDEX_BUDGET_BYTES, budget),
                index_path.resolve(),
            )
            pieces.append(f"## {memspec.MEMORY_INDEX_FILENAME}\n{slim}")

        ledger_path = vault / memspec.WORK_LEDGER_FILENAME
        if ledger_path.is_file():
            ledger = ledger_path.read_text(encoding="utf-8")
            pieces.append(f"## {memspec.WORK_LEDGER_FILENAME}")
            pieces.extend(ledger.splitlines())

    if expired(started_at):
        return None
    context = bounded_context("SessionStart", pieces, budget)
    return payload("SessionStart", context) if context else None


def _selftest():
    checks = []
    try:
        red_body = "# Heading\nordinary one\n🔴 urgent\n🔴🔴 critical\nordinary two\n"
        red_slim = memspec.slim_index(red_body, 256, "synthetic/MEMORY.md")
        checks.append(
            (
                "red priority retained",
                "🔴🔴 critical" in red_slim
                and "🔴 urgent" in red_slim
                and len(red_slim.encode("utf-8")) <= 256,
            )
        )

        plain_body = "# Plain\nalpha\nbeta\ngamma\n"
        plain_slim = memspec.slim_index(plain_body, 256, "synthetic/plain.md")
        checks.append(
            (
                "unmarked index retained",
                "alpha" in plain_slim and "beta" in plain_slim and "gamma" in plain_slim,
            )
        )

        with tempfile.TemporaryDirectory(prefix="epitype-sessionstart-") as temp_dir:
            root = Path(temp_dir).resolve()
            vault = root / "vault"
            vault.mkdir()
            (vault / memspec.MEMORY_INDEX_FILENAME).write_text(
                "# Synthetic Index\nindex detail\n",
                encoding="utf-8",
            )
            (vault / memspec.WORK_LEDGER_FILENAME).write_text(
                "ledger detail\n",
                encoding="utf-8",
            )
            config = root / "config.json"
            write_config(config, [vault])
            result = run_synthetic(Path(__file__), {"source": "startup"}, config)
            value = json.loads(result.stdout) if result.stdout.strip() else {}
            context = value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "index and ledger injection",
                    result.returncode == 0
                    and "index detail" in context
                    and "ledger detail" in context
                    and str((vault / memspec.MEMORY_INDEX_FILENAME).resolve()) in context,
                )
            )

            home = root / "home"
            project = root / "work" / "proj"
            project.mkdir(parents=True)
            slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
            native = home / ".claude" / "projects" / slug / "memory"
            native.mkdir(parents=True)
            (native / memspec.MEMORY_INDEX_FILENAME).write_text(
                "# Native Index\nnative index detail\n",
                encoding="utf-8",
            )
            native_result = run_synthetic(
                Path(__file__),
                {"source": "startup", "cwd": str(project)},
                config,
                environment={"HOME": os.fspath(home), "USERPROFILE": os.fspath(home)},
            )
            native_value = json.loads(native_result.stdout) if native_result.stdout.strip() else {}
            native_context = native_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "cwd-slug native index injected ahead of configured vaults",
                    native_result.returncode == 0
                    and native_context.index("native index detail") < native_context.index("index detail")
                    and "ledger detail" in native_context,
                )
            )

            (vault / "plan.md").write_text(
                "---\nname: plan\ndescription: synthetic plan\n---\n- 2026-07-22 未辦（owner 自行）：SWSetup\n",
                encoding="utf-8",
            )
            overdue_result = run_synthetic(Path(__file__), {"source": "startup"}, config)
            overdue_value = json.loads(overdue_result.stdout) if overdue_result.stdout.strip() else {}
            overdue_context = overdue_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "overdue pending line is announced first, in one line",
                    overdue_result.returncode == 0
                    and overdue_context.startswith("⏳ 殭屍待辦 1 行／1 卡")
                    and "index detail" in overdue_context,
                )
            )

            second = root / "second-vault"
            second.mkdir()
            (second / memspec.MEMORY_INDEX_FILENAME).write_text("# Second\nsecond index detail\n", encoding="utf-8")
            (second / memspec.WORK_LEDGER_FILENAME).write_text("second ledger detail\n", encoding="utf-8")
            two_config = root / "two-config.json"
            write_config(two_config, [vault, second])
            two_result = run_synthetic(Path(__file__), {"source": "startup"}, two_config)
            two_value = json.loads(two_result.stdout) if two_result.stdout.strip() else {}
            two_context = two_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "only the governance vault's index and ledger are injected, not another project's",
                    two_result.returncode == 0
                    and "index detail" in two_context
                    and "ledger detail" in two_context
                    and "second index detail" not in two_context
                    and "second ledger detail" not in two_context,
                )
            )

            # Adversarial review 2026-09-03 #3/#7: the cwd vault may also be a
            # configured one, and the governance vault is the ledger holder, not
            # whichever path the installer happened to sort first.
            both_home = root / "both-home"
            both_project = root / "both-work" / "proj"
            both_project.mkdir(parents=True)
            both_slug = re.sub(r"[^A-Za-z0-9]", "-", str(both_project))
            both_native = both_home / ".claude" / "projects" / both_slug / "memory"
            both_native.mkdir(parents=True)
            (both_native / memspec.MEMORY_INDEX_FILENAME).write_text("# Both\nboth native detail\n", encoding="utf-8")
            gov = root / "gov-vault"
            gov.mkdir()
            (gov / memspec.MEMORY_INDEX_FILENAME).write_text("# Gov\ngov index detail\n", encoding="utf-8")
            (gov / memspec.WORK_LEDGER_FILENAME).write_text("gov ledger detail\n", encoding="utf-8")
            other = root / "aaa-other-project"
            other.mkdir()
            (other / memspec.MEMORY_INDEX_FILENAME).write_text("# Other\nother index detail\n", encoding="utf-8")
            both_config = root / "both-config.json"
            write_config(both_config, [other, gov, both_native])
            both_result = run_synthetic(
                Path(__file__),
                {"source": "startup", "cwd": str(both_project)},
                both_config,
                environment={"HOME": os.fspath(both_home), "USERPROFILE": os.fspath(both_home)},
            )
            both_value = json.loads(both_result.stdout) if both_result.stdout.strip() else {}
            both_context = both_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "cwd vault survives being configured too; governance is the ledger holder",
                    both_result.returncode == 0
                    and "both native detail" in both_context
                    and "gov index detail" in both_context
                    and "gov ledger detail" in both_context
                    and "other index detail" not in both_context,
                )
            )

            bad_config = root / "bad-config.json"
            bad_config.write_text("{broken", encoding="utf-8")
            bad_result = run_synthetic(
                Path(__file__),
                {"source": "startup"},
                bad_config,
            )
            checks.append(
                (
                    "bad config fails open silently",
                    bad_result.returncode == 0
                    and not bad_result.stdout
                    and not bad_result.stderr,
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 8
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main():
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    try:
        event = read_event(sys.stdin)
        value = _handle(event, _STARTED_AT)
        if value is not None and not expired(_STARTED_AT):
            emit(value)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
