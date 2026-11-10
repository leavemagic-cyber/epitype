"""Lossy native names locate candidates; current source ownership selects content."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json, os, subprocess, tempfile, unittest
from unittest.mock import patch
from epitype import capture_route as route, host_sync, rule_context

class ProjectIdentityScope(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='epitype-identity-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root/'home'
        self.a, self.b = self.root/'命理分析', self.root/'便宜機票'
        self.a.mkdir(); self.b.mkdir()
        self.vault = self.home/'.claude/projects'/route.project_slug(self.a)/'memory'
        self.vault.mkdir(parents=True)
        for number, root in enumerate((self.a, self.b)):
            (self.vault.parent/f'{number}.jsonl').write_text(json.dumps({'cwd':str(root)})+'\n',encoding='utf-8')

    def card(self, name, **fields):
        path = self.vault/(name+'.md')
        path.write_text('---\nname: '+name+'\ndescription: relevant source\n'
            +'\n'.join(k+': '+json.dumps(v,ensure_ascii=False) for k,v in fields.items())
            +'\n---\nbody\n',encoding='utf-8')
        return path

    def test_lossy_slug_does_not_collapse_logical_identity(self):
        self.assertEqual(route.project_slug(self.a),route.project_slug(self.b))
        self.assertNotEqual(route.project_key(self.a),route.project_key(self.b))
        self.assertIsNone(route.project_root_of(self.vault))
        self.assertEqual(set(route.project_roots_of(self.vault)),{self.a,self.b})

    def test_unique_unicode_project_keeps_its_own_unscoped_reference(self):
        (self.vault.parent/'1.jsonl').unlink()
        card = self.card('own-reference')
        self.assertTrue(route.reference_owner_matches(self.vault, self.a))
        self.assertNotEqual(rule_context.reference_applicability({}, card, {'cwd':str(self.a)})[0], 'OUTSIDE')
        self.assertTrue(host_sync._project_card_allowed(card, self.a))
        self.assertFalse(route.reference_owner_matches(self.vault, self.b))

    def test_old_collision_outside_latest_three_is_still_observed(self):
        for number in range(2, 6):
            (self.vault.parent/f'{number}.jsonl').write_text(
                json.dumps({'cwd':str(self.a)})+'\n', encoding='utf-8')
        self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})
        self.assertFalse(route.reference_owner_matches(self.vault, self.a))

    def test_observed_collision_survives_transcript_removal(self):
        self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})
        (self.vault.parent/'1.jsonl').unlink()
        self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})
        self.assertFalse(route.reference_owner_matches(self.vault, self.a))
        card = self.card('explicit', project=str(self.a))
        self.assertEqual(rule_context.reference_applicability(
            {'project':str(self.a)}, card, {'cwd':str(self.a)})[0], 'APPLICABLE')

    def test_missing_or_corrupt_cache_rebuilds_from_all_available_headers(self):
        for number in range(2, 6):
            (self.vault.parent/f'{number}.jsonl').write_text(
                json.dumps({'cwd':str(self.a)})+'\n', encoding='utf-8')
        self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})
        cache = self.vault/'.epitype/project-roots.json'
        for value in (None, '{broken', '{"version":1,"roots":["relative"]}'):
            if value is None:
                cache.unlink()
            else:
                cache.write_text(value, encoding='utf-8')
            self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})

    def test_unchanged_headers_are_not_read_again(self):
        route.project_roots_of(self.vault)
        with patch.object(route, '_transcript_root', wraps=route._transcript_root) as read:
            self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})
            read.assert_not_called()

    def test_changed_transcript_adds_root_without_erasing_prior_observation(self):
        (self.vault.parent/'1.jsonl').unlink()
        self.assertEqual(route.project_roots_of(self.vault), [self.a])
        trace = self.vault.parent/'0.jsonl'
        stamp = trace.stat()
        trace.write_text(json.dumps({'cwd':str(self.b)})+'\n', encoding='utf-8')
        os.utime(trace, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
        self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})

    def test_failed_cache_write_does_not_hide_an_observed_collision(self):
        with patch.object(route.os, 'replace', side_effect=PermissionError('fixture')):
            self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})
        self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})

    def test_observation_does_not_create_a_missing_vault(self):
        absent = self.root/'missing'/'memory'
        self.assertEqual(route.project_roots_of(absent), [])
        self.assertFalse(absent.exists())

    def test_bad_manifest_entry_retains_roots_after_original_trace_is_removed(self):
        route.project_roots_of(self.vault)
        (self.vault.parent/'1.jsonl').unlink()
        cache = self.vault/'.epitype/project-roots.json'
        data = json.loads(cache.read_text(encoding='utf-8'))
        data['files']['0.jsonl'][0] = -1
        data['roots'].append('invalid-relative-root')
        cache.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})
        self.assertFalse(route.reference_owner_matches(self.vault, self.a))

    def test_newer_cache_retains_collision_and_is_not_downgraded(self):
        route.project_roots_of(self.vault)
        (self.vault.parent/'1.jsonl').unlink()
        cache = self.vault/'.epitype/project-roots.json'
        data = json.loads(cache.read_text(encoding='utf-8'))
        data['version'] = 2
        data['future-data'] = {'preserve': True}
        before = json.dumps(data).encode('utf-8')
        cache.write_bytes(before)
        self.assertEqual(set(route.project_roots_of(self.vault)), {self.a, self.b})
        self.assertEqual(cache.read_bytes(), before)
        self.assertFalse(route.reference_owner_matches(self.vault, self.a))

    def test_current_session_append_keeps_unicode_owner(self):
        (self.vault.parent/'1.jsonl').unlink()
        route.project_roots_of(self.vault)
        trace = self.vault.parent/'0.jsonl'
        with trace.open('a', encoding='utf-8') as stream:
            stream.write('{}\n')
        original = route._transcript_root
        def growing(path, rows):
            root = original(path, rows)
            with path.open('a', encoding='utf-8') as stream:
                stream.write('{}\n')
            return root
        with patch.object(route, '_transcript_root', growing):
            self.assertEqual(route.project_roots_of(self.vault), [self.a])
            self.assertTrue(route.reference_owner_matches(self.vault, self.a))

    def test_temporarily_unreadable_known_header_retains_its_observation(self):
        (self.vault.parent/'1.jsonl').unlink()
        route.project_roots_of(self.vault)
        with (self.vault.parent/'0.jsonl').open('a', encoding='utf-8') as stream:
            stream.write('{}\n')
        with patch.object(route, '_transcript_root', side_effect=PermissionError('fixture')):
            self.assertEqual(route.project_roots_of(self.vault), [self.a])
            self.assertTrue(route.reference_owner_matches(self.vault, self.a))

    def test_unknown_unreadable_header_never_borrows_foreign_ascii_owner(self):
        current, foreign = self.root/'alpha-one', self.root/'alpha.one'
        current.mkdir(); foreign.mkdir()
        self.assertEqual(route.project_slug(current), route.project_slug(foreign))
        vault = self.home/'.claude/projects'/route.project_slug(current)/'memory'
        vault.mkdir(parents=True)
        card = vault/'unknown.md'
        card.write_text('---\nname: unknown\ndescription: foreign precedent\n---\n', encoding='utf-8')
        (vault.parent/'known.jsonl').write_text(json.dumps({'cwd':str(foreign)})+'\n', encoding='utf-8')
        route.project_roots_of(vault)
        (vault.parent/'new.jsonl').write_text('{}\n', encoding='utf-8')
        original = route._transcript_root
        def unavailable(path, rows):
            if path.name == 'new.jsonl':
                raise PermissionError('fixture')
            return original(path, rows)
        with patch.object(route, '_transcript_root', unavailable):
            self.assertFalse(route.reference_owner_matches(vault, current))
            self.assertFalse(host_sync._project_card_allowed(card, current))

    @unittest.skipUnless(os.name == 'nt', 'Windows path identity is case-insensitive')
    def test_nonexistent_root_case_alias_does_not_invalidate_warm_cache(self):
        for trace in self.vault.parent.glob('*.jsonl'):
            trace.unlink()
        missing = self.root/'Gone'
        for number, value in enumerate((str(missing), str(missing).upper())):
            (self.vault.parent/f'{number}.jsonl').write_text(json.dumps({'cwd':value})+'\n', encoding='utf-8')
        self.assertEqual(len(route.project_roots_of(self.vault)), 1)
        with patch.object(route, '_transcript_root', wraps=route._transcript_root) as read:
            self.assertEqual(len(route.project_roots_of(self.vault)), 1)
            read.assert_not_called()

    def test_unknown_candidate_is_lookup_only_not_automatic_project_content(self):
        card=self.card('unknown')
        self.assertEqual(rule_context.reference_applicability({},card,{'cwd':str(self.a)})[0],'OUTSIDE')
        self.assertTrue(card.is_file())

    def test_index_filters_foreign_unknown_and_unfiltered_expansion(self):
        self.card('own',project=str(self.a));self.card('foreign',project=str(self.b))
        self.card('unknown');self.card('global',scope='global')
        (self.vault/'MEMORY.md').write_text('# index\n[Own](own.md)\n[Foreign](foreign.md)\n'
            '[Unknown](unknown.md)\n[Global](global.md)\n[All](_views/current.md)\n',encoding='utf-8')
        index=host_sync.project_index_text(self.vault,root=self.a)
        for name in ('own', 'global'):
            target = (self.vault / (name + '.md')).resolve()
            self.assertIn('](<' + target.as_posix() + '>)', index)
            self.assertTrue(target.is_file())
        for value in ('foreign.md','unknown.md','_views/current.md'):self.assertNotIn(value,index)
        self.assertTrue((self.vault/'foreign.md').is_file())

    def test_task_cwd_is_not_borrowed_from_another_checkout(self):
        card=self.card('task',cwd=str(self.a))
        self.assertEqual(rule_context.reference_applicability({'cwd':str(self.a)},card,{'cwd':str(self.b)})[0],'OUTSIDE')
        self.assertEqual(rule_context.reference_applicability({'cwd':str(self.a)},card,{'cwd':str(self.a)})[0],'APPLICABLE')

    def test_ambiguous_capture_retains_origin_in_governance_not_arbitrary_project(self):
        governance=self.root/'governance';governance.mkdir()
        self.card('registered-child')
        ancestor=self.home/'.claude/projects'/route.project_slug(self.root)/'memory'
        ancestor.mkdir(parents=True)
        (ancestor/'MEMORY.md').write_text('ancestor',encoding='utf-8')
        (ancestor.parent/'ancestor.jsonl').write_text(json.dumps({'cwd':str(self.root)})+'\n',encoding='utf-8')
        self.assertEqual(route.capture_vault(str(self.a),governance,self.home),governance)
        self.assertEqual(route.capture_vault(str(self.b),governance,self.home),governance)

    def test_missing_root_never_borrows_candidate(self):
        absent=self.root/'動畫試做'
        self.assertFalse(route.reference_owner_matches(self.vault,absent))
        self.assertIsNone(route.project_root_of(self.vault))

    def test_project_targets_keep_two_observed_roots_separate(self):
        governance=self.root/'gov';governance.mkdir()
        (governance/'_WORK_LEDGER.md').write_text('ledger',encoding='utf-8')
        targets,_=host_sync.project_targets([governance,self.vault],hosts=['codex'],home=self.home)
        self.assertEqual({root for _,root,_ in targets},{self.a,self.b})

    def test_explicit_lookup_retains_unowned_historical_sources(self):
        from epitype import memsearch
        path=self.card('foreign')
        memsearch.build_index(self.vault)
        self.assertIn(path.name,json.dumps(memsearch.query_index(self.vault,'foreign')))

    def test_ancestor_does_not_own_a_registered_child_project(self):
        self.card('registered-child')
        ancestor=self.home/'.claude/projects'/route.project_slug(self.root)/'memory'
        ancestor.mkdir(parents=True)
        (ancestor.parent/'ancestor.jsonl').write_text(json.dumps({'cwd':str(self.root)})+'\n',encoding='utf-8')
        self.assertFalse(route.reference_owner_matches(ancestor,self.a))

    def test_engineering_scope_shares_git_worktree_but_task_origin_does_not(self):
        main=self.root/'git-main';other=self.root/'git-other'
        def git(*args):
            result=subprocess.run(['git',*map(str,args)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
        git('init','-q',main)
        git('-C',main,'-c','user.name=Fixture','-c','user.email=fixture@example.com',
            'commit','--allow-empty','-qm','fixture')
        git('-C',main,'worktree','add','-q','--detach',other)
        path=self.card('engineering',project=str(main))
        event={'cwd':str(other)}
        self.assertEqual(rule_context.reference_applicability({'project':str(main)},path,event)[0],'APPLICABLE')
        self.assertEqual(rule_context.reference_applicability({'cwd':str(main)},path,event)[0],'OUTSIDE')

if __name__=='__main__':unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
