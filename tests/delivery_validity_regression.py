import sys; sys.dont_write_bytecode = True
"""Exercise stale-index delivery, real output acknowledgement and moved index links."""
import io
import contextlib
from datetime import date, datetime, timezone
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recall_selection_regression as fixtures
from epitype import capture_route, host_sync, hook_context, memsearch, memspec, rule_context
import _hook_common as common
import recall_hook as recall


class DeliveryValidityRegression(unittest.TestCase):
    setUp = fixtures.RecallSelectionRegression.setUp
    card = fixtures.RecallSelectionRegression.card
    invoke = fixtures.RecallSelectionRegression.invoke

    def test_current_fields_override_stale_search_index_at_all_card_outlets(self):
        for value in ('valid_until: 2000-01-01', 'status: retired', 'status: closed',
                      'status: superseded', 'status: SUPERSEDED', 'status: draft', 'status: deprecated'):
            with self.subTest(value=value):
                self.card(self.vaults[0], 'expiryneedle', 'expiryneedle advice')
                memsearch.build_index(self.vaults[0])
                target = self.vaults[0] / 'expiryneedle.md'
                original = target.read_text(encoding='utf-8')
                target.write_text(original.replace('---\nname:', '---\n' + value + '\nname:', 1),
                                  encoding='utf-8')
                fields, _ = memspec.frontmatter_fields(target)
                self.assertFalse(memspec.card_is_current(fields))
                self.assertIsNone(recall._admitted_card(
                    {'path': str(target)}, self.vaults[0], {'cwd': str(self.root)}, set()))
                self.assertFalse(host_sync._project_card_allowed(target, self.root))
                rule_context.record(self.vaults[0], 'expiry', target.name)
                destination = self.root / 'handoff.md'
                rule_context.snapshot(self.vaults, 'expiry', destination)
                self.assertEqual(rule_context.restore(destination, self.vaults), '')
                self.assertNotIn('expiryneedle', self.invoke('expiry-' + value, 'expiryneedle'))

    def test_live_card_still_retrievable(self):
        self.card(self.vaults[0], 'currentneedle', 'currentneedle advice')
        memsearch.build_index(self.vaults[0])
        self.assertIn('currentneedle', self.invoke('current', 'currentneedle'))

    def test_nonfuture_root_cache_versions_repair_once_without_losing_collision(self):
        vault = self.root / 'home' / '.claude/projects/C--native/memory'
        vault.mkdir(parents=True)
        trace = vault.parent / 'one.jsonl'
        trace.write_text(json.dumps({'cwd': str(self.root / 'project')}), encoding='utf-8')
        capture_route.project_roots_of(vault)
        cache = vault / memspec.FTS_INDEX_DIRECTORY / memspec.PROJECT_ROOTS_CACHE_FILENAME
        for value in (None, '1', 0, True):
            with self.subTest(version=value):
                data = json.loads(cache.read_text(encoding='utf-8'))
                data['version'] = value
                data['roots'].append(str(self.root / 'historical-project'))
                cache.write_text(json.dumps(data), encoding='utf-8')
                with patch.object(capture_route, '_transcript_root',
                                  wraps=capture_route._transcript_root) as read:
                    self.assertEqual(len(capture_route.project_roots_of(vault)), 2)
                    first = read.call_count
                    self.assertEqual(len(capture_route.project_roots_of(vault)), 2)
                    self.assertEqual(read.call_count, first)
                repaired = json.loads(cache.read_text(encoding='utf-8'))
                self.assertIs(type(repaired['version']), int)
                self.assertEqual(repaired['version'], 1)

    def test_generated_links_resolve_from_entry_directory(self):
        vault = self.vaults[0]
        target = vault / 'space name.md'
        target.write_text('---\nname: space\ndescription: relevant\n---\n', encoding='utf-8')
        index = '# Memory\n- [source](space%20name.md#anchor)\n- [web](https://example.test/a)\n'
        (vault / 'MEMORY.md').write_text(index, encoding='utf-8')
        result = host_sync.project_index_text(vault, root=self.root)
        located = str(target.resolve().as_posix()) + '#anchor'
        self.assertIn('](<' + located + '>)', result)
        self.assertIn('[web](https://example.test/a)', result)
        self.assertEqual(host_sync.project_index_text(vault), result)

    def test_ownership_is_reused_only_inside_event(self):
        vault = self.root / 'home/.claude/projects/C--native/memory'
        vault.mkdir(parents=True)
        trace = vault.parent / 'one.jsonl'
        first = self.root / 'first-project'
        first.mkdir()
        trace.write_text(json.dumps({'cwd': str(first)}), encoding='utf-8')
        with patch.object(capture_route, '_project_roots_of',
                          wraps=capture_route._project_roots_of) as scan:
            with capture_route.observation_window():
                for _ in range(20):
                    self.assertTrue(capture_route.reference_owner_matches(vault, first))
                self.assertEqual(scan.call_count, 1)
            (vault.parent / 'two.jsonl').write_text(
                json.dumps({'cwd': str(self.root / 'second-project')}), encoding='utf-8')
            with capture_route.observation_window():
                self.assertFalse(capture_route.reference_owner_matches(vault, first))
            self.assertEqual(scan.call_count, 2)

    def test_strict_index_checks_decoded_paths_and_keeps_real_public_card(self):
        vault = self.vaults[0]
        for name in ('_private', '.private', 'public'):
            self.card(vault, name)
            path = vault / (name + '.md')
            path.write_text(path.read_text(encoding='utf-8').replace(
                '---\nname:', '---\nscope: global\nname:', 1), encoding='utf-8')
        (vault / 'MEMORY.md').write_text(
            '# index\n[a](<_private.md>)\n[b](%5Fprivate.md)\n[c](%2Eprivate.md)\n'
            '[p](public.md)\n', encoding='utf-8')
        with patch.object(capture_route, 'reference_owner_matches', return_value=False):
            rendered = host_sync.project_index_text(vault, root=self.root)
        self.assertNotIn('private', rendered)
        self.assertIn((vault / 'public.md').resolve().as_posix(), rendered)

    def test_index_rewrite_preserves_titles_inline_code_and_fences(self):
        vault = self.vaults[0]
        text = ('[t](own%20card.md "a title")\n'
                '`[code](own%20card.md)` and [live](own%20card.md)\n'
                '```md\n[example](own%20card.md)\n```\n'
                '``[double](own%20card.md)``\n'
                '```[triple](own%20card.md)``` [outside](own%20card.md)\n'
                '[paren](own(card).md)\n')
        rendered = host_sync._absolute_index_links(text, vault)
        located = (vault / 'own card.md').resolve().as_posix()
        self.assertIn(f'[t](<{located}> "a title")', rendered)
        self.assertIn(f'`[code](own%20card.md)` and [live](<{located}>)', rendered)
        self.assertIn('```md\n[example](own%20card.md)\n```', rendered)
        self.assertIn('``[double](own%20card.md)``', rendered)
        self.assertIn(f'```[triple](own%20card.md)``` [outside](<{located}>)', rendered)
        self.assertIn(f'[paren](<{(vault / "own(card).md").resolve().as_posix()}>)', rendered)

    def test_timestamp_expiry_compares_the_instant_including_timezone(self):
        now = datetime(2026, 11, 10, 1, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(memspec.card_expired('2026-11-09T23:59:59-12:00', now))
        self.assertTrue(memspec.card_expired('2026-11-10T00:00:00Z', now))
        self.assertTrue(memspec.card_expired('2026-11-10T01:00:00+00:00', now))
        self.assertFalse(memspec.card_expired('2026-11-10', now))
        self.assertTrue(memspec.card_expired('2026-11-09', now))
        self.assertFalse(memspec.card_expired('malformed timestamp', now))
        for suffix in (' owner note', ' (one-time)', 'T25:00'):
            self.assertTrue(memspec.card_expired('2025-01-01'+suffix, now))

    def test_pending_notes_use_one_lock_and_lock_failure_is_visible(self):
        config = common.load_config(time.monotonic())
        with patch.object(memspec, 'file_lock', return_value=contextlib.nullcontext(False)), \
             patch.object(sys, 'stderr', io.StringIO()) as errors:
            common.leave_note(config, 'note-lock', 'Keep this note')
        self.assertIn('not stored', errors.getvalue())
        common.leave_note(config, 'note-lock', 'Keep this note')
        with patch.object(memspec, 'file_lock', return_value=contextlib.nullcontext(False)), \
             patch.object(sys, 'stderr', io.StringIO()) as errors:
            self.assertEqual(common.peek_notes(config, 'note-lock'), [])
            common.consume_notes(config, 'note-lock', ['Keep this note'])
        self.assertIn('read lock unavailable', errors.getvalue())
        self.assertIn('acknowledgement lock unavailable', errors.getvalue())
        self.assertEqual(common.peek_notes(config, 'note-lock'), ['Keep this note'])
        common.consume_notes(config, 'note-lock', ['Keep this note'])
        self.assertEqual(common.peek_notes(config, 'note-lock'), [])

    def test_notes_stay_pending_when_budget_or_output_fails(self):
        config = common.load_config(time.monotonic())
        config[memspec.CONFIG_BUDGET_BYTES_FIELD] = 180
        event = {'session_id': 'notes', 'transcript_path': str(self.root / '.claude/projects/fixture/notes.jsonl')}
        context_id = hook_context.identity(event)
        common.leave_note(config, context_id, '重要提醒' * 30)
        base = common.payload('UserPromptSubmit', '既有內容' * 8)
        self.assertEqual(recall._with_notes(base, config, event, []), base)
        self.assertTrue(common.peek_notes(config, context_id))
        config[memspec.CONFIG_BUDGET_BYTES_FIELD] = 3000
        pending = []
        delivered = recall._with_notes(base, config, event, pending)
        context = delivered['hookSpecificOutput']['additionalContext']
        self.assertTrue(common.payload_fits('UserPromptSubmit', context, 3000))
        self.assertEqual(len(pending), 1)
        self.assertTrue(common.peek_notes(config, context_id))

        with patch.object(recall, '_recall', return_value=base), \
             patch.object(recall, 'emit', side_effect=OSError('broken output')):
            self.assertEqual(self.invoke('notes', 'noun'), '')
        self.assertTrue(common.peek_notes(config, context_id))
        with patch.object(recall, '_recall', return_value=base):
            self.assertIn('重要提醒', self.invoke('notes', 'noun'))
        self.assertEqual(common.peek_notes(config, context_id), [])


if __name__ == '__main__':
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
