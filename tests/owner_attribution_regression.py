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
ADMISSION_SECONDS = 5.0
if '--admission-seconds' in sys.argv:
    index = sys.argv.index('--admission-seconds')
    ADMISSION_SECONDS = float(sys.argv[index + 1])
    del sys.argv[index:index + 2]
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

    def test_normal_stop_checks_aggregated_authority_once(self):
        event = {**self.event, 'last_assistant_message': f'owner 原話：「{OWNER_WORDS}」'}
        with patch.object(provenance, 'claim_violation', wraps=provenance.claim_violation) as check:
            self.assertIsNone(stop._handle(event, time.monotonic(), []))
        self.assertEqual(check.call_count, 1)

    def test_bad_authority_before_last_message_still_blocks(self):
        self.rows.extend([
            {'type': 'assistant', 'message': {'content': [
                {'type': 'text', 'text': f'owner 原話：「{ASSISTANT_WORDS}」'}]}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '一般進度。'}]}}])
        self.write_rows()
        event = {**self.event, 'last_assistant_message': '一般進度。'}
        self.assertEqual(stop._handle(event, time.monotonic(), [])['decision'], 'block')

    def test_missing_original_and_budget_exhaustion_are_not_passes(self):
        self.assertIsNotNone(provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', self.root/'missing.jsonl'))
        self.assertEqual(provenance.first_authorship(self.path, [OWNER_WORDS], max_bytes=1)[OWNER_WORDS]['status'], provenance.UNVERIFIED)

    def test_real_write_auto_verifies_true_quote_without_citation(self):
        text = f'owner 原話：「{OWNER_WORDS}」'
        event = {**self.event, 'tool_name': 'Write',
                 'tool_input': {'file_path': str(self.root/'runbook.md'), 'content': text}}
        result = pretool._handle(event, time.monotonic(), [])
        self.assertNotEqual((result or {}).get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
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

    def test_moving_existing_owner_heading_cannot_promote_unchanged_prose(self):
        before = f'# 討論文字\n- 「{ASSISTANT_WORDS}」\n# owner 原話\n- 「{OWNER_WORDS}」'
        after = f'# owner 原話\n- 「{ASSISTANT_WORDS}」\n# 討論文字\n- 「{OWNER_WORDS}」'
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

    def test_source_append_preserves_verified_stable_prefix(self):
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
        self.assertIsNone(result)

    def test_source_prefix_rewrite_during_admission_still_blocks(self):
        read = provenance._ReadBudget.read
        rewritten = False
        def rewrite_after_read(budget, stream, size, **kwargs):
            nonlocal rewritten
            result = read(budget, stream, size, **kwargs)
            if not rewritten:
                rewritten = True
                raw = self.path.read_bytes().replace(b'Colab', b'XXXXX')
                self.path.write_bytes(raw)
            return result
        with patch.object(provenance._ReadBudget, 'read', rewrite_after_read):
            result = provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', self.path)
        self.assertIn('source-changed', result)

    def test_incomplete_snapshot_tail_is_not_positive_evidence(self):
        self.path.write_text(json.dumps(self.rows[1], ensure_ascii=False)[:-2], encoding='utf-8')
        self.assertIsNotNone(provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', self.path))

    def test_complete_json_at_eof_without_newline_is_verified(self):
        # Message IDs are optional for automatic quotation lookup. An unrelated
        # payload field must not break the supported host-user message shape.
        row = {'type': 'user', 'payload': [], 'message': {'content': OWNER_WORDS}}
        self.path.write_text(json.dumps(row, ensure_ascii=False), encoding='utf-8')
        self.assertIsNone(provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', self.path,
                                                    hard_deadline=True))

    def test_daily_unannotated_sentences_pass_both_hosts(self):
        truth = '先改回用正常codex'
        self.rows.append({'type': 'user', 'sessionId': 'incident', 'uuid': 'daily',
                          'message': {'content': truth}})
        self.write_rows()
        cases = [('Write', 'owner 10-03「'+truth+'」'),
                 ('Write', 'API 每次最多 100 筆。'),
                 ('Task', '只改文件，禁止修改產品程式。'),
                 ('Write', '```python\nassert label == "owner 10-03"\n```'),
                 ('Stop', '你訂的餐廳已列入行程。'),
                 ('Stop', '以下為測試資料：\n```json\n{"label": "owner 10-03"}\n```')]
        for arguments in ((), ('--codex',)):
            for tool, text in cases:
                with self.subTest(host=arguments, tool=tool, text=text):
                    event = {**self.event, 'last_assistant_message': text} if tool == 'Stop' else {
                        **self.event, 'tool_name': tool, 'tool_input': {
                            'file_path': str(self.root/'daily.md'), 'content': text, 'prompt': text}}
                    script = 'stop_gate.py' if tool == 'Stop' else 'pretooluse_gate.py'
                    result = common.run_synthetic(ROOT/'adapters/claude'/script, event,
                                                  self.config, arguments=arguments)
                    output = json.loads(result.stdout) if result.stdout.strip() else {}
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotEqual(output.get('decision'), 'block', output)
                    self.assertNotEqual(output.get('hookSpecificOutput', {}).get('permissionDecision'),
                                        'deny', output)

    def test_cancel_directives_without_prompt_prefix_block_both_hosts(self):
        for arguments in ((), ('--codex',)):
            for text in ('前置檢查超過 10 分鐘沒有前進，就直接取消。',
                         '前置檢查超過預估一半以上就直接取消。'):
                result = common.run_synthetic(ROOT/'adapters/claude/stop_gate.py',
                    {**self.event, 'last_assistant_message': text}, self.config, arguments=arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['decision'], 'block', result.stdout)
        for text in ('API 每次最多 100 筆。', '前置檢查正常要15–25分鐘。',
                     'API 請求超過30秒，就直接取消。',
                     '昨天前置檢查超過10分鐘，已取消。'):
            self.assertIsNone(provenance.claim_violation(text))

    def test_true_quote_after_64mib_passes_but_earlier_assistant_still_blocks(self):
        padding = (json.dumps({'type': 'progress', 'data': 'x'*60000})+'\n').encode()
        with self.path.open('wb') as stream:
            while stream.tell() <= 65*1024*1024:
                stream.write(padding)
            for row in self.rows:
                stream.write((json.dumps(row, ensure_ascii=False)+'\n').encode())
        self.assertIsNone(provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', self.path,
                                                    hard_deadline=True))
        self.assertIn('role=A', provenance.claim_violation(f'owner 原話：「{ASSISTANT_WORDS}」',
                                                          self.path, hard_deadline=True))

    def test_repeated_markdown_returns_promptly_without_authority(self):
        old = '| a | b |\n'*20000
        target = self.root/'table.md'
        target.write_text(old, encoding='utf-8')
        event = {**self.event, 'tool_name': 'Edit', 'tool_input': {
            'file_path': str(target), 'old_string': old, 'new_string': '# 表格\n'+old}}
        start = time.monotonic()
        result = common.run_synthetic(ROOT/'adapters/claude/pretooluse_gate.py', event, self.config)
        self.assertLess(time.monotonic()-start, 2.0)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout) if result.stdout.strip() else {}
        self.assertNotEqual(output.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def test_hard_deadline_terminates_and_reaps_cpu_bound_worker(self):
        # Exercise an actually non-yielding CPU loop, rather than a fake clock.
        worker = [sys.executable, '-c', 'while True: pass']
        import subprocess
        processes = []
        popen = subprocess.Popen
        def launch_worker(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process
        start = time.monotonic()
        with patch.object(provenance, '_claim_worker_command', return_value=worker), \
             patch('subprocess.Popen', side_effect=launch_worker):
            reason = provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', self.path,
                                               seconds=0.3, hard_deadline=True)
        self.assertIn('deadline exhausted', reason)
        self.assertLess(time.monotonic()-start, 1.5)
        # run() owns kill + wait. Capture its real Popen object independently.
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll(), 'CPU worker must be exited and reaped')
        with patch.object(provenance, '_claim_worker_command', side_effect=AssertionError('no worker')):
            self.assertIsNone(provenance.claim_violation('API 每次最多 100 筆。',
                                                        seconds=0, hard_deadline=True))

    def test_cpu_timeout_on_code_example_does_not_invent_authority(self):
        worker = [sys.executable, '-c', 'while True: pass']
        with patch.object(provenance, '_claim_worker_command', return_value=worker):
            self.assertIsNone(provenance.claim_violation(
                '```python\nassert label == "owner 10-03"\n```',
                seconds=0.3, hard_deadline=True))
        self.assertIsNone(provenance.claim_violation(
            '```python\nassert label == "owner 10-03"\n```', seconds=0, hard_deadline=True))

    def test_repetitive_diff_with_real_authority_still_checks_new_claim(self):
        old = '| a | b |\n'*20000
        start = time.monotonic()
        self.assertIsNone(provenance.claim_violation(old+f'owner 原話：「{OWNER_WORDS}」',
            self.path, previous=old, hard_deadline=True))
        self.assertLess(time.monotonic()-start, 2.0)
        self.assertIsNotNone(provenance.claim_violation(old+f'owner 原話：「{ASSISTANT_WORDS}」',
            self.path, previous=old, hard_deadline=True))

    def review_source(self, arguments):
        truth, invented = '先改回用正常codex', '工作超過十分鐘必須取消'
        if arguments:
            self.rows = [{'type': 'session_meta', 'payload': {'id': 'incident'}}] + [
                {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                    'id': role, 'content': [{'type': 'output_text' if role == 'assistant'
                        else 'input_text', 'text': text}]}}
                for role, text in (('assistant', invented), ('user', truth))]
        else:
            self.rows = [{'type': role, 'sessionId': 'incident', 'uuid': role,
                'message': {'content': text}}
                for role, text in (('assistant', invented), ('user', truth))]
        self.write_rows()
        return truth, invented

    def check_surface(self, text, tool, arguments, blocked, previous='一般進度。', clock=None):
        target = self.root/'fix3.md'
        target.write_text(previous, encoding='utf-8')
        event = {**self.event, 'last_assistant_message': text} if tool == 'Stop' else {
            **self.event, 'tool_name': tool, 'tool_input': (
                {'prompt': text} if tool == 'Task' else
                {'file_path': str(target), 'old_string': previous, 'new_string': text}
                if tool == 'Edit' else {'file_path': str(target), 'content': text})}
        result = common.run_synthetic(ROOT/'adapters/claude'/(
            'stop_gate.py' if tool == 'Stop' else 'pretooluse_gate.py'),
            event, self.config, arguments=arguments, clock=clock)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout) if result.stdout.strip() else {}
        denied = output.get('decision') == 'block' or output.get(
            'hookSpecificOutput', {}).get('permissionDecision') == 'deny'
        self.assertEqual(denied, blocked, (tool, arguments, text, output, result.stderr))
        return output, result.stderr

    def test_fix3_all_rereview_counterexamples_all_surfaces_both_hosts(self):
        for arguments in ((), ('--codex',)):
            truth, invented = self.review_source(arguments)
            false = f'owner 原話：「{invented}」'
            cases = [(f'owner 原話：`{truth}`', False),
                     (f'owner 原話：``{truth}``', False),
                     (f'owner 原話：`{invented}`', True),
                     ('前置檢查已經超過 10 分鐘沒有前進，就直接取消。', True),
                     ('前置檢查已經超過預估一半以上就直接取消。', True),
                     ('昨天前置檢查已取消。\n前置檢查超過 10 分鐘沒有前進，就直接取消。', True),
                     (f'owner 原話：「{truth}」。`禁止修改產品程式。`', True)]
            cases += [(f'```{language}\n{false}\n```', True)
                      for language in ('', 'python', 'javascript', 'yaml')]
            for tool in ('Write', 'Edit', 'Task', 'Stop'):
                for text, blocked in cases:
                    with self.subTest(host=arguments, tool=tool, text=text):
                        self.check_surface(text, tool, arguments, blocked)

    def test_fix3_role_promotion_and_each_exemption_abuse(self):
        for arguments in ((), ('--codex',)):
            truth, invented = self.review_source(arguments)
            false = f'owner 原話：「{invented}」'
            sample = '```python\n"""\n'+false+'\n"""\n```'
            history = '錯誤紀錄：主控當時誤稱 '+false
            for before in (sample, '```javascript\n'+false+'\n```', history):
                with self.subTest(host=arguments, promotion=before):
                    self.check_surface(false, 'Edit', arguments, True, previous=before)
            pairs = [
                (sample, sample+'\n'+false),
                (f'```javascript\nconst label = "owner 10-03";\n```',
                 f'```javascript\nconst label = "owner 10-03";\n{false}\n```'),
                (history, history+'。'+false),
                (history, history+'，'+false),
                ('昨天前置檢查超過10分鐘，已取消。',
                 '昨天前置檢查已取消。前置檢查已經超過預估一半就直接取消。'),
                ('下一步請 owner 決定要不要合併。',
                 '下一步請 owner 決定。'+false),
                ('API 每次最多 100 筆。', '規則：工作程序只能跑10分鐘。'),
                ('只改文件，禁止修改產品程式。', 'owner 要求：「禁止修改產品程式。」'),
                ('你訂的餐廳已列入行程。', false),
                (f'引用資料：「{false}」。', f'引用資料：{false}'),
                ('```python\nassert label == "owner 10-03"\n```',
                 '```python\n# '+false+'\nassert label == "owner 10-03"\n```'),
            ]
            for ordinary, abuse in pairs:
                for tool in ('Write', 'Edit', 'Task', 'Stop'):
                    with self.subTest(host=arguments, tool=tool, exempt=ordinary):
                        self.check_surface(ordinary, tool, arguments, False)
                        self.check_surface(abuse, tool, arguments, True)
            # Unchanged legacy prose stays exempt; changing its authority does not.
            self.check_surface(false+'\n\n一般進度。', 'Edit', arguments, False, previous=false)
            self.check_surface('# owner 原話\n「'+invented+'」', 'Edit', arguments, True,
                               previous='# 討論\n「'+invented+'」')
            # Registered frontmatter is checked by card lint; an ordinary wrapper isn't.
            self.check_surface('---\n'+false+'\n---', 'Write', arguments, True)

    def test_fix3_zero_budget_uses_content_not_fence_language(self):
        truth, invented = self.review_source(())
        for language in ('', 'python', 'javascript', 'yaml', 'unknown'):
            for text in (f'owner 原話：「{invented}」',
                         '前置檢查已經超過 10 分鐘，就直接取消。'):
                reason = provenance.claim_violation(f'```{language}\n{text}\n```',
                    self.path, seconds=0, hard_deadline=True)
                self.assertIn('UNVERIFIED', reason, (language, text))
        self.assertIsNone(provenance.claim_violation(
            'API 每次最多 100 筆。', seconds=0, hard_deadline=True))

    def test_fix3_uncertain_role_warns_and_never_becomes_verified(self):
        text = '```custom\nemit!{"owner 10-03"}\n```'
        for arguments in ((), ('--codex',)):
            self.review_source(arguments)
            for tool in ('Write', 'Edit', 'Task', 'Stop'):
                with self.subTest(host=arguments, tool=tool):
                    output, diagnostic = self.check_surface(text, tool, arguments, False)
                    self.assertIn('UNVERIFIED', json.dumps(output)+diagnostic)
        for seconds in (0, 2):
            notices = []
            self.assertIsNone(provenance.claim_violation(text, seconds=seconds,
                hard_deadline=True, notices=notices))
            self.assertTrue(notices)
            self.assertIn('UNVERIFIED', notices[0])

    def test_fix3_classifier_exhaustion_warns_without_deadlocking_ordinary_work(self):
        notices = []
        with patch.object(provenance, '_role_units', side_effect=provenance._ReadBudgetExhausted):
            self.assertIsNone(provenance.claim_violation('owner 10-03', seconds=0,
                hard_deadline=True, notices=notices))
        self.assertIn('UNVERIFIED', notices[0])

    def test_fix3_reporting_requires_an_actual_report_and_keeps_heading_scope(self):
        false = f'owner 原話：「{ASSISTANT_WORDS}」'
        cancel = '前置檢查已經超過10分鐘，就直接取消。'
        for text, blocked in (
                ('錯誤紀錄：主控沒有誤稱 '+false, True),
                ('錯誤紀錄：主控當時誤稱「'+cancel+'」', False),
                ('錯誤紀錄：主控當時誤稱「'+cancel+'」。'+cancel, True),
                ('# owner 原話\n## 指示\n「'+ASSISTANT_WORDS+'」', True),
                ('# owner 原話\n## 指示\n「'+OWNER_WORDS+'」', False),
                ('當時前置檢查超過10分鐘就取消了。', False),
                ('昨天前置檢查超過10分鐘，已直接取消。', False),
                ('昨天前置檢查超過預估一半，已經立即取消。', False),
                ('當時前置檢查超過10分鐘就取消了。'+cancel, True)):
            with self.subTest(text=text):
                self.assertEqual(provenance.claim_violation(text, self.path) is not None, blocked)

    def test_fix3_registered_metadata_does_not_exempt_body_or_fake_wrappers(self):
        front = '---\nname: sample\ndescription: owner 原話：「範例資料」\nmetadata:\n  type: knowledge\n---\n'
        self.assertIsNone(provenance.claim_violation(front+'一般進度。', card_metadata=True))
        self.assertIsNotNone(provenance.claim_violation(front+f'owner 原話：「{ASSISTANT_WORDS}」',
            self.path, card_metadata=True))
        self.assertIsNotNone(provenance.claim_violation(front+'一般進度。', self.path))

    def test_fix3_expired_host_preserves_claim_decision_and_ordinary_work(self):
        for arguments in ((), ('--codex',)):
            truth, invented = self.review_source(arguments)
            for tool in ('Write', 'Edit', 'Task', 'Stop'):
                for text, blocked in ((f'```python\nowner 原話：「{invented}」\n```', True),
                                      ('```python\nassert label == "owner 10-03"\n```', False),
                                      ('API 每次最多 100 筆。', False)):
                    with self.subTest(host=arguments, tool=tool, text=text):
                        output, diagnostic = self.check_surface(text, tool, arguments, blocked,
                            clock=common.memspec.HOOK_CLOCK_EXPIRED)
                        if blocked:
                            self.assertIn('UNVERIFIED', json.dumps(output)+diagnostic)

    def test_fix3_examined_quotations_and_denials_are_local_roles(self):
        cancel = '前置檢查超過10分鐘就取消。'
        false = f'owner 原話：「{ASSISTANT_WORDS}」'
        for text, blocked in (
                ('拒絕測例：「'+cancel+'」', False),
                ('拒絕測例：「'+cancel+'」，以後照做。', True),
                ('不是 '+false, False),
                ('不是 '+false+'；'+false, True),
                ('不是 owner 原話：「'+cancel+'」', False),
                ('不是 owner 原話：「'+cancel+'」，以後照做。', True),
                ('下一步請 owner 決定。owner 已裁定：「'+ASSISTANT_WORDS+'」', True),
                ('範例：'+false, True)):
            with self.subTest(text=text):
                self.assertEqual(provenance.claim_violation(text, self.path) is not None, blocked)
        self.assertIsNone(provenance.claim_violation(
            '錯誤紀錄：主控當時誤稱 '+false+'，owner 原話：「'+OWNER_WORDS+'」', self.path))

    def test_fix3_claim_metadata_cannot_hide_a_second_cancel_rule(self):
        original = self.limitation(30)
        for second in ('前置檢查超過10分鐘且已確認工作程序卡死才取消',
                       '前置檢查超過預估一半以上就直接取消'):
            claim = copy.deepcopy(original)
            claim['text'] += '。'+second
            with self.subTest(second=second):
                self.assertIsNotNone(provenance.claim_violation(self.marker(claim)))
        claim = copy.deepcopy(original)
        claim['text'] += '。前置檢查超過35分鐘且已確認工作程序卡死才取消'
        self.assertIsNone(provenance.claim_violation(claim['text']+'\n'+self.marker(claim)))

    def test_fix3_large_syntax_at_zero_budget_warns_without_running_heavy_parser(self):
        text = '```python\n'+('label = "owner 10-03"\n'*2000)+'```'
        notices = []
        with patch.object(provenance, '_statement_data', side_effect=AssertionError('no reserve for parser')):
            self.assertIsNone(provenance.claim_violation(text, seconds=0,
                hard_deadline=True, notices=notices))
        self.assertIn('UNVERIFIED', notices[0])

    def fix4_pairs(self, truth, invented):
        false = f'owner 原話：「{invented}」'
        true = f'owner 原話：「{truth}」'
        # The nine independently failing variants from review 3, verbatim.
        return [
            ('multiline_truth', 'owner 原話：\n> 「'+truth+'」',
             'owner 原話：\n> 「'+invented+'」'),
            ('list_truth', '- owner 原話：\n  「'+truth+'」',
             '- owner 原話：\n  「'+invented+'」'),
            ('request_quoted_good', '請 owner 決定要不要採用「前置檢查超過10分鐘就取消」這項提案。',
             '請 owner 決定要不要採用「前置檢查超過10分鐘就取消」這項提案。現在就照做。'),
            ('multiline_time', '前置檢查超過 10 分鐘沒有前進，\n昨天已直接取消。',
             '前置檢查超過 10 分鐘沒有前進，\n就直接取消。'),
            ('multiline_subject', '前置檢查：\n超過 10 分鐘沒有前進，昨天已直接取消。',
             '前置檢查：\n超過 10 分鐘沒有前進，就直接取消。'),
            ('history_bad', '昨日工作超過 12 分鐘後，已經終止。\n今日繼續整理 API 文件。',
             '昨日工作超過 12 分鐘後，已經終止。\n前置檢查超過 12 分鐘，\n就直接取消。'),
            ('history_ratio_bad', '昨天已取消。\n前置檢查超過預估一半以上\n昨天已直接取消。',
             '昨天已取消。\n前置檢查超過預估一半以上\n就直接取消。'),
            ('mistake_adoption', '錯誤紀錄：曾經誤稱 '+false+'。',
             '錯誤紀錄：曾經誤稱 '+false+'，現在就照做。'),
            ('ordinary_separate_bad', true+'\n\n今日繼續整理 API 文件。',
             true+'\n\n因此禁止修改產品程式。'),
        ]

    def test_fix4_nine_review_variants_logical_units(self):
        truth, invented = self.review_source(())
        for name, ordinary, abuse in self.fix4_pairs(truth, invented):
            for text, blocked in ((ordinary, False), (abuse, True)):
                with self.subTest(name=name, text=text):
                    self.assertEqual(provenance.claim_violation(text, self.path) is not None, blocked)

    def test_fix4_nine_review_variants_all_surfaces_both_hosts(self):
        for arguments in ((), ('--codex',)):
            truth, invented = self.review_source(arguments)
            for name, ordinary, abuse in self.fix4_pairs(truth, invented):
                for tool in ('Write', 'Edit', 'Task', 'Stop'):
                    for text, blocked in ((ordinary, False), (abuse, True)):
                        with self.subTest(host=arguments, tool=tool, name=name, text=text):
                            self.check_surface(text, tool, arguments, blocked)

    def test_fix4_crossline_abuse_of_every_exemption(self):
        for arguments in ((), ('--codex',)):
            truth, invented = self.review_source(arguments)
            false = f'owner 原話：「{invented}」'
            pairs = [
                ('data', '```python\nsample = '+repr(false)+'\n```',
                 '```python\nsample = '+repr(false)+'\n```\nowner 原話：\n> 「'+invented+'」'),
                ('reported', '測例（預期拒絕）：「'+false+'」。',
                 '測例（預期拒絕）：「'+false+'」。\n\n現在就照做。'),
                ('history', '昨天前置檢查已取消。\n今天只改文件。',
                 '昨天前置檢查已取消。\n前置檢查超過 10 分鐘，\n就直接取消。'),
                ('request', '請 owner 決定是否採用「'+invented+'」這項提案。',
                 '請 owner 決定是否採用「'+invented+'」這項提案。\n現在就照做。'),
                ('ordinary', 'API 每次最多 100 筆。\n這次只改文件。',
                 'API 每次最多 100 筆。\nowner 原話：\n> 「'+invented+'」'),
            ]
            for name, ordinary, abuse in pairs:
                for tool in ('Write', 'Edit', 'Task', 'Stop'):
                    with self.subTest(host=arguments, tool=tool, exempt=name):
                        self.check_surface(ordinary, tool, arguments, False)
                        self.check_surface(abuse, tool, arguments, True)
            # Unchanged words become an active claim through a crossline heading.
            before = '# 討論\n> 「'+invented+'」'
            self.check_surface(before+'\n\n一般進度。', 'Edit', arguments, False, previous=before)
            self.check_surface('# owner 原話\n> 「'+invented+'」', 'Edit', arguments, True, previous=before)
            self.check_surface(false+'\n一般進度', 'Edit', arguments, False, previous=false)
            self.check_surface(false+'\n因此禁止修改產品程式。', 'Edit', arguments, True, previous=false)
            # Registered frontmatter remains linted; its body cannot inherit exemption.
            front = ('---\nname: fix4-reference\ndescription: owner 原話：「範例資料」\n'
                     'metadata:\n  type: knowledge\n---\n')
            self.assertIsNone(provenance.claim_violation(front+'一般進度。', self.path, card_metadata=True))
            self.assertIsNotNone(provenance.claim_violation(front+'owner 原話：\n> 「'+invented+'」',
                self.path, card_metadata=True))
            reference = self.vault/'reference-fix4.md'
            card_front = ('---\nname: reference-fix4\ndescription: 2026-10-03 參考資料\n'
                          'metadata:\n  type: reference\n---\n')
            for tool in ('Write', 'Edit'):
                for body, blocked in (('一般進度。', False), ('owner 原話：\n> 「'+invented+'」', True)):
                    reference.write_text(card_front+'一般進度。', encoding='utf-8')
                    event = {**self.event, 'tool_name': tool, 'tool_input': {
                        'file_path': str(reference), 'content': card_front+body,
                        'old_string': '一般進度。', 'new_string': body}}
                    run = common.run_synthetic(ROOT/'adapters/claude/pretooluse_gate.py',
                        event, self.config, arguments=arguments)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    output = json.loads(run.stdout) if run.stdout.strip() else {}
                    self.assertEqual(output.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny',
                        blocked, (tool, arguments, output, run.stderr))

    def test_fix4_explicit_continuation_and_local_adoption(self):
        truth, invented = self.review_source(())
        true, false = f'owner 原話：「{truth}」', f'owner 原話：「{invented}」'
        for connector in ('因此', '所以', '故', '於是'):
            text = true+'\n\n> '+connector+'禁止修改產品程式。'
            with self.subTest(connector=connector):
                self.assertIsNotNone(provenance.claim_violation(text, self.path))
                self.assertIn('UNVERIFIED', provenance.claim_violation(text, self.path,
                    seconds=0, hard_deadline=True))
        for prefix in ('錯誤紀錄：曾經誤稱 '+false+'。',
                       '不是 '+false+'。', '不當作 '+false+'。', '引用資料：「'+false+'」。',
                       '請 owner 決定是否採用「前置檢查超過10分鐘就取消」這項提案。'):
            for join in ('\n', '\n\n'):
                self.assertIsNone(provenance.claim_violation(prefix, self.path))
                self.assertIsNotNone(provenance.claim_violation(prefix+join+'現在就照做。', self.path))
        old = true+'\n\n禁止修改產品程式。'
        self.assertIsNotNone(provenance.claim_violation(true+'\n\n因此禁止修改產品程式。',
            self.path, previous=old))
        for prefix in ('錯誤紀錄：曾經誤稱 '+false,
                       '請 owner 決定要不要採用「前置檢查超過10分鐘就取消」這項提案'):
            for directive in (false, '前置檢查超過10分鐘就取消。'):
                self.assertIsNotNone(provenance.claim_violation(prefix+'\n'+directive, self.path))
        history = '昨天前置檢查超過10分鐘，已取消。'
        self.assertIsNone(provenance.claim_violation(history, self.path))
        self.assertIsNotNone(provenance.claim_violation(history+'\n現在就照做。', self.path))
        self.assertIsNone(provenance.claim_violation('錯誤紀錄：當時誤稱\n'+false, self.path))

    def test_fix4_large_source_two_seconds_solo_and_parallel(self):
        from concurrent.futures import ThreadPoolExecutor
        padding = (json.dumps({'type': 'progress', 'data': 'x'*60000})+'\n').encode()
        with self.path.open('wb') as stream:
            while stream.tell() <= 65*1024*1024:
                stream.write(padding)
            stream.write((json.dumps({'type': 'user', 'sessionId': 'incident', 'uuid': 'large-truth',
                'message': {'content': OWNER_WORDS}}, ensure_ascii=False)+'\n').encode('utf-8'))
        text = f'owner 原話：「{OWNER_WORDS}」'

        def measure(arguments, mode):
            began = time.monotonic()
            run = common.run_synthetic(ROOT/'adapters/claude/stop_gate.py',
                {**self.event, 'last_assistant_message': text}, self.config, arguments=arguments)
            elapsed = time.monotonic()-began
            self.assertEqual(run.returncode, 0, run.stderr)
            output = json.loads(run.stdout) if run.stdout.strip() else {}
            blocked = output.get('decision') == 'block'
            diagnostic = json.dumps(output)+run.stderr
            self.assertLess(elapsed, 3.5, diagnostic)
            if blocked:
                self.assertIn('UNVERIFIED', diagnostic)
                self.assertTrue('deadline' in diagnostic or 'read-budget-exhausted' in diagnostic, diagnostic)
            else:
                self.assertNotIn('UNVERIFIED', diagnostic)
            row = dict(mode=mode, host='codex' if arguments else 'claude',
                       bytes=self.path.stat().st_size, admission_seconds=2,
                       seconds=round(elapsed, 3), status='UNVERIFIED' if blocked else 'VERIFIED')
            print('LOAD_MEASUREMENT '+json.dumps(row), flush=True)
            return row

        solo = [measure(arguments, 'solo') for arguments in ((), ('--codex',))]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(measure, arguments, 'parallel-4')
                       for arguments in ((), ('--codex',), (), ('--codex',))]
            parallel = [future.result() for future in futures]
        self.assertEqual(len(solo)+len(parallel), 6)
        # Force the timeout branch too; a measured timeout never counts as source success.
        reason = provenance.claim_violation(text, self.path, seconds=0, hard_deadline=True)
        self.assertIn('UNVERIFIED', reason)
        self.assertIn('deadline', reason)

    def test_fix4_local_causal_ban_and_adopted_prohibition_all_surfaces(self):
        for arguments in ((), ('--codex',)):
            truth, invented = self.review_source(arguments)
            pairs = [
                ('錯誤紀錄：曾經誤稱 owner 原話：「'+invented+'」。',
                 '錯誤紀錄：曾經誤稱 owner 原話：「'+invented+'」。\n\n因此禁止修改產品程式。'),
                ('請 owner 決定是否採用「禁止修改產品程式」這項提案。',
                 '請 owner 決定是否採用「禁止修改產品程式」這項提案。\n現在就照做。'),
                ('前置檢查超過10分鐘，\n\n昨天已直接取消。',
                 '前置檢查超過10分鐘，\n\n就直接取消。'),
                ('請 owner 決定是否採用 `前置檢查超過10分鐘就取消` 這項提案。',
                 '請 owner 決定是否採用 `前置檢查超過10分鐘就取消` 這項提案。\n現在就照做。'),
            ]
            for ordinary, abuse in pairs:
                for tool in ('Write', 'Edit', 'Task', 'Stop'):
                    with self.subTest(host=arguments, tool=tool, text=abuse):
                        self.check_surface(ordinary, tool, arguments, False)
                        self.check_surface(abuse, tool, arguments, True)
                        self.assertIn('UNVERIFIED', provenance.claim_violation(abuse, self.path,
                            seconds=0, hard_deadline=True))

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
        bad = provenance.claim_violation(f'owner 原話：「{ASSISTANT_WORDS}」', snapshot, seconds=ADMISSION_SECONDS,
                                         hard_deadline=True)
        good = provenance.claim_violation(f'owner 原話：「{OWNER_WORDS}」', snapshot, seconds=ADMISSION_SECONDS,
                                          hard_deadline=True)
        self.assertIn('role=A, L16845', bad)
        self.assertIsNone(good)


if __name__ == '__main__':
    unittest.main()
