import sys; sys.dont_write_bytecode = True
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import stop_gate
from epitype import rewrite_cost


class RewriteCost(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp
    write_card = fixtures.ActionGuardRegression.write_card

    def test_required_wording_notes_but_forbidden_hazard_still_blocks(self):
        self.write_card('mixed.md', name='mixed', require_when='fixed', require_text='cause',
                        require_on_hit='note', forbidden=['hide the error'], description='保留安全禁止')
        event = {'session_id': 'cost-' + self.root.name, 'last_assistant_message': 'fixed'}
        self.assertIsNone(stop_gate._handle(event, time.monotonic(), []))
        self.assertIsNotNone(stop_gate._handle({**event, 'last_assistant_message': 'hide the error'}, time.monotonic(), []))
        rows = [SimpleNamespace(label='mixed', kind='stop_block', rule='require'),
                SimpleNamespace(label='mixed', kind='stop_block', rule='forbidden')]
        result = rewrite_cost.report([self.vault], rows)[0]
        self.assertEqual(result['observed_blocks'], 2)
        self.assertEqual(result['same_record_policy_blocks'], 1)
        self.assertIn('UNVERIFIED', result['useful_content'])
        self.write_card('second.md', name='second', require_when='fixed', require_text='verified',
                        description='真正缺少查證的關卡')
        self.assertIsNotNone(stop_gate._handle({**event, 'session_id': 'hard-second-' + self.root.name}, time.monotonic(), []))


if __name__ == '__main__':
    unittest.main()
