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
MAX_TRANSCRIPTS = 24
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
# 一張卡連續幾天完全沒命中，就值得回頭看它是不是當初就寫廢了。
STALE_CARD_DAYS = 30

Rule = namedtuple("Rule", "card kind path mtime forbidden require_when require_text tool fragments")
Hit = namedtuple("Hit", "card kind session at fragment digest final")


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
                                            "＋".join(rule.fragments), _digest(payload), True))
                    continue
                for index, text in enumerate(all_texts):
                    final = text is final_text and index == len(all_texts) - 1
                    if rule.kind == "forbidden":
                        for pattern in rule.forbidden:
                            found = _blocks_outside_quotes(pattern, text, cache)
                            if found is not None:
                                hits.append(Hit(rule.card, "forbidden", session, stamp,
                                                _one_line(found.group(0))[:80], _digest(text), final))
                                break
                    elif rule.kind == "require":
                        trigger = _blocks_outside_quotes(rule.require_when, text, cache)
                        if trigger is None:
                            continue
                        if _blocks_outside_quotes(rule.require_text, text, cache) is None:
                            hits.append(Hit(rule.card, "require", session, stamp,
                                            _one_line(trigger.group(0))[:80], _digest(text), final))
    return hits


def _digest(text):
    return hashlib.sha256(str(text).encode("utf-8", errors="replace")).hexdigest()[:16]


def gate_blocks(vault, since=None):
    """閘自己的稽核帳：(session, 卡名) -> 擋下次數，外加被擋過的 (session, 訊息指紋)。

    第二份是用來分辨「這張卡放行了」與「同一則訊息被別張卡擋下、我已經重寫過」。閘一次
    只報一個理由就停手，沒有這份的話，被擋訊息裡的第二個違規會全部被算成漏擋。
    """
    path = Path(vault) / memspec.GATE_LOG_FILENAME
    counts = {}
    stopped = set()
    try:
        stream = io.open(path, encoding="utf-8", errors="replace")
    except OSError:
        return counts, stopped
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
    return counts, stopped


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
    """要重放的對話紀錄檔，新的優先。"""
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
    return [path for _mtime, path in found[:limit]]


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
                "禁語在結尾與中間各命中一次，引號內那次不算",
                len(by_kind.get("forbidden", ())) == 2
                and sum(1 for hit in by_kind["forbidden"] if hit.final) == 1,
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
                and sorted(hit.kind for hit in missed) == ["guard", "require"],
            ))

            # 同一則訊息被別張卡擋下：那則已經退回重寫，不該再算成這張卡漏擋。
            require_hit = next(hit for hit in hits if hit.kind == "require")
            _b2, missed2, _u2 = reconcile(
                hits, {(session, "probe"): 1}, stopped={(session, require_hit.digest)}
            )
            checks.append((
                "同一則訊息已被別張卡擋下，就不算這張卡漏擋",
                [hit.kind for hit in missed2] == ["guard"],
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
            picked = transcripts_for([root], since_stamp=time.time() - 3600)
            checks.append((
                "只挑起算時間之後動過的紀錄檔，新的排前面",
                picked and picked[0] == newer,
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
    total = 10
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
