import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude PreCompact adapter that persists a bounded transcript recovery map."""

import json
import hashlib
import os
from pathlib import Path
import tempfile
import time

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import compact_map, memspec
from _hook_common import (
    clear_recall_markers,
    emit,
    expired,
    governance_vault,
    load_config,
    payload,
    payload_fits,
    read_event,
    recall_marker_directory,
    run_synthetic,
    session_component,
    write_config,
)


def _map_destination(vault, event, transcript):
    component = session_component(event.get("session_id", event.get("sessionId", "")), limit=80)
    digest = hashlib.sha256(os.fspath(transcript).encode("utf-8")).hexdigest()[:12]
    return (vault / memspec.COMPACT_MAP_DIRECTORY / f"{component}-{digest}.md").resolve()


def _sweep_maps(directory, keep):
    try:
        now = time.time()
        retained = []
        for path in directory.glob("*.md"):
            try:
                if path != keep and now - path.stat().st_mtime > memspec.COMPACT_MAP_TTL_SECONDS:
                    path.unlink()
                elif path != keep:
                    retained.append(path)
            except OSError:
                continue
        retained.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for path in retained[memspec.COMPACT_MAP_MAX_FILES - 1 :]:
            path.unlink(missing_ok=True)
    except OSError:
        pass


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
    vault = governance_vault(config)
    destination = _map_destination(vault, event, transcript)
    compact_map.build_map(
        transcript,
        destination,
        memspec.COMPACT_MAP_DEFAULT_BUDGET_BYTES,
    )
    # U53：壓縮丟掉的正是「我等一下會…」那句話。地圖是壓縮前唯一落地的快照，所以
    # 還沒兌現的承諾跟著它落地——讀地圖的人不必再去翻帳本。地圖已經寫好的事實不受
    # 這一段影響：帳本讀不到就什麼都不加。
    try:
        from epitype import commitments

        block = commitments.snapshot_block(vault, memspec.COMMITMENT_PRECOMPACT_MAX)
        if block:
            with destination.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n" + block)
    except Exception:
        pass
    _sweep_maps(destination.parent, destination)
    # What recall injected before compaction is gone after it; forget the
    # same-session dedupe with it so corrections and rulings can return.
    clear_recall_markers(event.get("session_id", event.get("sessionId", "")))
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
            destination = _map_destination(vault, {}, transcript.resolve())
            checks.append(
                (
                    "map persisted",
                    result.returncode == 0
                    and destination.is_file()
                    and "Synthetic recovery request"
                    in destination.read_text(encoding="utf-8"),
                )
            )

            second_transcript = root / "transcript-b.jsonl"
            second_transcript.write_text(
                json.dumps(
                    {"type": "user", "message": {"content": "Independent session B"}}
                )
                + "\n",
                encoding="utf-8",
            )
            second_event = {
                "transcript_path": str(second_transcript),
                "session_id": "session-b",
            }
            marker_directory = recall_marker_directory("session-b")
            marker_directory.mkdir(parents=True, exist_ok=True)
            (marker_directory / "digest").write_text("digest\n", encoding="ascii")
            second_result = run_synthetic(Path(__file__), second_event, config)
            checks.append((
                "compaction forgets the session's recall dedupe so injected cards can return",
                not marker_directory.exists(),
            ))
            second_destination = _map_destination(
                vault, second_event, second_transcript.resolve()
            )
            checks.append((
                "independent sessions retain independent recovery maps",
                second_result.returncode == 0
                and destination.is_file()
                and second_destination.is_file()
                and "Synthetic recovery request" in destination.read_text(encoding="utf-8")
                and "Independent session B" in second_destination.read_text(encoding="utf-8"),
            ))
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

            project_vault = root / "aaa-project"
            project_vault.mkdir()
            (vault / memspec.WORK_LEDGER_FILENAME).write_text(
                "governance ledger\n", encoding="utf-8"
            )
            destination.unlink()
            routed_config = root / "routed-config.json"
            write_config(routed_config, [project_vault, vault])
            routed = run_synthetic(
                Path(__file__),
                {"transcript_path": str(transcript)},
                routed_config,
            )
            checks.append((
                "compact map follows the governance ledger instead of vault order",
                routed.returncode == 0
                and destination.is_file()
                and not (project_vault / memspec.COMPACT_MAP_DIRECTORY).exists(),
            ))

            # U53: the map is the only pre-compaction snapshot that lands on disk,
            # so the promises that compaction would erase land with it.
            from epitype import commitments

            commitments.record(vault, "precompact-promise", ["我等一下會補上 settle 的測試。"])
            promise_result = run_synthetic(
                Path(__file__),
                {"transcript_path": str(transcript)},
                routed_config,
            )
            promise_map = destination.read_text(encoding="utf-8")
            checks.append((
                "open promises are appended to the pre-compaction map",
                promise_result.returncode == 0
                and memspec.COMMITMENT_PRECOMPACT_HEADING in promise_map
                and "我等一下會補上 settle 的測試。" in promise_map
                and "Synthetic recovery request" in promise_map,
            ))

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
    total = 8
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
