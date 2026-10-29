"""Rule-like cards must not carry one-off work records in their body; all records are synthetic."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
import tempfile
import unittest

from epitype import card_lint


def card(kind, body, status=''):
    return ('---\nname: synthetic\ndescription: 2026-10-28 合成卡\nlast_verified_at: 2026-10-28\n'
            'aliases:\n  - 合成卡\n' + (f'status: {status}\n' if status else '')
            + f'metadata:\n  type: {kind}\n---\n' + body + '\n')


class BodyNoise(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'synthetic.md'

    def noise(self, text):
        self.path.write_text(text, encoding='utf-8')
        _kind, findings = card_lint.check_card(self.path, 'synthetic.md')
        return [reason for level, rule, reason in findings
                if rule == 'body-noise' and level == card_lint.FAIL]

    def test_work_records_fail_in_rule_like_cards(self):
        for fragment in ('提交 abcdef0', 'session ' + '00000000' + '-0000-4000-8000-000000000000',
                         '備份 sample.json.bak_20260101_fixture', 'C:' + '\\Users\\' + 'SyntheticFixture' + r'\AppData\Local\Temp\fixture\a',
                         'rollout-2026-01-01T00-00-00-0000'):
            with self.subTest(fragment=fragment):
                reasons = self.noise(card('feedback', '做法：先查來源。' + fragment))
                self.assertEqual(len(reasons), 1)

    def test_rules_dates_quotes_and_examples_pass(self):
        body = ('**Why:** synthetic owner 2026-01-01「範例食譜的版本是 abcdef0」。\n'
                '範例備份副檔名是 .bak；日期 20260101。\n'
                '```\ngit show abcdef0\n```\n')
        self.assertEqual(self.noise(card('feedback', body)), [])

    def test_pointer_and_retired_cards_are_out_of_scope(self):
        record = '提交 abcdef0'
        self.assertEqual(self.noise(card('reference', record)), [])
        self.assertEqual(self.noise(card('project', record)), [])
        self.assertEqual(self.noise(card('feedback', record, status='superseded')), [])

    def test_date_joined_to_id_is_not_read_as_hash(self):
        reasons = self.noise(card('decision', '來源 20260101-abcdef012345'))
        self.assertEqual(len(reasons), 1)
        self.assertNotIn('20260101', reasons[0])


if __name__ == '__main__':
    unittest.main(argv=[argument for argument in sys.argv if argument != "--selftest"])
