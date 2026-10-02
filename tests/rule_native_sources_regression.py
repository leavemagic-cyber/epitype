import sys; sys.dont_write_bytecode = True
"""Real native rendering in fake homes; synthetic records grant no live authority."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import capture_route, host_native, memspec, rule_legacy, rule_pipeline

NATIVE_FIELDS = (
    'host_claude_env:\n  CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS: "4"\n'
    'host_claude_deny:\n  - Bash(git clean:*)\n'
    'host_codex_config:\n  agents.max_concurrent_threads_per_session: 4\n'
    'host_codex_rules:\n  - git clean\n'
    'host_source: https://developers.openai.com/codex/config-reference\n'
)
USER_SETTINGS = (
    '{\n  "env": {"USER_VALUE": "keep"},\n'
    '  "permissions": {"allow": ["Bash(git status)"], "deny": ["Read(user-file)"]},\n'
    '  "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "python user.py"}]}]},\n'
    '  "theme": "dark"\n}\n'
)
USER_CONFIG = (
    'model = "synthetic" # user-selected, preserve comment\n'
    '[features]\nhooks = true\n'
    '[hooks.state.user]\ntrusted_hash = "synthetic-user-hook"\n'
)
USER_RULES = 'prefix_rule(pattern=["synthetic-user-command"], decision="forbidden")\n'


class NativeSourceConsumers(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='epitype-native-source-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / 'home'
        self.vault = self.root / 'governance'
        self.vault.mkdir()
        (self.home / '.claude').mkdir(parents=True)
        (self.home / '.codex' / 'rules').mkdir(parents=True)
        (self.home / '.epitype').mkdir(parents=True)
        self.settings = self.home / '.claude' / 'settings.json'
        self.config = self.home / '.codex' / 'config.toml'
        self.rules = self.home / '.codex' / 'rules' / memspec.HOST_NATIVE_CODEX_RULES_FILENAME
        self.user_rules = self.rules.with_name('user.rules')
        self.settings.write_text(USER_SETTINGS, encoding='utf-8')
        self.config.write_text(USER_CONFIG, encoding='utf-8')
        self.user_rules.write_text(USER_RULES, encoding='utf-8')
        self.epiconfig = self.home / '.epitype' / 'config.json'
        self.epiconfig.write_text(json.dumps({'vaults': [str(self.vault)]}), encoding='utf-8')
        self.path = self.vault / 'ordinary-name.md'
        self.source = self.root / 'synthetic-original.jsonl'
        self.vaults = [self.vault]
        env = patch.dict(os.environ, {'HOME': str(self.home), 'USERPROFILE': str(self.home),
            'CODEX_HOME': str(self.home / '.codex'), 'EPITYPE_CONFIG': str(self.epiconfig),
            'EPITYPE_DREAM_MODE': 'off'})
        env.start()
        self.addCleanup(env.stop)

    def scope(self, *, project=None, case=None, expiry='unspecified'):
        return {'level': 'case' if case else ('project' if project else 'global'),
            'project': str(project) if project else None, 'case': case, 'actions': ['*'],
            'conditions': [], 'expiry': expiry, 'exceptions': []}

    def card(self, *, host='codex', scope=None, path=None, status='active',
             superseded_by=None, supersedes=None, prior=None, native=NATIVE_FIELDS,
             legacy=False, card_type='project', malformed_scope=False):
        path = Path(path or self.path)
        scope = copy.deepcopy(scope or self.scope())
        quote = 'Synthetic fixture only: ' + json.dumps(scope, ensure_ascii=False) + '; adopt native fixture values.'
        source = self.source if path == self.path else self.root / (path.stem + '-original.jsonl')
        sid, mid = 'synthetic-native-session-' + path.stem, 'synthetic-native-message-' + path.stem
        if host == 'claude':
            row = {'type': 'user', 'sessionId': sid, 'uuid': mid,
                'message': {'role': 'user', 'content': [{'type': 'text', 'text': quote}]}}
        else:
            row = {'type': 'response_item', 'payload': {'type': 'message', 'id': mid, 'role': 'user',
                'content': [{'type': 'input_text', 'text': quote}]}}
        meta = (json.dumps({'type': 'session_meta', 'payload': {'id': sid}}) + '\n').encode()
        raw = (json.dumps(row, ensure_ascii=False) + '\n').encode()
        source.write_bytes(meta + raw)
        fields = 'name: synthetic-native\ntype: ' + card_type + '\nstatus: ' + status + '\n'
        if superseded_by:
            fields += 'superseded_by: ' + json.dumps(str(superseded_by)) + '\n'
        plain = '---\n' + fields + native + '---\nSynthetic native body.\n'
        if legacy:
            path.write_text(plain, encoding='utf-8')
            return plain
        document = {'version': 1, 'key': 'synthetic-native-' + path.stem, 'kind': 'ai-derived',
            'author': 'synthetic-fixture-ai', 'quote': quote, 'text': 'Synthetic native execution arrangement.',
            'sources': [{'path': str(source), 'byte_offset': len(meta), 'line': 2,
                'record_sha256': hashlib.sha256(raw).hexdigest(), 'session_id': sid,
                'message_id': mid, 'role': 'U'}], 'scope': scope,
            'scope_basis': {'source': 0, 'quote': quote, 'reviewed_by': 'synthetic-fixture-reviewer',
                'reviewed_at': '2026-10-03', 'rationale': 'Synthetic record explicitly states its fixture scope.'},
            'supersedes': list(supersedes or [])}
        if prior:
            document['supersession_basis'] = {'prior_key': prior['key'], 'prior_scope': prior['scope'],
                **document['scope_basis']}
        if malformed_scope:
            document.pop('scope')
        document = rule_pipeline.bind_payload(document, plain, reviewer='synthetic-fixture-reviewer')
        bound = plain.replace('---\n', '---\nrule_provenance: '
            + json.dumps(json.dumps(document, ensure_ascii=False), ensure_ascii=False) + '\n', 1)
        path.write_text(bound, encoding='utf-8')
        self.document = document
        return bound

    def call(self, function=host_native.apply):
        report = io.StringIO()
        result = function(self.vaults, hosts=['claude', 'codex'], home=self.home, output=report)
        return result, report.getvalue()

    def snapshot(self):
        paths = [self.settings, self.config, self.rules, self.user_rules, host_native.state_path(self.home)]
        return {str(path): path.read_bytes() if path.exists() else None for path in paths}

    def assert_refuses_preserving(self, needle=None):
        before = self.snapshot()
        checked, report = self.call(host_native.check)
        self.assertEqual(checked[0], 0, report)
        self.assertGreater(checked[1], 0, report)
        self.assertIn('REFUSE', report)
        self.assertEqual(self.snapshot(), before)
        applied, report = self.call()
        self.assertEqual(applied[0], 0, report)
        self.assertGreater(applied[1], 0, report)
        if needle:
            self.assertIn(needle, report)
        self.assertEqual(self.snapshot(), before)
        return report

    def install(self):
        result, report = self.call()
        self.assertEqual(result, (3, 0), report)

    def freeze(self, paths=None):
        paths = paths or [self.path]
        entries = {str(path): hashlib.sha256(rule_pipeline._meaning(
            path.read_text(encoding='utf-8'), include_source=False).encode()).hexdigest() for path in paths}
        self.baseline = self.root / 'frozen-baseline.json'
        raw = json.dumps(rule_legacy.build_baseline(entries), ensure_ascii=False).encode()
        self.baseline.write_bytes(raw)
        options = json.loads(self.epiconfig.read_text(encoding='utf-8'))
        options.update(rule_source_baseline=str(self.baseline), rule_source_baseline_sha256=hashlib.sha256(raw).hexdigest())
        self.epiconfig.write_text(json.dumps(options), encoding='utf-8')

    def equal_stat_replace(self, path, old, new):
        before = path.stat()
        raw = path.read_bytes()
        changed = raw.replace(old, new)
        self.assertNotEqual(raw, changed)
        self.assertEqual(len(raw), len(changed))
        path.write_bytes(changed)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(path.stat().st_size, before.st_size)

    def project(self):
        project = self.root / 'synthetic-project'
        project.mkdir()
        vault = self.home / '.claude' / 'projects' / capture_route.project_slug(project) / 'memory'
        vault.mkdir(parents=True)
        (vault.parent / 'synthetic-session.jsonl').write_text(json.dumps(
            {'type': 'user', 'cwd': str(project)}) + '\n', encoding='utf-8')
        with self.config.open('a', encoding='utf-8') as stream:
            stream.write(f"\n[projects.'{project}']\ntrust_level = \"trusted\"\n")
        self.vaults.append(vault)
        self.epiconfig.write_text(json.dumps({'vaults': [str(p) for p in self.vaults]}), encoding='utf-8')
        return project, vault

    def test_both_original_host_formats_render_actual_native_targets(self):
        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                self.card(host=host)
                checked, report = self.call(host_native.check)
                self.assertGreater(checked[0], 0, report)
                self.assertEqual(checked[1], 0, report)
                self.assertEqual(self.call()[0][1], 0)
                settings = json.loads(self.settings.read_text(encoding='utf-8'))
                config = tomllib.loads(self.config.read_text(encoding='utf-8'))
                self.assertEqual(settings['env']['CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'], '4')
                self.assertIn('Bash(git clean:*)', settings['permissions']['deny'])
                self.assertEqual(config['agents']['max_concurrent_threads_per_session'], 4)
                self.assertIn('pattern=["git", "clean"]', self.rules.read_text(encoding='utf-8'))
                self.assertEqual(self.call(host_native.check)[0], (0, 0))
                # Force a fresh positive rendering on the next original format.
                self.path.unlink()
                self.assertEqual(self.call()[0][1], 0)

    def test_render_preserves_whole_user_files_hooks_comments_and_handwritten_rules(self):
        self.card()
        self.install()
        text = self.settings.read_text(encoding='utf-8')
        self.assertIn('"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "python user.py"}]}]}', text)
        parsed = json.loads(text)
        self.assertEqual(parsed['env']['USER_VALUE'], 'keep')
        self.assertEqual(parsed['permissions']['allow'], ['Bash(git status)'])
        self.assertEqual(parsed['theme'], 'dark')
        codex = self.config.read_text(encoding='utf-8')
        self.assertIn(USER_CONFIG, codex)
        self.assertEqual(self.user_rules.read_text(encoding='utf-8'), USER_RULES)
        before = self.snapshot()
        self.assertEqual(self.call()[0], (0, 0))
        self.assertEqual(self.snapshot(), before)

    def test_same_stat_source_tamper_refuses_and_retains_all_owned_settings(self):
        self.card()
        self.install()
        self.equal_stat_replace(self.source, b'adopt native', b'adapt native')
        self.assert_refuses_preserving()

    def test_same_stat_native_execution_field_tamper_refuses(self):
        self.card()
        self.install()
        self.equal_stat_replace(self.path, b'per_session: 4', b'per_session: 8')
        self.assert_refuses_preserving('complete rule body and execution fields')

    def test_same_stat_body_tamper_refuses(self):
        self.card()
        self.install()
        self.equal_stat_replace(self.path, b'Synthetic native body.', b'Synthetic forged body.')
        self.assert_refuses_preserving('complete rule body and execution fields')

    def test_provenance_envelope_change_is_fresh_even_when_meaning_unchanged(self):
        self.card()
        self.install()
        # Scope metadata is not in the separately bound card meaning, but its
        # source-bound scope review still must match the original record.
        old = self.path.read_text(encoding='utf-8')
        changed = old.replace('Synthetic fixture only:', 'Synthetic forgery only:')
        self.assertNotEqual(old, changed)
        self.path.write_text(changed, encoding='utf-8')
        self.assert_refuses_preserving()

    def test_missing_scope_does_not_promote_card_to_global_or_clear_owned_values(self):
        self.card()
        self.install()
        self.card(malformed_scope=True)
        self.assert_refuses_preserving('scope')

    def test_global_target_has_no_frontend_cwd_authority(self):
        self.card()
        self.install()
        self.card(scope=self.scope(project=self.root))
        before_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, before_cwd)
        self.assert_refuses_preserving('project context is unavailable')

    def test_unknown_case_refuses_without_empty_set_cleanup(self):
        self.card()
        self.install()
        self.card(scope=self.scope(project=self.root, case='synthetic-case'))
        # The global target lacks project context before it can infer a case.
        self.assert_refuses_preserving('context is unavailable')
        desired, problems = host_native.collect([self.vault], event={'cwd': str(self.root)})
        self.assertTrue(any('case context is unavailable' in problem for problem in problems))
        self.assertFalse(desired[host_native.SECTION_ENV])

    def test_actual_project_targets_use_their_own_root_and_skip_outside(self):
        project, vault = self.project()
        path = vault / 'ordinary-project.md'
        self.card(path=path, scope=self.scope(project=project))
        result, report = self.call()
        self.assertEqual(result, (3, 0), report)
        settings = project / '.claude' / 'settings.local.json'
        config = project / '.codex' / 'config.toml'
        rules = project / '.codex' / 'rules' / memspec.HOST_NATIVE_CODEX_RULES_FILENAME
        self.assertEqual(json.loads(settings.read_text())['env']['CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'], '4')
        self.assertEqual(tomllib.loads(config.read_text())['agents']['max_concurrent_threads_per_session'], 4)
        self.assertTrue(rules.exists())
        self.assertEqual(self.settings.read_text(), USER_SETTINGS)
        self.assertIn(USER_CONFIG, self.config.read_text())
        self.assertFalse(self.rules.exists())
        # A newly verified OUTSIDE scope retires only previously owned values.
        self.card(path=path, scope=self.scope(project=self.root / 'different-project'))
        result, report = self.call()
        self.assertEqual(result[1], 0, report)
        self.assertFalse(settings.exists())
        self.assertFalse(config.exists())
        self.assertFalse(rules.exists())
        self.assertEqual(self.settings.read_text(), USER_SETTINGS)

    def test_same_vault_context_cache_is_partitioned_by_target_root(self):
        self.card(scope=self.scope(project=self.root / 'inside'))
        inside = self.root / 'inside'
        outside = self.root / 'outside'
        targets = [host_native.Target('claude', host_native.KIND_CLAUDE, root / 'settings.json',
            str(root), [self.vault], root) for root in (inside, outside)]
        with patch.object(host_native, 'targets', return_value=targets):
            result, report = self.call()
        self.assertEqual(result, (1, 0), report)
        self.assertTrue((inside / 'settings.json').exists())
        self.assertFalse((outside / 'settings.json').exists())

    def test_actual_project_unknown_case_preserves_project_files_and_ownership(self):
        project, vault = self.project()
        path = vault / 'ordinary-project.md'
        self.card(path=path, scope=self.scope(project=project))
        self.assertEqual(self.call()[0], (3, 0))
        targets = [target.path for target in host_native.targets(self.vaults, ['claude', 'codex'], self.home)]
        before = {str(path): path.read_bytes() if path.exists() else None for path in targets}
        state = host_native.state_path(self.home).read_bytes()
        self.card(path=path, scope=self.scope(project=project, case='synthetic-case'))
        for function in (host_native.check, host_native.apply):
            result, report = self.call(function)
            self.assertEqual(result[0], 0, report)
            self.assertGreater(result[1], 0, report)
            self.assertIn('case context is unavailable', report)
            self.assertEqual({str(path): path.read_bytes() if path.exists() else None for path in targets}, before)
            self.assertEqual(host_native.state_path(self.home).read_bytes(), state)

    def test_expired_source_retires_only_owned_values(self):
        self.card()
        self.install()
        self.card(scope=self.scope(expiry='2000-01-01'))
        result, report = self.call()
        self.assertEqual(result, (3, 0), report)
        self.assertEqual(json.loads(self.settings.read_text()), json.loads(USER_SETTINGS))
        self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(USER_CONFIG))
        self.assertFalse(self.rules.exists())
        self.assertEqual(self.user_rules.read_text(), USER_RULES)

    def test_retired_card_source_is_checked_before_cleanup(self):
        self.card()
        self.install()
        self.card(status='retired')
        self.equal_stat_replace(self.source, b'adopt native', b'adapt native')
        self.assert_refuses_preserving()

    def test_source_verified_retired_card_releases_owned_keys(self):
        self.card()
        self.install()
        self.card(status='retired')
        self.assertEqual(self.call()[0], (3, 0))
        self.assertEqual(json.loads(self.settings.read_text()), json.loads(USER_SETTINGS))
        self.assertFalse(self.rules.exists())

    def test_reciprocal_source_verified_supersession_uses_only_successor(self):
        self.card()
        self.install()
        prior = copy.deepcopy(self.document)
        successor = self.vault / 'successor.md'
        self.card(status='superseded', superseded_by=successor.name)
        self.card(path=successor, supersedes=[self.path.name], prior=prior,
            native=NATIVE_FIELDS.replace('"4"', '"5"').replace('per_session: 4', 'per_session: 5'))
        result, report = self.call()
        self.assertEqual(result[1], 0, report)
        self.assertEqual(json.loads(self.settings.read_text())['env']['CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'], '5')
        self.assertEqual(tomllib.loads(self.config.read_text())['agents']['max_concurrent_threads_per_session'], 5)

    def test_broken_supersession_never_removes_existing_keys(self):
        self.card()
        self.install()
        self.card(status='superseded', superseded_by='absent-successor.md')
        self.assert_refuses_preserving('supersession')

    def test_unconfigured_legacy_remains_compatible_and_explicitly_unverified(self):
        self.card(legacy=True)
        result, report = self.call()
        self.assertEqual(result, (3, 0), report)
        self.assertIn('UNVERIFIED legacy source', report)
        self.assertIn('not Owner authorship or scope proof', report)
        self.assertNotIn('VERIFIED Owner', report)

    def test_frozen_baseline_allows_same_legacy_without_authorship_proof(self):
        self.card(legacy=True)
        self.freeze()
        result, report = self.call()
        self.assertEqual(result, (3, 0), report)
        self.assertIn('UNVERIFIED legacy source', report)
        self.assertEqual(self.call(host_native.check)[0], (0, 0))

    def test_frozen_baseline_rejects_new_native_card_without_enrollment(self):
        self.card(legacy=True)
        self.freeze()
        self.install()
        new = self.vault / 'new-native.md'
        self.card(path=new, legacy=True)
        self.assert_refuses_preserving('not in the frozen baseline')

    def test_frozen_baseline_rejects_equal_stat_changed_native_type(self):
        self.card(legacy=True, card_type='project')
        self.freeze()
        self.install()
        self.equal_stat_replace(self.path, b'type: project', b'type: user   ')
        self.assert_refuses_preserving('meaning differs')

    def test_frozen_baseline_rejects_removed_native_fields_even_after_type_demoted(self):
        self.card(legacy=True)
        self.freeze()
        self.install()
        self.path.write_text('---\nname: synthetic-native\ntype: reference\n---\nHistorical only.\n', encoding='utf-8')
        self.assert_refuses_preserving('meaning differs')

    def test_source_bound_native_fields_removed_without_new_binding_refuse(self):
        text = self.card()
        self.install()
        self.path.write_text(text.replace(NATIVE_FIELDS, ''), encoding='utf-8')
        self.assert_refuses_preserving('complete rule body and execution fields')

    def test_same_stat_frozen_baseline_tamper_refuses(self):
        self.card(legacy=True)
        self.freeze()
        self.install()
        raw = self.baseline.read_bytes()
        parsed = json.loads(raw)
        original = next(iter(parsed['entries'].values()))
        forged = ('0' if original[0] != '0' else '1') + original[1:]
        self.equal_stat_replace(self.baseline, original.encode(), forged.encode())
        self.assert_refuses_preserving('fixed rule_source_baseline_sha256')

    def test_read_failure_of_ordinary_named_card_is_not_an_empty_vault(self):
        self.card()
        self.install()
        read_text = Path.read_text
        def fail(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError('synthetic ordinary card unreadable')
            return read_text(path, *args, **kwargs)
        with patch.object(Path, 'read_text', fail):
            self.assert_refuses_preserving('PermissionError')

    def test_source_read_failure_refuses(self):
        self.card()
        self.install()
        original_open = Path.open
        def fail(path, *args, **kwargs):
            if path == self.source:
                raise PermissionError('synthetic original unreadable')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', fail):
            self.assert_refuses_preserving()

    def test_failed_vault_scan_is_not_an_empty_desired_set(self):
        self.card()
        self.install()
        scandir = os.scandir
        def fail(path):
            if Path(path) == self.vault:
                raise PermissionError('synthetic vault cannot be listed')
            return scandir(path)
        with patch.object(os, 'scandir', fail):
            self.assert_refuses_preserving('PermissionError')

    def test_unreadable_subtree_cannot_be_silently_dropped(self):
        self.card()
        self.install()
        subtree = self.vault / 'nested'
        subtree.mkdir()
        scandir = os.scandir
        def fail(path):
            if Path(path) == subtree:
                raise PermissionError('synthetic nested subtree unreadable')
            return scandir(path)
        with patch.object(os, 'scandir', fail):
            self.assert_refuses_preserving('PermissionError')

    def test_actual_check_and_apply_report_bounded_source_timeout(self):
        self.card()
        self.install()
        collect = host_native.collect
        def no_budget(vaults, **kwargs):
            return collect(vaults, seconds=0.0, **kwargs)
        with patch.object(host_native, 'collect', no_budget):
            self.assert_refuses_preserving('verification budget exhausted')

    def test_settings_read_failure_refuses_without_treating_file_as_absent(self):
        self.card()
        self.install()
        original_read = Path.read_bytes
        def fail(path, *args, **kwargs):
            if path == self.settings:
                raise PermissionError('synthetic existing settings unreadable')
            return original_read(path, *args, **kwargs)
        settings_before = self.settings.read_bytes()
        state_before = host_native.load_state(self.home)[host_native._key(self.settings)]
        with patch.object(Path, 'read_bytes', fail):
            result, report = self.call()
        self.assertEqual(result[0], 0, report)
        self.assertGreater(result[1], 0, report)
        self.assertIn('PermissionError', report)
        self.assertEqual(self.settings.read_bytes(), settings_before)
        self.assertEqual(host_native.load_state(self.home)[host_native._key(self.settings)], state_before)

    def test_current_target_changed_after_planning_is_preserved_and_refused(self):
        self.card()
        original_render = host_native._render
        changed = USER_SETTINGS.replace('"theme": "dark"', '"theme": "blue"')
        def concurrent_change(item, record):
            if item.target.path == self.settings:
                self.settings.write_text(changed, encoding='utf-8')
            return original_render(item, record)
        with patch.object(host_native, '_render', concurrent_change):
            result, report = self.call()
        self.assertGreater(result[1], 0, report)
        self.assertIn('changed after planning', report)
        self.assertEqual(self.settings.read_text(encoding='utf-8'), changed)
        self.assertNotIn(host_native._key(self.settings), host_native.load_state(self.home))

    def test_doctor_rendering_success_and_legacy_source_status_are_both_visible(self):
        self.card(legacy=True)
        self.install()
        lines, invalid = host_native.drift_lines(self.vaults, home=self.home)
        self.assertFalse(invalid)
        self.assertTrue(any(line.startswith('PASS host-native settings match') for line in lines))
        self.assertTrue(any('UNVERIFIED legacy source' in line for line in lines))

    def test_invalid_existing_ownership_state_refuses_instead_of_replacing_it(self):
        self.card()
        self.install()
        host_native.state_path(self.home).write_text('{broken ownership', encoding='utf-8')
        self.assert_refuses_preserving('UNVERIFIED ownership state')

    def test_ownership_state_read_failure_refuses_both_hosts_without_touching_files(self):
        self.card()
        self.install()
        before = self.snapshot()
        state = host_native.state_path(self.home)
        original_open = io.open
        def fail(path, *args, **kwargs):
            if Path(path) == state:
                raise PermissionError('synthetic ownership state unreadable')
            return original_open(path, *args, **kwargs)
        with patch.object(io, 'open', fail):
            for function in (host_native.check, host_native.apply):
                result, report = self.call(function)
                self.assertEqual(result[0], 0, report)
                self.assertGreater(result[1], 0, report)
                self.assertIn('UNVERIFIED ownership state', report)
        self.assertEqual(self.snapshot(), before)

    def ownership_payload(self):
        return json.loads(host_native.state_path(self.home).read_text(encoding='utf-8'))

    def write_ownership_payload(self, payload):
        host_native.state_path(self.home).write_text(json.dumps(payload), encoding='utf-8')

    def override_owned_user_values(self):
        settings = json.loads(self.settings.read_text(encoding='utf-8'))
        settings['env']['CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'] = 'user-later-value'
        self.settings.write_text(json.dumps(settings), encoding='utf-8')
        config = self.config.read_text(encoding='utf-8')
        self.assertIn('max_concurrent_threads_per_session = 4', config)
        self.config.write_text(config.replace('max_concurrent_threads_per_session = 4',
                                             'max_concurrent_threads_per_session = 7'), encoding='utf-8')

    def test_missing_remembered_value_refuses_retirement_and_preserves_both_user_overrides(self):
        self.card()
        self.install()
        self.override_owned_user_values()
        good = self.ownership_payload()
        self.card(scope=self.scope(expiry='2000-01-01'))
        for target, section, key in (
                (self.settings, host_native.SECTION_ENV, 'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'),
                (self.config, host_native.SECTION_CONFIG, 'agents.max_concurrent_threads_per_session')):
            with self.subTest(host=target.parent.name):
                bad = copy.deepcopy(good)
                del bad['targets'][host_native._key(target)][section][key]['value']
                self.write_ownership_payload(bad)
                # Both actual check/apply must refuse without touching any target,
                # handwritten rule, or the exact malformed ownership-state bytes.
                self.assert_refuses_preserving('UNVERIFIED ownership state')

    def test_valid_remembered_values_disown_later_user_overrides_on_retirement(self):
        self.card()
        self.install()
        self.override_owned_user_values()
        before_settings = json.loads(self.settings.read_text(encoding='utf-8'))
        before_config = tomllib.loads(self.config.read_text(encoding='utf-8'))
        self.card(scope=self.scope(expiry='2000-01-01'))
        checked, report = self.call(host_native.check)
        self.assertEqual(checked[1], 0, report)
        applied, report = self.call()
        self.assertEqual(applied[1], 0, report)
        settings = json.loads(self.settings.read_text(encoding='utf-8'))
        config = tomllib.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(settings['env'], before_settings['env'])
        self.assertEqual(settings['hooks'], before_settings['hooks'])
        self.assertEqual(settings['theme'], before_settings['theme'])
        self.assertEqual(config, before_config)
        self.assertIn('# user-selected, preserve comment', self.config.read_text(encoding='utf-8'))
        self.assertEqual(self.user_rules.read_text(encoding='utf-8'), USER_RULES)
        self.assertFalse(self.rules.exists())
        state = host_native.load_state(self.home)
        for target, section, key in (
                (self.settings, host_native.SECTION_ENV, 'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'),
                (self.config, host_native.SECTION_CONFIG, 'agents.max_concurrent_threads_per_session')):
            self.assertNotIn(key, (state.get(host_native._key(target)) or {}).get(section, {}))

    def test_nested_ownership_record_shape_refuses_in_band_for_both_hosts(self):
        self.card()
        self.install()
        good = self.ownership_payload()
        for target, section, key in (
                (self.settings, host_native.SECTION_ENV, 'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'),
                (self.settings, host_native.SECTION_DENY, 'Bash(git clean:*)'),
                (self.config, host_native.SECTION_CONFIG, 'agents.max_concurrent_threads_per_session')):
            for invalid in ('corrupt-record', [], 1, True, {}):
                with self.subTest(host=target.parent.name, section=section, invalid=invalid):
                    bad = copy.deepcopy(good)
                    bad['targets'][host_native._key(target)][section][key] = invalid
                    self.write_ownership_payload(bad)
                    self.assert_refuses_preserving('UNVERIFIED ownership state')

    def test_ownership_target_kind_schema_refuses_unknown_or_missing_kind(self):
        self.card()
        self.install()
        good = self.ownership_payload()
        for target in (self.settings, self.config, self.rules):
            for invalid in ('unknown-kind', 1, None):
                with self.subTest(host=target.parent.name, invalid=invalid):
                    bad = copy.deepcopy(good)
                    record = bad['targets'][host_native._key(target)]
                    if invalid is None:
                        del record['kind']
                    else:
                        record['kind'] = invalid
                    self.write_ownership_payload(bad)
                    self.assert_refuses_preserving('UNVERIFIED ownership state')

    def test_ownership_section_schema_refuses_non_dictionary_values(self):
        self.card()
        self.install()
        good = self.ownership_payload()
        for target, section in (
                (self.settings, host_native.SECTION_ENV),
                (self.settings, host_native.SECTION_DENY),
                (self.config, host_native.SECTION_CONFIG),
                (self.rules, host_native.SECTION_RULES)):
            for invalid in ([], 'corrupt-section', True):
                with self.subTest(host=target.parent.name, section=section, invalid=invalid):
                    bad = copy.deepcopy(good)
                    bad['targets'][host_native._key(target)][section] = invalid
                    self.write_ownership_payload(bad)
                    self.assert_refuses_preserving('UNVERIFIED ownership state')

    def test_ownership_entry_owned_schema_requires_actual_boolean(self):
        self.card()
        self.install()
        good = self.ownership_payload()
        for target, section, key in (
                (self.settings, host_native.SECTION_ENV, 'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'),
                (self.settings, host_native.SECTION_DENY, 'Bash(git clean:*)'),
                (self.config, host_native.SECTION_CONFIG, 'agents.max_concurrent_threads_per_session')):
            for invalid in ('true', 1, None):
                with self.subTest(host=target.parent.name, section=section, invalid=invalid):
                    bad = copy.deepcopy(good)
                    record = bad['targets'][host_native._key(target)][section][key]
                    if invalid is None:
                        del record['owned']
                    else:
                        record['owned'] = invalid
                    self.write_ownership_payload(bad)
                    self.assert_refuses_preserving('UNVERIFIED ownership state')

    def test_ownership_value_schema_refuses_unrenderable_values(self):
        self.card()
        self.install()
        good = self.ownership_payload()
        for target, section, key in (
                (self.settings, host_native.SECTION_ENV, 'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'),
                (self.config, host_native.SECTION_CONFIG, 'agents.max_concurrent_threads_per_session')):
            with self.subTest(host=target.parent.name):
                bad = copy.deepcopy(good)
                bad['targets'][host_native._key(target)][section][key]['value'] = None
                self.write_ownership_payload(bad)
                self.assert_refuses_preserving('UNVERIFIED ownership state')

    def test_ownership_created_schema_refuses_wrong_container_or_member_types(self):
        self.card()
        self.install()
        good = self.ownership_payload()
        for target in (self.settings, self.config):
            for invalid in ('env', {}, [1], [None]):
                with self.subTest(host=target.parent.name, invalid=invalid):
                    bad = copy.deepcopy(good)
                    bad['targets'][host_native._key(target)]['created'] = invalid
                    self.write_ownership_payload(bad)
                    self.assert_refuses_preserving('UNVERIFIED ownership state')
        for target in (self.settings, self.config, self.rules):
            for invalid in ('true', 1):
                with self.subTest(host=target.parent.name, created_file=invalid):
                    bad = copy.deepcopy(good)
                    bad['targets'][host_native._key(target)]['created_file'] = invalid
                    self.write_ownership_payload(bad)
                    self.assert_refuses_preserving('UNVERIFIED ownership state')

    def test_valid_deny_ownership_without_value_is_preserved_as_legal_format(self):
        self.card()
        self.install()
        state = self.ownership_payload()
        deny = state['targets'][host_native._key(self.settings)][host_native.SECTION_DENY]['Bash(git clean:*)']
        self.assertNotIn('value', deny)
        self.assertIs(deny['owned'], True)
        self.assertEqual(self.call(host_native.check)[0], (0, 0))
        self.assertEqual(self.call()[0], (0, 0))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
