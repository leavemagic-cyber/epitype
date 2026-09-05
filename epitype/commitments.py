import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""AI 自己開的承諾落成帳本：回合裡說「我等一下會…」的那句話，寫進帳，不靠記性。

2026-09-06 owner 痛點：AI 在回合裡承諾「之後我會…」「等 X 回報後我會…」，compaction
或換 session 之後兩邊都忘了，owner 得自己追。這裡是那條帳：Stop hook 每回合結束時
先 ``settle``（訊息裡有完成訊號就收尾）再 ``extract`` + ``record``（新承諾入帳），
SessionStart 與 PreCompact 把還 open 的條目端回模型眼前。

帳本＝``<治理 vault>/.epitype/commitments.jsonl``，刻意不是卡片目錄：這不是 owner 的
待辦，也不該被 pending_lint 當殭屍待辦點名，更不進喚回索引。句型表、引用排除、
digest 去重的規格一律在 memspec 的 ``COMMITMENT_*`` 區塊，線上 hook 與 CLI 讀同一份。

Fail-open by construction：帳本讀不到、壞行、鎖搶不到，一律當作沒有承諾，never raise
into the hook that called it.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import time

try:
    from . import memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import memspec


_TERMINATORS = re.escape(memspec.COMMITMENT_SENTENCE_TERMINATORS)
# One sentence with its terminator, or the trailing fragment that has none —
# the terminator is kept because「？」is what tells a question from a promise.
_SENTENCE_REGEX = re.compile(f"[^{_TERMINATORS}]*[{_TERMINATORS}]|[^{_TERMINATORS}]+$")
_WHITESPACE_REGEX = re.compile(r"\s+")


def _one_line(value):
    return " ".join(str(value or "").split())


def _normalized(text):
    """去空白＋casefold：「我會 補上 測試」與「我會補上測試」是同一句承諾。"""
    return _WHITESPACE_REGEX.sub("", str(text or "")).casefold()


def _is_question(sentence):
    text = sentence.strip()
    return text.endswith(memspec.STOP_GATE_QUESTION_ENDINGS) or any(
        marker in text for marker in memspec.STOP_GATE_QUESTION_MARKERS
    )


def digest(sentence):
    """capture.grant_digest 的同一個形狀：一句話一個 12 碼指紋。"""
    return hashlib.sha256(_one_line(sentence).encode("utf-8")).hexdigest()[:12]


def is_commitment(sentence):
    """一句話是不是 AI 自己開的承諾。

    排除四類：引號裡的觸發詞（覆述 owner 原話）、把主詞指給別人的覆述句、疑問句
    （問要不要做不是承諾）、已完成式（那是回報，不是待辦）。憑證形狀的句子比照
    capture 一律拒收——帳本是持久檔，之後還會被注入。"""
    text = _one_line(sentence)
    if not text:
        return False
    bare = memspec.GRANT_QUOTED_TEXT_REGEX.sub("", text)
    if not memspec.COMMITMENT_TRIGGER_REGEX.search(bare):
        return False
    if memspec.COMMITMENT_ATTRIBUTION_REGEX.search(text):
        return False
    if _is_question(text):
        return False
    if memspec.COMMITMENT_DONE_REGEX.search(text):
        return False
    if memspec.CAPTURE_REJECT_REGEX.search(text):
        return False
    return True


def extract(text):
    """回合結尾訊息裡的承諾句，依出現順序、去重、封頂。"""
    if not isinstance(text, str) or not text.strip():
        return []
    found = []
    seen = set()
    for match in _SENTENCE_REGEX.finditer(text[: memspec.STOP_GATE_MESSAGE_MAX_CHARS]):
        sentence = _one_line(match.group(0))
        if not is_commitment(sentence):
            continue
        sentence = sentence[: memspec.COMMITMENT_MAX_SENTENCE_CHARS]
        key = digest(sentence)
        if key in seen:
            continue
        seen.add(key)
        found.append(sentence)
        if len(found) >= memspec.COMMITMENT_MAX_PER_TURN:
            break
    return found


def _residue(text):
    """訊息裡「不是承諾」的部分。

    收尾比對只看這一段：同一則訊息若把上一輪的承諾原句再說一次，那是重申而非兌現，
    拿整段訊息比對會讓承諾自己把自己關掉。"""
    return "".join(
        match.group(0)
        for match in _SENTENCE_REGEX.finditer(str(text or "")[: memspec.STOP_GATE_MESSAGE_MAX_CHARS])
        if not is_commitment(_one_line(match.group(0)))
    )


def _fragment(text):
    """收尾比對的關鍵片段：句末標點先去掉，否則兌現時的回報句（「…的測試已完成」）
    永遠對不上帳本裡帶「。」的原句。"""
    core = _one_line(text).rstrip(memspec.COMMITMENT_SENTENCE_TERMINATORS + " ")
    return _normalized(core)[: memspec.COMMITMENT_SETTLE_PREFIX_CHARS]


def ledger_path(vault):
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY / memspec.COMMITMENT_LEDGER_FILENAME


def _rows(vault):
    """帳本現有列。壞行、壞檔、讀不到一律跳過：hook 不能被自己的帳本弄死。"""
    try:
        text = ledger_path(vault).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("digest"), str) and isinstance(row.get("text"), str):
            rows.append(row)
    return rows


def _encode(row):
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"


def _pruned(rows):
    """帳本封頂：open 全留（那是欠的帳），closed 只留最新的填到上限。"""
    if len(rows) <= memspec.COMMITMENT_LEDGER_MAX_ROWS:
        return rows
    still_open = [row for row in rows if row.get("status") == memspec.COMMITMENT_OPEN_STATUS]
    room = max(0, memspec.COMMITMENT_LEDGER_MAX_ROWS - len(still_open))
    closed = [row for row in rows if row.get("status") != memspec.COMMITMENT_OPEN_STATUS][-room:] if room else []
    keep = {id(row) for row in still_open} | {id(row) for row in closed}
    return [row for row in rows if id(row) in keep]


def _stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _session(session_id):
    return _one_line(session_id) or "nosession"


def _write_all(target, rows):
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temporary.write_bytes("".join(_encode(row) for row in rows).encode("utf-8"))
    os.replace(temporary, target)


def record(vault, session_id, sentences, timeout=memspec.COMMITMENT_LOCK_SECONDS):
    """新承諾入帳，回傳實際寫入的 digest。同 session 同 digest 不重複記。"""
    candidates = [item for item in (sentences or ()) if isinstance(item, str) and item.strip()]
    if not candidates:
        return []
    target = ledger_path(vault)
    written = []
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with memspec.file_lock(target, timeout) as locked:
            if not locked:
                return []
            rows = _rows(vault)
            session = _session(session_id)
            existing = {(row.get("session"), row.get("digest")) for row in rows}
            fresh = []
            stamp = _stamp()
            for sentence in candidates[: memspec.COMMITMENT_MAX_PER_TURN]:
                text = _one_line(sentence)[: memspec.COMMITMENT_MAX_SENTENCE_CHARS]
                key = digest(text)
                if not text or (session, key) in existing:
                    continue
                existing.add((session, key))
                fresh.append({
                    "ts": stamp,
                    "session": session,
                    "digest": key,
                    "text": text,
                    "status": memspec.COMMITMENT_OPEN_STATUS,
                })
                written.append(key)
            if not fresh:
                return []
            combined = rows + fresh
            kept = _pruned(combined)
            if len(kept) != len(combined):
                _write_all(target, kept)
            else:
                with target.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write("".join(_encode(row) for row in fresh))
    except OSError:
        return written
    return written


def _close_rows(vault, session_id, hits, timeout):
    """鎖內重讀後把命中的 open 列標 closed。回傳真正關掉的 digest。"""
    target = ledger_path(vault)
    closed = []
    try:
        with memspec.file_lock(target, timeout) as locked:
            if not locked:
                return []
            rows = _rows(vault)
            stamp = _stamp()
            for row in rows:
                if row.get("status") != memspec.COMMITMENT_OPEN_STATUS or row.get("digest") not in hits:
                    continue
                row["status"] = memspec.COMMITMENT_CLOSED_STATUS
                row["closed_at"] = stamp
                row["closed_by"] = _session(session_id)
                closed.append(row["digest"])
            if closed:
                _write_all(target, _pruned(rows))
    except OSError:
        return closed
    return closed


def settle(vault, session_id, text):
    """回合結尾訊息裡的完成訊號 → 標 closed，回傳被收尾的 digest。

    命中兩種：訊息直接寫出 digest（CLI／模型自報），或承諾原句的前
    COMMITMENT_SETTLE_PREFIX_CHARS 字出現在訊息「非承諾」的那一段（把事情做完之後
    的回報必然重述那件事）。重申承諾不算兌現，見 ``_residue``。"""
    if not isinstance(text, str) or not text.strip():
        return []
    open_rows = [row for row in _rows(vault) if row.get("status") == memspec.COMMITMENT_OPEN_STATUS]
    if not open_rows:
        return []
    haystack = _normalized(_residue(text))
    lowered = text.casefold()
    hits = set()
    for row in open_rows:
        key = row.get("digest")
        if not key:
            continue
        if key.casefold() in lowered:
            hits.add(key)
            continue
        fragment = _fragment(row.get("text"))
        if fragment and fragment in haystack:
            hits.add(key)
    if not hits:
        return []
    return _close_rows(vault, session_id, hits, memspec.COMMITMENT_LOCK_SECONDS)


def close(vault, digests, session_id="cli"):
    """CLI 收尾：照 digest 關帳。"""
    wanted = {item for item in (digests or ()) if isinstance(item, str) and item}
    if not wanted:
        return []
    return _close_rows(vault, session_id, wanted, memspec.COMMITMENT_LOCK_SECONDS)


def purge_closed(vault):
    """把 closed 列丟掉，回傳丟掉幾列（None＝沒動到檔）。"""
    target = ledger_path(vault)
    try:
        with memspec.file_lock(target, memspec.COMMITMENT_LOCK_SECONDS) as locked:
            if not locked:
                return None
            rows = _rows(vault)
            kept = [row for row in rows if row.get("status") == memspec.COMMITMENT_OPEN_STATUS]
            if len(kept) == len(rows):
                return 0
            _write_all(target, kept)
            return len(rows) - len(kept)
    except OSError:
        return None


def open_items(vault, limit=memspec.COMMITMENT_SESSIONSTART_MAX):
    """還沒兌現的承諾，最新在前。帳本是追加寫，尾端就是最新。"""
    rows = [row for row in _rows(vault) if row.get("status") == memspec.COMMITMENT_OPEN_STATUS]
    rows.reverse()
    return rows if limit is None or limit <= 0 else rows[:limit]


def summary_line(vaults, limit=memspec.COMMITMENT_SESSIONSTART_MAX):
    """SessionStart 的一行，沒有 open 承諾時 None。"""
    total = 0
    newest = None
    worst = None
    for vault in vaults or ():
        try:
            items = open_items(vault, limit)
        except OSError:
            continue
        if not items:
            continue
        total += len(items)
        if newest is None or _one_line(items[0].get("ts")) > _one_line(newest.get("ts")):
            newest, worst = items[0], vault
    if not total or newest is None:
        return None
    count = f"{limit}+" if total >= limit else str(total)
    excerpt = _one_line(newest.get("text"))[: memspec.COMMITMENT_SUMMARY_CHARS]
    return memspec.COMMITMENT_SESSIONSTART_LINE.format(count=count, excerpt=excerpt, vault=worst)


def snapshot_block(vault, limit=memspec.COMMITMENT_PRECOMPACT_MAX):
    """壓縮前快照要塞的那一段，沒有 open 承諾時空字串。"""
    items = open_items(vault, limit)
    if not items:
        return ""
    lines = [memspec.COMMITMENT_PRECOMPACT_HEADING]
    for row in items:
        text = _one_line(row.get("text"))[: memspec.COMMITMENT_MAX_SENTENCE_CHARS]
        lines.append(f"- {_one_line(row.get('ts'))} {row.get('digest')} {text}")
    return "\n".join(lines) + "\n"


def _print_report(vault, items, rows, output):
    for row in items:
        print(f"{row.get('digest')} {_one_line(row.get('ts'))} {_one_line(row.get('text'))}", file=output)
    closed = sum(1 for row in rows if row.get("status") != memspec.COMMITMENT_OPEN_STATUS)
    print(f"COMMITMENTS open={len(items)} closed={closed} ledger={ledger_path(vault)}", file=output)


def _selftest():
    import io
    import tempfile

    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-commit-") as temp_dir:
            vault = Path(temp_dir).resolve() / "vault"
            vault.mkdir()

            positives = (
                "我會在收工前補上 settle 的測試。",
                "我等一下把 stop_gate 的預算量一遍。",
                "稍後我再跑一次 exam 全集。",
                "下一步是把 CLI 子命令登記進 run_all。",
                "等 verifier 回報後我把 FAILURE_MODES 補完。",
                "等 owner 裁決後我會改成鏡像。",
                "I will re-run the privacy lint before reporting.",
                "Next I check the wall-time delta on a temp vault.",
                "After the exam finishes I'll paste the counts.",
            )
            hits = [sentence for sentence in positives if extract(sentence)]
            checks.append((
                "six-plus commitment shapes are extracted (Chinese and English)",
                len(hits) == len(positives),
            ))

            negatives = (
                "owner 說我會在收工前補上測試，但那是上週的事。",
                "我會不會先補測試？",
                "我已完成 settle 的測試，順手把 CLI 也補了。",
                "這支 hook 讀 config 找治理 vault，沒有其他來源。",
                "「我等一下把預算量一遍」是上一輪的原話。",
                "我不會在沒有證據的情況下宣稱通過。",
                "下一步由 owner 決定要不要上線。",
            )
            missed = [sentence for sentence in negatives if extract(sentence)]
            checks.append((
                "attribution, question, done-form, trigger-less, quoted and negated"
                " sentences are not commitments",
                not missed,
            ))

            turn = "我會補上 settle 的測試。順手改了註解。我會補上 settle 的測試。"
            checks.append((
                "one turn dedupes the same sentence and keeps the other text out",
                extract(turn) == ["我會補上 settle 的測試。"],
            ))

            first = record(vault, "s1", extract("我會補上 settle 的測試。"))
            again = record(vault, "s1", extract("我會補上 settle 的測試。"))
            other = record(vault, "s2", extract("我會補上 settle 的測試。"))
            checks.append((
                "the same digest is recorded once per session, and once more for a new session",
                len(first) == 1 and again == [] and len(other) == 1 and len(_rows(vault)) == 2,
            ))

            restated = settle(vault, "s1", "我會補上 settle 的測試。")
            checks.append((
                "restating the promise does not settle it",
                restated == [] and len(open_items(vault)) == 2,
            ))

            done = settle(vault, "s1", "我會補上 settle 的測試已完成，14/14 全過。")
            checks.append((
                "a completion report closes every matching open row",
                sorted(done) == sorted(first + other)
                and open_items(vault) == []
                and all(
                    row.get("closed_by") == "s1" and row.get("closed_at")
                    for row in _rows(vault)
                    if row.get("status") == memspec.COMMITMENT_CLOSED_STATUS
                ),
            ))

            record(vault, "s3", ["我會先做甲。"])
            record(vault, "s3", ["我會再做乙。"])
            record(vault, "s3", ["我會最後做丙。"])
            ordered = open_items(vault)
            checks.append((
                "open_items lists the newest first and honours its limit",
                [row["text"] for row in ordered] == ["我會最後做丙。", "我會再做乙。", "我會先做甲。"]
                and [row["text"] for row in open_items(vault, 1)] == ["我會最後做丙。"],
            ))

            line = summary_line([vault])
            checks.append((
                "the session-start line is one line naming the count and the newest excerpt",
                isinstance(line, str)
                and "\n" not in line
                and "承諾 3 條" in line
                and "我會最後做丙。" in line
                and str(vault) in line,
            ))
            checks.append((
                "an empty vault yields no line and no snapshot",
                summary_line([vault / "missing"]) is None and snapshot_block(vault / "missing") == "",
            ))
            snapshot = snapshot_block(vault)
            checks.append((
                "the pre-compaction snapshot carries the heading and the open rows",
                snapshot.startswith(memspec.COMMITMENT_PRECOMPACT_HEADING)
                and snapshot.count("\n- ") == 3
                and "我會最後做丙。" in snapshot,
            ))

            digested = digest("我會最後做丙。")
            closed_by_digest = close(vault, [digested])
            checks.append((
                "the CLI closes one row by digest and purge drops the closed rows",
                closed_by_digest == [digested]
                and purge_closed(vault) == 3
                and len(_rows(vault)) == 2,
            ))

            long_sentence = "我會" + ("補" * 400) + "。"
            recorded = record(vault, "s4", extract(long_sentence))
            stored = next(row for row in _rows(vault) if row["digest"] == recorded[0])
            checks.append((
                "an over-long sentence is truncated before it enters the ledger",
                len(stored["text"]) == memspec.COMMITMENT_MAX_SENTENCE_CHARS,
            ))

            checks.append((
                "credential-shaped promises are never stored",
                extract("我會把 api_key: abcdefghijklmnop 寫進設定。") == [],
            ))

            broken = Path(temp_dir).resolve() / "broken"
            (broken / memspec.FTS_INDEX_DIRECTORY).mkdir(parents=True)
            ledger_path(broken).write_text(
                "{not json\n"
                + json.dumps({"digest": "aaaaaaaaaaaa", "text": "我會做丁。", "status": "open"}, ensure_ascii=False)
                + "\n[]\n",
                encoding="utf-8",
            )
            checks.append((
                "a corrupt ledger fails open: bad lines are skipped, good ones still read",
                [row["digest"] for row in open_items(broken)] == ["aaaaaaaaaaaa"]
                and settle(broken, "s5", "沒有命中的訊息。") == [],
            ))

            out = io.StringIO()
            code = main([os.fspath(broken), "--list"], output=out)
            checks.append((
                "the CLI lists the ledger and reports the counts",
                code == 0 and "我會做丁。" in out.getvalue() and "COMMITMENTS open=1" in out.getvalue(),
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 15
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main(argv=None, output=sys.stdout):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    # argparse costs ~55 ms to import on this machine; the Stop hook imports this
    # module every turn and never reaches the CLI, so the parser stays deferred.
    import argparse

    parser = argparse.ArgumentParser(description="AI 承諾帳本：列出、收尾、清理。")
    parser.add_argument("vault", type=Path)
    parser.add_argument("--list", action="store_true", help="列出還沒兌現的承諾（預設）")
    parser.add_argument("--close", metavar="DIGEST", action="append", default=[], help="照 digest 標為 closed")
    parser.add_argument("--purge-closed", action="store_true", help="把 closed 列從帳本移除")
    parser.add_argument("--limit", type=int, default=0, help="最多列出幾條，0＝全部")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(arguments)
    vault = parsed.vault.expanduser()
    try:
        if parsed.close:
            closed = close(vault, parsed.close)
            print(f"CLOSED {len(closed)}/{len(parsed.close)} {' '.join(closed)}".rstrip(), file=output)
        if parsed.purge_closed:
            dropped = purge_closed(vault)
            print(f"PURGED {dropped if dropped is not None else 'skipped'}", file=output)
        items = open_items(vault, parsed.limit)
        if parsed.json:
            print(json.dumps(items, ensure_ascii=False, indent=1), file=output)
        else:
            _print_report(vault, items, _rows(vault), output)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
