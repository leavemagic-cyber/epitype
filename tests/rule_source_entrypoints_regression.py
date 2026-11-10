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
import precompact_hook as precompact
from epitype import compact_map, hook_context, rule_context

NOTE = '既有裁定提醒（本次寫入放行）'


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
                # A forbidden write is advisory: allowed, with the ruling note in context.
                noted = pretool._handle(event, time.monotonic(), [])
                self.assertNotIn('permissionDecision', noted['hookSpecificOutput'])
                self.assertIn(NOTE, noted['hookSpecificOutput']['additionalContext'])
                outside = {**event, 'session_id': host + '-outside-write',
                           'cwd': str(self.root / 'project-sibling')}
                allowed = pretool._handle(outside, time.monotonic(), [])
                self.assertTrue(allowed is None or (
                    allowed['hookSpecificOutput'].get('permissionDecision') != 'deny'
                    and NOTE not in allowed['hookSpecificOutput'].get('additionalContext', '')))
                scope['level'], scope['case'] = 'case', 'A'
                self.card(host=host, scope=scope)
                notices = []
                unknown = pretool._handle({**event, 'session_id': host + '-unknown-write'},
                                         time.monotonic(), notices)
                self.assertTrue(unknown is None or
                    unknown['hookSpecificOutput'].get('permissionDecision') != 'deny')
                self.assertIsNotNone(unknown)
                self.assertIn('case context', unknown['hookSpecificOutput']['additionalContext'])
                self.assertNotIn(NOTE, unknown['hookSpecificOutput']['additionalContext'])

    def test_real_compact_delivers_map_without_replaying_legacy_scoped_snapshot(self):
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text('{}\n', encoding='utf-8')
        scope = {'level': 'case', 'project': str(self.root), 'case': 'A',
                 'actions': ['*'], 'conditions': [], 'expiry': 'unspecified', 'exceptions': []}
        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                self.card(host=host, kind='ai-derived', scope=scope)
                session = host + '-compact-source'
                event = {'source': 'compact', 'session_id': session,
                         'transcript_path': str(transcript), 'cwd': str(self.root),
                         'rule_context': {'case': 'A'}}
                context_id = hook_context.identity(event)
                self.assertIsNotNone(context_id)
                rule_context.record(self.vault, context_id, self.target.name)
                destination = compact_map.handoff_destination(self.vault, session, transcript)
                rule_context.snapshot([self.vault], context_id, destination, context_id=context_id)
                precompact._handle(event, time.monotonic(), learn=False)
                map_path = precompact._map_destination(self.vault, event, transcript)
                self.assertTrue(map_path.is_file())
                trace = {}
                result = sessionstart._handle(event, time.monotonic(), trace=trace)
                context = result['hookSpecificOutput']['additionalContext']
                self.assertIn(str(map_path), context)
                self.assertNotIn(self.document['text'], context)
                self.assertNotIn(self.document['quote'], context)
                self.assertNotIn('synthetic-read-ai', context)
                self.assertNotIn('rule_context_diagnostics', trace)
                # The explicit legacy utility still checks scope and budget;
                # SessionStart no longer invokes it to reload rules.
                text = rule_context.restore(destination, [self.vault], event=event, context_id=context_id)
                self.assertIn(self.document['text'], text)
                self.assertIn('"case":"A"', text)
                self.assertIn('適用範圍：', text)
                self.assertNotIn(self.document['quote'], text)
                self.assertIn('synthetic-read-ai', text)
                self.assertNotIn('限定參考', text)
                unknown_trace = {}
                unknown = sessionstart._handle({k: v for k, v in event.items() if k != 'rule_context'},
                    time.monotonic(), trace=unknown_trace)
                self.assertEqual(unknown['hookSpecificOutput']['additionalContext'], context)
                self.assertNotIn('rule_context_diagnostics', unknown_trace)
                diagnostics = []
                reference = rule_context.restore(destination, [self.vault],
                    event={k: v for k, v in event.items() if k != 'rule_context'},
                    diagnostics=diagnostics, context_id=context_id)
                self.assertIn('限定參考', reference)
                self.assertTrue(any('case context' in item for item in diagnostics))
                # The current review is intact; only delivery size changes.
                diagnostics = []
                self.assertEqual(rule_context.restore(destination, [self.vault], budget=1,
                    event=event, diagnostics=diagnostics, context_id=context_id), '')
                self.assertTrue(any('budget' in item for item in diagnostics))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
