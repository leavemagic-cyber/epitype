"""card_lint and rule_examples must agree on unenforceable cards' examples; all cards are synthetic."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
import tempfile
import unittest

from epitype import card_lint, rule_examples


def card(extra):
    return ('---\nname: 合成判斷卡\ndescription: 2026-11-01 合成卡\nlast_verified_at: 2026-11-01\n'
            'aliases:\n  - 合成卡\nmetadata:\n  type: feedback\n' + extra
            + 'example_blocks:\n  - 這一句是合成範例\n---\n\n內文\n')


# 例句要求只對核心規則卡（resident 層、text 以 CORE-NN. 開頭）成立。
CORE = 'layer: resident\ntext: CORE-99. 合成核心規則\n'


class UnenforceableExamples(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'feedback_synthetic.md'

    def example_fails(self, text):
        self.path.write_text(text, encoding='utf-8')
        _kind, findings = card_lint.check_card(self.path, 'feedback_synthetic.md')
        return [reason for level, rule, reason in findings
                if rule == 'example' and level == card_lint.FAIL]

    def test_unarmed_unenforceable_one_sided_examples_pass_both_checks(self):
        text = card(CORE + 'unenforceable: 判斷型（合成）\n')
        self.assertEqual(self.example_fails(text), [])
        self.assertEqual(rule_examples.check_card(self.path), [])

    def test_one_sided_examples_still_fail_without_the_exemption(self):
        for extra in ('', 'unenforceable: 判斷型（合成）\nforbidden:\n  - 合成禁語\n'):
            with self.subTest(extra=extra):
                reasons = self.example_fails(card(CORE + extra))
                self.assertTrue(any('example_allows' in reason for reason in reasons), reasons)

    def test_a_core_card_whose_blocking_example_passes_still_fails(self):
        text = card(CORE + 'forbidden:\n  - 合成禁語\nexample_allows:\n  - 一般的句子\n')
        reasons = self.example_fails(text)
        self.assertTrue(any('這一句是合成範例' in reason for reason in reasons), reasons)

    def test_non_core_cards_are_not_held_to_examples(self):
        # owner 2026-11-13：核心規則綁動作只做派工跟交付。非核心卡的觸發欄位不綁動作，
        # 一側例句、例句與禁語不符都不再判 FAIL。
        for extra in ('', 'forbidden:\n  - 合成禁語\n'):
            with self.subTest(extra=extra):
                self.assertEqual(self.example_fails(card(extra)), [])


if __name__ == '__main__':
    unittest.main(argv=[argument for argument in sys.argv if argument != "--selftest"])
