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

from epitype import findings, memspec, telemetry
from _hook_common import (
    bounded_context,
    emit,
    expired,
    load_config,
    payload,
    read_event,
    resolve_vaults,
    run_synthetic,
    write_config,
)


def _metrics():
    return {
        "outcome": "miss",
        "hits": 0,
        "injected_bytes": 0,
        "terms": 0,
        "vaults": 0,
        "vault_skipped": 0,
        "reason": "no-context",
    }


def _handle(event, started_at, host):
    metrics = _metrics()
    try:
        config = load_config(started_at)
    except Exception:
        metrics.update(outcome="error", reason="config")
        return None, metrics
    if config is None:
        metrics.update(outcome="timeout", reason="timeout")
        return None, metrics
    budget = config[memspec.CONFIG_BUDGET_BYTES_FIELD]
    pieces = []
    resolved_vaults = resolve_vaults(config, event)
    primary_vault = config[memspec.CONFIG_VAULTS_FIELD][0]
    detector_vaults = [primary_vault]
    detector_vaults.extend(vault for vault in resolved_vaults if vault != primary_vault)
    metrics["vaults"] = len(detector_vaults)

    try:
        records, _ = findings.run_and_record(
            telemetry.runtime_home(),
            detector_vaults,
            current_host=host,
        )
    except Exception:
        try:
            records = findings.load(primary_vault)
        except Exception:
            records = []
    finding_line = findings.injection_line(records)
    if finding_line:
        pieces.append(finding_line)
        metrics["hits"] += 1

    for vault in resolved_vaults:
        if expired(started_at):
            metrics.update(outcome="timeout", reason="timeout")
            return None, metrics
        index_path = vault / memspec.MEMORY_INDEX_FILENAME
        if index_path.is_file():
            body = index_path.read_text(encoding="utf-8")
            slim = memspec.slim_index(
                body,
                min(memspec.SESSIONSTART_INDEX_BUDGET_BYTES, budget),
                index_path.resolve(),
            )
            pieces.append(f"## {memspec.MEMORY_INDEX_FILENAME}\n{slim}")
            metrics["hits"] += 1

        ledger_path = vault / memspec.WORK_LEDGER_FILENAME
        if ledger_path.is_file():
            ledger = ledger_path.read_text(encoding="utf-8")
            pieces.append(f"## {memspec.WORK_LEDGER_FILENAME}")
            pieces.extend(ledger.splitlines())
            metrics["hits"] += 1

    if expired(started_at):
        metrics.update(outcome="timeout", reason="timeout")
        return None, metrics
    context = bounded_context(
        "SessionStart",
        pieces,
        budget,
        required_first=bool(finding_line),
    )
    if not context:
        return None, metrics
    metrics.update(
        outcome="hit",
        injected_bytes=len(context.encode("utf-8")),
        reason="context-injected",
    )
    return payload("SessionStart", context), metrics


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
            root = Path(temp_dir)
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

            bad_config = root / "bad-config.json"
            bad_config.write_text("{broken", encoding="utf-8")
            bad_home = root / "bad-home"
            bad_result = run_synthetic(
                Path(__file__),
                {"source": "startup"},
                bad_config,
                environment={telemetry.TEST_HOME_ENV: str(bad_home)},
            )
            bad_records = telemetry.read_records(home=bad_home)
            checks.append(
                (
                    "bad config fails open and records config",
                    bad_result.returncode == 0
                    and not bad_result.stdout
                    and not bad_result.stderr
                    and len(bad_records) == 1
                    and bad_records[0]["outcome"] == "error"
                    and bad_records[0]["reason"] == "config",
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 5
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main():
    arguments = sys.argv[1:]
    if "--selftest" in arguments:
        return _selftest()
    host = telemetry.host_from_argv(arguments)
    metrics = _metrics()
    try:
        event = read_event(sys.stdin)
        value, metrics = _handle(event, _STARTED_AT, host)
        if value is not None and not expired(_STARTED_AT):
            emit(value)
        elif value is not None:
            metrics.update(outcome="timeout", injected_bytes=0, reason="timeout")
    except Exception:
        metrics.update(outcome="error", injected_bytes=0, reason="exception")
    telemetry.append(
        host,
        "SessionStart",
        metrics["outcome"],
        hits=metrics["hits"],
        injected_bytes=metrics["injected_bytes"],
        terms=metrics["terms"],
        vaults=metrics["vaults"],
        vault_skipped=metrics["vault_skipped"],
        ms=max(0, int((time.monotonic() - _STARTED_AT) * 1000)),
        reason=metrics["reason"],
    )
    telemetry.rotate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
