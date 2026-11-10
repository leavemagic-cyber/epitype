"""Action reminders follow resolved dispatch and parsed heredoc conditions."""
import sys
sys.dont_write_bytecode = True
import json
from pathlib import Path
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import pretooluse_gate as gate
import stop_gate as stop
from epitype import memspec


class ActionAdvicePrecision(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp
    write_card = fixtures.ActionGuardRegression.write_card
    call = fixtures.ActionGuardRegression.call

    def test_host_or_type_resolved_model_is_not_missing(self):
        self.write_card('model.md', name='model', guard_tool='Task', on_hit='note',
                        guard_requires=['model'],
                        guard_when=['dispatch_model_unresolved=true'],
                        guard_advice='依任務品質選擇尚未決定的模型。')
        for tool, payload in (
                ('Agent', {'subagent_type': 'scout-haiku55'}),
                ('Task', {'subagent_type': 'general-purpose'}),
                ('Task', {'subagent_type': 'fork'}),
                ('spawn_agent', {'message': 'review'}),
                ('Task', {'model': 'explicit-model'})):
            with self.subTest(tool=tool, payload=payload):
                self.assertIsNone(self.call(tool, payload))

    def test_unresolved_dispatch_still_gets_a_reminder(self):
        self.write_card('model.md', name='model', guard_tool='Task', on_hit='note',
                        guard_requires=['model'],
                        guard_when=['dispatch_model_unresolved=true'],
                        guard_advice='依任務品質選擇尚未決定的模型。')
        self.assertIn('依任務品質', str(self.call('Task', {'prompt': 'review'})))

    def test_heredoc_body_and_quoting_determine_the_reminder_on_both_hosts(self):
        self.write_card('heredoc.md', name='heredoc', guard_tool='Bash', on_hit='note',
                        guard_when=['shell_unquoted_heredoc_backslash=true'],
                        guard_advice='未加引號的 heredoc 含反斜線時，改用可靠寫檔方式並回讀。')
        cases = [
            ("cat <<EOF\nC:\\work\\file\nEOF\n", True),
            ("cat <<-EOF\n\tC:\\work\\file\n\tEOF\n", True),
            ("cat <<'EOF'\nC:\\work\\file\nEOF\n", False),
            ('cat <<"EOF"\nC:\\work\\file\nEOF\n', False),
            ("cat <<\\EOF\nC:\\work\\file\nEOF\n", False),
            ("cat <<E'OF'\nC:\\work\\file\nEOF\n", False),
            ("cat <<EOF\nplain\nEOF\nprintf '%s' 'C:\\work'", False),
            ("printf '%s' '<<EOF C:\\work'", False),
            ("cat <<<'C:\\work'", False),
        ]
        for tool in ('Bash', 'exec_command'):
            for number, (command, expected) in enumerate(cases):
                with self.subTest(tool=tool, command=command):
                    result = gate._handle({'session_id': f'{tool}-{number}', 'tool_name': tool,
                        'tool_input': {'command' if tool == 'Bash' else 'cmd': command, 'shell': 'bash'}},
                        time.monotonic(), [])
                    context = (result or {}).get('hookSpecificOutput', {}).get('additionalContext', '')
                    self.assertEqual('可靠寫檔' in context, expected, context)

    def test_one_current_line_without_source_metadata(self):
        self.write_card('advice.md', name='advice', guard_tool='Bash', on_hit='note',
                        guard_all_of=['reminder-marker'], guard_advice='先查範圍，再執行。')
        result = self.call('Bash', {'command': 'reminder-marker'})
        context = result['hookSpecificOutput']['additionalContext']
        self.assertEqual(len(context.splitlines()), 1)
        self.assertEqual(context.count('先查範圍，再執行。'), 1)
        for metadata in ('作者：', '適用範圍：', '來源原話', '"actions"'):
            self.assertNotIn(metadata, context)

    def test_non_posix_shell_is_not_a_bash_heredoc(self):
        self.write_card('heredoc.md', name='heredoc', guard_tool='Bash', on_hit='note',
                        guard_when=['shell_unquoted_heredoc_backslash=true'],
                        guard_advice='可靠寫檔提醒')
        for shell in ('powershell', 'pwsh.exe', ''):
            with self.subTest(shell=shell):
                self.assertIsNone(self.call('exec_command', {
                    'cmd': 'cat <<EOF\nC:\\work\nEOF\n', 'shell': shell}))

    def transcript(self, status):
        path = self.root / 'thread.jsonl'
        rows = [
            {'type': 'user', 'message': {'content': '繼續'}},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
                'id': 'dispatch', 'name': 'Agent', 'input': {'subagent_type': 'reviewer'}}]}},
            {'type': 'user', 'toolUseResult': {'status': status, 'isAsync': status == 'async_launched'},
             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'dispatch',
                'content': 'launch receipt' if status == 'async_launched' else 'completed report'}]}},
        ]
        path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
        return path

    def test_async_launch_receipt_is_not_a_delegated_result(self):
        turn = stop._turn_context(self.transcript('async_launched'))
        self.assertEqual(turn.dispatch, '')
        self.assertTrue(turn.call_ids, 'call provenance is still retained')

    def test_completed_result_still_requires_verification(self):
        turn = stop._turn_context(self.transcript('completed'))
        self.assertEqual(turn.dispatch, 'Agent')
        from turn_check_regression import decision
        card = decision(turn_check=memspec.TURN_CHECK_UNVERIFIED_DELEGATION)
        self.assertIsNotNone(stop._unverified_delegation_gap(card, '三項已修好。', turn))

    def test_a_new_dispatch_does_not_count_as_verifying_an_earlier_result(self):
        path = self.transcript('completed')
        rows = [
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
                'id': 'next', 'name': 'Agent', 'input': {'subagent_type': 'reviewer'}}]}},
            {'type': 'user', 'toolUseResult': {'status': 'async_launched'},
             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'next',
                'content': 'launch receipt'}]}},
        ]
        with path.open('a', encoding='utf-8') as stream:
            stream.write('\n' + '\n'.join(json.dumps(row) for row in rows))
        from turn_check_regression import decision
        card = decision(turn_check=memspec.TURN_CHECK_UNVERIFIED_DELEGATION)
        turn = stop._turn_context(path)
        self.assertIsNotNone(stop._unverified_delegation_gap(card, '三項已修好。', turn))


if __name__ == '__main__':
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
