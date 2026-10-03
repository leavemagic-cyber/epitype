"""Actual writer publication contracts; all originals here are synthetic records."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import copy
import hashlib
import io
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from epitype import capture, harvest, memspec, rule_pipeline, rule_provenance, starter
from rule_source_fixtures import source_bound_card


def fixture_card(text, path, *, source_root, case_id='writer', host='codex'):
    """A real original declares scope for this temp host; no gate is mocked."""
    bound = source_bound_card(text, path, source_root=source_root, case_id=case_id, host=host)
    fields, _ = memspec.frontmatter_text(bound)
    document = json.loads(fields[rule_provenance.FIELD])
    source = Path(document['sources'][0]['path'])
    meta, raw = source.read_bytes().splitlines(keepends=True)
    record = json.loads(raw)
    scope_quote = '此隔離合成測試 host 內全域所有動作：採用以下 AI 整理，保留全部原句及上下文。'
    message = record['message'] if host == 'claude' else record['payload']
    block = message['content'][0]
    # The declared human half follows all preserved AI context. In an arrow
    # reply, text before the last marker cannot stand in for the owner's words.
    prefix = '待發布的合成測試 AI 資料（完整保留）：\n' + text
    constraint = document.get('restriction_evidence')
    if constraint:
        observation = constraint['basis'][0]
        prefix += '\n合成測試已核的候選：'+observation['quote']+' '+observation['observed_at']+' '+observation['valid_until']
    block['text'] = prefix+'\nOwner 原句：'+document['quote']+'\n'+scope_quote
    raw = (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
    reviewed_source = source.with_name('writer-' + source.name)
    expected = meta + raw
    if reviewed_source.exists():
        if reviewed_source.read_bytes() != expected:
            raise ValueError('synthetic reviewed source identity collision')
    else:
        reviewed_source.write_bytes(expected)
    document['sources'][0]['path'] = str(reviewed_source)
    document['sources'][0]['record_sha256'] = hashlib.sha256(raw).hexdigest()
    if constraint:
        constraint['basis'][0]['source'] = dict(document['sources'][0])
    document['scope'] = {'level': 'global', 'project': None, 'case': None,
        'actions': ['*'], 'conditions': [], 'exceptions': [], 'expiry': 'unspecified'}
    document['scope_basis']['quote'] = scope_quote
    document = rule_pipeline.bind_payload(document, text, reviewer='synthetic-writer-reviewer',
                                         reviewed_at=document['scope_basis']['reviewed_at'])
    return text.replace('---\n', '---\nrule_provenance: '
        + json.dumps(rule_provenance.serialize(document), ensure_ascii=False) + '\n', 1)


def fixture_capture_event(found, vault, event, replay, *, source_root, host='codex'):
    kind, body, summary, digest_source = found
    digest = capture.grant_digest(digest_source)
    event_id, origin, _session = capture.event_identity(event, digest)
    stamp = replay.stamp
    name = f"{kind}-{stamp[:10].replace('-', '')}-{digest}-{event_id}"
    label = 'owner ruling auto-captured' if kind == 'ruling' else capture.CAPTURE_KINDS[kind][2]
    text = capture.capture_card_text(name, label, body, summary, stamp, event, event_id, origin, replay)
    bound = fixture_card(text, Path(vault) / f'{name}.md', source_root=source_root,
                         case_id=name, host=host)
    fields, _ = memspec.frontmatter_text(bound)
    result = dict(event)
    result[rule_provenance.FIELD] = json.loads(fields[rule_provenance.FIELD])
    return result


def fixture_starter_directory(root):
    """Review exact shipped payloads in a separate synthetic distribution."""
    directory = Path(root) / 'reviewed-templates'
    directory.mkdir()
    shipped = Path(starter.__file__).resolve().parent / memspec.STARTER_DIRECTORY
    for source in sorted(shipped.glob('*.md')):
        text = fixture_card(source.read_text(encoding='utf-8-sig'), directory / source.name,
                            source_root=Path(root) / 'sources', case_id=source.stem)
        (directory / source.name).write_text(text, encoding='utf-8')
    return directory


class WriterSources(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='epitype-writer-source-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.source_root = self.root / 'originals'
        self.event = {'cwd': str(self.root), 'session_id': 'synthetic-writer-session'}
        self.replay = capture.Replay(stamp='2026-10-03T00:00:00Z')
        self.prompt = '不要再亂改介面'

    def capture(self, event=None, body=None):
        if body is None:
            return capture.capture_event(self.prompt, self.vault, event or self.event,
                                         None, replay=self.replay)
        kind = 'correction'
        return capture.write_capture(self.vault, memspec.CORRECTION_DIRECTORY, kind,
            capture.grant_digest(body), capture.CAPTURE_KINDS[kind][2], body,
            event or self.event, None, replay=self.replay, source_text=self.prompt)

    def reviewed_event(self, *, host='codex', body=None):
        found = capture.classify(self.prompt)
        if body is not None:
            found = ('correction', body, body, body)
        return fixture_capture_event(found, self.vault, self.event, self.replay,
                                      source_root=self.source_root, host=host)

    def mutate_source(self, event, *, role='assistant'):
        document = event[rule_provenance.FIELD]
        source = Path(document['sources'][0]['path'])
        meta, raw = source.read_bytes().splitlines(keepends=True)
        record = json.loads(raw)
        record['payload']['role'] = role
        raw = (json.dumps(record, ensure_ascii=False) + '\n').encode()
        source.write_bytes(meta + raw)
        # Updating the literal hash must still not promote an assistant record.
        document['sources'][0]['record_sha256'] = hashlib.sha256(raw).hexdigest()

    def test_capture_shape_without_original_stays_pending(self):
        self.assertTrue(capture.auto_admitted(self.prompt)[0])
        path = self.capture()
        self.assertEqual(self.replay.status, capture.STATUS_PENDING)
        self.assertIn('captured_pending', path.parts)
        self.assertIn(self.prompt, path.read_text(encoding='utf-8'))
        self.assertNotIn('governance-core', path.read_text(encoding='utf-8'))

    def test_capture_real_source_both_hosts_publishes_exact_context(self):
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                self.event['session_id'] = 'synthetic-' + host
                event = self.reviewed_event(host=host)
                path = self.capture(event)
                self.assertEqual(self.replay.status, capture.STATUS_WRITTEN)
                self.assertEqual(path.parent, self.vault / memspec.CORRECTION_DIRECTORY)
                self.assertEqual(rule_pipeline.admission("", path.read_text(encoding='utf-8'),
                                                        'decision', path), [])

    def test_capture_scope_label_does_not_widen_source_or_change_cwd_routing(self):
        from epitype import card_lint, capture_route
        found = capture.classify(self.prompt)
        kind, body, summary, digest_source = found
        digest = capture.grant_digest(digest_source)
        event_id, origin, _ = capture.event_identity(self.event, digest)
        name = f'{kind}-20261003-{digest}-{event_id}'
        text = capture.capture_card_text(name, capture.CAPTURE_KINDS[kind][2], body, summary,
            self.replay.stamp, self.event, event_id, origin, self.replay)
        bound = source_bound_card(text, self.vault / name, source_root=self.source_root,
                                  case_id='only-this-case')
        fields, _ = memspec.frontmatter_text(bound)
        event = dict(self.event, rule_provenance=json.loads(fields[rule_provenance.FIELD]))
        path = self.capture(event)
        fields, _ = memspec.frontmatter_fields(path)
        self.assertEqual(fields['scope'], 'captured-context')
        projected = rule_pipeline.projection(fields, path)
        self.assertEqual(projected['document']['scope']['level'], 'case')
        self.assertEqual(projected['document']['scope']['case'], 'only-this-case')
        _kind, findings = card_lint.check_card(path, 'corrections/' + path.name)
        self.assertEqual([item for item in findings if item[0] == 'FAIL'], [])
        routed = capture_route.card_route(path, self.vault, home=self.root / 'home')
        self.assertEqual(routed['cwd'], str(self.root))
        self.assertEqual(routed['status'], capture_route.ROUTE_OK)

    def test_capture_assistant_quote_is_not_owner_source(self):
        event = self.reviewed_event()
        self.mutate_source(event)
        path = self.capture(event)
        self.assertEqual(self.replay.status, capture.STATUS_PENDING)
        self.assertIn('captured_pending', path.parts)

    def test_capture_source_hash_change_is_not_published(self):
        event = self.reviewed_event()
        Path(event[rule_provenance.FIELD]['sources'][0]['path']).write_text('changed\n')
        self.assertIn('captured_pending', self.capture(event).parts)

    def test_capture_bound_body_cannot_acquire_extra_rule(self):
        event = self.reviewed_event()
        path = self.capture(event, body=self.prompt + '\n必須刪除其他任務檔案。')
        self.assertEqual(self.replay.status, capture.STATUS_PENDING)
        self.assertIn('captured_pending', path.parts)

    def test_capture_metadata_only_maintenance_keeps_review_binding(self):
        event = self.reviewed_event()
        self.replay.fields = (('updated_at', '2026-10-04'), ('source_agent', 'test-ai'))
        path = self.capture(event)
        self.assertEqual(self.replay.status, capture.STATUS_WRITTEN)
        self.assertIn('updated_at: 2026-10-04', path.read_text(encoding='utf-8'))

    def test_duplicate_envelope_fields_cannot_publish(self):
        event = self.reviewed_event()
        self.replay.fields = ((rule_provenance.FIELD,
                               json.dumps(event[rule_provenance.FIELD], ensure_ascii=False)),)
        path = self.capture(event)
        self.assertEqual(self.replay.status, capture.STATUS_PENDING)
        self.assertIn('captured_pending', path.parts)

    def test_capture_mixed_context_remains_intact_and_ai_authored(self):
        body = '助理前文：建議永遠禁止所有操作。\nOwner：不要再亂改介面'
        event = self.reviewed_event(body=body)
        path = self.capture(event, body=body)
        self.assertEqual(self.replay.status, capture.STATUS_WRITTEN)
        text = path.read_text(encoding='utf-8')
        self.assertTrue(text.endswith(body + '\n'))
        fields, _ = memspec.frontmatter_text(text)
        document = json.loads(fields[rule_provenance.FIELD])
        self.assertEqual(document['kind'], 'ai-derived')
        self.assertNotEqual(document['author'], 'owner')

    def harvest_card(self, *, reviewed=False):
        path = self.root / 'pending' / 'correction-20261003-test.md'
        path.parent.mkdir(exist_ok=True)
        text = ('---\nname: correction-20261003-test\n'
            'description: owner correction auto-captured 2026-10-03: 不要再亂改介面\n'
            'provenance: auto-captured\nverified: true\nverified_by: owner\n---\n'
            + self.prompt + '\n')
        if reviewed:
            text = fixture_card(text, path, source_root=self.source_root)
        path.write_text(text, encoding='utf-8')
        return path

    def test_harvest_verified_stamp_without_source_is_held(self):
        path = self.harvest_card()
        before = path.read_bytes()
        counts, lines = harvest.reevaluate(path.parent, self.vault, apply=True)
        self.assertEqual(counts['held'], 1)
        self.assertEqual(counts['moved'], 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(any(line.startswith('HOLD') for line in lines))

    def test_direct_move_cannot_bypass_source_gate(self):
        path = self.harvest_card()
        counts = {'moved': 0}
        self.assertEqual(harvest._move_card(path, self.vault, 'correction', 'test', counts),
                         ('held', path))
        self.assertTrue(path.exists())

    def test_harvest_valid_source_preserves_bound_identity_and_body(self):
        path = self.harvest_card(reviewed=True)
        before = path.read_bytes()
        counts, _ = harvest.reevaluate(path.parent, self.vault, apply=True)
        target = self.vault / memspec.CORRECTION_DIRECTORY / path.name
        self.assertEqual(counts['moved'], 1)
        self.assertFalse(path.exists())
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(rule_pipeline.admission("", target.read_text(encoding='utf-8'),
                                                'decision', target), [])

    def test_harvest_invalid_body_and_source_stay_original(self):
        path = self.harvest_card(reviewed=True)
        path.write_text(path.read_text(encoding='utf-8') + '禁止額外行為。\n', encoding='utf-8')
        before = path.read_bytes()
        counts, _ = harvest.reevaluate(path.parent, self.vault, apply=True)
        self.assertEqual(counts['held'], 1)
        self.assertEqual(path.read_bytes(), before)

    def test_harvest_assistant_record_even_with_fresh_hash_is_held(self):
        path = self.harvest_card(reviewed=True)
        fields, _ = memspec.frontmatter_fields(path)
        document = json.loads(fields[rule_provenance.FIELD])
        old = fields[rule_provenance.FIELD]
        self.mutate_source({rule_provenance.FIELD: document})
        path.write_text(path.read_text(encoding='utf-8').replace(old,
                        json.dumps(document, ensure_ascii=False)), encoding='utf-8')
        counts, _ = harvest.reevaluate(path.parent, self.vault, apply=True)
        self.assertEqual(counts['held'], 1)
        self.assertTrue(path.exists())

    def test_harvest_historical_marker_cannot_hide_execution_or_owner_claim(self):
        for extra in ('owner_quote: 不要再亂改介面\n', 'forbidden: [bad]\n'):
            with self.subTest(extra=extra):
                path = self.root / 'event.md'
                path.write_text('---\nname: event\nauto: true\n' + extra
                                + '---\nHistorical event.\n', encoding='utf-8')
                outcome, _ = harvest._move_card(path, self.vault, 'grant', 'plain', {'moved': 0})
                self.assertEqual(outcome, 'held')
                self.assertTrue(path.exists())

    def test_harvest_same_identity_maintenance_does_not_republish_legacy(self):
        directory = self.vault / memspec.CORRECTION_DIRECTORY
        directory.mkdir()
        path = directory / 'correction-20261003-test.md'
        path.write_text('---\nname: old\n---\nlegacy\n')
        before = path.read_bytes()
        self.assertEqual(harvest._move_card(path, self.vault, 'correction', 'test', {'moved': 0}),
                         ('unchanged', path))
        self.assertEqual(path.read_bytes(), before)

    def test_harvest_plain_event_maintenance_remains_available(self):
        path = self.root / 'event.md'
        path.write_text('---\nname: event\nauto: true\n---\nHistorical event.\n')
        counts = {'moved': 0}
        outcome, target = harvest._move_card(path, self.vault, 'correction', 'plain', counts)
        self.assertEqual(outcome, 'historical')
        self.assertTrue(target.exists())
        self.assertIn('_drafts', target.parts)
        self.assertEqual(counts['moved'], 0)

    def test_verified_historical_directive_is_never_injected_as_owner_rule(self):
        from epitype import memsearch, owner_rules
        sys.path.insert(0, str(ROOT / 'adapters' / 'claude'))
        import stop_gate
        path = self.root / 'historical.md'
        text = ('---\nname: historical\nauto: true\nverified: true\nverified_by: owner\n'
                'description: 不要再亂改介面 historyneedle\n---\n'
                '不要再亂改介面，必須保留所有原話 historyneedle。\n')
        path.write_text(text, encoding='utf-8')
        outcome, target = harvest._move_card(path, self.vault, 'ruling', 'history', {'moved': 0})
        self.assertEqual(outcome, 'historical')
        self.assertEqual(target.read_text(encoding='utf-8'), text)
        config = self.root / 'config.json'
        config.write_text(json.dumps({'vaults': [str(self.vault)]}))
        with patch.dict('os.environ', {'EPITYPE_CONFIG': str(config), 'HOME': str(self.root / 'home'),
                                      'USERPROFILE': str(self.root / 'home')}):
            self.assertEqual(list(memsearch.card_files(self.vault)), [])
            memsearch.build_index(self.vault)
            self.assertEqual(memsearch.recall_index(self.vault, 'historyneedle')['count'], 0)
            self.assertEqual(stop_gate._decisions(self.vault, time.monotonic(), []), [])
            self.assertNotIn('historyneedle', owner_rules.render([self.vault]))

    def test_harvest_dry_run_and_apply_agree_when_source_is_missing(self):
        home = self.root / 'home'
        directory = home / '.claude' / 'projects' / 'C--Synthetic'
        directory.mkdir(parents=True)
        (directory / 'session.jsonl').write_text(json.dumps({'type': 'user', 'sessionId': 'synthetic',
            'timestamp': '2026-10-03T00:00:00Z', 'cwd': str(self.root),
            'message': {'role': 'user', 'content': self.prompt}}, ensure_ascii=False) + '\n', encoding='utf-8')
        import contextlib
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            planned, _, _ = harvest.harvest(home, [self.vault], dry_run=True)
            actual, _, _ = harvest.harvest(home, [self.vault])
        self.assertEqual(planned['corrections'], 0)
        self.assertEqual(actual['corrections'], 0)
        self.assertEqual(planned['drafts'], 1)
        self.assertEqual(actual['drafts'], 1)
        self.assertIn('WOULD PROPOSE', output.getvalue())

    def test_actual_shipped_templates_are_candidates_not_owner_rulings(self):
        cards = starter.shipped_cards()
        output = io.StringIO()
        self.assertEqual(starter._install(self.vault, cards, False, output), ([], []))
        self.assertEqual(list(self.vault.glob('*.md')), [])
        for source in cards:
            self.assertEqual(self.vault.joinpath(*starter.CANDIDATE_SUBPATH, source.name).read_bytes(),
                             source.read_bytes())
        self.assertIn('AI template; source UNVERIFIED; unpublished', output.getvalue())

    def test_starter_valid_sources_publish_and_pristine_remove_preserves_edits(self):
        directory = fixture_starter_directory(self.root)
        cards = sorted(directory.glob('*.md'))
        written, skipped = starter._install(self.vault, cards, False, io.StringIO())
        self.assertEqual(written, [path.name for path in cards])
        self.assertEqual(skipped, [])
        edited = self.vault / cards[0].name
        edited.write_bytes(edited.read_bytes() + b'\nmy note\n')
        removed, kept = starter._remove(self.vault, cards, False, io.StringIO())
        self.assertEqual(removed, [path.name for path in cards[1:]])
        self.assertEqual(kept, [cards[0].name])

    def test_starter_invalid_payload_or_assistant_source_never_publishes(self):
        directory = fixture_starter_directory(self.root)
        cards = sorted(directory.glob('*.md'))
        cards[0].write_text(cards[0].read_text(encoding='utf-8') + '\n必須其他限制。\n', encoding='utf-8')
        fields, _ = memspec.frontmatter_fields(cards[1])
        event = {rule_provenance.FIELD: json.loads(fields[rule_provenance.FIELD])}
        self.mutate_source(event)
        # Stale hash and role independently invalidate the original.
        written, _ = starter._install(self.vault, cards, False, io.StringIO())
        self.assertNotIn(cards[0].name, written)
        self.assertNotIn(cards[1].name, written)

    def test_starter_nonrule_and_unchanged_foreign_files_are_preserved(self):
        source = self.root / 'plain.md'
        source.write_text('---\nname: plain\nmetadata:\n  type: reference\n---\nA plain reference.\n')
        written, _ = starter._install(self.vault, [source], False, io.StringIO())
        self.assertEqual(written, ['plain.md'])
        target = self.vault / source.name
        target.write_text('Owner changed this.\n')
        before = target.read_bytes()
        self.assertEqual(starter._install(self.vault, [source], False, io.StringIO()),
                         ([], ['plain.md']))
        self.assertEqual(target.read_bytes(), before)

    def test_starter_candidate_dry_run_leaves_no_payload(self):
        starter._install(self.vault, starter.shipped_cards(), True, io.StringIO())
        self.assertEqual(list(self.vault.rglob('*')), [])


if __name__ == '__main__':
    sys.argv = [arg for arg in sys.argv if arg != '--selftest']
    unittest.main()
