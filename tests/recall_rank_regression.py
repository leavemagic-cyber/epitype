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
from unittest.mock import Mock, patch

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

    def automatic(self, prompt, **kwargs):
        memsearch.build_index(self.vault)
        return memsearch.recall_index(self.vault, prompt, automatic=True, **kwargs)

    def test_automatic_recall_rejects_body_and_description_fragments(self):
        self.card('emergency', '確認方式狀況', '修改先看目標 codex',
                  'aliases: [電腦卡死, 只重開 codex app]\n')
        for prompt in ('你先看核心目標，確定修改正確？',
                       '你確定這樣是最好的方式？'):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.automatic(prompt)['results'], [])
        self.assertTrue(self.recall('修改先看目標')['results'])

    def test_automatic_recall_accepts_emergency_paraphrase_and_short_identifier(self):
        self.card('emergency', body='Keep the system and restart entry alive.',
                  extra='aliases: [電腦卡死, 快撐不住]\n')
        self.card('bug-policy', body='Request useful evidence.', extra='aliases: [bug]\n')
        for prompt, expected in (('電腦卡死了，程式全部關掉', 'emergency'),
                                 ('我的電腦真的快撐不住了', 'emergency'),
                                 ('這個 bug 需要截圖嗎？', 'bug-policy')):
            with self.subTest(prompt=prompt):
                self.assertEqual([r['name'] for r in self.automatic(prompt)['results']], [expected])

    def test_automatic_uses_phrase_coverage_but_manual_keeps_fuzzy_search(self):
        self.card('response', extra='aliases: [核對實際送達內容, galaxyshortcut]\n')
        self.assertEqual(self.automatic('核對實際的送達內容')['results'][0]['name'], 'response')
        self.assertEqual(self.automatic('galaxyshortcu')['results'], [])
        self.assertEqual(self.recall('galaxyshortcu')['results'][0]['name'], 'response')

    def test_automatic_does_not_discover_a_card_from_backup_quote_alone(self):
        prompt = '原本說過這個完整指示'
        self.card('summary', extra=f'owner_quote: {prompt}\n')
        self.assertEqual(self.automatic(prompt)['results'], [])
        self.assertEqual(self.recall(prompt)['results'][0]['name'], 'summary')

    def test_automatic_graph_expands_only_admitted_topic_seeds_one_hop(self):
        self.card('entry', body='[[neighbour]]', extra='aliases: [程序終止]\n')
        self.card('neighbour', body='[[too-far]]')
        self.card('too-far')
        self.card('unrelated', body='確認修改 [[irrelevant]]')
        self.card('irrelevant')
        names = {r['name'] for r in self.automatic('程序終止前先確認修改')['results']}
        self.assertEqual(names, {'entry', 'neighbour'})
        denied = self.automatic('程序終止', seed_filter=lambda hit: False)
        self.assertEqual({r['name'] for r in denied['results']}, {'entry'})

    def test_automatic_busy_legacy_schema_still_rejects_body_only_hits(self):
        self.card('body-only', body='compatibilityneedle')
        self.card('named', extra='aliases: [compatibilityneedle]\n')
        memsearch.build_index(self.vault)
        with contextlib.closing(sqlite3.connect(memsearch._db_path(self.vault))) as database:
            with database:
                database.execute('ALTER TABLE cards DROP COLUMN recall_links')
                database.execute("UPDATE search_meta SET value = '8' WHERE key = 'fts_format'")
        with memspec.file_lock(memsearch._db_path(self.vault), 0) as acquired:
            self.assertTrue(acquired)
            result = memsearch.recall_index(self.vault, 'compatibilityneedle', automatic=True)
        self.assertEqual({r['name'] for r in result['results']}, {'named'})

    def test_automatic_keeps_clear_body_topics_and_unique_identifier_lookup(self):
        self.card('writes', 'Repeated writes', 'retry duplicated rows in the table')
        self.card('lookup', body='ordinarynostatusneedle identifies the current artifact.')
        self.assertIn('writes', [r['name'] for r in self.automatic('the retry duplicated rows in the table')['results']])
        self.assertEqual([r['name'] for r in self.automatic('ordinarynostatusneedle')['results']], ['lookup'])
        self.card('second', body='ordinarynostatusneedle has another unrelated meaning.')
        self.assertEqual(self.automatic('ordinarynostatusneedle')['results'], [])

    def test_four_clear_chinese_paraphrases_are_not_lost_to_alias_coverage(self):
        cases = (
            ('measure', '改前改後都要量', '改之前改之後都要量嗎'),
            ('session', '同一件事不要兩邊問', '同一件事我在兩個地方問了'),
            ('optimise', '先量測再優化', '我改了快取的失效策略，改完要不要先量一次'),
            ('alert', '告警必須帶下一步動作', '告警的通道路由我改好了，內容還要寫什麼'),
        )
        for name, alias, prompt in cases:
            self.card(name, extra=f'aliases: [{alias}]\n')
            self.assertIn(name, [r['name'] for r in self.automatic(prompt)['results']])

    def test_explicit_topic_lookup_keeps_description_matches_without_new_aliases(self):
        self.card('first', 'artifactneedle describes the artifact')
        self.card('second', 'artifactneedle is another applicable reference')
        for prompt in ('artifactneedle', 'find artifactneedle', 'load artifactneedle',
                       'search for artifactneedle?', 'artifactneedle 是什麼？'):
            with self.subTest(prompt=prompt):
                self.assertEqual({r['name'] for r in self.automatic(prompt)['results']},
                                 {'first', 'second'})
        self.assertEqual(self.automatic('discuss artifactneedle with the reviewer')['results'], [])

    def test_automatic_identifier_counts_final_current_successors(self):
        self.card('old', body='legacyneedle', extra='status: superseded\nsuperseded_by: middle.md\n')
        self.card('middle', extra='status: superseded\nsuperseded_by: current.md\n')
        self.card('current', body='The present instruction.')
        found = self.automatic('legacyneedle')
        self.assertEqual([r['card_path'] for r in found['replacement_results']], ['current.md'])
        self.card('current', body='legacyneedle is now governed by this instruction.')
        found = self.automatic('legacyneedle')
        self.assertEqual({r['card_path'] for r in found['results'] + found['replacement_results']}, {'current.md'})
        self.card('middle', extra='status: superseded\nsuperseded_by: old.md\n')
        self.card('current', body='A separate present instruction.')
        found = self.automatic('legacyneedle')
        self.assertEqual(found['results'] + found['replacement_results'], [])

    def test_low_frequency_description_is_evidence_but_common_words_are_not(self):
        self.card('policy', 'selectneedle current policy')
        self.assertEqual([r['name'] for r in self.automatic('selectneedle detailneedle')['results']], ['policy'])
        self.assertEqual(self.recall('selectneedle detailneedle')['results'][0]['name'], 'policy')
        for index in range(5):
            self.card(f'noise-{index}', 'selectneedle background')
        self.assertEqual(self.automatic('selectneedle detailneedle')['results'], [])

    def test_low_frequency_identity_token_is_a_hint_not_a_common_topic(self):
        self.card('emergency', '確認方式狀況', '修改先看目標 codex',
                  'aliases: [電腦卡死, 只重開 codex app]\n')
        prompt = '你跟 codex 討論，確認目前所有狀況'
        self.assertEqual([r['name'] for r in self.automatic(prompt)['results']], ['emergency'])
        for index in range(5):
            self.card(f'noise-{index}', 'codex background')
        self.assertEqual(self.automatic(prompt)['results'], [])

    def test_lexical_units_preserve_independent_tokens_and_merge_overlaps(self):
        self.assertEqual(memsearch._recall_units('cacheable cache ' * 5000,
                                               {'cacheable', 'cache'}), {'cacheable', 'cache'})
        self.assertEqual(memsearch._recall_units('克勞德 Claude', {'克勞', '勞德', 'claude'}),
                         {'克勞德', 'claude'})
        self.assertEqual(memsearch._recall_units('search for artifactneedle?', {'artifactneedle'}),
                         {'artifactneedle'})

    def test_same_front_field_evidence_is_not_specific_to_one_script(self):
        for name, token in (('en', 'rollback'), ('zh', '回滾'), ('pt', 'reversão')):
            self.card(name, token)
            self.assertIn(name, [r['name'] for r in self.automatic(token + ' extraidentifier')['results']])

    def test_automatic_latin_matches_start_at_a_word_boundary(self):
        self.card('restart', 'workflow restart handling')
        self.assertEqual(self.automatic('star extraidentifier')['results'], [])
        self.assertEqual(self.recall('star workflow')['results'][0]['name'], 'restart')
        self.card('star', 'workflow for adding a star')
        self.assertEqual([r['name'] for r in self.automatic('star extraidentifier')['results']], ['star'])

    def test_exact_source_quote_finds_the_current_summary(self):
        prompt = 'captureartifactneedle verifyeffectneedle'
        for number in range(10):
            self.card(f'noise-{number}', prompt + ' background detail')
        self.card('answer', 'Present requirement', extra=f'owner_quote: "{prompt}"\n')
        result = self.recall(prompt)
        self.assertEqual(result['results'][0]['name'], 'answer')
        self.assertIn('owner_quote', result['results'][0]['hit_fields'])
        self.assertNotIn('answer', [row['name'] for row in memsearch.query_index(self.vault, prompt)['results']])

    def test_each_complete_source_quotation_matches_with_unicode_normalization(self):
        self.card('answer', 'Present requirement', extra=(
            'owner_quote: 「核對全形：ＡＢＣＤ，再核對字詞」；「核對另一段：ＷＸＹＺ，再核對字詞」\n'))
        for prompt in ('核對全形:abcd,再核對字詞', '核對另一段:wxyz,再核對字詞'):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.recall(prompt)['results'][0]['name'], 'answer')

    def test_source_quote_fragments_and_generic_approvals_do_not_add_candidates(self):
        self.card('answer', 'Present requirement', extra=(
            'owner_quote: 「archive snapshotneedle preservechecksumneedle in sequence」；「Okay」；「可以」\n'))
        for prompt in ('archive snapshotneedle', 'snapshotneedle preservechecksumneedle', 'okay', '可以'):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.recall(prompt)['results'], [])

    def test_source_quote_edit_replaces_its_discovery_signal(self):
        self.card('answer', 'Present requirement', extra='owner_quote: "firsttopicneedle firstactionneedle"\n')
        self.assertEqual(self.recall('firsttopicneedle firstactionneedle')['results'][0]['name'], 'answer')
        self.card('answer', 'Present requirement', extra='owner_quote: "othertopicneedle otheractionneedle"\n')
        self.assertEqual(self.recall('othertopicneedle otheractionneedle')['results'][0]['name'], 'answer')
        self.assertEqual(memsearch.recall_index(self.vault, 'firsttopicneedle firstactionneedle')['results'], [])

    def test_quotation_inside_a_different_prompt_is_not_an_exact_match(self):
        phrase = 'originaltopicneedle originalactionneedle'
        self.card('answer', 'Present requirement', extra=f'owner_quote: "{phrase}"\n')
        self.assertEqual(self.recall(f'Compare 「{phrase}」 with a different request')['results'], [])

    def test_block_source_quote_keeps_complete_whitespace_normalized_phrase(self):
        self.card('answer', 'Present requirement', extra=(
            'owner_quote: >\n  originaltopicneedle\n  originalactionneedle.\n'))
        self.assertEqual(self.recall('ORIGINALTOPICNEEDLE   originalactionneedle!')['results'][0]['name'], 'answer')

    def test_superseded_source_quote_points_to_its_current_successor(self):
        prompt = 'originaltopicneedle originalactionneedle'
        self.card('old', 'Previous requirement', extra=(
            f'owner_quote: "{prompt}"\nstatus: superseded\nsuperseded_by: current.md\n'))
        self.card('current', 'Present requirement')
        result = self.recall(prompt)
        self.assertNotIn('old', [row['name'] for row in result['results']])
        self.assertEqual(result['replacement_results'][0]['name'], 'current')
        included = memsearch.recall_index(self.vault, prompt, include_superseded=True)
        self.assertIn('old', [row['name'] for row in included['results']])

    def test_source_quotes_do_not_reactivate_historical_or_noncard_records(self):
        prompt = 'recordtopicneedle recordactionneedle'
        self.card('archived', 'Previous requirement', extra=(
            f'owner_quote: "{prompt}"\nscope: historical-audit\n'))
        (self.vault / 'raw.md').write_text(f'---\nowner_quote: "{prompt}"\n---\nUnlabelled record.\n', encoding='utf-8')
        result = self.recall(prompt)
        self.assertEqual(result['results'], [])
        self.assertEqual(result['replacement_results'], [])

    def test_busy_upgrade_reads_format_nine_without_quote_metadata(self):
        self.card('answer', 'compatibilityneedle')
        memsearch.build_index(self.vault)
        with contextlib.closing(sqlite3.connect(memsearch._db_path(self.vault))) as database:
            with database:
                database.execute('ALTER TABLE cards DROP COLUMN recall_quotes')
                database.execute("UPDATE search_meta SET value = '9' WHERE key = 'fts_format'")
        with memspec.file_lock(memsearch._db_path(self.vault), 0) as acquired:
            self.assertTrue(acquired)
            result = memsearch.recall_index(self.vault, 'compatibilityneedle')
        self.assertEqual(result['results'][0]['name'], 'answer')
        self.assertNotIn('index_updated', result)

    def test_format_nine_upgrade_rebuilds_quote_metadata_from_cards(self):
        prompt = 'originaltopicneedle originalactionneedle'
        self.card('answer', 'Present requirement', extra=f'owner_quote: "{prompt}"\n')
        memsearch.build_index(self.vault)
        with contextlib.closing(sqlite3.connect(memsearch._db_path(self.vault))) as database:
            with database:
                database.execute('ALTER TABLE cards DROP COLUMN recall_quotes')
                database.execute("UPDATE search_meta SET value = '9' WHERE key = 'fts_format'")
        result = memsearch.recall_index(self.vault, prompt)
        self.assertTrue(result['index_updated'])
        self.assertEqual(result['results'][0]['name'], 'answer')

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

    def test_rejected_seed_has_no_forward_or_reverse_expansion(self):
        self.card('seed', 'admissiongraphneedle', '[[outgoing]]')
        self.card('outgoing', 'unrelated terminology')
        self.card('incoming', 'unrelated terminology', '[[seed]]')
        memsearch.build_index(self.vault)
        approve = Mock(return_value=False)
        filtered = memsearch.recall_index(self.vault, 'admissiongraphneedle', seed_filter=approve)
        self.assertEqual({row['name'] for row in filtered['results']}, {'seed'})
        self.assertEqual(approve.call_count, 1)
        unfiltered = memsearch.recall_index(self.vault, 'admissiongraphneedle')
        self.assertEqual({row['name'] for row in unfiltered['results']}, {'seed', 'incoming', 'outgoing'})

    def test_an_admitted_seed_still_supports_a_shared_neighbour(self):
        self.card('allowed', 'sharedgraphneedle', '[[shared]]')
        self.card('rejected', 'sharedgraphneedle', '[[shared]] [[unsupported]]')
        self.card('shared', 'unrelated terminology')
        self.card('unsupported', 'unrelated terminology')
        memsearch.build_index(self.vault)
        result = memsearch.recall_index(self.vault, 'sharedgraphneedle',
                                       seed_filter=lambda hit: hit['name'] == 'allowed')
        names = {row['name'] for row in result['results']}
        self.assertIn('shared', names)
        self.assertNotIn('unsupported', names)

    def test_seed_filter_does_not_remove_independent_alias_or_quote_matches(self):
        self.card('seed', 'independenttopic independentaction', '[[target]] [[alias-target]] [[quote-target]]')
        self.card('target', 'independenttopic independentaction')
        self.card('alias-target', 'different wording', extra='aliases: [independenttopic]\n')
        self.card('quote-target', 'different wording', extra='owner_quote: "independenttopic independentaction"\n')
        memsearch.build_index(self.vault)
        result = memsearch.recall_index(self.vault, 'independenttopic independentaction',
                                       seed_filter=lambda hit: False, limit=10)
        self.assertEqual({row['name'] for row in result['results']},
                         {'seed', 'target', 'alias-target', 'quote-target'})

    def test_seed_admission_is_lazy_and_does_not_widen_the_original_three_seeds(self):
        for number in range(6):
            self.card(f'seed-{number}', 'boundedgraphneedle', '[[outside-window]]' if number >= 3 else '')
        self.card('outside-window', 'unrelated terminology')
        memsearch.build_index(self.vault)
        approve = Mock(return_value=True)
        result = memsearch.recall_index(self.vault, 'boundedgraphneedle', seed_filter=approve, limit=20)
        self.assertEqual(approve.call_count, 0)
        self.assertNotIn('outside-window', {row['name'] for row in result['results']})

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
                             'scope': copy.deepcopy(hook._DEFAULT_RECALL_SCOPE), 'quote': 'Original owner words',
                             'text': 'Entire retained rule text'},
                'provenance': {'status': rule_provenance.VERIFIED if verified else rule_provenance.UNVERIFIED,
                               'source_unavailable': not verified},
                'text': 'Entire retained rule text', 'supersedes': [], 'expired': False, 'card_path': 'source.md',
                'effective_version': 'synthetic-version'}

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
        projected['document']['supersedes'] = ['old-key']
        compact = hook._recall_projection(projected)
        for expected in ('AI 整理', 'codex', '非 Owner 逐字裁定', 'Original owner words', 'example', projected['text']):
            self.assertIn(expected, compact)
        self.assertNotIn('old-key', compact)
        self.assertIn('old-key', rule_pipeline.render_projection(projected))

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
                    f'---\nname: {name}\ndescription: textneedle\naliases: [textneedle]\nrule_provenance: synthetic\n---\n', encoding='utf-8')
            memsearch.build_index(vault)
            config = {memspec.CONFIG_BUDGET_BYTES_FIELD: memspec.HOOK_DEFAULT_BUDGET_BYTES,
                      memspec.CONFIG_PROACTIVE_RECALL_FIELD: True}
            def projected(_fields, path, **_options):
                value = self.projection('ai-derived' if path.stem == 'ai-card' else 'owner-directive')
                value['text'] = f'Full body for {path.stem}'
                value['effective_version'] = 'fixture-version'
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
            config = {memspec.CONFIG_BUDGET_BYTES_FIELD: memspec.HOOK_DEFAULT_BUDGET_BYTES,
                      memspec.CONFIG_PROACTIVE_RECALL_FIELD: True}
            with patch.object(hook, 'scoped_vaults', return_value=[vault]), \
                 patch.object(hook, '_capture_vault', return_value=None), \
                 patch.object(hook, 'recall_marker_directory', lambda session: vault / 'markers' / session), \
                 patch.object(rule_pipeline, 'legacy_integrity', wraps=rule_pipeline.legacy_integrity) as verify:
                result = hook._recall({'prompt': 'slotneedle'}, time.monotonic(), config)
            self.assertEqual(verify.call_count, memspec.FTS_TOP_K)
            self.assertEqual(sum(line.startswith('- ') for line in result['hookSpecificOutput']['additionalContext'].splitlines()), memspec.FTS_TOP_K)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
