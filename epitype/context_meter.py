import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""Claude Code 與 Codex 的 context 用量計：壓縮前提醒寫交接，壓縮後由 SessionStart 交回。

模型看不到自己的 context 用量，也無法自己觸發壓縮；PreCompact 的文字到不了模型
（2026-08-19 實證）。所以只能在壓縮「之前」、由每次都會跑的 hook（PreToolUse、
UserPromptSubmit）在跨過學到的壓縮點 97% 的那一次附一行字，提醒模型自己把交接落檔。這支模組只算數字與決定要不要說，
輸出與標記的時機歸 adapter：先搶標記（獨占建立）、搶到的才輸出——Codex 並行的工具呼叫
會同時看到「還沒說過」；搶到卻沒送出去的放掉標記，下次再試。

門檻不猜（見 memspec 的 CONTEXT_METER 段落）：設定覆寫 → 學到的自動壓縮用量 → 都沒有
就完全不提醒。hook 內每次呼叫只讀 transcript 檔尾，找不到就算了，永遠不讀整檔。

Codex 不套上面這一套：同一個 hook 從檔尾的列認出是 Codex rollout，就照 Codex 自己的算法
算用量（最新 token_count＋最後一個模型產出項之後各項的估計），壓縮點從 Codex 的設定與
模型目錄算，提醒點是壓縮點減固定餘裕（memspec 的 Codex 段）。兩邊共用同一個標記名與
交接檔路徑，壓縮後的交回也是同一條路。
"""

import json
import os
from pathlib import Path
import re
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


def _claude_line(raw):
    """Claude transcript 的一行：(found, value)，found=True 時 value 是用量或 None。

    壓縮邊界之後還沒有新的 assistant 用量時，檔裡最後一筆用量是壓縮「前」的數字——
    拿它算，剛壓縮完就會立刻再叫一次。所以碰到邊界就停，回「目前不知道」。"""
    if b'"usage"' not in raw and b'"compact_boundary"' not in raw:
        return False, None
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False, None  # 正在寫的最後一行、或壞行：當作不存在。
    if _is_compact_boundary(row):
        return True, None
    total = _usage_total(row)
    if total is not None:
        return True, total
    return False, None


def _scan_lines(lines):
    """由後往前看完整的行。回 (found, value)：found=True 時 value 是用量或 None。"""
    for raw in reversed(lines):
        found, value = _claude_line(raw)
        if found:
            return True, value
    return False, None


def _oversized_assistant(tail):
    """一行超過單行上限、開頭還沒讀到時，用已讀進來的行尾判斷它是不是 assistant 列。

    真實 transcript 的 assistant 列把頂層 "type" 排在 message 之後、貼近行尾；user 列
    （工具結果）排在 message 之前。認得出是 assistant 列就代表最新用量讀不到——這時回
    None（不提醒），而不是往前拿一筆更早、更小的用量頂替，那會漏掉該發的提醒。"""
    window = tail[-memspec.CONTEXT_METER_OVERSIZED_TAIL_BYTES:]
    return any(marker in window for marker in memspec.CONTEXT_METER_ASSISTANT_TYPE_MARKERS)


_CODEX_ROW_HEAD = re.compile(memspec.CONTEXT_METER_CODEX_ROW_HEAD)
_CODEX_ROLE = re.compile(rb'"role":"([a-z_]{1,32})"')
# 超過單行上限的 Codex 列只看得到行首；role 在 payload 開頭附近（type、id 之後）。
_CODEX_ROLE_WINDOW_BYTES = 4096


def _codex_head(raw):
    """Codex rollout 一列的 (列型別, 項型別)；不是 Codex 列回 None。只看行首，不 parse。"""
    match = _CODEX_ROW_HEAD.match(raw)
    if match is None:
        return None
    sub = match.group(2)
    return match.group(1).decode("ascii"), (sub.decode("ascii") if sub else None)


def _text_bytes(value):
    return len(value.encode("utf-8")) if isinstance(value, str) else 0


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _codex_part_bytes(part):
    """一個內容片段的模型可見位元組（history.rs estimate_response_item_model_visible_bytes）。
    認不得的片段（例如音訊）回 None：估不出來就當整個用量不知道，不拿 0 頂替。"""
    if not isinstance(part, dict):
        return None
    kind = part.get("type")
    if kind in ("input_text", "output_text", "text"):
        return _text_bytes(part.get("text"))
    if kind == "input_image":
        if part.get("detail") == "original":
            return memspec.CONTEXT_METER_CODEX_ORIGINAL_IMAGE_BYTES
        return memspec.CONTEXT_METER_CODEX_IMAGE_BYTES
    if kind == "encrypted_content":
        return -(-_text_bytes(part.get("encrypted_content")) * 9 // 16)
    return None


def _codex_parts_bytes(parts):
    if isinstance(parts, str):
        return _text_bytes(parts)
    if not isinstance(parts, list):
        return None
    total = 0
    for part in parts:
        size = _codex_part_bytes(part)
        if size is None:
            return None
        total += size
    return total


def _codex_model_generated(payload):
    kind = payload.get("type")
    if kind == "message":
        return payload.get("role") == "assistant"
    return kind in memspec.CONTEXT_METER_CODEX_MODEL_ITEM_TYPES


def _codex_item_tokens(payload):
    """一個非模型產出項的估計 tokens（位元組 ÷4 無條件進位）；估不出來回 None。

    Codex 對不認得的項算 0（ResponseItem::Other），這裡照做；認得、但內容片段估不出來
    的才回 None。"""
    kind = payload.get("type")
    if kind == "message":
        size = _codex_parts_bytes(payload.get("content"))
    elif kind == "agent_message":
        size = _codex_parts_bytes(payload.get("content"))
        if size is not None:
            size += _text_bytes(payload.get("author")) + _text_bytes(payload.get("recipient"))
    elif kind in ("function_call_output", "custom_tool_call_output"):
        size = _codex_parts_bytes(payload.get("output"))
        if size is not None:
            size += sum(_text_bytes(payload.get(field)) for field in ("call_id", "name", "namespace"))
    elif kind in ("tool_search_output", "additional_tools"):
        tools = payload.get("tools")
        size = _json_bytes(tools) if tools is not None else 0
    else:
        size = 0
    if size is None:
        return None
    per_token = memspec.CONTEXT_METER_CODEX_BYTES_PER_TOKEN
    return -(-size // per_token)


def _nonnegative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class Reading:
    """一次檔尾讀取的結果。tokens 為 None＝不知道；window 是 Codex 自報的硬上限。"""

    __slots__ = ("host", "tokens", "window", "model")

    def __init__(self, host, tokens, window=None, model=None):
        self.host, self.tokens, self.window, self.model = host, tokens, window, model

    def __repr__(self):
        return f"Reading({self.host!r}, {self.tokens!r}, window={self.window!r}, model={self.model!r})"


class _CodexTail:
    """由後往前看 Codex rollout 的列，照 Codex 自己的算法算用量。

    先找最新一筆 token_count（有 info 的）與最後一個模型產出項；兩者之間、以及之後的
    非模型項（工具結果、使用者／developer 訊息）逐項估計加上去。壓縮邊界（compacted 列）
    比最新的 token_count 還新＝壓縮完還沒有新的用量，回不知道——拿壓縮前的數字算，剛
    壓縮完就會立刻再叫一次。途中碰到的 turn_context 順手記下模型名稱（hook 輸入沒帶
    model 時的退路），但不為它多讀。"""

    def __init__(self):
        self.total = None
        self.window = None
        self.after = 0
        self.anchored = False
        self.model = None
        self.unknown = False
        self.counted = False

    def _settle(self):
        if self.total is None or not self.anchored:
            return False
        self.counted = True
        return True

    def _parse(self, raw):
        try:
            row = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None  # 正在寫的最後一行、或壞行：當作不存在。
        payload = row.get("payload") if isinstance(row, dict) else None
        return payload if isinstance(payload, dict) else None

    def feed(self, kind, sub, raw):
        """一行完整的列；回 True＝夠了，停止往前讀。"""
        if kind == "compacted":
            return self._boundary()
        if kind == "turn_context" and self.model is None:
            payload = self._parse(raw)
            model = payload.get("model") if payload else None
            if isinstance(model, str) and model:
                self.model = model
        elif kind == "event_msg" and sub == "token_count" and self.total is None:
            payload = self._parse(raw)
            info = payload.get("info") if payload else None
            usage = info.get("last_token_usage") if isinstance(info, dict) else None
            total = usage.get("total_tokens") if isinstance(usage, dict) else None
            if _nonnegative_int(total):
                self.total = total
                window = info.get("model_context_window")
                self.window = window if _nonnegative_int(window) and window > 0 else None
        elif kind == "response_item" and not self.anchored:
            payload = self._parse(raw)
            if payload is None:
                return False
            if _codex_model_generated(payload):
                self.anchored = True
            else:
                tokens = _codex_item_tokens(payload)
                if tokens is None:
                    self.unknown = True
                    return True
                self.after += tokens
        return self._settle()

    def _boundary(self):
        if self.total is None:
            self.unknown = True
            return True
        # 壓縮後的歷史從這裡開始：邊界之前的項不在這一段裡，用量就是邊界之後的部分。
        self.anchored = True
        return self._settle()

    def oversized(self, head, raw_head):
        """超過單行上限的一列，只看得到行首：認得出型別就照型別處理，否則略過。"""
        if head is None:
            return False
        kind, sub = head
        if kind == "compacted":
            return self._boundary()
        if kind == "response_item" and not self.anchored:
            if sub == "message":
                match = _CODEX_ROLE.search(raw_head[:_CODEX_ROLE_WINDOW_BYTES])
                generated = match is not None and match.group(1) == b"assistant"
            else:
                generated = sub in memspec.CONTEXT_METER_CODEX_MODEL_ITEM_TYPES
            if not generated:
                # 讀不完的工具結果（多半是 base64 圖片）估不出模型可見的大小：不知道。
                self.unknown = True
                return True
            self.anchored = True
            return self._settle()
        return False

    def reading(self, complete):
        """讀到檔頭都沒碰到模型產出項：從檔頭起的每一項都算在最後一筆用量之後。
        讀到上限或逾時還沒定下來：不知道。"""
        if self.unknown or self.total is None or not (self.counted or complete):
            return None
        return Reading(memspec.CONTEXT_METER_HOST_CODEX, self.total + self.after, self.window, self.model)


class _Tail:
    """檔尾反向讀到的行交給這裡：第一行認得出宿主之後，就照那個宿主的規則找用量。"""

    def __init__(self):
        self.host = None
        self.done = False
        self.value = None
        self.codex = _CodexTail()

    def _classify(self, raw):
        self.host = (memspec.CONTEXT_METER_HOST_CODEX if _codex_head(raw) is not None
                     else memspec.CONTEXT_METER_HOST_CLAUDE)

    def lines(self, lines):
        for raw in reversed(lines):
            if self.host is None:
                if not raw.strip():
                    continue
                self._classify(raw)
            if self.host == memspec.CONTEXT_METER_HOST_CLAUDE:
                found, value = _claude_line(raw)
                if found:
                    self.value, self.done = value, True
                    return True
                continue
            head = _codex_head(raw)
            if head is not None and self.codex.feed(head[0], head[1], raw):
                self.done = True
                return True
        return False

    def oversized_tail(self, tail):
        """一行超過單行上限、開頭還沒讀到：Claude（或還認不出宿主時）先用行尾認 assistant 列。"""
        if self.host == memspec.CONTEXT_METER_HOST_CODEX:
            return False
        if _oversized_assistant(tail):
            self.value, self.done = None, True
            return True
        return False

    def oversized_head(self, head):
        """那一行的開頭終於讀到了（只有這一段，整行已丟）。"""
        if self.host is None:
            if not head.strip():
                return False
            self._classify(head)
        if self.host != memspec.CONTEXT_METER_HOST_CODEX:
            return False
        if self.codex.oversized(_codex_head(head), head):
            self.done = True
            return True
        return False

    def result(self, complete):
        if self.host == memspec.CONTEXT_METER_HOST_CODEX:
            return self.codex.reading(complete)
        if self.done and self.value is not None:
            return Reading(memspec.CONTEXT_METER_HOST_CLAUDE, self.value)
        return None


def measure(transcript_path, time_limit=None, clock=None):
    """這場目前的 context 用量：Reading，或 None（不知道）。

    從檔尾反向分塊讀：首塊 64 KiB、逐次加倍到 1 MiB 為止，總共最多 64 MiB，而且有
    時間上限（預設 150 ms，到了就停：hook 每次工具呼叫都要付這一份；時鐘可注入，
    測試不必賭 Windows 計時器的解析度）。最後一行
    可能是好幾 MB 的工具結果：一行超過單行上限還沒看到開頭，先用行尾認它是不是
    Claude 的 assistant 列——是就回 None；不是就把累積的片段丟掉、只繼續往前找換行，
    記憶體不跟著它長；那一行的開頭讀到時再交給 Codex 的規則認型別。任何讀檔錯誤都回 None。"""
    line_max = memspec.CONTEXT_METER_LINE_MAX_BYTES
    limit = memspec.CONTEXT_METER_TAIL_SECONDS if time_limit is None else time_limit
    clock = time.monotonic if clock is None else clock
    deadline = clock() + limit
    tail = _Tail()
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
                    return tail.result(complete=False)
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
                            if tail.oversized_tail(pending):
                                return tail.result(complete=False)
                            pending, oversized = b"", True
                    continue
                body = chunk[cut + 1:]
                if oversized:
                    last = body.rfind(b"\n")
                    complete = body[:last + 1] if last >= 0 else b""
                    # 那一行排在 complete 之後：反向讀的順序裡它先處理。
                    if tail.oversized_head(body[last + 1:]):
                        return tail.result(complete=False)
                else:
                    complete = body + pending
                if tail.lines(complete.split(b"\n")):
                    return tail.result(complete=False)
                pending = chunk[:cut] if start > 0 else b""
                oversized = len(pending) > line_max
                if oversized:
                    if tail.oversized_tail(pending):
                        return tail.result(complete=False)
                    pending = b""
        except OSError:
            return None
    return tail.result(complete=(end == 0))


def current_tokens(transcript_path, time_limit=None, clock=None):
    """Claude transcript 目前的 context 用量（最後一筆主鏈 assistant 的三欄加總）；找不到回 None。

    只認 Claude：Codex rollout 一律回 None——這支也餵 PreCompact 的學習，Codex 的數字
    不能學進 Claude 的門檻。讀法見 `measure`。"""
    reading = measure(transcript_path, time_limit, clock)
    if reading is None or reading.host != memspec.CONTEXT_METER_HOST_CLAUDE:
        return None
    return reading.tokens


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


# ---------------------------------------------------------------- Codex 壓縮點


def codex_home(environ=None, home=None):
    """Codex 的設定目錄：`$CODEX_HOME`，沒設就是 `~/.codex`。"""
    environ = os.environ if environ is None else environ
    value = environ.get(memspec.CONTEXT_METER_CODEX_HOME_ENV)
    if isinstance(value, str) and value.strip():
        return Path(value.strip()).expanduser()
    return Path(home or Path.home()) / ".codex"


def codex_limit(model=None, environ=None, home=None):
    """Codex 這一刻的自動壓縮點 tokens；讀不到、看不懂或不在支援範圍內都回 None。

    算法照 Codex 原始碼（memspec 的 Codex 段）：壓縮點＝min(自動門檻, 硬上限)。模型名稱
    先用 rollout 最新 turn_context 的，沒有才用設定的 `model`。用到才載入 tomllib（約
    40–60 ms）：呼叫端只在用量已經接近 Codex 自報的硬上限時才走到這裡。"""
    root = codex_home(environ, home)
    try:
        import tomllib

        config = tomllib.loads((root / memspec.CONTEXT_METER_CODEX_CONFIG_FILENAME).read_text(
            encoding="utf-8"))
        catalog = json.loads((root / memspec.CONTEXT_METER_CODEX_CATALOG_FILENAME).read_text(
            encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return None
    if not isinstance(config, dict) or not isinstance(catalog, dict):
        return None
    # profile 會整組覆寫模型與門檻；其他範圍與 token_budget 換掉的是壓縮的算法本身。
    if config.get("profile") is not None:
        return None
    if config.get("model_auto_compact_token_limit_scope", "total") != "total":
        return None
    features = config.get("features", {})
    if not isinstance(features, dict):
        return None
    budget = features.get("token_budget", False)
    if budget is not False and not (isinstance(budget, dict) and budget.get("enabled") is False):
        return None
    if not isinstance(model, str) or not model:
        model = config.get("model")
    if not isinstance(model, str) or not model:
        return None
    models = catalog.get("models")
    if not isinstance(models, list):
        return None
    entry = next((item for item in models if isinstance(item, dict) and item.get("slug") == model), None)
    if entry is None:
        return None
    percent = entry.get("effective_context_window_percent")
    catalog_window = entry.get("context_window")
    maximum = entry.get("max_context_window")
    auto = config.get("model_auto_compact_token_limit", entry.get("auto_compact_token_limit"))
    configured = config.get("model_context_window")
    if not _positive_int(percent) or any(
            value is not None and not _positive_int(value)
            for value in (catalog_window, maximum, auto, configured)):
        return None
    if configured is not None:
        window = min(configured, maximum) if maximum is not None else configured
    else:
        window = catalog_window if catalog_window is not None else maximum
    if window is None:
        return None
    ceiling = window * memspec.CONTEXT_METER_CODEX_AUTO_NUMERATOR // memspec.CONTEXT_METER_CODEX_AUTO_DENOMINATOR
    auto = ceiling if auto is None else min(auto, ceiling)
    return min(auto, window * percent // 100)


def _codex_limit_cached(model, directory, environ=None, home=None):
    """`codex_limit`，但記在這一場的標記目錄裡：鍵是設定檔與模型目錄的 (mtime_ns, 大小)、
    Codex 設定目錄與模型名稱，任何一項變了就重算。

    每次工具呼叫都要知道壓縮點，而重算一次要載入 tomllib 再解析兩個檔（實測 90–130 ms）。
    標記目錄在壓縮時整個清掉，所以每個壓縮週期最多重算一次。"""
    root = codex_home(environ, home)
    try:
        stats = [os.stat(root / name) for name in (memspec.CONTEXT_METER_CODEX_CONFIG_FILENAME,
                                                     memspec.CONTEXT_METER_CODEX_CATALOG_FILENAME)]
    except OSError:
        return None
    key = [os.fspath(root), model or ""] + [value for info in stats for value in (info.st_mtime_ns, info.st_size)]
    cache = Path(directory) / memspec.CONTEXT_METER_CODEX_LIMIT_CACHE if directory is not None else None
    if cache is not None:
        try:
            stored = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and stored.get("key") == key:
                limit = stored.get("limit")
                if limit is None or _positive_int(limit):
                    return limit
        except (OSError, ValueError):
            pass
    limit = codex_limit(model, environ, home)
    if cache is not None:
        staging = cache.with_name("." + cache.name + ".tmp-%d" % os.getpid())
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            staging.write_text(json.dumps({"key": key, "limit": limit}), encoding="utf-8")
            os.replace(staging, cache)
        except OSError:
            try:
                staging.unlink()
            except OSError:
                pass
    return limit


def codex_due(reading, marker_directory=None, environ=None, home=None, model=None):
    """(用量, 壓縮點)：這一刻該提醒 Codex 寫交接；否則 None。

    提醒點是壓縮點減固定餘裕，不是比例：Codex 一步就可能跨過好幾萬 tokens。模型名稱
    先用 hook 輸入的 `model`（Codex 每次都帶這一回合的模型），再用 rollout 途中看到的
    turn_context，最後才是設定的 `model`。"""
    current = reading.tokens
    if not _nonnegative_int(current):
        return None
    if marker_directory is not None and (Path(marker_directory) / memspec.CONTEXT_METER_MARKER).exists():
        return None  # 這個壓縮週期已經說過。
    model = model if isinstance(model, str) and model else reading.model
    limit = _codex_limit_cached(model, marker_directory, environ, home)
    if limit is None or current < limit - memspec.CONTEXT_METER_CODEX_HEADROOM_TOKENS:
        return None
    return current, limit


# ---------------------------------------------------------------- hook 端


def notice(event, vault, marker_directory, options=None, environ=None, home=None, clock=None):
    """這次呼叫要附的那一行、標記名與當下用量：(line, marker, tokens)；不該說就回 None。
    永不丟例外。

    這只是「看起來還沒說過」：呼叫端要在輸出之前用 `claim(目錄, marker, tokens)` 搶標記，
    搶到的才輸出——Codex 一次並行好幾個工具呼叫，每個 hook 行程這裡都會回同一行。搶到卻
    沒送出去（預算擠掉、逾時、輸出失敗）的要 `release`，下一次再試。子代理的呼叫一律不說、
    也不碰標記——提醒被子代理吃掉，主線就永遠收不到了（Codex 的子代理有自己的 session_id
    與 rollout，一樣帶 agent_id）。宿主由 transcript 檔尾的列決定：Codex rollout 走
    `codex_due`，其餘照 Claude 的門檻。標記記的用量比現在高出很多＝壓縮過而 PreCompact
    沒清到，先重新武裝（`_rearm_if_stale`）。`clock` 只給自測注入（見 `measure`）。"""
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
        reading = measure(transcript, clock=clock)
        if reading is None:
            return None
        stored = _rearm_if_stale(marker_directory, memspec.CONTEXT_METER_MARKER, reading.tokens)
        if stored is not None:
            _trace_rearm(event, stored, reading.tokens)
        if reading.host == memspec.CONTEXT_METER_HOST_CODEX:
            due = codex_due(reading, marker_directory, environ, home, event.get("model"))
            if due is None:
                return None
            current, limit = due
            line = memspec.CONTEXT_METER_CODEX_NOTICE.format(
                cur=_k(current), left=_k(limit - current),
                path=os.fspath(_handoff(vault, session_id, transcript)))
            return line, memspec.CONTEXT_METER_MARKER, current
        limit, source = threshold(options, state_path(vault), environ, home)
        if limit is None:
            return None
        current = reading.tokens
        marker = stage(current, limit)
        if marker is None:
            return None
        if (Path(marker_directory) / marker).exists():
            return None  # 這個壓縮週期已經說過。
        path = _handoff(vault, session_id, transcript)
        return render(current, limit, source, path), marker, current
    except Exception:
        return None


def _marker_tokens(path):
    """標記裡記的用量；舊格式（只有標記名）、壞檔、讀不到都回 None＝不判定過期。"""
    try:
        with open(path, "rb") as stream:
            value = json.loads(stream.read(memspec.CONTEXT_METER_MARKER_MAX_BYTES))
    except (OSError, ValueError):
        return None
    tokens = value.get("tokens") if isinstance(value, dict) else None
    return tokens if _positive_int(tokens) else None


def _rearm_if_stale(marker_directory, marker, current):
    """標記在、但現在的用量比標記記的跌了 REARM_DROP_RATIO 以上：移掉標記，回標記記的用量；
    否則回 None。

    PreCompact 是清標記的主路，但它沒跑完（宿主逾時砍掉）的話，下一個週期就永遠不提醒。
    這裡要在「低點」就移掉：到了下一次提醒點，用量又爬回標記記的那一帶，就看不出來了。
    移不掉就當它還在——寧可這一次不說，也不要每次都說。"""
    if marker_directory is None or not _nonnegative_int(current):
        return None
    path = Path(marker_directory) / marker
    stored = _marker_tokens(path)
    if stored is None or current > stored * (1 - memspec.CONTEXT_METER_REARM_DROP_RATIO):
        return None
    try:
        path.unlink()
    except FileNotFoundError:
        return None  # 同時間別的行程已經移掉了；由它記。
    except OSError:
        return None
    return stored


def _trace_rearm(event, stored, current):
    try:
        try:
            from . import meter_trace
        except ImportError:
            import meter_trace
        meter_trace.record(event.get("hook_event_name") or "meter", event, None,
                           outcome="meter-rearmed", stored=stored, tokens=current)
    except Exception:
        pass


def _handoff(vault, session_id, transcript):
    # 用到才載入：compact_map 會帶進 hashlib，絕大多數呼叫走不到這裡。
    try:
        from . import compact_map
    except ImportError:
        import compact_map
    return compact_map.handoff_destination(vault, session_id, transcript)


def claim(marker_directory, marker, tokens=None):
    """搶「這個壓縮週期由我來說」：獨占建立標記，搶到回 True。

    同時跑的幾個 hook 行程只有一個建得起來，其餘回 False、不得輸出這一行。已經有了、
    寫不了都回 False。標記記下當下用量（給 `_rearm_if_stale`）與行程號（給 `release`）。
    建起來了卻寫不進內容：刪掉自己剛建的空標記再回 False——留著它，這個週期就再也不說。"""
    try:
        path = Path(marker_directory) / marker
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("x", encoding="ascii")
    except (OSError, TypeError, ValueError):
        return False
    try:
        with stream:
            stream.write(json.dumps({"tokens": tokens if _positive_int(tokens) else None,
                                     "pid": os.getpid()}) + "\n")
    except (OSError, TypeError, ValueError):
        try:
            path.unlink()
        except OSError:
            pass
        return False
    return True


def release(marker_directory, marker):
    """放掉自己搶到、卻沒送出去的標記，讓下一次再試。只刪自己行程寫的那一份；永不丟例外。"""
    try:
        path = Path(marker_directory) / marker
        with open(path, "rb") as stream:
            value = json.loads(stream.read(memspec.CONTEXT_METER_MARKER_MAX_BYTES))
        if isinstance(value, dict) and value.get("pid") == os.getpid():
            path.unlink()
            return True
    except (OSError, TypeError, ValueError):
        pass
    return False


def record_autocompact(vault, transcript_path, environ=None, now=None, home=None, clock=None):
    """自動壓縮前的那一刻記下當下用量，給之後當門檻學。回寫入後的紀錄，或 None。

    只由 PreCompact 在 trigger=auto 時呼叫：手動壓縮的時點是人選的，不代表門檻。
    讀–追加–換名在同一把鎖裡：兩個場次同時壓縮時，沒有鎖的話後寫的會蓋掉先寫的那筆。
    拿不到鎖就放棄這一筆（回 None）——少學一筆無妨，卡住壓縮不行。`clock` 只給自測注入。"""
    tokens = current_tokens(transcript_path, clock=clock)
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
    reading = measure(transcript) if transcript else None
    if reading is not None and reading.host == memspec.CONTEXT_METER_HOST_CODEX:
        limit = codex_limit(reading.model, environ)
        headroom = memspec.CONTEXT_METER_CODEX_HEADROOM_TOKENS
        out(f"status: host=codex current={reading.tokens} model={reading.model or '(config model)'} "
            f"hard_cap={reading.window or 'unknown'} limit={limit or 'unknown'} "
            f"remind_at={limit - headroom if limit else '-'} "
            f"due={'yes' if limit and reading.tokens >= limit - headroom else 'no'}")
        return limit
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
        current = reading.tokens if reading is not None else None
        if current is None:
            out(f"status: current usage unknown for {transcript}")
        else:
            where = stage(current, limit) if limit else None
            share = f" ({current / limit:.0%} of threshold)" if limit else ""
            out(f"status: current={current} tokens{share} due={'yes' if where else 'no'}")
    return limit


# ---------------------------------------------------------------- Codex 重播（唯讀）


_CODEX_NAME = re.compile(rb'"name":"([^"]{1,128})"')


def _codex_replay_file(path, window):
    """一個 rollout 從頭重播：每個壓縮週期裡 hook 會跑的那些點當下的用量（Codex 算法）。

    回 (週期清單, 自動壓縮數)。週期＝[提醒點清單, 結束時的取樣序號, 是否以自動壓縮結束
    （None＝檔尾、沒有壓縮）, 當時的硬上限, 是否主線, 壓縮那一刻的用量]；
    提醒點＝(用量或 None, 當下的取樣序號)。hook 點：PreToolUse＝每個會觸發它的工具呼叫
    （write_stdin 與 code-mode wait 不觸發），用量含那一列本身；UserPromptSubmit＝一回合
    開始（task_started）後、第一個 response_item 之前，在回合前的壓縮之後。取樣序號＝
    模型開始一次新回應的次數（模型產出項緊跟在非模型項之後）。只收 Codex 自報的硬上限
    等於 window 的週期（同一組設定）。"""
    total = None
    reported = None
    after = 0  # None＝有一項估不出來
    previous_generated = False
    sampling = 0
    points = []
    cycles = []
    turn_active = False
    turn_compactions = []
    pending_prompt = False
    line_max = memspec.CONTEXT_METER_LINE_MAX_BYTES
    skip_tools = memspec.CONTEXT_METER_CODEX_UNHOOKED_TOOLS

    def current():
        if total is None or after is None:
            return None
        return total + after

    def close_turn():
        # 自動或手動：手動壓縮自成一回合，裡面沒有使用者訊息、也沒有模型產出項。
        for index in turn_compactions:
            cycles[index][2] = turn_active
        turn_compactions.clear()

    main = True
    with open(path, "rb") as stream:
        for number, raw in enumerate(stream):
            if number == 0 and memspec.CONTEXT_METER_CODEX_SUBAGENT_MARKER in raw[:_CODEX_ROLE_WINDOW_BYTES]:
                main = False
            head = _codex_head(raw)
            if head is None:
                continue
            kind, sub = head
            if kind == "event_msg" and sub == "task_started":
                close_turn()
                turn_active = False
                pending_prompt = True
                previous_generated = False
                continue
            if kind == "compacted":
                cycles.append([points, sampling, False, reported, main, current()])
                turn_compactions.append(len(cycles) - 1)
                points = []
                total, after = None, 0
                continue
            if kind == "event_msg" and sub == "token_count":
                try:
                    info = json.loads(raw)["payload"].get("info")
                    value = info["last_token_usage"]["total_tokens"]
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if _nonnegative_int(value):
                    total = value
                    window_value = info.get("model_context_window")
                    reported = window_value if _nonnegative_int(window_value) else reported
                continue
            if kind != "response_item":
                continue
            if pending_prompt:
                pending_prompt = False
                points.append((current(), sampling))
            oversized = len(raw) > line_max
            if sub == "message":
                match = _CODEX_ROLE.search(raw[:_CODEX_ROLE_WINDOW_BYTES])
                generated = match is not None and match.group(1) == b"assistant"
                if match is not None and match.group(1) == b"user":
                    turn_active = True
            else:
                generated = sub in memspec.CONTEXT_METER_CODEX_MODEL_ITEM_TYPES
            if generated:
                turn_active = True
                if not previous_generated:
                    sampling += 1
                previous_generated = True
                after = 0
                if sub in ("function_call", "custom_tool_call"):
                    name = _CODEX_NAME.search(raw[:_CODEX_ROLE_WINDOW_BYTES])
                    if name is None or name.group(1).decode("utf-8", "replace") not in skip_tools:
                        points.append((current(), sampling))
                continue
            previous_generated = False
            if after is None:
                continue
            if oversized:
                after = None
                continue
            try:
                tokens = _codex_item_tokens(json.loads(raw)["payload"])
            except (ValueError, KeyError, TypeError):
                continue
            after = None if tokens is None else after + tokens
    close_turn()
    cycles.append([points, sampling, None, reported, main, None])  # 檔尾：這個週期沒有壓縮
    kept = [cycle for cycle in cycles if cycle[3] == window]
    return kept, sum(1 for cycle in kept if cycle[2] is True)


def _quantile(values, share):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))] if ordered else None


def codex_calibrate(root, since, limit, window, headrooms, out=print):
    """唯讀重播 `<root>/**/rollout-*.jsonl`（修改時間在 since 之後）。什麼都不寫。

    對每個候選 H：主線自動壓縮裡，第一個用量 ≥ limit−H 的 hook 點之後、壓縮之前至少還有
    一次模型取樣（看得到提醒、還能動手寫交接）的比例；提早的取樣次數中位數；沒有壓縮
    的週期（場次結束）裡白提醒的次數。另列「用量已達壓縮點」的那一部分：壓縮那一刻照
    Codex 算法算出的用量 ≥ limit 的，才是這個門檻造成的壓縮——也拿來驗算法對不對。
    回 [(H, 命中, 自動壓縮數, 門檻型命中, 門檻型數, 中位數, 白提醒)]。"""
    files = [path for path in sorted(Path(root).rglob("rollout-*.jsonl"))
             if path.stat().st_mtime >= since]
    cycles = []
    for path in files:
        try:
            found, _count = _codex_replay_file(path, window)
        except OSError:
            continue
        cycles.extend(found)
    # 子代理的呼叫一律不提醒（notice 見 agent_id 就不說），所以 H 只看主線。
    auto = [cycle for cycle in cycles if cycle[2] is True and cycle[4]]
    driven = [cycle for cycle in auto if cycle[5] is not None and cycle[5] >= limit]
    driven_ids = {id(cycle) for cycle in driven}
    open_cycles = [cycle for cycle in cycles if cycle[2] is None and cycle[4]]
    pre = [cycle[5] - limit for cycle in auto if cycle[5] is not None]

    def first_warning(points, headroom):
        return next((at for value, at in points if value is not None and value >= limit - headroom), None)

    rows = []
    for headroom in headrooms:
        hits = driven_hits = 0
        warned = []
        for cycle in auto:
            points, end = cycle[0], cycle[1]
            first = first_warning(points, headroom)
            if first is not None and end - first >= 1:
                hits += 1
                if id(cycle) in driven_ids:
                    driven_hits += 1
                    warned.append(end - first)
        wasted = sum(1 for cycle in open_cycles if first_warning(cycle[0], headroom) is not None)
        median = _median(warned) if warned else 0
        rows.append((headroom, hits, len(auto), driven_hits, len(driven), median, wasted))
    out(f"codex-calibrate: files={len(files)} cycles={len(cycles)} main_auto={len(auto)} "
        f"main_auto_at_limit={len(driven)} subagent_auto="
        f"{sum(1 for cycle in cycles if cycle[2] is True and not cycle[4])} "
        f"manual={sum(1 for cycle in cycles if cycle[2] is False)} main_open={len(open_cycles)} "
        f"limit={limit} hard_cap={window}")
    if pre:
        out(f"count at compaction minus limit (main auto, known {len(pre)}/{len(auto)}): "
            f"p5={_quantile(pre, 0.05)} p50={_quantile(pre, 0.5)} p95={_quantile(pre, 0.95)} "
            f"min={min(pre)} max={max(pre)}")
    out("H\thit(all auto)\trate\thit(at limit)\trate\tmedian_steps(at limit)\tfired_without_compaction")
    for headroom, hits, count, driven_hits, driven_count, median, wasted in rows:
        rate = hits / count if count else 0
        driven_rate = driven_hits / driven_count if driven_count else 0
        out(f"{headroom}\t{hits}/{count}\t{rate:.1%}\t{driven_hits}/{driven_count}\t{driven_rate:.1%}"
            f"\t{median:g}\t{wasted}/{len(open_cycles)}")
    return rows


# ---------------------------------------------------------------- 自測


def _selftest():
    import contextlib
    import io
    import subprocess
    import tempfile
    import time
    from datetime import datetime, timedelta, timezone

    import builtins
    import types

    checks = []
    # 讀法與判斷的題目一律用停住的時鐘：時間上限是 150 ms 的真時鐘，機器一忙，讀到一半就
    # 「逾時回 None」，題目就跟著機器負載時好時壞（2026-09-25 併行實測 22/25）。
    # 「hook 只讀檔尾、預設期限內拿得到用量、過了期限就停」不量牆鐘，改用兩個替身驗：
    # 計數檔（讀了幾次、幾位元組）與每讀一次就前進的時鐘——後者換掉的是模組的
    # time.monotonic，走的是沒注入時鐘、沒給 time_limit 的正式預設路徑。

    def frozen_clock():
        return 0.0

    class CountingFile:
        """包住真的檔：記下 read 了幾次、拿到幾位元組。"""

        def __init__(self, raw, meter):
            self._raw, self._meter = raw, meter

        def __enter__(self):
            return self

        def __exit__(self, *_exception):
            self._raw.close()

        def seek(self, *args):
            return self._raw.seek(*args)

        def tell(self):
            return self._raw.tell()

        def read(self, size=-1):
            data = self._raw.read(size)
            self._meter["reads"] += 1
            self._meter["bytes"] += len(data)
            return data

    def metered(call, step=None):
        """在計數檔底下跑 call()，回 (結果, 讀了幾次, 讀了幾位元組)。

        step 不是 None：模組的 time.monotonic 換成「每讀一次前進 step 秒」，call 走的是
        沒注入時鐘的預設路徑；step=0.1 時期限（0.15 s）在第二塊讀完後跨過，不是第一塊之前。"""
        meter = {"reads": 0, "bytes": 0}
        module = globals()
        saved_time = module["time"]
        module["open"] = lambda path, mode="r", *rest, **named: CountingFile(
            builtins.open(path, mode, *rest, **named), meter)
        if step is not None:
            module["time"] = types.SimpleNamespace(monotonic=lambda: meter["reads"] * step)
        try:
            value = call()
        finally:
            module.pop("open", None)
            module["time"] = saved_time
        return value, meter["reads"], meter["bytes"]

    crossing_step = 0.1  # 兩塊之後跨過 0.15 s
    roomy_step = 0.001  # 150 次讀取之內都不逾時
    first_block = memspec.CONTEXT_METER_TAIL_FIRST_BYTES
    max_block = memspec.CONTEXT_METER_TAIL_MAX_BLOCK_BYTES

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

    saved_config_env = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-context-meter-") as temp_dir:
            root = Path(temp_dir).resolve()
            # 追蹤檔落在設定檔旁：自測一律指到暫存根，絕不寫進真的 ~/.epitype。
            os.environ[memspec.EPITYPE_CONFIG_ENV] = os.fspath(root / "selftest-config.json")
            transcript = root / "t.jsonl"

            # 1. 跨塊：最後一列是遠大於首塊的工具結果，用量在它前面。
            write(transcript, [assistant(1000), assistant(123456), tool_result(300 * 1024)])
            checks.append(("usage found across blocks behind a large tool result",
                           current_tokens(transcript, clock=frozen_clock) == 123456))
            # 用量那一列自己橫跨塊邊界（整列比首塊大）。
            big_assistant = json.loads(assistant(77777))
            big_assistant["message"]["content"] = "y" * (200 * 1024)
            write(transcript, [assistant(1000), json.dumps(big_assistant), tool_result(10)],
                  trailing_newline=False)
            straddle = current_tokens(transcript, clock=frozen_clock)

            # 2. sidechain／meta／compact summary 不算；加總為 0 的合成列不算。
            write(transcript, [assistant(50000), assistant(90000, isSidechain=True),
                               assistant(91000, isMeta=True), assistant(92000, isCompactSummary=True),
                               json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 0}}})])
            checks.append(("sidechain, meta, compact-summary and zero rows are skipped; a row "
                           "straddling the block edge is still read",
                           current_tokens(transcript, clock=frozen_clock) == 50000 and straddle == 77777))

            # 3. 檔不存在、沒有用量、壓縮邊界之後還沒有新用量：都回 None。
            write(transcript, [assistant(150000), json.dumps(
                {"type": "system", "subtype": "compact_boundary",
                 "compactMetadata": {"trigger": "auto", "preTokens": 150000}}),
                json.dumps({"type": "user", "isCompactSummary": True, "message": {"content": "s"}})])
            after_boundary = current_tokens(transcript, clock=frozen_clock)
            write(transcript, [tool_result(10)])
            checks.append(("missing file, no usage, and no usage since the compact boundary give None",
                           current_tokens(root / "absent.jsonl", clock=frozen_clock) is None
                           and current_tokens(transcript, clock=frozen_clock) is None
                           and after_boundary is None
                           and current_tokens(None, clock=frozen_clock) is None))

            # 3b. 最後一行是 9 MiB 的工具結果（超過單行上限、也超過舊的 8 MiB 總量）：丟掉
            # 那一行的片段、繼續往前找，前一行的用量照樣讀得到。前面墊 4 MB：讀的位元組不超過
            # 「用量那一列到檔尾」再多一塊，絕不是整檔；預設路徑拿得到，期限在讀到一半跨過就停。
            huge = root / "huge.jsonl"
            huge_tail = [assistant(345678), tool_result(9 * 1024 * 1024)]
            write(huge, [assistant(1000)] + [tool_result(4000)] * 1000 + huge_tail)
            huge_tail_bytes = len(("\n".join(huge_tail) + "\n").encode("utf-8"))
            huge_size = huge.stat().st_size
            huge_value, _reads, huge_bytes = metered(lambda: current_tokens(huge, clock=frozen_clock))
            huge_default, _reads, _bytes = metered(lambda: current_tokens(huge), roomy_step)
            huge_cut, cut_reads, cut_bytes = metered(lambda: current_tokens(huge), crossing_step)
            checks.append(("a 9 MiB tool-result last line is skipped whole and the usage before it is read "
                           "on the default path, reading only the tail; the default deadline crossed "
                           "mid-read gives None",
                           huge_value == 345678 and huge_default == 345678
                           and huge_bytes <= huge_tail_bytes + max_block
                           and huge_bytes < huge_size and huge_size - huge_tail_bytes > 4 * 1000 * 1000
                           and huge_cut is None and cut_reads == 2 and cut_bytes == 3 * first_block))

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
                    len(encoded) > memspec.CONTEXT_METER_LINE_MAX_BYTES and current_tokens(oversized, clock=frozen_clock) is None)
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
            unknown = notice(event, empty_vault, markers, options={}, environ=env92, clock=frozen_clock)
            disabled = notice(event, vault, markers, environ=env92, clock=frozen_clock,
                              options={"context_meter": {"enabled": False, "autocompact_tokens": 100000}})
            garbage = notice(event, vault, markers, environ=env92, clock=frozen_clock,
                             options={"context_meter": {"enabled": "no", "autocompact_tokens": 100000}})
            checks.append(("unknown threshold, enabled=false and a garbage enabled value say nothing",
                           unknown is None and disabled is None and garbage is None
                           and not markers.exists()))

            # 6. 只有一段、在 0.97T：低於不發；跨過發一次；同一個壓縮週期不重發。
            options = {"context_meter": {"autocompact_tokens": 100000}}

            def call(total, directory=markers, extra=None):
                write(transcript, [assistant(total)])
                found = notice({**event, **(extra or {})}, vault, directory, options=options, environ=env92,
                               clock=frozen_clock)
                if found is not None:
                    claim(directory, found[1], found[2])
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
            scaled_line = notice({**event, "session_id": "s2"}, vault, root / "scaled", clock=frozen_clock,
                                 options=scaled_options, environ={memspec.CONTEXT_METER_PCT_ENV: "46"})
            checks.append(("cleared markers re-arm the reminder; a scaled threshold is labelled",
                           rearmed is not None and rearmed[1] == memspec.CONTEXT_METER_MARKER
                           and scaled_line is not None
                           and memspec.CONTEXT_METER_SCALED_MARK in scaled_line[0]
                           and memspec.CONTEXT_METER_SCALED_MARK not in first[0]))

            # 8b. 並行：幾個行程在任何一個搶標記之前都看到「還沒說過」（Codex 並行的工具
            # 呼叫），但獨占建立只讓一個搶到；搶到卻沒送出去的放掉，下一次再說；別的行程
            # 寫的標記 release 不動。
            race = root / "claim-race"
            write(transcript, [assistant(98000)])
            seen = [notice(event, vault, race, options=options, environ=env92, clock=frozen_clock)
                    for _ in range(5)]
            wins = [claim(race, found[1], found[2]) for found in seen if found is not None]
            race_marker = race / memspec.CONTEXT_METER_MARKER
            stored_race = _marker_tokens(race_marker)
            after_claim = notice(event, vault, race, options=options, environ=env92, clock=frozen_clock)
            released = release(race, memspec.CONTEXT_METER_MARKER)
            retried = notice(event, vault, race, options=options, environ=env92, clock=frozen_clock)
            race_marker.write_text(json.dumps({"tokens": 98000, "pid": os.getpid() + 1}) + "\n",
                                   encoding="ascii")
            foreign = release(race, memspec.CONTEXT_METER_MARKER)
            checks.append(("concurrent notices all see the line but exactly one claim wins; a released claim "
                           "is retried; another process's marker is not released",
                           len(seen) == 5 and all(found is not None for found in seen)
                           and wins.count(True) == 1 and len(wins) == 5 and stored_race == 98000
                           and after_claim is None and released is True and retried is not None
                           and foreign is False and race_marker.is_file()))

            broken_claim_dir = root / "claim-broken"
            real_getpid = os.getpid
            os.getpid = lambda: object()  # 內容寫不出去（JSON 序列化失敗）
            try:
                broken_claim = claim(broken_claim_dir, memspec.CONTEXT_METER_MARKER, 98000)
            finally:
                os.getpid = real_getpid
            checks.append(("a claim whose content cannot be written removes its empty marker",
                           broken_claim is False
                           and not (broken_claim_dir / memspec.CONTEXT_METER_MARKER).exists()))

            # 8c. PreCompact 沒清到標記（Claude）：跌 ≥40% 才重新武裝、並記一行追蹤；舊格式
            # 標記（沒有用量）不自己武裝，照舊等 PreCompact。
            stale = root / "stale"
            stale_first = call(97500, stale)
            stale_dip = call(60000, stale)  # 60000 > 0.6×97500
            stale_dip_kept = (stale / memspec.CONTEXT_METER_MARKER).is_file()
            stale_back = call(97600, stale)
            stale_drop = call(58000, stale)  # ≤ 0.6×97500
            stale_cleared = not (stale / memspec.CONTEXT_METER_MARKER).exists()
            stale_again = call(97500, stale)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / memspec.CONTEXT_METER_MARKER).write_text(memspec.CONTEXT_METER_MARKER + "\n",
                                                                encoding="ascii")
            call(10000, legacy)
            legacy_kept = (legacy / memspec.CONTEXT_METER_MARKER).is_file() and call(98000, legacy) is None
            trace_file = root / memspec.CONTEXT_METER_TRACE_FILENAME
            rearm_rows = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()
                          if '"meter-rearmed"' in line] if trace_file.is_file() else []
            checks.append(("Claude: a marker PreCompact left behind re-arms after a >=40% drop (traced), "
                           "not after a smaller dip; a legacy marker without usage waits for PreCompact",
                           stale_first is not None and stale_dip is None and stale_dip_kept
                           and stale_back is None and stale_drop is None and stale_cleared
                           and stale_again is not None and legacy_kept
                           and any(row.get("stored") == 97500 and row.get("tokens") == 58000
                                   and row.get("session") == "s1" for row in rearm_rows)))

            # 8d. 唯讀 CLI：印檔在哪、開到哪一天，再印最後 N 行。
            printed = []
            before_cli = trace_file.read_bytes() if trace_file.is_file() else b""
            cli_status = trace_command(1, trace_file, out=printed.append)
            checks.append(("`context-meter trace --last N` prints where the trace is and its last N lines, read-only",
                           cli_status == 0 and len(printed) == 2
                           and printed[0].startswith(f"# trace {trace_file} (")
                           and json.loads(printed[1]).get("outcome") == "meter-rearmed"
                           and trace_file.read_bytes() == before_cli))

            # 9. 學習：record_autocompact 追加一筆、壞檔重建、只留 10 筆。
            state.write_text("{broken", encoding="utf-8")
            write(transcript, [assistant(150000)])
            rebuilt = record_autocompact(vault, transcript, environ=env92, clock=frozen_clock)
            for _ in range(12):
                record_autocompact(vault, transcript, environ=env92, clock=frozen_clock)
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
                " environ={'" + memspec.CONTEXT_METER_PCT_ENV + "': '92'}, clock=lambda: 0.0)\n"
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

            # ---- Codex ----
            import itertools

            ordinals = itertools.count(1)

            def codex_row(kind, payload):
                return json.dumps({"timestamp": "2026-09-25T00:00:00.000Z", "ordinal": next(ordinals),
                                   "type": kind, "payload": payload}, ensure_ascii=False, separators=(",", ":"))

            def token_count(total, window=228000):
                return codex_row("event_msg", {"type": "token_count", "info": {
                    "total_token_usage": {"total_tokens": 9_999_999},
                    "last_token_usage": {"input_tokens": total - 100, "output_tokens": 100, "total_tokens": total},
                    "model_context_window": window}})

            def call(name="exec_command"):
                return codex_row("response_item", {"type": "function_call", "name": name,
                                                   "arguments": "{}", "call_id": "c1"})

            def output(body):
                return codex_row("response_item", {"type": "function_call_output", "call_id": "c1", "output": body})

            def message(role, text):
                return codex_row("response_item", {"type": "message", "role": role,
                                                   "content": [{"type": "input_text", "text": text}]})

            reasoning = codex_row("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "e"})
            compacted = codex_row("compacted", {"message": "", "replacement_history": []})
            meta = codex_row("session_meta", {"id": "cx", "source": "vscode"})
            rollout = root / "rollout-2026-09-25T00-00-00-cx.jsonl"

            # C1. 認得出 Codex rollout：用量＝最新 token_count＋最後一個模型產出項之後各項的估計
            # （位元組 ÷4 無條件進位）；正在寫的最後一行不算；Claude 的讀法對它回 None。
            write(rollout, [meta, codex_row("turn_context", {"model": "old"}), message("user", "hi"),
                            reasoning, call(), token_count(150000), output("x" * 4000),
                            '{"timestamp":"t","ordinal":99,"type":"response_item","payload":{"type":"function_c'],
                  trailing_newline=False)
            mid_turn = measure(rollout, clock=frozen_clock)
            write(rollout, [meta, reasoning, call(), token_count(150000), message("assistant", "done"),
                            codex_row("turn_context", {"model": "gpt-small"}), message("user", "next")])
            new_turn = measure(rollout, clock=frozen_clock)
            checks.append(("a Codex rollout is read with Codex's own count; a row being written is ignored; "
                           "the Claude-only reader returns None for it",
                           mid_turn is not None and mid_turn.host == memspec.CONTEXT_METER_HOST_CODEX
                           and mid_turn.tokens == 150000 + 1001 and mid_turn.window == 228000
                           and mid_turn.model is None
                           and new_turn is not None and new_turn.tokens == 150001
                           and new_turn.model == "gpt-small"
                           and current_tokens(rollout, clock=frozen_clock) is None))

            # C2. 模型可見位元組：圖片 7,373（原尺寸取上限 40,000）、文字照 UTF-8、音訊估不出來。
            image = {"type": "function_call_output", "call_id": "ab", "output": [
                {"type": "input_text", "text": "abcd"}, {"type": "input_image", "image_url": "data:x"}]}
            original = {"type": "function_call_output", "call_id": "ab", "output": [
                {"type": "input_text", "text": "abcd"},
                {"type": "input_image", "image_url": "data:x", "detail": "original"}]}
            audio = {"type": "message", "role": "user", "content": [{"type": "input_audio", "audio_url": "a"}]}
            checks.append(("Codex item estimates follow its byte rules and give None for an unknown part",
                           _codex_item_tokens(image) == 1845
                           and _codex_item_tokens(original) == 10002
                           and _codex_item_tokens({"type": "message", "role": "developer",
                                                   "content": [{"type": "input_text", "text": "中文"}]}) == 2
                           and _codex_item_tokens(audio) is None
                           and _codex_item_tokens({"type": "ghost_snapshot"}) == 0))

            # C3. 壓縮邊界比最新 token_count 新＝不知道；壓縮後重算的 token_count 之後只算邊界之後
            # 的項；info 是 null 的 token_count 略過。
            write(rollout, [meta, token_count(200000), call(), compacted])
            just_compacted = measure(rollout, clock=frozen_clock)
            write(rollout, [meta, token_count(200000), call(), compacted, token_count(30000),
                            message("user", "hello world!")])
            after_compaction = measure(rollout, clock=frozen_clock)
            write(rollout, [meta, token_count(120000), call(),
                            codex_row("event_msg", {"type": "token_count", "info": None})])
            null_info = measure(rollout, clock=frozen_clock)
            checks.append(("a compacted row newer than the latest token_count gives None; after it only the "
                           "items past the boundary count; a null-info token_count is skipped",
                           just_compacted is None
                           and after_compaction is not None and after_compaction.tokens == 30003
                           and null_info is not None and null_info.tokens == 120000))

            # C4. 超過單行上限的 Codex 列只看行首：模型產出項照樣當錨、工具結果估不出來＝不知道、
            # 壓縮邊界照樣是邊界、事件列略過。
            # 行尾累積過單行上限時還沒碰到換行，才算「超過單行上限」：3 MiB 一行才走得到那條路。
            huge = "y" * (3 * memspec.CONTEXT_METER_LINE_MAX_BYTES)
            oversized_cases = []
            for last, expected in (
                    (codex_row("response_item", {"type": "custom_tool_call", "name": "apply_patch",
                                                 "call_id": "c1", "input": huge}), 100000),
                    (output(huge), None),
                    (codex_row("compacted", {"message": "", "replacement_history": [huge]}), None),
                    (codex_row("event_msg", {"type": "item_completed", "item": huge}), 100000)):
                # 前面墊 2 MB：讀到檔頭時整行都在手上，就不是「超過單行上限」的情形。
                padding = [output("p" * 2000)] * 1100
                rows = [meta, *padding, token_count(100000), call(), last]
                if expected == 100000 and "custom_tool_call" in last:
                    rows = [meta, *padding, token_count(100000), last]
                write(rollout, rows)
                found = measure(rollout, clock=frozen_clock)
                oversized_cases.append((found.tokens if found is not None else None) == expected)
            checks.append(("oversized Codex rows are judged by their head: model item anchors, tool output is "
                           "unknown, compacted is a boundary, events are skipped", oversized_cases == [True] * 4))

            # C5. 壓縮點照 Codex：設定的視窗夾在目錄上限內，min(自動門檻, 視窗×9/10, 硬上限)；
            # 範圍、token_budget、profile、找不到模型、壞設定都回 None；CODEX_HOME 優先於家目錄。
            catalog = {"models": [
                {"slug": "gpt-x", "context_window": 272000, "max_context_window": 872000,
                 "auto_compact_token_limit": None, "effective_context_window_percent": 95},
                {"slug": "gpt-small", "context_window": 128000, "max_context_window": 128000,
                 "auto_compact_token_limit": None, "effective_context_window_percent": 95},
                {"slug": "gpt-low", "context_window": 240000, "max_context_window": 240000,
                 "auto_compact_token_limit": None, "effective_context_window_percent": 80},
                {"slug": "gpt-auto", "context_window": 272000, "max_context_window": 272000,
                 "auto_compact_token_limit": 150000, "effective_context_window_percent": 95}]}
            pinned = 'model = "gpt-x"\nmodel_context_window = 240000\nmodel_auto_compact_token_limit = 210000\n'

            def codex_dir(name, config_text):
                directory = root / "codex-homes" / name
                directory.mkdir(parents=True)
                (directory / memspec.CONTEXT_METER_CODEX_CONFIG_FILENAME).write_text(config_text, encoding="utf-8")
                (directory / memspec.CONTEXT_METER_CODEX_CATALOG_FILENAME).write_text(
                    json.dumps(catalog), encoding="utf-8")
                return {memspec.CONTEXT_METER_CODEX_HOME_ENV: os.fspath(directory)}

            base_env = codex_dir("base", pinned)
            fallback_home = root / "codex-user"
            (fallback_home / ".codex").mkdir(parents=True)
            (fallback_home / ".codex" / memspec.CONTEXT_METER_CODEX_CONFIG_FILENAME).write_text(
                'model = "gpt-small"\n', encoding="utf-8")
            (fallback_home / ".codex" / memspec.CONTEXT_METER_CODEX_CATALOG_FILENAME).write_text(
                json.dumps(catalog), encoding="utf-8")
            limits = {
                "pinned": codex_limit(None, base_env),
                "event model": codex_limit("gpt-small", base_env),
                "catalog only": codex_limit(None, codex_dir("plain", 'model = "gpt-x"\n')),
                "clamped": codex_limit(None, codex_dir("clamp", 'model = "gpt-x"\nmodel_context_window = 1000000\n')),
                "percent": codex_limit("gpt-low", codex_dir("low", 'model = "gpt-x"\n')),
                "catalog auto": codex_limit("gpt-auto", codex_dir("auto", 'model = "gpt-x"\n')),
                "budget off": codex_limit(None, codex_dir("budget-off", pinned + "[features.token_budget]\nenabled = false\n")),
                "home": codex_limit(None, {}, fallback_home),
            }
            unknown_limits = [
                codex_limit(None, codex_dir("scope", pinned + 'model_auto_compact_token_limit_scope = "body_after_prefix"\n')),
                codex_limit(None, codex_dir("budget", pinned + "[features]\ntoken_budget = true\n")),
                codex_limit(None, codex_dir("profile", 'profile = "fast"\n' + pinned)),
                codex_limit("no-such-model", base_env),
                codex_limit(None, codex_dir("broken", "model = \n")),
                codex_limit(None, {memspec.CONTEXT_METER_CODEX_HOME_ENV: os.fspath(root / "no-codex-here")}),
            ]
            checks.append(("codex_limit follows Codex's window and auto-compact rules and is None outside them",
                           limits == {"pinned": 210000, "event model": 115200, "catalog only": 244800,
                                      "clamped": 784800, "percent": 192000, "catalog auto": 150000,
                                      "budget off": 210000,
                                      "home": 115200}
                           and unknown_limits == [None] * 6))

            # C6. Codex 的提醒：壓縮點－H 以下不說；跨過說一次（Codex 的字、交接檔路徑）、同一個
            # 週期不再說；Claude 的全域覆寫不套上 Codex；hook 輸入的 model 優先於設定。
            headroom = memspec.CONTEXT_METER_CODEX_HEADROOM_TOKENS
            codex_markers = root / "codex-markers"
            claude_override = {"context_meter": {"autocompact_tokens": 100000}}

            def codex_call(total, session="cx1", model="gpt-x", directory=codex_markers, **extra):
                write(rollout, [meta, reasoning, call(), token_count(total)])
                found = notice({"session_id": session, "transcript_path": os.fspath(rollout),
                                "model": model, **extra}, vault, directory, options=claude_override,
                               environ=base_env, clock=frozen_clock)
                if found is not None:
                    claim(directory, found[1], found[2])
                return found

            below = codex_call(210000 - headroom - 1)
            override_ignored = codex_call(120000)
            due = codex_call(210000 - headroom + 500)
            repeat = codex_call(209000)
            small = codex_call(115200 - headroom, session="cx2", directory=root / "codex-markers-2",
                               model="gpt-small")
            by_subagent = codex_call(200000, session="cx3", directory=root / "codex-markers-3", agent_id="a1")
            codex_handoff = _compact_map.handoff_destination(vault, "cx1", rollout)
            checks.append(("the Codex reminder fires once at limit-H with Codex's text and the handoff path, "
                           "ignores the Claude override, prefers the event's model, and skips subagents",
                           below is None and override_ignored is None
                           and due is not None and due[1] == memspec.CONTEXT_METER_MARKER
                           and due[0] == memspec.CONTEXT_METER_CODEX_NOTICE.format(
                               cur=_k(210000 - headroom + 500), left=_k(headroom - 500),
                               path=os.fspath(codex_handoff))
                           and repeat is None
                           and small is not None and os.fspath(
                               _compact_map.handoff_destination(vault, "cx2", rollout)) in small[0]
                           and by_subagent is None
                           and not (root / "codex-markers-3").exists()))

            # C6b. PreCompact 沒清到標記：壓縮後用量跌到標記記的 60% 以下就重新武裝；週期內
            # 的起伏（跌不到 40%）不算。
            codex_stale = root / "codex-stale"
            codex_point = 210000 - headroom + 500
            codex_claimed = codex_call(codex_point, directory=codex_stale)
            codex_dip = codex_call(int(codex_point * 0.61), directory=codex_stale)
            codex_dip_kept = (codex_stale / memspec.CONTEXT_METER_MARKER).is_file()
            codex_back = codex_call(209000, directory=codex_stale)
            codex_low = codex_call(28000, directory=codex_stale)
            codex_low_cleared = not (codex_stale / memspec.CONTEXT_METER_MARKER).exists()
            codex_rearmed = codex_call(codex_point, directory=codex_stale)
            checks.append(("Codex: a marker PreCompact left behind re-arms after a >=40% drop, "
                           "not after a smaller dip",
                           codex_claimed is not None and codex_claimed[2] == codex_point
                           and codex_dip is None and codex_dip_kept and codex_back is None
                           and codex_low is None and codex_low_cleared
                           and codex_rearmed is not None and codex_rearmed[0] == codex_claimed[0]))

            # C7. 壓縮點記在標記目錄：設定沒變就沿用，設定一變就重算。
            cache_dir = root / "codex-cache"
            first_limit = _codex_limit_cached("gpt-x", cache_dir, base_env)
            cache_file = cache_dir / memspec.CONTEXT_METER_CODEX_LIMIT_CACHE
            stored = json.loads(cache_file.read_text(encoding="utf-8"))
            cache_file.write_text(json.dumps({"key": stored["key"], "limit": 123}), encoding="utf-8")
            reused = _codex_limit_cached("gpt-x", cache_dir, base_env)
            config_file = Path(base_env[memspec.CONTEXT_METER_CODEX_HOME_ENV]) / memspec.CONTEXT_METER_CODEX_CONFIG_FILENAME
            config_file.write_text(pinned.replace("210000", "200000") + "# changed\n", encoding="utf-8")
            recomputed = _codex_limit_cached("gpt-x", cache_dir, base_env)
            other_model = _codex_limit_cached("gpt-small", cache_dir, base_env)
            config_file.write_text(pinned, encoding="utf-8")
            checks.append(("the Codex limit is cached per session and recomputed when the config changes",
                           first_limit == 210000 and reused == 123 and recomputed == 200000
                           and other_model == 115200))

            # C8. PreCompact 的學習只收 Claude：Codex rollout 一筆都不寫。
            learn_vault = root / "learn-vault"
            learn_vault.mkdir()
            write(rollout, [meta, reasoning, call(), token_count(205000)])
            checks.append(("record_autocompact learns nothing from a Codex rollout",
                           record_autocompact(learn_vault, rollout, environ=env92, clock=frozen_clock) is None
                           and not state_path(learn_vault).exists()))

            # C9. 重播：第一個用量 ≥ 壓縮點－H 的 hook 點之後還要有一次取樣才算來得及。
            replay_root = root / "replay" / "2026" / "09" / "25"
            replay_root.mkdir(parents=True)
            write(replay_root / "rollout-2026-09-25T00-00-00-r1.jsonl", [
                meta, codex_row("event_msg", {"type": "task_started"}), message("user", "go"),
                reasoning, call(), token_count(150000), output("o"),
                reasoning, call(), token_count(170000), output("o"),
                reasoning, call(), token_count(205000), output("o"),
                compacted, token_count(20000), reasoning, call(), token_count(40000)])
            replay_lines = []
            replay_rows = codex_calibrate(root / "replay", 0, 210000, 228000, [45000, 60000],
                                          out=replay_lines.append)
            checks.append(("the Codex replay counts a hit only when a sampling follows the first point at "
                           "limit-H, and reports without writing",
                           [row[:3] for row in replay_rows] == [(45000, 0, 1), (60000, 1, 1)]
                           and "main_auto=1" in replay_lines[0]
                           and sorted(item.name for item in replay_root.iterdir())
                           == ["rollout-2026-09-25T00-00-00-r1.jsonl"]))

            # C10. 50 MB rollout、最後一列是 2 MB 的事件列：hook 只讀檔尾（讀的位元組不超過「最後
            # 一個模型產出項到檔尾」再多一塊）；預設路徑拿得到用量，期限在讀到一半跨過就停。
            large_rollout = root / "rollout-large.jsonl"
            filler_row = (output("f" * 2000) + "\n").encode("utf-8")
            rollout_tail = [call(), token_count(180000),
                            codex_row("event_msg", {"type": "item_completed", "item": "i" * (2 * 1024 * 1024)})]
            with large_rollout.open("wb") as stream:
                stream.write((meta + "\n").encode("utf-8"))
                stream.write(filler_row * (50 * 1024 * 1024 // len(filler_row)))
                stream.write((reasoning + "\n").encode("utf-8"))
                for row in rollout_tail:
                    stream.write((row + "\n").encode("utf-8"))
            rollout_tail_bytes = len(("\n".join(rollout_tail) + "\n").encode("utf-8"))
            rollout_size = large_rollout.stat().st_size
            started = time.perf_counter()
            large_reading, _reads, rollout_bytes = metered(lambda: measure(large_rollout, clock=frozen_clock))
            print(f"context_meter: measure on a {rollout_size / 1e6:.1f} MB Codex rollout read "
                  f"{rollout_bytes} bytes in {(time.perf_counter() - started) * 1000:.1f} ms (not asserted)")
            rollout_default, _reads, _bytes = metered(lambda: measure(large_rollout), roomy_step)
            rollout_cut, cut_reads, cut_bytes = metered(lambda: measure(large_rollout), crossing_step)
            checks.append(("measure on a 50 MB Codex rollout ending in a 2 MB event row reads only the tail and "
                           "answers on the default path; the default deadline crossed mid-read gives None",
                           large_reading is not None and large_reading.tokens == 180000
                           and rollout_default is not None and rollout_default.tokens == 180000
                           and rollout_size >= 50 * 1000 * 1000
                           and rollout_bytes <= rollout_tail_bytes + max_block and rollout_bytes < rollout_size
                           and rollout_cut is None and cut_reads == 2 and cut_bytes == 3 * first_block))

            # C10b. 同一條預設路徑走 notice（Codex）：期限在讀到一半跨過就不說、不寫標記；
            # 沒跨過就說那一行。
            codex_event = {"session_id": "dp-codex", "transcript_path": os.fspath(large_rollout), "model": "gpt-x"}
            codex_cut_dir, codex_ok_dir = root / "dp-codex-cut", root / "dp-codex-ok"
            codex_cut, _reads, _bytes = metered(
                lambda: notice(codex_event, vault, codex_cut_dir, options={}, environ=base_env), crossing_step)
            codex_ok, _reads, _bytes = metered(
                lambda: notice(codex_event, vault, codex_ok_dir, options={}, environ=base_env), roomy_step)
            checks.append(("notice for Codex on the default path says nothing when the deadline is crossed "
                           "mid-read and says its line when it is not",
                           codex_cut is None
                           and not (codex_cut_dir / memspec.CONTEXT_METER_MARKER).exists()
                           and codex_ok is not None
                           and codex_ok[0] == memspec.CONTEXT_METER_CODEX_NOTICE.format(
                               cur=180, left=30,
                               path=os.fspath(_compact_map.handoff_destination(vault, "dp-codex", large_rollout)))))

            # 11. 效能：50 MB transcript、最後一列是 2 MB 工具結果，hook 只讀檔尾。
            large = root / "large.jsonl"
            filler = (assistant(1234) + "\n").encode("utf-8") * 1
            filler_block = filler * max(1, (1024 * 1024) // len(filler))
            with large.open("wb") as stream:
                for _ in range(48):
                    stream.write(filler_block)
                stream.write((assistant(222222) + "\n").encode("utf-8"))
                stream.write((tool_result(2 * 1024 * 1024) + "\n").encode("utf-8"))
            large_tail_bytes = len((assistant(222222) + "\n" + tool_result(2 * 1024 * 1024) + "\n").encode("utf-8"))
            large_size = large.stat().st_size
            started = time.perf_counter()
            measured, _reads, large_bytes = metered(lambda: current_tokens(large, clock=frozen_clock))
            print(f"context_meter: current_tokens on a {large_size / 1e6:.1f} MB transcript read "
                  f"{large_bytes} bytes in {(time.perf_counter() - started) * 1000:.1f} ms (not asserted)")
            # 預設路徑：沒注入時鐘、沒給 time_limit。期限沒跨過就拿得到用量；每讀一次前進 0.1 s
            # 時，讀完第二塊就跨過 0.15 s，停下來回 None——讀了剛好兩塊，證明期限是 0.15 s、
            # 是在讀到一半時跨過，不是在第一塊之前。
            default_value, _reads, _bytes = metered(lambda: current_tokens(large), roomy_step)
            cut_value, cut_reads, cut_bytes = metered(lambda: current_tokens(large), crossing_step)
            checks.append(("current_tokens on a 50 MB transcript ending in a 2 MB tool result reads only the tail "
                           "and answers on the default path with the 0.15 s limit; the default deadline crossed "
                           "mid-read gives None",
                           measured == 222222 and default_value == 222222
                           and large_size >= 50 * 1000 * 1000
                           and large_bytes <= large_tail_bytes + max_block and large_bytes < large_size
                           and large_bytes <= memspec.CONTEXT_METER_TAIL_MAX_BYTES
                           and memspec.CONTEXT_METER_TAIL_SECONDS == 0.15
                           and cut_value is None and cut_reads == 2 and cut_bytes == 3 * first_block))

            # 11b. 同一條預設路徑走 notice（Claude）：期限在讀到一半跨過就不說、不寫標記；
            # 沒跨過就說那一行。
            claude_event = {"session_id": "dp-claude", "transcript_path": os.fspath(large)}
            claude_options = {"context_meter": {"autocompact_tokens": 225000}}
            claude_cut_dir, claude_ok_dir = root / "dp-claude-cut", root / "dp-claude-ok"
            claude_cut, _reads, _bytes = metered(
                lambda: notice(claude_event, vault, claude_cut_dir, options=claude_options), crossing_step)
            claude_ok, _reads, _bytes = metered(
                lambda: notice(claude_event, vault, claude_ok_dir, options=claude_options), roomy_step)
            checks.append(("notice for Claude on the default path says nothing when the deadline is crossed "
                           "mid-read and says its line when it is not",
                           claude_cut is None
                           and not (claude_cut_dir / memspec.CONTEXT_METER_MARKER).exists()
                           and claude_ok is not None
                           and claude_ok[0] == render(
                               222222, 225000, memspec.CONTEXT_METER_SOURCE_OVERRIDE,
                               _compact_map.handoff_destination(vault, "dp-claude", large))))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        if saved_config_env is None:
            os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None)
        else:
            os.environ[memspec.EPITYPE_CONFIG_ENV] = saved_config_env

    passed = sum(bool(ok) for _, ok in checks)
    total = 32
    status_word = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status_word} {passed}/{total}")
    if status_word != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status_word == "PASS" else 1


def trace_command(count, path=None, out=print, now=None):
    """印追蹤檔最後 count 行（唯讀）。第一行說檔在哪、追蹤開到哪一天；開始與結束行依 run
    配成一列，只有開始沒有結束的標出來（見 meter_trace.paired）。"""
    try:
        from . import meter_trace
    except ImportError:
        import meter_trace
    target = Path(path) if path is not None else meter_trace.trace_path()
    state = "on" if meter_trace.enabled(now) else "off"
    out(f"# trace {target} ({state} until {meter_trace.until().isoformat()})")
    for line in meter_trace.paired(meter_trace.last(count, target)):
        out(line)
    return 0


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="epitype context-meter",
        description="Context meter for Claude Code (learned auto-compaction threshold) and Codex "
                    "(its own auto-compaction limit): current usage and when the handoff reminder fires.")
    parser.add_argument("--selftest", action="store_true")
    commands = parser.add_subparsers(dest="command")
    calibrate_parser = commands.add_parser(
        "calibrate", help="report (read-only) the auto-compaction points in recent Claude Code transcripts")
    calibrate_parser.add_argument("--root", help="transcript root (default: ~/.claude/projects)")
    replay_parser = commands.add_parser(
        "codex-calibrate", help="replay Codex rollouts (read-only) and report the reminder headroom H")
    replay_parser.add_argument("--root", help="rollout root (default: $CODEX_HOME/sessions)")
    replay_parser.add_argument("--since", default="2026-09-02", help="only rollouts modified on or after this date")
    replay_parser.add_argument("--limit", type=int, required=True, help="the auto-compaction limit in force")
    replay_parser.add_argument("--hard-cap", type=int, required=True,
                               help="the model_context_window the rollouts reported for that setting")
    replay_parser.add_argument("--headroom", type=int, nargs="+",
                               default=list(range(10000, 82000, 2000)), help="candidate H values")
    status_parser = commands.add_parser("status", help="print the threshold, its source and the current usage")
    status_parser.add_argument("--transcript", help="a Claude Code transcript or Codex rollout JSONL to measure")
    status_parser.add_argument("--vault", help="governance vault (default: from the Epitype config)")
    trace_parser = commands.add_parser(
        "trace", help="print (read-only) the last lines of the PreCompact/SessionStart/reminder trace")
    trace_parser.add_argument("--last", type=int, default=memspec.CONTEXT_METER_TRACE_DEFAULT_LAST,
                              help="how many lines (default: %(default)s)")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "codex-calibrate":
        from datetime import datetime, timezone

        root = Path(args.root).expanduser() if args.root else codex_home() / "sessions"
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp()
        codex_calibrate(root, since, args.limit, args.hard_cap, args.headroom)
        return 0
    if args.command == "calibrate":
        root = Path(args.root).expanduser() if args.root else Path.home() / ".claude" / "projects"
        calibrate(root)
        return 0
    if args.command == "trace":
        return trace_command(args.last)
    options = memspec.config_options()
    status(options, _governance(args.vault, options), args.transcript)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
