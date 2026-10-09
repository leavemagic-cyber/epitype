import sys; sys.dont_write_bytecode = True
import json
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import pretooluse_gate as pretool
from epitype import memspec, owner_rules

NOTE = '裁定生成段提醒（本次寫入放行）'


class OwnerRules(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp
    write_card = fixtures.ActionGuardRegression.write_card

    def test_generated_block_rewrite_and_removal_noted_both_hosts(self):
        self.write_card('decision-cap.md', name='decision-cap', decision_key='cap',
                        status='active', decided_by='owner-explicit',
                        current_decision_at='2026-10-01', owner_quote='同時最多三個，驗收也算',
                        description='owner 裁定', handoff_rule='true',
                        forbidden=['"唯讀分析與偵察不佔名額"'])
        target = self.root / 'handoff.md'
        before = owner_rules.generate('進度：完成 A。', [self.vault])
        target.write_text(before, encoding='utf-8')
        self.assertIn('同時最多三個，驗收也算', before)
        # Rewriting or removing the generated ruling block is advisory: the call is
        # allowed, the note names the ruling, and each call leaves a write_note row.
        # One session per call: the same note is otherwise shown once per session.
        calls = 0
        for replacement in (before.replace('最多三個', '不限人數'), '進度：完成 B。'):
            for tool, payload in (
                    ('Write', {'file_path': str(target), 'content': replacement}),
                    ('apply_patch', {'input': f'*** Begin Patch\n*** Delete File: {target}\n*** Add File: {target}\n'
                     + '\n'.join('+' + line for line in replacement.splitlines()) + '\n*** End Patch'})):
                calls += 1
                value = pretool._handle({'tool_name': tool, 'tool_input': payload,
                                       'session_id': f'owner-rules-{calls}'}, time.monotonic(), [])
                self.assertIsNotNone(value, tool)
                self.assertNotIn('permissionDecision', value['hookSpecificOutput'])
                self.assertIn(NOTE, value['hookSpecificOutput']['additionalContext'])
                self.assertIn('裁定', value['hookSpecificOutput']['additionalContext'])
        deleted = pretool._handle({'tool_name': 'apply_patch', 'session_id': 'owner-delete',
                    'tool_input': {'input': f'*** Begin Patch\n*** Delete File: {target}\n*** End Patch'}},
                    time.monotonic(), [])
        self.assertNotIn('permissionDecision', deleted['hookSpecificOutput'])
        self.assertIn(NOTE, deleted['hookSpecificOutput']['additionalContext'])
        log = self.vault / memspec.GATE_LOG_FILENAME
        rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines() if line.strip()]
        # One row per reviewed entry: two Writes, two patches of Delete+Add, one Delete.
        self.assertEqual(sum(row.get('kind') == 'write_note' and row.get('rule') == 'owner_rulings'
                             and row.get('target') == str(target) for row in rows), 7)
        self.assertIsNone(owner_rules.violation(before, before.replace('完成 A', '完成 B'), [self.vault]))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
