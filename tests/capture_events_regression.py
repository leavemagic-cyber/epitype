"""Synthetic human-event capture, search and execution-boundary checks."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import (capture, cardscan, core_gen, harvest, hook_context, host_native, memsearch, memspec,
                     owner_rules, rule_context, rule_pipeline, transcript)
import _hook_common as common
import pretooluse_gate as pretool
import recall_hook as recall
import stop_gate as stop

FIXTURE = {"source": "synthetic-session.jsonl", "records": [
    {"line": 2, "expected": False, "record": {"type": "user", "uuid": "notice",
        "timestamp": "2026-01-01T00:00:01Z", "message": {"content": "本週的使用量已達上限，明天重新計算。"}}},
    {"line": 3, "expected": False, "record": {"type": "user", "uuid": "question",
        "timestamp": "2026-01-01T00:00:02Z", "message": {"content": "你打算拿什麼資料來核對？"}}},
    {"line": 5, "expected": True, "needle": "測試報表", "record": {"type": "user", "uuid": "order",
        "timestamp": "2026-01-01T00:00:03Z", "message": {"content": "先交付完整的測試報表，再討論下次要補的欄位。"}}},
    {"line": 6, "expected": True, "needle": "範例設定", "excluded": "助理提供的候選說明",
        "record": {"type": "attachment", "uuid": "queued", "timestamp": "2026-01-01T00:00:04Z",
            "attachment": {"type": "queued_command", "origin": {"kind": "human"},
                "prompt": "助理提供的候選說明<-你應該先確認範例設定正確，再提出修改建議。"}}},
    {"line": 7, "expected": True, "needle": "cli", "record": {"type": "user", "uuid": "workflow",
        "timestamp": "2026-01-01T00:00:05Z", "message": {"content": "不要再開管理頁面，改用 cli 匯出測試清單。"}}},
    {"line": 1, "expected": False, "record": {"type": "user", "uuid": "tool",
        "timestamp": "2026-01-01T00:00:06Z", "message": {"content": [
            {"type": "tool_result", "content": "不要再開管理頁面，改用 cli 匯出測試清單。"}]}}},
    {"line": 4, "expected": False, "record": {"type": "user", "uuid": "summary",
        "timestamp": "2026-01-01T00:00:07Z", "isCompactSummary": True,
        "message": {"content": "不要再開管理頁面，改用 cli 匯出測試清單。"}}}
]}
RECORDS = FIXTURE["records"]


class CaptureEventsRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-owner-events-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            "CODEX_HOME": str(self.root / "home/.codex"),
            "EPITYPE_CONFIG": str(self.root / "config.json"), "EPITYPE_DREAM_MODE": "off",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.vault = self.root / "memory"
        self.vault.mkdir()
        common.write_config(self.root / "config.json", [self.vault])
        self.path = self.root / FIXTURE["source"]
        self.event = {"cwd": str(self.root), "session_id": Path(FIXTURE["source"]).stem,
                      "transcript_path": str(self.path)}

    def write_records(self, records, mode="w"):
        with self.path.open(mode, encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def cards(self):
        return sorted(self.vault.joinpath(*memspec.CAPTURE_PENDING_SUBPATH).rglob("*.md"))

    def test_synthetic_messages_save_search_and_reject_individually(self):
        for case in RECORDS:
            with self.subTest(line=case["line"]):
                record = transcript.source_record(case["record"])
                if record is None:
                    self.assertFalse(case["expected"])
                    continue
                role, text = record
                event = {**self.event, "source_message_id": case["record"]["uuid"],
                         "source_role": role, "message_index": case["line"]}
                replay = capture.Replay(stamp=case["record"]["timestamp"])
                path = capture.capture_event(text, self.vault, event, None, question="", replay=replay)
                self.assertEqual(path is not None, case["expected"])
                self.assertEqual(harvest.owner_text(case["record"]), text)
                if path is None:
                    continue
                fields, problem = memspec.frontmatter_fields(path)
                self.assertIsNone(problem)
                self.assertEqual(fields["capture_review"], "UNREVIEWED")
                self.assertEqual(fields["source_message_id"], case["record"]["uuid"])
                self.assertEqual(fields["source_line"], str(case["line"]))
                self.assertEqual(fields["captured_at"], case["record"]["timestamp"])
                self.assertEqual(fields["cwd"], str(self.root))
                body = path.read_text(encoding="utf-8").partition("\n---\n")[2].strip()
                self.assertEqual(body, capture.owner_reply(text))
                if case.get("excluded"):
                    self.assertNotIn(case["excluded"], body)
                memsearch.build_index(self.vault)
                hits = memsearch.query_index(self.vault, case["needle"])["results"]
                self.assertIn(path.name, [Path(hit["path"]).name for hit in hits])
                hit = next(hit for hit in hits if Path(hit["path"]) == path)
                self.assertEqual(hit["capture_review"], "UNREVIEWED")
                self.assertEqual(hit["authority"], "none")
                self.assertEqual(rule_context.reference_applicability(fields, path, self.event)[0], "OUTSIDE")
                self.assertEqual(memsearch.recall_index(self.vault, case["needle"])["count"], 0)
                with self.assertRaisesRegex(ValueError, "no execution authority"):
                    rule_pipeline.projection(fields, path)
                self.assertNotEqual(rule_pipeline.admission("", path.read_text(encoding="utf-8"),
                                                           "decision", self.vault / path.name), [])
        self.assertEqual(len(self.cards()), 3)
        self.assertEqual(cardscan.card_files(self.vault), [])

    def test_incremental_queue_retry_and_distinct_message_identity(self):
        self.write_records([case["record"] for case in RECORDS[:2]])
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(self.cards(), [])
        queue = next(case["record"] for case in RECORDS if case["line"] == 6)
        self.write_records([queue], "a")
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(len(self.cards()), 1)
        capture.capture_transcript(self.vault, self.event, None)
        self.write_records([queue], "a")  # same message delivered again
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(len(self.cards()), 1)
        distinct = {**queue, "uuid": queue["uuid"] + "-second"}
        self.write_records([distinct], "a")
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(len(self.cards()), 2)

    def test_partial_record_is_retried_and_nonhuman_queue_is_ignored(self):
        queue = next(case["record"] for case in RECORDS if case["line"] == 6)
        raw = json.dumps(queue, ensure_ascii=False)
        self.path.write_text(raw[:len(raw) // 2], encoding="utf-8")
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(self.cards(), [])
        self.path.write_text(raw + "\n", encoding="utf-8")
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(len(self.cards()), 1)
        nonhuman = {**queue, "uuid": "nonhuman", "attachment": {
            **queue["attachment"], "origin": {"kind": "agent"}}}
        self.write_records([nonhuman], "a")
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(len(self.cards()), 1)

    def test_oversized_record_and_corrupt_cursor_do_not_stall_later_messages(self):
        queue = next(case["record"] for case in RECORDS if case["line"] == 6)
        self.path.write_bytes(b"x" * (memspec.COMPACT_MAP_MAX_LINE_BYTES + 10) + b"\n")
        capture.capture_transcript(self.vault, self.event, None)
        self.write_records([queue], "a")
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(len(self.cards()), 1)
        cursor = next((self.vault / ".epitype/capture_cursors").glob("*.json"))
        cursor.write_text("[]", encoding="utf-8")
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(len(self.cards()), 1)

    def test_pending_event_cannot_arm_consumers_even_with_active_metadata(self):
        self.write_records([case["record"] for case in RECORDS])
        capture.capture_transcript(self.vault, self.event, None)
        card = self.cards()[0]
        text = card.read_text(encoding="utf-8").replace("---\n", "---\n"
            "decision_key: fixture-owner-event\nstatus: active\ndecided_by: owner-explicit\n"
            "forbidden: cli\nguard_tool: Bash\nguard_all_of: cli\nhandoff_rule: true\n", 1)
        card.write_text(text, encoding="utf-8")
        started = time.monotonic()
        self.assertEqual(list(stop._decisions(self.vault, started)), [])
        self.assertEqual(list(pretool._guards(self.vault, started, [])), [])
        config = common.load_config(started)
        verdict, _ = pretool._write_review({**self.event, "tool_name": "Write"}, "Write",
            {"file_path": str(self.root / "note.md"), "content": "cli"}, config, started)
        self.assertIsNone(verdict)
        desired, _ = host_native.collect([self.vault])
        self.assertFalse(any(desired.values()))
        self.assertEqual(core_gen.collect_rules([self.vault])[0], [])
        self.assertNotIn(card.name, owner_rules.render([self.vault]))
        memsearch.build_index(self.vault)
        self.assertIsNone(recall._admitted_card(
            {"path": str(card), "card_path": card.relative_to(self.vault).as_posix(),
             "status": "active"}, self.vault, self.event, set()))
        self.assertEqual(memsearch.recall_index(self.vault, "cli")["count"], 0)

    def test_claude_and_codex_message_shapes_share_results(self):
        source = next(case["record"] for case in RECORDS if case["line"] == 7)
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                vault = self.root / host
                vault.mkdir()
                path = self.root / ("rollout-test.jsonl" if host == "codex" else "claude.jsonl")
                row = source if host == "claude" else {
                    "type": "response_item", "timestamp": source["timestamp"],
                    "payload": {"type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": source["message"]["content"]}]}}
                rows = [row]
                if host == "codex":
                    rows.append({"type": "event_msg", "payload": {
                        "type": "user_message", "message": source["message"]["content"]}})
                path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                                encoding="utf-8")
                capture.capture_transcript(vault, {**self.event, "transcript_path": str(path)}, None)
                self.assertEqual(len(list(vault.rglob("*.md"))), 1)
                memsearch.build_index(vault)
                self.assertEqual(memsearch.query_index(vault, "cli")["count"], 1)

    def test_message_identity_is_scoped_to_its_session(self):
        row = next(case['record'] for case in RECORDS if case['line'] == 7)
        for session in ('sample-session-one', 'sample-session-two'):
            capture.capture_event(row['message']['content'], self.vault,
                {**self.event, 'session_id': session, 'source_message_id': row['uuid']},
                None, question='')
        self.assertEqual(len(self.cards()), 2)

    def test_codex_message_id_and_live_replay_identity_agree(self):
        row = {'type': 'response_item', 'timestamp': '2026-01-01T00:00:01Z',
            'payload': {'type': 'message', 'id': 'sample-codex-owner', 'role': 'user',
                'content': [{'type': 'input_text', 'text': '不要再改測試欄位。請保留清單。'}]}}
        self.assertEqual(capture.record_message_id(row, 'fallback'), 'sample-codex-owner')
        for timestamped in (True, False):
            with self.subTest(timestamped=timestamped):
                if not timestamped:
                    row.pop('timestamp')
                    row['payload'].pop('id')
                vault = self.root / ('timed' if timestamped else 'positional')
                vault.mkdir()
                self.write_records([row])
                capture.capture_transcript(vault, self.event, None)
                cards = list(vault.rglob('*.md'))
                self.assertEqual(len(cards), 1)
                line, text, question, item = next(harvest.utterances(self.path))
                event = harvest._record_event(item, self.event['session_id'], str(self.path), line)
                replay = capture.Replay()
                capture.capture_event(text, vault, event, None, question=question, replay=replay)
                self.assertEqual(replay.status, capture.STATUS_DUPLICATE)
                self.assertEqual(cards[0].read_text(encoding='utf-8').partition('\n---\n')[2].strip(), text)
                replayed = self.root / ('replayed-timed' if timestamped else 'replayed-positional')
                found = harvest.candidates(text, question)[0]
                kind, directory, digest, label, body, summary = found
                path = capture.write_capture(replayed, directory, kind, digest, label, body,
                    event, None, summary=summary, source_text=text, replay=capture.Replay())
                self.assertEqual(path.read_text(encoding='utf-8').partition('\n---\n')[2].strip(), text)

    def test_tool_and_stop_entrypoints_capture_human_queue_without_advice(self):
        row = next(case['record'] for case in RECORDS if case['line'] == 6)
        for host, handler in (('pretool', pretool._handle_review), ('stop', stop._handle)):
            with self.subTest(hook=host):
                vault = self.root / host
                vault.mkdir()
                self.write_records([row])
                event = {**self.event, 'tool_name': 'Read',
                    'tool_input': {'file_path': str(self.root / 'sample.txt')},
                    'last_assistant_message': 'Sample completed.'}
                with patch.object(common, 'capture_vault', return_value=vault):
                    result = handler(event, time.monotonic(), [])
                self.assertIsNone(result)
                cards = list(vault.joinpath(*memspec.CAPTURE_PENDING_SUBPATH).rglob('*.md'))
                self.assertEqual(len(cards), 1)
                self.assertIn('source_role: Q', cards[0].read_text(encoding='utf-8'))

    def test_malformed_cursor_line_does_not_break_capture(self):
        self.write_records([])
        capture.capture_transcript(self.vault, self.event, None)
        cursor = next((self.vault / '.epitype/capture_cursors').glob('*.json'))
        state = json.loads(cursor.read_text(encoding='utf-8'))
        state['line'] = 'malformed'
        cursor.write_text(json.dumps(state), encoding='utf-8')
        self.write_records([next(case['record'] for case in RECORDS if case['line'] == 7)], 'a')
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(len(self.cards()), 1)

    def test_other_drafts_and_junctions_stay_private(self):
        self.write_records([case["record"] for case in RECORDS])
        capture.capture_transcript(self.vault, self.event, None)
        other = self.vault / "_drafts/private.md"
        other.write_text("---\nname: private\ndescription: cli\n---\ncli\n", encoding="utf-8")
        hidden = self.cards()[0].parent / "_private.md"
        hidden.write_text(other.read_text(encoding="utf-8"), encoding="utf-8")
        scanned = {path for _, path, _, _ in memsearch._scan_vault(self.vault)}
        self.assertNotIn(other, scanned)
        self.assertNotIn(hidden, scanned)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.md").write_text(other.read_text(encoding="utf-8"), encoding="utf-8")
        link = self.cards()[0].parent / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            return  # Windows may not grant unprivileged symlink creation.
        self.assertFalse(any("linked" in relative for relative, *_ in memsearch._scan_vault(self.vault)))

    def test_shared_hook_helper_and_prompt_capture_without_advice(self):
        queue = next(case["record"] for case in RECORDS if case["line"] == 6)
        self.write_records([queue])
        with patch.object(common, "capture_vault", return_value=self.vault):
            self.assertIsNone(common.capture_transcript_events({}, self.event, time.monotonic()))
        self.assertEqual(len(self.cards()), 1)
        with patch.object(recall, "_capture_vault", return_value=self.vault):
            result = recall._recall_cards({**self.event, "prompt": ""}, time.monotonic(),
                                         {memspec.CONFIG_PROACTIVE_RECALL_FIELD: False})
        self.assertIsNone(result)
        self.assertEqual(len(self.cards()), 1)


class CaptureIsolationRegression(unittest.TestCase):
    setUp = CaptureEventsRegression.setUp
    write_records = CaptureEventsRegression.write_records
    cards = CaptureEventsRegression.cards

    def test_pretool_capture_exception_preserves_real_guard_denial(self):
        from rule_source_fixtures import owner_bound_card
        card = self.vault / 'sample-guard.md'
        card.write_text(owner_bound_card(
            '---\nname: sample guard\ndescription: synthetic read guard\n'
            'guard_tool: Read\nguard_all_of: [blocked-sample.txt]\non_hit: block\n'
            'guard_advice: Inspect the sample first.\n---\nSynthetic guard.\n',
            card, source_root=self.root / 'sources', case_id='capture-isolation'), encoding='utf-8')
        event = {**self.event, 'tool_name': 'Read',
                 'tool_input': {'file_path': str(self.root / 'blocked-sample.txt')}}
        self.write_records([])
        with patch.object(common, 'capture_vault', return_value=self.vault):
            with patch.object(capture, 'capture_transcript', return_value=False):
                expected = pretool._handle_review(event, time.monotonic(), [])
            self.assertEqual(expected['hookSpecificOutput']['permissionDecision'], 'deny')
            for error in (RuntimeError, ValueError, KeyError):
                with self.subTest(error=error.__name__), redirect_stderr(io.StringIO()) as stderr, \
                        patch.object(capture, 'capture_transcript', side_effect=error('synthetic')):
                    self.assertEqual(pretool._handle_review(event, time.monotonic(), []), expected)
                self.assertIn(f'Epitype event capture deferred: {error.__name__}', stderr.getvalue())

    def test_stop_capture_exception_preserves_real_verdict_and_note(self):
        (self.vault / 'sample-stop.md').write_text(
            '---\nname: sample stop\ndescription: synthetic stop advice\n'
            'forbidden: [sample-forbidden]\nadvice: Check the sample.\n---\nSynthetic note.\n',
            encoding='utf-8')
        self.write_records([])
        event = {**self.event, 'last_assistant_message': 'sample-forbidden'}
        config = common.load_config(time.monotonic())
        with patch.object(common, 'capture_vault', return_value=self.vault), \
                patch.object(stop, '_claim_marker', return_value=True):
            with patch.object(capture, 'capture_transcript', return_value=False):
                expected = stop._handle(event, time.monotonic(), [])
            expected_notes = common.take_notes(config, hook_context.notification_identity(event))
            self.assertTrue(expected_notes)
            with redirect_stderr(io.StringIO()) as stderr, \
                    patch.object(capture, 'capture_transcript', side_effect=RuntimeError('synthetic')), \
                    patch.object(stop, '_verdict', wraps=stop._verdict) as verdict:
                self.assertEqual(stop._handle(event, time.monotonic(), []), expected)
            verdict.assert_called_once()
            self.assertEqual(common.take_notes(config, hook_context.notification_identity(event)), expected_notes)
            self.assertIn('Epitype event capture deferred: RuntimeError', stderr.getvalue())

    def test_recall_capture_exception_still_delivers_real_search_result(self):
        (self.vault / 'sample-reference.md').write_text(
            '---\nname: samplelookup\ndescription: samplelookup reference\n---\n'
            'samplelookup synthetic reference.\n', encoding='utf-8')
        memsearch.build_index(self.vault)
        config = common.load_config(time.monotonic())
        config[memspec.CONFIG_PROACTIVE_RECALL_FIELD] = True
        event = {**self.event, 'prompt': 'samplelookup'}
        with patch.object(recall, '_capture_vault', return_value=self.vault), \
                patch.object(recall, 'scoped_vaults', return_value=[self.vault]):
            with patch.object(capture, 'capture_transcript', return_value=True):
                expected = recall._recall_cards(event, time.monotonic(), config)
            self.assertIsNotNone(expected)
            self.assertIn('samplelookup', str(expected))
            with redirect_stderr(io.StringIO()) as stderr, \
                    patch.object(capture, 'capture_transcript', side_effect=RuntimeError('synthetic')), \
                    patch.object(memsearch, 'recall_index', wraps=memsearch.recall_index) as search:
                self.assertEqual(recall._recall_cards(event, time.monotonic(), config), expected)
            search.assert_called_once()
            self.assertIn('Epitype event capture deferred: RuntimeError', stderr.getvalue())

    def test_capture_routing_and_prompt_fallback_errors_are_reported(self):
        for target, name in ((common, 'capture_vault'), (recall, '_capture_vault'),
                             (recall, '_capture_event')):
            with self.subTest(stage=name), redirect_stderr(io.StringIO()) as stderr, \
                    patch.object(common, 'capture_vault', return_value=self.vault), \
                    patch.object(recall, '_capture_vault', return_value=self.vault), \
                    patch.object(capture, 'capture_transcript', return_value=False), \
                    patch.object(target, name, side_effect=RuntimeError('synthetic')):
                if target is common:
                    common.capture_transcript_events({}, self.event, time.monotonic())
                else:
                    recall._recall_cards({'prompt': 'samplelookup'}, time.monotonic(), {})
            self.assertIn('Epitype event capture deferred: RuntimeError', stderr.getvalue())

    def test_budget_checkpoint_resumes_without_skipping_or_duplicating(self):
        first = next(case['record'] for case in RECORDS if case['line'] == 7)
        second = {**first, 'uuid': 'budget-second'}
        self.write_records([first, second])
        calls = []
        clock = [0.0]
        capture_event = capture.capture_event

        def complete_record(*args, **kwargs):
            calls.append(args[2]['source_message_id'])
            result = capture_event(*args, **kwargs)
            clock[0] = 0.101
            return result

        with patch.object(common, 'capture_vault', return_value=self.vault), \
                patch.object(capture, 'expired', return_value=False), \
                patch.object(capture.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(capture, 'capture_event', side_effect=complete_record):
            common.capture_transcript_events({}, self.event, 0.0)
        self.assertEqual(calls, [first['uuid']])
        cursor = next((self.vault / '.epitype/capture_cursors').glob('*.json'))
        checkpoint = json.loads(cursor.read_text(encoding='utf-8'))
        self.assertEqual(checkpoint['offset'], len(self.path.read_bytes().splitlines(keepends=True)[0]))
        self.assertEqual(checkpoint['line'], 1)
        with patch.object(common, 'capture_vault', return_value=self.vault):
            common.capture_transcript_events({}, self.event, time.monotonic())
            self.assertEqual(len(self.cards()), 2)
            common.capture_transcript_events({}, self.event, time.monotonic())
            self.assertEqual(len(self.cards()), 2)
        self.assertEqual(json.loads(cursor.read_text(encoding='utf-8'))['offset'], self.path.stat().st_size)

    def test_unexpected_record_error_keeps_cursor_for_retry(self):
        self.write_records([])
        capture.capture_transcript(self.vault, self.event, None)
        cursor = next((self.vault / '.epitype/capture_cursors').glob('*.json'))
        checkpoint = cursor.read_bytes()
        row = next(case['record'] for case in RECORDS if case['line'] == 7)
        self.write_records([row], 'a')
        with patch.object(common, 'capture_vault', return_value=self.vault):
            with redirect_stderr(io.StringIO()) as stderr, \
                    patch.object(transcript, 'source_record', side_effect=KeyError('synthetic')):
                common.capture_transcript_events({}, self.event, time.monotonic())
            self.assertIn('Epitype event capture deferred: KeyError', stderr.getvalue())
            self.assertEqual(cursor.read_bytes(), checkpoint)
            common.capture_transcript_events({}, self.event, time.monotonic())
            common.capture_transcript_events({}, self.event, time.monotonic())
        self.assertEqual(len(self.cards()), 1)


class CodexExecCaptureRegression(unittest.TestCase):
    setUp = CaptureEventsRegression.setUp
    write_records = CaptureEventsRegression.write_records
    cards = CaptureEventsRegression.cards
    prompt = "不要再改測試欄位，請保留範例清單。"

    def codex_records(self, meta):
        rows = [] if meta is None else [{"type": "session_meta", "payload": meta}]
        rows.append({"type": "response_item", "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"type": "message", "role": "user", "id": "synthetic-codex-input",
                "content": [{"type": "input_text", "text": self.prompt}]}})
        return rows

    def select_case(self, name, meta):
        self.path = self.root / ("rollout-" + name + ".jsonl")
        self.vault = self.root / name
        self.vault.mkdir()
        self.event = {**self.event, "transcript_path": str(self.path), "prompt": self.prompt}
        self.write_records(self.codex_records(meta))

    def test_exec_metadata_skips_prompt_incremental_and_harvest_capture(self):
        for name, meta in (
                ("originator", {"originator": "codex_exec"}),
                ("source", {"source": "exec"}),
                ("both", {"originator": "codex_exec", "source": "exec"}),
                ("source-overrides", {"originator": "codex_work_desktop", "source": "exec"})):
            with self.subTest(name=name):
                self.select_case(name, meta)
                with patch.object(common, "capture_vault", return_value=self.vault):
                    common.capture_transcript_events({}, self.event, time.monotonic())
                with patch.object(recall, "_capture_vault", return_value=self.vault):
                    recall._recall_cards(self.event, time.monotonic(), {})
                for prompt, question in ((self.prompt, ""), ("你可以直接修改範例。", ""),
                                         ("就用第二案，以後都不要再問這件事。", "請你確認要用哪個方案？")):
                    found = capture.classify(prompt, question)
                    self.assertIsNotNone(found)
                    self.assertIsNone(capture.capture_event(prompt, self.vault, self.event, None,
                                                            question=question))
                    self.assertIsNone(capture.capture_owner_sentence(prompt, self.vault, self.event,
                        None, found[0], question=question))
                    self.assertIsNone(capture.capture_ruling(prompt, self.vault, self.event, None,
                                                            question=question))
                self.assertEqual(list(harvest.utterances(self.path)), [])
                self.assertEqual(list(self.vault.rglob("*.md")), [])
                self.assertFalse((self.vault / ".epitype/capture_cursors").exists())

    def test_desktop_and_missing_metadata_keep_capture(self):
        for name, meta in (("desktop", {"originator": "codex_work_desktop", "source": "vscode"}),
                           ("missing", None), ("unknown", {})):
            with self.subTest(name=name):
                self.select_case(name, meta)
                capture.capture_transcript(self.vault, self.event, None)
                self.assertEqual(len(self.cards()), 1)
                self.assertEqual([row[1] for row in harvest.utterances(self.path)], [self.prompt])
                direct = self.vault / "direct"
                self.assertIsNotNone(capture.capture_event(self.prompt, direct, self.event, None,
                                                           question=""))

    def test_unreadable_metadata_keeps_direct_capture_and_is_not_cached(self):
        for name, header in (("bad-json", b"{broken\n"), ("array", b"[]\n"),
                             ("bad-payload", b'{"type":"session_meta","payload":[]}\n'),
                             ("large", b" " * 32769 + b"\n")):
            with self.subTest(name=name):
                self.select_case(name, None)
                self.path.write_bytes(header + self.path.read_bytes())
                capture.capture_transcript(self.vault, self.event, None)
                self.assertEqual(len(self.cards()), 1)
                self.assertEqual(len(list(harvest.utterances(self.path))), 1)
                self.assertIsNotNone(capture.capture_event(self.prompt, self.vault / "direct",
                                                           self.event, None, question=""))
        self.select_case("late-header", None)
        self.path.unlink()
        self.assertFalse(capture.is_codex_exec_transcript(self.path))
        with patch.object(capture.Path, "open", side_effect=PermissionError("synthetic")):
            self.assertFalse(capture.is_codex_exec_transcript(self.path))
        self.write_records(self.codex_records({"originator": "codex_exec"}))
        self.assertTrue(capture.is_codex_exec_transcript(self.path))
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(self.cards(), [])

    def test_claude_exec_shaped_fields_are_not_session_metadata(self):
        self.select_case("claude", None)
        self.write_records([{"type": "user", "originator": "codex_exec", "source": "exec",
                             "message": {"content": self.prompt}}])
        capture.capture_transcript(self.vault, self.event, None)
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(len(list(harvest.utterances(self.path))), 1)

    def test_exec_pretool_still_enforces_real_guard(self):
        from rule_source_fixtures import owner_bound_card
        self.select_case("guard", {"originator": "codex_exec", "source": "exec"})
        common.write_config(self.root / "config.json", [self.vault])
        card = self.vault / "sample-guard.md"
        card.write_text(owner_bound_card(
            "---\nname: sample guard\ndescription: synthetic read guard\n"
            "guard_tool: Read\nguard_all_of: [blocked-sample.txt]\non_hit: block\n"
            "guard_advice: Inspect the sample first.\n---\nSynthetic guard.\n",
            card, source_root=self.root / "sources", case_id="capture-isolation"), encoding="utf-8")
        event = {**self.event, "tool_name": "Read",
                 "tool_input": {"file_path": str(self.root / "blocked-sample.txt")}}
        with patch.object(common, "capture_vault", return_value=self.vault):
            result = pretool._handle_review(event, time.monotonic(), [])
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.cards(), [])

    def test_exec_stop_still_checks_reply_and_queues_real_note(self):
        self.select_case("stop", {"originator": "codex_exec", "source": "exec"})
        common.write_config(self.root / "config.json", [self.vault])
        (self.vault / "sample-stop.md").write_text(
            "---\nname: sample stop\ndescription: synthetic stop advice\n"
            "forbidden: [sample-forbidden]\nadvice: Check the sample.\n---\nSynthetic note.\n",
            encoding="utf-8")
        event = {**self.event, "last_assistant_message": "sample-forbidden"}
        with patch.object(common, "capture_vault", return_value=self.vault), \
                patch.object(stop, "_claim_marker", return_value=True), \
                patch.object(stop, "_verdict", wraps=stop._verdict) as verdict:
            stop._handle(event, time.monotonic(), [])
        verdict.assert_called_once()
        config = common.load_config(time.monotonic())
        notes = common.take_notes(config, hook_context.notification_identity(event))
        self.assertIn("synthetic stop advice", str(notes))
        self.assertEqual(self.cards(), [])

    def test_exec_compaction_still_writes_map_and_resumes_handoff(self):
        from epitype import compact_map
        import precompact_hook as precompact
        import sessionstart_hook as sessionstart
        self.select_case("compact", {"originator": "codex_exec", "source": "exec"})
        common.write_config(self.root / "config.json", [self.vault])
        trace = {}
        precompact._handle(self.event, time.monotonic(), learn=False, trace=trace)
        self.assertEqual(trace["outcome"], "ok")
        destination = compact_map.map_destination(self.vault, self.event["session_id"], self.path)
        self.assertIn(self.prompt, destination.read_text(encoding="utf-8"))
        handoff = compact_map.handoff_destination(self.vault, self.event["session_id"], self.path)
        handoff.write_text("<!-- EPITYPE RESUME BEGIN -->\nsynthetic exec state\n"
                           "<!-- EPITYPE RESUME END -->\n", encoding="utf-8")
        with patch.object(sessionstart, "_dream_spawn"), patch.object(sessionstart, "_startup_segments"):
            result = sessionstart._handle({**self.event, "source": "compact"}, time.monotonic())
        self.assertIn("synthetic exec state", str(result))


if __name__ == "__main__":
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
