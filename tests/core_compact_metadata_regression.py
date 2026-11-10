"""Startup metadata is factored without changing text or losing recovery boundaries."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from epitype import core_gen, host_sync, memspec
import core_scope_compaction_regression as scope_fixtures


class CompactMetadata(unittest.TestCase):
    def rule(self, number, **changes):
        row = scope_fixtures.ScopeCompaction().rule(number, **core_gen.GLOBAL_SCOPE)
        row['core_display'] = 'plain'
        row['provenance']['status'] = 'REVIEWED'
        row['projection']['document'].update(changes)
        row['text'] = row['projection']['document']['text']
        return row

    def test_default_metadata_is_once_and_empty_scopes_disappear(self):
        rows = [self.rule(n) for n in range(1, 27)]
        recovery = {}
        text = core_gen.assemble(rows, recovery=recovery)
        self.assertEqual(text.count('以下條文為 AI 整理'), 1)
        self.assertEqual(text.count('現行內容已審'), 1)
        self.assertNotIn('AI 整理：', text)
        self.assertNotIn('範圍覆寫：', text)
        self.assertNotIn('來源範圍基底：', text)
        self.assertNotIn('<!--r:', text)
        self.assertNotIn('來源卡：', text)
        self.assertEqual(text.count(core_gen.RECOVERY_PREFIX), 1)
        self.assertEqual(len(recovery), 1)
        for row in rows:
            self.assertEqual(text.count('- ' + row['text'] + '\n'), 1)

    def test_different_author_review_and_nonempty_override_stay_identifiable(self):
        rows = [self.rule(n) for n in range(1, 5)]
        rows[1]['projection']['document']['author'] = 'claude'
        rows[2]['projection']['document']['scope']['conditions'] = ['before executing']
        rows[3]['provenance']['status'] = 'VERIFIED'
        text = core_gen.assemble(rows)
        self.assertEqual(text.count('AI 整理：claude'), 1)
        self.assertNotIn('範圍覆寫：', text)
        self.assertEqual(text.count('適用：before executing'), 1)
        self.assertEqual(text.count(memspec.CORE_GEN_VERIFIED_SOURCE), 1)

    def test_legacy_entries_have_individual_status_and_one_explanation(self):
        rows = [self.rule(n) for n in (1, 2)]
        for row in rows:
            row['provenance'] = {'status': 'UNVERIFIED'}
            row.pop('projection')
        text = core_gen.assemble(rows)
        self.assertEqual(text.count('來源未核的舊卡：'), 1)
        self.assertEqual(text.count(memspec.CORE_GEN_LEGACY_SOURCE), 2)
        self.assertNotIn('現行內容已審', text)

    def test_manifest_json_roundtrip_preserves_host_floor_and_multiline_boundaries(self):
        literal = 'First line\n### Literal heading\n- Literal bullet\n範圍覆寫：literal text\nLast line  '
        rows = [self.rule(1, text=literal), self.rule(2), self.rule(3)]
        rows[0]['layer'] = 'floor'
        rows[1]['hosts'] = ('codex',)
        rows[2]['hosts'] = ('claude',)
        recovery = {}
        text = core_gen.assemble(rows, recovery=recovery)
        recovery = json.loads(json.dumps(recovery))
        for host, last_path in [('claude', rows[2]['path']), ('codex', rows[1]['path'])]:
            view = core_gen.host_view(text, host).strip('\n')
            entries = host_sync._previous_rules(view, host, recovery=recovery)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]['layer'], 'floor')
            self.assertTrue(entries[0]['previous_rendering'].startswith(literal + '\n'))
            self.assertEqual(entries[0]['scope'], core_gen.GLOBAL_SCOPE)
            self.assertEqual(entries[1]['path'], last_path)
            self.assertEqual(entries[1]['hosts'], (host,))

    def test_missing_tampered_manifest_and_changed_host_text_refuse_recovery(self):
        recovery = {}
        text = core_gen.assemble([self.rule(1)], recovery=recovery).strip('\n')
        digest = core_gen.recovery_key(text)
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaisesRegex(ValueError, 'recovery'):
                host_sync._previous_rules(text, 'codex', home=home)
        damaged = copy.deepcopy(recovery)
        damaged[digest]['views']['codex']['entries'][0]['identity'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            host_sync._previous_rules(text, 'codex', recovery=damaged)
        with self.assertRaisesRegex(ValueError, 'host view mismatch'):
            host_sync._previous_rules(text + '\nmanual addition', 'codex', recovery=recovery)

    def test_project_view_without_applicable_entries_has_no_empty_shell(self):
        row = self.rule(1)
        row['hosts'] = ('codex',)
        with patch.object(core_gen, 'collect_rules', return_value=([row], [])):
            self.assertEqual(host_sync.project_rules_text('source-vault', 'claude'), '')
            self.assertIn(row['text'], host_sync.project_rules_text('source-vault', 'codex'))
        with patch.object(core_gen, 'collect_rules', return_value=([], [])):
            self.assertEqual(host_sync.project_rules_text('source-vault', 'codex'), '')

    def test_core_gen_pack_carries_manifest_and_check_remains_byte_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            vault = core_gen._build_vault(root / 'vault')
            out = root / 'core.md'
            result = core_gen.generate([vault], out, config={})
            self.assertEqual(result['status'], core_gen.STATUS_WRITTEN)
            pack = json.loads(core_gen.pack_path(vault).read_text(encoding='utf-8'))
            text = out.read_text(encoding='utf-8')
            digest = core_gen.recovery_key(text)
            self.assertEqual(core_gen.recovery_digest(pack['recovery'][digest]), digest)
            self.assertEqual(pack['out_sha256'], hashlib.sha256(out.read_bytes()).hexdigest())
            self.assertTrue(host_sync._previous_rules(text.strip('\n'), 'codex', recovery=pack['recovery']))
            self.assertEqual(core_gen.check([vault], out, config={})['status'], core_gen.STATUS_MATCH)
            out.write_text(text + 'changed\n', encoding='utf-8')
            self.assertEqual(core_gen.check([vault], out, config={})['status'], core_gen.STATUS_DRIFT)

    def test_conditions_named_actions_and_specific_exceptions_remain_plain_text(self):
        row = self.rule(1)
        scope = row['projection']['document']['scope']
        scope.update(actions=['*', 'upload'], conditions=['before delivery'],
                     exceptions=['higher-priority system or developer instructions', 'service unavailable'],
                     case='sample-case', expiry='2999-01-01')
        text = core_gen.assemble([row])
        self.assertIn('適用：案例 sample-case；執行 upload 時；before delivery；除外：service unavailable；期限 2999-01-01', text)
        self.assertNotIn('higher-priority', text)
        self.assertNotIn('範圍覆寫：', text)
        self.assertNotIn('共用範圍：', text)

    def test_literal_body_conditions_are_not_repeated_or_semantically_guessed(self):
        scope = dict(core_gen.GLOBAL_SCOPE, actions=['review'], conditions=['before delivery'])
        self.assertEqual(core_gen.scope_summary(scope, 'Review before delivery.'), '')
        self.assertIn('before delivery', core_gen.scope_summary(scope, '交付前核對。'))
        self.assertIn('review', core_gen.scope_summary(scope, 'Preview before delivery.'))
        scope['conditions'] = ['交付前']
        self.assertEqual(core_gen.scope_summary(scope, '交付前核對。'), '適用：執行 review 時')

    def test_scope_prose_and_literal_metadata_in_body_survive_recovery(self):
        literal = 'A rule.\n適用：literal body line\n範圍覆寫：literal body line'
        row = self.rule(1, text=literal)
        row['projection']['document']['scope']['conditions'] = ['before delivery']
        recovery = {}
        block = core_gen.assemble([row], recovery=recovery)
        entry = host_sync._previous_rules(block, 'codex', recovery=recovery)[0]
        self.assertTrue(entry['previous_rendering'].startswith(literal + '\n'))
        self.assertEqual(entry['scope']['conditions'], ['before delivery'])

    def test_source_filename_exception_is_the_only_individual_path(self):
        rows = [self.rule(1, text='CORE-01. First rule.'), self.rule(2, text='CORE-02. Second rule.')]
        rows[0]['path'] = 'rule-core-01-english.md'
        rows[1]['path'] = 'rule-special.md'
        text = core_gen.assemble(rows)
        self.assertEqual(text.count('來源卡：'), 1)
        self.assertIn('來源卡：rule-special.md', text)

    def test_old_scope_base_and_marked_override_remain_recoverable(self):
        scope = dict(core_gen.GLOBAL_SCOPE, conditions=['before delivery'])
        old = ('來源範圍基底：' + json.dumps(core_gen.GLOBAL_SCOPE) + '\n## B. resident\n'
               '<!--r:0123456789abcdef 2 1-->\n- Keep evidence.\n'
               '範圍覆寫：{"conditions":["before delivery"]}')
        entry = host_sync._previous_rules(old, 'codex')[0]
        self.assertEqual(entry['scope'], scope)
        self.assertIn('適用：before delivery', entry['previous_rendering'])
        self.assertNotIn('範圍覆寫：', entry['previous_rendering'])

    def test_old_partial_shared_scope_applies_to_entries_without_overrides(self):
        old = ('共用範圍：{"conditions":["before delivery"]}；legacy suffix\n'
               '## B. resident\n<!--r:0123456789abcdef 1 1-->\n- Keep evidence.')
        entry = host_sync._previous_rules(old, 'codex')[0]
        self.assertEqual(entry['scope'], dict(core_gen.GLOBAL_SCOPE, conditions=['before delivery']))
        self.assertIn('適用：before delivery', entry['previous_rendering'])


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
