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
        for fragment in ('提交 97b8789', 'session 405ad9d1-02d6-4547-9217-9a11eda1098d',
                         '備份 config.json.bak_20261028_trim', r'C:\Users\x\AppData\Local\Temp\claude\a',
                         'rollout-2026-10-14T10-27-39-01a1'):
            with self.subTest(fragment=fragment):
                reasons = self.noise(card('feedback', '做法：先查來源。' + fragment))
                self.assertEqual(len(reasons), 1)

    def test_rules_dates_quotes_and_examples_pass(self):
        body = ('**Why:** owner 2026-10-28「誰提的？97b8789 不是我提的」。\n'
                '清掉自己留的 .bak（先比對內容）；日期 20261028。\n'
                '```\ngit show 97b8789\n```\n')
        self.assertEqual(self.noise(card('feedback', body)), [])

    def test_pointer_and_retired_cards_are_out_of_scope(self):
        record = '提交 97b8789'
        self.assertEqual(self.noise(card('reference', record)), [])
        self.assertEqual(self.noise(card('project', record)), [])
        self.assertEqual(self.noise(card('feedback', record, status='superseded')), [])

    def test_date_joined_to_id_is_not_read_as_hash(self):
        reasons = self.noise(card('decision', '來源 20260907-9f156894953d'))
        self.assertEqual(len(reasons), 1)
        self.assertNotIn('20260907', reasons[0])


if __name__ == '__main__':
    unittest.main(argv=[argument for argument in sys.argv if argument != "--selftest"])
