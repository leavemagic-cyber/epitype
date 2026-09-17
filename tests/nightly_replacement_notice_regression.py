import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""夜間同步換掉認不出的字時：原文要留得住，而且寫明是誰換的。

同步會取代宿主檔區塊裡認不出是 Epitype 寫的內容（不然規則永遠到不了代理面前）。代價是
那可能是使用者的字，所以取代前原文附加存進宿主檔旁的紀錄檔，每筆寫明取代者與時間；
夜報也要列出這件事。owner 2026-09-17：「換掉就是標註誰做得就好了」。
"""

import io
import os
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epitype import dream, host_sync, memspec

RULE_CARD = (
    "---\nname: rule-a\ndescription: 2026-09-17 一條底線\nlayer: floor\n"
    "section: evidence\norder: 10\ntext: Honesty governs this contract.\n"
    "decided_by: owner-explicit\napproved_by: owner\napproved_at: 2026-09-17\n"
    "aliases: [誠實]\nmetadata:\n  type: rule\n---\nbody\n"
)
TODAY = date(2026, 9, 17)


def _fixture(root, host_body):
    """一個治理庫加一個宿主檔，指紋紀錄刻意不存在（解除安裝過就是這個狀態）。"""
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    vault = root / "vault"
    vault.mkdir()
    (vault / "rule-a.md").write_text(RULE_CARD, encoding="utf-8")
    (vault / memspec.WORK_LEDGER_FILENAME).write_text("# 帳本\n", encoding="utf-8")
    (vault / memspec.HOST_SYNC_INDEX_FILENAME).write_text(
        "# 入口\n\n- [一張卡](a.md)\n", encoding="utf-8"
    )
    host_path = host_sync.host_path("claude", home)
    host_path.write_text(host_body, encoding="utf-8")
    state = host_sync._state_path(home)
    if state.exists():
        state.unlink()
    return home, vault, host_path


def _night(home, vault, config_text=None):
    """跑一次夜間第 14 節；家目錄與設定都指到臨時目錄。"""
    saved = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
    config = home.parent / "config.json"
    config.write_text(config_text or "{}", encoding="utf-8")
    original_home = Path.home
    try:
        os.environ[memspec.EPITYPE_CONFIG_ENV] = str(config)
        Path.home = staticmethod(lambda: home)
        return dream._section_host_sync([vault], TODAY, TODAY, config={})
    finally:
        Path.home = original_home
        if saved is None:
            os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None)
        else:
            os.environ[memspec.EPITYPE_CONFIG_ENV] = saved


def main():
    checks = []
    index_begin, index_end = memspec.HOST_SYNC_MARKERS[memspec.HOST_SYNC_INDEX_REGION]
    rules_begin, rules_end = memspec.HOST_SYNC_MARKERS[memspec.HOST_SYNC_RULES_REGION]

    # 指紋紀錄不在、索引塊裡是認不出的三行。
    with tempfile.TemporaryDirectory(prefix="epitype-nightly-notice-") as temp_dir:
        root = Path(temp_dir).resolve()
        home, vault, host_path = _fixture(
            root,
            f"我的開頭\n\n{index_begin}\n我寫的第一行\n我寫的第二行\n我寫的第三行\n{index_end}\n",
        )
        section = _night(home, vault)
        after = host_path.read_text(encoding="utf-8")
        log_path = host_path.with_name(host_path.name + memspec.HOST_SYNC_REPLACED_SUFFIX)
        log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        replaced_lines = [
            line for line in section["examples"]
            if line.startswith(memspec.HOST_SYNC_REPLACED_PREFIX)
        ]
        checks.append((
            "夜間真的換掉了那三行（前提成立），區塊外的字原樣留著",
            "我寫的第一行" not in after and "我的開頭" in after,
        ))
        checks.append((
            "原文三行都存進宿主檔旁的紀錄檔，寫明是夜間同步換的",
            all(text in log for text in ("我寫的第一行", "我寫的第二行", "我寫的第三行"))
            and memspec.HOST_SYNC_ACTOR_NIGHTLY in log,
        ))
        checks.append((
            "夜報列出取代：幾行、哪一塊、誰換的、原文在哪，但不算錯誤",
            len(replaced_lines) == 1 and "3 行" in replaced_lines[0]
            and memspec.HOST_SYNC_INDEX_REGION in replaced_lines[0]
            and memspec.HOST_SYNC_ACTOR_NIGHTLY in replaced_lines[0]
            and str(log_path) in replaced_lines[0]
            and section["counts"]["replaced"] == 1 and section["errors"] == [],
        ))
        checks.append((
            "漂移只數 DRIFT 行，不把取代通知也算成一次漂移",
            section["counts"]["drifted"] == 2,
        ))

    # 沒有東西被換掉的正常夜晚：不出取代紀錄、零錯誤。
    with tempfile.TemporaryDirectory(prefix="epitype-nightly-quiet-") as temp_dir:
        root = Path(temp_dir).resolve()
        home, vault, host_path = _fixture(root, "我的開頭\n")
        first = _night(home, vault)
        again = _night(home, vault)
        log_path = host_path.with_name(host_path.name + memspec.HOST_SYNC_REPLACED_SUFFIX)
        checks.append((
            "沒有東西被換掉的夜晚安安靜靜：零錯誤、沒有取代紀錄",
            first["errors"] == [] and again["errors"] == []
            and again["counts"]["drifted"] == 0 and not log_path.exists(),
        ))

    # 標記壞掉：整個宿主一個位元組都不動，拒寫要報。
    with tempfile.TemporaryDirectory(prefix="epitype-nightly-damaged-") as temp_dir:
        root = Path(temp_dir).resolve()
        home, vault, host_path = _fixture(
            root,
            f"{rules_begin}\nA\n{rules_end}\n{rules_begin}\nB\n{rules_end}\n"
            f"{index_begin}\n我寫的一行\n{index_end}\n",
        )
        before_bytes = host_path.read_bytes()
        night = _night(home, vault)
        checks.append((
            "標記壞掉：檔案一個位元組都不動，拒寫進 errors 與下一步",
            host_path.read_bytes() == before_bytes
            and any(line.startswith("REFUSE") for line in night["errors"])
            and not night["counts"].get("replaced")
            and any("拒寫" in step for step in dream._next_steps(
                [{"id": 14, "counts": night["counts"]}])),
        ))

    # 規則超過設定的 core_cap_bytes：規則塊拒寫，索引照寫（不是整晚跳過）。
    with tempfile.TemporaryDirectory(prefix="epitype-nightly-capped-") as temp_dir:
        root = Path(temp_dir).resolve()
        home, vault, host_path = _fixture(root, "我的開頭\n")
        capped = _night(home, vault, '{"core_cap_bytes": 10}')
        written = host_path.read_text(encoding="utf-8")
        checks.append((
            "規則超上限的夜晚：索引照寫、規則塊不寫、拒寫講出上限",
            "一張卡" in written and rules_begin not in written
            and any("core_cap_bytes" in line for line in capped["errors"]),
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
