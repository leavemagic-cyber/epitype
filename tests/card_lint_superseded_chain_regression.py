"""Superseded prose is a reminder; quotes, examples and retired cards are exempt."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import card_lint, memspec


class SupersededChain(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-chain-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "synthetic.md"

    def findings(self, body, kind="feedback", status=""):
        text = ("---\nname: synthetic\ndescription: 2026-10-28 合成卡\n"
                "last_verified_at: 2026-10-28\naliases:\n  - 合成卡\n"
                f"status: {status}\nmetadata:\n  type: {kind}\n---\n{body}\n")
        self.path.write_text(text, encoding="utf-8")
        _kind, findings = card_lint.check_card(self.path, "synthetic.md")
        return [(level, reason) for level, rule, reason in findings if rule == "superseded-chain"]

    def test_two_markers_warn_in_each_live_rule_like_type(self):
        for kind in memspec.CARD_LINT_BODY_NOISE_TYPES:
            with self.subTest(kind=kind):
                findings = self.findings("取代上段：新做法。\n沿革：舊做法。", kind=kind)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0][0], card_lint.WARN)
                for fragment in ("取代上段", "沿革", "現行版本", "git 歷史"):
                    self.assertIn(fragment, findings[0][1])

    def test_one_marker_and_overlapping_word_do_not_warn(self):
        for marker in ("取代上段", "取代前段", "取代上一段", "已作廢", "作廢", "已撤銷",
                       "已過時", "舊版段落", "沿革", "superseded"):
            with self.subTest(marker=marker):
                self.assertEqual(self.findings(marker), [])

    def test_all_markers_and_case_insensitive_english_count(self):
        for marker in ("取代上段", "取代前段", "取代上一段", "已作廢", "作廢", "已撤銷",
                       "已過時", "舊版段落", "沿革", "SUPERSEDED"):
            with self.subTest(marker=marker):
                self.assertEqual(len(self.findings(marker + "\n取代前段")), 1)

    def test_quotes_and_fences_do_not_count(self):
        for body in ("「已作廢，沿革」", "取代上段。\n「已撤銷」", "“沿革，superseded”",
                     "```text\n已過時\n沿革\n```\n取代前段"):
            with self.subTest(body=body):
                self.assertEqual(self.findings(body), [])

    def test_retired_statuses_and_non_rule_cards_are_exempt(self):
        for status in memspec.STOP_GATE_SILENT_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(self.findings("取代上段\n沿革", status=status), [])
        for kind in ("reference", "project"):
            self.assertEqual(self.findings("取代上段\n沿革", kind=kind), [])

    def test_git_history_pointer_does_not_count(self):
        pointer = "「舊版段落已移出卡片，要看改版經過查記憶庫 git 紀錄。」"
        self.assertEqual(self.findings(pointer), [])
        self.assertEqual(self.findings(pointer + "\n沿革"), [])


if __name__ == "__main__":
    unittest.main(argv=[argument for argument in sys.argv if argument != "--selftest"])
