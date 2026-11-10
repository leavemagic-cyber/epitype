import sys; sys.dont_write_bytecode = True
import copy
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import verification_deadline_regression as fixtures
from epitype import memspec, rule_pipeline, rule_provenance

class ChainReceiptRegression(unittest.TestCase):
    def setUp(self):
        fixtures.VerificationDeadlineRegression.setUp(self)
        environment = patch.dict(os.environ, {memspec.EPITYPE_CONFIG_ENV: str(self.vault/'config.json')})
        environment.start()
        self.addCleanup(environment.stop)
    card = fixtures.VerificationDeadlineRegression.card
    checked = fixtures.VerificationDeadlineRegression.checked
    chain = fixtures.VerificationDeadlineRegression.chain

    def test_warm_valid_chain_survives_deadline_and_dependency_edit_invalidates(self):
        text = self.chain()
        self.assertEqual(self.checked(text).status, 'REVIEWED')
        with rule_provenance.verification_window(time.monotonic()-1):
            self.assertEqual(self.checked(text).status, 'REVIEWED')
            old = self.vault/'rule-prior.md'
            old.write_text(old.read_text(encoding='utf-8').replace('status: superseded', 'status: active'), encoding='utf-8')
            self.assertEqual(self.checked(text).status, 'UNVERIFIED')
        self.assertEqual(self.checked(text).status, 'INVALID')

    def test_known_invalid_stays_invalid_under_deadline_and_repair_revalidates(self):
        text = self.chain()
        old = self.vault/'rule-prior.md'
        good = old.read_text(encoding='utf-8')
        before = old.stat()
        old.write_text(good.replace('superseded_by: rule-current.md','superseded_by: rule-unknown.md'), encoding='utf-8')
        os.utime(old, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(old.stat().st_size, before.st_size)
        self.assertEqual(self.checked(text).status, 'INVALID')
        with rule_provenance.verification_window(time.monotonic()-1):
            self.assertEqual(self.checked(text).status, 'INVALID')
            old.write_text(good, encoding='utf-8')
            self.assertEqual(self.checked(text).status, 'UNVERIFIED')
        self.assertEqual(self.checked(text).status, 'REVIEWED')

    def test_missing_dependency_appearing_invalidates_negative_receipt(self):
        text = self.chain()
        old = self.vault/'rule-prior.md'
        good = old.read_bytes(); old.unlink()
        self.assertEqual(self.checked(text).status, 'INVALID')
        with rule_provenance.verification_window(time.monotonic()-1):
            self.assertEqual(self.checked(text).status, 'INVALID')
            old.write_bytes(good)
            self.assertEqual(self.checked(text).status, 'UNVERIFIED')
        self.assertEqual(self.checked(text).status, 'REVIEWED')

    def test_unreadable_dependency_is_unverified_never_cached_as_invalid(self):
        text = self.chain()
        self.assertEqual(self.checked(text).status, 'REVIEWED')
        old = self.vault/'rule-prior.md'
        read = Path.read_text
        def unreadable(path, *args, **kwargs):
            if path == old: raise PermissionError('fixture temporarily locked')
            return read(path, *args, **kwargs)
        with patch.object(Path,'read_text',unreadable):
            self.assertEqual(self.checked(text).status, 'UNVERIFIED')
        with rule_provenance.verification_window(time.monotonic()-1):
            self.assertEqual(self.checked(text).status, 'REVIEWED')

    def test_corrupt_receipt_does_not_establish_validity(self):
        text = self.chain()
        self.assertEqual(self.checked(text).status, 'REVIEWED')
        for malformed in ('bad json', '[]', 'null'):
            rule_pipeline._chain_cache_path(self.path).write_text(malformed,encoding='utf-8')
            with rule_provenance.verification_window(time.monotonic()-1):
                self.assertEqual(self.checked(text).status, 'UNVERIFIED')
        self.assertEqual(self.checked(text).status,'REVIEWED')

    def test_future_receipt_schema_is_not_overwritten(self):
        text = self.chain()
        self.assertEqual(self.checked(text).status, 'REVIEWED')
        path = rule_pipeline._chain_cache_path(self.path)
        future = json.loads(path.read_text(encoding='utf-8'))
        future['version'] = rule_pipeline._CHAIN_RECEIPT_VERSION + 1
        path.write_text(json.dumps(future), encoding='utf-8')
        before = path.read_bytes()
        self.assertEqual(self.checked(text).status, 'REVIEWED')
        self.assertEqual(path.read_bytes(), before)

    def assert_warm_equals_cold(self, text):
        warm = self.checked(text)
        rule_pipeline._chain_cache_path(self.path).unlink(missing_ok=True)
        cold = self.checked(text)
        self.assertEqual(warm.to_dict(), cold.to_dict())
        self.assertEqual(warm.document, cold.document)
        return warm

    def externalize(self, path):
        import hashlib
        text = path.read_text(encoding='utf-8')
        fields, _ = memspec.frontmatter_text(text)
        document = rule_provenance.parse(fields)
        target = self.vault/(path.stem+'-current-provenance.json')
        raw = json.dumps(document, ensure_ascii=False).encode()
        target.write_bytes(raw)
        ref = {'ref': str(target.resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}
        replacement = 'rule_provenance: '+json.dumps(json.dumps(ref))
        path.write_text('\n'.join(replacement if line.startswith('rule_provenance:') else line
                                  for line in text.split('\n')), encoding='utf-8')
        return target, raw

    def test_warm_cold_equivalence_external_current_envelope_changes_root_and_predecessor(self):
        for root in (False, True):
            with self.subTest(root=root):
                self.chain()
                path = self.path if root else self.vault/'rule-prior.md'
                external, good = self.externalize(path)
                text = self.path.read_text(encoding='utf-8')
                self.assertEqual(self.assert_warm_equals_cold(text).status,'REVIEWED')
                external.write_bytes(good+b' ')
                self.assertEqual(self.assert_warm_equals_cold(text).status,'INVALID')
                external.unlink()
                self.assertNotEqual(self.assert_warm_equals_cold(text).status,'REVIEWED')
                external.write_bytes(good)
                self.assertEqual(self.assert_warm_equals_cold(text).status,'REVIEWED')

    def test_warm_cold_equivalence_payload_migration_state_changes(self):
        from epitype import rule_binding
        text = self.chain()
        old = self.vault/'rule-prior.md'
        prior_text = old.read_text(encoding='utf-8')
        fields, _ = memspec.frontmatter_text(prior_text)
        document = rule_provenance.parse(fields)
        binding = document['payload_binding']
        binding.pop('version')
        binding['sha256'] = rule_pipeline._digest(rule_pipeline._meaning(prior_text, include_source=False))
        replacement = 'rule_provenance: '+json.dumps(json.dumps(document,ensure_ascii=False),ensure_ascii=False)
        old.write_text('\n'.join(replacement if line.startswith('rule_provenance:') else line
                                 for line in prior_text.split('\n')),encoding='utf-8')
        self.assertEqual(self.checked(text).status,'REVIEWED')
        migration = rule_binding._path('payload',[document['key'],binding['sha256']])
        good = migration.read_bytes()
        old.write_text(old.read_text(encoding='utf-8')+'\nAdditional historical backup.\n',encoding='utf-8')
        self.assertEqual(self.assert_warm_equals_cold(text).status,'REVIEWED')
        migration.write_text(json.dumps({'version':1,'sha256':'0'*64}),encoding='utf-8')
        self.assertEqual(self.assert_warm_equals_cold(text).status,'INVALID')
        migration.unlink()
        self.assertEqual(self.assert_warm_equals_cold(text).status,'INVALID')
        migration.write_bytes(good)
        self.assertEqual(self.assert_warm_equals_cold(text).status,'REVIEWED')

    def test_sourced_chain_does_not_depend_on_legacy_integrity(self):
        text = self.chain()
        with patch.object(rule_pipeline,'legacy_integrity',side_effect=AssertionError('unexpected legacy read')):
            self.assertEqual(self.assert_warm_equals_cold(text).status,'REVIEWED')

    def test_unreadable_binding_state_defers_without_memoizing_invalid_result(self):
        from epitype import rule_binding
        text = self.chain()
        prior = self.vault/'rule-prior.md'
        original = prior.read_text(encoding='utf-8')
        fields, _ = memspec.frontmatter_text(original)
        document = rule_provenance.parse(fields)
        binding = document['payload_binding']
        binding.pop('version')
        binding['sha256'] = rule_pipeline._digest(rule_pipeline._meaning(original, include_source=False))
        replacement = 'rule_provenance: '+json.dumps(json.dumps(document,ensure_ascii=False),ensure_ascii=False)
        prior.write_text('\n'.join(replacement if line.startswith('rule_provenance:') else line
                                  for line in original.split('\n')),encoding='utf-8')
        self.assertEqual(self.checked(text).status,'REVIEWED')
        migration = rule_binding._path('payload',[document['key'],binding['sha256']])
        prior.write_text(prior.read_text(encoding='utf-8')+'\nAuxiliary backup.\n',encoding='utf-8')
        self.assertEqual(self.checked(text).status,'REVIEWED')
        receipt = rule_pipeline._chain_cache_path(self.path)
        saved = receipt.read_bytes()
        read_bytes = Path.read_bytes
        def unavailable(path):
            if path == migration:
                raise PermissionError('fixture binding temporarily unreadable')
            return read_bytes(path)
        with patch.object(Path,'read_bytes',unavailable):
            self.assertEqual(self.checked(text).status,'UNVERIFIED')
            self.assertEqual(read_bytes(receipt),saved)
            receipt.unlink()
            self.assertEqual(self.checked(text).status,'UNVERIFIED')
            self.assertFalse(receipt.exists())
        self.assertEqual(self.checked(text).status,'REVIEWED')

    def test_receipt_outcome_or_dependency_edit_cannot_change_verdict(self):
        text = self.chain()
        old = self.vault/'rule-prior.md'
        original = old.read_text(encoding='utf-8')
        for broken in (False, True):
            old.write_text(original.replace('superseded_by: rule-current.md','superseded_by: rule-unknown.md')
                           if broken else original,encoding='utf-8')
            expected = 'INVALID' if broken else 'REVIEWED'
            self.assertEqual(self.checked(text).status,expected)
            path = rule_pipeline._chain_cache_path(self.path)
            for key in ('issues','dependencies'):
                receipt = json.loads(path.read_text(encoding='utf-8'))
                if key=='issues':receipt[key]=[] if broken else ['invented invalidity']
                else:receipt[key]={str(self.path.resolve()):receipt[key][str(self.path.resolve())]}
                path.write_text(json.dumps(receipt),encoding='utf-8')
                self.assertEqual(self.checked(text).status,expected)

    def test_nested_card_cache_stays_in_vault_root(self):
        (self.vault/'MEMORY.md').write_text('# Memory',encoding='utf-8')
        nested=self.vault/'nested';nested.mkdir()
        destination=rule_pipeline._chain_cache_path(nested/'rule.md')
        self.assertEqual(destination.parent,self.vault/'.epitype'/'chain-verification')
        self.assertFalse((nested/'.epitype').exists())

    def test_effective_version_tracks_scope_execution_status_not_quote_or_locator(self):
        text = self.card()
        base = rule_pipeline.effective_version(text)
        for update in ({'quote':'history only'}, {'sources':[]}, {'quote':'another history'}):
            document=copy.deepcopy(self.doc);document.update(update)
            self.assertEqual(rule_pipeline.effective_version(self.card(document)),base)
        for name, value in (('text','changed current instruction'),('author','reviewer-two')):
            document=copy.deepcopy(self.doc);document[name]=value
            self.assertNotEqual(rule_pipeline.effective_version(self.card(document)),base)
        for name,value in (('exceptions',['new exception']),('conditions',['only after review']),('expiry','2027-01-01')):
            document=copy.deepcopy(self.doc);document['scope'][name]=value
            self.assertNotEqual(rule_pipeline.effective_version(self.card(document)),base)
        self.assertNotEqual(rule_pipeline.effective_version(self.card(on_hit='block')),base)
        self.assertNotEqual(rule_pipeline.effective_version(self.card(status='retired')),base)

if __name__=='__main__':unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
