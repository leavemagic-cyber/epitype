"""Startup accounting and incremental guard discovery use isolated synthetic data."""
import sys
sys.dont_write_bytecode = True
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'adapters'/'claude'), str(ROOT/'tests')]
import _hook_common as common
import pretooluse_gate as gate
import sessionstart_hook as start
from epitype import card_lint, cardscan, lint_result, memspec, rule_examples
from rule_source_fixtures import owner_bound_card


class StartupCacheRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='epitype-startup-cache-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root/'home'
        self.vault = self.root/'vault'
        self.vault.mkdir()
        self.config = self.root/'config.json'
        common.write_config(self.config, [self.vault])
        env = patch.dict(os.environ, dict(HOME=str(self.home), USERPROFILE=str(self.home),
            CODEX_HOME=str(self.home/'.codex'), EPITYPE_CONFIG=str(self.config), EPITYPE_DREAM_MODE='off',
            TEMP=str(self.root), TMP=str(self.root), TMPDIR=str(self.root)))
        env.start()
        self.addCleanup(env.stop)

    def card(self, name, guard=False):
        path = self.vault/(name+'.md')
        text = ('---\nname: '+name+'\ndescription: 2026-11-01 合成卡\naliases: [合成]\n'
                'metadata:\n  type: feedback\n')
        if guard:
            text += 'guard_tool: Read\nguard_all_of: [synthetic-danger]\non_hit: block\n'
        text += '---\n合成測試內容。\n'
        if guard:
            text = owner_bound_card(text, path, source_root=self.root/'sources', case_id=name)
        path.write_text(text, encoding='utf-8')
        return path

    def warm(self):
        gate.warm_guard_cache([self.vault], time.monotonic(), deadline=time.monotonic()+60)

    def read_cache(self):
        return json.loads(gate._guard_cache(self.vault).read_text(encoding='utf-8'))

    def test_warm_reads_every_cold_candidate_once_then_neither_positive_nor_negative(self):
        for name, guard in [('positive', True), ('negative', False), ('broken', False)]:
            path = self.card(name, guard)
            if name == 'broken':
                path.write_text('---\nbroken frontmatter\n', encoding='utf-8')
        with patch.object(gate, '_guard_candidate', wraps=gate._guard_candidate) as read:
            self.warm()
            self.assertEqual(read.call_count, 3)
            read.reset_mock()
            self.warm()
            read.assert_not_called()

    def test_new_card_and_changed_negative_becoming_positive_are_processed(self):
        self.card('negative')
        self.card('unchanged', True)
        self.warm()
        changed = self.card('negative', True)
        new = self.card('new', True)
        with patch.object(gate, '_guard_candidate', wraps=gate._guard_candidate) as read:
            self.warm()
        self.assertEqual({call.args[0] for call in read.call_args_list}, {changed, new})

    def test_changed_positive_becoming_negative_and_removed_card_are_not_retained(self):
        self.card('changed', True)
        removed = self.card('removed', True)
        self.warm()
        self.card('changed')
        removed.unlink()
        self.warm()
        cache = self.read_cache()
        self.assertIsNone(cache['guards']['changed.md'])
        self.assertNotIn('removed.md', cache['guards'])
        self.assertNotIn('removed.md', cache['manifest'])

    def test_version_mismatch_reprocesses_all_candidates(self):
        self.card('positive', True)
        self.card('negative')
        self.warm()
        value = self.read_cache()
        value['version'] = gate._GUARD_CACHE_VERSION-1
        gate._guard_cache(self.vault).write_text(json.dumps(value), encoding='utf-8')
        with patch.object(gate, '_guard_candidate', wraps=gate._guard_candidate) as read:
            self.warm()
        self.assertEqual(read.call_count, 2)
        self.assertEqual(self.read_cache()['version'], gate._GUARD_CACHE_VERSION)

    def test_truncated_warm_saves_completed_work_and_resumes(self):
        self.card('a')
        self.card('b', True)
        expired = [False]
        original = gate._guard_candidate

        def read(path):
            result = original(path)
            expired[0] = True
            return result

        with patch.object(gate, 'expired', side_effect=lambda _started: expired[0]), \
             patch.object(gate, '_guard_candidate', side_effect=read):
            self.warm()
        self.assertEqual(set(self.read_cache()['manifest']), {'a.md'})
        with patch.object(gate, '_guard_candidate', wraps=original) as resumed:
            self.warm()
        self.assertEqual([call.args[0].name for call in resumed.call_args_list], ['b.md'])

    def test_card_changed_during_warm_is_reprocessed_next_time(self):
        card = self.card('changed')
        original = gate._guard_candidate

        def read(path):
            value = original(path)
            path.write_text(path.read_text(encoding='utf-8')+'合成變更\n', encoding='utf-8')
            return value

        with patch.object(gate, '_guard_candidate', side_effect=read):
            self.warm()
        with patch.object(gate, '_guard_candidate', wraps=original) as reread:
            self.warm()
        self.assertEqual([call.args[0] for call in reread.call_args_list], [card])

    def test_cold_late_guard_is_available_to_first_tool_after_warming(self):
        for index in range(memspec.ACTION_GUARD_MAX_CARDS_PER_VAULT+2):
            self.card('plain-%04d' % index)
        positive = self.card('z-positive', True)
        self.warm()
        event = dict(tool_name='Read', tool_input={'file_path':'synthetic-danger'}, session_id='cold')
        guards = gate._guards(self.vault, time.monotonic(), [], event=event)
        self.assertIn(positive, [guard.path for guard in guards])
        self.assertEqual(next(guard.on_hit for guard in guards if guard.path == positive), 'block')
        value = gate._handle(event, time.monotonic())
        self.assertEqual(value['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_cold_warm_locates_without_duplicate_source_validation(self):
        self.card('positive', True)
        with patch.object(gate, '_read_guard', side_effect=AssertionError('warm validation')):
            self.warm()
        self.assertIs(self.read_cache()['guards']['positive.md'], True)

    def test_actual_tool_rechecks_positive_and_rejects_changed_card_payload(self):
        card = self.card('positive', True)
        self.warm()
        event = dict(tool_name='Read', tool_input={'file_path':'synthetic-danger'}, session_id='fresh')
        card.write_text(card.read_text(encoding='utf-8')+'合成變更\n', encoding='utf-8')
        with patch.object(gate, '_read_guard', wraps=gate._read_guard) as read:
            defects = []
            guards = gate._guards(self.vault, time.monotonic(), defects, event=event)
        self.assertTrue(read.called)
        self.assertEqual(guards, [])
        self.assertTrue(any('INVALID' in line for line in defects))

    def test_startup_warms_exact_tool_scope_and_does_not_run_lint(self):
        self.card('positive', True)
        (self.vault/memspec.WORK_LEDGER_FILENAME).write_text('synthetic ledger', encoding='utf-8')
        project = self.root/'project'
        project.mkdir()
        slug = re.sub(r'[^A-Za-z0-9]', '-', str(project))
        native = self.home/'.claude'/'projects'/slug/'memory'
        native.mkdir(parents=True)
        (native/'synthetic.md').write_text('---\nname: synthetic\n---\nbody\n', encoding='utf-8')
        other = self.root/'other'
        other.mkdir()
        common.write_config(self.config, [other, self.vault, native])
        event = dict(source='startup', cwd=str(project), session_id='scope')
        config = common.load_config(time.monotonic())
        with patch.object(gate, 'warm_guard_cache', wraps=gate.warm_guard_cache) as warm, \
             patch.object(card_lint, 'scan_vault', side_effect=AssertionError('startup lint')):
            trace = {}
            self.assertIsNone(start._handle(event, time.monotonic(), trace=trace))
        self.assertEqual(warm.call_args.args[0], gate.resolve_vaults(config, event))
        self.assertFalse(gate._guard_cache(other).exists())
        self.assertEqual(trace['card_lint'], ['missing', 'missing'])

    def test_missing_incomplete_and_expired_lint_are_not_passes(self):
        self.card('clean')
        self.assertEqual(lint_result.read(self.vault)['status'], 'missing')
        lint_result.refresh(self.vault)
        self.assertEqual(lint_result.read(self.vault)['status'], 'pass')
        value = json.loads(lint_result.result_path(self.vault).read_text(encoding='utf-8'))
        self.assertEqual(lint_result.read(self.vault, now=value['checked_at']+lint_result.MAX_AGE_SECONDS)['status'], 'stale')
        value['complete'] = False
        lint_result.result_path(self.vault).write_text(json.dumps(value), encoding='utf-8')
        self.assertEqual(lint_result.read(self.vault)['status'], 'incomplete')

    def test_lint_report_missing_fields_is_incomplete(self):
        self.card('clean')
        lint_result.refresh(self.vault)
        value = json.loads(lint_result.result_path(self.vault).read_text(encoding='utf-8'))
        del value['report']['total']
        lint_result.result_path(self.vault).write_text(json.dumps(value), encoding='utf-8')
        self.assertEqual(lint_result.read(self.vault)['status'], 'incomplete')

    def test_current_lint_failure_stays_failure_without_startup_maintenance_notices(self):
        (self.vault/'bad.md').write_text('---\nname: bad\n---\nbody\n', encoding='utf-8')
        lint_result.refresh(self.vault)
        self.assertEqual(lint_result.read(self.vault)['status'], 'fail')
        trace = {}
        self.assertIsNone(start._handle(dict(source='startup'), time.monotonic(), trace=trace))
        self.assertEqual(trace['card_lint'], ['fail'])

    def test_card_add_edit_delete_and_engine_version_invalidate_lint(self):
        card = self.card('clean')
        for change in ('add', 'edit', 'delete', 'engine'):
            with self.subTest(change=change):
                lint_result.refresh(self.vault)
                if change == 'add':
                    self.card('new')
                elif change == 'edit':
                    card.write_text(card.read_text(encoding='utf-8')+'合成變更\n', encoding='utf-8')
                elif change == 'delete':
                    (self.vault/'new.md').unlink()
                else:
                    with patch.object(lint_result, 'VERSION', lint_result.VERSION+1):
                        self.assertEqual(lint_result.read(self.vault)['status'], 'stale')
                    continue
                self.assertEqual(lint_result.read(self.vault)['status'], 'stale')

    def test_lint_external_source_changes_invalidate_result(self):
        self.card('positive', True)
        lint_result.refresh(self.vault)
        self.assertIn(lint_result.read(self.vault)['status'], ('pass', 'fail'))
        next((self.root/'sources').glob('*.jsonl')).write_text('{}\n', encoding='utf-8')
        self.assertEqual(lint_result.read(self.vault)['status'], 'stale')

    def test_timed_out_oversized_and_changed_during_scan_are_incomplete(self):
        card = self.card('clean')
        original = card_lint.scan_vault
        for kind in ('timed_out', 'oversized_skipped', 'changed'):
            def scan(*args, **kwargs):
                report = original(*args, **kwargs)
                if kind == 'changed':
                    card.write_text(card.read_text(encoding='utf-8')+'合成變更\n', encoding='utf-8')
                else:
                    report[kind] = True if kind == 'timed_out' else 1
                return report
            with self.subTest(kind=kind), patch.object(card_lint, 'scan_vault', side_effect=scan):
                lint_result.refresh(self.vault)
            self.assertEqual(lint_result.read(self.vault)['status'], 'incomplete')

    def test_unreadable_inventory_cannot_publish_or_keep_a_pass(self):
        self.card('clean')
        lint_result.refresh(self.vault)
        with patch.object(lint_result.cardscan, 'scan_vault', side_effect=PermissionError):
            self.assertEqual(lint_result.read(self.vault)['status'], 'stale')
            with self.assertRaises(PermissionError):
                lint_result.refresh(self.vault)
        self.assertEqual(lint_result.read(self.vault)['status'], 'incomplete')

    def test_lint_entrypoint_publishes_complete_result_only_when_requested(self):
        self.card('clean')
        self.assertEqual(card_lint.main([str(self.vault), '--json'], output=io.StringIO()), 0)
        self.assertFalse(lint_result.result_path(self.vault).exists())
        self.assertEqual(card_lint.main([str(self.vault), '--refresh-cache', '--json'], output=io.StringIO()), 0)
        with patch.object(card_lint, 'scan_vault', side_effect=AssertionError('cached read')):
            self.assertEqual(lint_result.read(self.vault)['status'], 'pass')

    def test_missing_example_validator_cannot_keep_a_complete_lint_result(self):
        self.card('clean')
        lint_result.refresh(self.vault)
        with patch.object(rule_examples, '_gate_modules', return_value=(None, None)):
            with self.assertRaises(RuntimeError):
                lint_result.refresh(self.vault)
        self.assertEqual(lint_result.read(self.vault)['status'], 'incomplete')

    def test_strict_inventory_reports_directory_read_failure(self):
        with patch.object(cardscan.os, 'scandir', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                cardscan.scan_vault(self.vault, strict=True)
            self.assertEqual(cardscan.scan_vault(self.vault), [])

    def test_validator_code_and_utc_date_changes_invalidate_lint(self):
        self.card('clean')
        lint_result.refresh(self.vault)
        with patch.object(lint_result, '_engine', return_value={}):
            self.assertEqual(lint_result.read(self.vault)['status'], 'stale')
        value = json.loads(lint_result.result_path(self.vault).read_text(encoding='utf-8'))
        value['checked_date'] = '2000-01-01'
        lint_result.result_path(self.vault).write_text(json.dumps(value), encoding='utf-8')
        self.assertEqual(lint_result.read(self.vault)['status'], 'stale')

    def test_switching_configuration_path_invalidates_lint(self):
        self.card('clean')
        lint_result.refresh(self.vault)
        with patch.dict(os.environ, EPITYPE_CONFIG=str(self.root/'different-config.json')):
            self.assertEqual(lint_result.read(self.vault)['status'], 'stale')

    def test_malformed_candidate_is_kept_for_actual_tool_diagnostics(self):
        path = self.vault/'bad.md'
        path.write_text('---\nbroken frontmatter\n', encoding='utf-8')
        self.warm()
        self.assertIs(self.read_cache()['guards']['bad.md'], True)
        defects = []
        gate._guards(self.vault, time.monotonic(), defects, event={})
        self.assertTrue(any('INVALID' in item for item in defects))


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
