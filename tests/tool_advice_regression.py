import sys; sys.dont_write_bytecode = True
import json
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import unittest
from epitype import memspec
import pretooluse_gate as pretool


class ToolAdviceRegression(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp
    write_card = fixtures.ActionGuardRegression.write_card
    call = fixtures.ActionGuardRegression.call
    def test_three_text_cards_both_hosts_and_session_dedup(self):
        for number, fragment in enumerate(('search', 'inspect', 'review')):
            self.write_card(f'advice-{number}.md', name=f'advice-{number}',
                            description=f'原規則 {number}', unenforceable='需要判斷',
                            advise_tool='Bash', advise_all_of=[fragment],
                            advise_text=f'原文提醒 {number}')
        for host_tool in ('Bash', 'exec_command'):
            for number, fragment in enumerate(('search', 'inspect', 'review')):
                event = {'session_id': f'advice-{host_tool}', 'tool_name': host_tool,
                         'tool_input': {'command': fragment}}
                started = time.monotonic()
                result = pretool._handle(event, started, [])
                elapsed = time.monotonic() - started
                context = (result or {}).get('hookSpecificOutput', {}).get('additionalContext', '')
                self.assertIn(f'原文提醒 {number}', context)
                self.assertLess(elapsed, 2)
                self.assertIsNone(pretool._handle(event, time.monotonic(), []))

    def test_limit_two_and_retry_unserved_card(self):
        for number in range(3):
            self.write_card(f'limit-{number}.md', name=f'limit-{number}',
                            advise_tool='Bash', advise_text=f'有限提醒 {number}')
        first = self.call('Bash', {'command': 'echo x'})
        self.assertEqual(json.dumps(first, ensure_ascii=False).count('有限提醒'), 2)
        second = self.call('Bash', {'command': 'echo x'})
        self.assertEqual(json.dumps(second, ensure_ascii=False).count('有限提醒'), 1)

    def test_nonmatching_fragment_never_shown(self):
        self.write_card('miss.md', name='miss', advise_tool='Bash',
                        advise_all_of=['never-match'], advise_text='不要送我')
        self.assertIsNone(self.call('Bash', {'command': 'echo x'}))

    def test_changed_advice_is_delivered_again_for_the_same_target(self):
        event = {'session_id': 'changed-' + self.root.name, 'tool_name': 'Bash',
                 'tool_input': {'command': 'inspect example'}}
        self.write_card('changed.md', name='changed', advise_tool='Bash',
                        advise_text='previous advice')
        first = pretool._handle(event, time.monotonic(), [])
        self.assertIn('previous advice', first['hookSpecificOutput']['additionalContext'])
        self.write_card('changed.md', name='changed', advise_tool='Bash',
                        advise_text='replacement advice')
        second = pretool._handle(event, time.monotonic(), [])
        self.assertIn('replacement advice', (second or {}).get(
            'hookSpecificOutput', {}).get('additionalContext', ''))
        self.assertIsNone(pretool._handle(event, time.monotonic(), []))


    def test_dropped_output_releases_advice_for_retry(self):
        self.write_card('retry.md', name='retry', description='先查證', advise_tool='Bash')
        event = {'session_id': 'retry-' + self.root.name, 'tool_name': 'Bash', 'tool_input': {'command': 'echo x'}}
        claims = []
        first = pretool._handle(event, time.monotonic(), [], advice_claims=claims)
        self.assertIn('先查證', first['hookSpecificOutput']['additionalContext'])
        self.assertTrue(claims)
        pretool._finish_advice(claims, False)
        second = pretool._handle(event, time.monotonic(), [], advice_claims=claims)
        self.assertIn('先查證', second['hookSpecificOutput']['additionalContext'])
        pretool._finish_advice(claims, True)
        self.assertIsNone(pretool._handle(event, time.monotonic(), []))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
