"""Real write and compact entrypoints preserve the event's source scope."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import time
import unittest
import rule_source_consumers_regression as source_fixture
import pretooluse_gate as pretool
import sessionstart_hook as sessionstart
from epitype import compact_map, rule_context


class SourceEntrypoints(unittest.TestCase):
    setUp = source_fixture.SourceConsumers.setUp
    card = source_fixture.SourceConsumers.card

    def test_real_write_entrypoint_matches_project_and_reports_unknown_case(self):
        for host, tool in (('claude', 'Write'), ('codex', 'apply_patch')):
            with self.subTest(host=host):
                scope = {'level': 'project', 'project': str(self.root / 'project'),
                         'case': None, 'actions': ['*'], 'conditions': [],
                         'expiry': 'unspecified', 'exceptions': []}
                self.card(host=host, scope=scope)
                target = self.root / 'artifact.md'
                tool_input = ({'file_path': str(target), 'content': 'badold'} if host == 'claude'
                    else {'patch': '*** Begin Patch\n*** Add File: ' + str(target)
                          + '\n+badold\n*** End Patch'})
                event = {'session_id': host + '-project-write', 'tool_name': tool,
                         'tool_input': tool_input, 'cwd': str(self.root / 'project' / 'sub')}
                denied = pretool._handle(event, time.monotonic(), [])
                self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
                outside = {**event, 'session_id': host + '-outside-write',
                           'cwd': str(self.root / 'project-sibling')}
                allowed = pretool._handle(outside, time.monotonic(), [])
                self.assertTrue(allowed is None or
                    allowed['hookSpecificOutput'].get('permissionDecision') != 'deny')
                scope['level'], scope['case'] = 'case', 'A'
                self.card(host=host, scope=scope)
                notices = []
                unknown = pretool._handle({**event, 'session_id': host + '-unknown-write'},
                                         time.monotonic(), notices)
                self.assertTrue(unknown is None or
                    unknown['hookSpecificOutput'].get('permissionDecision') != 'deny')
                self.assertIsNotNone(unknown)
                self.assertIn('case context', unknown['hookSpecificOutput']['additionalContext'])

    def test_real_compact_entrypoint_preserves_scope_and_reports_budget_gap(self):
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text('{}\n', encoding='utf-8')
        scope = {'level': 'case', 'project': str(self.root), 'case': 'A',
                 'actions': ['*'], 'conditions': [], 'expiry': 'unspecified', 'exceptions': []}
        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                self.card(host=host, kind='ai-derived', scope=scope)
                session = host + '-compact-source'
                rule_context.record(self.vault, session, self.target.name)
                destination = compact_map.handoff_destination(self.vault, session, transcript)
                rule_context.snapshot([self.vault], session, destination)
                event = {'source': 'compact', 'session_id': session,
                         'transcript_path': str(transcript), 'cwd': str(self.root),
                         'rule_context': {'case': 'A'}}
                trace = {}
                result = sessionstart._handle(event, time.monotonic(), trace=trace)
                text = result['hookSpecificOutput']['additionalContext']
                self.assertIn(self.document['quote'], text)
                self.assertIn('synthetic-read-ai', text)
                self.assertNotIn('限定參考', text)
                unknown_trace = {}
                unknown = sessionstart._handle({k: v for k, v in event.items() if k != 'rule_context'},
                    time.monotonic(), trace=unknown_trace)
                self.assertIn('限定參考', unknown['hookSpecificOutput']['additionalContext'])
                self.assertTrue(any('case context' in item for item in
                    unknown_trace['rule_context_diagnostics']))
                # The original still verifies; only delivery size changes.
                diagnostics = []
                self.assertEqual(rule_context.restore(destination, [self.vault], budget=1,
                    event=event, diagnostics=diagnostics), '')
                self.assertTrue(any('budget' in item for item in diagnostics))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
