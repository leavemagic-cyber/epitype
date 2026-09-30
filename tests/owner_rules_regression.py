import sys; sys.dont_write_bytecode = True
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import pretooluse_gate as pretool
from epitype import owner_rules


class OwnerRules(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp
    write_card = fixtures.ActionGuardRegression.write_card

    def test_generated_block_rewrite_and_removal_denied_both_hosts(self):
        self.write_card('decision-cap.md', name='decision-cap', decision_key='cap',
                        decision_status='active', decided_by='owner-explicit',
                        current_decision_at='2026-10-01', owner_quote='同時最多三個，驗收也算',
                        description='owner 裁定', handoff_rule='true',
                        forbidden=['"唯讀分析與偵察不佔名額"'])
        target = self.root / 'handoff.md'
        before = owner_rules.generate('進度：完成 A。', [self.vault])
        target.write_text(before, encoding='utf-8')
        self.assertIn('同時最多三個，驗收也算', before)
        for replacement in (before.replace('最多三個', '不限人數'), '進度：完成 B。'):
            for tool, payload in (
                    ('Write', {'file_path': str(target), 'content': replacement}),
                    ('apply_patch', {'input': f'*** Begin Patch\n*** Delete File: {target}\n*** Add File: {target}\n'
                     + '\n'.join('+' + line for line in replacement.splitlines()) + '\n*** End Patch'})):
                value = pretool._handle({'tool_name': tool, 'tool_input': payload,
                                       'session_id': 'owner-rules'}, time.monotonic(), [])
                self.assertIsNotNone(value, tool)
                self.assertEqual(value['hookSpecificOutput']['permissionDecision'], 'deny')
                self.assertIn('裁定', value['hookSpecificOutput']['permissionDecisionReason'])
        deleted = pretool._handle({'tool_name': 'apply_patch', 'session_id': 'owner-delete',
                    'tool_input': {'input': f'*** Begin Patch\n*** Delete File: {target}\n*** End Patch'}},
                    time.monotonic(), [])
        self.assertEqual(deleted['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIsNone(owner_rules.violation(before, before.replace('完成 A', '完成 B'), [self.vault]))


if __name__ == '__main__':
    unittest.main()
