import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude PreCompact adapter that persists a bounded transcript recovery map."""

import json
import os
from pathlib import Path
import time

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# context_meter 與 tempfile 用到才載入：Codex 的壓縮（--codex）不學門檻，而 hook 每一次
# 都要重新編譯載入的模組（dont_write_bytecode）；Codex 在 10 秒硬逾時砍掉 PreCompact 時，
# 還沒跑到清標記那一步，下一個壓縮週期的交接提醒就不會重新武裝。
from epitype import compact_map, memspec
from _hook_common import (
    isolated_temp_root,
    clear_recall_markers,
    expired,
    failure_reason,
    governance_vault,
    load_config,
    read_event,
    recall_marker_directory,
    run_synthetic,
    trace_hook,
    write_codex_fixture,
    write_config,
)


def _map_destination(vault, event, transcript):
    """本地薄殼：算法住在 epitype.compact_map，SessionStart 壓縮續場讀同一份。"""
    return compact_map.map_destination(
        vault, event.get("session_id", event.get("sessionId", "")), transcript
    )


def _sweep_maps(directory, keep):
    """30 天過期，其餘各留最新 COMPACT_MAP_MAX_FILES 份。

    地圖與壓縮前交接檔（`*.handoff.md`）同目錄、同樣過期，但上限分開數：交接檔是模型
    寫的，一場可能留好幾份，跟地圖混著數會把還有用的地圖提早擠掉。代價是這個目錄最多
    可以有兩倍上限的檔。交接的已交付紀錄（`*.handoff.delivered`）只按 30 天過期，不算進
    任何一種上限。`keep` 是這一場的地圖、交接檔與已交付紀錄，永遠不刪。"""
    keep = set(keep) if isinstance(keep, (tuple, list, set, frozenset)) else {keep}
    try:
        now = time.time()
        groups = {False: [], True: []}
        for path in directory.glob("*.md"):
            try:
                if path in keep:
                    continue
                if now - path.stat().st_mtime > memspec.COMPACT_MAP_TTL_SECONDS:
                    path.unlink()
                else:
                    groups[path.name.endswith(memspec.COMPACT_HANDOFF_SUFFIX)].append(path)
            except OSError:
                continue
        for path in directory.glob("*" + memspec.COMPACT_HANDOFF_DELIVERED_SUFFIX):
            try:
                if path not in keep and now - path.stat().st_mtime > memspec.COMPACT_MAP_TTL_SECONDS:
                    path.unlink()
            except OSError:
                continue
        for retained in groups.values():
            retained.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            for path in retained[memspec.COMPACT_MAP_MAX_FILES - 1 :]:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def _is_subagent(event):
    """官方文件：agent_id「Present only when the hook fires inside a subagent call」。"""
    return bool(event.get("agent_id") or event.get("agentId"))


def _learn_threshold(event, vault, transcript):
    """自動壓縮的這一刻就是門檻：記下當下用量給用量計學。手動壓縮不記（時點是人選的）。

    兩種欄位名都讀（`trigger`／`triggered_by`）。子代理自己的壓縮不記：那是子代理的
    context，不是主線的門檻。用量計關掉時也不記。記不了就算了——這是學習，不是關卡，
    不能讓它擋掉地圖。"""
    trigger = event.get("trigger") or event.get("triggered_by")
    if trigger != "auto" or _is_subagent(event):
        return
    try:
        from epitype import context_meter

        if not context_meter.enabled(memspec.config_options()):
            return
        context_meter.record_autocompact(vault, transcript)
    except Exception:
        pass


def _handle(event, started_at, learn=True, trace=None):
    """寫地圖、清標記。`trace` 是給追蹤的一個 dict：提早結束的原因、標記清了沒、地圖寫了沒。"""
    trace = {} if trace is None else trace
    transcript_value = event.get("transcript_path")
    if not isinstance(transcript_value, str) or not transcript_value.strip():
        trace["outcome"] = "no-transcript"
        return None
    config = load_config(started_at)
    if config is None or expired(started_at):
        trace["outcome"] = "budget-expired"
        return None

    transcript = Path(transcript_value).expanduser().resolve()
    if not transcript.is_file():
        trace["outcome"] = "transcript-missing"
        return None
    session_id = event.get("session_id", event.get("sessionId", ""))
    # 子代理跟主線共用 session_id：它自己的壓縮不代表主線的 context 變小了，所以主線
    # 已經說過的用量計那一行要留著——清喚回標記時直接跳過它，不先清再補。
    # Compaction invalidates recall dedupe even if the recovery-map write fails.
    if _is_subagent(event):
        clear_recall_markers(session_id, keep=(memspec.CONTEXT_METER_MARKER,))
    else:
        clear_recall_markers(session_id)
    trace["cleared"] = True
    vault = governance_vault(config, for_write=True)
    # 只有 Claude 側要學門檻：Codex 的壓縮點從它自己的設定算（context_meter.codex_limit），
    # 學進來反而會把 Codex 的數字混進 Claude 的門檻。
    if learn:
        _learn_threshold(event, vault, transcript)
    destination = _map_destination(vault, event, transcript)
    # 地圖檔名（session＋transcript 路徑雜湊）：跟 SessionStart 找的那個對不上，追蹤裡一眼看得出來。
    trace["stem"] = destination.stem
    handoff = compact_map.handoff_destination(vault, session_id, transcript)
    compact_map.build_map(
        transcript,
        destination,
        memspec.COMPACT_MAP_DEFAULT_BUDGET_BYTES,
    )
    trace["map"] = True
    _sweep_maps(
        destination.parent,
        (destination, handoff, compact_map.handoff_delivered_path(handoff)),
    )
    trace["outcome"] = "ok"
    # 這裡曾經回一句「地圖已落於…」。它在兩邊宿主都到不了模型：Claude Code 的
    # PreCompact 不能注入（2026-08-19 實證），Codex 0.153 的 PreCompactOutcome 只有
    # Continue／Stopped。印一句沒有人收得到的話，只會讓下一個讀碼的人以為鏈是通的。
    # 壓縮後把地圖交回模型的是 SessionStart（source=compact），走同一個 map_destination。
    return None


def _selftest():
    import tempfile

    from epitype import context_meter

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

            # 用量計：自動壓縮記下當下用量（壞檔重建）、清掉提醒標記讓那一行重新武裝；
            # 手動壓縮與 Codex 不記。
            usage_row = {"type": "assistant", "message": {"usage": {
                "input_tokens": 100000, "cache_creation_input_tokens": 20000,
                "cache_read_input_tokens": 30000}}}
            auto_transcript = root / "transcript-auto.jsonl"
            auto_transcript.write_text(json.dumps(usage_row) + "\n", encoding="utf-8")
            state = context_meter.state_path(vault)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("{broken", encoding="utf-8")
            auto_markers = recall_marker_directory("session-auto")
            auto_markers.mkdir(parents=True, exist_ok=True)
            for name in (memspec.CONTEXT_METER_MARKER,):
                (auto_markers / name).write_text(name + "\n", encoding="ascii")
            pct_env = {memspec.CONTEXT_METER_PCT_ENV: "92"}
            auto_event = {"transcript_path": str(auto_transcript), "session_id": "session-auto"}
            auto_result = run_synthetic(
                Path(__file__), {**auto_event, "trigger": "auto"}, config, environment=pct_env)
            learned = context_meter.read_samples(state)
            checks.append((
                "an auto compaction re-arms the context reminders and rebuilds the broken state with one sample",
                auto_result.returncode == 0
                and not auto_result.stdout
                and not auto_markers.exists()
                and len(learned) == 1
                and learned[0]["tokens"] == 150000
                and learned[0]["pct"] == "92",
            ))
            triggered_by = run_synthetic(
                Path(__file__), {**auto_event, "triggered_by": "auto"}, config, environment=pct_env)
            checks.append((
                "triggered_by=auto is read too and adds one sample",
                triggered_by.returncode == 0 and len(context_meter.read_samples(state)) == 2,
            ))
            manual = run_synthetic(
                Path(__file__), {**auto_event, "trigger": "manual"}, config, environment=pct_env)
            codex_auto = run_synthetic(
                Path(__file__), {**auto_event, "trigger": "auto"}, config, ("--codex",),
                environment=pct_env)
            checks.append((
                "a manual compaction and a Codex run record nothing",
                manual.returncode == 0 and codex_auto.returncode == 0
                and len(context_meter.read_samples(state)) == 2,
            ))

            # 交接檔跟地圖同目錄、同樣 30 天過期，但上限分開數：一堆交接檔不得把地圖擠掉；
            # 這一場的交接檔就算過期也留著。
            maps_dir = destination.parent
            maps_before = sorted(
                path.name for path in maps_dir.glob("*.md")
                if not path.name.endswith(memspec.COMPACT_HANDOFF_SUFFIX))
            week_ago = time.time() - 7 * 24 * 3600
            for index in range(memspec.COMPACT_MAP_MAX_FILES + 6):
                extra = maps_dir / f"other-{index:03d}{memspec.COMPACT_HANDOFF_SUFFIX}"
                extra.write_text("handoff\n", encoding="utf-8")
                os.utime(extra, (week_ago + index, week_ago + index))
            expired_handoff = maps_dir / f"expired{memspec.COMPACT_HANDOFF_SUFFIX}"
            expired_handoff.write_text("old\n", encoding="utf-8")
            month_ago = time.time() - 31 * 24 * 3600
            os.utime(expired_handoff, (month_ago, month_ago))
            # 這一場第一次壓縮（還沒有地圖）：交接檔就算超過 30 天也留著。
            sweep_transcript = root / "transcript-sweep.jsonl"
            sweep_transcript.write_text(json.dumps(usage_row) + "\n", encoding="utf-8")
            own_handoff = compact_map.handoff_destination(
                vault, "session-sweep", sweep_transcript.resolve())
            own_handoff.write_text("mine\n", encoding="utf-8")
            os.utime(own_handoff, (month_ago, month_ago))
            run_synthetic(
                Path(__file__),
                {"transcript_path": str(sweep_transcript), "session_id": "session-sweep"},
                config,
            )
            maps_after = sorted(
                path.name for path in maps_dir.glob("*.md")
                if not path.name.endswith(memspec.COMPACT_HANDOFF_SUFFIX))
            handoffs_after = list(maps_dir.glob("*" + memspec.COMPACT_HANDOFF_SUFFIX))
            checks.append((
                "handoffs expire with the maps but are capped separately, and this session's is kept",
                set(maps_before) <= set(maps_after)
                and own_handoff.is_file()
                and not expired_handoff.exists()
                and len(handoffs_after) == memspec.COMPACT_MAP_MAX_FILES
                and not (maps_dir / f"other-000{memspec.COMPACT_HANDOFF_SUFFIX}").exists(),
            ))

            # 子代理的自動壓縮：不學、不清主線的用量計標記；喚回標記照舊清。
            (auto_markers).mkdir(parents=True, exist_ok=True)
            for name in (memspec.CONTEXT_METER_MARKER, "digest"):
                (auto_markers / name).write_text(name + "\n", encoding="ascii")
            before_subagent = len(context_meter.read_samples(state))
            subagent_run = run_synthetic(
                Path(__file__), {**auto_event, "trigger": "auto", "agent_id": "agent-1"}, config,
                environment=pct_env)
            checks.append((
                "a subagent's auto compaction learns nothing and keeps the main thread's meter markers",
                subagent_run.returncode == 0
                and len(context_meter.read_samples(state)) == before_subagent
                and (auto_markers / memspec.CONTEXT_METER_MARKER).is_file()
                and not (auto_markers / "digest").exists(),
            ))

            # Codex（--codex）的自動壓縮：清掉這一場的用量計標記與壓縮點快取（下一個週期重新
            # 武裝），而且一筆都不學——Codex 的數字不能變成 Claude 的門檻。
            codex_rollout, codex_env = write_codex_fixture(root, "codex-pc", 205000)
            codex_markers = recall_marker_directory("codex-pc")
            codex_markers.mkdir(parents=True, exist_ok=True)
            for name in (memspec.CONTEXT_METER_MARKER, memspec.CONTEXT_METER_CODEX_LIMIT_CACHE):
                (codex_markers / name).write_text("x\n", encoding="ascii")
            codex_run = run_synthetic(
                Path(__file__),
                {"hook_event_name": "PreCompact", "session_id": "codex-pc", "turn_id": "t1",
                 "transcript_path": os.fspath(codex_rollout), "cwd": os.fspath(root),
                 "model": "gpt-x", "trigger": "auto"},
                config, arguments=("--codex",), environment={**pct_env, **codex_env})
            checks.append((
                "a Codex auto compaction clears the meter marker and limit cache and learns nothing",
                codex_run.returncode == 0
                and not (codex_markers / memspec.CONTEXT_METER_MARKER).exists()
                and not (codex_markers / memspec.CONTEXT_METER_CODEX_LIMIT_CACHE).exists()
                and len(context_meter.read_samples(state)) == before_subagent
                and compact_map.map_destination(vault, "codex-pc", codex_rollout).is_file(),
            ))

            # enabled=false：自動壓縮也不學。
            disabled_config = root / "disabled-config.json"
            write_config(disabled_config, [vault])
            disabled_options = json.loads(disabled_config.read_text(encoding="utf-8"))
            disabled_options[memspec.CONTEXT_METER_CONFIG_FIELD] = {
                memspec.CONTEXT_METER_ENABLED_FIELD: False}
            disabled_config.write_text(json.dumps(disabled_options), encoding="utf-8")
            disabled_run = run_synthetic(
                Path(__file__), {**auto_event, "trigger": "auto"}, disabled_config,
                environment=pct_env)
            checks.append((
                "with the context meter disabled an auto compaction learns nothing",
                disabled_run.returncode == 0
                and len(context_meter.read_samples(state)) == before_subagent,
            ))

            # 同一場兩次壓縮：PreCompact 不刪交接檔（就算比上一份地圖舊）——交不交回歸
            # SessionStart 的已交付紀錄。已交付紀錄只按 30 天過期、不算進上限，這一場的留著。
            twice_transcript = root / "transcript-twice.jsonl"
            twice_transcript.write_text(json.dumps(usage_row) + "\n", encoding="utf-8")
            twice_event = {"transcript_path": str(twice_transcript), "session_id": "session-twice"}
            twice_map = _map_destination(vault, twice_event, twice_transcript.resolve())
            twice_handoff = compact_map.handoff_destination(
                vault, "session-twice", twice_transcript.resolve())
            twice_handoff.write_text("第一段的交接\n", encoding="utf-8")
            run_synthetic(Path(__file__), twice_event, config)
            stamp = twice_map.stat().st_mtime
            os.utime(twice_handoff, (stamp - 10, stamp - 10))
            own_record = compact_map.handoff_delivered_path(twice_handoff)
            own_record.write_text("1\n", encoding="ascii")
            os.utime(own_record, (month_ago, month_ago))
            stale_record = maps_dir / f"stale{memspec.COMPACT_HANDOFF_DELIVERED_SUFFIX}"
            stale_record.write_text("1\n", encoding="ascii")
            os.utime(stale_record, (month_ago, month_ago))
            fresh_records = []
            for index in range(memspec.COMPACT_MAP_MAX_FILES + 6):
                record = maps_dir / f"fresh-{index:03d}{memspec.COMPACT_HANDOFF_DELIVERED_SUFFIX}"
                record.write_text("1\n", encoding="ascii")
                fresh_records.append(record)
            run_synthetic(Path(__file__), twice_event, config)
            checks.append((
                "PreCompact never deletes a handoff; delivered records expire at 30 days, "
                "are not capped, and this session's is kept",
                twice_handoff.is_file()
                and own_record.is_file()
                and not stale_record.exists()
                and all(record.is_file() for record in fresh_records),
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

            # 追蹤：每次一行，寫在設定檔旁；提早結束要說出原因。
            run_synthetic(Path(__file__), {"session_id": "no-transcript"}, config)
            run_synthetic(Path(__file__), {"transcript_path": str(transcript)}, root / "absent-config.json")
            trace_file = root / memspec.CONTEXT_METER_TRACE_FILENAME
            rows = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
            rows = [row for row in rows if row.get("event") == "PreCompact"]
            outcomes = {row["outcome"] for row in rows}
            checks.append((
                "each PreCompact run traces host, markers cleared, map written and elapsed ms, "
                "or the reason it stopped early",
                any(row["host"] == "claude" and row["outcome"] == "ok" and row["map"] is True
                    and row["cleared"] is True and row["lines"] == [] and isinstance(row["ms"], int)
                    for row in rows)
                and any(row["host"] == "codex" and row["outcome"] == "ok" for row in rows)
                and any(row["session"] == "session-b" for row in rows)
                and {"exception:JSONDecodeError", "config-missing", "no-transcript"} <= outcomes
                and all(row["map"] is False for row in rows if row["outcome"] != "ok"),
            ))
            broken_root = root / "trace-broken"
            broken_root.mkdir()
            (broken_root / memspec.CONTEXT_METER_TRACE_FILENAME).mkdir()
            broken_config = broken_root / "config.json"
            write_config(broken_config, [vault])
            broken_transcript = root / "transcript-broken-trace.jsonl"
            broken_transcript.write_text(transcript.read_text(encoding="utf-8"), encoding="utf-8")
            broken_run = run_synthetic(Path(__file__), {"transcript_path": str(broken_transcript)}, broken_config)
            checks.append((
                "a trace that cannot be written changes nothing: the map is written and the hook is silent",
                broken_run.returncode == 0 and not broken_run.stdout and not broken_run.stderr
                and _map_destination(vault, {}, broken_transcript.resolve()).is_file(),
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 18
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
    event = None
    # 追蹤：壓縮後地圖沒交回來的原因還沒查明，每次都記一行（宿主、清標記、寫地圖、結束原因）。
    trace = {"outcome": "unknown", "cleared": False, "map": False, "lines": []}
    try:
        # 只寫檔，永遠不輸出：`--codex` 仍被接受（Codex 的 hooks.json 這樣掛），
        # 但兩邊宿主的輸出路徑都已經退役，所以兩條路徑跑的是同一段程式。
        event = read_event(sys.stdin)
        _handle(event, _STARTED_AT, learn="--codex" not in arguments, trace=trace)
    except Exception as exc:
        trace["outcome"] = failure_reason(exc)
    trigger = (event.get("trigger") or event.get("triggered_by")) if isinstance(event, dict) else None
    trace_hook("PreCompact", event, _STARTED_AT, codex="--codex" in arguments, trigger=trigger, **trace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
