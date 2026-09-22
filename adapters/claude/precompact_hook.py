import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude PreCompact adapter that persists a bounded transcript recovery map."""

import json
import os
from pathlib import Path
import tempfile
import time

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import compact_map, memspec
from _hook_common import (
    isolated_temp_root,
    clear_recall_markers,
    expired,
    governance_vault,
    load_config,
    read_event,
    recall_marker_directory,
    run_synthetic,
    write_config,
)


def _map_destination(vault, event, transcript):
    """本地薄殼：算法住在 epitype.compact_map，SessionStart 壓縮續場讀同一份。"""
    return compact_map.map_destination(
        vault, event.get("session_id", event.get("sessionId", "")), transcript
    )


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
    # Compaction invalidates recall dedupe even if the recovery-map write fails.
    clear_recall_markers(event.get("session_id", event.get("sessionId", "")))
    vault = governance_vault(config, for_write=True)
    destination = _map_destination(vault, event, transcript)
    compact_map.build_map(
        transcript,
        destination,
        memspec.COMPACT_MAP_DEFAULT_BUDGET_BYTES,
    )
    _sweep_maps(destination.parent, destination)
    # 這裡曾經回一句「地圖已落於…」。它在兩邊宿主都到不了模型：Claude Code 的
    # PreCompact 不能注入（2026-08-19 實證），Codex 0.153 的 PreCompactOutcome 只有
    # Continue／Stopped。印一句沒有人收得到的話，只會讓下一個讀碼的人以為鏈是通的。
    # 壓縮後把地圖交回模型的是 SessionStart（source=compact），走同一個 map_destination。
    return None


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
            # U-R1：PreCompact 不再印任何 context——那句話到不了模型（兩邊宿主皆然）。
            # 地圖照寫，交回模型的工作歸 SessionStart 的壓縮續場。
            checks.append(
                (
                    "PreCompact writes the map and says nothing: the context never reached the model",
                    not result.stdout
                    and not result.stderr
                    and destination.is_file()
                    and os.fspath(destination)
                    == os.fspath(
                        compact_map.map_destination(vault, "", transcript.resolve())
                    ),
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

            # Owner 2026-09-09 (§30): the commitment ledger is gone, so the map is
            # the transcript's recovery map and nothing else — a leftover ledger in
            # the vault must not be appended to it.
            legacy_ledger = vault / memspec.FTS_INDEX_DIRECTORY / "commitments.jsonl"
            legacy_ledger.parent.mkdir(parents=True, exist_ok=True)
            legacy_ledger.write_text(
                json.dumps({"digest": "d1", "ts": "2026-09-08T00:00:00Z",
                            "text": "我等一下會補上 settle 的測試。", "status": "open",
                            "session_id": "s1"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            promise_result = run_synthetic(
                Path(__file__),
                {"transcript_path": str(transcript)},
                routed_config,
            )
            promise_map = destination.read_text(encoding="utf-8")
            checks.append((
                "a leftover commitment ledger is not appended to the map",
                promise_result.returncode == 0
                and "我等一下會補上 settle 的測試。" not in promise_map
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
        with isolated_temp_root():
            return _selftest()
    try:
        # 只寫檔，永遠不輸出：`--codex` 仍被接受（Codex 的 hooks.json 這樣掛），
        # 但兩邊宿主的輸出路徑都已經退役，所以兩條路徑跑的是同一段程式。
        _handle(read_event(sys.stdin), _STARTED_AT)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
