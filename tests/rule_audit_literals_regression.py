"""Old/source audit literals are data; proposed new instructions remain checked.

Since owner 2026-10-03 (6f6fce1; CORE-25) a forbidden-text finding on a write is a
note and the write goes through, and the Stop gate leaves a short note instead of
blocking. "Checked" is therefore the note, not a denial."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import time
import unittest
import json
from unittest.mock import patch
import rule_source_consumers_regression as fixtures
import pretooluse_gate as pretool
import stop_gate as stop
from epitype import rule_pipeline

WRITE_NOTE = '既有裁定提醒（本次寫入放行）'


class AuditLiterals(unittest.TestCase):
    setUp = fixtures.SourceConsumers.setUp
    card = fixtures.SourceConsumers.card

    def test_real_two_host_audit_write_and_new_instruction(self):
        for host, tool in (('claude', 'Write'), ('codex', 'apply_patch')):
            self.card(host=host)
            target = self.root / 'audit_candidate.py'
            for index, (text, noted) in enumerate((
                ("patch = {'old_literal': 'badold', 'new_literal': 'corrected'}\n", False),
                ("patch = {'exact_quote': 'badold', 'author': 'original'}\n", False),
                ("patch = {'old_literal': 'badold', 'new_literal': 'badold'}\n", True),
                ("patch = {'old_literal': 'badold'}\nexecute('badold')\n", True),
                ("patch = {'old_literal': execute('badold')}\n", True),
                ("emit({'old_literal':'badold'}['old_literal'])\n", True),
                ("def sample(values={'exact_quote':'badold'}):\n    return values['exact_quote']\nemit(sample())\n", True),
                ("data={'old_literal':'badold'}\nemit(data['old_literal'])\n", True),
                ("consumer.value={'old_literal':'badold'}\n", True),
                ("patch = {'old_literal': 'badold'\n", True),
            )):
                with self.subTest(host=host, case=index):
                    tool_input = ({'file_path': str(target), 'content': text}
                        if host == 'claude' else {'patch': '*** Begin Patch\n*** Add File: '
                            + str(target) + '\n' + ''.join('+' + line + '\n' for line in text.splitlines())
                            + '*** End Patch'})
                    out = pretool._handle({'session_id': host + '-audit-' + str(index),
                        'cwd': str(self.root), 'tool_name': tool, 'tool_input': tool_input},
                        time.monotonic(), [])
                    output = (out or {}).get('hookSpecificOutput', {})
                    self.assertNotIn('permissionDecision', output)
                    self.assertEqual(WRITE_NOTE in output.get('additionalContext', ''), noted,
                                     output.get('additionalContext', ''))

    def test_unicode_offsets_and_exact_scope(self):
        self.assertTrue(pretool._audit_literal_only('舊錯誤', "patch={'old_literal':'前文舊錯誤後文'}"))
        self.assertFalse(pretool._audit_literal_only('舊錯誤', "patch={'old_literal':'舊錯誤'}\nprint('舊錯誤')"))
        self.assertFalse(pretool._audit_literal_only('bad', "patch={'description':'bad'}"))
        self.assertFalse(pretool._audit_literal_only('bad', "emit({'old_literal':'bad'}['old_literal'])"))
        self.assertFalse(pretool._audit_literal_only('bad', "def sample(values={'exact_quote':'bad'}):\n    return values['exact_quote']\nemit(sample())"))
        self.assertTrue(pretool._audit_literal_only('bad', '{"old_literal":"bad","new_literal":"fixed"}'))

    def test_note_policy_is_advisory_at_actual_write_and_stop_consumers(self):
        for host in ('claude', 'codex'):
            self.card(host=host)
            text = '\n'.join(line for line in self.target.read_text(encoding='utf-8').splitlines()
                             if not line.startswith('rule_provenance:')) + '\n'
            text = text.replace('---\n', '---\non_hit: note\n', 1)
            document = rule_pipeline.bind_payload(self.document, text, reviewer='synthetic-read-reviewer')
            text = text.replace('---\n', '---\nrule_provenance: '
                                + json.dumps(document, ensure_ascii=False) + '\n', 1)
            self.target.write_text(text, encoding='utf-8')
            notices = []
            found = pretool._forbidden_write({'session_id': host + '-note-write', 'cwd': str(self.root)},
                {'vaults': [self.vault]}, self.root / 'artifact.md', ['badold'], 'badold',
                time.monotonic(), notices)
            self.assertIsNone(found)
            self.assertTrue(any(self.document['quote'] in note for note in notices))
            captured = []
            with patch.object(stop, 'leave_note', side_effect=lambda *args: captured.append(args)):
                out = stop._handle({'session_id': host + '-note-stop', 'cwd': str(self.root),
                    'last_assistant_message': 'badold'}, time.monotonic(), [])
            self.assertIsNone(out)
            self.assertTrue(captured)
            # The Stop note names the card and the check only; it no longer quotes the
            # rule or the matched text (b3ae5d3), so the card is identified by its key.
            self.assertIn(self.document['key'] + ' 的 forbidden', captured[-1][-1])
            self.assertNotIn('別說', captured[-1][-1])

    def test_write_says_advisory_and_unadopted_lines_only_where_they_concern_it(self):
        def write(target, text):
            notices = []
            found = pretool._forbidden_write({'session_id': 'scoped-write', 'cwd': str(self.root)},
                {'vaults': [self.vault]}, target, [text], text, time.monotonic(), notices)
            return found, notices
        artifact = self.root / 'artifact.md'
        self.card(scope={'level': 'case', 'project': str(self.root), 'case': 'A', 'actions': ['*'],
                         'conditions': [], 'expiry': 'unspecified', 'exceptions': []}, kind='ai-derived')
        found, notices = write(artifact, 'unrelated text')
        self.assertIsNone(found)
        self.assertFalse(any('限定參考' in note for note in notices), notices)
        found, notices = write(artifact, 'badold')
        self.assertIsNone(found)
        self.assertTrue(any('限定參考' in note and 'case context' in note for note in notices), notices)
        self.card()
        self.target.write_text(self.target.read_text(encoding='utf-8') + '未綁定的新增正文。\n',
                               encoding='utf-8')
        found, notices = write(artifact, 'unrelated text')
        self.assertIsNone(found)
        self.assertFalse(any('來源裁定未採用' in note for note in notices), notices)
        found, notices = write(self.target, 'unrelated text')
        self.assertTrue(any(f'[{self.target.name}] 來源裁定未採用' in note for note in notices), notices)


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
