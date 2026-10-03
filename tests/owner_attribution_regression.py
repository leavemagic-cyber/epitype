import sys; sys.dont_write_bytecode = True
"""Replay the incident's exact sentences and authority-admission boundaries."""
import copy
import hashlib
from datetime import datetime, timedelta, timezone
import json
import os
import shlex
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'adapters' / 'claude')]
from epitype import capture, compact_map, rule_provenance as provenance
import _hook_common as common
import pretooluse_gate as pretool
import stop_gate as stop

ASSISTANT_WORDS = '大量重掃照原計畫做成 Colab 工作，由我來跑，所以校準資料不用放到 lobshsu'
OWNER_WORDS = '能用dot就用dot'
REAL_TRANSCRIPT = None
if '--real-transcript' in sys.argv:
    index = sys.argv.index('--real-transcript')
    REAL_TRANSCRIPT = Path(sys.argv[index + 1])
    del sys.argv[index:index + 2]
if '--selftest' in sys.argv:
    sys.argv.remove('--selftest')


class OwnerAttributionRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='epitype-attribution-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / 'original.jsonl'
        self.rows = [
            {'type': 'assistant', 'sessionId': 'incident', 'uuid': 'proposal',
             'message': {'content': [{'type': 'text', 'text': ASSISTANT_WORDS + '；'}]}},
            {'type': 'user', 'sessionId': 'incident', 'uuid': 'reply',
             'message': {'content': ASSISTANT_WORDS + '；《' + OWNER_WORDS}},
        ]
        self.write_rows()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.config = self.root / 'config.json'
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {'EPITYPE_CONFIG': str(self.config),
            'HOME': str(self.root / 'home'), 'USERPROFILE': str(self.root / 'home')})
        environment.start()
        self.addCleanup(environment.stop)
        self.event = {'transcript_path': str(self.path), 'session_id': 'incident',
                      'cwd': str(self.root)}

    def write_rows(self):
        self.path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in self.rows), encoding='utf-8')

    def owner_claim(self):
        return {'kind': 'owner-directive', 'text': OWNER_WORDS, 'quote': OWNER_WORDS,
                'source': {'path': str(self.path), 'line': 2, 'message_id': 'reply'}}

    def marker(self, claim):
        return '<!-- EPITYPE CLAIM ' + json.dumps(claim, ensure_ascii=False) + ' -->'

    def limitation(self, timeout=30):
        now = datetime.now(timezone.utc)-timedelta(seconds=2)
        stamp, expiry = now.isoformat(), (now+timedelta(hours=1)).isoformat()
        quote = '前置檢查正常要15–25分鐘；已確認工作程序卡死；checkpoint已核可續跑；重做成本2分鐘；按checkpoint恢復'
        self.rows.append({'type': 'assistant', 'sessionId': 'incident', 'uuid': 'observed',
                          'message': {'content': [{'type': 'text', 'text': quote+' '+stamp+' '+expiry}]}})
        self.write_rows()
        return {'kind': 'ai-derived', 'author': '主控', 'challengeable': True,
                'text': f'前置檢查超過 {timeout} 分鐘且已確認工作程序卡死才取消', 'subject': '前置檢查',
                'cancel_condition': '已確認工作程序卡死', 'checkpoint': 'checkpoint已核可續跑',
                'redo_cost': '重做成本2分鐘', 'recovery': '按checkpoint恢復',
                'basis': [{'quote': quote, 'observed_at': stamp, 'valid_until': expiry,
                           'source': {'path': str(self.path), 'line': len(self.rows)}}]}

    def test_exact_incident_assistant_words_block_and_owner_words_pass(self):
        bad = provenance.claim_violation(f'owner 原話：「{ASSISTANT_WORDS}」', self.path)
        self.assertIn('role=A, L1', bad)
        self.assertIsNone(provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', self.path))

    def test_both_reply_markers_exclude_quoted_prefix(self):
        for marker in ('<', '《', '<-', '<='):
            text = ASSISTANT_WORDS + marker + OWNER_WORDS
            self.assertFalse(provenance._human_quote(text, ASSISTANT_WORDS))
            self.assertTrue(provenance._human_quote(text, OWNER_WORDS))
            self.assertEqual(capture.owner_reply_parts(text), (ASSISTANT_WORDS, OWNER_WORDS))

    def test_user_envelope_does_not_establish_authorship(self):
        self.rows = self.rows[1:]
        self.write_rows()
        self.assertIn('rejected', provenance.claim_violation(f'owner 原話：「{ASSISTANT_WORDS}」', self.path))

    def test_first_assistant_occurrence_blocks_plain_user_echo(self):
        self.rows[1]['message']['content'] = ASSISTANT_WORDS
        self.write_rows()
        self.assertIn('role=A', provenance.claim_violation(f'owner 裁定：「{ASSISTANT_WORDS}」', self.path))

    def test_compact_summary_is_not_an_original_human_source(self):
        self.rows.append({'type': 'user', 'isCompactSummary': True,
                          'message': {'content': f'owner 原話：「{ASSISTANT_WORDS}」'}})
        self.write_rows()
        self.assertIn('rejected', provenance.claim_violation(f'# owner 原話\n- 「{ASSISTANT_WORDS}」', self.path))

    def test_english_summary_and_relayed_ruling_labels_are_checked(self):
        for label in ('The owner has ruled', 'Owner instructions', 'owner 已要求', 'owner 授權'):
            self.assertIsNotNone(provenance.claim_violation(f'{label}:「{ASSISTANT_WORDS}」', self.path))
            self.assertIsNone(provenance.claim_violation(f'{label}:「{OWNER_WORDS}」', self.path))

    def test_asking_owner_to_decide_does_not_claim_a_past_ruling(self):
        self.assertIsNone(provenance.claim_violation('下一步請 owner 決定要不要合併。', self.path))

    def test_compact_map_separates_reply_and_citation(self):
        raw = self.path.read_bytes().splitlines()
        users, _ = compact_map._extract_candidates(list(enumerate(raw, 1)))
        self.assertIn('owner_reply='+OWNER_WORDS, users[0][2])
        self.assertIn('cited_text='+ASSISTANT_WORDS, users[0][2])

    def test_normal_assistant_quotation_of_owner_is_allowed_both_hosts(self):
        for codex in (False, True):
            if codex:
                self.rows = [{'type': 'session_meta', 'payload': {'id': 'incident'}},
                    {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                      'id': 'reply', 'content': [{'type': 'input_text', 'text': OWNER_WORDS}]}}]
                self.write_rows()
            event = {**self.event, 'last_assistant_message': f'owner 原話：「{OWNER_WORDS}」'}
            self.assertIsNone(stop._handle(event, time.monotonic(), []))

    def test_missing_original_and_budget_exhaustion_are_not_passes(self):
        self.assertIsNotNone(provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', self.root/'missing.jsonl'))
        self.assertEqual(provenance.first_authorship(self.path, [OWNER_WORDS], max_bytes=1)[OWNER_WORDS]['status'], provenance.UNVERIFIED)

    def test_real_write_requires_citation_and_checks_true_source(self):
        text = f'owner 原話：「{OWNER_WORDS}」'
        event = {**self.event, 'tool_name': 'Write',
                 'tool_input': {'file_path': str(self.root/'runbook.md'), 'content': text}}
        result = pretool._handle(event, time.monotonic(), [])
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        event['tool_input']['content'] += '\n'+self.marker(self.owner_claim())
        result = pretool._handle(event, time.monotonic(), [])
        self.assertNotEqual((result or {}).get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def test_false_citation_and_summary_citation_block(self):
        for line in (1, 3, 99):
            bad = self.owner_claim()
            bad['source']['line'] = line
            self.assertIsNotNone(provenance.claim_violation(self.marker(bad)))
        bad = self.owner_claim()
        bad['text'] = bad['quote'] = ASSISTANT_WORDS
        self.assertIsNotNone(provenance.claim_violation(self.marker(bad)))

    def test_normal_patch_and_dispatch_share_the_same_guard(self):
        text = f'owner 原話：「{ASSISTANT_WORDS}」'
        patch_text = '*** Begin Patch\n*** Add File: dispatch.md\n+'+text+'\n*** End Patch'
        for name, tool_input in (('apply_patch', {'input': patch_text}), ('Task', {'prompt': text}),
                                 ('collaboration.send_message', {'message': text})):
            result = pretool._handle({**self.event, 'tool_name': name, 'tool_input': tool_input}, time.monotonic(), [])
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_python_literal_writes_share_the_guard_and_reads_are_not_mistaken(self):
        target = self.root/'plan.md'
        for text, blocked in ((f'owner 原話：「{ASSISTANT_WORDS}」', True),
                              (f'owner 原話：「{OWNER_WORDS}」\n'+self.marker(self.owner_claim()), False)):
            source = f'from pathlib import Path\np=Path({str(target)!r})\np.write_text({text!r}, encoding="utf-8")'
            for command in ("python - <<'PY'\n"+source+'\nPY', 'python -c '+shlex.quote(source)):
                result = pretool._handle({**self.event, 'tool_name': 'Bash', 'tool_input': {'command': command}}, time.monotonic(), [])
                self.assertEqual((result or {}).get('hookSpecificOutput', {}).get('permissionDecision') == 'deny', blocked)
                self.assertFalse(target.exists(), 'preflight must not execute submitted code')
        command = 'python -c '+shlex.quote(f'print("owner 原話：{ASSISTANT_WORDS}")')
        self.assertIsNone(pretool._handle({**self.event, 'tool_name': 'Bash', 'tool_input': {'command': command}}, time.monotonic(), []))

    def test_python_replace_does_not_persist_false_attribution(self):
        target = self.root/'plan.md'
        target.write_text('一般計畫', encoding='utf-8')
        new = f'owner 原話：「{ASSISTANT_WORDS}」'
        source = f'from pathlib import Path\np=Path({str(target)!r})\ns=p.read_text(encoding="utf-8")\ns=s.replace("一般計畫", {new!r})\np.write_text(s, encoding="utf-8")'
        event = {**self.event, 'tool_name': 'Bash', 'tool_input': {'command': "python - <<'PY'\n"+source+'\nPY'}}
        self.assertEqual(pretool._handle(event, time.monotonic(), [])['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(target.read_text(encoding='utf-8'), '一般計畫')

    def test_python_path_alias_and_assigned_write_are_checked(self):
        text = f'owner 原話：「{ASSISTANT_WORDS}」'
        source = f'import pathlib as pl\np=pl.Path({str(self.root/"plan.md")!r})\nn=p.write_text({text!r})'
        event = {**self.event, 'tool_name': 'Bash', 'tool_input': {'command': 'python -c '+shlex.quote(source)}}
        self.assertEqual(pretool._handle(event, time.monotonic(), [])['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_unimported_path_method_is_not_assumed_to_be_a_file_write(self):
        source = f'query=Path("search.md")\nquery.write_text({ASSISTANT_WORDS!r})'
        self.assertEqual(pretool._literal_python_writes('python -c '+shlex.quote(source), self.root), [])

    def test_unknown_python_content_does_not_raise_or_exempt_literal_claims(self):
        for text, denied in (('ordinary report', False), (f'owner 原話：「{ASSISTANT_WORDS}」', True)):
            source = f'from pathlib import Path\np=Path("plan.md")\np.write_text({text!r}+generate())'
            event = {**self.event, 'tool_name': 'Bash', 'tool_input': {'command': 'python -c '+shlex.quote(source)}}
            result = pretool._handle(event, time.monotonic(), [])
            self.assertEqual((result or {}).get('hookSpecificOutput', {}).get('permissionDecision') == 'deny', denied)

    def test_python_control_flow_does_not_reuse_an_assumed_path(self):
        source = f'from pathlib import Path\np=Path("plan.md")\nif enabled:\n p=query\np.write_text({ASSISTANT_WORDS!r})'
        self.assertEqual(pretool._literal_python_writes('python -c '+shlex.quote(source), self.root), [])

    def test_python_negative_replace_count_respects_utf8_expansion_bound(self):
        (self.root/'plan.md').write_text('x'*100, encoding='utf-8')
        source = 'from pathlib import Path\np=Path("plan.md")\ns=p.read_text(encoding="utf-8").replace("x", "界界", -1)\np.write_text(s)'
        with patch.object(pretool.memspec, 'WRITE_GATE_MAX_CONTENT_BYTES', 1000):
            writes = pretool._literal_python_writes('python -c '+shlex.quote(source), str(self.root))
            self.assertEqual(writes[0][2], '界'*200)
        with patch.object(pretool.memspec, 'WRITE_GATE_MAX_CONTENT_BYTES', 400):
            writes = pretool._literal_python_writes('python -c '+shlex.quote(source), str(self.root))
            self.assertIsNone(writes[0][2], 'negative count replaces all, so UTF-8 expansion must remain bounded')

    def test_stop_rewrite_cannot_repeat_false_attribution(self):
        event = {**self.event, 'stop_hook_active': True,
                 'last_assistant_message': f'owner 原話：「{ASSISTANT_WORDS}」'}
        self.assertEqual(stop._handle(event, time.monotonic(), [])['decision'], 'block')

    def test_write_during_stop_rewrite_still_checks_authorship(self):
        event = {**self.event, 'stop_hook_active': True, 'tool_name': 'Write', 'tool_input': {
            'file_path': str(self.root/'plan.md'), 'content': f'owner 原話：「{ASSISTANT_WORDS}」'}}
        result = pretool._handle(event, time.monotonic(), [])
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_unreadable_old_file_does_not_exempt_a_new_false_claim(self):
        target = self.root/'plan.md'
        target.write_bytes(b'\xff')
        event = {**self.event, 'tool_name': 'Write', 'tool_input': {
            'file_path': str(target), 'content': f'owner 原話：「{ASSISTANT_WORDS}」'}}
        result = pretool._handle(event, time.monotonic(), [])
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        event['tool_input']['content'] = '一般進度'
        self.assertNotEqual((pretool._handle(event, time.monotonic(), []) or {}).get(
            'hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def test_multiple_citations_batch_one_original_prefix(self):
        second = copy.deepcopy(self.owner_claim())
        second['text'] = second['quote'] = OWNER_WORDS[:4]
        text = self.marker(self.owner_claim())+'\n'+self.marker(second)
        with patch.object(provenance, 'first_authorship', wraps=provenance.first_authorship) as scan:
            self.assertIsNone(provenance.claim_violation(text))
            self.assertEqual(scan.call_count, 1)

    def test_fenced_dispatch_does_not_bypass_authority_check(self):
        text = f'```\nowner 原話：「{ASSISTANT_WORDS}」\n```'
        self.assertIsNotNone(provenance.claim_violation(text, self.path))

    def test_fake_frontmatter_outside_a_memory_card_does_not_bypass(self):
        text = f'---\nowner 裁定：「{ASSISTANT_WORDS}」\n---\n'
        self.assertIsNotNone(provenance.claim_violation(text, self.path, require_citations=True))

    def test_tool_result_text_inside_user_envelope_is_not_owner(self):
        self.rows = [{'type': 'user', 'sessionId': 'incident', 'message': {'content': [
            {'type': 'tool_result', 'content': 'tool'}, {'type': 'text', 'text': OWNER_WORDS}]}}]
        self.write_rows()
        self.assertIsNotNone(provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', self.path))

    def test_english_restrictions_and_duration_units_use_the_same_admission(self):
        self.assertIsNotNone(provenance.claim_violation('policy: dot must not use calibration data'))
        claim = self.limitation(10)
        old = claim['basis'][0]['quote']
        new = old.replace('前置檢查正常要15–25分鐘；已確認工作程序卡死',
                          'preflight normally takes 15–25 Minutes; process is stuck')
        self.rows[-1]['message']['content'][0]['text'] = self.rows[-1]['message']['content'][0]['text'].replace(old, new)
        self.write_rows()
        claim['basis'][0]['quote'] = new
        claim.update(subject='preflight', cancel_condition='process is stuck',
                     text='new rule: cancel preflight after 10 minutes if process is stuck')
        self.assertIn('contradicts observed', provenance.claim_violation(self.marker(claim)))
        claim['text'] = 'new rule: cancel preflight after 30 minutes if process is stuck'
        self.assertIsNone(provenance.claim_violation(self.marker(claim)))

    def test_ten_minute_rule_without_evidence_blocks(self):
        self.assertIsNotNone(provenance.claim_violation('記下規則：前置檢查超過 10 分鐘就取消'))

    def test_ten_minute_rule_contradicting_real_normal_range_blocks(self):
        claim = self.limitation(10)
        self.assertIn('contradicts', provenance.claim_violation(self.marker(claim)))

    def test_source_bound_consistent_rule_passes(self):
        claim = self.limitation(30)
        text = '主控自訂、可挑戰：'+claim['text']+'\n'+self.marker(claim)
        self.assertIsNone(provenance.claim_violation(text, require_citations=True))

    def test_consistent_duration_alone_still_cannot_cancel_valid_work(self):
        claim = self.limitation(30)
        claim['text'] = '前置檢查超過30分鐘就取消'
        self.assertIn('elapsed time alone', provenance.claim_violation(self.marker(claim)))
        for key in ('checkpoint', 'redo_cost', 'recovery'):
            claim = self.limitation(30)
            del claim[key]
            self.assertIsNotNone(provenance.claim_violation(self.marker(claim)))

    def test_duration_can_trigger_diagnosis_without_justifying_cancellation(self):
        claim = self.limitation(30)
        claim['text'] = '前置檢查超過30分鐘啟動診斷'
        for key in ('cancel_condition', 'checkpoint', 'redo_cost', 'recovery'):
            del claim[key]
        self.assertIsNone(provenance.claim_violation(self.marker(claim)))

    def test_ai_label_alone_and_fabricated_dates_do_not_buy_a_pass(self):
        text = '主控自訂、可挑戰：前置檢查超過 10 分鐘就取消'
        self.assertIsNotNone(provenance.claim_violation(text, require_citations=True))
        claim = self.limitation()
        claim['basis'][0]['observed_at'] = '2001-01-01T00:00:00+00:00'
        self.assertIsNotNone(provenance.claim_violation(self.marker(claim)))

    def test_extra_prohibition_cannot_borrow_true_owner_words(self):
        text = f'owner 裁定：「{OWNER_WORDS}」，校準資料不交dot\n'+self.marker(self.owner_claim())
        self.assertIsNotNone(provenance.claim_violation(text, require_citations=True))

    def test_second_owner_claim_needs_its_own_evidence(self):
        text = f'owner 原話：「{OWNER_WORDS}」；owner 原話：「{ASSISTANT_WORDS}」\n'+self.marker(self.owner_claim())
        self.assertIsNotNone(provenance.claim_violation(text, require_citations=True))

    def test_heading_and_inline_metadata_cannot_hide_false_owner_words(self):
        for text in (f'# owner 原話：「{ASSISTANT_WORDS}」',
                     f'owner 原話：「{ASSISTANT_WORDS}」 '+self.marker(self.owner_claim())):
            self.assertIsNotNone(provenance.claim_violation(text, self.path))

    def test_new_owner_heading_cannot_promote_unchanged_assistant_text(self):
        before = f'# 討論文字\n- 「{ASSISTANT_WORDS}」'
        after = before.replace('討論文字', 'owner 原話')
        self.assertIsNotNone(provenance.claim_violation(after, self.path, previous=before))

    def test_a_valid_marker_cannot_hide_a_second_malformed_marker(self):
        text = self.marker(self.owner_claim())+'\n<!-- EPITYPE CLAIM broken -->'
        self.assertIn('malformed', provenance.claim_violation(text))

    def test_stop_true_quote_does_not_license_an_adjacent_prohibition(self):
        text = f'owner 原話：「{OWNER_WORDS}」，校準資料不交dot'
        self.assertIsNotNone(provenance.claim_violation(text, self.path))

    def test_actual_stop_and_write_entry_points_both_hosts(self):
        for arguments in ((), ('--codex',)):
            for quoted, blocked in ((ASSISTANT_WORDS, True), (OWNER_WORDS, False)):
                event = {**self.event, 'last_assistant_message': f'owner 原話：「{quoted}」'}
                result = common.run_synthetic(ROOT/'adapters/claude/stop_gate.py', event,
                                              self.config, arguments=arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout) if result.stdout.strip() else {}
                self.assertEqual(output.get('decision') == 'block', blocked, result.stderr)
            for claim_text, blocked in ((f'owner 裁定：「{ASSISTANT_WORDS}」', True),
                (f'owner 裁定：「{OWNER_WORDS}」\n'+self.marker(self.owner_claim()), False)):
                event = {**self.event, 'tool_name': 'Write', 'tool_input': {
                    'file_path': str(self.root/'dispatch.md'), 'content': claim_text}}
                result = common.run_synthetic(ROOT/'adapters/claude/pretooluse_gate.py', event,
                                              self.config, arguments=arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout) if result.stdout.strip() else {}
                self.assertEqual(output.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny',
                                 blocked, result.stderr)

    def test_other_quotes_on_same_line_are_not_misattributed(self):
        text = f'偏好「其他文字」已設好。owner 原話：「{OWNER_WORDS}」。按「設定」修改。'
        self.assertIsNone(provenance.claim_violation(text, self.path))

    def test_ratio_rule_without_comparable_data_is_not_a_repair(self):
        self.assertIsNotNone(provenance.claim_violation('我這邊的判斷：前置檢查超過預估一半以上就直接取消'))

    def test_historical_error_reporting_and_unmodified_legacy_are_allowed(self):
        text = f'錯誤紀錄：主控當時誤稱 owner 原話：「{ASSISTANT_WORDS}」'
        self.assertIsNone(provenance.claim_violation(text, require_citations=True))
        legacy = f'owner 原話：「{ASSISTANT_WORDS}」'
        self.assertIsNone(provenance.claim_violation(legacy+'\n一般進度', previous=legacy, require_citations=True))

    def test_editing_incident_does_not_rejudge_unchanged_rule_description(self):
        body = 'Run 按鈕區塊必須是一條完整的 PowerShell 指令。\n'
        before = '---\nname: feedback-run-button\ndescription: 2026-09-01 Run 按鈕區塊只能是一條指令\naliases: [Run 按鈕]\nunenforceable: 判斷型\nmetadata:\n  type: feedback\n---\n'+body
        after = before+'\n**再犯（2026-09-25）**：Run 區塊又拆成兩條指令。\n'
        self.assertIsNone(provenance.claim_violation(after, previous=before, require_citations=True))

    def test_a_source_changing_during_admission_still_blocks(self):
        read = provenance._ReadBudget.read
        appended = False
        def append_after_read(budget, stream, size, **kwargs):
            nonlocal appended
            result = read(budget, stream, size, **kwargs)
            if not appended:
                appended = True
                with self.path.open('ab') as original:
                    original.write(b'{}\n')
            return result
        with patch.object(provenance._ReadBudget, 'read', append_after_read):
            result = provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', self.path)
        self.assertIn('source-changed', result)

    @unittest.skipUnless(REAL_TRANSCRIPT, 'explicit original transcript required')
    def test_real_original_snapshot_record_replay(self):
        rows = {}
        snapshot = self.root/'incident-prefix.jsonl'
        # Preserve every original byte up to the reply, including the entire
        # earlier-authorship search space. Do not race the active logger during
        # two independent admission checks or keep a private payload artifact.
        with REAL_TRANSCRIPT.open('rb') as stream, snapshot.open('wb') as target:
            for number, raw in enumerate(stream, 1):
                target.write(raw)
                if number in (16845, 16875):
                    rows[number] = json.loads(raw)
                if number >= 16875:
                    break
        with REAL_TRANSCRIPT.open('rb') as stream:
            original_prefix = stream.read(snapshot.stat().st_size)
        self.assertEqual(hashlib.sha256(original_prefix).digest(),
                         hashlib.sha256(snapshot.read_bytes()).digest(),
                         'the frozen replay must equal the actual original prefix')
        assistant = provenance.source_record(rows[16845])
        owner = provenance.source_record(rows[16875])
        self.assertEqual(assistant[0], 'A')
        self.assertIn(ASSISTANT_WORDS, assistant[1])
        self.assertEqual(owner[0], 'U')
        self.assertFalse(provenance._human_quote(owner[1], ASSISTANT_WORDS))
        self.assertTrue(provenance._human_quote(owner[1], OWNER_WORDS))
        bad = provenance.claim_violation(f'owner 原話：「{ASSISTANT_WORDS}」', snapshot, seconds=5)
        good = provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', snapshot, seconds=5)
        self.assertIn('role=A, L16845', bad)
        self.assertIsNone(good)


if __name__ == '__main__':
    unittest.main()
