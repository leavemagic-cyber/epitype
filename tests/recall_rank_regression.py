import sys; sys.dont_write_bytecode = True
"""Ordinal fusion, one-hop boundaries and compact source labels on temporary cards."""

import copy
import contextlib
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'adapters' / 'claude')]
from epitype import memsearch, memspec, rule_pipeline, rule_provenance
import recall_hook as hook
import recall_benchmark as benchmark


class RecallRankRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='epitype-rank-')
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name).resolve()

    def card(self, name, description='', body='', extra=''):
        path = self.vault / f'{name}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nname: {name}\ndescription: {description}\n{extra}---\n{body}', encoding='utf-8')

    def recall(self, prompt):
        memsearch.build_index(self.vault)
        return memsearch.recall_index(self.vault, prompt)

    def test_exact_alias_outvotes_body_mentions(self):
        for index in range(12):
            self.card(f'noise-{index}', 'ranking measurement', 'galaxyshortcut ' * 20)
        self.card('answer', 'specific preference', extra='aliases: [galaxyshortcut]\n')
        self.assertEqual(self.recall('galaxyshortcut')['results'][0]['name'], 'answer')

    def test_single_alias_term_does_not_fill_a_multi_topic_query_window(self):
        self.card('answer', 'alphaquery betaquery gammaquery deltaquery')
        for number in range(20):
            self.card(f'noise-{number}', 'alphaquery', extra='aliases: [alphaquery]\n')
        memsearch.build_index(self.vault)
        names = [row['name'] for row in memsearch.recall_index(
            self.vault, 'alphaquery betaquery gammaquery deltaquery', limit=8)['results']]
        self.assertIn('answer', names[:8])

    def test_near_alias_discovers_card_without_full_query_match(self):
        self.card('answer', 'specific preference', extra='aliases: [antigravity]\n')
        self.assertEqual(self.recall('antigravty')['results'][0]['name'], 'answer')

    def test_short_latin_prefix_does_not_bypass_qualification(self):
        self.card('tiered', extra='aliases: [tier]\n')
        self.assertEqual(self.recall('tie')['results'], [])

    def test_short_alias_does_not_match_a_longer_prompt_word(self):
        self.card('unrelated', extra='aliases: [tie]\n')
        self.assertEqual(self.recall('tiered')['results'], [])

    def test_alias_cache_changes_with_the_indexed_alias(self):
        self.card('answer', extra='aliases: [antigravity]\n')
        self.assertEqual(self.recall('antigravty')['results'][0]['name'], 'answer')
        self.card('answer', extra='aliases: [differenttopic]\n')
        self.assertEqual(self.recall('antigravty')['results'], [])

    def test_busy_upgrade_reads_the_previous_schema_with_legacy_ranking(self):
        self.card('answer', 'upgradefixture')
        memsearch.build_index(self.vault)
        with contextlib.closing(sqlite3.connect(memsearch._db_path(self.vault))) as database:
            with database:
                database.execute('ALTER TABLE cards DROP COLUMN recall_date')
                database.execute('ALTER TABLE cards DROP COLUMN recall_links')
                database.execute("UPDATE search_meta SET value = '8' WHERE key = 'fts_format'")
        with memspec.file_lock(memsearch._db_path(self.vault), 0) as acquired:
            self.assertTrue(acquired)
            result = memsearch.recall_index(self.vault, 'upgradefixture')
        self.assertEqual(result['results'][0]['name'], 'answer')
        self.assertNotIn('index_updated', result)

    def test_links_discover_one_hop_only(self):
        self.card('seed', 'graphneedle', '[[neighbour]]')
        self.card('neighbour', 'unrelated terminology', '[[two-hops-away]]')
        self.card('two-hops-away', 'unrelated terminology')
        names = {row['name'] for row in self.recall('graphneedle')['results']}
        self.assertIn('neighbour', names)
        self.assertNotIn('two-hops-away', names)

    def test_reverse_links_are_neighbours(self):
        self.card('seed', 'graphneedle')
        self.card('neighbour', 'unrelated terminology', '[[seed|label]]')
        self.assertIn('neighbour', {row['name'] for row in self.recall('graphneedle')['results']})

    def test_relative_links_prefer_the_local_file_over_an_ambiguous_stem(self):
        self.card('folder/seed', 'graphneedle', '[[./target.MD|label]]')
        self.card('folder/target', 'unrelated terminology')
        self.card('elsewhere/target', 'unrelated terminology')
        paths = {row['card_path'] for row in self.recall('graphneedle')['results']}
        self.assertIn('folder/target.md', paths)
        self.assertNotIn('elsewhere/target.md', paths)

    def test_dates_only_order_relevant_candidates(self):
        self.card('a-old', 'freshneedle', extra='last_verified_at: 2020-01-01\n')
        self.card('z-new', 'freshneedle', extra='current_decision_at: 2026-10-29\n')
        self.card('unrelated-20261030', 'no relation')
        names = [row['name'] for row in self.recall('freshneedle')['results']]
        self.assertEqual(names[0], 'z-new')
        self.assertNotIn('unrelated-20261030', names)

    def test_date_in_filename_and_invalid_metadata(self):
        self.assertEqual(memsearch._recall_date({'last_verified_at': '2026-02-31'}, 'a-20261029.md'), '2026-10-29')
        self.assertEqual(memsearch._recall_date({'current_decision_at': '2026-10-30'}, 'a-20261029.md'), '2026-10-30')

    def test_retired_and_historical_neighbours_do_not_leak(self):
        self.card('seed', 'graphneedle', '[[old]] [[archived]]')
        self.card('old', 'graphneedle', extra='status: superseded\nsuperseded_by: current.md\n')
        self.card('current', 'new terminology')
        self.card('archived', 'historical audit copy retained', extra='scope: historical-audit\n')
        result = self.recall('graphneedle')
        names = {row['name'] for row in result['results']}
        self.assertNotIn('old', names)
        self.assertNotIn('archived', names)
        self.assertEqual(result['replacement_results'][0]['name'], 'current')

    def test_generic_prompt_does_not_expand_graph(self):
        self.card('seed', 'what is the', '[[neighbour]]')
        self.card('neighbour', 'something else')
        self.assertEqual(self.recall('what is the')['results'], [])


class RecallTextRegression(unittest.TestCase):
    def test_benchmark_does_not_count_body_mentions_or_pointers_as_card_delivery(self):
        vault = (ROOT / 'synthetic-vault').resolve()
        head = f'vaults: V1={vault}\n'
        self.assertIsNone(benchmark.delivered_kind(
            head + '- other | see V1/expected.md | V1/other.md', vault, ['expected.md']))
        self.assertEqual(benchmark.delivered_kind(
            head + '- V1/expected.md', vault, ['expected.md']), 'pointer')
        self.assertEqual(benchmark.delivered_kind(
            head + '- title | actual summary | V1/expected.md', vault, ['expected.md']), 'card')
        relative_head = f'vaults: V1={os.path.relpath(vault)}\n'
        self.assertEqual(benchmark.delivered_kind(
            relative_head + '- title | actual summary | V1/expected.md', vault, ['expected.md']), 'card')

    def test_nested_source_checks_share_and_restore_the_hook_deadline(self):
        with patch.object(rule_provenance.time, 'monotonic', return_value=100):
            self.assertEqual(rule_provenance.verification_deadline(50), 150)
            with rule_provenance.verification_window(105):
                self.assertEqual(rule_provenance.verification_deadline(50), 105)
                with rule_provenance.verification_window(102):
                    self.assertEqual(rule_provenance.verification_deadline(50), 102)
                self.assertEqual(rule_provenance.verification_deadline(50), 105)
                with rule_provenance.verification_window(99):
                    budget = rule_provenance._ReadBudget(rule_provenance.verification_deadline(50), 1024)
                    with self.assertRaises(OSError):
                        budget.check()
            self.assertEqual(rule_provenance.verification_deadline(50), 150)

    def test_hook_leaves_time_for_search_rendering_and_process_exit(self):
        with patch.object(rule_provenance.time, 'monotonic', return_value=100), \
             patch.object(hook, '_recall_cards') as cards:
            cards.side_effect = lambda *args: rule_provenance.verification_deadline(50)
            deadline = hook._recall({'prompt': 'source check'}, 100, {})
        self.assertEqual(deadline, 106)
        self.assertLess(deadline, 100 + memspec.HOOK_TIMEOUT_SECONDS)

    def projection(self, kind='owner-directive', verified=True):
        return {'document': {'key': 'source-key', 'kind': kind, 'author': 'owner' if kind == 'owner-directive' else 'codex',
                             'scope': copy.deepcopy(hook._DEFAULT_RECALL_SCOPE), 'quote': 'Original owner words'},
                'provenance': {'status': rule_provenance.VERIFIED if verified else rule_provenance.UNVERIFIED,
                               'source_unavailable': not verified},
                'text': 'Entire retained rule text', 'supersedes': [], 'expired': False, 'card_path': 'source.md'}

    def test_default_scope_and_fixed_source_details_are_shared(self):
        projected = self.projection(verified=False)
        compact = hook._recall_projection(projected)
        self.assertIn('未核', compact)
        self.assertIn('Owner 原話', compact)
        self.assertIn('owner', compact)
        self.assertIn(projected['text'], compact)
        self.assertNotIn('unspecified', compact)
        self.assertLess(len(compact.encode('utf-8')), len(rule_pipeline.render_projection(projected).encode('utf-8')))

    def test_ai_wording_quote_scope_and_supersession_remain_per_item(self):
        projected = self.projection('ai-derived')
        projected['document']['scope']['level'] = 'project'
        projected['document']['scope']['project'] = 'example'
        projected['supersedes'] = ['old-key']
        compact = hook._recall_projection(projected)
        for expected in ('AI 整理', 'codex', '非 Owner 逐字裁定', 'Original owner words', 'example', 'old-key', projected['text']):
            self.assertIn(expected, compact)

    def test_synthetic_corpus_covers_four_vaults_and_dated_cases(self):
        corpus = json.loads((ROOT / 'tests/fixtures/recall_synthetic.json').read_text(encoding='utf-8'))
        questions = corpus['questions']
        self.assertGreaterEqual(len(questions), 40)
        self.assertEqual({row['vault_id'] for row in questions}, {'V1', 'V2', 'V3', 'V4'})
        self.assertEqual(len({row['id'] for row in questions}), len(questions))
        self.assertTrue(all(row['source'] and row['expect'] for row in questions))
        self.assertTrue({'dated-orchard', 'dated-pottery', 'dated-kites'} <= {row['id'] for row in questions})

    def test_injection_shares_scope_and_keeps_each_source_label_and_body(self):
        with tempfile.TemporaryDirectory(prefix='epitype-recall-text-') as directory:
            vault = Path(directory).resolve()
            for name in ('owner-card', 'ai-card'):
                (vault / f'{name}.md').write_text(
                    f'---\nname: {name}\ndescription: textneedle\nrule_provenance: synthetic\n---\n', encoding='utf-8')
            memsearch.build_index(vault)
            config = {memspec.CONFIG_BUDGET_BYTES_FIELD: memspec.HOOK_DEFAULT_BUDGET_BYTES}
            def projected(_fields, path):
                value = self.projection('ai-derived' if path.stem == 'ai-card' else 'owner-directive')
                value['text'] = f'Full body for {path.stem}'
                return value
            with patch.object(hook, 'scoped_vaults', return_value=[vault]), \
                 patch.object(hook, '_capture_vault', return_value=None), \
                 patch.object(hook, 'recall_marker_directory', lambda session: vault / 'markers' / session), \
                 patch.object(rule_pipeline, 'projection', side_effect=projected):
                result = hook._recall({'prompt': 'textneedle'}, time.monotonic(), config)
            context = result['hookSpecificOutput']['additionalContext']
            self.assertEqual(context.count('"expiry":"unspecified"'), 1)
            self.assertEqual(context.count('Owner 原話'), 1)
            self.assertEqual(context.count('非 Owner 逐字裁定'), 1)
            for name in ('owner-card', 'ai-card'):
                self.assertIn(f'Full body for {name}', context)
                self.assertIn(f'V1/{name}.md', context)

    def test_full_ordinary_slots_skip_unused_source_validation(self):
        with tempfile.TemporaryDirectory(prefix='epitype-recall-slots-') as directory:
            vault = Path(directory).resolve()
            for number in range(15):
                (vault / f'card-{number:02}.md').write_text(
                    f'---\nname: card-{number:02}\ndescription: slotneedle\n---\n', encoding='utf-8')
            memsearch.build_index(vault)
            config = {memspec.CONFIG_BUDGET_BYTES_FIELD: memspec.HOOK_DEFAULT_BUDGET_BYTES}
            with patch.object(hook, 'scoped_vaults', return_value=[vault]), \
                 patch.object(hook, '_capture_vault', return_value=None), \
                 patch.object(hook, 'recall_marker_directory', lambda session: vault / 'markers' / session), \
                 patch.object(rule_pipeline, 'legacy_integrity', wraps=rule_pipeline.legacy_integrity) as verify:
                result = hook._recall({'prompt': 'slotneedle'}, time.monotonic(), config)
            self.assertEqual(verify.call_count, memspec.FTS_TOP_K)
            self.assertEqual(sum(line.startswith('- ') for line in result['hookSpecificOutput']['additionalContext'].splitlines()), memspec.FTS_TOP_K)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
