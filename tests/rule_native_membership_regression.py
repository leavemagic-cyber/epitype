import sys; sys.dont_write_bytecode = True
"""Pinned native identity tests through actual check/apply in synthetic homes."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import host_native, rule_legacy, rule_pipeline
import rule_native_sources_regression as native_fixture
NATIVE_FIELDS = native_fixture.NATIVE_FIELDS


class NativeMembershipConsumers(unittest.TestCase):
    def setUp(self):
        self.f = native_fixture.NativeSourceConsumers('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def adopt_fixture(self, document, *, update_pin=True):
        f = self.f
        raw = json.dumps(document, ensure_ascii=False).encode()
        f.baseline.write_bytes(raw)
        if update_pin:
            options = json.loads(f.epiconfig.read_text(encoding='utf-8'))
            options['rule_source_baseline_sha256'] = hashlib.sha256(raw).hexdigest()
            f.epiconfig.write_text(json.dumps(options), encoding='utf-8')

    def freeze_v2(self, paths=None, native_paths=None):
        f = self.f
        f.freeze(paths)
        entries = json.loads(f.baseline.read_bytes())['entries']
        selected = [str(f.path)] if native_paths is None else native_paths
        self.document = rule_legacy.build_baseline(entries, native_paths=selected)
        self.adopt_fixture(self.document)

    def ordinary(self):
        path = self.f.vault / 'unrelated-history.md'
        path.write_text('---\ntype: rule\n---\nOrdinary body A.\n', encoding='utf-8')
        return path

    def test_actual_two_host_unrelated_change_is_scoped_only_in_v2(self):
        f = self.f
        f.card(legacy=True)
        ordinary = self.ordinary()
        f.freeze([f.path, ordinary]); f.install()
        initial = json.loads(f.baseline.read_bytes())
        before = f.snapshot()
        ordinary.write_text('---\ntype: reference\n---\nOrdinary body B.\n', encoding='utf-8')
        f.assert_refuses_preserving('meaning differs')  # Exact pre-fix v1 behavior.
        self.adopt_fixture(rule_legacy.build_baseline(initial['entries'], native_paths=[str(f.path)]))
        for function in (host_native.check, host_native.apply):
            result, report = f.call(function)
            self.assertEqual(result[1], 0, report)
            self.assertNotIn('REFUSE', report)
            self.assertIn('UNVERIFIED', report)
        self.assertEqual(f.snapshot(), before)
        text = ordinary.read_text(encoding='utf-8')
        fields, _ = rule_pipeline.memspec.frontmatter_text(text)
        self.assertFalse(rule_pipeline.legacy_integrity(fields, ordinary, card_text=text).allowed)
        self.assertEqual(set(initial['entries']), set(json.loads(f.baseline.read_bytes())['entries']))

    def test_malformed_v2_metadata_has_no_v1_fallback(self):
        f = self.f
        f.card(legacy=True); self.freeze_v2(); f.install()
        invalid = [None, {}, 'not-a-list', [str(f.path), str(f.path).upper()],
                   [str(f.root / 'absent.md')], [12], ['relative.md']]
        for paths in invalid:
            with self.subTest(native_paths=paths):
                document = copy.deepcopy(self.document); document['native_paths'] = paths
                self.adopt_fixture(document)
                f.assert_refuses_preserving('INVALID native baseline')
        for changed in ({'version': 2, 'entries': self.document['entries']},
                        {**self.document, 'extra': 1}, {**self.document, 'version': True}):
            with self.subTest(schema=changed):
                self.adopt_fixture(changed)
                f.assert_refuses_preserving('INVALID native baseline')
        self.adopt_fixture(self.document)
        self.assertTrue(rule_legacy.native_membership().matched)

    def test_original_native_field_drop_and_type_change_cannot_escape(self):
        f = self.f
        original = f.card(legacy=True)
        self.freeze_v2(); f.install()
        f.equal_stat_replace(f.path, b'type: project', b'type: user   ')
        f.assert_refuses_preserving('meaning differs')
        f.path.write_text(original.replace(NATIVE_FIELDS, '').replace('type: project', 'type: reference'), encoding='utf-8')
        f.assert_refuses_preserving('meaning differs')
        # A missing source ref is not retirement permission.
        f.path.unlink()
        f.assert_refuses_preserving('ownership source')

    def test_new_native_and_invalid_settings_provenance_refuse(self):
        f = self.f
        f.card(legacy=True); self.freeze_v2(); f.install()
        unknown = f.vault / 'new-native.md'
        f.card(path=unknown, legacy=True)
        f.assert_refuses_preserving('no automatic enrollment')
        unknown.unlink()
        for host in ('claude', 'codex'):
            with self.subTest(source_host=host):
                text = f.card(host=host)
                f.equal_stat_replace(f.source, b'Synthetic fixture only', b'Synthetic FIXTURE only')
                f.assert_refuses_preserving('INVALID')
                f.card(host=host)
                f.equal_stat_replace(f.path, b'"4"', b'"5"')
                f.assert_refuses_preserving('complete rule body and execution fields')
                f.path.write_text(text, encoding='utf-8')
        # Explicit settings provenance is still checked when no native fields remain.
        f.card(path=unknown, native='', scope={**f.scope(), 'actions': ['host_sync']})
        text = unknown.read_text(encoding='utf-8')
        unknown.write_text(text.replace('Synthetic native body.', 'Changed native body.'), encoding='utf-8')
        f.assert_refuses_preserving('complete rule body and execution fields')

    def test_owned_state_identity_survives_roster_omission_and_refs_are_strict(self):
        f = self.f
        f.card(); self.freeze_v2(native_paths=[]); f.install()
        state_path = host_native.state_path(f.home)
        original_state = json.loads(state_path.read_bytes())
        text = f.path.read_text(encoding='utf-8')
        f.path.write_text(text.replace(NATIVE_FIELDS, ''), encoding='utf-8')
        f.assert_refuses_preserving('complete rule body and execution fields')
        # Removing both the envelope and native fields still cannot erase an owned identity.
        f.path.write_text('---\ntype: reference\n---\nHistorical only.\n', encoding='utf-8')
        f.assert_refuses_preserving('meaning differs')
        f.path.write_text(text, encoding='utf-8')
        for reference in ('../ordinary-name.md', '/ordinary-name.md', 'C:/ordinary-name.md',
                          '_hidden.md', 'ordinary-name.txt', 'missing.md'):
            with self.subTest(reference=reference):
                state = copy.deepcopy(original_state)
                for record in state['targets'].values():
                    for section in ('env', 'deny', 'keys', 'rules'):
                        for key, previous in record.get(section, {}).items():
                            if section == 'rules': record[section][key] = reference
                            else: previous['card'] = reference
                state_path.write_text(json.dumps(state), encoding='utf-8')
                f.assert_refuses_preserving()
        state_path.write_text(json.dumps(original_state), encoding='utf-8')
        result, report = f.call(host_native.check)
        self.assertEqual(result[1], 0, report)

    def test_fresh_raw_pin_and_current_membership_never_use_stat_cache(self):
        f = self.f
        f.card(legacy=True); self.freeze_v2(); f.install()
        f.equal_stat_replace(f.baseline, b'"version": 2', b'"version": 1')
        f.assert_refuses_preserving('bytes differ from fixed')
        self.adopt_fixture(self.document)
        self.assertTrue(rule_legacy.native_membership().matched)
        old = json.dumps({'native_paths': self.document['native_paths']})[1:-1].encode()
        new = json.dumps({'native_paths': [p.upper() for p in self.document['native_paths']]})[1:-1].encode()
        f.equal_stat_replace(f.baseline, old, new)  # Still valid v2 schema and normalized membership.
        f.assert_refuses_preserving('bytes differ from fixed')
        self.adopt_fixture(self.document)
        f.baseline.unlink()
        f.assert_refuses_preserving('cannot be read')
        self.adopt_fixture(self.document)
        options = json.loads(f.epiconfig.read_bytes())
        options.pop('rule_source_baseline_sha256')
        f.epiconfig.write_text(json.dumps(options), encoding='utf-8')
        f.assert_refuses_preserving('requires both')

    def test_explicit_builder_normalizes_subset_and_retirement_is_source_verified(self):
        digest = 'a' * 64
        baseline = rule_legacy.build_baseline({'C:/Vault/Native.md': digest,
            'C:/Vault/Other.md': digest}, native_paths=['c:/VAULT/native.md'])
        self.assertEqual(baseline['version'], 2)
        self.assertEqual(baseline['native_paths'], [r'c:\vault\native.md'])
        self.assertEqual(rule_legacy.build_baseline(baseline['entries'])['version'], 1)
        with self.assertRaises(ValueError):
            rule_legacy.build_baseline(baseline['entries'], native_paths=['C:/Vault/new.md'])
        for invalid in (None, {}, 'C:/Vault/native.md', [r'c:\vault\native.md', 'C:/Vault/Native.md']):
            with self.subTest(builder_native_paths=invalid), self.assertRaises(ValueError):
                rule_legacy.build_baseline(baseline['entries'], native_paths=invalid)
        f = self.f
        f.card(); self.freeze_v2(); f.install()
        f.card(scope=f.scope(project=f.root, case='synthetic-unresolved-case'))
        f.assert_refuses_preserving('UNVERIFIED rule scope')
        f.card(status='retired')
        before = json.loads(f.settings.read_bytes())
        result, report = f.call()
        self.assertEqual(result[1], 0, report)
        after = json.loads(f.settings.read_bytes())
        self.assertNotIn('CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS', after['env'])
        self.assertEqual(after['hooks'], before['hooks'])
        self.assertEqual(after['env']['USER_VALUE'], 'keep')
        self.assertIn('synthetic-user-command', f.user_rules.read_text(encoding='utf-8'))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
