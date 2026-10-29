"""Owner attribution, incremental recovery and read-only index locators."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import owner_index as owner


def message(text, role='user', **fields):
    return dict(type=role, message={'content': text}, timestamp='2026-10-01T00:00:00Z', **fields)


def encode(rows):
    return b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8') for row in rows)


def prompt_and_answer(answer='只收您的話', session='synthetic-session'):
    question = '您希望收哪些內容？'
    prompt = {'type': 'assistant', 'sessionId': session, 'uuid': 'prompt-record', 'message': {
        'content': [{'type': 'tool_use', 'name': 'AskUserQuestion', 'id': 'ask-one', 'input': {
            'questions': [{'question': question, 'options': [
                {'label': '只收您的話', 'description': '保留原話'}]}]}}]}}
    result = {'type': 'user', 'sessionId': session, 'uuid': 'answer-record',
              'sourceToolAssistantUUID': 'prompt-record', 'timestamp': '2026-10-02T00:00:00Z',
              'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'ask-one'}]},
              'toolUseResult': {'answers': {question: answer}}}
    return prompt, result


class OwnerIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='epitype-owner-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source.jsonl'
        self.index = self.root / 'index.sqlite3'

    def build(self, rows):
        self.source.write_bytes(encode(rows))
        return owner.update(self.index, [self.source], rebuild=True)

    def find(self, *words, **kwargs):
        return owner.find(words, path=self.index, **kwargs)['records']

    def test_owner_only_with_offsets_ids_context_and_exact_digest(self):
        rows = [message('前一段解釋', 'assistant'), message('保留我的原話', sessionId='s', uuid='u'),
                {'type': 'attachment', 'attachment': {'type': 'queued_command',
                 'origin': {'kind': 'human'}, 'prompt': '稍後保留排隊訊息'}},
                message('錯誤摘要', isCompactSummary=True), message('腳本話語', entrypoint='sdk-python'),
                message('腳本來源', origin='codex_exec'),
                message('工具說的', isMeta=True), message('子代理', isSidechain=True),
                {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '重複事件'}}]
        before = encode(rows)
        report = self.build(rows)
        self.assertEqual(2, report['messages'])
        row = self.find('我的原話')[0]
        self.assertEqual(('U', 's', 'u', '前一段解釋'),
                         (row['role'], row['session_id'], row['message_uuid'], row['previous']))
        self.assertEqual(len(encode(rows[:1])), row['byte_offset'])
        self.assertEqual(hashlib.sha256(encode(rows[1:2])).hexdigest(), row['record_sha256'])
        self.assertFalse(row['verified'])
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual('Q', self.find('排隊')[0]['role'])
        self.assertEqual([], self.find('前一段解釋'))

    def test_codex_origin_metadata_and_duplicate_event_exclusion(self):
        def codex(origin):
            return [{'type': 'session_meta', 'payload': {'id': 'session', 'originator': origin}},
                    {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '不收事件'}},
                    {'type': 'response_item', 'timestamp': '2026-10-02', 'payload': {
                        'type': 'message', 'id': 'message-id', 'role': 'user', 'content': [
                            {'type': 'input_text', 'text': '真的輸入'}]}}]
        for origin in ('codex_exec', 'sdk-typescript'):
            self.assertEqual(0, self.build(codex(origin))['messages'])
        execution = codex('Codex Desktop')
        execution[0]['payload']['source'] = 'exec'
        self.assertEqual(0, self.build(execution)['messages'])
        self.assertEqual(1, self.build(codex('codex_cli_rs'))['messages'])
        self.assertEqual('session', self.find('真的輸入')[0]['session_id'])
        self.assertEqual('message-id', self.find('真的輸入')[0]['message_uuid'])

    def test_wrappers_commands_and_full_owner_text(self):
        self.build([message('<environment_context>不是原話</environment_context>\n真實輸入'),
                    message('《引用內容》\n接著的原話'),
                    message('<command-name>/do</command-name><command-args>請執行</command-args>'),
                    message('<system-reminder>只有注入</system-reminder>'),
                    message('# AGENTS.md instructions\n規則'), message('長文' * 12000)])
        self.assertEqual(4, len(self.find('原話')) + len(self.find('真實輸入'))
                         + len(self.find('請執行')) + len(self.find('長文')))
        self.assertEqual([], self.find('引用內容'))
        self.assertEqual([], self.find('只有注入'))
        self.assertEqual(24000, len(self.find('長文')[0]['text']))

    def test_inline_owner_reply_markers_exclude_the_copied_prefix_for_u_and_q(self):
        rows = []
        for marker in ('《', '<', '<-', '<='):
            text = '這是貼回來的助理文字' + marker + '只留下我自己的回覆'
            rows.extend([message(text), {'type': 'attachment', 'attachment': {
                'type': 'queued_command', 'origin': {'kind': 'human'}, 'prompt': text}}])
        self.build(rows)
        self.assertEqual([], self.find('貼回來的助理文字'))
        self.assertEqual(1, len(self.find('自己的回覆')))
        self.assertEqual(8, self.find('自己的回覆')[0]['locator_count'])
        self.assertTrue(all(row['text'] == '只留下我自己的回覆' for row in self.find('自己的回覆')))
        self.assertEqual('直接回覆', owner.owner_text('《直接回覆'))

    def test_codex_guardian_replay_is_not_a_new_owner_turn(self):
        records = [{'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': header},
                {'type': 'input_text', 'text': '[25] user: replayed words'},
                {'type': 'input_text', 'text': '[27] user: more old words'}]}}
                   for header in owner.REPLAY_HEADERS]
        self.build(records)
        self.assertEqual([], self.find('words'))
        self.build([{'type': 'session_meta', 'payload': {
            'thread_source': 'guardian_review', 'originator': 'codex_work_desktop'}},
                    message('history without its introductory header')])
        self.assertEqual([], self.find('history'))
        records[0]['payload']['content'] = [
            {'type': 'input_text', 'text': '[358] tool wait_agent result: old tool output'},
            {'type': 'input_text', 'text': '>>> APPROVAL REQUEST END\n'}]
        self.build(records[:1])
        self.assertEqual([], self.find('output'))

    def test_embedded_host_blocks_and_repeated_request_markers(self):
        wrapped = ('first new words\n<in-app-browser-context>ambient state</in-app-browser-context>\n'
                   '# Files mentioned by the user:\n## attached.png: /local/path\n'
                   '## My request:\nsecond new words\n<image src="attachment"/>\n'
                   '## My request:\nthird new words</image></image>')
        self.build([message(wrapped), message('<sample>long real paste</sample>' * 1000),
                    message('# Context from my IDE setup:\n## Active file: ambient.py\n'
                            '## Open tabs:\nambient.py\n## My request for Codex:\nnew IDE request'),
                    message('# Context from my IDE setup:\n## Active file: only-ambient.py')])
        row = self.find('first')[0]
        self.assertEqual('first new words\n\nsecond new words\n\nthird new words', row['text'])
        self.assertEqual([], self.find('ambient'))
        self.assertEqual([], self.find('attached'))
        self.assertEqual('new IDE request', self.find('IDE request')[0]['text'])
        self.assertEqual('<sample>long real paste</sample>' * 1000, self.find('long real paste')[0]['text'])

    def test_image_boundaries_preserve_new_input_and_strip_paired_content(self):
        for tag in ('<image src="attachment"/>', '<IMAGE src="attachment" / >'):
            with self.subTest(tag=tag):
                self.assertEqual('before\n\nafter', owner.owner_text(
                    'before\n' + tag + '\nafter</image></image>'))
        self.assertEqual('before\n\nafter', owner.owner_text(
            'before\n<image src="attachment">ambient image text</image>\nafter'))

    def test_identical_text_across_archive_and_original_has_distinct_locators(self):
        self.build([message('identical owner words')])
        archive = self.root / 'copy.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('copy.jsonl', self.source.read_bytes())
        report = owner.update(self.index, [archive])
        self.assertEqual((1, 2), (report['messages'], report['locators']))
        self.source.write_bytes(encode([message('replacement owner words')]))
        owner.update(self.index, [self.source])
        self.assertEqual(1, self.find('identical')[0]['locator_count'])
        self.assertTrue(self.find('identical')[0]['source'].endswith('copy.zip!copy.jsonl'))

    def test_automation_context_and_new_wrapped_human_answers(self):
        answer = '<send_user_message_question_reply>' + json.dumps([
            {'questionItemId': 'synthetic-question', 'question': 'AI question context',
             'answer': 'new human answer'}]) + '</send_user_message_question_reply>'
        self.build([message('<heartbeat><instructions>automated replay</instructions></heartbeat>'),
                    message('<subagent_notification>old agent report</subagent_notification>'),
                    message('<hook_prompt>hook feedback</hook_prompt>'),
                    message('<codex_delegation><input>relayed instructions</input></codex_delegation>'),
                    message('</input>\n</codex_delegation>'),
                    message('<codex_delegation><input>&lt;codex_delegation&gt;'
                            'encoded old instructions&lt;/codex_delegation&gt;'
                            '</input></codex_delegation>'),
                    message(answer), message('<realtime_delegation><input>fresh voice input</input>'
                                             '<transcript_delta>old voice turns</transcript_delta>'
                                             '</realtime_delegation>'),
                    message('<command-message>do</command-message><command-name>/do</command-name>'
                            '<command-args>fresh command arguments</command-args>')])
        self.assertEqual([], self.find('automated'))
        self.assertEqual([], self.find('old'))
        self.assertEqual([], self.find('feedback'))
        self.assertEqual([], self.find('relayed'))
        self.assertEqual([], self.find('codex_delegation'))
        row = self.find('human answer')[0]
        self.assertEqual(('new human answer', 'AI question context'), (row['text'],row['question']))
        self.assertEqual('fresh voice input', self.find('voice input')[0]['text'])
        self.assertEqual('fresh command arguments', self.find('command arguments')[0]['text'])

    def test_append_checks_only_a_small_boundary_and_rereads_on_mismatch(self):
        self.build([message('original'), message('padding' * owner.WINDOW_BYTES)])
        original = self.source.read_bytes()
        self.source.write_bytes(original.replace(b'original', b'replaced') + encode([message('new tail')]))
        report = owner.update(self.index, [self.source])
        self.assertEqual((1, 0, 2 * owner.WINDOW_BYTES),
                         (report['appended'], report['reread'], report['bytes_verified']))
        self.assertEqual('original', self.find('original')[0]['text'])
        modified = self.source.read_bytes().replace(b'new tail', b'bad tail')
        self.source.write_bytes(modified + encode([message('more tail')]))
        report = owner.update(self.index, [self.source])
        self.assertEqual(1, report['reread'])
        self.assertEqual([], self.find('original'))
        self.assertEqual('replaced', self.find('replaced')[0]['text'])

    def test_append_resumes_question_and_previous_without_parsing_prefix(self):
        prompt, answer = prompt_and_answer()
        self.build([message('請選一個', 'assistant'), prompt])
        previous_size = self.source.stat().st_size
        with self.source.open('ab') as stream:
            stream.write(encode([answer]))
        report = owner.update(self.index, [self.source])
        verified = min(previous_size, owner.WINDOW_BYTES) + min(self.source.stat().st_size, owner.WINDOW_BYTES)
        self.assertEqual((1, len(encode([answer])), verified),
                         (report['appended'], report['bytes_parsed'], report['bytes_verified']))
        row = self.find('希望收哪些')[0]
        self.assertEqual(('S', '您希望收哪些內容？', '請選一個'),
                         (row['role'], row['question'], row['previous']))
        self.assertEqual(previous_size, row['byte_offset'])
        # An unchanged source is never read, even for prefix verification.
        real_open = Path.open

        def unchanged_not_opened(path, *args, **kwargs):
            if path == self.source:
                raise AssertionError('opened an unchanged transcript')
            return real_open(path, *args, **kwargs)

        with patch.object(owner, '_consume', side_effect=AssertionError('reread')), \
                patch.object(owner, '_fingerprint', side_effect=AssertionError('rehash')), \
                patch.object(Path, 'open', unchanged_not_opened):
            unchanged = owner.update(self.index, [self.source])
        self.assertEqual((1, 0, 0), (unchanged['unchanged'], unchanged['bytes_parsed'], unchanged['bytes_verified']))

    def test_question_typed_answers_and_invalid_pairing(self):
        prompt, answer = prompt_and_answer('我自己打的回答')
        self.build([prompt, answer])
        self.assertEqual('U', self.find('自己打的')[0]['role'])
        answer['sourceToolAssistantUUID'] = 'other-record'
        self.assertEqual(0, self.build([prompt, answer])['messages'])

    def test_partial_last_row_is_consumed_only_after_newline(self):
        row = encode([message('等到寫完')])
        self.source.write_bytes(row[:15])
        self.assertEqual(0, owner.update(self.index, [self.source])['messages'])
        with self.source.open('ab') as stream:
            stream.write(row[15:-1])
        self.assertEqual(0, owner.update(self.index, [self.source])['messages'])
        with self.source.open('ab') as stream:
            stream.write(b'\n')
        self.assertEqual(1, owner.update(self.index, [self.source])['messages'])

    def test_shrink_same_size_and_prefix_edit_plus_append_replace_old_hits(self):
        self.build([message('older text'), message('extra text')])
        self.source.write_bytes(encode([message('newer text')]))
        self.assertEqual(1, owner.update(self.index, [self.source])['reread'])
        self.assertEqual([], self.find('older'))
        before = self.source.stat()
        self.source.write_bytes(encode([message('other text')]))
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns + 10000000))
        self.assertEqual(1, owner.update(self.index, [self.source])['reread'])
        self.assertEqual([], self.find('newer'))
        self.source.write_bytes(encode([message('third text'), message('appended')]))
        self.assertEqual(1, owner.update(self.index, [self.source])['reread'])
        self.assertEqual([], self.find('other'))
        self.assertEqual(1, len(self.find('third')))

    def test_archive_once_offsets_and_missing_optional_zstandard(self):
        archive = self.root / 'archive.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('old.jsonl', encode([message('封存原話')]).rstrip(b'\n'))
            bundle.writestr('old.jsonl.zst', b'not zstd')
        standalone = self.root / 'other.jsonl.zst'
        standalone.write_bytes(b'not zstd')
        with patch.object(owner, 'zstandard', None):
            report = owner.update(self.index, [archive, standalone])
            self.assertEqual((1, 2), (report['messages'], report['skipped_zst']))
            with patch.object(owner, '_consume', side_effect=AssertionError('archive reread')):
                self.assertEqual(1, owner.update(self.index, [archive])['unchanged'])
        self.assertTrue(self.find('封存')[0]['source'].endswith('archive.zip!old.jsonl'))

    @unittest.skipIf(owner.zstandard is None, 'optional zstandard is absent')
    def test_zstandard_standalone_and_zip_member_and_broken_stream(self):
        compressed = owner.zstandard.ZstdCompressor().compress(encode([message('壓縮原話')]))
        standalone = self.root / 'old.jsonl.zst'
        standalone.write_bytes(compressed)
        broken = self.root / 'broken.jsonl.zst'
        broken.write_bytes(b'invalid zstd')
        archive = self.root / 'compressed.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('inside.jsonl.zst', compressed)
        report = owner.update(self.index, [standalone, broken, archive])
        self.assertEqual((1, 2, 1), (report['messages'], report['locators'], len(report['errors'])))
        with closing(sqlite3.connect(self.index)) as db:
            sizes = db.execute('SELECT size,offset FROM sources').fetchall()
        self.assertEqual([(len(compressed), len(encode([message('壓縮原話')]))) ] * 2, sizes)
        self.assertEqual(2, owner.update(self.index, [standalone, archive])['unchanged'])

    def test_broken_sources_are_reported_without_losing_good_sources(self):
        broken = self.root / 'broken.zip'
        broken.write_bytes(b'not zip')
        self.source.write_bytes(b'not json\n' + b'{"type":"event_msg",broken}\n'
                                + encode([None, message('繼續索引')]))
        report = owner.update(self.index, [broken, self.root / 'missing.jsonl', self.source])
        self.assertEqual((1, 2, 3), (report['messages'], len(report['errors']), len(report['unreadable_rows'])))
        self.assertEqual(1, len(self.find('繼續索引')))

    def test_failed_read_rolls_back_source_and_retries(self):
        self.build([message('原本的文字')])
        self.source.write_bytes(encode([message('改寫的文字')]))
        consume = owner._consume

        def fail_after(*args):
            consume(*args)
            raise OSError('synthetic interrupted read')

        with patch.object(owner, '_consume', fail_after):
            self.assertEqual(1, len(owner.update(self.index, [self.source])['errors']))
        self.assertEqual(1, len(self.find('原本的')))
        self.assertEqual([], self.find('改寫的'))
        self.assertEqual(1, owner.update(self.index, [self.source])['messages'])
        self.assertEqual(1, len(self.find('改寫的')))

    def test_trigram_and_short_chinese_filters_normalization_and_sql_literals(self):
        self.build([message('中文，原話 ABC_123'), message('中文其它'),
                    {'type': 'attachment', 'attachment': {'type': 'queued_command',
                     'origin': {'kind': 'human'}, 'prompt': '中文原話'}, 'timestamp': '2026-10-03'}])
        self.assertEqual(2, len(self.find('中文', limit=2)))
        self.assertEqual(1, len(self.find('中文原話', 'abc123')))
        self.assertEqual(1, len(self.find('中文', after='2026-10-02', roles=('Q',))))
        self.assertEqual(1, len(self.find('中文', after='20261002', roles=('Q',))))
        self.assertEqual([], self.find('中文', after='2026-10-04'))
        self.assertEqual([], self.find('" OR 1=1 --'))
        with self.assertRaises(ValueError):
            self.find('中文', after='bad-date')
        with self.assertRaises(ValueError):
            self.find('中文', limit=0)
        with self.assertRaises(ValueError):
            self.find('中文', roles=('A',))
        with closing(sqlite3.connect(self.index)) as db:
            self.assertIn("tokenize='trigram'", db.execute(
                "SELECT sql FROM sqlite_master WHERE name='owner_fts'").fetchone()[0])

    def test_reordered_json_fields_are_still_owner_messages(self):
        self.build([{'message': {'content': [{'type': 'text', 'text': '順序不同的原話'}]},
                     'type': 'user'}])
        self.assertEqual(1, len(self.find('順序不同')))

    def test_default_roots_and_configured_archive_roots(self):
        home = self.root / 'home'
        projects = home / '.claude' / 'projects' / 'project'
        projects.mkdir(parents=True)
        (projects / 'old.jsonl.backup_fixture').write_bytes(encode([message('備份原話')]))
        sessions = home / '.codex' / 'sessions'
        sessions.mkdir(parents=True)
        (sessions / 'new.jsonl').write_bytes(encode([message('新原話')]))
        with patch.object(owner.Path, 'home', return_value=home), \
                patch.object(owner.memspec, 'config_options', return_value={}):
            self.assertEqual(2, owner.update(self.index)['messages'])
        with patch.object(owner.memspec, 'config_options', return_value={
                'owner_index_roots': [str(projects)]}):
            self.assertEqual([projects], owner.default_roots())

    def test_find_never_creates_a_missing_index(self):
        missing = self.root / 'not-created' / 'missing.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            owner.find(['任何原話'], path=missing)
        self.assertFalse(missing.parent.exists())

    def test_cli_build_update_find_and_source_locator_preserve_verification(self):
        self.source.write_bytes(encode([message('搜尋原話')]))
        def run(*args):
            result = subprocess.run([sys.executable, '-m', 'epitype', *args], cwd=ROOT,
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(0, result.returncode, result.stderr)
            return json.loads(result.stdout)
        self.assertEqual(1, run('owner-index', 'build', '--root', str(self.source),
                                '--index', str(self.index))['messages'])
        self.assertEqual(1, run('owner-index', 'update', '--root', str(self.source),
                                '--index', str(self.index))['unchanged'])
        row = run('owner-index', 'find', '搜尋原話', '--index', str(self.index))['records'][0]
        self.assertFalse(row['verified'])
        located = run('source', '--find', '搜尋原話', '--index', str(self.index))
        self.assertEqual(row, located['records'][0])
        verified = run('source', row['source'], '--offset', str(row['byte_offset']), '--limit', '1')
        self.assertEqual(row['record_sha256'], verified['records'][0]['record_sha256'])


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
