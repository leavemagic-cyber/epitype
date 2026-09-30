import sys; sys.dont_write_bytecode = True
import json
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import pretooluse_gate as pretool
import precompact_hook as precompact
import sessionstart_hook as sessionstart
from epitype import compact_map, rule_context


class RuleContextRegression(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp
    write_card = fixtures.ActionGuardRegression.write_card

    def test_advice_then_compact_restore_both_hosts(self):
        self.write_card('text.md', name='text', description='先查原始證據',
                        advise_tool='Bash', advise_text='先查證據')
        transcript = self.root / 'trace.jsonl'
        transcript.write_text('{}\n', encoding='utf-8')
        for tool in ('Bash', 'exec_command'):
            event = {'session_id': 'compact-' + tool, 'transcript_path': str(transcript),
                     'tool_name': tool, 'tool_input': {'command': 'echo x'}}
            self.assertIsNotNone(pretool._handle(event, time.monotonic(), []))
            precompact._handle(event, time.monotonic(), learn=False)
            result = sessionstart._handle({**event, 'source': 'compact'}, time.monotonic())
            self.assertIn('先查原始證據', result['hookSpecificOutput']['additionalContext'])

    def test_blocked_card_recorded(self):
        self.write_card('guard.md', name='guard', description='禁止測試動作',
                        guard_tool='Bash', guard_all_of=['dangerous'], guard_advice='停下')
        result = pretool._handle({'session_id': 'blocked', 'tool_name': 'Bash',
                                'tool_input': {'command': 'dangerous'}}, time.monotonic(), [])
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(json.loads(rule_context.state_path(self.vault, 'blocked').read_text()), ['guard.md'])

    def test_stop_audit_records_path_without_changing_decision_identity(self):
        import stop_gate
        from epitype import gates_report, memspec
        self.write_card('different-filename.md', name='different-filename', description='不要重開裁定')
        stop_gate._audit({memspec.CONFIG_VAULTS_FIELD: [self.vault]}, 'owner-key', 'forbidden',
                         time.monotonic(), 'stop-record', card_path=self.vault / 'different-filename.md')
        self.assertEqual(json.loads(rule_context.state_path(self.vault, 'stop-record').read_text()),
                         ['different-filename.md'])
        self.assertEqual(gates_report.load_rows(self.vault / memspec.GATE_LOG_FILENAME)[0][-1].label, 'owner-key')
        self.assertEqual(gates_report._row_label({'kind': memspec.ACTION_GUARD_LOG_KIND,
                        'card': 'guard', 'card_path': 'guard.md'}), 'guard')

    def test_budget_expiry_and_path_boundary(self):
        for number in range(20):
            self.write_card(f'rule-{number}.md', name=f'rule-{number}', description='規則描述' * 90)
            rule_context.record(self.vault, 's', f'rule-{number}.md')
        self.write_card('expired.md', name='expired', description='不應補送', valid_until='2000-01-01')
        rule_context.record(self.vault, 's', 'expired.md')
        self.write_card('retired.md', name='retired', description='已被取代不補送', status='superseded')
        rule_context.record(self.vault, 's', 'retired.md')
        destination = self.root / 'handoff.md'
        rule_context.snapshot([self.vault], 's', destination)
        result = rule_context.restore(destination, [self.vault])
        self.assertLessEqual(len(result.encode('utf-8')), 1800)
        self.assertNotIn('不應補送', result)
        self.assertNotIn('已被取代不補送', result)
        self.assertEqual(rule_context.restore(destination, [self.root / 'other']), '')


if __name__ == '__main__':
    unittest.main()
