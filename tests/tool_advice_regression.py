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

    def call(self, tool_name, tool_input):
        return pretool._handle({'session_id': 'advice-test',
            'transcript_path': str(self.root / 'thread.jsonl'),
            'tool_name': tool_name, 'tool_input': tool_input}, time.monotonic(), [])
    def test_three_text_cards_both_hosts_and_session_dedup(self):
        for number, fragment in enumerate(('search', 'inspect', 'review')):
            self.write_card(f'advice-{number}.md', name=f'advice-{number}',
                            description=f'原規則 {number}', unenforceable='需要判斷',
                            advise_tool='Bash', advise_all_of=[fragment],
                            advise_text=f'原文提醒 {number}')
        for host_tool in ('Bash', 'exec_command'):
            for number, fragment in enumerate(('search', 'inspect', 'review')):
                event = {'session_id': f'advice-{host_tool}', 'tool_name': host_tool,
                         'transcript_path': str(self.root / f'{host_tool}.jsonl'),
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
        event = {'session_id': 'changed-' + self.root.name, 'tool_name': 'Bash', 'transcript_path': str(self.root / 'transcript.jsonl'),
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
        event = {'session_id': 'retry-' + self.root.name, 'tool_name': 'Bash',
                 'transcript_path': str(self.root / 'retry.jsonl'), 'tool_input': {'command': 'echo x'}}
        claims = []
        first = pretool._handle(event, time.monotonic(), [], advice_claims=claims)
        self.assertIn('先查證', first['hookSpecificOutput']['additionalContext'])
        self.assertTrue(claims)
        pretool._finish_advice(claims, False)
        second = pretool._handle(event, time.monotonic(), [], advice_claims=claims)
        self.assertIn('先查證', second['hookSpecificOutput']['additionalContext'])
        pretool._finish_advice(claims, True)
        self.assertIsNone(pretool._handle(event, time.monotonic(), []))

    # advise_also_tool + advise_also_any_of: one card, a second trigger. The dispatch
    # rule fires on every Agent call and only on shell calls that hand work elsewhere.
    DISPATCH_TEXT = '派工限定範圍和回傳大小。'
    DISPATCH_FRAGMENTS = ['codex exec', 'dots-bridge', 'kaggle kernels push']

    def dispatch_card(self, filename='rule-core-97.md', name='rule-core-97', core=True, **extra):
        fields = dict(name=name, description='dispatch rule', advise_tool='Agent',
                      advise_also_tool='Bash|PowerShell',
                      advise_also_any_of=self.DISPATCH_FRAGMENTS,
                      advise_text=self.DISPATCH_TEXT)
        if core:
            fields.update(layer='resident', text=f'"CORE-97. {self.DISPATCH_TEXT}"')
        fields.update(extra)
        self.write_card(filename, **fields)

    def context(self, tool_name, tool_input):
        # A fresh session per call: the per-session dedupe must not hide a second hit.
        self.calls = getattr(self, 'calls', 0) + 1
        session = f'dispatch-{self.calls}'
        result = pretool._handle({'session_id': session,
            'transcript_path': str(self.root / f'{session}.jsonl'),
            'tool_name': tool_name, 'tool_input': tool_input}, time.monotonic(), [])
        return (result or {}).get('hookSpecificOutput', {}).get('additionalContext', '')

    def set_core_only(self, value):
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config[memspec.CONFIG_CORE_ONLY_BINDING_FIELD] = value
        self.config.write_text(json.dumps(config), encoding='utf-8')

    def test_extra_binding_fires_on_dispatch_calls_and_matching_shell_commands(self):
        self.dispatch_card()
        line = memspec.ACTION_GUARD_ADVISORY_LINE.format(card='rule-core-97', excerpt=self.DISPATCH_TEXT)
        cases = [
            ('Agent', {'prompt': 'read the docs', 'subagent_type': 'scout'}, True),
            ('Task', {'prompt': 'read the docs'}, True),
            ('spawn_agent', {'message': 'read the docs', 'model': 'gpt-5'}, True),
            ('Bash', {'command': 'codex exec -m gpt-5 "review the diff"'}, True),
            ('PowerShell', {'command': 'python C:/tools/dots-bridge/send.py --task review'}, True),
            # Codex sends its shell call as exec_command with the command in `cmd`.
            ('exec_command', {'cmd': 'kaggle kernels push -p ./kernel'}, True),
            ('exec_command', {'cmd': 'codex exec "x"', 'shell': 'powershell'}, True),
            ('Shell', {'command': 'codex exec "x"'}, True),
            ('Bash', {'command': 'ls'}, False),
            ('PowerShell', {'command': 'Get-ChildItem'}, False),
            ('exec_command', {'cmd': 'git status'}, False),
            # Fragments match what the call does, not the prose describing it.
            ('Bash', {'command': 'echo ok', 'description': 'then codex exec'}, False),
            # A tool neither binding names stays silent even with a fragment in it.
            ('Read', {'file_path': 'C:/notes/codex exec.md'}, False),
        ]
        for tool_name, tool_input, expected in cases:
            with self.subTest(tool=tool_name, tool_input=tool_input):
                context = self.context(tool_name, tool_input)
                if expected:
                    self.assertEqual(context.count(line), 1, context)
                else:
                    self.assertNotIn(self.DISPATCH_TEXT, context)

    def test_primary_binding_keeps_its_all_of_and_one_line_per_call(self):
        self.write_card('both.md', name='both', advise_tool='Bash', advise_all_of=['git', 'push'],
                        advise_also_tool='Bash|PowerShell', advise_also_any_of=['deploy', 'push'],
                        advise_text='both bindings')
        self.assertIn('both bindings', self.context('Bash', {'command': 'git push origin'}))
        self.assertIn('both bindings', self.context('PowerShell', {'command': 'deploy.ps1'}))
        self.assertIn('both bindings', self.context('Bash', {'command': 'run deploy'}))
        self.assertNotIn('both bindings', self.context('PowerShell', {'command': 'git status'}))
        # Both bindings match this call; the card still says its line once.
        self.assertEqual(self.context('Bash', {'command': 'git push'}).count('both bindings'), 1)

    def test_card_without_extra_binding_behaves_as_before(self):
        self.write_card('agent-only.md', name='agent-only', advise_tool='Agent',
                        advise_text='agent only reminder')
        self.assertIn('agent only reminder', self.context('Agent', {'prompt': 'x'}))
        self.assertNotIn('agent only reminder', self.context('Bash', {'command': 'codex exec "x"'}))
        guard, = pretool._guards(self.vault, time.monotonic(), [], event={'session_id': 's'})
        self.assertEqual((guard.advise_also_tool, guard.advise_also_fragments), ('', ()))

    def test_core_only_binding_silences_a_non_core_extra_binding(self):
        self.dispatch_card()
        self.dispatch_card('scar-dispatch.md', name='scar-dispatch', core=False,
                           advise_tool='Read', advise_text='non-core dispatch reminder')
        self.set_core_only(True)
        context = self.context('Bash', {'command': 'codex exec "x"'})
        self.assertIn(self.DISPATCH_TEXT, context)
        self.assertNotIn('non-core dispatch reminder', context)
        self.set_core_only(False)
        self.assertIn('non-core dispatch reminder', self.context('Bash', {'command': 'codex exec "x"'}))

    def test_half_declared_extra_binding_is_a_named_defect(self):
        # Half a binding cannot be guessed into a whole one: a tool without fragments
        # would fire on every shell call, so the card reports itself instead.
        for filename, extra in (('no-fragments.md', {'advise_also_tool': 'Bash'}),
                                ('no-tool.md', {'advise_also_any_of': ['codex exec']}),
                                ('no-primary.md', {'advise_tool': None, 'advise_also_tool': 'Bash',
                                                   'advise_also_any_of': ['codex exec']})):
            with self.subTest(card=filename):
                for stale in self.vault.glob('*.md'):
                    stale.unlink()
                fields = {'name': filename[:-3], 'advise_tool': 'Agent', 'advise_text': 'half'}
                fields.update(extra)
                self.write_card(filename, **{key: value for key, value in fields.items() if value is not None})
                defects = []
                guards = pretool._guards(self.vault, time.monotonic(), defects, event={'session_id': 's'})
                self.assertEqual(guards, [])
                self.assertTrue(any(filename[:-3] in defect and 'advise_also' in defect
                                    for defect in defects), defects)
                self.assertNotIn('half', self.context('Agent', {'prompt': 'x'}))
                self.assertNotIn('half', self.context('Bash', {'command': 'codex exec "x"'}))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
