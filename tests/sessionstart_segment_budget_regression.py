import sys; sys.dont_write_bytecode = True
"""開場的每一段都有自己的時間上限，而且上限是真的。

2026-09-06 事故：SessionStart 逾 10 s 被宿主砍成 Failed。當時的結論是「每一段都要在
剩餘預算內完成」，但實作只在段與段之間問一次「還有沒有剩」——問完就放手，段內沒有任何
期限。2026-09-22 在 300 張卡的合成庫上量到兩段還在這麼做：

- `warm_guard_cache` 逐卡比的是宿主的 9 s 逾時（`expired()`），張數上限 1<<30 等於沒有，
  實測一段吃掉 3.9 s。
- `card_lint.summary_line` 把「逾時」與「還沒掃」都寫成 `reports=None`，於是共用的那一趟
  一逾時，這裡就用預設預算再掃一整趟——1.0 s 的預算實耗 2.07 s。

這些測試釘的是「上限存在且生效」，不是「跑得多快」：斷言的是讀到的卡數、回傳值與實耗
不超過撥給的預算，數字都留了寬裕，忙碌的機器不該讓它們變紅。
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import card_lint, memspec
import pretooluse_gate as pretool

CARDS = 300
# 期限是在「做下一張卡之前」問的，所以實耗可以超出一張卡的工時；這個餘裕含 Windows 上
# 一次檔案讀取的抖動，遠小於任何一段的預算本身。
SLACK_SECONDS = 0.3


def build_vault(root, count=CARDS):
    vault = root / "bulk-vault"
    vault.mkdir(parents=True)
    (vault / memspec.MEMORY_INDEX_FILENAME).write_text("# Bulk\n", encoding="utf-8")
    for index in range(count):
        (vault / f"feedback-bulk-{index:03d}.md").write_text(
            f"---\nname: bulk-{index:03d}\ndescription: 合成卡 {index}\n---\n"
            "- 2026-01-01 待辦：合成殭屍待辦，沒有出口\n本文一行。\n",
            encoding="utf-8",
        )
    return vault


class WarmGuardCacheRespectsItsDeadline(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-warm-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = build_vault(self.root)

    def cached_guards(self):
        """暖快取裡目前有幾張卡的判定；沒有快取檔就是一張都沒讀。"""
        try:
            loaded = json.loads(pretool._guard_cache(self.vault).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        guards = loaded.get("guards")
        return len(guards) if isinstance(guards, dict) else 0

    def test_an_already_expired_deadline_reads_nothing(self):
        # 撥不到時間就整段省略：起了跑不完的一段，付的錢一樣，答案卻是半個。
        started = time.monotonic()
        pretool.warm_guard_cache([self.vault], started, deadline=time.monotonic() - 1.0)
        spent = time.monotonic() - started
        self.assertEqual(self.cached_guards(), 0)
        self.assertLess(spent, SLACK_SECONDS)

    def test_a_short_deadline_stops_partway_without_raising(self):
        # 截斷只影響「多快暖完」：暖到的寫回快取，沒暖到的仍由每次工具呼叫的逐次補讀
        # 收斂，所以這裡要看到「有進展、但沒讀完」，而不是例外或零。
        started = time.monotonic()
        pretool.warm_guard_cache([self.vault], started, deadline=time.monotonic() + 0.1)
        spent = time.monotonic() - started
        self.assertLess(spent, 0.1 + SLACK_SECONDS)
        self.assertLess(self.cached_guards(), CARDS)

    def test_a_generous_deadline_still_warms_the_whole_vault(self):
        # 上限不得換成「少擋一點」：時間夠的時候，暖的範圍一張都不能少。
        pretool.warm_guard_cache([self.vault], time.monotonic(), deadline=time.monotonic() + 60.0)
        self.assertEqual(self.cached_guards(), CARDS)

    def test_no_deadline_takes_a_named_budget_not_the_host_timeout(self):
        # 沒帶期限不等於沒有期限。以前這條路徑一路吃到宿主的 9 s 逾時為止。
        self.assertLess(
            memspec.SESSIONSTART_WARM_GUARD_BUDGET_SECONDS, memspec.HOOK_TIMEOUT_SECONDS
        )
        started = time.monotonic()
        pretool.warm_guard_cache([self.vault], started)
        spent = time.monotonic() - started
        self.assertLess(
            spent, memspec.SESSIONSTART_WARM_GUARD_BUDGET_SECONDS + SLACK_SECONDS
        )


class CardLintRespectsItsTimeBudget(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-lint-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = build_vault(self.root)
        # 期限是在每張卡之前問的，所以實耗最多超出「一張卡」；而這個行程裡的第一張卡
        # 還要付掉惰性匯入與正則編譯（實測 ~0.3 s），那是一次性的行程成本，不是期限
        # 沒生效。先用一張卡把它付掉，下面量到的才是期限本身。
        warmup = build_vault(self.root / "warmup", count=1)
        card_lint.scan_vaults([warmup], time_budget=10.0)

    def test_a_tiny_budget_returns_none_inside_that_budget(self):
        # 逾時回 None 的語意不變：半個庫的數字是錯的數字，點名錯的比不點名更糟。
        for budget in (0.05, 0.2):
            started = time.monotonic()
            reports = card_lint.scan_vaults([self.vault], time_budget=budget)
            spent = time.monotonic() - started
            self.assertIsNone(reports, budget)
            self.assertLess(spent, budget + SLACK_SECONDS, budget)

    def test_a_spent_budget_does_not_start_the_next_vault(self):
        # 逐卡的檢查只救得了「這一庫掃不完」；期限過了還起下一庫，是再賭一次目錄走訪。
        second = build_vault(self.root / "second-root")
        started = time.monotonic()
        reports = card_lint.scan_vaults([self.vault, second], time_budget=0.05)
        spent = time.monotonic() - started
        self.assertIsNone(reports)
        self.assertLess(spent, 0.05 + SLACK_SECONDS)

    def test_a_timed_out_shared_scan_is_not_paid_for_twice(self):
        # 開場先跑一趟 scan_vaults，再把結果餵給 summary_line。那一趟逾時時傳進來的
        # 是 None，而 None 以前的意思是「還沒掃」——於是正好在付不起的時候再掃一趟。
        started = time.monotonic()
        self.assertIsNone(card_lint.summary_line([self.vault], reports=None))
        self.assertLess(time.monotonic() - started, SLACK_SECONDS)

    def test_omitting_reports_still_scans(self):
        # 沒帶 reports 的呼叫端（CLI、自測）行為不變，否則上面那條就是靜靜關掉這一行。
        vault = self.root / "one-bad-card"
        vault.mkdir()
        (vault / memspec.MEMORY_INDEX_FILENAME).write_text("# One\n", encoding="utf-8")
        (vault / "feedback-broken.md").write_text("沒有 frontmatter\n", encoding="utf-8")
        self.assertIsNotNone(card_lint.summary_line([vault], time_budget=5.0))


class SessionStartAllocatesEverySegment(unittest.TestCase):
    def test_the_warm_segment_is_allocated_not_unbounded(self):
        # 這一段以前只問一次 `_soft_remaining > 0`，問完就放手跑到宿主砍人為止。
        source = (ROOT / "adapters" / "claude" / "sessionstart_hook.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_segment_budget(started_at, memspec.SESSIONSTART_WARM_GUARD_BUDGET_SECONDS)",
                      source)
        self.assertNotIn("warm_guard_cache(resolved, started_at)\n", source)

    def test_the_budget_constants_stay_inside_the_host_timeout(self):
        # 讓數字過關的方式只有一種：把工作做完在預算內。調大預算不是修好。
        self.assertLessEqual(
            memspec.SESSIONSTART_BUDGET_SECONDS, memspec.HOOK_TIMEOUT_SECONDS
        )
        self.assertLessEqual(
            memspec.CARD_LINT_HOOK_BUDGET_SECONDS
            + memspec.SESSIONSTART_WARM_GUARD_BUDGET_SECONDS,
            memspec.SESSIONSTART_BUDGET_SECONDS,
        )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
