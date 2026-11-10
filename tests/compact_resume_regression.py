"""Working-state recovery must preserve complete boundaries without replaying history."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import tempfile
import contextlib
import io
import json
import os
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'adapters' / 'claude')]
from epitype import compact_map, fixed_load
import _hook_common as common
import sessionstart_hook


class CompactResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='epitype-resume-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.transcript = self.root / 'session.jsonl'
        self.transcript.write_text('{}\n', encoding='utf-8')
        self.event = {'session_id': 'own-task', 'transcript_path': str(self.transcript), 'source': 'compact'}
        self.path = compact_map.handoff_destination(self.root, 'own-task', self.transcript)
        self.path.parent.mkdir(parents=True)

    def write_state(self, state='目前只改續行，其他設定保留。'):
        self.path.write_text(compact_map.RESUME_BEGIN + '\n' + state + '\n' +
                             compact_map.RESUME_END + '\n歷史原話不應整份載回。', encoding='utf-8')

    def test_complete_state_excludes_history(self):
        self.write_state()
        result = compact_map.handoff_resume(self.path)
        self.assertIn('目前只改續行', result)
        self.assertIn('AI整理', result)
        self.assertNotIn('歷史原話', result)

    def test_compact_recovery_does_not_replay_old_rule_snapshots(self):
        from epitype import rule_context
        self.write_state('current task state')
        with self.isolated_start(), patch.object(
                rule_context, 'restore', return_value='unrelated old rule') as restore:
            value = sessionstart_hook._handle(self.event, time.monotonic())
        restore.assert_not_called()
        text = value['hookSpecificOutput']['additionalContext']
        self.assertIn('current task state', text)
        self.assertNotIn('unrelated old rule', text)

    def test_missing_or_legacy_state_uses_existing_pointer(self):
        self.assertIsNone(compact_map.handoff_resume(self.path))
        self.path.write_text('legacy handoff', encoding='utf-8')
        self.assertIsNone(compact_map.handoff_resume(self.path))
        self.assertIn(str(self.path), sessionstart_hook._handoff_line(self.event, self.root)[0])

    def test_incomplete_and_oversized_state_never_loses_a_boundary_by_truncation(self):
        self.path.write_text(compact_map.RESUME_BEGIN+'\n禁止修改其他設定',encoding='utf-8')
        self.assertIsNone(compact_map.handoff_resume(self.path))
        self.write_state('keep scope\n'+'x'*2100+'\ncritical exception')
        self.assertIsNone(compact_map.handoff_resume(self.path))
        self.assertIsNotNone(sessionstart_hook._handoff_line(self.event, self.root))

    def test_ambiguous_marker_or_invalid_utf8_falls_back(self):
        self.write_state(compact_map.RESUME_BEGIN+' nested')
        self.assertIsNone(compact_map.handoff_resume(self.path))
        self.path.write_bytes(compact_map.RESUME_BEGIN.encode()+b'\n\xff\n'+compact_map.RESUME_END.encode())
        self.assertIsNone(compact_map.handoff_resume(self.path))

    def test_unchanged_working_state_returns_after_a_second_compaction(self):
        self.write_state()
        claim = sessionstart_hook._handoff_line(self.event, self.root)
        sessionstart_hook._record_deliveries([claim[1:]])
        self.assertIsNotNone(sessionstart_hook._handoff_line(self.event, self.root))

    def test_legacy_dedupe_and_session_isolation_are_preserved(self):
        self.path.write_text('legacy next step',encoding='utf-8')
        claim = sessionstart_hook._handoff_line(self.event, self.root)
        sessionstart_hook._record_deliveries([claim[1:]])
        self.assertIsNone(sessionstart_hook._handoff_line(self.event, self.root))
        other = dict(self.event, session_id='other-task')
        self.assertIsNone(sessionstart_hook._handoff_line(other, self.root))

    def isolated_start(self, budget=12000):
        stack = contextlib.ExitStack()
        config = {'vaults': [self.root], 'budget_bytes': budget}
        for target, value in [('load_config', config), ('resolve_vaults', [self.root]),
                ('native_cwd_vaults', []), ('governance_vault', self.root),
                ('_dream_spawn', None), ('_dream_notice', None), ('clear_recall_markers', None)]:
            stack.enter_context(patch.object(sessionstart_hook, target, return_value=value))
        stack.enter_context(patch.object(fixed_load, 'notice', return_value=(None, None)))
        stack.enter_context(patch.object(sessionstart_hook, 'config_path', return_value=self.root/'no-config'))
        stack.enter_context(patch.dict(os.environ, {
            'HOME': str(self.root), 'USERPROFILE': str(self.root), 'CODEX_HOME': str(self.root/'.codex')}))
        stack.enter_context(patch.object(sessionstart_hook, 'trace_hook'))
        return stack

    def actual_context(self, budget):
        with self.isolated_start(budget):
            trace, deliveries = {}, []
            value = sessionstart_hook._handle(self.event, time.monotonic(), deliveries, trace)
        text = (value or {}).get('hookSpecificOutput', {}).get('additionalContext', '')
        return text, trace, deliveries

    def test_complete_state_has_priority_over_history_and_diagnostics(self):
        self.write_state('進度與授權\n'+'x'*900+'\n關鍵例外必須保留。')
        compact_map.map_destination(self.root, 'own-task', self.transcript).write_text('map')
        resume = compact_map.handoff_resume(self.path)
        budget = next(n for n in range(1000, 2500, 10)
                      if common.payload_fits('SessionStart', resume, n))
        text, trace, claims = self.actual_context(budget)
        self.assertIn(resume, text)
        self.assertTrue(trace['resume'])
        self.assertTrue(common.payload_fits('SessionStart', text, budget))
        self.assertEqual(len(claims), 1)
        self.assertFalse(compact_map.handoff_delivered_path(self.path).exists())

    def test_long_pointer_does_not_gate_working_state(self):
        self.root = self.root/('nested-'+'a'*55)/('nested-'+'b'*55)/('nested-'+'c'*55)
        self.path = compact_map.handoff_destination(self.root, 'own-task', self.transcript)
        self.path.parent.mkdir(parents=True)
        self.write_state('完整工作狀態：正確範圍、必要例外、下一步。')
        self.assertIsNone(compact_map.handoff_notice(self.path))
        text, trace, claims = self.actual_context(12000)
        self.assertIn(compact_map.handoff_resume(self.path), text)
        self.assertTrue(trace['resume'])
        self.assertEqual(len(claims), 1)

    def test_final_budget_never_sends_partial_state(self):
        self.write_state('完整授權\n'+'x'*900+'\n關鍵例外必須保留。')
        text, trace, _ = self.actual_context(450)
        self.assertNotIn('完整授權', text)
        self.assertNotIn('x'*50, text)
        self.assertFalse(trace['resume'])
        self.assertTrue(not text or common.payload_fits('SessionStart', text, 450))

    def run_main_with_notices(self, output, budget=12000):
        self.write_state('同一任務完整狀態')
        def dream_notice(vault, source, settings, deliveries):
            deliveries.append(('dream observation', self.root, {}, True))
            return 'dream observation'
        with self.isolated_start(budget), contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(fixed_load, 'notice', return_value=('fixed load observation', {})))
            stack.enter_context(patch.object(sessionstart_hook, '_dream_notice', side_effect=dream_notice))
            publish = stack.enter_context(patch.object(fixed_load, 'publish'))
            mark = stack.enter_context(patch.object(sessionstart_hook, '_dream_mark_notified'))
            stack.enter_context(patch.object(sessionstart_hook, '_STARTED_AT', time.monotonic()))
            stack.enter_context(patch.object(sys, 'argv', ['sessionstart']))
            stack.enter_context(patch.object(sys, 'stdin', io.StringIO(json.dumps(self.event))))
            stack.enter_context(contextlib.redirect_stdout(output))
            sessionstart_hook.main()
            return publish.call_count, mark.call_count

    def test_failed_flush_only_records_fixed_load_observation(self):
        class FailedFlush(io.StringIO):
            def flush(self):
                raise OSError('synthetic flush failure')
        self.assertEqual(self.run_main_with_notices(FailedFlush()), (1, 0))
        self.assertFalse(compact_map.handoff_delivered_path(self.path).exists())

    def test_successful_output_acknowledges_only_after_flush(self):
        output = io.StringIO()
        self.assertEqual(self.run_main_with_notices(output), (1, 1))
        self.assertTrue(compact_map.handoff_delivered_path(self.path).exists())
        self.assertIn('同一任務完整狀態', json.loads(output.getvalue())['hookSpecificOutput']['additionalContext'])

    def test_notices_cut_by_final_budget_are_not_acknowledged(self):
        self.write_state('同一任務完整狀態')
        resume = compact_map.handoff_resume(self.path)
        budget = next(n for n in range(100, 1000)
                      if common.payload_fits('SessionStart', resume, n))
        output = io.StringIO()
        self.assertEqual(self.run_main_with_notices(output, budget), (1, 0))
        self.assertEqual(json.loads(output.getvalue())['hookSpecificOutput']['additionalContext'], resume)

    def test_fixed_load_records_observation_without_claiming_context_delivery(self):
        self.write_state()
        with self.isolated_start(), patch.object(fixed_load, 'notice', return_value=('fixed observation', {})), \
                patch.object(fixed_load, 'publish') as publish:
            claims = []
            value = sessionstart_hook._handle(self.event, time.monotonic(), notice_claims=claims)
            self.assertNotIn('fixed observation', value['hookSpecificOutput']['additionalContext'])
            self.assertEqual(claims, [])
            publish.assert_called_once_with({}, fixed_load.cache_paths(
                self.root, Path.cwd(), legacy=True)[1])

    def test_fallback_is_required_only_when_complete_state_is_not_delivered(self):
        self.write_state('續行狀態\n'+'x'*900+'\n必要例外不得遺漏。')
        resume = compact_map.handoff_resume(self.path)
        exact = next(n for n in range(100, 2500)
                     if common.payload_fits('SessionStart', resume, n))
        delivered, trace, claims = self.actual_context(exact)
        self.assertIn(resume, delivered)
        self.assertTrue(trace['resume'])
        self.assertEqual(len(claims), 1)
        missing, trace, claims = self.actual_context(exact-1)
        self.assertNotIn(resume, missing)
        self.assertIn(compact_map.handoff_notice(self.path), missing)
        self.assertNotIn('接續原件（按需查）', missing)
        self.assertFalse(trace['resume'])
        self.assertEqual(len(claims), 1)
        roomy, trace, _ = self.actual_context(12000)
        self.assertIn(compact_map.resume_notice(self.path), roomy)
        self.assertNotIn(compact_map.handoff_notice(self.path), roomy)

    def test_omission_notice_cannot_evict_state_at_exact_utf8_boundary(self):
        self.write_state('有效進度、範圍與例外。'*24)
        resume = compact_map.handoff_resume(self.path)
        exact = next(n for n in range(100, 2500)
                     if common.payload_fits('SessionStart', resume, n))
        text, trace, _ = self.actual_context(exact)
        self.assertEqual(text, resume)
        self.assertGreater(trace['omitted_segments'], 0)
        self.assertFalse(trace['omission_notice_sent'])

    def test_required_handoff_precedes_restored_rules_when_state_cannot_fit(self):
        from epitype import rule_context
        self.write_state('目前任務與例外\n'+'s'*1800)
        resume = compact_map.handoff_resume(self.path)
        must_read = compact_map.handoff_notice(self.path)
        restored = 'complete restored rule '+('r'*900)
        budget = next(n for n in range(100, 2000)
            if common.payload_fits('SessionStart', restored, n)
            and common.payload_fits('SessionStart', must_read, n)
            and not common.payload_fits('SessionStart', restored+'\n'+must_read, n)
            and not common.payload_fits('SessionStart', resume, n))
        output = io.StringIO()
        with self.isolated_start(budget), contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(rule_context, 'restore', return_value=restored))
            stack.enter_context(patch.object(sessionstart_hook.hook_context, 'identity', return_value='ctx-own'))
            stack.enter_context(patch.object(sessionstart_hook, '_STARTED_AT', time.monotonic()))
            stack.enter_context(patch.object(sys, 'argv', ['sessionstart']))
            stack.enter_context(patch.object(sys, 'stdin', io.StringIO(json.dumps(self.event))))
            stack.enter_context(contextlib.redirect_stdout(output))
            sessionstart_hook.main()
        text = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
        self.assertIn(must_read, text)
        self.assertNotIn(resume, text)
        self.assertTrue(compact_map.handoff_delivered_path(self.path).exists())
        self.assertTrue(common.payload_fits('SessionStart', text, budget))

    def test_omission_notice_reports_skips_without_evicting_priority_content(self):
        state, restored = 'working state '+('s'*100), 'restored complete rule '+('r'*300)
        optional = 'very long optional source pointer '+('p'*700)
        trace, selected = {}, []
        base = state+'\n'+restored
        budget = next(n for n in range(100, 2000) if common.payload_fits('SessionStart',
            base+'\n'+sessionstart_hook.memspec.CONTEXT_TRUNCATED_SUFFIX.format(dropped=1), n))
        text = sessionstart_hook._compact_context([state, restored, optional], budget,
            preserve=(state, restored), selected_out=selected, trace=trace)
        self.assertEqual(selected, [state, restored])
        self.assertIn(state, text)
        self.assertIn(restored, text)
        self.assertTrue(trace['omission_notice_sent'])
        self.assertEqual(trace['omitted_segments'], 1)
        self.assertTrue(common.payload_fits('SessionStart', text, budget))

    def test_output_timeout_does_not_acknowledge(self):
        with self.isolated_start(), contextlib.ExitStack() as stack:
            def prepared(event, started, deliveries, trace, notices):
                notices.append(('dream', (self.root, {}, True)))
                return common.payload('SessionStart', 'prepared but never emitted')
            stack.enter_context(patch.object(sessionstart_hook, '_handle', side_effect=prepared))
            stack.enter_context(patch.object(sessionstart_hook, 'expired', return_value=True))
            mark = stack.enter_context(patch.object(sessionstart_hook, '_dream_mark_notified'))
            stack.enter_context(patch.object(sys, 'argv', ['sessionstart']))
            stack.enter_context(patch.object(sys, 'stdin', io.StringIO(json.dumps(self.event))))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            sessionstart_hook.main()
            self.assertEqual(output.getvalue(), '')
            mark.assert_not_called()


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
