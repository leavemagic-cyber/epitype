"""A shell write cannot silently activate changed/new unproven legacy rules."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hashlib
import copy
import json
import os
import time
import unittest
import rule_source_consumers_regression as fixture
from epitype import decision_lint, memspec, owner_rules, rule_context, rule_legacy, rule_pipeline
import stop_gate as stop
import recall_hook as recall


class LegacyConsumers(unittest.TestCase):
    setUp = fixture.SourceConsumers.setUp
    card = fixture.SourceConsumers.card

    def legacy(self, host='codex'):
        self.card(host=host)
        text = '\n'.join(line for line in self.target.read_text(encoding='utf-8').splitlines()
                         if not line.startswith('rule_provenance:')) + '\n'
        text = text.replace('---\n', '---\nhandoff_rule: true\n', 1)
        self.target.write_text(text, encoding='utf-8')
        digest = hashlib.sha256(rule_pipeline._meaning(text, include_source=False).encode()).hexdigest()
        baseline = self.root / 'baseline.json'
        baseline.write_text(json.dumps(rule_legacy.build_baseline({str(self.target): digest})), encoding='utf-8')
        options = json.loads(self.config.read_text(encoding='utf-8'))
        options.update(rule_source_baseline=str(baseline),
                       rule_source_baseline_sha256=hashlib.sha256(baseline.read_bytes()).hexdigest())
        self.config.write_text(json.dumps(options), encoding='utf-8')
        return text, baseline

    def test_complete_payload_tampering_is_rejected_after_cached_read_both_hosts(self):
        for host in ('claude', 'codex'):
            text, baseline = self.legacy(host)
            event = {'cwd': str(self.root)}
            original = stop._decisions(self.vault, time.monotonic(), [], event=event)
            self.assertEqual(len(original), 1)
            self.assertEqual(original[0].source_status, 'UNVERIFIED')
            stat = self.target.stat()
            self.target.write_text(text.replace('Synthetic body.', 'Tampering body.'), encoding='utf-8')
            self.assertEqual(self.target.stat().st_size, stat.st_size)
            os.utime(self.target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            diagnostics = []
            self.assertEqual(stop._decisions(self.vault, time.monotonic(), diagnostics, event=event), [])
            self.assertTrue(diagnostics)
            self.assertFalse(recall._active_decision(self.target))
            report = decision_lint.lint_vault(self.vault)
            self.assertTrue(any(item.rule == 'source' for item in report.failures))
            with self.assertRaisesRegex(ValueError, 'frozen baseline'):
                owner_rules.render([self.vault])

    def test_new_legacy_membership_is_not_auto_enrolled(self):
        text, baseline = self.legacy()
        new = self.vault / 'new-decision.md'
        new.write_text(text, encoding='utf-8')
        fields, _problem = memspec.frontmatter_fields(new)
        with self.assertRaisesRegex(ValueError, 'no automatic enrollment'):
            rule_pipeline.projection(fields, new)
        self.assertFalse(recall._active_decision(new))
        diagnostics = []
        decisions = stop._decisions(self.vault, time.monotonic(), diagnostics, event={'cwd': str(self.root)})
        self.assertEqual([str(decision.path) for decision in decisions], [str(self.target)])
        self.assertTrue(any(new.name in item for item in diagnostics))

    def test_compaction_reports_legacy_drift_and_baseline_pin_change(self):
        text, baseline = self.legacy()
        session = 'legacy-context'
        rule_context.record(self.vault, session, self.target.name)
        destination = self.root / 'handoff.md'
        destination.write_text('', encoding='utf-8')
        rule_context.snapshot([self.vault], session, destination)
        self.assertIn('UNVERIFIED', rule_context.restore(destination, [self.vault]))
        self.target.write_text(text.replace('Synthetic body.', 'Tampering body.'), encoding='utf-8')
        diagnostics = []
        self.assertEqual(rule_context.restore(destination, [self.vault], diagnostics=diagnostics), '')
        self.assertTrue(any('frozen baseline' in item for item in diagnostics))
        self.target.write_text(text, encoding='utf-8')
        baseline.write_bytes(baseline.read_bytes() + b' ')
        fields, _problem = memspec.frontmatter_fields(self.target)
        with self.assertRaisesRegex(ValueError, 'fixed rule_source_baseline_sha256'):
            rule_pipeline.projection(fields, self.target)

    def test_pure_routing_maintenance_and_verified_source_migration(self):
        text, baseline = self.legacy()
        maintained = text.replace('---\n', '---\nupdated_at: 2026-10-03\n', 1)
        # Armed Stop aliases deliberately have no routing-only exemption.
        self.target.write_text(maintained, encoding='utf-8')
        fields, _problem = memspec.frontmatter_fields(self.target)
        self.assertEqual(rule_pipeline.projection(fields, self.target)['provenance']['status'], 'UNVERIFIED')
        self.card()
        fields, _problem = memspec.frontmatter_fields(self.target)
        projected = rule_pipeline.projection(fields, self.target)
        self.assertEqual(projected['provenance']['status'], 'VERIFIED')
        self.assertFalse(projected['provenance']['action_authorization'])

    def test_enrolled_rule_cannot_escape_by_removing_classification_fields(self):
        for host in ('claude', 'codex'):
            self.legacy(host)
            self.target.write_text('---\nname: legacy-reclassification\ntype: project\n'
                'description: Only follow aliasold and otherold.\naliases: [aliasold, otherold]\n'
                '---\nOnly follow a changed interpretation.\n', encoding='utf-8')
            fields, _problem = memspec.frontmatter_fields(self.target)
            self.assertFalse(rule_pipeline.is_rule(fields, 'project', self.target.read_text(encoding='utf-8')))
            check = rule_pipeline.legacy_integrity(fields, self.target)
            self.assertEqual(check.status, 'INVALID')
            self.assertTrue(check.baseline_contains)
            with self.assertRaisesRegex(ValueError, 'frozen baseline'):
                rule_pipeline.projection(fields, self.target)

    def test_projection_hash_and_binding_use_the_same_snapshot(self):
        for host in ('claude', 'codex'):
            text = self.card(host=host)
            fields, _problem = memspec.frontmatter_fields(self.target)
            self.target.write_text(text.replace('Synthetic body.', 'Unreviewed body.'), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'changed after its reviewed snapshot'):
                rule_pipeline.projection(fields, self.target, card_text=text)

    def test_stale_caller_fields_cannot_verify_a_changed_source_envelope(self):
        for host in ('claude', 'codex'):
            text = self.card(host=host)
            fields, _problem = memspec.frontmatter_fields(self.target)
            changed = copy.deepcopy(self.document)
            changed['sources'][0]['record_sha256'] = '0' * 64
            replacement = 'rule_provenance: ' + json.dumps(json.dumps(changed, ensure_ascii=False), ensure_ascii=False)
            self.target.write_text('\n'.join(replacement if line.startswith('rule_provenance:') else line
                for line in text.splitlines()) + '\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'rule provenance INVALID'):
                rule_pipeline.projection(fields, self.target)


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
