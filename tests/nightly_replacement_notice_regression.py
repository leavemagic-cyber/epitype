import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""夜間同步換掉使用者的字時，不得靜悄悄。

同步在指紋紀錄不見時會覆蓋宿主檔索引區塊裡認不出來的內容——這個取捨是刻意的（不然
解除安裝過一次就再也同步不回來）。代價是推錯時消失的是使用者的字，所以三條路徑都要
當場講出來。

安裝器與手動跑 `sync` 印在 stdout 上，有人看著。夜間這條把輸出收進 StringIO，只撈
`WROTE` 開頭的行——那句話寫進去就被丟掉了。結果是：使用者的字消失、備份裡沒有（備份
留的是第一次動它之前那一份）、夜報零錯誤、沒有任何地方提過發生什麼事。

唯一沒人看著的那條路徑，正是最需要講的那條。
"""

import io
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


def main():
    checks = []
    index_begin, index_end = memspec.HOST_SYNC_MARKERS[memspec.HOST_SYNC_INDEX_REGION]
    today = date(2026, 9, 17)

    with tempfile.TemporaryDirectory(prefix="epitype-nightly-notice-") as temp_dir:
        root = Path(temp_dir).resolve()
        home, vault, host_path = _fixture(
            root,
            f"我的開頭\n\n{index_begin}\n我寫的第一行\n我寫的第二行\n我寫的第三行\n{index_end}\n",
        )
        original_home = Path.home
        try:
            Path.home = staticmethod(lambda: home)
            section = dream._section_host_sync([vault], today, today, config={})
        finally:
            Path.home = original_home

        after = host_path.read_text(encoding="utf-8")
        replaced_lines = [
            line for line in section["errors"]
            if line.startswith(memspec.HOST_SYNC_REPLACED_PREFIX)
        ]
        checks.append((
            "夜間真的換掉了使用者的三行（前提成立，不是測到一個沒發生的情況）",
            "我寫的第一行" not in after and "我的開頭" in after,
        ))
        checks.append((
            "換掉的事實出現在夜報，而且算成錯誤：這一晚不能被當成乾淨跑完",
            bool(replaced_lines) and any("3 行" in line for line in replaced_lines),
        ))
        checks.append((
            "夜報講得出是哪一塊、原檔在哪",
            any(memspec.HOST_SYNC_INDEX_REGION in line for line in replaced_lines)
            and any(memspec.HOST_SYNC_BACKUP_SUFFIX in line for line in replaced_lines),
        ))
        # 這一場真的有兩塊漂移（規則塊還沒建立、索引塊與卡片不一致），加上一行 REPLACE
        # 通知，報告總共三行非空行。舊寫法數的是「所有非空行」，所以會報 3。
        checks.append((
            "漂移只數 DRIFT 行，不把取代通知也算成一次漂移",
            section["counts"]["drifted"] == 2
            and section["counts"]["replaced"] == len(replaced_lines) == 1,
        ))

    # 沒有東西被換掉的正常夜晚，不得冒出這種錯誤——不然它會變成每晚都有的噪音，
    # 而每晚都有的警告等於沒有警告。
    with tempfile.TemporaryDirectory(prefix="epitype-nightly-quiet-") as temp_dir:
        root = Path(temp_dir).resolve()
        home, vault, _host_path = _fixture(root, "我的開頭\n")
        original_home = Path.home
        try:
            Path.home = staticmethod(lambda: home)
            first = dream._section_host_sync([vault], today, today, config={})
            again = dream._section_host_sync([vault], today, today, config={})
        finally:
            Path.home = original_home
        checks.append((
            "沒有東西被換掉的夜晚安安靜靜，零錯誤",
            first["errors"] == [] and again["errors"] == []
            and again["counts"]["drifted"] == 0,
        ))

    # 整個宿主停手（標記被改壞）時，不得先講一句沒發生的取代。
    with tempfile.TemporaryDirectory(prefix="epitype-nightly-damaged-") as temp_dir:
        root = Path(temp_dir).resolve()
        rules_begin, rules_end = memspec.HOST_SYNC_MARKERS[
            memspec.HOST_SYNC_RULES_REGION]
        home, vault, host_path = _fixture(
            root,
            f"{rules_begin}\nA\n{rules_end}\n{rules_begin}\nB\n{rules_end}\n"
            f"{index_begin}\n我寫的一行\n{index_end}\n",
        )
        before_bytes = host_path.read_bytes()
        report = io.StringIO()
        host_sync.apply([vault], hosts=["claude"], home=home, output=report)
        said = report.getvalue()
        checks.append((
            "整個宿主停手時不先講一句沒發生的取代",
            memspec.HOST_SYNC_REPLACED_PREFIX not in said
            and "REFUSE" in said
            and host_path.read_bytes() == before_bytes,
        ))

    # 規則塊是使用者自己的字 → 夜間拒絕、不呼叫 apply；索引塊的取代今晚不會發生，不得報成錯誤。
    with tempfile.TemporaryDirectory(prefix="epitype-nightly-refused-") as temp_dir:
        root = Path(temp_dir).resolve()
        rules_begin, rules_end = memspec.HOST_SYNC_MARKERS[memspec.HOST_SYNC_RULES_REGION]
        home, vault, host_path = _fixture(
            root,
            f"{rules_begin}\n使用者自己的規則\n{rules_end}\n"
            f"{index_begin}\n我寫的一行\n{index_end}\n",
        )
        before_bytes = host_path.read_bytes()
        original_home = Path.home
        try:
            Path.home = staticmethod(lambda: home)
            refused = dream._section_host_sync([vault], today, today, config={})
        finally:
            Path.home = original_home
        checks.append((
            "拒絕寫入的夜晚不把沒發生的取代報成錯誤，檔案一個位元組不動",
            refused["errors"] == [] and host_path.read_bytes() == before_bytes,
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
