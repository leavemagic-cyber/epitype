import sys; sys.dont_write_bytecode = True
"""夜間回饋：把白天的對話重跑一次，找出「該擋卻沒擋」的地方。

閘是在 owner 等回話的當下跑的，所以它有期限、有每次呼叫的讀卡上限、出錯一律放行。
這三件事都會讓它漏，而且漏的時候完全不出聲——擋到幾次查得到，漏掉幾次查不到，於是
「Epitype 有沒有在發揮作用」只答得出報喜的那一半。

這支在夜裡跑，沒有期限也沒有上限：同一批卡、同一套比對，重放整天的對話紀錄，再跟閘
自己的稽核帳對帳。對得上就是當場擋下了，對不上就是漏了。

刻意重用閘自己的讀卡與遮罩程式（stop_gate._read_decision／_quoted_spans、
pretooluse_gate._read_guard），不另寫一份：重放若用不同的判讀，對不上代表這支寫錯，
不代表閘漏了，那樣的「發現」比沒有更糟。
"""

from collections import namedtuple
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _extra in (str(_REPO_ROOT), str(_REPO_ROOT / "adapters" / "claude")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from epitype import memspec

# 一次夜跑最多讀幾個對話檔、每個檔最多幾 MB。夢有總預算，重放不該把它吃光。
MAX_TRANSCRIPTS = 250
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
# 一張卡連續幾天完全沒命中，就值得回頭看它是不是當初就寫廢了。
STALE_CARD_DAYS = 30

Rule = namedtuple("Rule", "card kind path mtime forbidden require_when require_text tool fragments")
Hit = namedtuple("Hit", "card kind session at fragment digest final text")

# 一次攔截「什麼都沒改變」的判準：重寫後與原文的相似度。誤擋通常只換兩三個字，真違規
# 要補一整段或整句重來。
NOOP_SIMILARITY = 0.90
# 單一次就認定太急（我也可能只是小改一下再被擋一次），所以要累積；相似到幾乎沒動過的
# 那種，一次就夠。
NOOP_SIMILARITY_INSTANT = 0.98
NOOP_EVIDENCE_NEEDED = 2
# 一張卡最多自動放行幾串字。超過就不是某個字誤擋，是這條樣式整個寫太寬。
MAX_EXCEPTIONS_PER_CARD = 5
# 例外不是永久豁免：這段時間內沒有再出現誤擋證據就自動失效，樣式回到完整範圍。
EXCEPTION_TTL_DAYS = 90


def _one_line(value):
    return " ".join(str(value or "").split())


def armed_rules(vault):
    """這個庫裡所有「擋得住」的卡，用閘自己的讀法讀出來。"""
    import pretooluse_gate
    import stop_gate

    rules = []
    vault = Path(vault)
    for path in sorted(vault.rglob("*.md")):
        if any(part.startswith((".", "_")) for part in path.relative_to(vault).parts):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        try:
            ruling = stop_gate._read_decision(path)
        except Exception:
            ruling = None
        if isinstance(ruling, dict):
            name = _one_line(ruling.get("key")) or path.stem
            forbidden = [item for item in ruling.get(memspec.FORBIDDEN_FIELD, []) if item]
            if forbidden:
                rules.append(Rule(name, "forbidden", path, mtime, forbidden, "", "", "", ()))
            when = _one_line(ruling.get("require_when"))
            text = _one_line(ruling.get("require_text"))
            if when and text:
                rules.append(Rule(name, "require", path, mtime, [], when, text, "", ()))
        try:
            guard = pretooluse_gate._read_guard(path)
        except Exception:
            guard = None
        if isinstance(guard, dict) and guard.get("substrings"):
            rules.append(
                Rule(
                    _one_line(guard.get("card")) or path.stem,
                    "guard",
                    path,
                    mtime,
                    [],
                    "",
                    "",
                    _one_line(guard.get("tool")),
                    tuple(guard["substrings"]),
                )
            )
    return rules


def _blocks_outside_quotes(pattern, text, cache):
    """閘的判讀：命中若整段落在引用裡就不算，所以重放也不算。"""
    import stop_gate
    from _hook_common import compile_bounded_regex

    if pattern not in cache:
        try:
            cache[pattern] = compile_bounded_regex(pattern)
        except Exception:
            cache[pattern] = None
    regex = cache[pattern]
    if regex is None:
        return None
    quoted = stop_gate._quoted_spans(text)
    for found in regex.finditer(text):
        if any(start <= found.start() and found.end() <= end for start, end in quoted):
            continue
        return found
    return None


def _turns(path, max_bytes=MAX_TRANSCRIPT_BYTES):
    """(session, 時間, 這一輪最後一段文字, 這一輪全部文字, 這一輪的工具呼叫)。

    閘只看得到一輪的最後一段話，中間那些它從來看不到。兩者分開回傳，因為
    「最後一段命中卻沒擋」是漏擋，「中間命中」是閘的視野之外——兩種要分開講，混成
    一個數字會把設計邊界說成缺陷。
    """
    read = 0
    session, stamp = "", ""
    texts, calls = [], []
    try:
        stream = io.open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with stream:
        for line in stream:
            read += len(line)
            if read > max_bytes:
                break
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            kind = row.get("type")
            if kind == "user":
                if _is_tool_result(row):
                    # 工具回傳也記成 `user` 列，但它不是一個回合的結束——把它當邊界的話，
                    # 一個回合會被切成十幾段。後果有兩個：機會數（分母）虛高好幾倍，讓
                    # 自動降級的門檻形同虛設；以及一堆工具呼叫前的中段訊息被標成「回合
                    # 最後一則」，全部變成假漏擋。2026-09-17 對抗審查實測：某個紀錄 300
                    # 個 user 列裡有 279 個是工具回傳。
                    continue
                if texts or calls:
                    yield session, stamp, texts[-1] if texts else "", list(texts), list(calls)
                texts, calls = [], []
                continue
            if kind != "assistant":
                continue
            session = row.get("sessionId") or session
            stamp = row.get("timestamp") or stamp
            message = row.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    if block["text"].strip():
                        texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    payload = block.get("input")
                    joined = "\n".join(
                        value for value in (payload or {}).values() if isinstance(value, str)
                    ) if isinstance(payload, dict) else ""
                    calls.append((_one_line(block.get("name")), joined))
    if texts or calls:
        yield session, stamp, texts[-1] if texts else "", list(texts), list(calls)


def _is_tool_result(row):
    """這一列是不是工具回傳（宿主把它記成 user 列，但它不是使用者說話）。"""
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _epoch_of(stamp):
    """對話紀錄的時間字串轉成秒；讀不出來就回 None（當作無法判斷新舊）。"""
    from datetime import datetime

    text = str(stamp or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def replay(rules, transcripts, epoch=None, since=None):
    """整天的對話對上這些卡，回傳每一次命中。

    只算「卡片存在之後」發生的事。比的是事件當下的時間，不是現在：拿現在去比，等於把
    卡片還不存在的那整段時間也算成漏擋，憑空長出一堆假缺口——2026-09-17 第一次實跑就
    這樣多算了 374 次。`epoch` 是額外的上界（測試用），事件時間讀不出來時才退回它。
    """
    hits = []
    cache = {}
    for path in transcripts:
        for session, stamp, final_text, all_texts, calls in _turns(path):
            happened = _epoch_of(stamp)
            # 一個今天動過的對話檔，裡面裝的是好幾天份的內容。重放的時間窗必須照事件
            # 本身的時間切，不能照檔案的修改時間——否則會跟稽核帳的起算日對不齊，把
            # 窗外早就擋過的事算成漏擋（2026-09-17 第一次實跑就是這樣多報一條）。
            if since is not None and happened is not None and happened < since:
                continue
            if happened is None:
                happened = epoch
            for rule in rules:
                if happened is not None and rule.mtime > happened:
                    continue
                if rule.kind == "guard":
                    for tool, payload in calls:
                        if tool.casefold() != rule.tool.casefold() or not payload:
                            continue
                        if all(fragment in payload for fragment in rule.fragments):
                            hits.append(Hit(rule.card, "guard", session, stamp,
                                            "＋".join(rule.fragments), _digest(payload), True, payload))
                    continue
                for index, text in enumerate(all_texts):
                    final = text is final_text and index == len(all_texts) - 1
                    if rule.kind == "forbidden":
                        for pattern in rule.forbidden:
                            found = _blocks_outside_quotes(pattern, text, cache)
                            if found is not None:
                                hits.append(Hit(rule.card, "forbidden", session, stamp,
                                                _one_line(found.group(0))[:80], _digest(text), final, text))
                                break
                    elif rule.kind == "require":
                        trigger = _blocks_outside_quotes(rule.require_when, text, cache)
                        if trigger is None:
                            continue
                        if _blocks_outside_quotes(rule.require_text, text, cache) is None:
                            hits.append(Hit(rule.card, "require", session, stamp,
                                            _one_line(trigger.group(0))[:80], _digest(text), final, text))
    return hits


def _digest(text):
    return hashlib.sha256(str(text).encode("utf-8", errors="replace")).hexdigest()[:16]


def gate_blocks(vault, since=None):
    """閘自己的稽核帳：(session, 卡名) -> 擋下次數，外加被擋過的 (session, 訊息指紋)。

    第二份是用來分辨「這張卡放行了」與「同一則訊息被別張卡擋下、我已經重寫過」。閘一次
    只報一個理由就停手，沒有這份的話，被擋訊息裡的第二個違規會全部被算成漏擋。
    """
    # 輪替出去的那幾份也要讀：稽核檔滿了會改名存檔，只讀主檔的話，輪替之前每一次真的
    # 擋下都會被算成漏擋（2026-09-17 對抗審查）。
    root = Path(vault)
    paths = [root / memspec.GATE_LOG_FILENAME]
    paths += sorted(root.glob(memspec.GATE_LOG_FILENAME + ".*"))
    counts = {}
    stopped = set()
    for path in paths:
        _read_gate_log(path, since, counts, stopped)
    return counts, stopped


def _read_gate_log(path, since, counts, stopped):
    """把一份稽核檔併進計數與指紋集合（就地更新，不回傳）。"""
    try:
        stream = io.open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("kind") not in (
                memspec.STOP_GATE_LOG_KIND,
                memspec.ACTION_GUARD_LOG_KIND,
                memspec.WRITE_GATE_LOG_KIND,
            ):
                continue
            stamp = str(row.get("timestamp", ""))
            if since and stamp[:10] < since:
                continue
            session = row.get("session_id") or ""
            key = (session, row.get("card") or row.get("decision") or "")
            counts[key] = counts.get(key, 0) + 1
            if row.get("digest"):
                stopped.add((session, row["digest"]))


def reconcile(hits, blocks, stopped=()):
    """命中對上稽核帳：哪些當場擋了，哪些漏了。

    三件事不算漏擋，因為它們都是設計而不是缺口：
    - 同一場、同一張卡、同一段話重複出現——Stop 閘只擋一次，不然改不動就被永遠擋著。
    - 回合中間的訊息——Stop 閘只看最後一段，中間那些它從來看不到，另外歸一類。
    - 同一則訊息已經被別張卡擋下——閘一次只報一個理由就停手，那則訊息已經被退回重寫。
    """
    remaining = dict(blocks)
    stopped = set(stopped)
    seen = set()
    blocked, missed, unseen = [], [], []
    for hit in hits:
        key = (hit.session, hit.card)
        if not hit.final and hit.kind != "guard":
            unseen.append(hit)
            continue
        marker = (hit.session, hit.card, hit.digest)
        if marker in seen:
            continue
        seen.add(marker)
        if remaining.get(key, 0) > 0:
            remaining[key] -= 1
            blocked.append(hit)
        elif (hit.session, hit.digest) in stopped:
            blocked.append(hit)
        else:
            missed.append(hit)
    return blocked, missed, unseen


def transcripts_for(roots, since_stamp=None, limit=MAX_TRANSCRIPTS):
    """(要重放的對話紀錄檔, 符合條件的總數)，新的優先。

    總數一起回傳，因為「只讀了其中一部分」與「查遍全部、零漏擋」報出來長得一模一樣。
    2026-09-17 實測這台機器 24 小時內動過 101 個檔，當時上限 24——76% 被無聲丟掉。
    """
    found = []
    for root in roots:
        try:
            entries = list(Path(root).rglob("*.jsonl"))
        except OSError:
            continue
        for path in entries:
            try:
                info = path.stat()
            except OSError:
                continue
            if since_stamp is not None and info.st_mtime < since_stamp:
                continue
            found.append((info.st_mtime, path))
    found.sort(reverse=True)
    return [path for _mtime, path in found[:limit]], len(found)


def final_messages(transcripts):
    """每一場依序的 (訊息指紋, 訊息全文)——閘看得到的那一則，也就是每回合的最後一段。

    誤擋要靠「擋下之後我重寫成什麼」來判，所以得拿得到被擋那則的下一則。指紋跟閘記在
    稽核帳上的是同一個算法，兩邊才對得起來。
    """
    order = {}
    for path in transcripts:
        for session, _stamp, final_text, _all_texts, _calls in _turns(path):
            if final_text:
                order.setdefault(session, []).append((_digest(final_text), final_text))
    return order


def _sentence_around(text, fragment):
    """命中所在的那一句話。找不到就退回整段。

    比對範圍要跟「改了什麼」對齊。拿整則訊息算相似度的話，一則四百字的回覆就算真的
    照規則整句改寫，整體差異也只有百分之一——長訊息必然被判成「擋了等於沒擋」。
    """
    body = str(text or "")
    where = body.find(fragment) if fragment else -1
    if where < 0:
        return body
    starts = max(
        (body.rfind(mark, 0, where) for mark in memspec.STOP_GATE_SENTENCE_TERMINATORS),
        default=-1,
    )
    ends = [body.find(mark, where) for mark in memspec.STOP_GATE_SENTENCE_TERMINATORS]
    ends = [index for index in ends if index >= 0]
    return body[starts + 1: (min(ends) + 1) if ends else len(body)].strip() or body


def noop_blocks(blocked, finals):
    """擋了等於沒擋的那些：擋下之後重寫出來的東西跟原本幾乎一樣。

    真的違規要補一整段或整句重來；誤擋只換兩三個字就送出去了，因為要改的東西本來就
    不存在。回傳 (卡, 命中字串, 相似度)。

    這個判準對「規避」也會給高分——把「應該可以」換成「應當可以」意思沒變、相似度也
    高。所以它只是證據，不是結論：自動放行的對象限縮成**當時命中的那一串字**，而且要
    累積到門檻、還會過期。規避改掉的正是那串字，於是被放行的是一個我已經不用的寫法。
    """
    from difflib import SequenceMatcher

    found = []
    for hit in blocked:
        sequence = finals.get(hit.session) or []
        index = next((i for i, (digest, _text) in enumerate(sequence) if digest == hit.digest), None)
        if index is None or index + 1 >= len(sequence):
            continue
        rewritten = sequence[index + 1][1]
        before = _sentence_around(hit.text, hit.fragment)
        after = _sentence_around(rewritten, hit.fragment)
        # 不能用「那串字還在不在」當判準：誤擋的時候我也一定會改掉那串字，不然過不了
        # 閘。差別在改動的幅度——照規則改要重寫一整句，誤擋只換兩三個字。
        ratio = SequenceMatcher(None, before, after).ratio()
        if ratio >= NOOP_SIMILARITY:
            found.append((hit.card, hit.digest, round(ratio, 3)))
    return found


def opportunities(transcripts, since=None):
    """歷史裡有幾次「它本來就有機會命中」：回合數，以及各工具的呼叫次數。

    命中次數本身說不出一條樣式寬不寬——heredoc 那條 36 小時攔了 8 次，因為我真的犯了
    8 次。要看的是比例：在幾次機會裡命中了幾次。
    """
    turns = 0
    tools = {}
    for path in transcripts:
        for _session, stamp, final_text, _all_texts, calls in _turns(path):
            happened = _epoch_of(stamp)
            if since is not None and happened is not None and happened < since:
                continue
            if final_text:
                turns += 1
            for tool, payload in calls:
                if payload:
                    key = tool.casefold()
                    tools[key] = tools.get(key, 0) + 1
    return {"turns": turns, "tools": tools}


def rehearse(rules, hits, chances):
    """每條樣式在歷史裡的命中率：(命中次數, 機會次數, 比率)。

    這是 Dream-RSI 那套「歷史就是模擬器」用在規則上：一條新樣式不必等它明天在 owner
    面前出錯，拿累積的紀錄重走一次就知道它會攔到什麼。一條在十次機會裡命中超過一次的
    樣式，描述的已經不是某個具體錯誤，而是我平常說話的方式——那種東西不該擋。
    """
    # 分子只算閘看得到的：回合最後一則訊息，以及工具呼叫。回合中段的訊息 Stop 閘從來
    # 看不到，把它們算進來會讓一條「正在生效」的規則（我已經學會不寫進結尾、推理中還會
    # 提）因為命中率高而被自動關掉——分子分母單位不一致，2026-09-17 審查實跑重現。
    counted = {}
    for hit in hits:
        if hit.kind != "guard" and not hit.final:
            continue
        counted[hit.card] = counted.get(hit.card, 0) + 1
    result = {}
    for rule in rules:
        if rule.kind == "guard":
            chance = chances["tools"].get(rule.tool.casefold(), 0)
        else:
            chance = chances["turns"]
        matches = counted.get(rule.card, 0)
        rate = (matches / chance) if chance else 0.0
        result[rule.card] = (matches, chance, round(rate, 4))
    return result


# 十次機會命中超過一次就不是在描述某個具體錯誤了。超過就自動降級成只計數不攔，
# 比率掉回來會自動恢復——降級是可逆的觀察，不是把規則刪掉。
REHEARSAL_MAX_RATE = 0.10
# 機會太少時比率沒有意義（跑兩回合命中一次＝50%）。
REHEARSAL_MIN_CHANCES = 40


def demoted_cards(vault):
    """永遠是空的：自動降級已經拿掉。

    命中率分不出「規則太寬」與「我一直犯這條」。2026-09-17 驗收實跑：一條 6 次命中、
    6 次全部擋下、0 漏擋的規則，率 0.12 就被關掉 14 天，連 heredoc 那張安全守衛卡都一
    起關——規則因為正常運作而失效，而且受管制的那一方自己就能觸發。判準錯了就不是調
    門檻的問題。彩排的數字照算、照存，給寫規則的人看；沒有任何東西會因為它而停止攔截。

    留著這個函式是為了讓兩道閘的呼叫端不必分岔，也讓「有沒有東西被自動關掉」這件事
    有一個確定的答案。
    """
    return set()


HEALTH_FILENAME = "gate_health.json"
HEALTH_WINDOW_DAYS = 30


def health_path(vault):
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY / HEALTH_FILENAME


def load_health(vault, defects=None):
    """每張武裝卡的命中健康度，讀不到或型別不對就當空的並出聲。

    刻意放在卡片旁邊而不是寫進卡片：寫進卡片會動到它的修改時間，而重放正是靠那個時間
    判斷「事情發生時這張卡存不存在」——寫一次統計就把隔天的判斷弄壞。

    這個檔能決定哪些卡不生效，所以逐欄位驗型別，壞的那一筆丟掉、其餘照用。以前任何一
    個欄位型別不對（例如 `last_hit` 寫成數字）就會在排序時丟例外，被上層整段吞掉，整
    個庫的排序當場退回字母序而且完全無聲——2026-09-17 對抗審查實跑重現。
    """
    defects = [] if defects is None else defects
    try:
        loaded = json.loads(io.open(health_path(vault), encoding="utf-8").read())
    except OSError:
        return {}
    except ValueError as exc:
        defects.append(f"健康度檔讀不動（{exc}），這一輪不採用它的排序與例外")
        return {}
    cards = loaded.get("cards") if isinstance(loaded, dict) else None
    if not isinstance(cards, dict):
        defects.append("健康度檔的 cards 不是物件，這一輪不採用")
        return {}

    clean = {}
    for name, entry in cards.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            defects.append(f"健康度檔有一筆型別不對（{str(name)[:40]}），已忽略")
            continue
        checked = {}
        for field, wanted in (
            ("hits_30d", int), ("last_hit", str), ("first_seen", str),
            ("demoted_since", str), ("demoted_until", str), ("broadened_at", str),
            ("pattern_digest", str), ("recent", dict), ("exceptions", dict),
            ("noop_evidence", dict), ("rehearsed", dict),
        ):
            if field not in entry:
                continue
            value = entry[field]
            if isinstance(value, wanted) and not isinstance(value, bool):
                checked[field] = value
            else:
                defects.append(f"健康度檔 {name} 的 {field} 型別不對，已忽略該欄位")
        clean[name] = checked
    return clean


# 2026-09-17 四份獨立對抗審查一致判定：自動放行與自動降級這兩條迴圈，可以在一個晚上、
# 零人工的情況下把一條規則關掉，而且無聲。三個各自獨立的破法都實跑重現過：
#   - 字面樣式的「命中字串」就是樣式本身，所以放行一串字＝關掉整條規則 90 天；
#   - 相似度算的是整則訊息，長訊息就算真的照規則改了也會被判成「擋了等於沒擋」；
#   - 彩排把閘從來看不到的回合中段訊息也算進分子，一條正在生效的規則會因此被降級。
# 三個都是單位算錯，不是概念錯，所以 2026-09-17 當天逐一修好後恢復：放行改認訊息指紋
# （字面規則再也不會被一串字關掉）、相似度只比命中那一句、彩排只算閘看得到的。降級另外
# 加上到期日——夜跑一停，沒有到期日的降級就是永久靜音。
DEMOTION_TTL_DAYS = 14


def exceptions_for(vault):
    """卡名 -> 已自動放行的**訊息指紋**集合；過期的不算。

    放行的對象刻意是「那一則訊息」而不是「那串字」。字面規則的命中字串就是規則本身，
    放行一串字＝把整條規則關掉 90 天（2026-09-17 對抗審查實跑重現）。改成認指紋之後，
    同一句無害的話不會被重複擋，而任何新的違規照擋——這條路徑再也不可能關掉一條規則。
    """
    from datetime import date

    cards = load_health(vault)
    today = date.today().isoformat()
    result = {}
    for name, entry in cards.items():
        if not isinstance(entry, dict):
            continue
        live = set()
        for fragment, meta in (entry.get("exceptions") or {}).items():
            if isinstance(meta, dict) and str(meta.get("until", "")) >= today:
                live.add(fragment)
        if live:
            result[name] = live
    return result


def _fold_exceptions(entry, card, noops, today):
    """把今晚的誤擋證據併進這張卡的放行清單，回傳新增了哪幾串字。"""
    from datetime import date

    evidence = dict(entry.get("noop_evidence") or {})
    exceptions = dict(entry.get("exceptions") or {})
    added = []
    for name, fragment, ratio in noops:
        if name != card or not fragment:
            continue
        rows = list(evidence.get(fragment) or [])
        rows.append(ratio)
        evidence[fragment] = rows[-8:]
        enough = len(rows) >= NOOP_EVIDENCE_NEEDED or ratio >= NOOP_SIMILARITY_INSTANT
        if not enough or fragment in exceptions:
            continue
        if len(exceptions) >= MAX_EXCEPTIONS_PER_CARD:
            # 五串字都誤擋，問題就不在某個字，而是這條樣式整個寫太寬。放行到此為止，
            # 由「連續誤擋」這件事本身留在證據裡，不再自動擴大豁免範圍。
            entry["over_broad_since"] = entry.get("over_broad_since") or today.isoformat()
            break
        exceptions[fragment] = {
            "since": today.isoformat(),
            "until": (today + timedelta(days=EXCEPTION_TTL_DAYS)).isoformat(),
            "ratios": evidence[fragment],
        }
        added.append(fragment)
    entry["noop_evidence"] = evidence
    entry["exceptions"] = {
        fragment: meta for fragment, meta in exceptions.items()
        if isinstance(meta, dict) and str(meta.get("until", "")) >= today.isoformat()
    }
    return added


def _fold_rehearsal(entry, rule, measured, today):
    """把歷史命中率併進來，太寬的自動降級，掉回來自動恢復。

    樣式改過就重算：健康度裡存著上一次的樣式指紋，指紋變了代表這是一條新樣式，它在
    歷史上的表現要重新看，不能沿用舊的數字背書。
    """
    matches, chances, rate = measured
    digest = _digest("|".join(
        list(rule.forbidden) + [rule.require_when, rule.require_text, rule.tool]
        + list(rule.fragments)
    ))
    changed = entry.get("pattern_digest") not in (None, digest)
    entry["pattern_digest"] = digest
    entry["rehearsed"] = {"matches": matches, "chances": chances, "rate": rate,
                          "on": today.isoformat()}
    # 數字只記不動作。以前這裡會把命中率高的卡關掉，而命中率高分不出「太寬」與「我一直
    # 犯」——留下的是紀錄，判斷交給寫規則的人。
    entry.pop("demoted_since", None)
    entry.pop("demoted_until", None)
    if chances >= REHEARSAL_MIN_CHANCES and rate > REHEARSAL_MAX_RATE and changed:
        # 改過之後才變這麼寬：記下來，讓「這一版比它取代的那一版差」看得見。
        entry["broadened_at"] = today.isoformat()


def update_health(vault, rules, hits, today, noops=(), rehearsed=None):
    """把今天的命中併進健康度，回傳新的一份（並落檔）。

    夢每晚自己做完這件事，不必有人去讀報告——這份檔案下一次工具呼叫就會被閘讀到。
    """
    cards = dict(load_health(vault))
    stamp = today.isoformat()
    counted = {}
    for hit in hits:
        counted[hit.card] = counted.get(hit.card, 0) + 1
    for rule in rules:
        entry = dict(cards.get(rule.card) or {})
        entry.setdefault("first_seen", stamp)
        today_hits = counted.get(rule.card, 0)
        recent = entry.get("recent")
        recent = dict(recent) if isinstance(recent, dict) else {}
        if today_hits:
            recent[stamp] = recent.get(stamp, 0) + today_hits
            entry["last_hit"] = stamp
        cutoff = (today - timedelta(days=HEALTH_WINDOW_DAYS)).isoformat()
        recent = {day: count for day, count in recent.items() if day >= cutoff}
        entry["recent"] = recent
        entry["hits_30d"] = sum(recent.values())
        _fold_exceptions(entry, rule.card, noops, today)
        if rehearsed and rule.card in rehearsed:
            _fold_rehearsal(entry, rule, rehearsed[rule.card], today)
        cards[rule.card] = entry
    try:
        target = health_path(vault)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(f".{target.name}.tmp-{os.getpid()}")
        io.open(staging, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"version": 1, "updated": stamp, "cards": cards},
                       ensure_ascii=False, separators=(",", ":"))
        )
        os.replace(staging, target)
    except OSError:
        pass
    return cards


def priority_rank(vault):
    """卡名 -> 排序鍵；近期真的攔到東西的排前面。

    閘每次呼叫只讀得動一小片卡，所以「先讀哪幾張」決定了哪些規則實際生效。照命中排序
    之後，會攔到東西的卡永遠落在上限之內，而閒著的卡用剩下的額度慢慢輪——這是「該擋
    沒擋」唯一機械歸因得出來的原因，夢每晚自己修掉它，不必有人介入。
    """
    cards = load_health(vault)
    ranks = {}
    for name, entry in cards.items():
        if not isinstance(entry, dict):
            continue
        hits = entry.get("hits_30d")
        last = entry.get("last_hit") or ""
        ranks[name] = (-(hits if isinstance(hits, int) else 0), "" if not last else _negated(last))
    return ranks


def _negated(stamp):
    """讓新的日期排前面：字串比較下，反轉每一位數字。"""
    return "".join(chr(ord("9") - (ord(ch) - ord("0"))) if ch.isdigit() else ch for ch in stamp)


def _selftest():
    import tempfile
    import time
    import uuid

    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-compliance-") as temp_dir:
            root = Path(temp_dir).resolve()
            vault = root / "vault"
            vault.mkdir()
            (vault / "decision-x.md").write_text(
                "---\nname: 測試裁定\ndescription: 說明\ndecision_key: probe\nstatus: active\n"
                "current_decision_at: 2026-09-16\ndecided_by: owner-explicit\nowner_quote: 不准講這句\n"
                "aliases: [甲名, 乙名]\nforbidden:\n  - 這句話不准講\n---\nbody\n",
                encoding="utf-8",
            )
            (vault / "scar-y.md").write_text(
                '---\nname: 測試守衛\ndescription: 說明\nguard_tool: Bash\n'
                'guard_all_of:\n  - "<<"\n  - "\\\\"\n---\nbody\n',
                encoding="utf-8",
            )
            (vault / "decision-z.md").write_text(
                "---\nname: 測試要求\ndescription: 說明\ndecision_key: needs\nstatus: active\n"
                "current_decision_at: 2026-09-16\ndecided_by: owner-explicit\nowner_quote: 要帶證據\n"
                "aliases: [丙名, 丁名]\nrequire_when: (已完成)\nrequire_text: (實測)\n---\nbody\n",
                encoding="utf-8",
            )
            rules = armed_rules(vault)
            kinds = sorted(rule.kind for rule in rules)
            checks.append((
                "三種武裝欄位都被讀成規則",
                kinds == ["forbidden", "guard", "require"],
            ))

            session = "s-" + uuid.uuid4().hex

            def row(kind, blocks):
                return json.dumps({
                    "type": kind, "sessionId": session, "timestamp": "2026-09-16T10:00:00+00:00",
                    "message": {"role": kind, "content": blocks},
                }, ensure_ascii=False)

            transcript = root / "t.jsonl"
            transcript.write_text(
                "\n".join([
                    row("user", [{"type": "text", "text": "開始"}]),
                    row("assistant", [
                        {"type": "text", "text": "中間這句話不准講，但這是中間。"},
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "python - <<'PY'\np='c:\\x'\nPY\n"}},
                        {"type": "text", "text": "結尾這句話不准講。"},
                    ]),
                    row("user", [{"type": "text", "text": "下一輪"}]),
                    row("assistant", [{"type": "text", "text": "這批已完成，收工。"}]),
                    row("user", [{"type": "text", "text": "再一輪"}]),
                    row("assistant", [{"type": "text", "text": "這批已完成：實測 3/3。"}]),
                    row("user", [{"type": "text", "text": "引用"}]),
                    row("assistant", [{"type": "text", "text": "規則擋的是「這句話不准講」這種講法。"}]),
                    # 誤擋的形狀：被擋下之後只換了一個字就重送，內容完全沒動。
                    row("user", [{"type": "text", "text": "再來"}]),
                    row("assistant", [{"type": "text", "text": "兩邊帳這句話不准講，所以我先把它放這裡。"}]),
                    row("user", [{"type": "text", "text": "改一下"}]),
                    row("assistant", [{"type": "text", "text": "兩邊帳這句話不准說，所以我先把它放這裡。"}]),
                    # 工具回傳也記成 user 列，但它不是回合邊界。
                    row("user", [{"type": "text", "text": "最後一輪"}]),
                    row("assistant", [
                        {"type": "text", "text": "中段：先查一下。"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    ]),
                    row("user", [{"type": "tool_result", "tool_use_id": "t1"}]),
                    row("assistant", [{"type": "text", "text": "查完了，結尾在這裡。"}]),
                ]) + "\n",
                encoding="utf-8",
            )

            # 夾具的事件時間是 2026-09-16，卡是剛剛才寫的，所以這裡把事件時間蓋掉，
            # 專測比對邏輯；「卡比事件新就不算」由下面那項單獨測。
            hits = replay(
                [rule._replace(mtime=0) for rule in rules], [transcript], epoch=time.time() + 60
            )
            by_kind = {}
            for hit in hits:
                by_kind.setdefault(hit.kind, []).append(hit)
            checks.append((
                "禁語命中三次（兩次在回合結尾、一次在回合中間），引號內那次不算",
                len(by_kind.get("forbidden", ())) == 3
                and sum(1 for hit in by_kind["forbidden"] if hit.final) == 2,
            ))
            checks.append((
                "工具呼叫命中守衛一次",
                len(by_kind.get("guard", ())) == 1,
            ))
            checks.append((
                "宣稱完成沒帶證據命中一次，帶了證據那次不算",
                len(by_kind.get("require", ())) == 1,
            ))

            blocked, missed, unseen = reconcile(hits, {(session, "probe"): 1})
            checks.append((
                "稽核帳裡有的算擋下，沒有的算漏擋，中間那次歸「閘看不到」",
                len(blocked) == 1 and len(unseen) == 1
                and sorted(hit.kind for hit in missed) == ["forbidden", "guard", "require"],
            ))

            # 同一則訊息被別張卡擋下：那則已經退回重寫，不該再算成這張卡漏擋。
            require_hit = next(hit for hit in hits if hit.kind == "require")
            _b2, missed2, _u2 = reconcile(
                hits, {(session, "probe"): 1}, stopped={(session, require_hit.digest)}
            )
            checks.append((
                "同一則訊息已被別張卡擋下，就不算這張卡漏擋",
                sorted(hit.kind for hit in missed2) == ["forbidden", "guard"],
            ))

            log = vault / memspec.GATE_LOG_FILENAME
            log.write_text(
                json.dumps({"timestamp": "2026-09-16T10:00:00+00:00",
                            "kind": memspec.STOP_GATE_LOG_KIND, "decision": "probe",
                            "session_id": session, "digest": "d00d"}, ensure_ascii=False) + "\n"
                + json.dumps({"timestamp": "2026-01-01T00:00:00+00:00",
                              "kind": memspec.STOP_GATE_LOG_KIND, "decision": "old",
                              "session_id": session}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            counts, stopped_keys = gate_blocks(vault, since="2026-09-01")
            checks.append((
                "稽核帳只讀閘的三種紀錄，看得懂起算日，也收得到訊息指紋",
                counts == {(session, "probe"): 1} and stopped_keys == {(session, "d00d")},
            ))

            windowed = replay(
                [rule._replace(mtime=0) for rule in rules], [transcript],
                epoch=time.time() + 60, since=time.time(),
            )
            checks.append((
                "時間窗照事件本身的時間切，窗外的不算",
                windowed == [],
            ))

            newer = root / "new.jsonl"
            newer.write_text("{}\n", encoding="utf-8")
            os.utime(newer, (time.time(), time.time()))
            picked, available = transcripts_for([root], since_stamp=time.time() - 3600)
            checks.append((
                "只挑起算時間之後動過的紀錄檔，新的排前面，並回報符合條件的總數",
                picked and picked[0] == newer and available == len(picked),
            ))
            few, total = transcripts_for([root], since_stamp=time.time() - 3600, limit=1)
            checks.append((
                "上限砍掉幾個檔要數得出來，不能靜靜少讀",
                len(few) == 1 and total >= 2 and total > len(few),
            ))

            from datetime import date

            day = date(2026, 9, 17)
            cards = update_health(vault, rules, hits, day)
            checks.append((
                "命中併進健康度，沒命中的卡也建檔但次數是零",
                cards["probe"]["hits_30d"] == 3
                and cards["probe"]["last_hit"] == "2026-09-17"
                and cards["needs"]["hits_30d"] == 1
                and cards["測試守衛"]["hits_30d"] == 1,
            ))
            update_health(vault, rules, [], day + timedelta(days=1))
            aged = load_health(vault)
            checks.append((
                "沒命中的那天不會清掉窗內的舊紀錄，也不會假造新的",
                aged["probe"]["hits_30d"] == 3 and aged["probe"]["last_hit"] == "2026-09-17",
            ))
            faded = update_health(vault, rules, [], day + timedelta(days=HEALTH_WINDOW_DAYS + 1))
            checks.append((
                "超過視窗的命中自動退出統計",
                faded["probe"]["hits_30d"] == 0,
            ))

            # 誤擋自動放行：擋完照原樣重送 → 那一串字進放行清單，而且會過期。
            finals = final_messages([transcript])
            probe_hit = next(
                hit for hit in hits
                if hit.kind == "forbidden" and hit.final and hit.text.startswith("兩邊帳")
            )
            noops = noop_blocks([probe_hit], finals)
            checks.append((
                "重寫後幾乎沒變＝擋了等於沒擋，認得出來",
                noops and noops[0][0] == "probe" and noops[0][2] >= NOOP_SIMILARITY,
            ))
            update_health(vault, rules, [], day, noops)
            update_health(vault, rules, [], day, noops)
            checks.append((
                "累積到門檻就自動放行，而且放行的是那一則訊息、不是那串字",
                exceptions_for(vault).get("probe") == {probe_hit.digest}
                and probe_hit.fragment not in exceptions_for(vault)["probe"],
            ))
            # 放行一則訊息不得讓同一條規則對別的訊息失效。
            other = probe_hit._replace(digest="ffffffffffffffff")
            checks.append((
                "別的訊息照擋：放行永遠關不掉一條規則",
                other.digest not in exceptions_for(vault)["probe"],
            ))
            entry = load_health(vault)["probe"]["exceptions"][probe_hit.digest]
            checks.append((
                "放行帶起訖日，會過期",
                entry["since"] == day.isoformat()
                and entry["until"] > entry["since"],
            ))

            # 彩排：在歷史上命中太頻繁的樣式自動降級，掉回來自動恢復。
            chances = opportunities([transcript])
            checks.append((
                "工具回傳不是回合邊界：那一輪只算一個回合，結尾是工具跑完之後那句",
                chances["turns"] == 7 and chances["tools"].get("bash") == 2,
            ))
            endings = [final for _s, _t, final, _a, _c in _turns(transcript)]
            checks.append((
                "工具前後被切成兩段的話，結尾會是中段那句——現在不會",
                endings[-1].startswith("查完了") and "中段" not in endings[-1],
            ))
            wide = Rule("寬樣式", "forbidden", vault / "decision-x.md", 0,
                        ["。"], "", "", "", ())
            wide_hits = replay([wide], [transcript], epoch=time.time() + 60)
            measured = rehearse([wide], wide_hits, {"turns": 100, "tools": {}})
            visible = sum(1 for hit in wide_hits if hit.final or hit.kind == "guard")
            checks.append((
                "命中率的分子只算閘看得到的（回合最後一則與工具呼叫），不是全部命中",
                measured["寬樣式"][0] == visible < len(wide_hits)
                and measured["寬樣式"][2] > 0,
            ))
            update_health(vault, [wide], wide_hits, day,
                          rehearsed={"寬樣式": (20, 100, 0.20)})
            entry = load_health(vault)["寬樣式"]
            checks.append((
                "命中率只記不動作：再高也不會有任何卡被自動關掉",
                demoted_cards(vault) == set()
                and "demoted_since" not in entry
                and entry["rehearsed"]["rate"] == 0.20,
            ))
            update_health(vault, [wide], [], day, rehearsed={"寬樣式": (2, 100, 0.02)})
            checks.append((
                "比率掉回來就自動恢復，降級是可逆的",
                not load_health(vault)["寬樣式"].get("demoted_since"),
            ))
            update_health(vault, [wide], [], day,
                          rehearsed={"寬樣式": (20, 10, 2.0)})
            checks.append((
                "機會太少時不下判斷（跑兩回合命中一次不算 50% 太寬）",
                demoted_cards(vault) == set(),
            ))

            # 健康度檔是外部可寫的資料，型別壞掉不得讓整套排序無聲消失。
            poisoned = health_path(vault)
            good = io.open(poisoned, encoding="utf-8").read()
            io.open(poisoned, "w", encoding="utf-8", newline="\n").write(
                json.dumps({"version": 1, "cards": {
                    "probe": {"hits_30d": 7},
                    "filler": {"last_hit": 20260917},
                    "壞的一筆": "不是物件",
                }}, ensure_ascii=False))
            noise = []
            poisoned_cards = load_health(vault, noise)
            poisoned_ranks = priority_rank(vault)
            checks.append((
                "健康度檔型別壞掉：丟掉壞的那幾筆、其餘照用，而且出聲",
                poisoned_cards["probe"]["hits_30d"] == 7
                and "last_hit" not in poisoned_cards["filler"]
                and "壞的一筆" not in poisoned_cards
                and len(noise) == 2
                and poisoned_ranks.get("probe") == (-7, ""),
            ))
            io.open(poisoned, "w", encoding="utf-8", newline="\n").write(good)

            # 稽核檔輪替出去的那幾份也要讀，否則輪替前擋下的都變成漏擋。
            log = vault / memspec.GATE_LOG_FILENAME
            rotated_path = vault / (memspec.GATE_LOG_FILENAME + ".1")
            io.open(rotated_path, "w", encoding="utf-8", newline="\n").write(
                json.dumps({"timestamp": "2026-09-16T09:00:00+00:00",
                            "kind": memspec.STOP_GATE_LOG_KIND, "decision": "rotated-away",
                            "session_id": session, "digest": "beef"},
                           ensure_ascii=False) + "\n")
            rotated_counts, rotated_stopped = gate_blocks(vault, since="2026-09-01")
            checks.append((
                "輪替出去的稽核檔也算數",
                rotated_counts.get((session, "rotated-away")) == 1
                and (session, "beef") in rotated_stopped,
            ))

            ranks = priority_rank(vault)
            checks.append((
                "健康度轉得出排序鍵；沒有檔案時退回一致的預設",
                set(ranks) == {"probe", "needs", "測試守衛", "寬樣式"}
                and priority_rank(root / "no-such-vault") == {},
            ))

            # 卡片不能因為記錄統計而被動到：重放靠卡片的修改時間判斷它當時存不存在。
            checks.append((
                "健康度寫在卡片旁邊，不動卡片本身",
                health_path(vault).is_file()
                and not any(
                    "gate_" in io.open(vault / name, encoding="utf-8").read()
                    for name in ("decision-x.md", "scar-y.md", "decision-z.md")
                ),
            ))

            # 卡比事件新：那天還沒有這張卡，不能算成漏擋。夾具事件是 2026-09-16，
            # 卡是此刻建立的，所以真實的事件時間本身就足以判掉全部。
            checks.append((
                "卡片建立之前發生的事不算數",
                replay(rules, [transcript]) == [],
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 28
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    print("用法：python -m epitype.compliance --selftest（本模組由夢的第 13 節呼叫）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
