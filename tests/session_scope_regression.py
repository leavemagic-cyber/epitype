"""Synthetic two-host session-project selection, isolation and CLI transactions."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'adapters' / 'claude')]
import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from epitype import session_scope as scope, capture_route, rule_context, memspec, rule_provenance
import _hook_common as common
import sessionstart_hook
import stop_gate


class SessionScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='epi-scope-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'host'
        self.a, self.b = self.root / 'project-a', self.root / 'project-b'
        self.a.mkdir(); self.b.mkdir()
        self.gov = self.root / 'governance'; self.gov.mkdir()
        (self.gov / '_WORK_LEDGER.md').write_text('# governance\n')
        self.va = self.vault(self.a); self.vb = self.vault(self.b)
        self.config = {'vaults': [self.gov, self.va, self.vb], 'project_roots': {}}
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'USERPROFILE': str(self.home),
                                          'CODEX_HOME': str(self.home / '.codex'),
                                          'EPITYPE_DREAM_MODE': 'off'})
        self.env.start(); self.addCleanup(self.env.stop)

    def vault(self, root):
        v = self.home / '.claude' / 'projects' / capture_route.project_slug(root) / 'memory'
        v.mkdir(parents=True)
        (v / 'MEMORY.md').write_text('# synthetic index\n')
        return v

    def event(self, host='claude', **extra):
        if host == 'claude':
            transcript = self.va.parent / 'synthetic.jsonl'
            transcript.write_text(json.dumps({'cwd': str(self.a)}) + '\n')
        else:
            transcript = self.home / '.codex' / 'sessions' / 'rollout-synthetic.jsonl'
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(json.dumps({'type':'session_meta','payload':{'cwd':str(self.a)}})+'\n')
        return {'cwd': str(self.b), 'transcript_path': str(transcript), 'session_id': 'synthetic', **extra}

    def test_home_both_hosts_wins_over_here_and_reads_match_capture(self):
        for host in ('claude', 'codex'):
            event = self.event(host)
            project = scope.session_project(event, self.config)
            self.assertEqual(project.vault, self.va)
            self.assertTrue(project.home)
            self.assertEqual(capture_route.capture_vault(str(self.b), self.gov,
                config=self.config, event=event), self.va)
            candidates = common.scoped_vaults(self.config, event)
            self.assertEqual(candidates, [self.vb, self.gov, self.va])
            card = self.va / 'reference.md'
            card.write_text('body')
            self.assertEqual(rule_context.reference_applicability({'cwd': str(self.a)}, card, event)[0], 'APPLICABLE')
            self.assertEqual(rule_context.applicability({'project':str(self.a),'actions':['*']},event,card=card)[0], 'APPLICABLE')
            # A case restriction is not relaxed by project selection.
            self.assertEqual(rule_context.applicability({'project':str(self.a),'case':'different','actions':['*']},event,card=card)[0], 'ADVISORY')

    def test_other_vault_captured_origin_remains_outside(self):
        event = self.event(); common.scoped_vaults(self.config, event)
        card = self.vb / 'reference.md'; card.write_text('body')
        self.assertEqual(rule_context.reference_applicability({'cwd':str(self.a)},card,event)[0], 'OUTSIDE')

    def test_lossy_slug_collision_does_not_widen_other_project(self):
        # dev/a-b and dev/a/b share one native slug; the vault's transcripts prove a-b.
        ab, a_b = self.root / 'dev' / 'a-b', self.root / 'dev' / 'a' / 'b'
        ab.mkdir(parents=True); a_b.mkdir(parents=True)
        self.assertEqual(capture_route.project_slug(ab), capture_route.project_slug(a_b))
        shared = self.vault(ab)
        (shared.parent / 'observed.jsonl').write_text(json.dumps({'cwd': str(ab)}) + '\n')
        (shared / 'card.md').write_text('---\nname: card\ndescription: d\n---\nbody\n')
        config = dict(self.config, vaults=[*self.config['vaults'], shared])
        card = shared / 'reference.md'; card.write_text('body')
        # Step 3 (native), no transcript, cwd in the other project.
        event = {'cwd': str(a_b), 'session_id': 'collide'}
        self.assertIsNone(scope.session_project(event, config))
        common.scoped_vaults(config, event)
        self.assertEqual(rule_context.reference_applicability({'cwd': str(ab)}, card, event)[0], 'OUTSIDE')
        self.assertNotEqual(capture_route.capture_vault(str(a_b), self.gov, config=config, event=event), shared)
        # Codex rollout started in the other project: no home from the colliding slug.
        rollout = self.home / '.codex' / 'sessions' / 'rollout-collide.jsonl'
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'cwd': str(a_b)}}) + '\n')
        codex_event = {'cwd': str(a_b), 'transcript_path': str(rollout), 'session_id': 'collide-codex'}
        self.assertIsNone(scope.session_project(codex_event, config))
        common.scoped_vaults(config, codex_event)
        self.assertEqual(rule_context.reference_applicability({'cwd': str(ab)}, card, codex_event)[0], 'OUTSIDE')
        # The proven project itself still resolves.
        self.assertEqual(scope.session_project({'cwd': str(ab)}, config).vault, shared)

    def test_claude_home_in_colliding_folder_is_not_trusted(self):
        # The host writes the a/b session's transcript into the a-b folder (same slug).
        ab, a_b = self.root / 'dev' / 'a-b', self.root / 'dev' / 'a' / 'b'
        ab.mkdir(parents=True); a_b.mkdir(parents=True)
        shared = self.vault(ab)
        (shared.parent / 'observed.jsonl').write_text(json.dumps({'cwd': str(ab)}) + '\n')
        session = shared.parent / 'other-project-session.jsonl'
        session.write_text(json.dumps({'cwd': str(a_b)}) + '\n')
        (shared / 'card.md').write_text('---\nname: card\ndescription: d\n---\nbody\n')
        config = dict(self.config, vaults=[*self.config['vaults'], shared])
        card = shared / 'reference.md'; card.write_text('body')
        event = {'cwd': str(a_b), 'transcript_path': str(session), 'session_id': 'collide-claude'}
        project = scope.session_project(event, config)
        self.assertFalse(project is not None and project.vault == shared)
        common.scoped_vaults(config, event)
        self.assertEqual(rule_context.reference_applicability({'cwd': str(ab)}, card, event)[0], 'OUTSIDE')
        self.assertNotEqual(rule_context.applicability({'project': str(ab), 'actions': ['*']}, event,
                                                       card=card)[0], 'APPLICABLE')
        # A folder with one observed root keeps its home, even when cwd is elsewhere.
        (shared.parent / 'other-project-session.jsonl').unlink()
        (shared / memspec.FTS_INDEX_DIRECTORY / memspec.PROJECT_ROOTS_CACHE_FILENAME).unlink()
        alone = shared.parent / 'own-session.jsonl'
        alone.write_text(json.dumps({'cwd': str(ab)}) + '\n')
        project = scope.session_project({'cwd': str(self.b), 'transcript_path': str(alone)}, config)
        self.assertEqual((project.vault, project.home), (shared, True))

    def test_slug_fallback_needs_a_folder_without_transcripts(self):
        # A transcript without a usable cwd gives no observed root; the ASCII slug
        # guess must still not run because the folder has host transcripts.
        ab = self.root / 'dev' / 'slugfall'
        ab.mkdir(parents=True)
        shared = self.vault(ab)
        (shared.parent / 'headless.jsonl').write_text('{"type": "summary"}\n')
        config = dict(self.config, vaults=[*self.config['vaults'], shared])
        with patch.object(capture_route, 'reference_owner_matches', return_value=None):
            project = scope.session_project({'cwd': str(ab / 'sub')}, config)
        self.assertEqual(project.vault, shared)
        self.assertEqual(project.roots, ())

    def test_unusable_cwd_never_picks_native_vault_unchecked(self):
        unc = '\\\\server\\share\\proj'
        slug = capture_route.project_slug(unc)
        vault = self.home / '.claude' / 'projects' / slug / 'memory'
        vault.mkdir(parents=True)
        (vault / 'card.md').write_text('---\nname: c\ndescription: d\n---\nbody\n')
        config = dict(self.config, vaults=[*self.config['vaults'], vault])
        with patch.object(scope, '_native', return_value=[vault.resolve()]):
            self.assertIsNone(scope.session_project({'cwd': unc}, config))

    @unittest.skipUnless(os.name == 'nt', 'Windows junction')
    def test_junctioned_claude_folder_keeps_home(self):
        real = self.root / 'real-claude'
        (self.home).mkdir(parents=True, exist_ok=True)
        (self.home / '.claude').rename(real)
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.home / '.claude'), str(real)],
                              capture_output=True)
        if made.returncode:
            self.skipTest('junction unavailable')
        event = self.event()
        event['transcript_path'] = str(self.home / '.claude' / 'projects' / self.va.parent.name / 'synthetic.jsonl')
        config = dict(self.config, vaults=[self.gov, real / 'projects' / self.va.parent.name / 'memory', self.vb])
        project = scope.session_project(event, config)
        self.assertEqual((project.vault, project.home), ((real / 'projects' / self.va.parent.name / 'memory'), True))

    def test_growing_session_transcript_does_not_rewrite_roots_cache(self):
        live = self.va.parent / 'live.jsonl'
        live.write_text(json.dumps({'cwd': str(self.a)}) + '\n')
        capture_route.project_roots_of(self.va)
        cache = self.va / memspec.FTS_INDEX_DIRECTORY / memspec.PROJECT_ROOTS_CACHE_FILENAME
        before = cache.read_bytes()
        with patch.object(capture_route.os, 'replace', wraps=os.replace) as replace:
            for turn in range(3):
                with live.open('a') as stream:
                    stream.write(json.dumps({'turn': turn}) + '\n')
                self.assertEqual(capture_route.project_roots_of(self.va), [self.a])
        self.assertEqual(replace.call_count, 0)
        self.assertEqual(cache.read_bytes(), before)

    def test_custom_vault_roots_never_glob_transcripts(self):
        custom = self.root / 'custom-vault'; custom.mkdir()
        (custom / 'card.md').write_text('---\nname: c\ndescription: d\n---\nbody\n')
        (self.root / 'stray.jsonl').write_text(json.dumps({'cwd': str(self.root / 'elsewhere')}) + '\n')
        config = dict(self.config, vaults=[*self.config['vaults'], custom],
                      project_roots={str(custom): [str(self.b)]})
        project = scope.session_project({'cwd': str(self.b / 'x')}, config)
        self.assertEqual(project.vault, custom.resolve())
        self.assertEqual(project.roots, (self.b,))
        self.assertFalse((custom / memspec.FTS_INDEX_DIRECTORY).exists())

    def test_doctor_lines_survive_malformed_config(self):
        for bad in ({'vaults': 5}, {'vaults': None, 'project_roots': 'x'}):
            lines = scope.doctor_lines(bad, self.home)
            self.assertTrue(lines and all(isinstance(line, str) for line in lines))

    def test_resolution_is_computed_once_per_event(self):
        event = self.event()
        with patch.object(scope, '_resolve', wraps=scope._resolve) as inner:
            first = scope.resolve(event, self.config)
            for _ in range(5):
                self.assertIs(scope.resolve(event, self.config), first)
            event['cwd'] = str(self.a)  # a changed event is resolved again
            scope.resolve(event, self.config)
        self.assertEqual(inner.call_count, 2)

    def test_pending_capture_never_promoted_by_home(self):
        event = self.event(); common.scoped_vaults(self.config,event)
        card = self.va / '_drafts/captured_pending/example.md'
        self.assertEqual(rule_context.reference_applicability({}, card,event)[0], 'OUTSIDE')

    def test_forged_evidence_dict_is_not_trusted(self):
        card = self.va / 'card.md'
        event = {'cwd':str(self.b),'_epitype_session_project':{'vault':str(self.va),'home':True}}
        self.assertFalse(scope.card_in_project(card,event))

    def test_event_remains_json_serializable(self):
        event=self.event(); common.scoped_vaults(self.config,event)
        decoded=json.loads(json.dumps(event))
        self.assertFalse(scope.card_in_project(self.va/'card.md',decoded))

    def test_claude_child_project_path(self):
        event=self.event(transcript_path=str(self.va.parent/'synthetic/subagents/agent.jsonl'))
        self.assertEqual(scope.session_project(event,self.config).vault,self.va)

    def test_codex_parent_id_does_not_scan_parent(self):
        event=self.event('codex')
        Path(event['transcript_path']).write_text(json.dumps({'type':'session_meta','payload':{
            'cwd':str(self.a),'parent_thread_id':'parent'}})+'\n')
        self.assertEqual(scope.session_project(event,self.config).vault,self.vb)

    def test_forged_outside_transcript_and_unregistered_home(self):
        event=self.event(transcript_path=str(self.root/'foreign/projects/x/session.jsonl'))
        self.assertEqual(scope.session_project(event,self.config).vault,self.vb)
        event=self.event(); self.config['vaults']=[self.gov,self.vb]
        self.assertEqual(scope.session_project(event,self.config).vault,self.vb)

    def test_symlink_transcript_escape(self):
        outside=self.root/'outside'; outside.mkdir()
        target=outside/'session.jsonl';target.write_text('{}')
        link=self.va.parent/'escape.jsonl'
        try: link.symlink_to(target)
        except OSError: self.skipTest('symlinks unavailable')
        event={'cwd':str(self.b),'transcript_path':str(link)}
        self.assertEqual(scope.session_project(event,self.config).vault,self.vb)

    def test_codex_missing_oversize_invalid_header_fail_to_here(self):
        for value in ('', '{broken', 'x'*(scope.MAX_HEADER_BYTES+1), '[]'):
            event=self.event('codex');Path(event['transcript_path']).write_text(value)
            self.assertEqual(scope.session_project(event,self.config).vault,self.vb)
        Path(event['transcript_path']).unlink()
        self.assertEqual(scope.session_project(event,self.config).vault,self.vb)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO fixture')
    def test_codex_special_file_does_not_block(self):
        event=self.event('codex');path=Path(event['transcript_path']);path.unlink();os.mkfifo(path)
        self.assertEqual(scope.session_project(event,self.config).vault,self.vb)

    def test_mapped_longest_root_and_component_boundary(self):
        nested=self.b/'nested';nested.mkdir()
        self.config['project_roots']={str(self.va):[str(self.b)],str(self.vb):[str(nested)]}
        self.assertEqual(scope.session_project({'cwd':str(nested)},self.config).vault,self.vb)
        self.assertEqual(scope.session_project({'cwd':str(self.b)},self.config).vault,self.va)
        sibling=self.root/'project-b-other';sibling.mkdir()
        self.assertIsNone(scope.session_project({'cwd':str(sibling)},self.config))

    def test_ambiguous_root_ignored_and_reported(self):
        self.config['project_roots']={str(self.va):[str(self.b)],str(self.vb):[str(self.b)]}
        result=scope.resolve({'cwd':str(self.b)},self.config)
        self.assertEqual(result.project.vault,self.vb) # legacy here remains
        self.assertTrue(any('對應不明確' in d for d in result.diagnostics))

    def test_one_bad_source_does_not_remove_valid_mapping(self):
        self.config['project_roots']={str(self.va):[str(self.b)]}
        event=self.event('codex');Path(event['transcript_path']).write_text('{')
        self.assertEqual(scope.session_project(event,self.config).vault,self.va)

    def test_legacy_candidate_ancestors_and_missing_cwd_preserved(self):
        event={'cwd':str(self.b)}
        legacy=[self.vb,self.va,self.gov]
        self.assertEqual(scope.candidates(legacy,event,self.config),legacy)
        self.assertEqual(common.scoped_vaults(self.config,{}),[self.gov,self.va,self.vb])
        self.assertEqual(common.scoped_vaults(self.config,{'cwd':str(self.b)}),[self.vb,self.gov])

    def test_project_excludes_governance(self):
        self.config['vaults']=[self.va,self.vb]
        event=self.event()
        self.assertEqual(scope.session_project(event,self.config).vault,self.vb)

    def test_native_failure_preserves_existing_candidates(self):
        with patch.object(capture_route,'native_cwd_vaults',side_effect=OSError('offline')):
            self.assertEqual(scope.candidates([self.gov],{},self.config),[self.gov])

    def test_root_failure_does_not_remove_proven_home(self):
        event=self.event()
        with patch.object(capture_route,'project_roots_of',side_effect=OSError('offline')):
            self.assertEqual(scope.session_project(event,self.config).vault,self.va)

    def test_source_cli_and_doctor_preserve_unknown_config(self):
        config=self.root/'config.json'
        config.write_text(json.dumps({'vaults':[str(self.gov),str(self.va)],'unknown':{'retain':True}}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scope.main(['--config',str(config),'roots','add',str(self.b),'--vault',str(self.va)]),0)
        data=json.loads(config.read_text());self.assertEqual(data['unknown'],{'retain':True})
        self.assertEqual(data['project_roots'][str(self.va)],[str(self.b)])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scope.main(['--config',str(config),'roots','remove',str(self.b),'--vault',str(self.va)]),0)
        self.assertEqual(json.loads(config.read_text())['project_roots'],{})
        router=self.home/'.claude/hooks/recall_router.json';router.parent.mkdir(parents=True);router.write_text('{}')
        lines=scope.doctor_lines({'vaults':[str(self.gov)],'project_roots':{str(self.va):[str(self.a)]}},self.home)
        self.assertTrue(any('關閉' in x for x in lines));self.assertTrue(any('未註冊' in x for x in lines))
        self.assertTrue(any('不使用此檔' in x for x in lines))

    def test_mapping_cli_lock_contention_does_not_write(self):
        config=self.root/'config.json';config.write_text(json.dumps({'vaults':[str(self.gov),str(self.va)]}))
        before=config.read_bytes()
        with memspec.file_lock(config,0) as held:
            self.assertTrue(held)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(scope.main(['--config',str(config),'roots','add',str(self.b),'--vault',str(self.va)]),1)
        self.assertEqual(config.read_bytes(),before)

    def test_two_cli_writers_preserve_both_mappings(self):
        config=self.root/'config.json'
        config.write_text(json.dumps({'vaults':[str(self.gov),str(self.va)],'retain':'yes'}))
        commands=[[sys.executable,'-m','epitype.cli','roots','add',str(root),
                   '--vault',str(self.va),'--config',str(config)] for root in (self.a,self.b)]
        children=[subprocess.Popen(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                  for command in commands]
        for child in children:
            out,err=child.communicate(timeout=5)
            self.assertEqual(child.returncode,0,err.decode())
        data=json.loads(config.read_text())
        self.assertEqual(set(data['project_roots'][str(self.va)]),{str(self.a),str(self.b)})
        self.assertEqual(data['retain'],'yes')

    def test_mapping_cli_replace_failure_leaves_config_intact(self):
        config=self.root/'config.json';config.write_text(json.dumps({'vaults':[str(self.gov),str(self.va)]}))
        before=config.read_bytes()
        with patch.object(scope.os,'replace',side_effect=PermissionError('busy')),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scope.main(['--config',str(config),'roots','add',str(self.b),'--vault',str(self.va)]),1)
        self.assertEqual(config.read_bytes(),before)
        self.assertEqual(list(self.root.glob('.roots-*')),[])

    def test_hook_config_and_stop_scope_two_hosts(self):
        config=self.root/'config.json'
        config.write_text(json.dumps({'vaults':list(map(str,self.config['vaults'])),
                                     'project_roots':{str(self.va):[str(self.b)]}}))
        with patch.dict(os.environ,{'EPITYPE_CONFIG':str(config)}):
            loaded=common.load_config(time.monotonic())
            self.assertIn('project_roots',loaded)
            for host in ('claude','codex'):
                event=self.event(host)
                self.assertIn(self.va,stop_gate._vaults(loaded,event))
                self.assertTrue(scope.card_in_project(self.va/'card.md',event))

    def test_real_recall_entrypoint_both_hosts_and_output_budget(self):
        from epitype import memsearch
        import recall_hook
        card=self.va/'session-reference.md'
        card.write_text('---\nname: session-reference\ndescription: scopeuniqueneedle\n'
                        +'project: '+str(self.a)+'\n---\nSynthetic reference.\n')
        memsearch.build_index(self.va)
        config=self.root/'config.json'
        config.write_text(json.dumps({'vaults':list(map(str,self.config['vaults'])),
                                     'proactive_recall':True,'budget_bytes':2048,'dream':{'mode':'off'}}))
        for host in ('claude','codex'):
            event=self.event(host,prompt='scopeuniqueneedle',session_id='synthetic-'+host)
            out=io.StringIO()
            with patch.dict(os.environ,{'EPITYPE_CONFIG':str(config)}), \
                    patch.object(sys,'stdin',io.StringIO(json.dumps(event))), \
                    patch.object(sys,'argv',['recall']), \
                    patch.object(recall_hook,'_STARTED_AT',time.monotonic()), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(recall_hook.main(),0)
            response=json.loads(out.getvalue())
            context=response['hookSpecificOutput']['additionalContext']
            self.assertIn('session-reference',context)
            self.assertLessEqual(len(out.getvalue().encode('utf-8')),2048)

    def test_bounded_git_worktree_applicability_unchanged(self):
        # Exercise the same-common-dir branch, not a new ad-hoc .git parser.
        with patch.object(rule_context,'_git_location',return_value=('synthetic-common',Path('.'))):
            self.assertEqual(rule_context._path_applicability(str(self.a),str(self.b))[0], 'APPLICABLE')
        with patch.object(rule_context,'_git_location',return_value=False):
            self.assertEqual(rule_context._path_applicability(str(self.a),str(self.b))[0], 'ADVISORY')
        # Captures still cannot borrow the common-dir exception in another vault.
        card=self.vb/'captured.md'
        with patch.object(rule_context,'_git_location',return_value=('synthetic-common',Path('.'))):
            self.assertEqual(rule_context.reference_applicability({'cwd':str(self.a)},card,
                             {'cwd':str(self.b)})[0], 'OUTSIDE')

    def test_real_git_worktree_same_common_dir_and_explicit_project(self):
        import shutil
        if shutil.which('git') is None:
            self.skipTest('Git unavailable')
        main=self.root/'main-repo'; linked=self.root/'linked-repo'; main.mkdir()
        commands=[['git','init','-q',str(main)],
                  ['git','-C',str(main),'-c','user.name=Synthetic',
                   '-c','user.email=synthetic@example.invalid','commit','--allow-empty','-qm','fixture'],
                  ['git','-C',str(main),'worktree','add','--detach',str(linked)]]
        for command in commands:
            result=subprocess.run(command,capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
        rule_context._git_location.cache_clear()
        # Correctness, not speed: the 0.3 s hook budget is covered elsewhere; under a loaded
        # machine git alone can exceed it and the answer degrades to ADVISORY by design.
        with rule_provenance.unbounded_verification():
            self.assertEqual(rule_context._path_applicability(str(main),str(linked))[0],'APPLICABLE')
        # No home/mapping/native vault: a common Git directory alone is not
        # new authority to choose the state store (v2 section 9.2).
        self.assertIsNone(scope.session_project({'cwd':str(linked)},self.config))
        self.config['project_roots']={str(self.va):[str(linked)]}
        self.assertEqual(scope.session_project({'cwd':str(linked)},self.config).vault,self.va)

    def test_worktree_with_explicit_mapping_keeps_other_vault_origin_isolated(self):
        # Shared Git metadata alone is not a state project; explicit mapping is.
        self.config['project_roots']={str(self.va):[str(self.b)]}
        event={'cwd':str(self.b)};common.scoped_vaults(self.config,event)
        self.assertEqual(scope.session_project(event,self.config).vault,self.va)
        self.assertTrue(scope.card_in_project(self.va/'card.md',event))
        self.assertFalse(scope.card_in_project(self.vb/'card.md',event))


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
