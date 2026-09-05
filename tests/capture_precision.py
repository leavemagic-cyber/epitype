import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，避免繁中輸出在程式進入點就中斷。
"""事件捕捉精準度的量測台。

線上 hook 只給「有沒有寫卡」，寫錯了沒有人會知道；裁定卡還會被喚回置頂，誤抓一張
就是每場對話都被汙染一次。本檔把 capture.classify 的判定攤成可計分的形狀：
`--selftest` 跑合成句（八類誤抓各兩條反例＋多子句真裁定＋三類正例），`--local` 對
一份人工標記過的真句量測 precision／recall／混淆矩陣。真句一律留在 repo 外，repo 內
只放自己編的合成句。
"""

import argparse
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _import_root in (_REPO_ROOT, _REPO_ROOT / "epitype"):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from epitype import capture  # noqa: E402

KINDS = ("grant", "correction", "ruling")
NONE = "none"


def predicted_kind(owner_text, assistant_text=None):
    found = capture.classify(owner_text, assistant_text or None)
    return NONE if found is None else found[0]


# ---------------------------------------------------------------- synthetic set
# 每條都是本檔作者編的合成句：不含任何 owner 原話、專案名或人名（privacy_lint 會擋）。
# expect=None 代表「不該入卡」。八類誤抓各兩條反例，對應 docs/FAILURE_MODES.md 的
# 「事件捕捉精準度」一節。
_ASKED = "這一步要你決定：要走甲案還是乙案？請你確認"

SYNTHETIC = (
    # 1 空殼附和：觸發詞落在「依照你建議」上，句子自己沒有決定
    ("shell-ack-1", "所有都依照你的建議辦理", None, None),
    ("shell-ack-2", "第三項就照你的意見處理", None, None),
    # 2 結構缺口：助理問了，owner 只回一句純疑問——不是裁定
    ("bare-question-1", "你拿什麼去驗證？", _ASKED, None),
    ("bare-question-2", "我要決定什麼？", _ASKED, None),
    # 3 疑問形授權：「你可以…嗎？」是在問，不是在准
    ("permission-question-1", "你可以讀取那個資料夾嗎？", None, None),
    ("permission-question-2", "你可以直接改設定檔嗎？我不確定", None, None),
    # 4 owner 自認錯：不是對 agent 的糾正
    ("self-error-1", "我錯了，剛剛那個欄位是我看反的", None, None),
    ("self-error-2", "我說錯了，等一下再送出就好", None, None),
    # 5 溝通方式要求：要白話說明，不是治理決定
    ("style-request-1", "這兩個方案的差別，白話跟我說", None, None),
    ("style-request-2", "告訴我現在卡在哪一步", None, None),
    # 6 助理長段分析被貼回來：>200 字或數字密度高
    (
        "assistant-prose-1",
        "量測結果：抽樣 480 筆裡 312 筆命中、118 筆落空、50 筆重複，命中率 65%，"
        "落空率 24%，重複率 10%，其中 27 筆是同一批來源，18 筆時間戳為 2026-03-04，"
        "另有 9 筆缺欄位，合計 6 個群組共 231 個檔案受影響",
        _ASKED,
        None,
    ),
    (
        "assistant-prose-2",
        "第 1 階段 12 項、第 2 階段 34 項、第 3 階段 56 項，共 102 項，其中 78 項已完成",
        _ASKED,
        None,
    ),
    # 7 一詞式無範圍應答：喚回時佔置頂卻沒有可執行內容
    ("scopeless-ack-1", "我同意", None, None),
    ("scopeless-ack-2", "核准了", _ASKED, None),
    # 8 催促疑問：進度質問不是糾正也不是裁定
    ("urging-1", "這個怎麼還沒做完？", _ASKED, None),
    ("urging-2", "測試怎麼還在跑，到底什麼問題", None, None),
    # 多子句真裁定：反問嵌在句中，去掉反問子句後其餘子句仍有決定性內容
    # 2026-09-06：multi-clause-ruling-1／4 原標 correction，是舊版「correction 一律
    # 優先」規則的產物——助理上一句 _ASKED 確實在請 owner 決定，owner 這兩句都直接
    # 回應了那個請求，改判 ruling（見 docs/FAILURE_MODES.md §13、classify() 的
    # correction→ruling 讓位條件）。
    (
        "multi-clause-ruling-1",
        "不是！只有第一種算正式合約，其他都算試辦，這樣了解嗎？",
        _ASKED,
        "ruling",
    ),
    (
        "multi-clause-ruling-2",
        "全部怎麼可能只留三份？以後都以清單上的數量為主，不要自己加",
        _ASKED,
        "ruling",
    ),
    (
        "multi-clause-ruling-3",
        "這樣算對嗎？先不要送出，等到欄位補齊再送，一律照這個順序",
        _ASKED,
        "ruling",
    ),
    (
        "multi-clause-ruling-4",
        "你為什麼又改成第二種？我明明就有裁定過要用第一種，不要自己換",
        _ASKED,
        "ruling",
    ),
    # 三類正例
    ("grant-1", "那個資料夾的整理你可以直接動，以後不用再問我", None, "grant"),
    ("grant-2", "我同意你安裝那個外掛並拿它做截圖", None, "grant"),
    ("grant-3", "我授權你移除舊版套件，不必每次都問", None, "grant"),
    ("correction-1", "我說過只做有統計意義的抽驗，不要跑一整批", None, "correction"),
    ("correction-2", "我不是說過，暫存檔只能放在專案第二層，不要亂放", None, "correction"),
    ("correction-3", "你搞錯了，那個欄位是給小分類用的，不是給品名用的", None, "correction"),
    ("ruling-1", "先不做那個轉檔，等原始資料齊了再說", _ASKED, "ruling"),
    ("ruling-2", "第二項不用，登入就能跑就好", _ASKED, "ruling"),
    ("ruling-3", "以後都用第一種寫法，一律不要混用", None, "ruling"),
    # 2026-09-06：correction ⇄ ruling 讓位規則——助理上一句確有提問（ruling_question
    # 命中）時，owner 帶糾正詞的決定性回覆讓位給 ruling；沒有提問脈絡時同一句仍是
    # correction，糾正詞的優先序不變。
    ("correction-to-ruling-with-question", "就用第二案，以後都不要再問這件事", _ASKED, "ruling"),
    ("correction-without-question-stays-correction", "不要再亂改設定", None, "correction"),
)


def _selftest():
    checks = []
    for name, owner, asked, expected in SYNTHETIC:
        got = predicted_kind(owner, asked)
        want = expected or NONE
        checks.append((f"{name} -> {want}", got == want, got))

    # 反問子句本身不能是唯一證據：拿掉決定性子句後，同一句必須落回不入卡。
    checks.append((
        "question clause alone is never evidence",
        predicted_kind("這樣算對嗎？", _ASKED) == NONE,
        None,
    ))
    # 空殼與正例只差一個決定性子句：規則不是靠句長。
    checks.append((
        "a decisive clause beside a shell ack still captures",
        predicted_kind("依照你的建議處理，但以後都不要自己加欄位", _ASKED) != NONE,
        None,
    ))
    # 助理提問不再是裁定的唯一憑證：沒有助理提問時，常規性語自己成立。
    checks.append((
        "standing scope makes a ruling without an assistant question",
        predicted_kind("以後都用第一種寫法，一律不要混用", None) == "ruling",
        None,
    ))
    # 同一句只發一張卡：不能同時進 grants/ 與 corrections/。
    checks.append((
        "one utterance earns one kind",
        predicted_kind("我說過那個目錄你可以直接動，不要再問", None) == "correction",
        None,
    ))

    failed = [(name, got) for name, ok, got in checks if not ok]
    for name, ok, got in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f" (got {got})"))
    print(f"capture_precision selftest: {len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


# ------------------------------------------------------------------ local score
def _score(rows):
    matrix = {}
    for row in rows:
        got = predicted_kind(row.get("owner_text", ""), row.get("assistant_text") or None)
        row["predicted_kind"] = got
        key = (row.get("expected_kind", NONE), got)
        matrix[key] = matrix.get(key, 0) + 1
    return matrix


def _report(rows, matrix):
    captured = [row for row in rows if row["predicted_kind"] != NONE]
    right = [row for row in captured if row["predicted_kind"] == row.get("expected_kind")]
    keepers = [row for row in rows if row.get("keep") and row.get("expected_kind") in KINDS]
    kept = [row for row in keepers if row["predicted_kind"] != NONE]
    kept_right = [row for row in keepers if row["predicted_kind"] == row.get("expected_kind")]
    valued = [row for row in captured if row.get("keep")]

    def ratio(part, whole):
        return len(part) / len(whole) if whole else 0.0

    print(f"rows={len(rows)} captured={len(captured)} kind-correct={len(right)}")
    print(f"precision(kind correct / captured) = {ratio(right, captured):.3f}")
    print(f"recall(captured / keep&3-kind)     = {ratio(kept, keepers):.3f}  [{len(kept)}/{len(keepers)}]")
    print(f"recall(right kind / keep&3-kind)   = {ratio(kept_right, keepers):.3f}")
    print(f"value(keep / captured)             = {ratio(valued, captured):.3f}")
    columns = KINDS + (NONE,)
    print("confusion  expected \\ got " + " ".join(f"{name:>11}" for name in columns))
    for expected in columns:
        cells = " ".join(f"{matrix.get((expected, got), 0):>11}" for got in columns)
        print(f"{expected:>25} {cells}")
    return ratio(right, captured), ratio(kept, keepers)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="capture_precision", description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="run the synthetic checks")
    parser.add_argument("--local", type=Path, default=None, help="labelled real-sentence set (kept outside the repo)")
    parser.add_argument("--misses", action="store_true", help="list every disagreement with the labels")
    arguments = parser.parse_args(argv)
    if arguments.local is None or arguments.selftest:
        code = _selftest()
        if arguments.local is None:
            return code
    rows = json.loads(arguments.local.read_text(encoding="utf-8"))
    matrix = _score(rows)
    precision, recall = _report(rows, matrix)
    if arguments.misses:
        for row in rows:
            if row["predicted_kind"] != row.get("expected_kind"):
                print(
                    f"  {row.get('expected_kind'):>10} -> {row['predicted_kind']:<10}"
                    f" keep={row.get('keep')} {row.get('why', '')} | {row.get('owner_text', '')[:70]!r}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
