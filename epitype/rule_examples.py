# -*- coding: utf-8 -*-
"""每條規則自帶的兩向例句，用閘門自己的程式碼跑一次。

規則寫好就上線（owner 2026-09-19：直接上線給我用，我就是試用品），所以誤擋的保護只剩
兩道，這是第一道：**寫卡的當下就試跑**。一條規則要附兩種例句——

- ``example_blocks``：這幾句一定要被這張卡擋下來。擋不到，代表規則寫得太鬆或寫錯了。
- ``example_allows``：這幾句一定不能被這張卡擋。擋到了，就是誤擋，當場攔在門外。

判斷不自己重寫一份比對邏輯，直接叫閘門的函式來判：規則在真機上怎麼判，這裡就怎麼判。
自己複製一份比對法的話，測試會通過、真機仍然誤擋——那是最貴的一種綠燈。

2026-09-19 這一天抓到七種誤擋（cp950 的否定語境、測試不等於上線的否定語境、數字要帶
範圍漏了掃描與題、白話規則套到英文提交訊息、規則擋掉自己的測試樣本、引號裡的要求被遮掉、
PDF 頁碼範圍被當成整檔讀）。每一種都是一句「不該擋卻擋了」的話——也就是一句
``example_allows``。
"""

import re
import sys
from pathlib import Path

try:
    from . import memspec
except ImportError:  # 直接當腳本跑
    import memspec

_ADAPTER_DIR = Path(__file__).resolve().parents[1] / "adapters" / "claude"


def _gate_modules():
    """閘門模組。載入失敗就回 (None, None)——例句檢查不該讓卡片檢查器整個倒掉。"""
    if str(_ADAPTER_DIR) not in sys.path:
        sys.path.insert(0, str(_ADAPTER_DIR))
    try:
        import pretooluse_gate
        import stop_gate
    except Exception:
        return None, None
    return stop_gate, pretooluse_gate


def _decision_from(ruling, stop_gate):
    """把卡片讀出來的裁定湊成閘門用的那個結構，欄位對齊 `_Decision`。"""
    return stop_gate._Decision(
        key=str(ruling.get("key") or ""),
        decided_at="",
        quote="",
        forbidden=tuple(ruling.get(memspec.FORBIDDEN_FIELD) or ()),
        aliases=(),
        path=Path("."),
        decided_by="",
        require_when=str(ruling.get("require_when") or ""),
        require_text=str(ruling.get("require_text") or ""),
        advice="",
        applies_to="",
        turn_check="",
        turn_check_limit="",
    )


def _speech_hit(decision, sentence, stop_gate):
    """這句話會不會被這張卡擋下來——用閘門自己的兩個判斷式。"""
    defects = []
    if decision.forbidden and stop_gate._forbidden_fragment(decision, sentence, defects) is not None:
        return True
    if decision.require_when and decision.require_text:
        if stop_gate._requirement_gap(decision, sentence, defects) is not None:
            return True
    return False


def _guard_hit(guard, payload):
    """這一串指令會不會被這張守衛卡擋下來（只看字面片段那一種）。"""
    substrings = [item for item in (guard.get("substrings") or ()) if item]
    if not substrings:
        return None  # 欄位型守衛沒有字面可比，例句測不了它
    return all(fragment in payload for fragment in substrings)


def check_card(path, front_lines=None, fields=None):
    """一張卡的例句findings：[(level, code, message)]。沒寫例句就回空。"""
    stop_gate, pretooluse_gate = _gate_modules()
    if stop_gate is None:
        return []
    path = Path(path)
    if front_lines is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        front_lines, _closing = memspec.split_frontmatter(text)
        if front_lines is None:
            return []
    blocks = memspec.sequence_items(front_lines, memspec.EXAMPLE_BLOCKS_FIELD)
    allows = memspec.sequence_items(front_lines, memspec.EXAMPLE_ALLOWS_FIELD)
    if not blocks and not allows:
        return []

    findings = []
    try:
        ruling = stop_gate._read_decision(path)
    except Exception:
        ruling = None
    try:
        guard = pretooluse_gate._read_guard(path)
    except Exception:
        guard = None
    guard = guard if isinstance(guard, dict) else None

    decision = _decision_from(ruling, stop_gate) if isinstance(ruling, dict) else None
    armed_speech = bool(decision and (decision.forbidden or
                                      (decision.require_when and decision.require_text)))
    armed_guard = bool(guard and (guard.get("substrings") or ()))
    if not armed_speech and not armed_guard:
        findings.append((
            memspec.EXAMPLE_LEVEL_FAIL, "example",
            f"寫了例句，但這張卡沒有可以用例句測的武裝"
            f"（{memspec.FORBIDDEN_FIELD}／{memspec.REQUIRE_WHEN_FIELD} 配對／"
            f"{memspec.ACTION_GUARD_ALL_OF_FIELD}）——例句測不到任何東西",
        ))
        return findings

    def hit(sentence):
        if armed_speech and _speech_hit(decision, sentence, stop_gate):
            return True
        if armed_guard and _guard_hit(guard, sentence):
            return True
        return False

    for sentence in blocks[: memspec.EXAMPLE_MAX_ITEMS]:
        if not hit(sentence):
            findings.append((
                memspec.EXAMPLE_LEVEL_FAIL, "example",
                f"{memspec.EXAMPLE_BLOCKS_FIELD} 的「{sentence[:60]}」沒有被這張卡擋下來"
                "——規則寫得比它自己的例句還鬆",
            ))
    for sentence in allows[: memspec.EXAMPLE_MAX_ITEMS]:
        if hit(sentence):
            findings.append((
                memspec.EXAMPLE_LEVEL_FAIL, "example",
                f"{memspec.EXAMPLE_ALLOWS_FIELD} 的「{sentence[:60]}」被這張卡擋下來了"
                "——這就是誤擋，規則要收窄",
            ))
    return findings


def _selftest():
    import tempfile

    checks = []

    def card(name, body):
        directory = Path(tempfile.mkdtemp(prefix="epitype-examples-"))
        target = directory / (name + ".md")
        target.write_text(body, encoding="utf-8")
        return target

    good = card("good", (
        "---\nname: 測試通過不等於上線\ndescription: 說明\n"
        "decision_key: t1\nstatus: active\ncurrent_decision_at: 2026-09-19\n"
        "decided_by: owner-explicit\nowner_quote: x\n"
        "forbidden:\n  - 測試過了所以沒問題\n"
        "example_blocks:\n  - 測試過了所以沒問題，可以上線\n"
        "example_allows:\n  - 測試過了，但還沒接上，所以不算上線\n---\nbody\n"
    ))
    findings = check_card(good)
    checks.append(("兩向都對的卡沒有 finding", findings == [], findings))

    loose = card("loose", (
        "---\nname: 太鬆\ndescription: 說明\n"
        "decision_key: t2\nstatus: active\ncurrent_decision_at: 2026-09-19\n"
        "decided_by: owner-explicit\nowner_quote: x\n"
        "forbidden:\n  - 這句話根本不會出現\n"
        "example_blocks:\n  - 測試過了所以沒問題\n---\nbody\n"
    ))
    findings = check_card(loose)
    checks.append(("擋不到自己的例句要 FAIL",
                   any("沒有被這張卡擋下來" in item[2] for item in findings), findings))

    wide = card("wide", (
        "---\nname: 太寬\ndescription: 說明\n"
        "decision_key: t3\nstatus: active\ncurrent_decision_at: 2026-09-19\n"
        "decided_by: owner-explicit\nowner_quote: x\n"
        "forbidden:\n  - 測試\n"
        "example_allows:\n  - 我把測試跑完了，62/62\n---\nbody\n"
    ))
    findings = check_card(wide)
    checks.append(("擋到不該擋的例句要 FAIL",
                   any("這就是誤擋" in item[2] for item in findings), findings))

    bare = card("bare", (
        "---\nname: 沒武裝\ndescription: 說明\n"
        "example_blocks:\n  - 任何一句話\n---\nbody\n"
    ))
    findings = check_card(bare)
    checks.append(("沒武裝卻寫例句要 FAIL",
                   any("測不到任何東西" in item[2] for item in findings), findings))

    none = card("none", (
        "---\nname: 沒例句\ndescription: 說明\n"
        "decision_key: t4\nstatus: active\ncurrent_decision_at: 2026-09-19\n"
        "decided_by: owner-explicit\nowner_quote: x\nforbidden:\n  - 隨便\n---\nbody\n"
    ))
    checks.append(("沒寫例句就不管它", check_card(none) == [], None))

    guard = card("guard", (
        "---\nname: heredoc\ndescription: 說明\n"
        'guard_tool: Bash\nguard_all_of:\n  - "<<"\n  - "\\\\"\n'
        "guard_advice: 改用寫檔工具\nlast_verified_at: 2026-09-19\n"
        'example_blocks:\n  - \'cat <<EOF > C:\\tmp\\x.txt\'\n'
        "example_allows:\n  - git status --short\n---\nbody\n"
    ))
    findings = check_card(guard)
    checks.append(("守衛卡的例句也兩向測", findings == [], findings))

    good_count = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        if not ok:
            print("FAIL", name, detail)
    print("SELFTEST %s %d/%d" % ("PASS" if good_count == len(checks) else "FAIL",
                                 good_count, len(checks)))
    return 0 if good_count == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
