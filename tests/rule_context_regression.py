import sys; sys.dont_write_bytecode = True
import json
import contextlib
import io
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import pretooluse_gate as pretool
import precompact_hook as precompact
import sessionstart_hook as sessionstart
from epitype import compact_map, hook_context, rule_context


class RuleContextRegression(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp
    write_card = fixtures.ActionGuardRegression.write_card

    def test_advice_then_compact_delivers_map_without_rule_replay_both_hosts(self):
        self.write_card('text.md', name='text', description='先查原始證據',
                        advise_tool='Bash', advise_text='先查證據')
        transcript = self.root / 'trace.jsonl'
        transcript.write_text('{}\n', encoding='utf-8')
        for tool in ('Bash', 'exec_command'):
            event = {'session_id': 'compact-' + tool, 'transcript_path': str(transcript),
                     'tool_name': tool, 'tool_input': {'command': 'echo x'}}
            output = io.StringIO()
            with patch.object(sys, 'stdin', io.StringIO(json.dumps(event))), \
                 patch.object(sys, 'argv', ['pretool']), \
                 patch.object(pretool, '_STARTED_AT', time.monotonic()), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(pretool.main(), 0)
            self.assertIn('先查證據', json.loads(output.getvalue())['hookSpecificOutput']['additionalContext'])
            precompact._handle(event, time.monotonic(), learn=False)
            result = sessionstart._handle({**event, 'source': 'compact'}, time.monotonic())
            destination = precompact._map_destination(self.vault, event, transcript)
            self.assertTrue(destination.is_file())
            context = result['hookSpecificOutput']['additionalContext']
            self.assertIn(str(destination), context)
            self.assertNotIn('先查原始證據', context)
            self.assertNotIn('先查證據', context)
            self.assertFalse(rule_context.state_path(self.vault, hook_context.identity(event)).exists())
            handoff = compact_map.handoff_destination(self.vault, event['session_id'], transcript)
            self.assertFalse(list(handoff.parent.glob(handoff.stem + '*.rules.json')))

    def test_block_audit_is_not_recorded_as_card_delivery_before_output(self):
        # A guard denies only when its card declares on_hit: block; the default is a note.
        self.write_card('guard.md', name='guard', description='禁止測試動作', on_hit='block',
                        guard_tool='Bash', guard_all_of=['dangerous'], guard_advice='停下')
        result = pretool._handle({'session_id': 'blocked', 'tool_name': 'Bash',
                                'tool_input': {'command': 'dangerous'}}, time.monotonic(), [])
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertFalse(rule_context.state_path(self.vault, 'blocked').exists())

    def test_deny_emits_current_advice_without_delivery_records_or_compact_replay_both_hosts(self):
        self.write_card('guard.md', name='guard', description='禁止測試動作', on_hit='block',
                        guard_tool='Bash', guard_all_of=['dangerous'], guard_advice='停下')
        transcript = self.root/'deny.jsonl'
        transcript.write_text('{}\n')

        class FailedFlush(io.StringIO):
            def flush(self):
                raise OSError('synthetic output failure')

        for tool in ('Bash', 'exec_command'):
            with self.subTest(tool=tool):
                event = {'session_id': 'deny-'+tool, 'transcript_path': str(transcript),
                         'tool_name': tool, 'tool_input': {'command': 'dangerous'}}
                key = hook_context.identity(event)
                pending = []
                rendered = pretool._handle(event, time.monotonic(), [], advice_claims=pending)
                self.assertEqual(rendered['hookSpecificOutput']['permissionDecision'], 'deny')
                reason = rendered['hookSpecificOutput']['permissionDecisionReason']
                self.assertIn('guard', reason)
                self.assertIn('停下', reason)
                self.assertNotIn('\n', reason)
                for omitted in ('[synthetic-guard]', '採用 guard.md 測試守衛規則', '作者', '來源', '"scope"'):
                    self.assertNotIn(omitted, reason)
                self.assertTrue(pending)
                self.assertFalse(rule_context.state_path(self.vault, key).exists())

                def emit(stream):
                    with patch.object(sys, 'stdin', io.StringIO(json.dumps(event))), \
                         patch.object(sys, 'argv', ['pretool']), \
                         patch.object(pretool, '_STARTED_AT', time.monotonic()), \
                         contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(pretool.main(), 0)
                emit(FailedFlush())
                self.assertFalse(rule_context.state_path(self.vault, key).exists())
                emitted = io.StringIO()
                emit(emitted)
                self.assertEqual(json.loads(emitted.getvalue()), rendered)
                self.assertFalse(rule_context.state_path(self.vault, key).exists())
                precompact._handle(event, time.monotonic(), learn=False)
                destination = precompact._map_destination(self.vault, event, transcript)
                handoff = compact_map.handoff_destination(self.vault, event['session_id'], transcript)
                self.assertTrue(destination.is_file())
                self.assertFalse(list(handoff.parent.glob(handoff.stem + '*.rules.json')))
                # Even a legacy snapshot must not be replayed by SessionStart.
                rule_context.record(self.vault, key, 'guard.md')
                rule_context.snapshot([self.vault], key, handoff, context_id=key)
                self.assertIn('採用 guard.md 測試守衛規則', rule_context.restore(
                    handoff, [self.vault], event=event, context_id=key))
                result = sessionstart._handle({**event, 'source': 'compact'}, time.monotonic())
                restored = result['hookSpecificOutput']['additionalContext']
                self.assertIn(str(destination), restored)
                for omitted in ('[synthetic-guard]', '採用 guard.md 測試守衛規則', '停下', '禁止測試動作'):
                    self.assertNotIn(omitted, restored)

    def test_stop_audit_keeps_decision_identity_without_claiming_card_delivery(self):
        import stop_gate
        from epitype import gates_report, memspec
        self.write_card('different-filename.md', name='different-filename', description='不要重開裁定')
        stop_gate._audit({memspec.CONFIG_VAULTS_FIELD: [self.vault]}, 'owner-key', 'forbidden',
                         time.monotonic(), 'stop-record', card_path=self.vault / 'different-filename.md')
        self.assertFalse(rule_context.state_path(self.vault, 'stop-record').exists())
        self.assertEqual(gates_report.load_rows(self.vault / memspec.GATE_LOG_FILENAME)[0][-1].label, 'owner-key')
        self.assertEqual(gates_report._row_label({'kind': memspec.ACTION_GUARD_LOG_KIND,
                        'card': 'guard', 'card_path': 'guard.md'}), 'guard')

    def test_budget_expiry_and_path_boundary(self):
        for number in range(20):
            self.write_card(f'rule-{number}.md', name=f'rule-{number}', description='規則描述' * 90)
            rule_context.record(self.vault, 's', f'rule-{number}.md')
        self.write_card('expired.md', name='expired', description='不應補送', valid_until='2000-01-01')
        rule_context.record(self.vault, 's', 'expired.md')
        self.write_card('retired.md', name='retired', description='已被取代不補送', status='superseded')
        rule_context.record(self.vault, 's', 'retired.md')
        destination = self.root / 'handoff.md'
        rule_context.snapshot([self.vault], 's', destination)
        result = rule_context.restore(destination, [self.vault])
        self.assertLessEqual(len(result.encode('utf-8')), 1800)
        self.assertNotIn('不應補送', result)
        self.assertNotIn('已被取代不補送', result)
        self.assertEqual(rule_context.restore(destination, [self.root / 'other']), '')

    def test_project_delivery_ledger_retains_source_vault_without_rule_snapshot(self):
        import _hook_common as common
        project = self.root / 'project-vault'
        project.mkdir()
        (project / 'project-rule.md').write_text('---\nname: project-rule\ndescription: 專案規則\n---\n', encoding='utf-8')
        common.append_recall_deliveries(self.vault, [{'session_id':'project-recall',
             'vault':str(project), 'card':'project-rule.md','host':'codex','mode':'card'}], time.monotonic())
        from epitype import memspec
        ledger = self.vault / memspec.FTS_INDEX_DIRECTORY / memspec.RECALL_DELIVERY_LOG_FILENAME
        rows = [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual({key: rows[0][key] for key in ('session_id', 'vault', 'card', 'host', 'mode')},
                         {'session_id': 'project-recall', 'vault': str(project),
                          'card': 'project-rule.md', 'host': 'codex', 'mode': 'card'})
        self.assertFalse((project / memspec.FTS_INDEX_DIRECTORY / memspec.RECALL_DELIVERY_LOG_FILENAME).exists())
        self.assertFalse(rule_context.state_path(project,'project-recall').exists())
        self.assertFalse(rule_context.state_path(self.vault,'project-recall').exists())


    def test_restore_does_not_reintroduce_another_projects_legacy_reference(self):
        other = self.root / 'other'
        path = self.vault / 'scoped-reference.md'
        path.write_text('---\nname: scoped-reference\ndescription: project-specific receipt\n'
                        'metadata:\n  type: reference\n---\n'
                        f'**適用專案**：`{other}`。\n', encoding='utf-8')
        rule_context.record(self.vault, 'scope-restore', path.name)
        destination = self.root / 'scope-handoff.md'
        rule_context.snapshot([self.vault], 'scope-restore', destination)
        self.assertEqual(rule_context.restore(destination, [self.vault], event={'cwd': str(self.root / 'here')}), '')
        self.assertIn('project-specific receipt', rule_context.restore(
            destination, [self.vault], event={'cwd': str(other)}))


class ScopeApplicabilityRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='epitype-scope-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        rule_context._git_location.cache_clear()
        self.addCleanup(rule_context._git_location.cache_clear)

    def reference(self, body, **fields):
        from epitype import memspec
        text = ('---\nname: fixture\nmetadata:\n  type: reference\n'
                + ''.join(f'{key}: {value}\n' for key, value in fields.items()) + '---\n' + body)
        values, _ = memspec.frontmatter_text(text)
        return rule_context.reference_applicability(values, self.root / 'fixture.md',
                                                    {'cwd': str(self.root / 'here')}, card_text=text)[0]

    def test_only_unambiguous_explicit_legacy_ownership_excludes(self):
        declaration = f'**適用專案**：`{self.root / "other"}`。\n'
        self.assertEqual(self.reference(declaration), 'OUTSIDE')
        for body in ('````markdown\n```\n' + declaration + '````\n',
                     '~~~markdown\n' + declaration + '~~~\n',
                     '> ' + declaration,
                     '    ' + declaration,
                     declaration + declaration,
                     declaration + '**適用專案**：unknown\n',
                     '**適用專案**：`relative/project`。\n',
                     f'**適用專案**：`{self.root / "other"}`、`{self.root / "here"}`。\n',
                     'This mentions an unrelated project in ordinary prose.\n'):
            with self.subTest(body=body):
                self.assertEqual(self.reference(body), 'ADVISORY')

    def test_global_and_legacy_decisions_are_not_scoped_from_prose(self):
        declaration = f'**適用專案**：`{self.root / "other"}`。\n'
        self.assertEqual(self.reference(declaration, scope='global'), 'ADVISORY')
        self.assertEqual(self.reference(declaration, decision_key='standing-rule'), 'ADVISORY')

    def test_missing_or_named_context_is_advisory_and_explicit_mismatch_is_outside(self):
        scope = {'project': str(self.root / 'project'), 'actions': ['*']}
        self.assertEqual(rule_context.applicability(scope)[0], 'ADVISORY')
        self.assertEqual(rule_context.applicability(scope, {'cwd': 'relative'})[0], 'ADVISORY')
        scope['project'] = 'named-project'
        self.assertEqual(rule_context.applicability(scope, {'cwd': str(self.root)})[0], 'ADVISORY')
        self.assertEqual(rule_context.applicability(scope, {'project': 'another-project'})[0], 'OUTSIDE')
        self.assertEqual(rule_context.applicability(scope, {'project': 'named-project'})[0], 'APPLICABLE')

    def test_path_boundaries_and_broken_git_metadata(self):
        project = self.root / 'project'
        project.mkdir()
        (project / '.git').mkdir()  # Not a functioning repository.
        scope = {'project': str(project), 'actions': ['*']}
        self.assertEqual(rule_context.applicability(scope, {'cwd': str(project / 'child')})[0], 'APPLICABLE')
        self.assertEqual(rule_context.applicability(scope, {'cwd': str(self.root / 'project-other')})[0], 'OUTSIDE')
        with patch.object(rule_context, '_git_location', return_value=False):
            self.assertEqual(rule_context.applicability(scope, {'cwd': str(self.root / 'unknown')})[0], 'ADVISORY')

    def test_worktrees_share_project_but_keep_monorepo_subdirectory_boundaries(self):
        repo = self.root / 'repo'
        repo.mkdir()
        (repo / 'README.md').write_text('fixture\n', encoding='utf-8')
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
        git('init', '-q')
        git('add', 'README.md')
        git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.com',
            'commit', '-qm', 'fixture')
        worktree = self.root / 'worktree'
        git('worktree', 'add', '--detach', str(worktree))
        for root in (repo, worktree):
            (root / 'component').mkdir()
            (root / 'other').mkdir()
        scope = {'project': str(repo), 'actions': ['*']}
        self.assertEqual(rule_context.applicability(scope, {'cwd': str(worktree)})[0], 'APPLICABLE')
        scope['project'] = str(repo / 'component')
        self.assertEqual(rule_context.applicability(scope, {'cwd': str(worktree / 'component')})[0], 'APPLICABLE')
        self.assertEqual(rule_context.applicability(scope, {'cwd': str(worktree / 'other')})[0], 'OUTSIDE')
        self.assertEqual(rule_context.applicability(scope, {'project': str(worktree / 'component')})[0], 'APPLICABLE')

    def test_git_timeout_and_exhausted_budget_remain_advisory(self):
        source, current = self.root / 'source', self.root / 'current'
        for path in (source, current):
            (path / '.git').mkdir(parents=True)
        scope = {'project': str(source), 'actions': ['*']}
        event = {'cwd': str(current)}
        rule_context._git_location.cache_clear()
        with patch.object(rule_context.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired('git', 1)) as run:
            self.assertEqual(rule_context.applicability(scope, event)[0], 'ADVISORY')
            self.assertTrue(run.called)
            self.assertGreater(run.call_args.kwargs['timeout'], 0)
            self.assertLessEqual(run.call_args.kwargs['timeout'], 0.3)
        rule_context._git_location.cache_clear()
        with patch.object(rule_context.time, 'monotonic', return_value=100), \
             rule_context.rule_provenance.verification_window(100.05), \
             patch.object(rule_context.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired('git', 0.05)) as run:
            self.assertEqual(rule_context.applicability(scope, event)[0], 'ADVISORY')
            self.assertAlmostEqual(run.call_args.kwargs['timeout'], 0.05)
        rule_context._git_location.cache_clear()
        with rule_context.rule_provenance.verification_window(time.monotonic() - 1), \
             patch.object(rule_context.subprocess, 'run') as run:
            self.assertEqual(rule_context.applicability(scope, event)[0], 'ADVISORY')
            run.assert_not_called()

    def test_exhausted_budget_is_not_cached_but_completed_lookups_are(self):
        source, current = self.root / 'source', self.root / 'current'
        for path in (source, current):
            (path / '.git').mkdir(parents=True)
        scope = {'project': str(source), 'actions': ['*']}
        event = {'cwd': str(current)}
        with rule_context.rule_provenance.verification_window(time.monotonic() - 1), \
             patch.object(rule_context.subprocess, 'run') as run:
            self.assertEqual(rule_context.applicability(scope, event)[0], 'ADVISORY')
            run.assert_not_called()
        self.assertEqual(rule_context._git_location.cache_info().currsize, 0)
        def completed(command, **kwargs):
            path = command[2]
            return subprocess.CompletedProcess(command, 0, f'{self.root / "common.git"}\n{path}\n')
        with rule_context.rule_provenance.verification_window(time.monotonic() + 60), \
             patch.object(rule_context.subprocess, 'run', side_effect=completed) as run:
            self.assertEqual(rule_context.applicability(scope, event)[0], 'APPLICABLE')
            self.assertEqual(run.call_count, 2)
            self.assertEqual(rule_context.applicability(scope, event)[0], 'APPLICABLE')
            self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
