import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude PreCompact adapter that persists a bounded transcript recovery map."""

import json
from pathlib import Path
import tempfile

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import compact_map, memspec, telemetry
from _hook_common import (
    emit,
    expired,
    load_config,
    payload,
    payload_fits,
    read_event,
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
        "reason": "invalid-event",
    }


def _handle(event, started_at, host):
    metrics = _metrics()
    transcript_value = event.get("transcript_path")
    if not isinstance(transcript_value, str) or not transcript_value.strip():
        return None, metrics
    try:
        config = load_config(started_at)
    except Exception:
        metrics.update(outcome="error", reason="config")
        return None, metrics
    if config is None or expired(started_at):
        metrics.update(outcome="timeout", reason="timeout")
        return None, metrics
    metrics["vaults"] = len(config[memspec.CONFIG_VAULTS_FIELD])

    transcript = Path(transcript_value).expanduser().resolve()
    if not transcript.is_file():
        metrics["reason"] = "no-context"
        return None, metrics
    vault = config[memspec.CONFIG_VAULTS_FIELD][0]
    destination = (vault / memspec.COMPACT_MAP_FILENAME).resolve()
    compact_map.build_map(
        transcript,
        destination,
        memspec.COMPACT_MAP_DEFAULT_BUDGET_BYTES,
    )
    metrics.update(outcome="hit", hits=1, reason="map-written")
    context = f"地圖已落於{destination},壓縮後先讀它按行號回撈原文。"
    budget = config[memspec.CONFIG_BUDGET_BYTES_FIELD]
    if expired(started_at) or not payload_fits("PreCompact", context, budget):
        if expired(started_at):
            metrics.update(outcome="timeout", hits=0, reason="timeout")
        return None, metrics
    metrics["injected_bytes"] = 0 if host == "codex" else len(context.encode("utf-8"))
    return payload("PreCompact", context), metrics


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-precompact-") as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            transcript = root / "transcript.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"content": "Synthetic recovery request"},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": "Synthetic recovery result"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = root / "config.json"
            write_config(config, [vault])
            result = run_synthetic(
                Path(__file__),
                {"transcript_path": str(transcript)},
                config,
            )
            destination = (vault / memspec.COMPACT_MAP_FILENAME).resolve()
            checks.append(
                (
                    "map persisted",
                    result.returncode == 0
                    and destination.is_file()
                    and "Synthetic recovery request"
                    in destination.read_text(encoding="utf-8"),
                )
            )
            value = json.loads(result.stdout) if result.stdout.strip() else {}
            context = value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "output contains map path",
                    str(destination) in context
                    and value.get("hookSpecificOutput", {}).get("hookEventName")
                    == "PreCompact",
                )
            )
            destination.unlink()
            codex_result = run_synthetic(
                Path(__file__),
                {"transcript_path": str(transcript)},
                config,
                ("--codex",),
            )
            checks.append(
                (
                    "Codex mode persists map without incompatible output",
                    codex_result.returncode == 0
                    and destination.is_file()
                    and not codex_result.stdout
                    and not codex_result.stderr,
                )
            )

            bad_config = root / "bad-config.json"
            bad_config.write_text("{broken", encoding="utf-8")
            bad_home = root / "bad-home"
            bad_result = run_synthetic(
                Path(__file__),
                {"transcript_path": str(transcript)},
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
    total = 4
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
        if value is not None and not expired(_STARTED_AT) and "--codex" not in arguments:
            emit(value)
        elif value is not None and expired(_STARTED_AT):
            metrics.update(outcome="timeout", hits=0, injected_bytes=0, reason="timeout")
    except Exception:
        metrics.update(outcome="error", hits=0, injected_bytes=0, reason="exception")
    telemetry.append(
        host,
        "PreCompact",
        metrics["outcome"],
        hits=metrics["hits"],
        injected_bytes=metrics["injected_bytes"],
        terms=metrics["terms"],
        vaults=metrics["vaults"],
        vault_skipped=metrics["vault_skipped"],
        ms=max(0, int((time.monotonic() - _STARTED_AT) * 1000)),
        reason=metrics["reason"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
