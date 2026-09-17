"""喚回的自我修正：一再端出卻從沒被用到的資訊型卡，暫時不再端出。

喚回是 Epitype 最大的 token 開銷——注入之後每一次呼叫都要再讀一次。夜裡從對話紀錄數
每張卡「在幾段對話裡被端出」與「端出之後有沒有被讀或被提到」，只有資訊型卡
（`memspec.RECALL_QUIET_TYPES`）才會被靜音；裁定、規則、傷疤、行為卡一律不動，因為它們
沒被點名不代表沒起作用。視窗是滾動的，靜音的卡不再累積端出次數，過了視窗自然回來重評。
"""

import io
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epitype import memspec

CARD_LINE = re.compile(r"^- .*\| V\d+/(\S+?\.md)[ \t]*$", re.M)


def health_path(vault):
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY / memspec.RECALL_HEALTH_FILENAME


def _rows(path, max_bytes):
    try:
        with io.open(path, "rb") as stream:
            data = stream.read(max_bytes)
    except OSError:
        return
    for raw in data.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def _assistant_text(row):
    parts = []
    for block in (row.get("message") or {}).get("content") or ():
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            parts.append(json.dumps(block.get("input") or {}, ensure_ascii=False))
    return "\n".join(parts)


def measure(transcripts, since=None, max_bytes=64 * 1024 * 1024):
    """{卡片相對路徑: [端出的段數, 其中之後被用到的段數]}。

    `since`（YYYY-MM-DD）只限制「端出」那一列的日期：長壽的對話檔每晚都會被重讀，不限的
    話同一次端出會被算好幾晚。「用到」＝同一段裡端出之後，回覆或工具呼叫提到了卡名。
    """
    counts = {}
    for path in transcripts:
        segment = 0
        pending = {}      # (段, 卡) -> 還沒看到被用到
        for row in _rows(path, max_bytes):
            if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                segment += 1
                pending = {key: used for key, used in pending.items() if key[0] == segment}
                continue
            attachment = row.get("attachment") if row.get("type") == "attachment" else None
            if isinstance(attachment, dict) and attachment.get("type") == "hook_additional_context":
                if since and str(row.get("timestamp", ""))[:10] < since:
                    continue
                for text in attachment.get("content") or ():
                    if not isinstance(text, str):
                        continue
                    for card in CARD_LINE.findall(text):
                        key = (segment, card)
                        if key in pending:
                            continue
                        pending[key] = False
                        counts.setdefault(card, [0, 0])[0] += 1
                continue
            if row.get("type") != "assistant" or not pending:
                continue
            said = _assistant_text(row)
            for key, used in pending.items():
                if not used and Path(key[1]).stem in said:
                    pending[key] = True
                    counts[key[1]][1] += 1
    return counts


def _card_type(vault, card):
    from epitype import card_lint

    path = Path(vault) / card
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    card_type, fields = card_lint.card_type_of(card, text, path)
    if any(field in fields for field in memspec.CARD_GATE_FIELDS):
        return None  # 武裝的卡是行為卡，不論自報什麼型別
    return card_type


def update(vault, counts, today):
    """把今晚的數字寫進這個庫的健康檔，重算靜音名單並回傳它。只收這個庫裡真的有的卡。"""
    from epitype.host_sync import atomic_write

    vault = Path(vault)
    stamp = today.isoformat() if isinstance(today, date) else str(today)
    state = _load(vault)
    days = state.get("days", {})
    days[stamp] = {card: pair for card, pair in counts.items() if (vault / card).is_file()}
    oldest = (date.fromisoformat(stamp) - timedelta(days=memspec.RECALL_QUIET_WINDOW_DAYS)).isoformat()
    days = {day: value for day, value in days.items() if day > oldest}

    totals = {}
    for value in days.values():
        for card, (shown, used) in value.items():
            total = totals.setdefault(card, [0, 0])
            total[0] += shown
            total[1] += used
    quiet = sorted(
        card for card, (shown, used) in totals.items()
        if shown >= memspec.RECALL_QUIET_MIN_SEGMENTS and used == 0
        and _card_type(vault, card) in memspec.RECALL_QUIET_TYPES
    )
    path = health_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({"days": days, "quiet": quiet}, ensure_ascii=False).encode("utf-8"))
    return quiet


def _load(vault):
    try:
        with io.open(health_path(vault), encoding="utf-8") as stream:
            value = json.loads(stream.read())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("days"), dict):
        return {}
    days = {}
    for day, cards in value["days"].items():
        if not isinstance(day, str) or not isinstance(cards, dict):
            continue
        days[day] = {
            card: [int(pair[0]), int(pair[1])] for card, pair in cards.items()
            if isinstance(card, str) and isinstance(pair, list) and len(pair) == 2
            and all(isinstance(n, int) and not isinstance(n, bool) for n in pair)
        }
    quiet = value.get("quiet")
    return {"days": days, "quiet": quiet if isinstance(quiet, list) else []}


def quiet_cards(vault):
    """喚回要略過的卡。讀不到、格式不對一律回空集合：寧可多端，不可因壞檔少端。"""
    return frozenset(card for card in _load(vault).get("quiet", ()) if isinstance(card, str))


def _selftest():
    import tempfile

    checks = []
    with tempfile.TemporaryDirectory(prefix="epitype-recall-quiet-") as temp_dir:
        root = Path(temp_dir).resolve()
        vault = root / "vault"
        vault.mkdir()
        for name, card_type, extra in (("project-a.md", "project", ""), ("feedback-b.md", "feedback", ""),
                                       ("reference-c.md", "reference", ""),
                                       ("reference-armed.md", "reference", "forbidden: [壞話]\n")):
            (vault / name).write_text(
                f"---\nname: {name[:-3]}\ndescription: d\n{extra}metadata:\n  type: {card_type}\n---\nbody\n",
                encoding="utf-8")

        def injection(stamp, *cards):
            lines = "\n".join(f"- x | V1/{card}" for card in cards)
            return {"type": "attachment", "timestamp": stamp, "attachment": {
                "type": "hook_additional_context", "content": [f"vaults: V1={vault}\n{lines}"]}}

        def said(text):
            return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

        rows = []
        for index in range(memspec.RECALL_QUIET_MIN_SEGMENTS):
            rows.append(injection("2026-09-17T01:00:00Z", "project-a.md", "feedback-b.md",
                                  "reference-c.md", "reference-armed.md"))
            rows.append(injection("2026-09-17T01:00:00Z", "project-a.md"))  # 同一段重複不算第二次
            if index == 0:
                rows.append(said("看一下 reference-c 那張"))
            rows.append({"type": "system", "subtype": "compact_boundary"})
        transcript = root / "t.jsonl"
        transcript.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

        counts = measure([transcript], since="2026-09-17")
        checks.append(("每段只算一次端出", counts.get("project-a.md") == [memspec.RECALL_QUIET_MIN_SEGMENTS, 0]))
        checks.append(("端出之後被提到才算用到", counts.get("reference-c.md") == [memspec.RECALL_QUIET_MIN_SEGMENTS, 1]))
        checks.append(("視窗之前的端出不算", measure([transcript], since="2026-09-18") == {}))

        quiet = update(vault, counts, date(2026, 9, 17))
        checks.append(("只有從沒用到、沒武裝的資訊型卡被靜音", quiet == ["project-a.md"]))
        checks.append(("行為卡再沒用到也不靜音", "feedback-b.md" not in quiet_cards(vault)))
        checks.append(("喚回讀得到靜音名單", quiet_cards(vault) == frozenset({"project-a.md"})))

        later = date(2026, 9, 17) + timedelta(days=memspec.RECALL_QUIET_WINDOW_DAYS + 1)
        checks.append(("過了視窗自動回來重評", update(vault, {}, later) == []))

        update(vault, {"project-a.md": [99, 0], "gone.md": [99, 0]}, date(2026, 9, 17))
        checks.append(("庫裡沒有的卡不記", "gone.md" not in json.loads(health_path(vault).read_text(encoding="utf-8"))["days"]["2026-09-17"]))

        health_path(vault).write_text("{broken", encoding="utf-8")
        checks.append(("健康檔壞掉就不靜音任何卡", quiet_cards(vault) == frozenset()))

    passed = sum(bool(ok) for _, ok in checks)
    total = 9
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    for name, ok in checks:
        if not ok:
            print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        getattr(_stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace")
    if sys.argv[1:] == ["--selftest"]:
        raise SystemExit(_selftest())
    raise SystemExit("usage: python -m epitype.recall_quiet --selftest")
