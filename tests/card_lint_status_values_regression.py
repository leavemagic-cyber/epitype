"""card_lint accepts every status the gates already treat as not in effect."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import card_lint, memspec


class StatusValues(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-status-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "synthetic.md"

    def status_findings(self, kind, status):
        text = ("---\nname: synthetic\ndescription: 2026-11-13 合成卡\n"
                "last_verified_at: 2026-11-13\naliases:\n  - 合成卡\n"
                f"status: {status}\nmetadata:\n  type: {kind}\n---\nbody\n")
        self.path.write_text(text, encoding="utf-8")
        _kind, findings = card_lint.check_card(self.path, "synthetic.md")
        return [(level, reason) for level, rule, reason in findings if rule == "status"]

    def test_gate_silent_statuses_are_accepted_for_every_card_type(self):
        # 閘門把這些狀態當「不生效」照常略過；lint 判它們 FAIL 等於兩邊值域不同源。
        kinds = ("feedback", "project", "pending", "reference", "habit", "scar", "user")
        statuses = (memspec.ACTIVE_DECISION_STATUS, *memspec.STOP_GATE_SILENT_STATUSES)
        self.assertIn("retired", statuses)
        self.assertIn("closed", statuses)
        for kind in kinds:
            for status in statuses:
                with self.subTest(kind=kind, status=status):
                    self.assertEqual(self.status_findings(kind, status), [])

    def test_values_outside_the_set_still_fail_and_name_the_type(self):
        for kind, status in (("feedback", "draft"), ("project", "pending"), ("pending", "done")):
            with self.subTest(kind=kind, status=status):
                found = self.status_findings(kind, status)
                self.assertEqual([level for level, _reason in found], [card_lint.FAIL])
                self.assertIn(f"status={status}", found[0][1])
                self.assertIn(f"（{kind} 型）", found[0][1])

    def test_decision_chain_values_stay_two(self):
        # decision_lint 靠這兩個值守「每個 decision_key 恰一張現行卡」，不跟著放寬。
        self.assertEqual(memspec.DECISION_STATUS_VALUES,
                         (memspec.ACTIVE_DECISION_STATUS, memspec.SUPERSEDED_DECISION_STATUS))


if __name__ == "__main__":
    unittest.main(argv=[argument for argument in sys.argv if argument != "--selftest"])
