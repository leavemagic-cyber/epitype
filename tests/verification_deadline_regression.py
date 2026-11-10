import sys; sys.dont_write_bytecode = True
from pathlib import Path
import contextlib
import copy
import io
import json
import os
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import current_content_review_regression as fixtures
from epitype import memsearch, memspec, rule_context, rule_pipeline, rule_provenance
import _hook_common as common
import recall_hook as recall


class VerificationDeadlineRegression(unittest.TestCase):
    setUp = fixtures.CurrentContent.setUp
    card = fixtures.CurrentContent.card
    checked = fixtures.CurrentContent.checked

    def chain(self):
        current = self.path
        old = copy.deepcopy(self.doc)
        old.update(text='較早的完整規則。')
        self.path = self.vault / 'rule-prior.md'
        self.card(old, status='superseded', superseded_by=current.name)
        self.path = current
        new = copy.deepcopy(self.doc)
        new['supersedes'] = ['rule-prior.md']
        text = self.card(new)
        result = self.checked(text)
        self.assertEqual(result.status, rule_provenance.REVIEWED, result.issues)
        rule_pipeline._chain_cache_path(self.path).unlink(missing_ok=True)
        return text

    def test_exhaustion_is_unverified_and_current_text_is_available_for_lookup(self):
        text = self.chain()
        fields, _ = memspec.frontmatter_text(text)
        with rule_provenance.verification_window(time.monotonic() - 1):
            result = self.checked(text)
            self.assertEqual(result.status, rule_provenance.UNVERIFIED)
            self.assertTrue(result.verification_deferred)
            self.assertFalse(result.to_dict()['blocking_basis'])
            with self.assertRaisesRegex(ValueError, 'UNVERIFIED'):
                rule_pipeline.projection(fields, self.path)
            projected = rule_pipeline.projection(fields, self.path, allow_deferred=True)
            self.assertEqual(projected['text'], self.doc['text'])
            self.assertIn('本輪未核', rule_pipeline.render_projection(projected))
            self.assertNotIn('來源未核', recall._recall_projection(projected))
            for rendered in (rule_pipeline.render_projection(projected), recall._recall_projection(projected)):
                self.assertIn('AI 整理', rendered)
                self.assertIn(projected['document']['author'], rendered)
                self.assertIn(rule_provenance.serialize(projected['document']['scope']), rendered)
            self.assertIn('UNVERIFIED', rule_pipeline.admission('', text, 'rule', self.path)[0])

    def test_payload_error_stays_invalid_even_when_deadline_passed(self):
        text = self.chain()
        text = text.replace('text: "', 'text: "誤改', 1)
        self.path.write_text(text, encoding='utf-8')
        with rule_provenance.verification_window(time.monotonic() - 1):
            result = self.checked(text)
        self.assertEqual(result.status, rule_provenance.INVALID)
        self.assertFalse(result.verification_deferred)

    def test_deferred_lookup_does_not_revive_expired_rule(self):
        self.doc['scope']['expiry'] = '2000-01-01T00:00:00+00:00'
        text = self.chain()
        fields, _ = memspec.frontmatter_text(text)
        with rule_provenance.verification_window(time.monotonic() - 1):
            projected = rule_pipeline.projection(fields, self.path, allow_deferred=True)
            self.assertTrue(projected['expired'])
            self.assertEqual(rule_pipeline.render_projection(projected), '')
            self.assertIsNone(recall._admitted_card(
                {'path': str(self.path)}, self.vault, {'cwd': str(self.vault)}, set()))

    def test_intrinsically_wrong_root_state_is_invalid_before_any_deadline_check(self):
        for updates in ({'status': 'active', 'superseded_by': 'elsewhere.md'},
                        {'status': 'deprecated'}, {'status': 'draft'}):
            with self.subTest(updates=updates):
                self.chain()
                document = copy.deepcopy(self.doc)
                document['supersedes'] = ['rule-prior.md']
                text = self.card(document, **updates)
                for deadline in (None, time.monotonic() - 1):
                    with (rule_provenance.verification_window(deadline) if deadline is not None
                          else contextlib.nullcontext()):
                        result = self.checked(text)
                        self.assertEqual(result.status, rule_provenance.INVALID, result.issues)
                        self.assertFalse(result.verification_deferred)

    def test_deferred_guard_keeps_guidance_and_does_not_create_blocking(self):
        import pretooluse_gate as gate
        self.chain()
        document = copy.deepcopy(self.doc)
        document['supersedes'] = ['rule-prior.md']
        self.card(document, guard_tool='Bash', guard_all_of='["guard-hit-marker"]',
                  guard_advice='Keep scoped advice', on_hit='block')
        event = {'cwd': str(self.vault), 'tool_name': 'Bash',
                 'tool_input': {'command': 'echo guard-hit-marker'}}
        with rule_provenance.verification_window(time.monotonic() - 1):
            entry = gate._read_guard(self.path)
            self.assertIsInstance(entry, dict, entry)
            self.assertEqual(entry['source_status'], 'UNVERIFIED')
            self.assertEqual(entry['advice'], 'Keep scoped advice')
            defects = []
            guards = gate._guards(self.vault, time.monotonic(), defects, event=event,
                                  match_first=True)
        self.assertEqual(len(guards), 1, defects)
        self.assertEqual(guards[0].on_hit, 'note')
        self.assertIn('Keep scoped advice', guards[0].advice)
        self.assertFalse(any('INVALID' in item for item in defects))

    def test_inactive_guard_status_is_not_delivered_or_enforced(self):
        import pretooluse_gate as gate
        for status in ('draft', 'Deprecated', 'RETIRED'):
            self.card(status=status, guard_tool='Bash', guard_all_of='["guard-hit-marker"]',
                      guard_advice='inactive guard', on_hit='block')
            self.assertIsNone(gate._read_guard(self.path))

    def test_native_deferred_projection_preserves_applied_settings_without_false_invalid(self):
        import rule_native_sources_regression as native_fixtures
        from epitype import host_native
        fx = native_fixtures.NativeSourceConsumers(methodName='runTest')
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        prior_path = fx.vault / 'old.md'
        fx.card(path=prior_path, status='superseded', superseded_by=fx.path.name)
        prior = copy.deepcopy(fx.document)
        fx.card(supersedes=[prior_path.name], prior=prior)
        fx.install()
        before = {str(path): path.read_bytes() for path in
                  (fx.settings, fx.config, fx.rules, fx.user_rules)}
        fx.card(supersedes=[prior_path.name], prior=prior,
                native=native_fixtures.NATIVE_FIELDS.replace('4', '8'))
        projection = rule_pipeline.projection
        def deferred(*args, **kwargs):
            with rule_provenance.verification_window(time.monotonic() - 1):
                return projection(*args, **kwargs)
        with patch.object(rule_pipeline, 'projection', deferred):
            result, report = fx.call(host_native.apply)
        self.assertIn('UNVERIFIED', report)
        self.assertNotIn('INVALID', report)
        self.assertEqual({str(path): path.read_bytes() for path in
                          (fx.settings, fx.config, fx.rules, fx.user_rules)}, before, report)

    def test_prompt_and_explicit_restore_keep_deferred_selected_rule(self):
        self.chain()
        fields, _ = memspec.frontmatter_fields(self.path)
        self.card(json.loads(fields[rule_provenance.FIELD]), layer="recall")
        config = self.vault / 'config.json'
        common.write_config(config, [self.vault], budget=5000)
        memsearch.build_index(self.vault)
        transcript = self.vault / 'trace.jsonl'
        event = {'prompt': 'current-content review', 'cwd': str(self.vault),
                 'session_id': 'short-verification', 'transcript_path': str(transcript)}
        output = io.StringIO()
        with patch.dict(os.environ, {memspec.EPITYPE_CONFIG_ENV: str(config),
                    memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
                    'HOME': str(self.vault / 'home'), 'USERPROFILE': str(self.vault / 'home')}), \
             patch.object(recall, 'recall_marker_directory', lambda s: self.vault / 'markers' / s), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(event))), \
             patch.object(sys, 'argv', ['recall']), \
             patch.object(recall, '_STARTED_AT', time.monotonic()), \
             rule_provenance.verification_window(time.monotonic() - 1), \
             contextlib.redirect_stdout(output):
            recall.main()
            rule_context.record(self.vault, event['session_id'], self.path.name)
            destination = self.vault / 'handoff.md'
            rule_context.snapshot([self.vault], event['session_id'], destination)
            restored = rule_context.restore(destination, [self.vault], budget=5000, event=event)
        value = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
        self.assertIn(self.doc['text'], value)
        self.assertIn('取代鏈本輪未核', value)
        self.assertNotIn('INVALID', value)
        self.assertIn(self.doc['text'], restored)


if __name__ == '__main__':
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
