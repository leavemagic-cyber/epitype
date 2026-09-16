import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""引用豁免的計數在多個庫時不得漏算。

這一版對「短句加引號繞過 Stop 閘」的答案不是擋掉它——機器分不出引用與違規。答案是
「分不出來至少數得出來」：閘每豁免一次寫一列稽核，夜間把它數成 `quoted_exemptions`。
那個數字就是這個取捨唯一的憑據，所以它算錯的嚴重性跟漏擋同級：使用者看到 2 會以為
今天只被繞過兩次，實際是五次，而且看不出差別。

原本的寫法是每個庫 `dict.update`，同名卡在後一個庫的數字會把前一個庫的整份蓋掉。
"""

import json
import os
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epitype import dream, memspec


def _vault(projects, slug, rows):
    """做一個住在 `.claude/projects/<slug>/memory` 底下的庫，帶自己的閘稽核帳。

    夢的第 13 節只認名字叫 `projects`、其父目錄叫 `.claude` 的祖先目錄，所以這個假樹
    不碰真機的家目錄也能走到真正的逐庫迴圈。
    """
    vault = projects / slug / "memory"
    vault.mkdir(parents=True)
    (vault / memspec.GATE_LOG_FILENAME).write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return vault


def _masked(card, stamp):
    return {
        "timestamp": stamp,
        "kind": memspec.STOP_GATE_MASKED_LOG_KIND,
        "decision": card,
        "rule": "quoted",
        "fragment": "片段",
        "digest": stamp,
    }


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix="epitype-quotecount-") as temp_dir:
        root = Path(temp_dir).resolve()
        projects = root / ".claude" / "projects"
        projects.mkdir(parents=True)

        # 同一張卡在兩個庫都有稽核列：三次 + 兩次。逐鍵累加要得到五。
        day = "2026-09-17"
        shared = "decisions/no-hedged-completion.md"
        vault_a = _vault(projects, "proj-a", [
            _masked(shared, f"{day}T01:00:0{n}+00:00") for n in range(3)
        ])
        vault_b = _vault(projects, "proj-b", [
            _masked(shared, f"{day}T02:00:0{n}+00:00") for n in range(2)
        ] + [_masked("decisions/only-here.md", f"{day}T03:00:00+00:00")])

        today = date(2026, 9, 17)
        section = dream._section_compliance(
            [vault_a, vault_b], today, today, config={}
        )
        counts = section["counts"]

        checks.append((
            "兩個庫的同名卡逐鍵累加，不是後蓋前",
            counts.get("quoted_exemptions") == 6,
        ))
        checks.append((
            "這一節真的走到逐庫迴圈（不是第一天的唯讀早退）",
            "quoted_exemptions" in counts and not section["errors"],
        ))

        # 只有一個庫時的數字不得因為這次改動而變。
        single = dream._section_compliance([vault_a], today, today, config={})
        checks.append((
            "單一庫的數字不變",
            single["counts"].get("quoted_exemptions") == 3,
        ))

        # 日期窗口照舊有效：窗口之前的豁免不算進今天。
        old = _vault(projects, "proj-old", [
            _masked(shared, "2026-09-01T01:00:00+00:00"),
        ])
        windowed = dream._section_compliance([old], today, today, config={})
        checks.append((
            "窗口之前的豁免不算進今天",
            windowed["counts"].get("quoted_exemptions") == 0,
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
