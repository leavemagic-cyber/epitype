# -*- coding: utf-8 -*-
"""兩件「規則治理」的機械檢查：宿主檔會不會被安靜截斷，以及哪些規則從來沒擋過。

兩者都來自計畫第 4 項。第 4 項原本的主體是「用成效數字自動退役規則」，那一半沒有做，
理由寫在 never_fired 那一段：2026-09-17 實跑過自動降級，判準分不出「規則太寬」與
「我一直犯這條」，一條 6 次命中、6 次全擋下、0 漏擋的規則被關了 14 天。沒有可靠的成效
訊號之前，照那個方向做就是重犯同一個錯。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tempfile
import unittest

from epitype import gates_report, host_sync, memspec


class HostFileBudget(unittest.TestCase):
    """宿主只載入到上限為止，超出的部分是安靜被丟掉的——被丟掉的可能正是規則塊。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-budget-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.vault = self.home / "vault"
        (self.vault / ".epitype").mkdir(parents=True, exist_ok=True)
        (self.vault / "card.md").write_text(
            "---\nname: x\ndescription: y\n---\nbody\n", encoding="utf-8")
        self.agents = self.home / ".codex" / "AGENTS.md"
        self.agents.parent.mkdir(parents=True, exist_ok=True)

    def problems(self):
        plan = host_sync.plan_for("codex", [self.vault], home=self.home)
        return [item for item in plan.problems if "安靜丟掉" in item]

    def test_a_small_host_file_syncs(self):
        self.agents.write_text("# 我自己的說明\n\n短短幾行。\n", encoding="utf-8")
        self.assertEqual(self.problems(), [])

    def test_a_host_file_over_the_documented_cap_is_refused_with_the_number(self):
        # 這個合成庫只有一張卡，規則塊很小，所以要靠使用者自己的內容把整檔推過上限——
        # 那正是要防的情況：我們這兩塊都合規，被丟掉的卻可能是它們。
        self.agents.write_text("# 說明\n\n" + ("使用者自己寫的內容。\n" * 1600), encoding="utf-8")
        problems = self.problems()
        self.assertEqual(len(problems), 1)
        self.assertIn(str(memspec.HOST_BUDGET_BYTES["codex"]), problems[0])

    def test_only_documented_caps_are_enforced(self):
        # Claude 沒有公告過 CLAUDE.md 的硬上限，所以不編一個出來擋人。
        self.assertIsNone(memspec.HOST_BUDGET_BYTES["claude"])


class RulesThatNeverFired(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-quiet-")
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name)
        (self.vault / "loud.md").write_text(
            "---\nname: 會擋的\ndescription: 說明\ndecision_key: loud\nstatus: active\n"
            "current_decision_at: 2026-09-19\ndecided_by: owner-explicit\nowner_quote: x\n"
            "forbidden:\n  - 這句話會被擋\n---\nbody\n", encoding="utf-8")
        (self.vault / "quiet.md").write_text(
            "---\nname: 沒擋過的\ndescription: 說明\ndecision_key: quiet\nstatus: active\n"
            "current_decision_at: 2026-09-19\ndecided_by: owner-explicit\nowner_quote: x\n"
            "forbidden:\n  - 這句話從來沒出現過\n---\nbody\n", encoding="utf-8")

    def lines(self, fired_labels):
        rows = [gates_report.Row(timestamp=None, kind="stop_block", rule="forbidden",
                                 label=label, session_id="s", reason=None)
                for label in fired_labels]
        return gates_report.never_fired_lines(self.vault, rows)

    def test_it_names_the_rule_that_never_fired(self):
        lines = self.lines(["loud"])
        body = "\n".join(lines)
        self.assertIn("quiet", body)
        self.assertNotIn("\n  loud", body)

    def test_a_rule_that_has_fired_is_not_listed(self):
        body = "\n".join(self.lines(["loud", "quiet"]))
        self.assertIn("0 條從來沒擋過", body)

    def test_it_says_out_loud_that_it_does_not_retire_anything(self):
        # 這一行不是客套：自動退役 2026-09-17 實跑過並被推翻，名單只是線索。
        self.assertTrue(any("只報不動" in line for line in self.lines(["loud"])))


def _selftest():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromTestCase(case)
        for case in (HostFileBudget, RulesThatNeverFired)
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
