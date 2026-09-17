import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""夜間上限檢查的生成塊漂移，只拿治理庫的規則比。

生成塊是跨專案契約，`epitype sync` 只從治理庫組（專案規則不升格成全域）。夜間第 11 節
卻拿全部登記庫組來比：只要有一個專案庫帶規則卡，每晚都報「生成塊漂移」，照指示重生
又會把專案規則混進契約。上限的量法也要與 core-gen、sync 一樣量單一宿主載入的量。
"""

import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epitype import core_gen, dream, memspec


def _rule(name, layer, text):
    return (
        f"---\nname: {name}\ndescription: 2026-09-17 測試規則\nlayer: {layer}\n"
        f"section: evidence\norder: 10\ntext: {text}\n"
        "decided_by: owner-explicit\napproved_by: owner\napproved_at: 2026-09-17\n"
        "aliases: [測試]\nmetadata:\n  type: rule\n---\nbody\n"
    )


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix="epitype-core-drift-") as temp_dir:
        root = Path(temp_dir).resolve()
        governance = root / "governance"
        governance.mkdir()
        (governance / memspec.WORK_LEDGER_FILENAME).write_text("# 帳本\n", encoding="utf-8")
        (governance / "rule-a.md").write_text(
            _rule("rule-a", "floor", "Honesty governs this contract."), encoding="utf-8")
        project = root / "project"
        project.mkdir()
        (project / "rule-p.md").write_text(
            _rule("rule-p", "resident", "Project scoped rule."), encoding="utf-8")
        out = root / "CORE_GENERATED.md"
        core_gen.generate([governance], out, config={})

        checks.append((
            "前提：拿兩個庫組出來的塊確實與只用治理庫的不同",
            core_gen.drifted([governance, project], out),
        ))
        checks.append((
            "登記了帶規則卡的專案庫，夜間仍不報生成塊漂移",
            dream._core_drift([governance, project], [out]) == [],
        ))
        (governance / "rule-b.md").write_text(
            _rule("rule-b", "floor", "A new floor sentence."), encoding="utf-8")
        checks.append((
            "治理庫的規則真的改了，照樣報漂移",
            len(dream._core_drift([governance, project], [out])) == 1,
        ))
        text = out.read_text(encoding="utf-8")
        checks.append((
            "上限量的是單一宿主載入的量，與 core-gen 一致",
            dream._core_file_loaded_bytes(out) == max(core_gen.host_loads(text).values()),
        ))

    passed = sum(bool(ok) for _, ok in checks)
    total = 4
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    for name, ok in checks:
        if not ok:
            print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
