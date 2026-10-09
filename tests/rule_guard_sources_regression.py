"""Fresh source, complete interpretation and scope through real tool callers.

A guard denies only when its card says on_hit: block; the default since owner
2026-10-03 (6f6fce1; CORE-25) is a note, pinned in pretool_advisory_regression.
These tests are about the source binding, so their guard cards declare on_hit:
block inside the bound text, and the binding stays valid."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'adapters' / 'claude')]
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from epitype import memspec, rule_legacy, rule_pipeline
import _hook_common as common
import pretooluse_gate as gate


class GuardSources(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='epitype-guard-sources-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.card_path = self.vault / 'guard.md'
        self.source = self.root / 'owner.jsonl'
        self.config = self.root / 'config.json'
        common.write_config(self.config, [self.vault], budget=16000)
        environment = patch.dict(os.environ, {
            'EPITYPE_CONFIG': str(self.config), 'EPITYPE_DREAM_MODE': 'off',
            'HOME': str(self.root / 'home'), 'USERPROFILE': str(self.root / 'home'),
            'CODEX_HOME': str(self.root / 'home' / '.codex')})
        environment.start()
        self.addCleanup(environment.stop)

    def scope(self, **changes):
        value = {'level': 'global', 'project': None, 'case': None, 'actions': ['*'],
                 'conditions': [], 'expiry': 'unspecified', 'exceptions': []}
        value.update(changes)
        return value

    def card(self, *, host='codex', scope=None, kind='owner-directive', fields=None):
        scope = copy.deepcopy(scope or self.scope())
        extent = '隔離測試 host 全域所有動作' if scope['level'] == 'global' else (
            '隔離測試專案 ' + str(scope['project']) + '，案件 ' + str(scope['case']) + '，所有動作')
        for key in ('actions', 'conditions', 'exceptions'):
            if scope[key] and scope[key] != ['*']:
                extent += '，' + key + '：' + '、'.join(scope[key])
        if scope['expiry'] != 'unspecified':
            extent += '，有效至 ' + scope['expiry']
        quote = extent + '：採用此測試規則，禁止 guard-hit-marker。'
        stamp = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        expiry = (datetime.fromisoformat(stamp)+timedelta(hours=1)).isoformat()
        observed = 'guard-hit-marker 在隔離測試中依來源禁止。'
        sid, mid = 'synthetic-guard-session', 'synthetic-guard-owner'
        if host == 'claude':
            row = {'type': 'user', 'sessionId': sid, 'uuid': mid,
                   'message': {'role': 'user', 'content': [{'type': 'text', 'text': quote+'\n'+observed+' '+stamp+' '+expiry}]}}
        else:
            row = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'id': mid, 'content': [{'type': 'input_text', 'text': quote+'\n'+observed+' '+stamp+' '+expiry}]}}
        meta = (json.dumps({'type': 'session_meta', 'payload': {'id': sid}}) + '\n').encode()
        raw = (json.dumps(row, ensure_ascii=False) + '\n').encode()
        self.source.write_bytes(meta + raw)
        document = {'version': 1, 'key': 'synthetic-guard-rule', 'kind': kind,
            'author': 'owner' if kind == 'owner-directive' else 'synthetic-guard-ai',
            'quote': quote, 'text': quote if kind == 'owner-directive' else '執行整理：禁止 guard-hit-marker。',
            'sources': [{'path': str(self.source), 'byte_offset': len(meta), 'line': 2,
                'record_sha256': hashlib.sha256(raw).hexdigest(), 'session_id': sid,
                'message_id': mid, 'role': 'U'}], 'scope': scope, 'scope_basis': {
                'source': 0, 'quote': quote, 'reviewed_by': 'synthetic-guard-reviewer',
                'reviewed_at': '2026-10-03', 'rationale': 'The synthetic human source expressly declares scope.'},
            'supersedes': []}
        if kind == 'ai-derived':
            document['restriction_evidence'] = {
                'kind': kind, 'author': document['author'], 'challengeable': True,
                'text': document['text'], 'subject': 'guard-hit-marker',
                'basis': [{'quote': observed, 'observed_at': stamp, 'valid_until': expiry,
                           'source': document['sources'][0]}],
                'rationale': 'The original synthetic human source explicitly prohibits this marker.'}
        actual = {'name': document['key'], 'type': 'rule', 'status': 'active',
            'description': '隔離測試守衛。', 'guard_tool': 'Bash',
            'guard_all_of': ['guard-hit-marker'], 'guard_advice': '隔離測試建議。'}
        if fields is not None:
            actual = {'name': document['key'], 'type': 'rule', 'status': 'active', **fields}
        if 'guard_tool' in actual:
            actual.setdefault('on_hit', 'block')
        text = self.write_card(actual)
        document = rule_pipeline.bind_payload(document, text, reviewer='synthetic-guard-reviewer')
        text = text.replace('---\n', '---\nrule_provenance: '
            + json.dumps(json.dumps(document, ensure_ascii=False), ensure_ascii=False) + '\n', 1)
        self.card_path.write_text(text, encoding='utf-8')
        self.document = document
        return text

    def write_card(self, fields):
        text = '---\n' + '\n'.join(key + ': ' + json.dumps(value, ensure_ascii=False)
            for key, value in fields.items()) + '\n---\nSynthetic body.\n'
        self.card_path.write_text(text, encoding='utf-8')
        return text

    def legacy(self):
        return self.write_card({'name': 'legacy-guard', 'type': 'rule', 'guard_tool': 'Bash',
            'guard_all_of': ['guard-hit-marker'], 'guard_advice': 'legacy advice',
            'on_hit': 'block'})

    def event(self, *, tool='exec_command', context=None):
        event = {'tool_name': tool, 'tool_input': {'command': 'guard-hit-marker'},
                 'session_id': 'synthetic-guard-call', 'cwd': str(self.root)}
        if context is not None:
            event['rule_context'] = context
        return event

    def call(self, event=None, defects=None):
        return gate._handle(event or self.event(), time.monotonic(),
                            [] if defects is None else defects)

    def denial(self, value):
        return (value or {}).get('hookSpecificOutput', {}).get('permissionDecisionReason')

    def allowed_context(self, value):
        output = (value or {}).get('hookSpecificOutput', {})
        self.assertNotIn('permissionDecision', output)
        return output.get('additionalContext', '')

    def baseline(self):
        digest = hashlib.sha256(rule_pipeline._meaning(
            self.card_path.read_text(encoding='utf-8'), include_source=False).encode()).hexdigest()
        self.baseline_path = self.root / 'baseline.json'
        raw = json.dumps(rule_legacy.build_baseline({str(self.card_path): digest})).encode()
        self.baseline_path.write_bytes(raw)
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config.update({rule_legacy.PATH_OPTION: str(self.baseline_path),
                       rule_legacy.PIN_OPTION: hashlib.sha256(raw).hexdigest()})
        self.config.write_text(json.dumps(config), encoding='utf-8')

    def replace_same_stat(self, path, before, after):
        old = path.stat()
        raw = path.read_bytes()
        self.assertEqual(len(before), len(after))
        self.assertIn(before, raw)
        path.write_bytes(raw.replace(before, after))
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(path.stat().st_size, old.st_size)
        self.assertEqual(path.stat().st_mtime_ns, old.st_mtime_ns)

    def test_both_original_formats_and_host_tool_names(self):
        for host, tool in (('claude', 'Bash'), ('codex', 'exec_command')):
            with self.subTest(host=host):
                self.card(host=host)
                reason = self.denial(self.call(self.event(tool=tool)))
                self.assertIn(self.document['quote'], reason)
                self.assertIn('作者：owner', reason)
                self.assertIn('適用範圍：', reason)

    def test_project_inside_outside(self):
        self.card(scope=self.scope(level='project', project=str(self.root / 'project')))
        inside = self.event(context={'project': str(self.root / 'project')})
        self.assertIsNotNone(self.denial(self.call(inside)))
        self.assertIsNone(self.denial(self.call(self.event())))

    def test_case_inside_outside_unknown(self):
        self.card(scope=self.scope(level='case', project=str(self.root), case='case-A'))
        self.assertIsNotNone(self.denial(self.call(self.event(context={'case': 'case-A'}))))
        self.assertIsNone(self.denial(self.call(self.event(context={'case': 'case-B'}))))
        defects = []
        result = self.call(defects=defects)
        self.assertIsNone(self.denial(result))
        self.assertTrue(any('case context is unavailable' in item for item in defects))
        self.assertTrue(any(self.document['quote'] in item for item in defects))

    def test_uninterpreted_action_condition_exception_are_advisory(self):
        for clause in ({'actions': ['shell']}, {'conditions': ['special context']},
                       {'exceptions': ['known exception']}):
            with self.subTest(clause=clause):
                self.card(scope=self.scope(**clause))
                defects = []
                self.assertIsNone(self.denial(self.call(defects=defects)))
                self.assertTrue(any('ADVISORY' in item for item in defects))

    def test_expired_source_scope_never_enforces(self):
        self.card(scope=self.scope(expiry='2000-01-01T00:00:00Z'))
        self.assertIsNone(self.denial(self.call()))

    def test_original_tamper_same_size_and_mtime_ignores_warm_card_cache(self):
        self.card()
        self.assertIsNotNone(self.denial(self.call()))
        self.replace_same_stat(self.source, b'guard-hit-marker', b'guard-new-marker')
        defects = []
        self.assertIsNone(self.denial(self.call(defects=defects)))
        self.assertTrue(any('INVALID' in item for item in defects))

    def test_complete_body_and_execution_fields_read_fresh(self):
        for old, new in ((b'Synthetic body.', b'Synthetic evil.'),
                         (b'guard_all_of: ["guard-hit-marker"]',
                          b'guard_all_of: ["guard-new-marker"]')):
            with self.subTest(old=old):
                self.card()
                self.assertIsNotNone(self.denial(self.call()))
                self.replace_same_stat(self.card_path, old, new)
                defects = []
                self.assertIsNone(self.denial(self.call(defects=defects)))
                self.assertTrue(any('INVALID' in item for item in defects))

    def test_warm_without_event_discovers_but_never_enforces(self):
        self.card()
        self.assertEqual(gate._guards(self.vault, time.monotonic(), [], cap=100), [])
        self.assertIsNotNone(self.denial(self.call()))

    def test_cached_payload_cannot_supply_guard_or_scope(self):
        self.card(scope=self.scope(level='case', project=str(self.root), case='case-A'))
        self.call(self.event(context={'case': 'case-A'}))
        cache = gate._guard_cache(self.vault)
        value = json.loads(cache.read_text(encoding='utf-8'))
        entry = value['guards']['guard.md']
        entry.update({'substrings': ['different-marker'], 'advice': 'forged cache authority',
                      'source_scope': self.scope(), 'source_status': 'VERIFIED'})
        cache.write_text(json.dumps(value), encoding='utf-8')
        self.assertIsNotNone(self.denial(self.call(self.event(context={'case': 'case-A'}))))
        defects = []
        self.assertIsNone(self.denial(self.call(defects=defects)))
        self.assertTrue(any('ADVISORY' in item for item in defects))

    def test_all_known_guards_refresh_even_when_discovery_cap_is_zero(self):
        self.card()
        self.call()
        self.replace_same_stat(self.source, b'guard-hit-marker', b'guard-new-marker')
        defects = []
        self.assertEqual(gate._guards(self.vault, time.monotonic(), defects,
                                     cap=0, event=self.event()), [])
        self.assertTrue(any('INVALID' in item for item in defects))

    def test_legacy_unconfigured_is_compatible_and_explicitly_unverified(self):
        self.legacy()
        defects = []
        reason = self.denial(self.call(defects=defects))
        self.assertIn('來源未驗證', reason)
        self.assertIn('作者：UNVERIFIED', reason)
        self.assertTrue(any('UNVERIFIED' in item for item in defects))

    def test_frozen_legacy_fresh_meaning_rejects_tamper(self):
        self.legacy()
        self.baseline()
        self.assertIsNotNone(self.denial(self.call()))
        self.replace_same_stat(self.card_path, b'Synthetic body.', b'Synthetic evil.')
        defects = []
        self.assertIsNone(self.denial(self.call(defects=defects)))
        self.assertTrue(any('INVALID' in item for item in defects))

    def test_frozen_legacy_valid_json_tamper_is_not_cached(self):
        self.legacy()
        self.baseline()
        self.assertIsNotNone(self.denial(self.call()))
        raw = self.baseline_path.read_bytes()
        digest = next(iter(json.loads(raw)['entries'].values())).encode()
        self.replace_same_stat(self.baseline_path, digest, b'0' * 64)
        defects = []
        self.assertIsNone(self.denial(self.call(defects=defects)))
        self.assertTrue(any('pin' in item for item in defects))

    def test_new_legacy_card_not_automatically_enrolled(self):
        self.legacy()
        self.baseline()
        self.card_path.rename(self.vault / 'new-guard.md')
        defects = []
        self.assertIsNone(self.denial(self.call(defects=defects)))
        self.assertTrue(any('not in the frozen baseline' in item for item in defects))

    def test_legacy_cache_tamper_cannot_replace_fresh_guard(self):
        self.legacy()
        self.baseline()
        self.call()
        cache = gate._guard_cache(self.vault)
        value = json.loads(cache.read_text(encoding='utf-8'))
        value['guards']['guard.md'].update({'tool': 'different-tool',
            'substrings': ['different-marker'], 'advice': 'forged legacy authority'})
        cache.write_text(json.dumps(value), encoding='utf-8')
        reason = self.denial(self.call())
        self.assertIn('legacy advice', reason)
        self.assertNotIn('forged legacy authority', reason)

    def test_invalid_successor_is_diagnosed_instead_of_retiring_guard_silently(self):
        self.card(fields={'guard_tool': 'Bash', 'guard_all_of': ['guard-hit-marker'],
                          'status': 'superseded', 'superseded_by': 'missing-successor'})
        defects = []
        self.assertIsNone(self.denial(self.call(defects=defects)))
        self.assertTrue(any('INVALID' in item for item in defects))

    def test_missing_legacy_pin_cannot_guard(self):
        self.legacy()
        self.baseline()
        config = json.loads(self.config.read_text(encoding='utf-8'))
        del config[rule_legacy.PIN_OPTION]
        self.config.write_text(json.dumps(config), encoding='utf-8')
        defects = []
        self.assertIsNone(self.denial(self.call(defects=defects)))
        self.assertTrue(any('INVALID' in item for item in defects))

    def test_read_failure_and_timeout_are_visible(self):
        self.card()
        self.call()
        read = Path.read_text
        def unreadable(path, *args, **kwargs):
            if path == self.card_path:
                raise OSError('synthetic read failure')
            return read(path, *args, **kwargs)
        defects = []
        with patch.object(Path, 'read_text', unreadable):
            self.assertIsNone(self.denial(self.call(defects=defects)))
        self.assertTrue(any('unreadable' in item for item in defects))
        defects = []
        with patch.object(gate, 'expired', return_value=True):
            self.assertIsNone(self.denial(self.call(defects=defects)))
        self.assertTrue(any('budget exhausted' in item for item in defects))

    def test_advice_keeps_ai_author_original_full_scope_together(self):
        self.card(kind='ai-derived', scope=self.scope(level='case', project=str(self.root),
            case='case-A'), fields={'advise_tool': 'Bash', 'advise_all_of': ['guard-hit-marker'],
                                  'advise_text': 'synthetic scoped advice'})
        guards = gate._guards(self.vault, time.monotonic(), [],
                              event=self.event(context={'case': 'case-A'}))
        advice = guards[0].advise_text
        self.assertIn('作者：synthetic-guard-ai', advice)
        self.assertIn(self.document['quote'], advice)
        self.assertIn('case-A', advice)
        self.assertIn('適用範圍：', advice)
        self.assertIsNone(self.denial(self.call(self.event(context={'case': 'case-A'}))))

    def test_denial_does_not_truncate_verified_source_details(self):
        self.card(kind='ai-derived', fields={'guard_tool': 'Bash',
            'guard_all_of': ['guard-hit-marker'], 'guard_advice': 'long advice ' * 240})
        reason = self.denial(self.call())
        self.assertGreater(len(reason), 2000)
        self.assertIn(self.document['quote'], reason)
        self.assertIn('作者：synthetic-guard-ai', reason)
        self.assertIn('適用範圍：', reason)

    def test_repeated_calls_cannot_use_a_notice_marker_to_autoapprove(self):
        self.card()
        for _ in range(3):
            self.assertIsNotNone(self.denial(self.call()))

    def test_real_write_caller_obeys_source_case_and_reports_unknown(self):
        # The encoding check only notes (owner 2026-10-03; CORE-25). A Write is repaired
        # whatever the cards say, so an Edit, which only the card's note covers, is what
        # shows the source scope being obeyed.
        self.card(scope=self.scope(level='case', project=str(self.root), case='case-A'),
                  fields={'write_check': memspec.WRITE_CHECK_PS51_UTF8_BOM})
        target = self.root / 'synthetic.ps1'
        target.write_text('# 中文內容\nWrite-Output 1\n', encoding='utf-8')
        event = self.event(tool='Edit', context={'case': 'case-A'})
        event['tool_input'] = {'file_path': str(target), 'old_string': '1', 'new_string': '2'}
        context = self.allowed_context(self.call(event))
        self.assertIn('編碼提醒（本次寫入放行）', context)
        self.assertIn(self.document['quote'], context)
        self.assertIn('適用範圍：', context)
        event['rule_context']['case'] = 'case-B'
        event['session_id'] = 'synthetic-guard-call-case-b'
        self.assertNotIn('編碼提醒', self.allowed_context(self.call(event)))
        del event['rule_context']
        defects = []
        result = self.call(event, defects)
        self.assertIsNone(self.denial(result))
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertTrue('ADVISORY' in rendered or any('ADVISORY' in item for item in defects))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
