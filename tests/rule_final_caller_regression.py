"""Independent integration counterexamples: aliases, event claims, budgets and CLI."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'adapters' / 'claude')]
import io
import json
import subprocess
import time
import unittest
from contextlib import redirect_stderr

from epitype import card_lint, memsearch, memspec, rule_pipeline
import rule_source_consumers_regression as source_fixtures
import recall_hook as recall
import stop_gate as stop


class FinalCallers(unittest.TestCase):
    def setUp(self):
        fixture = source_fixtures.SourceConsumers(methodName='test_legacy_behavior_stays_active_but_compact_is_labelled_routing')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def test_runtime_alias_change_needs_complete_review_but_routing_maintenance_does_not(self):
        f = self.fixture
        before = f.card()
        after = before.replace('aliases: [aliasold, otherold]', 'aliases: [unrelatedtopic, differenttopic]')
        self.assertTrue(rule_pipeline.admission(before, after, 'decision', f.target))
        f.target.write_text(after, encoding='utf-8')
        fields, _ = memspec.frontmatter_text(after)
        with self.assertRaisesRegex(ValueError, 'execution fields'):
            rule_pipeline.projection(fields, f.target)
        hook = stop._handle({'session_id': 'alias-counterexample',
                            'last_assistant_message': 'unrelatedtopic 和 differenttopic 要不要調整？'},
                           time.monotonic(), [])
        self.assertIsNone(hook)
        routing = before.replace('decision_key: "synthetic-read-rule"\n', '').replace('forbidden: [badold]\n', '')
        maintained = routing.replace('aliases: [aliasold, otherold]', 'aliases: [unrelatedtopic, differenttopic]')
        self.assertEqual(rule_pipeline.admission(routing, maintained, 'rule', f.target), [])

    def test_existing_auto_event_cannot_add_owner_claim_or_handoff_while_exempt(self):
        f = self.fixture
        before = ('---\nname: historical\ndescription: Historical capture pointer.\nmetadata:\n'
            '  type: correction\nauto: true\nstatus: active\ncaptured_at: 2026-10-03\n'
            'session_id: synthetic-read-session\naliases: [capture, history]\n---\nCaptured event.\n')
        after = before.replace('auto: true\n', 'auto: true\ndecided_by: owner-explicit\n'
            'owner_quote: 每次工作必須先問。\nhandoff_rule: true\n')
        f.target.write_text(after, encoding='utf-8')
        kind, findings = card_lint.check_card(f.target, 'correction.md', before_text=before)
        self.assertEqual(kind, 'correction')
        self.assertTrue(any(level == 'FAIL' and rule == 'rule-source-write' for level, rule, _ in findings))
        fields, _ = memspec.frontmatter_text(after)
        self.assertTrue(rule_pipeline.is_rule(fields, kind, after))
        # Date and retrieval repairs on a historical raw event remain maintenance.
        maintained = before.replace('aliases: [capture, history]', 'aliases: [capture, history, pointer]')
        self.assertEqual(rule_pipeline.admission(before, maintained, kind, f.target), [])
        self.assertEqual(rule_pipeline.admission('', before, kind, f.target), [])

    def recall(self, budget, *, session='budget-test', markers=None):
        config = recall.load_config(time.monotonic())
        config['budget_bytes'] = budget
        value = recall._recall({'prompt': 'aliasold', 'cwd': str(self.fixture.root), 'session_id': session},
                               time.monotonic(), config, markers)
        return (value or {}).get('hookSpecificOutput', {}).get('additionalContext', '')

    def test_low_budget_has_complete_pointer_or_explicit_omission_without_partial_authority(self):
        f = self.fixture
        f.card(kind='ai-derived')
        memsearch.build_index(f.vault)
        for budget in (200, 400, 600):
            with self.subTest(budget=budget):
                markers = []
                context = self.recall(budget, session='low-' + str(budget), markers=markers)
                self.assertTrue(context)
                self.assertLessEqual(len(context.encode()), budget)
                self.assertIn('未載入', context)
                self.assertNotIn('⚖ 裁定', context)
                self.assertNotIn(f.document['text'], context)
                self.assertNotIn(f.document['quote'], context)
                self.assertFalse(any(digest.startswith('card-') for _session, digest in markers))

    def test_pointer_delivery_does_not_suppress_later_complete_source_delivery(self):
        f = self.fixture
        f.card(kind='ai-derived')
        memsearch.build_index(f.vault)
        markers = []
        low = self.recall(600, markers=markers)
        self.assertIn('decision.md', low)
        self.assertTrue(any(digest.startswith('pointer-') for _session, digest in markers))
        for session, digest in markers:
            recall._claim_marker(session, digest)
        complete = self.recall(10240)
        self.assertIn(f.document['text'], complete)
        self.assertIn(f.document['quote'], complete)
        self.assertIn('synthetic-read-ai', complete)
        self.assertIn('適用範圍', complete)

    def test_impossibly_small_budget_reports_stderr_and_claims_no_delivery(self):
        f = self.fixture
        f.card()
        memsearch.build_index(f.vault)
        stream = io.StringIO()
        markers = []
        with redirect_stderr(stream):
            context = self.recall(1, markers=markers)
        self.assertEqual(context, '')
        self.assertIn('source recall omitted', stream.getvalue())
        self.assertFalse(markers)

    def test_cli_and_projection_agree_on_payload_and_successor_failures(self):
        f = self.fixture
        for fault in ('body', 'successor'):
            with self.subTest(fault=fault):
                text = f.card()
                if fault == 'body':
                    text += 'An unreviewed new requirement.\n'
                else:
                    text = text.replace('status: "active"', 'status: "superseded"\nsuperseded_by: missing.md')
                    fields, _ = memspec.frontmatter_text(text)
                    document = rule_pipeline.bind_payload(json.loads(fields['rule_provenance']), text,
                                                         reviewer='synthetic-read-reviewer')
                    text = '\n'.join('rule_provenance: ' + json.dumps(json.dumps(document, ensure_ascii=False),
                        ensure_ascii=False) if line.startswith('rule_provenance:') else line
                        for line in text.splitlines()) + '\n'
                f.target.write_text(text, encoding='utf-8')
                fields, _ = memspec.frontmatter_text(text)
                with self.assertRaises(ValueError):
                    rule_pipeline.projection(fields, f.target)
                for entry in (['epitype.rule_pipeline'], ['epitype', 'rule-source']):
                    command = subprocess.run([sys.executable, '-B', '-m', *entry, str(f.target)],
                        cwd=ROOT, capture_output=True, encoding='utf-8', timeout=20)
                    self.assertEqual(command.returncode, 1, command.stderr)
                    row = json.loads(command.stdout)[0]
                    self.assertEqual(row['status'], 'INVALID')
                    self.assertFalse(row['action_authorization'])


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
