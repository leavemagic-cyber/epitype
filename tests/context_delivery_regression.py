"""Receipts must follow model contexts rather than shared parent session IDs."""
import sys
sys.dont_write_bytecode = True
import json
import contextlib
import io
import time
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import recall_selection_regression as fixtures
from epitype import hook_context, memspec, rule_context
import _hook_common as common
import pretooluse_gate as pretool
import sessionstart_hook as sessionstart
import recall_hook as recall
import stop_gate as stop
import precompact_hook as precompact


class ContextDeliveryRegression(unittest.TestCase):
    def setUp(self):
        fixtures.RecallSelectionRegression.setUp(self)
        temporary = patch.object(common, 'temp_root', return_value=self.root/'hook-temp')
        temporary.start()
        self.addCleanup(temporary.stop)

    card = fixtures.RecallSelectionRegression.card
    populate = fixtures.RecallSelectionRegression.populate
    invoke = fixtures.RecallSelectionRegression.invoke
    lines = staticmethod(fixtures.RecallSelectionRegression.lines)

    def test_child_transcript_detection_uses_normalized_directory_components(self):
        self.assertTrue(hook_context.is_subagent_transcript('C:\\tasks\\subagents\\agent-A.jsonl'))
        self.assertTrue(hook_context.is_subagent_transcript('/tasks/subagents/agent-A.jsonl'))
        for path in ('/tasks/subagents-backup/main.jsonl', '/tasks/subagents/../main.jsonl',
                     '/tasks/subagents.jsonl', None):
            self.assertFalse(hook_context.is_subagent_transcript(path))

    def test_codex_native_parent_metadata_identifies_child_but_exec_mode_alone_does_not(self):
        for name, payload, expected in (
                ('child', {'parent_thread_id': 'parent', 'source': {'subagent': {
                    'thread_spawn': {'parent_thread_id': 'parent', 'depth': 1}}}}, True),
                ('root', {'originator': 'Codex Desktop', 'source': 'vscode'}, False),
                ('exec', {'originator': 'codex_exec', 'source': 'exec'}, False)):
            with self.subTest(name=name):
                path = self.root / ('rollout-' + name + '.jsonl')
                path.write_text(json.dumps({'type': 'session_meta', 'payload': payload})+'\n', encoding='utf-8')
                event = {'session_id': 'parent', 'transcript_path': str(path), 'source': 'startup'}
                self.assertEqual(hook_context.is_subagent(event), expected)
                if expected:
                    with patch.object(sessionstart, 'load_config', side_effect=AssertionError('child startup')):
                        self.assertIsNone(sessionstart._handle(event, time.monotonic()))

    def test_codex_child_detection_is_bounded_to_the_first_metadata_line(self):
        child = json.dumps({'type': 'session_meta', 'payload': {'parent_thread_id': 'parent'}})
        for name, content in (('late', '{}\n'+child), ('large', ' '*32769+child),
                              ('bad', '{bad'), ('array', '[]\n')):
            path = self.root / ('rollout-' + name + '.jsonl')
            path.write_text(content, encoding='utf-8')
            self.assertFalse(hook_context.is_subagent_transcript(str(path)))

    def test_codex_child_compaction_keeps_its_own_short_state(self):
        from epitype import compact_map
        path = self.root / 'rollout-native-child.jsonl'
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'parent_thread_id': 'parent', 'source': {'subagent': {'other': 'guardian'}}}})+'\n', encoding='utf-8')
        event = {'session_id': 'parent', 'transcript_path': str(path), 'source': 'compact'}
        precompact._handle(event, time.monotonic(), learn=False)
        handoff = compact_map.handoff_destination(self.vaults[0], 'parent', path)
        handoff.write_text('<!-- EPITYPE RESUME BEGIN -->\nnative child state\n'
                           '<!-- EPITYPE RESUME END -->\n', encoding='utf-8')
        with patch.object(sessionstart, '_dream_spawn') as spawn, \
             patch.object(sessionstart, '_startup_segments') as maintenance:
            value = sessionstart._handle(event, time.monotonic())
        self.assertIn('native child state', str(value))
        spawn.assert_not_called()
        maintenance.assert_not_called()

    def test_child_startup_never_loads_parent_recovery_or_maintenance(self):
        cases = [
            {'agent_id': 'worker-A', 'transcript_path': str(self.root/'parent.jsonl')},
            {'agentId': 'worker-A', 'transcript_path': str(self.root/'parent.jsonl')},
            {'transcript_path': str(self.root/'subagents/agent-worker-A.jsonl')},
        ]
        for fields in cases:
            with self.subTest(fields=fields), patch.object(
                    sessionstart, 'load_config', side_effect=AssertionError('child scanned parent startup')):
                trace = {}
                self.assertIsNone(sessionstart._handle(
                    {'session_id': 'parent', 'source': 'startup', **fields}, time.monotonic(), trace=trace))
                self.assertEqual(trace['outcome'], 'subagent-startup')

    def test_child_compaction_with_parent_transcript_cannot_write_parent_files(self):
        transcript = self.root/'parent.jsonl'
        transcript.write_text('{}\n', encoding='utf-8')
        event = {'session_id': 'parent', 'agent_id': 'worker-A',
                 'transcript_path': str(transcript), 'trigger': 'auto'}
        with patch.object(precompact, 'load_config', side_effect=AssertionError('unconfirmed child write')):
            trace = {}
            self.assertIsNone(precompact._handle(event, time.monotonic(), trace=trace))
            self.assertEqual(trace['outcome'], 'subagent-transcript-unconfirmed')

    def test_unconfirmed_child_resume_preserves_parent_and_child_receipts(self):
        main = {'session_id': 'parent', 'transcript_path': str(self.root/'parent.jsonl')}
        child = {**main, 'agent_id': 'worker-A', 'source': 'compact'}
        with patch.object(common, 'recall_marker_directory', lambda key: self.root/'markers'/key):
            sentinels = []
            for event in (main, child):
                path = common.recall_marker_directory(hook_context.identity(event))/'receipt'
                path.parent.mkdir(parents=True)
                path.write_text('delivered', encoding='utf-8')
                sentinels.append(path)
            sessionstart._handle(child, time.monotonic())
            self.assertTrue(all(path.read_text(encoding='utf-8') == 'delivered' for path in sentinels))

    def test_lifecycle_trace_records_field_names_without_custom_values(self):
        event = {'session_id': 'parent', 'source': 'startup', 'agent_id': 'worker-A',
                 'transcript_path': str(self.root/'parent.jsonl'), 'custom_field': 'synthetic-private-value'}
        for module in (sessionstart, precompact):
            with self.subTest(module=module.__name__), \
                 patch.object(sys, 'argv', ['hook']), \
                 patch.object(sys, 'stdin', io.StringIO(json.dumps(event))), \
                 patch.object(module, 'trace_hook') as trace:
                self.assertEqual(module.main(), 0)
                self.assertEqual(len(trace.call_args_list), 2)
                for call in trace.call_args_list:
                    self.assertEqual(call.kwargs['event_fields'], sorted(event))
                    self.assertTrue(call.kwargs['child'])
                    self.assertNotIn('synthetic-private-value', str(call.kwargs))

    def test_precompact_builds_recovery_map_without_recopying_rule_snapshots(self):
        from epitype import compact_map
        transcript = self.root/'parent.jsonl'
        transcript.write_text('{}\n', encoding='utf-8')
        event = {'session_id': 'parent', 'transcript_path': str(transcript)}
        with patch.object(rule_context, 'snapshot') as snapshot:
            precompact._handle(event, time.monotonic(), learn=False)
        snapshot.assert_not_called()
        self.assertTrue(compact_map.map_destination(self.vaults[0], 'parent', transcript).is_file())

    def test_child_own_compaction_resumes_only_its_state_without_maintenance(self):
        from epitype import compact_map, fixed_load
        parent = self.root/'parent.jsonl'
        child = self.root/'subagents/agent-worker-A.jsonl'
        child.parent.mkdir()
        parent.write_text('{}\n', encoding='utf-8')
        child.write_text('{}\n', encoding='utf-8')
        vault = self.vaults[0]
        parent_map = compact_map.map_destination(vault, 'parent', parent)
        parent_map.parent.mkdir(parents=True)
        parent_map.write_text('parent map sentinel', encoding='utf-8')
        parent_handoff = compact_map.handoff_destination(vault, 'parent', parent)
        parent_handoff.write_text('parent handoff sentinel', encoding='utf-8')
        event = {'session_id': 'parent', 'transcript_path': str(child),
                 'source': 'compact', 'trigger': 'auto'}
        precompact._handle(event, time.monotonic(), learn=False)
        child_map = compact_map.map_destination(vault, 'parent', child)
        self.assertNotEqual(child_map, parent_map)
        self.assertTrue(child_map.is_file())
        handoff = compact_map.handoff_destination(vault, 'parent', child)
        handoff.write_text('<!-- EPITYPE RESUME BEGIN -->\nchild current state\n'
                           '<!-- EPITYPE RESUME END -->\n', encoding='utf-8')
        with patch.object(sessionstart, '_startup_segments', side_effect=AssertionError('maintenance')), \
             patch.object(sessionstart, '_dream_spawn', side_effect=AssertionError('spawn')) as spawn, \
             patch.object(sessionstart, '_dream_notice', side_effect=AssertionError('dream')) as dream, \
             patch.object(fixed_load, 'notice', side_effect=AssertionError('fixed')) as fixed:
            value = sessionstart._handle(event, time.monotonic())
        self.assertIn('child current state', str(value))
        self.assertNotIn('parent handoff sentinel', str(value))
        spawn.assert_not_called()
        dream.assert_not_called()
        fixed.assert_not_called()
        self.assertEqual(parent_map.read_text(encoding='utf-8'), 'parent map sentinel')
        self.assertEqual(parent_handoff.read_text(encoding='utf-8'), 'parent handoff sentinel')

    def test_main_and_explicit_child_each_receive_cards_and_keep_raw_audit_session(self):
        self.populate(count=1)
        records = []
        with patch.object(recall, 'append_recall_deliveries',
                          lambda vault, rows, started: records.extend(rows)):
            main = self.invoke('parent')
            child = self.invoke('parent', agent_id='worker-A',
                                transcript_path=str(self.root/'subagents/worker-A.jsonl'))
        self.assertEqual((len(self.lines(main)), len(self.lines(child))), (2, 2))
        self.assertEqual({row['session_id'] for row in records}, {'parent'})
        self.assertEqual(len({row['context_id'] for row in records}), 2)

    def test_distinct_transcripts_without_agent_field_do_not_share_receipts(self):
        self.populate(count=1)
        main = self.invoke('parent')
        other = self.invoke('parent', transcript_path=str(self.root/'other.jsonl'))
        self.assertEqual((len(self.lines(main)), len(self.lines(other))), (2, 2))

    def test_same_known_context_keeps_deduplication(self):
        self.populate(count=1)
        self.assertEqual(len(self.lines(self.invoke('stable'))), 2)
        self.assertEqual(self.invoke('stable'), '')

    def test_missing_context_identity_never_uses_shared_parent_receipts(self):
        self.populate(count=1)
        for _ in range(2):
            self.assertEqual(len(self.lines(self.invoke('unknown', transcript_path=None))), 2)

    def test_known_transcript_without_parent_id_still_has_its_own_receipts(self):
        self.populate(count=1)
        event = {'transcript_path': str(self.root/'standalone.jsonl')}
        self.assertEqual(len(self.lines(self.invoke('', **event))), 2)
        self.assertEqual(self.invoke('', **event), '')

    def test_agent_and_host_identity_are_distinct_without_reading_transcripts(self):
        event = {'session_id': 'parent', 'transcript_path': str(self.root/'.claude/projects/a.jsonl')}
        self.assertNotEqual(hook_context.identity(event),
                            hook_context.identity({**event, 'agent_id': 'worker-A'}))
        self.assertNotEqual(hook_context.identity(event),
                            hook_context.identity({**event, 'transcript_path': str(self.root/'.codex/sessions/a.jsonl')}))
        self.assertEqual(hook_context.identity(event), hook_context.identity(dict(event)))

    def test_path_normalization_and_parent_ids_do_not_use_lossy_filename_sanitizing(self):
        event = {'session_id': 'a/b', 'cwd': str(self.root), 'transcript_path': 'logs/../thread.jsonl'}
        canonical = {**event, 'transcript_path': str(self.root/'thread.jsonl')}
        self.assertEqual(hook_context.identity(event), hook_context.identity(canonical))
        self.assertNotEqual(hook_context.identity(event), hook_context.identity({**event, 'session_id': 'a_b'}))
        self.assertIsNone(hook_context.identity({'session_id': 'a', 'transcript_path': 'relative.jsonl'}))

    def test_compact_reset_clears_only_the_compacted_agents_receipts_even_without_config(self):
        main = {'session_id': 'parent', 'transcript_path': str(self.root/'main.jsonl')}
        child = {**main, 'agent_id': 'worker-A', 'transcript_path': str(self.root/'subagents/child.jsonl')}
        contexts = [hook_context.identity(event) for event in (main, child)]
        with patch.object(sessionstart, 'clear_recall_markers', common.clear_recall_markers), \
             patch.object(common, 'recall_marker_directory', lambda key: self.root/'markers'/key), \
             patch.object(sessionstart, 'load_config', return_value=None):
            for key in contexts:
                directory = common.recall_marker_directory(key)
                directory.mkdir(parents=True)
                (directory/'receipt').write_text('delivered')
            sessionstart._handle({**child, 'source': 'compact'}, time.monotonic())
            self.assertTrue((common.recall_marker_directory(contexts[0])/'receipt').is_file())
            self.assertFalse(common.recall_marker_directory(contexts[1]).exists())
            sessionstart._handle({**main, 'source': 'compact'}, time.monotonic())
            self.assertFalse(common.recall_marker_directory(contexts[0]).exists())

    def test_advice_state_requires_successful_output_and_is_not_an_audit_receipt(self):
        vault = self.vaults[0]
        self.card(vault, 'advised')
        path = vault/'advised.md'
        event = {'session_id': 'parent', 'transcript_path': str(self.root/'main.jsonl')}
        key = hook_context.identity(event)
        common.append_gate_log(vault, {'session_id': key, 'card': path.name}, time.monotonic())
        self.assertFalse(rule_context.state_path(vault, key).exists())
        claims = [(key, 'tool-advice', path, vault)]
        pretool._finish_advice(claims, False)
        self.assertFalse(rule_context.state_path(vault, key).exists())
        pretool._finish_advice([(key, 'tool-advice', path, vault)], True)
        self.assertFalse(rule_context.state_path(vault, key).exists())
        self.assertFalse(rule_context.state_path(vault, 'parent').exists())

    def test_delivery_audit_does_not_write_retired_snapshot_state(self):
        vault = self.vaults[0]
        row = {'session_id': 'parent', 'context_id': 'known-context', 'card': 'rule.md', 'vault': str(vault)}
        with patch.object(rule_context, 'record', side_effect=AssertionError('unused state write')):
            common.append_recall_deliveries(vault, [row], time.monotonic())
        path = vault / memspec.FTS_INDEX_DIRECTORY / memspec.RECALL_DELIVERY_LOG_FILENAME
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['context_id'], 'known-context')

    def test_stop_note_reaches_only_its_context_on_the_next_real_prompt(self):
        main = {'session_id': 'parent', 'transcript_path': str(self.root/'main.jsonl')}
        child = {**main, 'agent_id': 'worker-A', 'transcript_path': str(self.root/'child.jsonl')}
        defects = []
        with patch.object(stop.rule_provenance, 'claim_violation', return_value='bounded attribution finding'):
            stop._source_notice(child, 'fixture reply', time.monotonic(), defects)
        self.assertEqual(defects, [])
        self.assertEqual(self.invoke('parent', prompt='unmatched', **{k: v for k, v in main.items() if k != 'session_id'}), '')
        options = {k: v for k, v in child.items() if k != 'session_id'}
        self.assertIn('bounded attribution finding', self.invoke('parent', prompt='unmatched', **options))
        self.assertEqual(self.invoke('parent', prompt='unmatched', **options), '')
        self.assertFalse(rule_context.state_path(self.vaults[0], hook_context.identity(child)).exists())

    def test_documented_subagentstop_attribution_note_goes_to_parent_once(self):
        parent = {'session_id': 'parent', 'transcript_path': str(self.root/'main.jsonl')}
        child = {**parent, 'hook_event_name': 'SubagentStop', 'agent_id': 'worker-A',
                 'agent_type': 'Explore', 'agent_transcript_path': str(self.root/'subagents/worker-A.jsonl')}
        defects = []
        with patch.object(stop.rule_provenance, 'claim_violation', return_value='documented subagent finding'):
            stop._source_notice(child, 'fixture reply', time.monotonic(), defects)
        self.assertEqual(defects, [])
        self.assertNotEqual(hook_context.identity(child), hook_context.identity(parent))
        self.assertEqual(hook_context.notification_identity(child), hook_context.identity(parent))
        self.assertFalse(common.peek_notes({'vaults': self.vaults}, hook_context.identity(child)))
        first = self.invoke('parent', prompt='unmatched', transcript_path=parent['transcript_path'])
        self.assertIn('documented subagent finding', first)
        self.assertIn('子代理 worker-A', first)
        self.assertFalse(common.peek_notes({'vaults': self.vaults}, hook_context.identity(parent)))
        self.assertEqual(self.invoke('parent', prompt='unmatched', transcript_path=parent['transcript_path']), '')

    def test_documented_subagentstop_verdict_note_goes_to_parent_once(self):
        import turn_check_regression as turn_fixtures
        parent = {'session_id': 'parent', 'transcript_path': str(self.root/'main.jsonl')}
        child = {**parent, 'hook_event_name': 'SubagentStop', 'agent_id': 'worker-B',
                 'agent_type': 'Explore', 'agent_transcript_path': str(self.root/'subagents/worker-B.jsonl')}
        decision = turn_fixtures.decision(forbidden=('bounded-forbidden-phrase',))
        with patch.object(stop, '_vaults', return_value=self.vaults[:1]), \
             patch.object(stop, '_decisions', return_value=[decision]):
            self.assertIsNone(stop._verdict(child, 'bounded-forbidden-phrase',
                              {'vaults': self.vaults}, time.monotonic(), []))
        first = self.invoke('parent', prompt='unmatched', transcript_path=parent['transcript_path'])
        self.assertIn('子代理 worker-B', first)
        self.assertIn('細節寫進檔案', first)
        self.assertNotIn('bounded-forbidden-phrase', first)
        self.assertFalse(common.peek_notes({'vaults': self.vaults}, hook_context.identity(child)))
        self.assertFalse(common.peek_notes({'vaults': self.vaults}, hook_context.identity(parent)))
        self.assertEqual(self.invoke('parent', prompt='unmatched', transcript_path=parent['transcript_path']), '')

    def test_pretool_flush_failure_releases_advice_then_each_context_gets_it(self):
        vault = self.vaults[0]
        self.card(vault, 'advised')
        path = vault/'advised.md'
        config = {'vaults': self.vaults, 'budget_bytes': 12000}
        main = {'session_id': 'parent', 'transcript_path': str(self.root/'main.jsonl'),
                'tool_name': 'Read', 'tool_input': {'file_path': str(path)}}
        child = {**main, 'agent_id': 'worker-A', 'transcript_path': str(self.root/'child.jsonl')}

        class FailedFlush(io.StringIO):
            def flush(self):
                raise OSError('synthetic output failure')

        def handle(event, started, defects, meters, claims):
            return pretool._allow_context(event, config=config,
                advisories=[(path, 'advised', 'context-specific advice')], advice_claims=claims)

        def invoke(event, stream):
            with patch.object(pretool, '_handle', side_effect=handle), \
                 patch.object(pretool, '_best_effort_record'), \
                 patch.object(sys, 'stdin', io.StringIO(json.dumps(event))), \
                 patch.object(sys, 'argv', ['pretool']), \
                 patch.object(pretool, '_STARTED_AT', time.monotonic()), \
                 contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(pretool.main(), 0)
            return stream.getvalue()

        invoke(main, FailedFlush())
        self.assertFalse(rule_context.state_path(vault, hook_context.identity(main)).exists())
        self.assertIn('context-specific advice', invoke(main, io.StringIO()))
        self.assertFalse(rule_context.state_path(vault, hook_context.identity(main)).exists())
        self.assertEqual(invoke(main, io.StringIO()), '')
        self.assertIn('context-specific advice', invoke(child, io.StringIO()))
        self.assertFalse(rule_context.state_path(vault, hook_context.identity(child)).exists())

    def test_read_counts_are_not_shared_with_another_agent(self):
        target = self.root/'read.md'
        target.write_text('bounded fixture')
        config = {'vaults': self.vaults, 'budget_bytes': 12000}
        main = {'session_id': 'parent', 'transcript_path': str(self.root/'main.jsonl')}
        child = {**main, 'agent_id': 'worker-A', 'transcript_path': str(self.root/'child.jsonl')}
        def read(event):
            return pretool._waste_review(event, 'Read', {'file_path': str(target)}, config, time.monotonic())[1]
        self.assertIsNone(read(main))
        self.assertIn('第 2 次', read(main))
        self.assertIsNone(read(child))
        self.assertIn('第 3 次', read(main))

    def test_context_snapshots_reject_other_contexts_and_replace_stale_records_with_empty(self):
        vault = self.vaults[0]
        self.card(vault, 'current', 'still relevant')
        key = 'ctx-v1-test-main'
        destination = self.root/'resume.md'
        rule_context.record(vault, key, 'current.md')
        rule_context.snapshot([vault], key, destination, context_id=key)
        self.assertIn('still relevant', rule_context.restore(destination, [vault], context_id=key))
        self.assertEqual(rule_context.restore(destination, [vault], context_id='ctx-v1-other'), '')
        self.assertEqual(rule_context.restore(destination, [vault]), '')
        rule_context.state_path(vault, key).unlink()
        rule_context.snapshot([vault], key, destination, context_id=key)
        self.assertEqual(rule_context.restore(destination, [vault], context_id=key), '')

    def test_legacy_snapshot_is_not_proof_of_this_hosts_new_context(self):
        vault = self.vaults[0]
        self.card(vault, 'legacy', 'legacy content')
        destination = self.root/'legacy.md'
        destination.with_suffix('.rules.json').write_text(
            json.dumps([{'vault': str(vault.resolve()), 'card': 'legacy.md'}]))
        diagnostics = []
        self.assertEqual(rule_context.restore(destination, [vault], context_id='ctx-v1-main',
                                             diagnostics=diagnostics), '')
        self.assertIn('no model-context proof', diagnostics[0])

    def test_same_handoff_location_cannot_let_child_snapshot_replace_parent(self):
        vault = self.vaults[0]
        self.card(vault, 'parent-rule', 'parent-only content')
        self.card(vault, 'child-rule', 'child-only content')
        destination = self.root/'shared-handoff.md'
        rule_context.record(vault, 'parent-context', 'parent-rule.md')
        rule_context.record(vault, 'child-context', 'child-rule.md')
        rule_context.snapshot([vault], 'parent-context', destination, context_id='parent-context')
        rule_context.snapshot([vault], 'child-context', destination, context_id='child-context')
        parent = rule_context.restore(destination, [vault], context_id='parent-context')
        child = rule_context.restore(destination, [vault], context_id='child-context')
        self.assertIn('parent-only content', parent)
        self.assertNotIn('child-only content', parent)
        self.assertIn('child-only content', child)
        self.assertNotIn('parent-only content', child)

    def test_live_compact_without_identity_does_not_use_rule_snapshot_fallback(self):
        event = {'source': 'compact', 'session_id': 'unknown', 'transcript_path': 'relative.jsonl'}
        trace = {}
        with patch.object(rule_context, 'restore', return_value='unproven legacy instructions') as restore:
            result = sessionstart._handle(event, time.monotonic(), trace=trace)
        restore.assert_not_called()
        self.assertNotIn('unproven legacy instructions', str(result))


if __name__ == '__main__':
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
