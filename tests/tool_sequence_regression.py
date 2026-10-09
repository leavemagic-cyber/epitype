import sys; sys.dont_write_bytecode = True
import json
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import stop_gate
from epitype import memspec, tool_sequence


class ToolSequenceRegression(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp
    write_card = fixtures.ActionGuardRegression.write_card

    def event(self, host, commands, **extra):
        path = self.root / (host + '.jsonl')
        rows = [{'type': 'user', 'message': {'role': 'user', 'content': '請完成工作'}}]
        for number, command in enumerate(commands):
            if host == 'Claude':
                rows.append({'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'id': str(number), 'name': 'Bash', 'input': {'command': command}}]}})
            else:
                rows.append({'type': 'response_item', 'payload': {'type': 'function_call',
                    'name': 'exec_command', 'arguments': json.dumps({'cmd': command}), 'call_id': str(number)}})
        path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
        return {'session_id': 'seq-' + host + '-' + self.root.name, 'transcript_path': str(path),
                'last_assistant_message': '完成了', **extra}

    def noted(self, event):
        """Run the Stop gate, which never blocks, and return the (card, rule) pairs it noted."""
        log = self.vault / memspec.GATE_LOG_FILENAME
        def rows():
            return ([json.loads(line) for line in log.read_text(encoding='utf-8').splitlines() if line.strip()]
                    if log.is_file() else [])
        before = len(rows())
        self.assertIsNone(stop_gate._handle(event, time.monotonic(), []))
        return [(row.get('decision'), row.get('rule')) for row in rows()[before:]
                if row.get('kind') == memspec.STOP_NOTE_LOG_KIND]

    def test_three_order_cards_both_hosts_true_allow_and_note(self):
        for number, (before, after) in enumerate((('inspect', 'mutate'), ('backup', 'replace'), ('test', 'commit'))):
            self.write_card(f'seq-{number}.md', name=f'seq-{number}',
                            require_tool_before=[f'Bash::{before} => Bash::{after}'], audience='root')
        for host in ('Claude', 'Codex'):
            for number, (before, after) in enumerate((('inspect', 'mutate'), ('backup', 'replace'), ('test', 'commit'))):
                self.assertEqual(self.noted(self.event(host, [after])),
                                 [(f'seq-{number}', 'tool_sequence')], (host, after))
                self.assertEqual(self.noted(self.event(host, [before, after])), [], (host, before))

    def test_pair_and_audience(self):
        self.write_card('pair.md', name='pair', pair_tools=['Bash::open => Bash::close'], audience='root')
        self.assertEqual(self.noted(self.event('Claude', ['open'])), [('pair', 'tool_sequence')])
        self.assertEqual(self.noted(self.event('Claude', ['open', 'close'])), [])
        # Its own session: the same card and message are otherwise noted once per session.
        self.assertEqual(self.noted(self.event('Claude', ['open'], agent_id='sub',
                                               session_id='seq-sub-' + self.root.name)), [])
        self.assertFalse(tool_sequence.applies('sub', {}))

    def test_same_call_is_not_sequence_and_malformed_rejected(self):
        self.assertIsNotNone(tool_sequence.gap(['Bash::inspect => Bash::mutate'], (),
                                             [('Bash', {'command': 'inspect; mutate'})]))
        with self.assertRaises(ValueError):
            tool_sequence.gap(['bad'], (), ())


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
