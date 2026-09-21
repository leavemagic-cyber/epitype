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
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import card_lint, memspec
import pretooluse_gate as pretool
import sessionstart_hook

CARDS = 300
# 期限是在「做下一張卡之前」問的，所以實耗可以超出一張卡的工時；這個餘裕含 Windows 上
# 一次檔案讀取的抖動，遠小於任何一段的預算本身。
SLACK_SECONDS = 0.3


def past_deadline(budget, start=1000.0, before=4):
    """前 `before` 次問答都在期限內，之後每一次都已經過期。

    要釘的是「掃到一半期限到了就停手並回 None」，所以不能一開始就過期——那會在
    `scan_vaults` 的迴圈開頭就回 None，等於把「掃到一半逾時」偷換成「期限本來就過了」
    （那條另有一項在測）。前幾次放行，是為了讓它真的走進 `scan_vault` 的逐卡迴圈。

    釘「期限有沒有被遵守」不該靠「機器夠不夠慢」——快的機器在小預算內掃得完，測試就
    紅在機器上而不是紅在缺陷上（2026-09-22 的發布閘就是這樣被擋下來的）。
    """
    calls = [0]

    def clock():
        calls[0] += 1
        return start if calls[0] <= before else start + budget + 100.0

    return clock


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
        #
        # 用假時鐘讓期限「必然」在掃描途中過去，不賭這台機器夠慢。2026-09-22 這一項的
        # 第一版就是用 0.05 s 的真預算去賭 300 張卡掃不完：本機（Windows）綠、GitHub
        # 的 runner 快到掃得完，於是回了報告而不是 None，把發布閘擋掉。期限有沒有被
        # 遵守與機器快慢無關，斷言也就不該跟機器快慢有關。
        for budget in (0.05, 0.2):
            started = time.monotonic()
            with unittest.mock.patch.object(card_lint.time, "monotonic", past_deadline(budget)):
                reports = card_lint.scan_vaults([self.vault], time_budget=budget)
            spent = time.monotonic() - started
            self.assertIsNone(reports, budget)
            self.assertLess(spent, budget + SLACK_SECONDS, budget)

    def test_a_spent_budget_does_not_start_the_next_vault(self):
        # 逐卡的檢查只救得了「這一庫掃不完」；期限過了還起下一庫，是再賭一次目錄走訪。
        #
        # 釘的是「第二庫根本沒被起」，所以直接數 scan_vault 被叫了幾次，不靠實耗去推。
        # 時鐘由這裡掌握：第一庫照常掃完（沒逾時），掃完的那一刻期限才過去——真實世界
        # 就是這個形狀，而這個形狀用真時鐘賭不出來。
        second = build_vault(self.root / "second-root")
        scanned = []
        real_scan = card_lint.scan_vault
        now = [1000.0]

        def spy(vault, today, deadline):
            scanned.append(Path(vault).name)
            report = real_scan(vault, today, deadline)
            now[0] = deadline + 1.0  # 這一庫掃完了，期限正好在這時過去
            return report

        with unittest.mock.patch.object(card_lint.time, "monotonic", lambda: now[0]), \
                unittest.mock.patch.object(card_lint, "scan_vault", spy):
            reports = card_lint.scan_vaults([self.vault, second], time_budget=5.0)
        self.assertEqual(len(scanned), 1, scanned)
        self.assertIsNone(reports)

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


class RepeatGuardNoticesRespectsItsDeadline(unittest.TestCase):
    """張數上限不是時間上限：這一段 300 卡實測 0.00 s，但卡變大、變多或磁碟變慢時，
    `ACTION_GUARD_MAX_CARDS_PER_VAULT` 攔不住它吃掉預算。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-notice-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = build_vault(self.root)
        self.blocked_card = "scar-heredoc.md"
        # 這一行只在同一張卡最近一直擋人時才出現，所以紀錄檔要先有夠多次擋下。
        rows = [
            json.dumps({
                "kind": memspec.ACTION_GUARD_LOG_KIND,
                "timestamp": "2026-09-22T00:00:00+00:00",
                "card": self.blocked_card,
            }, ensure_ascii=False)
            for _ in range(memspec.GUARD_REPEAT_NOTICE_THRESHOLD + 2)
        ]
        (self.vault / memspec.GATE_LOG_FILENAME).write_text(
            "\n".join(rows) + "\n", encoding="utf-8"
        )
        # `now` 固定成紀錄的時間，這些列才落在「最近一天」的窗內。
        self.now = 1758499200.0  # 2026-09-22T00:00:00Z

    def notices(self, deadline):
        return sessionstart_hook._repeat_guard_notices(
            [self.vault], time.monotonic(), now=self.now, deadline=deadline
        )

    def test_a_generous_deadline_still_produces_the_line(self):
        # 先釘住「時間夠的時候這一行真的會出現」，否則下面兩條可能只是在測一個死路徑。
        lines = self.notices(time.monotonic() + 60.0)
        self.assertTrue(lines)
        self.assertIn(self.blocked_card, "\n".join(lines))

    def test_an_already_expired_deadline_returns_nothing(self):
        started = time.monotonic()
        self.assertEqual(self.notices(time.monotonic() - 1.0), [])
        self.assertLess(time.monotonic() - started, SLACK_SECONDS)

    def test_a_deadline_that_passes_mid_scan_gives_no_half_true_count(self):
        # 只數了一半的庫給出的「擋了 N 次」是錯的數字，而這一行的全部內容就是那個數字。
        # 所以中止時回空，不回半真的數字——而且不能是例外。
        #
        # 時鐘用假的，量到的才是「期限到了就停」而不是「這台機器今天多快」：第一次問
        # （進函式）還在期限內，第二次問（正要數第一個庫）已經過了。真正的計時交給
        # 上面那兩條與 profile。
        deadline = 100.0
        ticks = iter([99.0] + [101.0] * 50)
        with unittest.mock.patch("time.monotonic", lambda: next(ticks)):
            lines = sessionstart_hook._repeat_guard_notices(
                [self.vault, self.vault], 0.0, now=self.now, deadline=deadline
            )
        self.assertEqual(lines, [])

    def test_a_short_real_deadline_answers_fully_or_not_at_all(self):
        # 短期限的結果只有兩種是對的：完整的那一行，或什麼都不說。落在中間的（數到一半
        # 的次數、少了庫的次數）才是這一段最該避免的東西。小庫通常來得及，所以這裡不能
        # 斷言「一定回空」——那會變成在測這台機器多慢。
        complete = self.notices(time.monotonic() + 60.0)
        started = time.monotonic()
        lines = self.notices(time.monotonic() + 0.01)
        spent = time.monotonic() - started
        self.assertIn(lines, ([], complete))
        self.assertLess(spent, 0.01 + SLACK_SECONDS)


class SessionStartAllocatesEverySegment(unittest.TestCase):
    SEGMENTS = (
        ("warm_guard_cache", "memspec.SESSIONSTART_WARM_GUARD_BUDGET_SECONDS"),
        ("_repeat_guard_notices", "memspec.SESSIONSTART_GUARD_NOTICE_BUDGET_SECONDS"),
    )

    def source(self):
        return (ROOT / "adapters" / "claude" / "sessionstart_hook.py").read_text(encoding="utf-8")

    def test_every_segment_is_allocated_not_just_asked_if_time_is_left(self):
        # 這些段以前都只問一次 `_soft_remaining > 0`，問完就放手跑到宿主砍人為止。
        source = self.source()
        for name, constant in self.SEGMENTS:
            self.assertIn(f"_segment_budget(started_at, {constant})", source, name)
        self.assertNotIn("warm_guard_cache(resolved, started_at)\n", source)
        self.assertNotIn("_repeat_guard_notices(resolved, started_at)\n", source)

    def test_the_budget_constants_stay_inside_the_host_timeout(self):
        # 讓數字過關的方式只有一種：把工作做完在預算內。調大預算不是修好。
        self.assertLessEqual(
            memspec.SESSIONSTART_BUDGET_SECONDS, memspec.HOOK_TIMEOUT_SECONDS
        )
        self.assertLessEqual(
            memspec.CARD_LINT_HOOK_BUDGET_SECONDS
            + memspec.SESSIONSTART_WARM_GUARD_BUDGET_SECONDS
            + memspec.SESSIONSTART_GUARD_NOTICE_BUDGET_SECONDS,
            memspec.SESSIONSTART_BUDGET_SECONDS,
        )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
