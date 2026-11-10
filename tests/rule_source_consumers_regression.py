"""Source, complete payload, scope and expiry through real read-side consumers."""
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

from epitype import decision_lint, memspec, rule_context, rule_pipeline, rule_provenance
import _hook_common as common
import stop_gate as stop
import pretooluse_gate as pretool


class SourceConsumers(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='epitype-source-consumers-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.target = self.vault / 'decision.md'
        self.source = self.root / 'owner.jsonl'
        self.config = self.root / 'config.json'
        common.write_config(self.config, [self.vault])
        env = patch.dict(os.environ, {'HOME': str(self.root / 'home'),
            'USERPROFILE': str(self.root / 'home'), 'CODEX_HOME': str(self.root / 'home' / '.codex'),
            'EPITYPE_CONFIG': str(self.config), 'EPITYPE_DREAM_MODE': 'off'})
        env.start()
        self.addCleanup(env.stop)
        marker = patch.object(stop, 'recall_marker_directory',
            lambda session: self.root / 'markers' / common.session_component(session))
        marker.start()
        self.addCleanup(marker.stop)

    def card(self, *, host='codex', kind='owner-directive', scope=None):
        scope = copy.deepcopy(scope or {'level': 'global', 'project': None, 'case': None,
            'actions': ['*'], 'conditions': [], 'expiry': 'unspecified', 'exceptions': []})
        if scope['level'] == 'global':
            extent = '此隔離測試 temp host 內全域所有動作'
        else:
            extent = '此隔離測試專案 ' + str(scope['project']) + '，案件 ' + str(scope['case']) + '，所有動作'
        if scope['conditions']:
            extent += '，僅於 ' + '、'.join(scope['conditions'])
        if scope['actions'] != ['*']:
            extent += '，限定动作 ' + '、'.join(scope['actions'])
        if scope['exceptions']:
            extent += '，例外 ' + '、'.join(scope['exceptions'])
        if scope['expiry'] != 'unspecified':
            extent += '，有效至 ' + scope['expiry']
        quote = extent + '：採用此測試規則，禁止 badold。'
        stamp = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        expiry = (datetime.fromisoformat(stamp)+timedelta(hours=1)).isoformat()
        observed = 'badold 在隔離測試中依來源禁止。'
        sid, mid = 'synthetic-read-session', 'synthetic-read-owner'
        if host == 'claude':
            row = {'type': 'user', 'sessionId': sid, 'uuid': mid,
                'message': {'role': 'user', 'content': [{'type': 'text', 'text': quote+'\n'+observed+' '+stamp+' '+expiry}]}}
        else:
            row = {'type': 'response_item', 'payload': {'type': 'message', 'id': mid, 'role': 'user',
                'content': [{'type': 'input_text', 'text': quote+'\n'+observed+' '+stamp+' '+expiry}]}}
        meta = (json.dumps({'type': 'session_meta', 'payload': {'id': sid}}) + '\n').encode()
        raw = (json.dumps(row, ensure_ascii=False) + '\n').encode()
        self.source.write_bytes(meta + raw)
        document = {'version': 1, 'key': 'synthetic-read-rule', 'kind': kind,
            'author': 'owner' if kind == 'owner-directive' else 'synthetic-read-ai',
            'quote': quote, 'text': quote if kind == 'owner-directive' else '採用禁止 badold 的執行整理。',
            'sources': [{'path': str(self.source), 'byte_offset': len(meta), 'line': 2,
                'record_sha256': hashlib.sha256(raw).hexdigest(), 'session_id': sid,
                'message_id': mid, 'role': 'U'}], 'scope': scope,
            'scope_basis': {'source': 0, 'quote': quote, 'reviewed_by': 'synthetic-read-reviewer',
                'reviewed_at': '2026-10-03', 'rationale': 'Synthetic original expressly declares this scope.'},
            'supersedes': []}
        if kind == 'ai-derived':
            document['restriction_evidence'] = {
                'kind': kind, 'author': document['author'], 'challengeable': True,
                'text': document['text'], 'subject': 'badold',
                'basis': [{'quote': observed, 'observed_at': stamp, 'valid_until': expiry,
                           'source': document['sources'][0]}],
                'rationale': 'The original synthetic human source explicitly prohibits badold.'}
        fields = {'name': 'synthetic-read-rule', 'description': '採用本測試規則。',
            'decision_key': document['key'], 'status': 'active', 'current_decision_at': '2026-10-03',
            'decided_by': 'owner-explicit', 'owner_quote': quote, 'text': document['text'],
            'forbidden': '[badold]', 'aliases': '[aliasold, otherold]'}
        unbound = '---\n' + '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}'
            if key not in ('forbidden', 'aliases') else f'{key}: {value}'
            for key, value in fields.items()) + '\n---\nSynthetic body.\n'
        document = rule_pipeline.bind_payload(document, unbound, reviewer='synthetic-read-reviewer')
        text = unbound.replace('---\n', '---\nrule_provenance: '
            + json.dumps(json.dumps(document, ensure_ascii=False), ensure_ascii=False) + '\n', 1)
        self.target.write_text(text, encoding='utf-8')
        self.document = document
        return text

    def decisions(self, event=None, defects=None):
        return stop._decisions(self.vault, time.monotonic(), defects, event=event)

    def test_budget_limited_source_keeps_bound_content_but_not_a_changed_payload(self):
        self.card()
        fields,_=memspec.frontmatter_fields(self.target)
        with patch.object(rule_provenance,'_original',side_effect=AssertionError('No historic IO')):
            projected=rule_pipeline.projection(fields,self.target)
            self.assertEqual(projected['provenance']['status'],'REVIEWED')
            self.assertEqual(projected['text'],self.document['text'])
            self.target.write_text(self.target.read_text(encoding='utf-8')+'Unreviewed new execution rule.',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'not bound'):
                rule_pipeline.projection(fields,self.target)

    def test_pattern_only_ai_guard_reminds_but_owner_guard_can_block(self):
        for kind in ('ai-derived', 'owner-directive'):
            with self.subTest(kind=kind):
                text = self.card(kind=kind)
                document = copy.deepcopy(self.document)
                document.pop('restriction_evidence', None)
                lines = [line for line in text.splitlines() if not line.startswith('rule_provenance:')]
                lines[1:1] = ['guard_tool: Bash', 'guard_all_of: [badold]', 'on_hit: block']
                text = '\n'.join(lines)+'\n'
                document = rule_pipeline.bind_payload(document, text, reviewer='synthetic-read-reviewer')
                text = text.replace('---\n', '---\nrule_provenance: '
                    + json.dumps(json.dumps(document, ensure_ascii=False), ensure_ascii=False)+'\n', 1)
                self.target.write_text(text, encoding='utf-8')
                value = pretool._handle({'tool_name':'Bash', 'tool_input':{'command':'echo badold'},
                    'session_id':'pattern-guard-'+kind}, time.monotonic(), [])
                output = (value or {}).get('hookSpecificOutput', {})
                if kind == 'ai-derived':
                    self.assertNotEqual(output.get('permissionDecision'), 'deny')
                    self.assertIn('AI 程序僅作提醒', output.get('additionalContext', ''))
                else:
                    self.assertEqual(output.get('permissionDecision'), 'deny')

    def restore(self, event=None, diagnostics=None):
        destination = self.root / 'compact.md'
        rule_context.record(self.vault, 'session', self.target.name)
        rule_context.snapshot([self.vault], 'session', destination)
        return rule_context.restore(destination, [self.vault], budget=12000,
                                    event=event, diagnostics=diagnostics)

    def test_both_original_host_formats_reach_real_stop_and_lint(self):
        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                self.card(host=host)
                found = self.decisions()
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].source_status, 'REVIEWED')
                self.assertEqual(found[0].source_original_quote, '')
                self.assertEqual(found[0].source_scope, self.document['scope'])
                self.assertEqual(decision_lint.lint_vault(self.vault).exit_code, 0)
                event = {'session_id': host + '-source-read', 'hook_event_name': 'Stop',
                         'transcript_path': str(self.root / ('.claude' if host == 'claude' else '.codex') / 'session.jsonl'),
                         'cwd': str(self.root), 'last_assistant_message': 'badold'}
                notices = []
                with patch.object(stop, 'leave_note', side_effect=lambda _config, _session, text:
                                  notices.append(text)):
                    self.assertIsNone(stop._handle(event, time.monotonic(), notices))
                self.assertTrue(any(found[0].key in notice for notice in notices), notices)
                self.assertEqual(notices, ['Epitype 提醒（synthetic-read-rule）：採用本測試規則。'])
                notice = notices[0]
                self.assertNotIn('\n', notice)
                for omitted in (self.document['quote'], 'badold', '作者', '來源',
                                json.dumps(self.document['scope'], ensure_ascii=False),
                                str(self.source)):
                    self.assertNotIn(omitted, notice)

    def test_original_changes_invalidate_cached_ruling_with_unchanged_card(self):
        self.card()
        stale = stop._read_decision(self.target)
        source_stat = self.source.stat()
        self.source.write_bytes(self.source.read_bytes().replace(b'badold', b'badevil'))
        os.utime(self.source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        self.assertIsNotNone(stop._decision_from_ruling(self.vault, self.target.name, stale))
        self.assertEqual(len(self.decisions()), 1)
        fields,_=memspec.frontmatter_fields(self.target)
        self.assertFalse(rule_pipeline.inspect_card(fields,self.target,audit_originals=True).verified)

    def test_body_and_execution_fields_cannot_bypass_binding(self):
        for change in ('body', 'guard'):
            with self.subTest(change=change):
                text = self.card()
                changed = text + '新增必須先問 Owner 的限制。\n' if change == 'body' else text.replace(
                    'forbidden: [badold]', 'forbidden: [safeold]')
                self.target.write_text(changed, encoding='utf-8')
                self.assertIsNone(stop._read_decision(self.target))
                self.assertEqual(self.decisions(), [])
                self.assertEqual(self.restore(), '')
                self.assertTrue(any(item.rule == 'source' for item in decision_lint.lint_vault(self.vault).failures))

    def test_cache_cannot_downgrade_source_status_or_supply_stale_guards(self):
        self.card()
        cached = stop._read_decision(self.target)
        cached['source_status'] = 'UNVERIFIED'
        cached['forbidden'] = ['forged-guard']
        fresh = stop._decision_from_ruling(self.vault, self.target.name, cached)
        self.assertEqual(fresh.source_status, 'REVIEWED')
        self.assertEqual(fresh.forbidden, ('badold',))
        self.source.write_bytes(self.source.read_bytes().replace(b'badold', b'badevil'))
        self.assertEqual(stop._decision_from_ruling(self.vault, self.target.name, cached).forbidden, ('badold',))

    def test_compact_budget_reports_omission_without_cutting_scope_or_quote(self):
        self.card(kind='ai-derived')
        destination = self.root / 'compact.md'
        rule_context.record(self.vault, 'session', self.target.name)
        rule_context.snapshot([self.vault], 'session', destination)
        diagnostics = []
        result = rule_context.restore(destination, [self.vault], budget=8, diagnostics=diagnostics)
        self.assertEqual(result, '')
        self.assertTrue(any('context budget' in item for item in diagnostics))

    def test_fabricated_quote_never_becomes_owner_ruling(self):
        text = self.card()
        document = copy.deepcopy(self.document)
        document['quote'] = document['text'] = 'Owner 沒說過的原話。'
        fields, _ = memspec.frontmatter_text(text)
        fields['owner_quote'] = fields['text'] = document['quote']
        # A fresh complete binding cannot substitute for an original human record.
        plain = '---\n' + '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False)}'
            for k, v in fields.items() if k != 'rule_provenance') + '\n---\nSynthetic body.\n'
        document = rule_pipeline.bind_payload(document, plain, reviewer='synthetic-read-reviewer')
        forged = plain.replace('---\n', '---\nrule_provenance: '
            + json.dumps(json.dumps(document, ensure_ascii=False), ensure_ascii=False) + '\n', 1)
        self.target.write_text(forged, encoding='utf-8')
        ruling=stop._read_decision(self.target)
        self.assertEqual(ruling['source_status'],'REVIEWED')
        self.assertEqual(ruling['source_original_quote'],'')
        fields,_=memspec.frontmatter_fields(self.target)
        self.assertFalse(rule_pipeline.inspect_card(fields,self.target,audit_originals=True).verified)

    def test_finite_expiry_is_removed_unspecified_remains_current(self):
        self.card()
        self.assertEqual(len(self.decisions()), 1)
        self.assertIn('卡片記載作者：owner', self.restore())
        scope = copy.deepcopy(self.document['scope'])
        scope['expiry'] = '2000-01-01'
        self.card(scope=scope)
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.restore(), '')
        report = decision_lint.lint_vault(self.vault)
        self.assertFalse(report.failures)
        self.assertTrue(any(item.rule == 'source' and '到期' in item.reason for item in report.warnings))

    def test_case_missing_context_is_advisory_and_preserves_source_scope(self):
        scope = {'level': 'case', 'project': str(self.root), 'case': 'A', 'actions': ['*'],
                 'conditions': [], 'expiry': 'unspecified', 'exceptions': []}
        self.card(scope=scope, kind='ai-derived')
        defects = []
        self.assertEqual(self.decisions({'cwd': str(self.root)}, defects), [])
        self.assertTrue(any('case context' in item for item in defects))
        cache = stop._read_cache(self.vault / memspec.FTS_INDEX_DIRECTORY / stop._DECISION_CACHE_FILENAME)
        self.assertEqual(cache[1][self.target.name]['source_scope'], scope)
        restored = self.restore({'cwd': str(self.root)})
        self.assertIn('限定參考', restored)
        self.assertIn('synthetic-read-ai', restored)
        self.assertNotIn(self.document['quote'], restored)
        self.assertIn('"case":"A"', restored)
        explicit = {'cwd': str(self.root), 'rule_context': {'case': 'A'}}
        self.assertEqual(len(self.decisions(explicit)), 1)
        self.assertEqual(self.decisions({'cwd': str(self.root), 'rule_context': {'case': 'B'}}), [])
        self.assertEqual(self.restore({'cwd': str(self.root), 'rule_context': {'case': 'B'}}), '')

    def test_natural_action_conditions_and_exceptions_do_not_expand_globally(self):
        for name, value in (('actions', ['整理規則']), ('conditions', ['僅週三']), ('exceptions', ['出差期間'])):
            with self.subTest(clause=name):
                scope = {'level': 'global', 'project': None, 'case': None, 'actions': ['*'],
                    'conditions': [], 'expiry': 'unspecified', 'exceptions': []}
                scope[name] = value
                self.card(scope=scope)
                self.assertEqual(self.decisions(), [])
                restored = self.restore()
                self.assertIn('限定參考', restored)
                self.assertIn(value[0], restored)

    def test_project_path_boundary_and_missing_context(self):
        scope = {'level': 'project', 'project': str(self.root / 'project'), 'case': None,
                 'actions': ['*'], 'conditions': [], 'expiry': 'unspecified', 'exceptions': []}
        self.card(scope=scope)
        self.assertEqual(self.decisions(), [])
        self.assertEqual(len(self.decisions({'cwd': str(self.root / 'project' / 'sub')})), 1)
        self.assertEqual(self.decisions({'cwd': str(self.root / 'project-sibling')}), [])

    def test_legacy_behavior_stays_active_but_compact_is_labelled_routing(self):
        self.target.write_text('---\nname: legacy\ndescription: Legacy pointer.\ndecision_key: legacy\n'
            'status: active\ncurrent_decision_at: 2026-10-03\ndecided_by: owner-explicit\n'
            'owner_quote: Legacy quote.\nforbidden: [badold]\n---\nBody.\n', encoding='utf-8')
        found = self.decisions()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].source_status, 'UNVERIFIED')
        self.assertIn('UNVERIFIED', self.restore())
        self.assertIn('路由', self.restore())
        report = decision_lint.lint_vault(self.vault)
        self.assertFalse(any(item.rule == 'source' for item in report.failures + report.warnings))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
