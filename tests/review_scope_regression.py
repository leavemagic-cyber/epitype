import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""夜報只數需要判的東西。

2026-09-18 真庫實測：待判 94 列裡 80 列只是「這張卡身上掛著 owner 原話」，什麼都沒發生；
草稿待審 555 份裡 360 份是 09-09 判完標成 retired 的；拆卡候選 77 張裡 48 張是紀錄型卡片
（專案進度、參考資料），長是它們的功能。三個數字都把已經判過或不必判的東西算進待辦，
門檻因此永遠成立，而永遠成立的門檻等於沒有門檻。
"""

import json
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epitype import dream, memspec

TODAY = date(2026, 9, 18)


def _vault(root):
    vault = root / "vault"
    vault.mkdir()
    (vault / memspec.WORK_LEDGER_FILENAME).write_text("# 帳本\n", encoding="utf-8")
    return vault


def _block(card, session, digest, day="2026-09-17"):
    return json.dumps({
        "timestamp": f"{day}T02:00:00+00:00",
        "kind": memspec.STOP_GATE_LOG_KIND,
        "rule": "forbidden",
        "card": card,
        "session_id": session,
        "digest": digest,
    }, ensure_ascii=False)


def _gate_log(vault, lines):
    (vault / memspec.GATE_LOG_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pack(vault):
    sections = [{"id": index, "counts": {}} for index in (8, 9, 10, 11, 12)]
    return dream._section_review_pack([vault], TODAY, TODAY, {}, sections)


def _rows(result):
    return {item["card"]: item for item in result["examples"]}


def main():
    checks = []

    # 擋下：同一張卡兩場獨立擋下＝待判；一場裡多句各擋一次＝機器擋到了，背景。
    with tempfile.TemporaryDirectory(prefix="epitype-review-blocks-") as temp_dir:
        vault = _vault(Path(temp_dir).resolve())
        _gate_log(vault, [
            _block("two-sessions", "s1", "d1"),
            _block("two-sessions", "s2", "d2"),
            _block("one-session", "s3", "d3"),
            _block("one-session", "s3", "d4"),
            _block("one-session", "s3", "d5"),
            _block("same-sentence", "s4", "d6"),
            _block("same-sentence", "s4", "d6"),
        ])
        result = _pack(vault)
        rows = _rows(result)
        checks.append((
            "兩場獨立擋下＝待判；一場裡擋下三句不同的話＝背景",
            rows["two-sessions"]["judge"] is True
            and rows["one-session"]["judge"] is False
            and rows["one-session"]["blocks"] == 3,
        ))
        checks.append((
            "同一句被擋兩次＝待判（擋了但行為沒改），並記在 repeat_blocks",
            rows["same-sentence"]["judge"] is True
            and rows["same-sentence"]["repeat_blocks"] == 1,
        ))
        checks.append((
            "待判數只算待判的卡，其餘進背景數字",
            result["counts"]["review_items"] == 2
            and result["counts"]["background_cards"] == 1
            and result["counts"]["cards"] == 3,
        ))

    # 事件：卡上掛著 owner 原話但什麼都沒發生 → 背景，不是待判。
    with tempfile.TemporaryDirectory(prefix="epitype-review-events-") as temp_dir:
        vault = _vault(Path(temp_dir).resolve())
        (vault / memspec.RULING_DIRECTORY).mkdir()
        (vault / memspec.RULING_DIRECTORY / "ruling-20260917-aaaa.md").write_text(
            "---\nname: ruling-20260917-aaaa\ndescription: owner ruling auto-captured\n"
            f"{memspec.CAPTURED_AT_FIELD}: 2026-09-17T00:00:00Z\n"
            f"{memspec.SESSION_FIELD}: s9\n"
            f"{memspec.CARRIED_BY_FIELD}: quoted-card\n---\n原話\n",
            encoding="utf-8")
        result = _pack(vault)
        rows = _rows(result)
        checks.append((
            "只被原話關聯到的卡不算待判，但仍列得出來",
            result["counts"]["review_items"] == 0
            and result["counts"]["background_cards"] == 1
            and rows["quoted-card"]["events"] + rows["quoted-card"]["unverified_events"] == 1,
        ))

    # 草稿：判過的（triaged 有值且不是 hold）不再算待審，但檔案還在。
    with tempfile.TemporaryDirectory(prefix="epitype-review-drafts-") as temp_dir:
        root = Path(temp_dir).resolve()
        vault = _vault(root)
        drafts = vault / "_drafts" / "decisions"
        drafts.mkdir(parents=True)
        old = datetime.now(timezone.utc) - timedelta(days=30)
        for name, front in (
            ("judged.md", f"{memspec.DRAFT_TRIAGED_FIELD}: retired\n"),
            ("held.md", f"{memspec.DRAFT_TRIAGED_FIELD}: hold\n"),
            ("fresh.md", ""),
        ):
            path = drafts / name
            path.write_text(f"---\nname: {name[:-3]}\ndescription: 測試草稿\n{front}---\n內文\n",
                            encoding="utf-8")
            stamp = old.timestamp()
            import os
            os.utime(path, (stamp, stamp))
        waiting, judged = dream._drafts_of(vault)
        section = dream._section_drafts([vault], TODAY, TODAY, {}, context={"home": None})
        aging = dream._section_draft_aging([vault], TODAY, TODAY, {})
        checks.append((
            "判過的草稿不算待審，檔案照留，數字另外報",
            len(waiting) == 2 and judged == 1
            and section["counts"]["total_drafts"] == 2
            and section["counts"]["judged_kept"] == 1
            and (drafts / "judged.md").is_file(),
        ))
        checks.append((
            "草稿老化也只看待審的那些",
            aging["counts"]["total_drafts"] == 2 and aging["counts"]["over_7_days"] == 2,
        ))

    # 拆卡候選：規範型卡才列，紀錄型不列。
    with tempfile.TemporaryDirectory(prefix="epitype-review-mixed-") as temp_dir:
        vault = _vault(Path(temp_dir).resolve())
        # 2026-09-19：小標數不再是拆卡訊號（收窄兩輪後真庫 27 張仍有 25 張誤判），
        # 樣本改用還在的訊號——正文超過上限。這一項要驗的「規範型才列、紀錄型不列」沒變。
        body = "## 第一段\n" + ("長" * (memspec.CARD_BODY_MIXED_BYTES // 3 + 10)) + "\n"
        (vault / "feedback-mixed.md").write_text(
            "---\nname: feedback-mixed\ndescription: 兩件事的行為卡\nmetadata:\n"
            f"  type: {memspec.CARD_TYPE_FEEDBACK}\n---\n{body}", encoding="utf-8")
        (vault / "project-log.md").write_text(
            "---\nname: project-log\ndescription: 專案流水帳\nmetadata:\n"
            f"  type: {memspec.CARD_TYPE_PROJECT}\n---\n{body}", encoding="utf-8")
        mixed = dream._section_mixed_cards([vault], TODAY, TODAY, {})
        listed = {Path(item["path"]).name for item in mixed["examples"]}
        checks.append((
            "行為卡列成拆卡候選，專案紀錄卡不列",
            mixed["counts"]["mixed_cards"] == 1
            and "feedback-mixed.md" in listed and "project-log.md" not in listed
            and mixed["counts"]["skipped_by_reason"].get(dream.MIXED_SKIP_RECORD_TYPE) == 1,
        ))

    passed = sum(bool(ok) for _, ok in checks)
    total = 7
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    for name, ok in checks:
        if not ok:
            print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
