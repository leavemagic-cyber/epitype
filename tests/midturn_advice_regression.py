import sys; sys.dont_write_bytecode = True
"""An owner message sent mid-turn gets one reminder until the AI answers it in text."""
import json
import os
import time
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
from rule_source_fixtures import owner_bound_card
import pretooluse_gate as pretool
from epitype import memspec

TEMPLATE = 'owner 中途問：『{message}』。先停下，用白話回答（現在在做什麼、做到哪、下一步），再繼續。'
QUESTION = '目前進度?我看不懂現在在幹嘛'


def claude_rows(*, answered=False, origin='human', prompt=QUESTION, source='src-1'):
    rows = [
        {'type': 'user', 'uuid': 'u1', 'message': {'role': 'user', 'content': '把測試跑完'}},
        {'type': 'assistant', 'uuid': 'a1', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'ls'}}]}},
        {'type': 'user', 'uuid': 'r1', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}]}},
        {'type': 'queue-operation', 'operation': 'remove', 'content': prompt, 'reason': 'absorbed_mid_turn'},
        {'type': 'attachment', 'uuid': 'att1', 'attachment': {
            'type': 'queued_command', 'prompt': prompt, 'source_uuid': source,
            'origin': {'kind': origin}, 'humanTurn': origin == 'human'}},
        {'type': 'assistant', 'uuid': 'a2', 'message': {'role': 'assistant', 'content': [
            {'type': 'thinking', 'thinking': 'keep going'}]}},
    ]
    if answered:
        rows.append({'type': 'assistant', 'uuid': 'a3', 'message': {'role': 'assistant', 'content': [
            {'type': 'text', 'text': '現在在跑測試，做完三項，下一步提交。'}]}})
    rows.append({'type': 'assistant', 'uuid': 'a4', 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 't2', 'name': 'Bash', 'input': {'command': 'pytest'}}]}})
    return rows


def codex_rows(*, provenance='user', after_tool=True, answered=False, text=QUESTION):
    rows = [{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-1'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'id': 'msg-open',
             'content': [{'type': 'input_text', 'text': '把測試跑完'}],
             'internal_chat_message_metadata_passthrough': {'content_item_kinds': ['user.text'],
             'content_item_metadata': [{'provenance': {'type': 'user'}}]}}}]
    if after_tool:
        rows += [{'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command',
                  'arguments': '{"cmd": "ls"}', 'call_id': 'c1'}},
                 {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'c1',
                  'output': 'ok'}}]
    kind = 'user.text' if provenance == 'user' else 'environments.environment_context'
    rows.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'id': 'msg-steer',
                 'content': [{'type': 'input_text', 'text': text}],
                 'internal_chat_message_metadata_passthrough': {'content_item_kinds': [kind],
                 'content_item_metadata': [{'provenance': {'type': provenance}}]}}})
    if answered:
        rows.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': '現在在跑測試。'}]}})
    return rows


class MidturnAdviceRegression(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp
    write_card = fixtures.ActionGuardRegression.write_card

    def core_card(self, filename='rule-core-96.md', name='rule-core-96', core=True, **extra):
        fields = dict(name=name, description='mid-turn', advise_on_midturn='true',
                      advise_midturn_text=f'"{TEMPLATE}"')
        if core:
            fields.update(layer='resident', text='"CORE-96. 新指令要回應。"')
        fields.update(extra)
        self.write_card(filename, **fields)

    def transcript(self, rows, name='thread.jsonl', filler=b''):
        path = self.root / name
        with path.open('wb') as stream:
            stream.write(filler)
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False).encode('utf-8') + b'\n')
        return path

    def context(self, path, session='mid', tool='Bash', tool_input=None, **extra):
        event = {'session_id': session, 'transcript_path': str(path), 'cwd': str(self.root),
                 'hook_event_name': 'PreToolUse', 'tool_name': tool,
                 'tool_input': tool_input or {'command': 'pytest -q'}, **extra}
        result = pretool._handle(event, time.monotonic(), [])
        return (result or {}).get('hookSpecificOutput', {}).get('additionalContext', '')

    def expected_line(self, card='rule-core-96', message=QUESTION):
        return memspec.ACTION_GUARD_ADVISORY_LINE.format(
            card=card, excerpt=TEMPLATE.replace('{message}', message))

    def test_unanswered_message_gets_one_reminder_once(self):
        self.core_card()
        path = self.transcript(claude_rows())
        self.assertEqual(self.context(path).count(self.expected_line()), 1)
        # Same owner message, another call target in the same session: said once only.
        self.assertNotIn(QUESTION, self.context(path, tool_input={'command': 'git status'}))

    def test_answered_message_gets_nothing(self):
        self.core_card()
        self.assertNotIn(QUESTION, self.context(self.transcript(claude_rows(answered=True))))

    def test_a_new_message_after_the_first_is_reminded_again(self):
        self.core_card()
        path = self.transcript(claude_rows())
        self.assertIn(QUESTION, self.context(path))
        later = '先停一下，改跑另一組'
        path = self.transcript(claude_rows(answered=True) + claude_rows(prompt=later, source='src-2')[3:5])
        self.assertEqual(self.context(path).count(self.expected_line(message=later)), 1)

    def test_non_human_queued_command_gets_nothing(self):
        self.core_card()
        for origin in ('task-notification', 'coordinator'):
            with self.subTest(origin=origin):
                self.assertNotIn(QUESTION, self.context(
                    self.transcript(claude_rows(origin=origin)), session='mid-' + origin))

    def test_slash_commands_and_wrapped_host_text_are_not_owner_words(self):
        self.core_card()
        for number, prompt in enumerate((
                '/goal clear', '/goal 把測試跑完', '/compact',
                '<system-reminder>\nThe user started your suggested background task x.\n</system-reminder>')):
            with self.subTest(prompt=prompt):
                context = self.context(self.transcript(claude_rows(prompt=prompt, source=f'w-{number}')),
                                       session=f'wrapped-{number}')
                self.assertNotIn('中途問', context)

    def test_owner_words_around_a_wrapper_or_with_angle_brackets_are_kept(self):
        self.core_card()
        # A paste is the owner's own input: its tags go, its text stays (closed or not).
        cases = (('<pasted_content id="p">\nerror log\n</pasted_content id="p">\n這是什麼錯',
                  'error log 這是什麼錯'),
                 ('<pasted_content id="q">\n【使用者回饋】進度通知太多', '【使用者回饋】進度通知太多'),
                 ('a < b 還是 <div> 壞了?', 'a < b 還是 <div> 壞了?'),
                 ('/c/work/out.txt 打不開', '/c/work/out.txt 打不開'))
        for number, (prompt, said) in enumerate(cases):
            with self.subTest(prompt=prompt):
                context = self.context(self.transcript(claude_rows(prompt=prompt, source=f'k-{number}')),
                                       session=f'kept-{number}')
                self.assertEqual(context.count(self.expected_line(message=said)), 1, context)

    def test_long_message_is_trimmed(self):
        self.core_card()
        long_text = '進度' * 100
        context = self.context(self.transcript(claude_rows(prompt=long_text)))
        trimmed = long_text[: memspec.MIDTURN_MESSAGE_MAX_CHARS] + '…'
        self.assertIn(self.expected_line(message=trimmed), context)

    def test_huge_transcript_reads_only_its_tail(self):
        self.core_card()
        filler_row = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'x', 'content': 'y' * 4000}]}}).encode() + b'\n'
        filler = filler_row * (16 * 1024 * 1024 // len(filler_row))
        # An unanswered message at the end of a 16 MB transcript is still found ...
        path = self.transcript(claude_rows(), name='huge.jsonl', filler=filler)
        self.assertEqual(self.context(path, session='huge').count(self.expected_line()), 1)
        reads = []
        original = pretool.Path.open

        class Spy:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *details):
                return self.inner.__exit__(*details)

            def seek(self, *arguments):
                return self.inner.seek(*arguments)

            def read(self, *arguments):
                chunk = self.inner.read(*arguments)
                reads.append(len(chunk))
                return chunk

        with patch.object(pretool.Path, 'open', lambda own, *a, **k: Spy(original(own, *a, **k))):
            found = pretool._midturn_pending({'transcript_path': str(path)})
        self.assertEqual(found, ('claude:src-1', QUESTION))
        self.assertTrue(reads)
        self.assertLessEqual(max(reads), memspec.MIDTURN_TAIL_BYTES)
        # ... and one followed by more than the tail window of tool traffic is not looked for.
        buried = self.transcript(claude_rows()[:5], name='buried.jsonl')
        with buried.open('ab') as stream:
            stream.write(filler_row * (memspec.MIDTURN_TAIL_BYTES // len(filler_row) + 2))
        self.assertNotIn(QUESTION, self.context(buried, session='buried'))

    def test_takes_priority_within_the_two_line_cap(self):
        for number in range(2):
            self.write_card(f'generic-{number}.md', name=f'generic-{number}', advise_tool='Bash',
                            advise_text=f'一般提醒 {number}')
        self.core_card()
        context = self.context(self.transcript(claude_rows()))
        lines = [line for line in context.splitlines() if line.startswith('Epitype [')]
        self.assertEqual(len(lines), 2, context)
        self.assertEqual(lines[0], self.expected_line())

    def test_core_only_binding(self):
        self.core_card('scar-mid.md', name='scar-mid', core=False)
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config[memspec.CONFIG_CORE_ONLY_BINDING_FIELD] = True
        self.config.write_text(json.dumps(config), encoding='utf-8')
        path = self.transcript(claude_rows())
        self.assertNotIn(QUESTION, self.context(path, session='core-only-1'))
        self.core_card()
        self.assertIn(self.expected_line(), self.context(path, session='core-only-2'))

    def test_card_without_the_field_and_half_declared_card(self):
        self.write_card('plain.md', name='plain', advise_tool='Agent', advise_text='agent only')
        path = self.transcript(claude_rows())
        self.assertNotIn(QUESTION, self.context(path, session='plain'))
        self.write_card('half.md', name='half', advise_on_midturn='true')
        # `false` is off and silent, even beside a leftover text.
        self.write_card('off.md', name='off', advise_on_midturn='false', advise_midturn_text='"x {message}"')
        # An empty fragment list alone is half a binding, reported, never a crash.
        self.write_card('empty-also.md', name='empty-also', advise_also_any_of='[]')
        defects = []
        cards = [guard.card for guard in pretool._guards(self.vault, time.monotonic(), defects,
                                                         event={'session_id': 's'})]
        self.assertEqual(cards, ['plain'])
        self.assertTrue(any('half' in item and 'advise_midturn_text' in item for item in defects), defects)
        self.assertTrue(any('empty-also' in item and 'advise_also_tool' in item for item in defects), defects)
        self.assertFalse(any('[off' in item or 'off.md' in item for item in defects), defects)

    def test_subagent_calls_are_not_reminded(self):
        self.core_card()
        path = self.transcript(claude_rows())
        self.assertNotIn(QUESTION, self.context(path, session='sub', agent_id='a1b2'))

    def test_codex_steer_message(self):
        self.core_card()
        rollout = self.root / '.codex' / 'sessions' / 'rollout-x.jsonl'
        rollout.parent.mkdir(parents=True)
        reply = ('<send_user_message_question_reply>\n[{"questionItemId":"x","answer":"A"}]\n'
                 '</send_user_message_question_reply>')
        cases = (({}, True), ({'answered': True}, False), ({'provenance': 'harness'}, False),
                 ({'after_tool': False}, False),
                 # The owner answering the AI's own question is not an interruption.
                 ({'text': reply}, False))
        for number, (shape, expected) in enumerate(cases):
            with self.subTest(shape=shape):
                self.transcript(codex_rows(**shape), name=str(rollout.relative_to(self.root)))
                context = self.context(rollout, session=f'codex-{number}', tool='exec_command',
                                       tool_input={'cmd': 'pytest -q'}, turn_id='turn-1')
                if expected:
                    self.assertEqual(context.count(self.expected_line()), 1, context)
                else:
                    self.assertNotIn('中途問', context)



CORE_QUOTE = 'CORE-96. 隔離測試 host 全域所有動作：owner 中途傳訊息時先回答。'


class MidturnBlockOnceRegression(unittest.TestCase):
    """`advise_midturn_on_hit: block`: the first call after an unanswered owner message is
    denied with the same plain line, once per message; never a second time, never a loop."""
    write_card = fixtures.ActionGuardRegression.write_card
    core_card = MidturnAdviceRegression.core_card
    transcript = MidturnAdviceRegression.transcript
    expected_line = MidturnAdviceRegression.expected_line

    def setUp(self):
        fixtures.ActionGuardRegression.setUp(self)
        # Deny claims are per-session markers under the temp root: keep them per test.
        temp = self.root / 'tmp'
        temp.mkdir()
        environment = patch.dict(os.environ, {'TEMP': str(temp), 'TMP': str(temp), 'TMPDIR': str(temp)})
        environment.start()
        self.addCleanup(environment.stop)

    def block_card(self, owner_bound=True, value='block', filename='rule-core-96.md',
                   name='rule-core-96', core=True):
        fields = dict(name=name, description='mid-turn', advise_on_midturn='true',
                      advise_midturn_text=f'"{TEMPLATE}"', advise_midturn_on_hit=value)
        if core:
            fields.update(layer='resident', text=f'"{CORE_QUOTE}"')
        text = fixtures.card(**fields)
        if owner_bound:
            text = owner_bound_card(text, self.vault / filename, source_root=self.root / 'sources',
                                    case_id=filename, quote=CORE_QUOTE if core else None)
        (self.vault / filename).write_text(text, encoding='utf-8')
        cache = pretool._guard_cache(self.vault)
        if cache.exists():
            cache.unlink()

    def output(self, path, session='mid', tool='Bash', tool_input=None, defects=None, **extra):
        event = {'session_id': session, 'transcript_path': str(path), 'cwd': str(self.root),
                 'hook_event_name': 'PreToolUse', 'tool_name': tool,
                 'tool_input': tool_input or {'command': 'pytest -q'}, **extra}
        if session is None:
            del event['session_id']
        result = pretool._handle(event, time.monotonic(), [] if defects is None else defects)
        return (result or {}).get('hookSpecificOutput', {})

    def assertDenied(self, output):
        self.assertEqual(output.get('permissionDecision'), 'deny', output)
        # The plain line, then how an AI that already answered recovers: resend.
        self.assertEqual(output.get('permissionDecisionReason'),
                         self.expected_line() + ' ' + memspec.MIDTURN_BLOCK_RETRY)

    def assertPassesSilently(self, output):
        self.assertNotEqual(output.get('permissionDecision'), 'deny', output)
        self.assertNotIn('中途問', json.dumps(output, ensure_ascii=False))

    def test_first_call_is_denied_once_then_never_again(self):
        self.block_card()
        path = self.transcript(claude_rows())
        self.assertDenied(self.output(path))
        # The AI still wrote no text: the same message neither blocks nor advises again.
        for command in ('pytest -q', 'git status', 'ls'):
            self.assertPassesSilently(self.output(path, tool_input={'command': command}))

    def test_a_new_message_after_a_blocked_one_is_blocked_once_too(self):
        self.block_card()
        rows = claude_rows()
        path = self.transcript(rows)
        self.assertDenied(self.output(path))
        rows = rows + [dict(rows[4], uuid='att2', attachment=dict(rows[4]['attachment'], source_uuid='src-2'))]
        self.transcript(rows)
        self.assertDenied(self.output(path))
        self.assertPassesSilently(self.output(path))

    def test_text_after_the_message_still_blocks_once_but_note_mode_stays_quiet(self):
        # Live 2026-11-17: an unrelated "Re-issuing…" line would count as an answer, so block
        # mode does not judge text; note mode keeps the text check.
        self.block_card()
        path = self.transcript(claude_rows(answered=True))
        self.assertDenied(self.output(path))
        self.assertPassesSilently(self.output(path))
        self.block_card(value='note')
        self.assertNotIn('中途問', json.dumps(self.output(path, session='note-answered'), ensure_ascii=False))

    def test_a_new_prompt_ends_the_message_turn(self):
        # Never a deny in a later turn for a message the AI never met with a tool call.
        self.block_card()
        rows = claude_rows(answered=True)
        rows.insert(-1, {'type': 'user', 'uuid': 'u2', 'message': {'role': 'user', 'content': '下一件事'}})
        self.assertPassesSilently(self.output(self.transcript(rows)))

    def test_without_block_the_card_only_advises(self):
        for value in (None, 'note'):
            with self.subTest(value=value):
                if value is None:
                    self.core_card()
                else:
                    self.block_card(value=value)
                output = self.output(self.transcript(claude_rows()), session=f'advice-{value}')
                self.assertNotIn('permissionDecision', output)
                self.assertEqual(output.get('additionalContext', '').count(self.expected_line()), 1)

    def test_an_unverified_card_cannot_deny(self):
        # CORE-08/CORE-25: only an owner-backed source may deny; this one only reminds.
        self.block_card(owner_bound=False)
        output = self.output(self.transcript(claude_rows()))
        self.assertNotIn('permissionDecision', output)
        self.assertIn(self.expected_line(), output.get('additionalContext', ''))

    def test_core_only_binding_still_applies(self):
        self.block_card(filename='scar-mid.md', name='scar-mid', core=False)
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config[memspec.CONFIG_CORE_ONLY_BINDING_FIELD] = True
        self.config.write_text(json.dumps(config), encoding='utf-8')
        path = self.transcript(claude_rows())
        self.assertPassesSilently(self.output(path, session='core-only-1'))
        self.block_card()
        self.assertDenied(self.output(path, session='core-only-2'))

    def test_subagent_and_sidechain_calls_are_not_blocked(self):
        self.block_card()
        path = self.transcript(claude_rows())
        self.assertPassesSilently(self.output(path, session='sub', agent_id='a1b2'))
        (self.root / 'subagents').mkdir()
        nested = self.transcript(claude_rows(), name='subagents/agent-x.jsonl')
        self.assertPassesSilently(self.output(nested, session='nested'))
        # Neither spent the main thread's one deny.
        self.assertDenied(self.output(path, session='sub'))

    def test_unreadable_transcript_fails_open(self):
        self.block_card()
        self.assertPassesSilently(self.output(self.root / 'missing.jsonl', session='gone'))
        self.assertPassesSilently(self.output(self.root, session='dir'))
        path = self.transcript(claude_rows())
        opener = Path.open

        def refuse(target, *arguments, **keywords):
            if Path(target) == path:
                raise PermissionError(13, 'denied')
            return opener(target, *arguments, **keywords)
        with patch.object(Path, 'open', refuse):
            self.assertPassesSilently(self.output(path, session='locked'))
        # The failed reads claimed nothing: once readable, the first call is denied.
        self.assertDenied(self.output(path, session='locked'))

    def test_no_durable_claim_means_no_deny(self):
        # A deny that could not be remembered would repeat on every call: fail open instead.
        self.block_card()
        path = self.transcript(claude_rows())
        self.assertPassesSilently(self.output(path, session=None))
        blocker = self.root / 'not-a-directory'
        blocker.write_text('x', encoding='utf-8')
        with patch.object(pretool, 'notice_marker_directory', lambda session: blocker / session):
            for _ in range(2):
                self.assertPassesSilently(self.output(path, session='unwritable'))

    def test_parallel_calls_of_one_response_deny_exactly_one(self):
        import threading
        self.block_card()
        path = self.transcript(claude_rows())
        event = {'session_id': 'parallel', 'transcript_path': str(path), 'cwd': str(self.root),
                 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}
        guard = next(item for item in pretool._guards(self.vault, time.monotonic(), [], event=event)
                     if item.midturn_text)
        self.assertEqual(guard.midturn_on_hit, 'block')
        gate = threading.Barrier(6)
        results = []

        def call():
            gate.wait()
            results.append(pretool._midturn_block(event, guard, [], []))
        threads = [threading.Thread(target=call) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        denied = [item for item in results if item is not None]
        self.assertEqual(len(results), 6)
        self.assertEqual(len(denied), 1, results)
        self.assertEqual(denied[0]['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_invalid_value_is_a_card_defect(self):
        self.block_card(value='always')
        defects = []
        output = self.output(self.transcript(claude_rows()), defects=defects)
        self.assertNotEqual(output.get('permissionDecision'), 'deny')
        self.assertTrue(any('advise_midturn_on_hit' in item for item in defects), defects)

    def test_codex_steer_message_is_blocked_once(self):
        self.block_card()
        rollout = self.root / '.codex' / 'sessions' / 'rollout-b.jsonl'
        rollout.parent.mkdir(parents=True)
        reply = ('<send_user_message_question_reply>\n[{"questionItemId":"x","answer":"A"}]\n'
                 '</send_user_message_question_reply>')
        for number, (shape, expected) in enumerate((({}, True), ({'answered': True}, True),
                                                    ({'text': reply}, False))):
            with self.subTest(shape=shape):
                self.transcript(codex_rows(**shape), name=str(rollout.relative_to(self.root)))
                arguments = dict(session=f'codex-b{number}', tool='exec_command',
                                 tool_input={'cmd': 'pytest -q'}, turn_id='turn-1')
                if expected:
                    self.assertDenied(self.output(rollout, **arguments))
                self.assertPassesSilently(self.output(rollout, **arguments))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
