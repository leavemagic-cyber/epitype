import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude PreCompact adapter that persists a bounded transcript recovery map."""

import json
from pathlib import Path
import tempfile

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import compact_map, memspec
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


def _handle(event, started_at):
    transcript_value = event.get("transcript_path")
    if not isinstance(transcript_value, str) or not transcript_value.strip():
        return None
    config = load_config(started_at)
    if config is None or expired(started_at):
        return None

    transcript = Path(transcript_value).expanduser().resolve()
    if not transcript.is_file():
        return None
    vault = config[memspec.CONFIG_VAULTS_FIELD][0]
    destination = (vault / memspec.COMPACT_MAP_FILENAME).resolve()
    compact_map.build_map(
        transcript,
        destination,
        memspec.COMPACT_MAP_DEFAULT_BUDGET_BYTES,
    )
    context = f"地圖已落於{destination},壓縮後先讀它按行號回撈原文。"
    budget = config[memspec.CONFIG_BUDGET_BYTES_FIELD]
    if expired(started_at) or not payload_fits("PreCompact", context, budget):
        return None
    return payload("PreCompact", context)


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-precompact-") as temp_dir:
            root = Path(temp_dir).resolve()
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
            bad_result = run_synthetic(
                Path(__file__),
                {"transcript_path": str(transcript)},
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
    try:
        event = read_event(sys.stdin)
        value = _handle(event, _STARTED_AT)
        if value is not None and not expired(_STARTED_AT) and "--codex" not in arguments:
            emit(value)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
