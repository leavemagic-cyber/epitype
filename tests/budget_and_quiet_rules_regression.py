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

import json
import tempfile
import unittest

from epitype import gates_report, host_sync, memspec, rule_pipeline


class HostFileBudget(unittest.TestCase):
    """整個宿主檔照宿主真正的行為判：會丟內容才算問題，只是建議或只警告的算提醒。"""

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
        self.claude = self.home / ".claude" / "CLAUDE.md"
        self.claude.parent.mkdir(parents=True, exist_ok=True)

    def budget(self, host, raw, *, project=False):
        return host_sync._budget_problems(host, self.home / "f.md", raw, [], self.home, project=project)

    def test_host_facts_are_the_documented_numbers(self):
        self.assertEqual(memspec.CLAUDE_MD_SKIP_BYTES, 4 * 1024 * 1024)
        self.assertEqual(memspec.CLAUDE_MD_RECOMMENDED_LINES, 200)
        self.assertEqual(memspec.CODEX_PROJECT_DOC_MAX_BYTES, 32 * 1024)

    def test_a_small_host_file_syncs(self):
        self.agents.write_text("# 我自己的說明\n\n短短幾行。\n", encoding="utf-8")
        plan = host_sync.plan_for("codex", [self.vault], home=self.home)
        self.assertFalse([p for p in plan.problems if "32768" in p])
        self.assertEqual(self.budget("codex", "短\n"), ([], []))
        self.assertEqual(self.budget("claude", "短\n"), ([], []))

    def test_global_codex_file_over_32_kib_is_only_a_notice(self):
        # Codex 不截全域 ~/.codex/AGENTS.md，只警告；擋寫入會留下舊規則每場照載。
        self.agents.write_text("# 說明\n\n" + ("使用者自己寫的內容。\n" * 1600), encoding="utf-8")
        plan = host_sync.plan_for("codex", [self.vault], home=self.home)
        self.assertFalse([p for p in plan.problems if "32768" in p])
        self.assertTrue(any("32768" in n and "不會被截" in n for n in plan.notices))
        problems, notices = self.budget("codex", "x" * 32768)
        self.assertEqual((problems, notices), ([], []))
        problems, notices = self.budget("codex", "x" * 32769)
        self.assertEqual((problems, len(notices)), ([], 1))

    def test_project_codex_file_over_32_kib_is_a_problem_because_the_tail_is_cut(self):
        problems, notices = self.budget("codex", "x" * 32769, project=True)
        self.assertEqual(len(problems), 1)
        self.assertIn("32768", problems[0])
        self.assertIn("截", problems[0])
        self.assertEqual(self.budget("codex", "x" * 32768, project=True), ([], []))

    def test_claude_file_over_4_mib_is_a_problem_and_over_200_lines_a_notice(self):
        problems, notices = self.budget("claude", "x" * (4 * 1024 * 1024 + 1))
        self.assertEqual(len(problems), 1)
        self.assertIn("整份跳過", problems[0])
        self.assertEqual(self.budget("claude", "x" * (4 * 1024 * 1024)), ([], []))
        problems, notices = self.budget("claude", "- x\n" * 201)
        self.assertEqual((problems, len(notices)), ([], 1))
        self.assertIn("建議", notices[0])
        self.assertEqual(self.budget("claude", "- x\n" * 200), ([], []))


class RulesThatNeverFired(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-quiet-")
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name).resolve()
        self.card("loud", "會擋的", "這句話會被擋")
        self.card("quiet", "沒擋過的", "這句話從來沒出現過")

    def card(self, key, name, forbidden):
        # 規則卡要帶已審的現行內容綁定才算武裝；沒綁定的卡兩道閘都不讀，名單就會是空的。
        text = ("---\nname: %s\ndescription: 說明\ndecision_key: %s\nstatus: active\n"
                "current_decision_at: 2026-09-19\ndecided_by: owner-explicit\nowner_quote: x\n"
                "forbidden:\n  - %s\n---\nbody\n" % (name, key, forbidden))
        doc = {"version": 1, "key": key, "kind": "ai-derived", "author": "claude", "text": name, "quote": "x",
               "scope": {"level": "project", "project": str(self.vault), "case": None,
                         "actions": ["*"], "conditions": [], "expiry": "unspecified",
                         "exceptions": []},
               "supersedes": []}
        doc = rule_pipeline.bind_payload(doc, text, reviewer="claude",
                                         rationale="Synthetic fixture: reviewed the complete card.")
        text = text.replace("---\n", "---\nrule_provenance: "
                            + json.dumps(json.dumps(doc, ensure_ascii=False), ensure_ascii=False)
                            + "\n", 1)
        (self.vault / ("%s.md" % key)).write_text(text, encoding="utf-8")

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
        # 「武裝規則 2 條」一起比：只比「0 條」的話，一條都沒讀到也會通過。
        self.assertIn("武裝規則 2 條，其中 0 條從來沒擋過", body)

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
