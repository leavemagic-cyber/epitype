import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""Claude Code 的 context 用量計：壓縮前提醒寫交接，壓縮後由 SessionStart 交回。

模型看不到自己的 context 用量，也無法自己觸發壓縮；PreCompact 的文字到不了模型
（2026-08-19 實證）。所以只能在壓縮「之前」、由每次都會跑的 hook（PreToolUse、
UserPromptSubmit）在跨過學到的壓縮點 97% 的那一次附一行字，提醒模型自己把交接落檔。這支模組只算數字與決定要不要說，
輸出與標記的時機歸 adapter：先輸出、後寫標記，沒送出去的提醒下次再試。

門檻不猜（見 memspec 的 CONTEXT_METER 段落）：設定覆寫 → 學到的自動壓縮用量 → 都沒有
就完全不提醒。hook 內每次呼叫只讀 transcript 檔尾，找不到就算了，永遠不讀整檔。
"""

import json
import os
from pathlib import Path
import time

try:
    from . import memspec
except ImportError:  # 直接當腳本跑（--selftest、run_all）。
    import memspec


# ---------------------------------------------------------------- 當前用量


def _usage_total(row):
    """主鏈 assistant 列的 context 用量（三欄加總）；不是這種列、或加總為 0 就回 None。

    加總為 0 的列是宿主自己合成的訊息（例如 API 錯誤），拿它當「現在的用量」會把門檻
    判斷拉回原點。"""
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return None
    if any(row.get(flag) for flag in ("isSidechain", "isMeta", "isCompactSummary")):
        return None
    message = row.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    total = 0
    for key in memspec.CONTEXT_METER_USAGE_FIELDS:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            total += value
    return total or None


def _is_compact_boundary(row):
    return (
        isinstance(row, dict)
        and row.get("type") == "system"
        and row.get("subtype") == "compact_boundary"
    )


def _scan_lines(lines):
    """由後往前看完整的行。回 (found, value)：found=True 時 value 是用量或 None。

    壓縮邊界之後還沒有新的 assistant 用量時，檔裡最後一筆用量是壓縮「前」的數字——
    拿它算，剛壓縮完就會立刻再叫一次。所以碰到邊界就停，回「目前不知道」。"""
    for raw in reversed(lines):
        if b'"usage"' not in raw and b'"compact_boundary"' not in raw:
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue  # 正在寫的最後一行、或壞行：當作不存在。
        if _is_compact_boundary(row):
            return True, None
        total = _usage_total(row)
        if total is not None:
            return True, total
    return False, None


def _oversized_assistant(tail):
    """一行超過單行上限、開頭還沒讀到時，用已讀進來的行尾判斷它是不是 assistant 列。

    真實 transcript 的 assistant 列把頂層 "type" 排在 message 之後、貼近行尾；user 列
    （工具結果）排在 message 之前。認得出是 assistant 列就代表最新用量讀不到——這時回
    None（不提醒），而不是往前拿一筆更早、更小的用量頂替，那會漏掉該發的提醒。"""
    window = tail[-memspec.CONTEXT_METER_OVERSIZED_TAIL_BYTES:]
    return any(marker in window for marker in memspec.CONTEXT_METER_ASSISTANT_TYPE_MARKERS)


def current_tokens(transcript_path, time_limit=None, clock=None):
    """這場目前的 context 用量（最後一筆主鏈 assistant 的三欄加總）；找不到回 None。

    從檔尾反向分塊讀：首塊 64 KiB、逐次加倍到 1 MiB 為止，總共最多 64 MiB，而且有
    時間上限（預設 150 ms，到了就回 None：hook 每次工具呼叫都要付這一份；時鐘可注入，
    測試不必賭 Windows 計時器的解析度）。最後一行
    可能是好幾 MB 的工具結果：一行超過單行上限還沒看到開頭，先用行尾認它是不是
    assistant 列——是就回 None；不是就把累積的片段丟掉、只繼續往前找換行，記憶體不跟著
    它長。任何讀檔錯誤都回 None。"""
    line_max = memspec.CONTEXT_METER_LINE_MAX_BYTES
    limit = memspec.CONTEXT_METER_TAIL_SECONDS if time_limit is None else time_limit
    clock = time.monotonic if clock is None else clock
    deadline = clock() + limit
    try:
        stream = open(transcript_path, "rb")
    except (OSError, TypeError, ValueError):
        return None
    with stream:
        try:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            block = memspec.CONTEXT_METER_TAIL_FIRST_BYTES
            budget = memspec.CONTEXT_METER_TAIL_MAX_BYTES
            pending = b""  # 一行的後半段：它的開頭還在更前面、尚未讀到。
            oversized = False  # pending 那一行已超過單行上限、片段已丟：它的開頭也不要。
            while end > 0 and budget > 0:
                if clock() >= deadline:
                    return None
                size = min(block, end, budget)
                start = end - size
                stream.seek(start)
                chunk = stream.read(size)
                budget -= size
                end = start
                block = min(block * 2, memspec.CONTEXT_METER_TAIL_MAX_BLOCK_BYTES)
                # 讀到檔頭時，第 0 個位元組就是一行的開頭：整塊都是完整的行。
                cut = chunk.find(b"\n") if start > 0 else -1
                if start > 0 and cut < 0:
                    if not oversized:
                        pending = chunk + pending
                        if len(pending) > line_max:
                            if _oversized_assistant(pending):
                                return None
                            pending, oversized = b"", True
                    continue
                body = chunk[cut + 1:]
                if oversized:
                    last = body.rfind(b"\n")
                    complete = body[:last + 1] if last >= 0 else b""
                else:
                    complete = body + pending
                found, value = _scan_lines(complete.split(b"\n"))
                if found:
                    return value
                pending = chunk[:cut] if start > 0 else b""
                oversized = len(pending) > line_max
                if oversized:
                    if _oversized_assistant(pending):
                        return None
                    pending = b""
        except OSError:
            return None
    return None


# ---------------------------------------------------------------- 門檻


def _section(options):
    """設定裡的 context_meter 段；缺了回 {}，不是物件回 None（壞設定＝不動作）。"""
    if not isinstance(options, dict):
        return {}
    section = options.get(memspec.CONTEXT_METER_CONFIG_FIELD, {})
    return section if isinstance(section, dict) else None


def enabled(options):
    """預設開。只有明寫 true 或沒寫才算開：寫了看不懂的值，安靜比亂叫安全。"""
    section = _section(options)
    if section is None:
        return False
    value = section.get(memspec.CONTEXT_METER_ENABLED_FIELD, True)
    return value is True


def state_path(vault):
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY / memspec.CONTEXT_METER_STATE_FILENAME


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def read_samples(path):
    """學習狀態檔裡的紀錄（舊到新）；檔不在、壞掉、不是預期形狀都回 []。"""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    samples = value.get("samples") if isinstance(value, dict) else None
    if not isinstance(samples, list):
        return []
    kept = []
    for item in samples:
        if not isinstance(item, dict) or not _positive_int(item.get("tokens")):
            continue
        pct = item.get("pct", "")
        kept.append({
            "tokens": item["tokens"],
            "pct": pct if isinstance(pct, str) else "",
            "at": item.get("at") if isinstance(item.get("at"), str) else "",
        })
    return kept


def write_samples(path, samples):
    """原子寫入（同目錄暫存檔再換名），只留最近 CONTEXT_METER_STATE_KEEP 筆。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = list(samples)[-memspec.CONTEXT_METER_STATE_KEEP:]
    staging = path.with_name("." + path.name + ".tmp-%d" % os.getpid())
    try:
        staging.write_text(json.dumps({"samples": kept}, ensure_ascii=False), encoding="utf-8")
        os.replace(staging, path)
    finally:
        try:
            staging.unlink()
        except OSError:
            pass
    return kept


def _pct(text):
    """CLAUDE_AUTOCOMPACT_PCT_OVERRIDE 的數值；不是 1–100 的數字回 None。"""
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 100 else None


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def learned_threshold(samples, pct_now):
    """(門檻, 來源) 或 (None, None)。取最近 5 筆，逐筆換算到目前的 pct 再取中位數。

    紀錄當時的 pct 跟現在一樣：原值。兩邊都是 1–100 的數字但不同：按比例換算。
    有一邊不是數字（例如一邊沒設）：那一筆換算不了，丟掉——不拿不同設定下的數字硬套。"""
    pct_now = "" if pct_now is None else str(pct_now).strip()
    values = []
    scaled = False
    for sample in list(samples)[-memspec.CONTEXT_METER_STATE_MEDIAN_OF:]:
        recorded = sample.get("pct", "").strip()
        tokens = sample["tokens"]
        if recorded == pct_now:
            values.append(tokens)
            continue
        old, new = _pct(recorded), _pct(pct_now)
        if old is None or new is None:
            continue
        if old != new:
            scaled = True
        values.append(tokens * new / old)
    if not values:
        return None, None
    source = memspec.CONTEXT_METER_SOURCE_SCALED if scaled else memspec.CONTEXT_METER_SOURCE_LEARNED
    return int(_median(values)), source


def current_pct(environ=None, home=None):
    """宿主現在用的自動壓縮百分比字串（可能是空字串）。

    先看行程環境變數；沒有這個變數時退到宿主設定 `~/.claude/settings.json` 的 `env`
    （讀不到當空）。hook 行程不一定繼承得到設定裡的 env：只看環境變數的話，學到的
    pct=92 會跟 hook 看到的空字串對不上，整個用量計就安靜地不提醒。"""
    environ = os.environ if environ is None else environ
    value = environ.get(memspec.CONTEXT_METER_PCT_ENV)
    if value is not None:
        return str(value).strip()
    return (_settings_pct(home) or "").strip()


def threshold(options, state_file, environ=None, home=None):
    """(自動壓縮門檻 tokens, 來源)；不知道就 (None, None)——不知道就不提醒。"""
    section = _section(options)
    if section is None:
        return None, None
    override = section.get(memspec.CONTEXT_METER_OVERRIDE_FIELD)
    if _positive_int(override):
        return override, memspec.CONTEXT_METER_SOURCE_OVERRIDE
    if state_file is None:
        return None, None
    samples = read_samples(state_file)
    if not samples:
        return None, None
    return learned_threshold(samples, current_pct(environ, home))


def stage(current, limit):
    """跨過門檻的 97% 回那一段的標記名，否則 None。只有一段。"""
    if not _positive_int(limit) or not isinstance(current, (int, float)):
        return None
    if current >= memspec.CONTEXT_METER_STAGE_RATIO * limit:
        return memspec.CONTEXT_METER_MARKER
    return None


def _k(tokens):
    return int(round(max(0, tokens) / 1000))


def render(current, limit, source, path):
    mark = memspec.CONTEXT_METER_SCALED_MARK if source == memspec.CONTEXT_METER_SOURCE_SCALED else ""
    return memspec.CONTEXT_METER_NOTICE.format(
        cur=_k(current), left=_k(limit - current), mark=mark, path=os.fspath(path))


# ---------------------------------------------------------------- hook 端


def notice(event, vault, marker_directory, options=None, environ=None, home=None):
    """這次呼叫要附的那一行與要寫的標記名：(line, marker)；不該說就回 None。永不丟例外。

    標記由呼叫端在「真的輸出之後」用 `claim` 寫：沒送出去（預算擠掉、逾時）的提醒
    下一次還要再試。子代理的呼叫一律不說、也不寫標記——提醒被子代理吃掉，主線就永遠
    收不到了。"""
    try:
        if not isinstance(event, dict) or event.get("agent_id") or event.get("agentId"):
            return None
        session_id = event.get("session_id", event.get("sessionId", ""))
        transcript = event.get("transcript_path")
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        if not isinstance(transcript, str) or not transcript.strip() or marker_directory is None:
            return None
        options = memspec.config_options() if options is None else options
        if not enabled(options):
            return None
        limit, source = threshold(options, state_path(vault), environ, home)
        if limit is None:
            return None
        current = current_tokens(transcript)
        marker = stage(current, limit)
        if marker is None:
            return None
        if (Path(marker_directory) / marker).exists():
            return None  # 這個壓縮週期已經說過。
        # 用到才載入：compact_map 會帶進 hashlib 與 argparse，絕大多數呼叫走不到這裡。
        try:
            from . import compact_map
        except ImportError:
            import compact_map
        path = compact_map.handoff_destination(vault, session_id, transcript)
        return render(current, limit, source, path), marker
    except Exception:
        return None


def claim(marker_directory, marker):
    """寫下「這一段這個壓縮週期已經說過」。已經有了回 False；寫不了也回 False（下次再試）。"""
    try:
        directory = Path(marker_directory)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / marker).open("x", encoding="ascii") as stream:
            stream.write(marker + "\n")
    except (OSError, TypeError, ValueError):
        return False
    return True


def record_autocompact(vault, transcript_path, environ=None, now=None, home=None):
    """自動壓縮前的那一刻記下當下用量，給之後當門檻學。回寫入後的紀錄，或 None。

    只由 PreCompact 在 trigger=auto 時呼叫：手動壓縮的時點是人選的，不代表門檻。
    讀–追加–換名在同一把鎖裡：兩個場次同時壓縮時，沒有鎖的話後寫的會蓋掉先寫的那筆。
    拿不到鎖就放棄這一筆（回 None）——少學一筆無妨，卡住壓縮不行。"""
    tokens = current_tokens(transcript_path)
    if tokens is None:
        return None
    from datetime import datetime, timezone

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sample = {"tokens": tokens, "pct": current_pct(environ, home), "at": stamp}
    path = state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    with memspec.file_lock(path, memspec.CONTEXT_METER_LOCK_SECONDS) as locked:
        if not locked:
            return None
        samples = read_samples(path)  # 檔壞了＝從空的重建。
        samples.append(sample)
        return write_samples(path, samples)


# ---------------------------------------------------------------- CLI


def _parse_time(text):
    from datetime import datetime, timezone

    if not isinstance(text, str) or not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def scan_autocompactions(root, days=memspec.CONTEXT_METER_CALIBRATE_DAYS, now=None):
    """`<root>/*/*.jsonl` 近 N 天自動壓縮的 (時間, preTokens)，舊到新。

    逐行串流，只 parse 含 `"compactMetadata"` 的行：這些檔動輒幾百 MB。"""
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    found = []
    for path in sorted(Path(root).glob("*/*.jsonl")):
        try:
            if path.stat().st_mtime < since.timestamp():
                continue
            with path.open("rb") as stream:
                for raw in stream:
                    if b'"compactMetadata"' not in raw:
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        continue
                    meta = row.get("compactMetadata") if isinstance(row, dict) else None
                    if not isinstance(meta, dict) or meta.get("trigger") != "auto":
                        continue
                    tokens = meta.get("preTokens")
                    when = _parse_time(row.get("timestamp"))
                    if _positive_int(tokens) and when is not None and when >= since:
                        found.append((when, tokens))
        except OSError:
            continue
    found.sort(key=lambda item: item[0])
    return found


def _settings_path(home=None):
    return Path(home or Path.home()) / ".claude" / "settings.json"


def _settings_pct(home=None):
    """宿主設定裡的 CLAUDE_AUTOCOMPACT_PCT_OVERRIDE（終端機跑 CLI 時環境變數通常沒有）。"""
    try:
        value = json.loads(_settings_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    env = value.get("env") if isinstance(value, dict) else None
    pct = env.get(memspec.CONTEXT_METER_PCT_ENV) if isinstance(env, dict) else None
    return str(pct) if pct is not None else None


def _governance(vault_argument, options):
    if vault_argument:
        return Path(vault_argument).expanduser().resolve()
    vaults = options.get(memspec.CONFIG_VAULTS_FIELD) if isinstance(options, dict) else None
    if not isinstance(vaults, list) or not vaults:
        return None
    try:
        from .dream import governance_vault
    except ImportError:
        from dream import governance_vault

    return governance_vault([Path(item).expanduser().resolve() for item in vaults if isinstance(item, str)])


def calibrate(root, now=None, out=print):
    """唯讀報告：近 30 天自動壓縮最近 5 筆的中位數、樣本數、時間範圍。什麼都不寫。

    歷史樣本當時用的 pct 證明不了——行程環境變數優先於設定檔，設定檔的修改時間也證明不了
    哪個行程用了什麼——證明不了就不學。壓縮點只由 PreCompact 在實際自動壓縮的當下記。"""
    found = scan_autocompactions(root, now=now)
    recent = found[-memspec.CONTEXT_METER_STATE_MEDIAN_OF:]
    if not recent:
        out(f"calibrate: no auto-compaction in the last {memspec.CONTEXT_METER_CALIBRATE_DAYS} days "
            f"under {root}")
        out(memspec.CONTEXT_METER_CALIBRATE_REPORT_ONLY)
        return None
    candidate = int(_median([tokens for _when, tokens in recent]))
    first = recent[0][0].strftime("%Y-%m-%dT%H:%M:%SZ")
    last = recent[-1][0].strftime("%Y-%m-%dT%H:%M:%SZ")
    out(f"calibrate: candidate={candidate} tokens samples={len(recent)} "
        f"(of {len(found)} found) range={first}..{last}")
    out(memspec.CONTEXT_METER_CALIBRATE_REPORT_ONLY)
    return candidate


def status(options, vault, transcript=None, environ=None, out=print):
    environ = os.environ if environ is None else environ
    if not enabled(options):
        out("status: disabled by config")
        return None
    limit, source = threshold(options, state_path(vault) if vault else None, environ)
    pct = current_pct(environ)
    if limit is None:
        out(f"status: threshold unknown (no override, no learned sample usable at pct={pct or '(unset)'}); "
            "no reminders until this machine's first auto-compaction (or set "
            f"{memspec.CONTEXT_METER_CONFIG_FIELD}.{memspec.CONTEXT_METER_OVERRIDE_FIELD}).")
    else:
        out(f"status: threshold={limit} tokens source={source} pct={pct or '(unset)'} "
            f"remind_at={int(memspec.CONTEXT_METER_STAGE_RATIO * limit)}")
    if transcript:
        current = current_tokens(transcript)
        if current is None:
            out(f"status: current usage unknown for {transcript}")
        else:
            where = stage(current, limit) if limit else None
            share = f" ({current / limit:.0%} of threshold)" if limit else ""
            out(f"status: current={current} tokens{share} due={'yes' if where else 'no'}")
    return limit


# ---------------------------------------------------------------- 自測


def _selftest():
    import contextlib
    import io
    import subprocess
    import tempfile
    import time
    from datetime import datetime, timedelta, timezone

    checks = []

    def assistant(total, **flags):
        row = {"type": "assistant", "message": {"usage": {
            "input_tokens": total - 300, "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 200, "output_tokens": 999}}}
        row.update(flags)
        return json.dumps(row)

    def tool_result(size):
        return json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "x" * size}]}})

    def write(path, rows, trailing_newline=True):
        path.write_text("\n".join(rows) + ("\n" if trailing_newline else ""), encoding="utf-8")

    try:
        with tempfile.TemporaryDirectory(prefix="epitype-context-meter-") as temp_dir:
            root = Path(temp_dir).resolve()
            transcript = root / "t.jsonl"

            # 1. 跨塊：最後一列是遠大於首塊的工具結果，用量在它前面。
            write(transcript, [assistant(1000), assistant(123456), tool_result(300 * 1024)])
            checks.append(("usage found across blocks behind a large tool result",
                           current_tokens(transcript) == 123456))
            # 用量那一列自己橫跨塊邊界（整列比首塊大）。
            big_assistant = json.loads(assistant(77777))
            big_assistant["message"]["content"] = "y" * (200 * 1024)
            write(transcript, [assistant(1000), json.dumps(big_assistant), tool_result(10)],
                  trailing_newline=False)
            straddle = current_tokens(transcript)

            # 2. sidechain／meta／compact summary 不算；加總為 0 的合成列不算。
            write(transcript, [assistant(50000), assistant(90000, isSidechain=True),
                               assistant(91000, isMeta=True), assistant(92000, isCompactSummary=True),
                               json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 0}}})])
            checks.append(("sidechain, meta, compact-summary and zero rows are skipped; a row "
                           "straddling the block edge is still read",
                           current_tokens(transcript) == 50000 and straddle == 77777))

            # 3. 檔不存在、沒有用量、壓縮邊界之後還沒有新用量：都回 None。
            write(transcript, [assistant(150000), json.dumps(
                {"type": "system", "subtype": "compact_boundary",
                 "compactMetadata": {"trigger": "auto", "preTokens": 150000}}),
                json.dumps({"type": "user", "isCompactSummary": True, "message": {"content": "s"}})])
            after_boundary = current_tokens(transcript)
            write(transcript, [tool_result(10)])
            checks.append(("missing file, no usage, and no usage since the compact boundary give None",
                           current_tokens(root / "absent.jsonl") is None
                           and current_tokens(transcript) is None
                           and after_boundary is None
                           and current_tokens(None) is None))

            # 3b. 最後一行是 9 MiB 的工具結果（超過單行上限、也超過舊的 8 MiB 總量）：丟掉
            # 那一行的片段、繼續往前找，前一行的用量照樣讀得到（預設 150 ms 上限內）；
            # 時間上限到了回 None。
            huge = root / "huge.jsonl"
            write(huge, [assistant(1000), assistant(345678), tool_result(9 * 1024 * 1024)])
            checks.append(("a 9 MiB tool-result last line is skipped whole and the usage before it is read "
                           "within the default limit; an exhausted time limit gives None",
                           current_tokens(huge) == 345678
                           and current_tokens(huge, time_limit=0) is None))

            # 3c. 最後一行是超過單行上限的 assistant 列（欄位順序照真實 transcript：頂層
            # type 排在 message 之後）：最新用量讀不到就回 None，不拿前一筆 50,000 頂替。
            oversized_row = {
                "parentUuid": "p", "isSidechain": False,
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": "z" * (3 * 1024 * 1024)}],
                            "usage": {"input_tokens": 97700, "cache_creation_input_tokens": 100,
                                      "cache_read_input_tokens": 200}},
                "requestId": "r", "type": "assistant", "uuid": "u", "timestamp": "2026-09-25T00:00:00Z",
            }
            oversized = root / "oversized-assistant.jsonl"
            oversized_results = []
            for separators in ((",", ":"), (", ", ": ")):  # 宿主的緊湊寫法，以及帶空白的寫法
                encoded = json.dumps(oversized_row, separators=separators)
                write(oversized, [assistant(50000), encoded])
                oversized_results.append(
                    len(encoded) > memspec.CONTEXT_METER_LINE_MAX_BYTES and current_tokens(oversized) is None)
            checks.append(("an oversized assistant last line gives None instead of the older usage",
                           oversized_results == [True, True]))

            # 4. 三層門檻：覆寫 > 學到 > 未知；pct 換算。
            vault = root / "vault"
            vault.mkdir()
            state = state_path(vault)
            env92 = {memspec.CONTEXT_METER_PCT_ENV: "92"}
            write_samples(state, [{"tokens": value, "pct": "92", "at": ""}
                                  for value in (1, 2, 100000, 110000, 120000, 130000, 140000)])
            learned = threshold({}, state, env92)
            override = threshold({"context_meter": {"autocompact_tokens": 50000}}, state, env92)
            bad_override = threshold({"context_meter": {"autocompact_tokens": True}}, state, env92)
            scaled = threshold({}, state, {memspec.CONTEXT_METER_PCT_ENV: "46"})
            same_value = threshold({}, state, {memspec.CONTEXT_METER_PCT_ENV: "92.0"})
            no_settings_home = root / "home-without-settings"
            unset = threshold({}, state, {}, home=no_settings_home)
            checks.append(("threshold order override > learned (median of last 5) > unknown, with pct scaling",
                           override == (50000, memspec.CONTEXT_METER_SOURCE_OVERRIDE)
                           and learned == (120000, memspec.CONTEXT_METER_SOURCE_LEARNED)
                           and bad_override == learned
                           and scaled == (60000, memspec.CONTEXT_METER_SOURCE_SCALED)
                           and same_value == (120000, memspec.CONTEXT_METER_SOURCE_LEARNED)
                           and unset == (None, None)
                           and threshold({}, root / "none.json", env92) == (None, None)))

            # 4b. hook 行程沒有這個環境變數：退到宿主設定 ~/.claude/settings.json 的 env；
            # 設定壞掉當空；變數有設（即使是空字串）就以變數為準。
            settings_home = root / "home-with-settings"
            (settings_home / ".claude").mkdir(parents=True)
            (settings_home / ".claude" / "settings.json").write_text(
                json.dumps({"env": {memspec.CONTEXT_METER_PCT_ENV: "92"}}), encoding="utf-8")
            broken_home = root / "home-broken-settings"
            (broken_home / ".claude").mkdir(parents=True)
            (broken_home / ".claude" / "settings.json").write_text("{broken", encoding="utf-8")
            checks.append(("without the environment variable the host settings' env supplies the pct",
                           threshold({}, state, {}, home=settings_home)
                           == (120000, memspec.CONTEXT_METER_SOURCE_LEARNED)
                           and threshold({}, state, {}, home=broken_home) == (None, None)
                           and threshold({}, state, {memspec.CONTEXT_METER_PCT_ENV: ""},
                                         home=settings_home) == (None, None)
                           and current_pct({}, settings_home) == "92"))

            # 5. 未知時不提醒（用量再高也一樣）；enabled=false 不做。
            markers = root / "markers"
            write(transcript, [assistant(190000)])
            event = {"session_id": "s1", "transcript_path": os.fspath(transcript)}
            empty_vault = root / "empty-vault"
            empty_vault.mkdir()
            unknown = notice(event, empty_vault, markers, options={}, environ=env92)
            disabled = notice(event, vault, markers, environ=env92,
                              options={"context_meter": {"enabled": False, "autocompact_tokens": 100000}})
            garbage = notice(event, vault, markers, environ=env92,
                             options={"context_meter": {"enabled": "no", "autocompact_tokens": 100000}})
            checks.append(("unknown threshold, enabled=false and a garbage enabled value say nothing",
                           unknown is None and disabled is None and garbage is None
                           and not markers.exists()))

            # 6. 只有一段、在 0.97T：低於不發；跨過發一次；同一個壓縮週期不重發。
            options = {"context_meter": {"autocompact_tokens": 100000}}

            def call(total, directory=markers, extra=None):
                write(transcript, [assistant(total)])
                found = notice({**event, **(extra or {})}, vault, directory, options=options, environ=env92)
                if found is not None:
                    claim(directory, found[1])
                return found

            far_below = call(66000)
            just_below = call(96900)
            first = call(97500)
            again = call(98000)
            past_threshold = call(101000)
            try:
                from . import compact_map as _compact_map
            except ImportError:
                import compact_map as _compact_map
            handoff = _compact_map.handoff_destination(vault, "s1", transcript)
            checks.append(("one reminder at 0.97T: nothing below it, once when crossed, not again in the cycle",
                           far_below is None and just_below is None
                           and first is not None and again is None and past_threshold is None
                           and first[1] == memspec.CONTEXT_METER_MARKER
                           and sorted(item.name for item in markers.iterdir()) == [memspec.CONTEXT_METER_MARKER]
                           and first[0] == memspec.CONTEXT_METER_NOTICE.format(
                               cur=98, left=2, mark="", path=os.fspath(handoff))))

            # 7. 子代理不說、不寫標記，主線之後照樣收得到。
            sub = root / "sub"
            by_agent = call(98000, sub, {"agent_id": "a1"})
            by_agent_camel = call(98000, sub, {"agentId": "a1"})
            agent_left_nothing = not sub.exists() or not any(sub.iterdir())
            main_after = call(98000, sub)
            checks.append(("a subagent call says nothing and leaves the main thread's reminder armed",
                           by_agent is None and by_agent_camel is None and agent_left_nothing
                           and main_after is not None
                           and main_after[1] == memspec.CONTEXT_METER_MARKER))

            # 8. 標記清掉（壓縮）之後重新武裝；換算值標上「（換算）」。
            for item in markers.iterdir():
                item.unlink()
            rearmed = call(97500)
            scaled_options = {}
            write(transcript, [assistant(60000)])
            scaled_line = notice({**event, "session_id": "s2"}, vault, root / "scaled",
                                 options=scaled_options, environ={memspec.CONTEXT_METER_PCT_ENV: "46"})
            checks.append(("cleared markers re-arm the reminder; a scaled threshold is labelled",
                           rearmed is not None and rearmed[1] == memspec.CONTEXT_METER_MARKER
                           and scaled_line is not None
                           and memspec.CONTEXT_METER_SCALED_MARK in scaled_line[0]
                           and memspec.CONTEXT_METER_SCALED_MARK not in first[0]))

            # 9. 學習：record_autocompact 追加一筆、壞檔重建、只留 10 筆。
            state.write_text("{broken", encoding="utf-8")
            write(transcript, [assistant(150000)])
            rebuilt = record_autocompact(vault, transcript, environ=env92)
            for _ in range(12):
                record_autocompact(vault, transcript, environ=env92)
            kept = read_samples(state)
            checks.append(("autocompact learning rebuilds a broken state file and keeps the last 10",
                           rebuilt is not None and len(rebuilt) == 1
                           and rebuilt[0]["tokens"] == 150000 and rebuilt[0]["pct"] == "92"
                           and len(kept) == memspec.CONTEXT_METER_STATE_KEEP
                           and not list(state.parent.glob(".*.tmp-*"))))

            # 9b. 兩個行程同時記錄：讀–追加–換名在同一把鎖裡，每一筆都留下。
            race_vault = root / "race-vault"
            race_dir = root / "race"
            race_dir.mkdir()
            child = (
                "import sys, time, pathlib\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from epitype import context_meter\n"
                "ready = pathlib.Path(sys.argv[4]) / ('ready-' + sys.argv[5])\n"
                "go = pathlib.Path(sys.argv[4]) / 'go'\n"
                "ready.write_text('1')\n"
                "limit = time.monotonic() + 60\n"
                "while not go.exists() and time.monotonic() < limit:\n"
                "    time.sleep(0.001)\n"
                "for _ in range(3):\n"
                "    context_meter.record_autocompact(sys.argv[2], sys.argv[3],"
                " environ={'" + memspec.CONTEXT_METER_PCT_ENV + "': '92'})\n"
            )
            repo_root = Path(__file__).resolve().parents[1]
            racers = []
            for name, tokens in (("a", 111111), ("b", 222222)):
                source = race_dir / f"{name}.jsonl"
                write(source, [assistant(tokens)])
                racers.append(subprocess.Popen(
                    [sys.executable, "-c", child, os.fspath(repo_root), os.fspath(race_vault),
                     os.fspath(source), os.fspath(race_dir), name]))
            limit = time.monotonic() + 60
            while (not all((race_dir / f"ready-{name}").exists() for name in "ab")
                   and time.monotonic() < limit):
                time.sleep(0.01)
            (race_dir / "go").write_text("1", encoding="utf-8")
            codes = [racer.wait(timeout=120) for racer in racers]
            raced = [sample["tokens"] for sample in read_samples(state_path(race_vault))]
            checks.append(("two processes recording at once both keep every sample",
                           codes == [0, 0]
                           and sorted(raced) == [111111] * 3 + [222222] * 3
                           and not Path(os.fspath(state_path(race_vault)) + ".lock").exists()))

            # 10. calibrate：只取 auto、近 30 天、最近 5 筆的中位數；只報告、不寫、沒有 --apply。
            projects = root / "projects"
            (projects / "p1").mkdir(parents=True)
            now = datetime(2026, 9, 25, tzinfo=timezone.utc)

            def boundary(trigger, tokens, days_ago):
                return json.dumps({"type": "system", "subtype": "compact_boundary",
                                   "timestamp": (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                   "compactMetadata": {"trigger": trigger, "preTokens": tokens}})

            write(projects / "p1" / "a.jsonl", [
                boundary("auto", 1, 40), boundary("manual", 999999, 1),
                tool_result(10), boundary("auto", 180000, 9), boundary("auto", 181000, 8),
                boundary("auto", 182000, 7), boundary("auto", 183000, 6), boundary("auto", 900000, 5),
                boundary("auto", 184000, 4)])
            tight = root / "projects-tight"
            (tight / "p1").mkdir(parents=True)
            write(tight / "p1" / "a.jsonl", [
                boundary("auto", 1, 40), boundary("manual", 999999, 1), tool_result(10),
                boundary("auto", 180000, 9), boundary("auto", 181000, 8), boundary("auto", 182000, 7),
                boundary("auto", 183000, 6), boundary("auto", 186000, 5), boundary("auto", 184000, 4)])
            lines = []
            candidate = calibrate(tight, now=now, out=lines.append)
            cal_vault = root / "cal-vault"
            cal_vault.mkdir()
            cal_config = root / "cal-config.json"
            cal_config.write_text(json.dumps({memspec.CONFIG_VAULTS_FIELD: [os.fspath(cal_vault)]}),
                                  encoding="utf-8")
            saved_config = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
            os.environ[memspec.EPITYPE_CONFIG_ENV] = os.fspath(cal_config)
            sink = io.StringIO()
            try:
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    cli_code = main(["calibrate", "--root", os.fspath(tight)])
                    try:
                        main(["calibrate", "--apply"])
                        apply_rejected = False
                    except SystemExit as exit_:
                        apply_rejected = exit_.code == 2
            finally:
                if saved_config is None:
                    os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None)
                else:
                    os.environ[memspec.EPITYPE_CONFIG_ENV] = saved_config
            checks.append(("calibrate only reports the median of the last 5 auto compactions within 30 days, "
                           "says it writes nothing, and has no --apply",
                           candidate == 183000
                           and "samples=5" in lines[0] and "(of 6 found)" in lines[0]
                           and memspec.CONTEXT_METER_CALIBRATE_REPORT_ONLY in lines
                           and cli_code == 0 and apply_rejected
                           and not state_path(cal_vault).exists()))

            # 11. 效能：50 MB transcript、最後一列是 2 MB 工具結果，hook 只讀檔尾。
            large = root / "large.jsonl"
            filler = (assistant(1234) + "\n").encode("utf-8") * 1
            filler_block = filler * max(1, (1024 * 1024) // len(filler))
            with large.open("wb") as stream:
                for _ in range(48):
                    stream.write(filler_block)
                stream.write((assistant(222222) + "\n").encode("utf-8"))
                stream.write((tool_result(2 * 1024 * 1024) + "\n").encode("utf-8"))
            started = time.perf_counter()
            measured = current_tokens(large)  # 預設的 150 ms 上限：逾時就不會是 222222
            elapsed = time.perf_counter() - started
            print(f"context_meter: current_tokens on a {large.stat().st_size / 1e6:.1f} MB "
                  f"transcript took {elapsed * 1000:.1f} ms")
            # 時間上限的回退用注入的時鐘驗：每讀一次前進 1 秒，預設 150 ms 在第一塊之前就用完；
            # 時鐘不動就永遠不逾時。不賭 Windows 計時器的解析度。
            ticks = iter(range(1_000_000))
            exhausted = current_tokens(large, clock=lambda: float(next(ticks)))
            frozen = current_tokens(large, clock=lambda: 0.0)
            checks.append(("current_tokens on a 50 MB transcript ending in a 2 MB tool result answers inside the "
                           "default 150 ms limit and under 300 ms; an exhausted default limit gives None",
                           measured == 222222 and large.stat().st_size >= 50 * 1000 * 1000
                           and elapsed < 0.3 and exhausted is None and frozen == 222222))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 15
    status_word = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status_word} {passed}/{total}")
    if status_word != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status_word == "PASS" else 1


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="epitype context-meter",
        description="Claude Code context meter: learned auto-compaction threshold and current usage.")
    parser.add_argument("--selftest", action="store_true")
    commands = parser.add_subparsers(dest="command")
    calibrate_parser = commands.add_parser(
        "calibrate", help="report (read-only) the auto-compaction points in recent Claude Code transcripts")
    calibrate_parser.add_argument("--root", help="transcript root (default: ~/.claude/projects)")
    status_parser = commands.add_parser("status", help="print the threshold, its source and the current usage")
    status_parser.add_argument("--transcript", help="a Claude Code transcript JSONL to measure")
    status_parser.add_argument("--vault", help="governance vault (default: from the Epitype config)")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "calibrate":
        root = Path(args.root).expanduser() if args.root else Path.home() / ".claude" / "projects"
        calibrate(root)
        return 0
    options = memspec.config_options()
    status(options, _governance(args.vault, options), args.transcript)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
