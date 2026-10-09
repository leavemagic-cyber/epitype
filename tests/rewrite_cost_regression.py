import sys; sys.dont_write_bytecode = True
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import stop_gate
from epitype import memspec, rewrite_cost


class RewriteCost(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp
    write_card = fixtures.ActionGuardRegression.write_card

    def noted(self, event):
        """Run the Stop gate, which never blocks, and return the (card, rule) pairs it noted."""
        log = self.vault / memspec.GATE_LOG_FILENAME
        def rows():
            return ([json.loads(line) for line in log.read_text(encoding='utf-8').splitlines() if line.strip()]
                    if log.is_file() else [])
        before = len(rows())
        self.assertIsNone(stop_gate._handle(event, time.monotonic(), []))
        return [(row.get('decision'), row.get('rule')) for row in rows()[before:]
                if row.get('kind') == memspec.STOP_NOTE_LOG_KIND]

    def test_required_wording_and_forbidden_hazard_are_both_noted(self):
        self.write_card('mixed.md', name='mixed', require_when='fixed', require_text='cause',
                        require_on_hit='note', forbidden=['hide the error'], description='保留安全禁止')
        event = {'session_id': 'cost-' + self.root.name, 'last_assistant_message': 'fixed'}
        self.assertEqual(self.noted(event), [('mixed', 'require')])
        self.assertEqual(self.noted({**event, 'last_assistant_message': 'hide the error'}),
                         [('mixed', 'forbidden')])
        rows = [SimpleNamespace(label='mixed', kind='stop_block', rule='require'),
                SimpleNamespace(label='mixed', kind='stop_block', rule='forbidden')]
        result = rewrite_cost.report([self.vault], rows)[0]
        self.assertEqual(result['observed_blocks'], 2)
        self.assertEqual(result['same_record_policy_blocks'], 1)
        self.assertIn('UNVERIFIED', result['useful_content'])
        self.write_card('second.md', name='second', require_when='fixed', require_text='verified',
                        description='真正缺少查證的關卡')
        # One finding per turn is recorded; both cards miss their required wording here.
        self.assertEqual([rule for _card, rule in self.noted(
            {**event, 'session_id': 'hard-second-' + self.root.name})], ['require'])


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
