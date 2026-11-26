import sys; sys.dont_write_bytecode = True
"""Narrow before/after checks for the owner's 2026-10-03 Stop advisory change."""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import turn_check_regression as turn_fixtures
import stop_gate as stop
from epitype import hook_context, memspec

BASELINE = "--baseline" in sys.argv
if BASELINE:
    sys.argv.remove("--baseline")


class StopAdvisory(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp

    def event(self, text="一般回覆", **extra):
        return {"session_id": "advisory-" + self.root.name,
                "transcript_path": str(self.root / "thread.jsonl"),
                "hook_event_name": "Stop", "last_assistant_message": text, **extra}

    def verdict(self, decision, text, turn=None, event=None, **patches):
        with patch.object(stop, "_vaults", return_value=[str(self.vault)]), \
                patch.object(stop, "_decisions", return_value=[decision]), \
                patch.object(stop, "_claim_marker", return_value=True):
            return stop._verdict(event or self.event(text), text,
                                 {"vaults": [self.vault]}, time.monotonic(), [], turn)

    def assert_disposition(self, value, blocked_before=True):
        if BASELINE and blocked_before:
            self.assertEqual((value or {}).get("decision"), "block")
        else:
            self.assertIsNone(value)

    def notes(self):
        """The attribution notice is a note for the next prompt, not stderr (item 2,
        2026-10-10): stderr never reaches the model."""
        import _hook_common as common
        return common.take_notes(common.load_config(time.monotonic()), hook_context.identity(self.event()))

    def rows(self):
        path = self.vault / memspec.GATE_LOG_FILENAME
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a26_forbidden_is_advisory(self):
        card = turn_fixtures.decision(forbidden=("forbidden-text",))
        self.assert_disposition(self.verdict(card, "forbidden-text"))
        self.assertEqual(self.rows()[0]["kind"],
                         memspec.STOP_GATE_LOG_KIND if BASELINE else memspec.STOP_NOTE_LOG_KIND)

    def test_a27_missing_required_word_is_advisory(self):
        card = turn_fixtures.decision(require_when="完成", require_text="實測")
        self.assert_disposition(self.verdict(card, "已完成"))

    def test_a28_unread_claim_is_advisory(self):
        card = turn_fixtures.decision(turn_check=memspec.TURN_CHECK_CITED_UNREAD)
        from epitype import opened
        with patch.object(opened, "strong_names", return_value=frozenset({"checked.py"})):
            self.assert_disposition(self.verdict(card, "我查過 ghost.py。", stop._turn()))

    def test_a29_delegated_completion_is_advisory(self):
        card = turn_fixtures.decision(turn_check=memspec.TURN_CHECK_UNVERIFIED_DELEGATION)
        self.assert_disposition(self.verdict(card, "已完成。", stop._turn(dispatch="worker")))

    def test_a30_missing_prompt_fence_is_advisory(self):
        card = turn_fixtures.decision(turn_check=memspec.TURN_CHECK_PROMPT_BLOCK)
        self.assert_disposition(self.verdict(card, "請直接操作。", stop._turn(prompt="幫我寫 prompt")))

    def test_a31_english_output_is_advisory(self):
        card = turn_fixtures.decision(turn_check=memspec.TURN_CHECK_OWNER_LANGUAGE)
        self.assert_disposition(self.verdict(card, turn_fixtures.ENGLISH_REPLY))

    def test_a32_finished_agent_cleanup_is_advisory(self):
        card = turn_fixtures.decision(turn_check=memspec.TURN_CHECK_FINISHED_AGENTS)
        with patch.object(stop, "_finished_agents_gap", return_value="本場代理仍開著"):
            self.assert_disposition(self.verdict(card, "完成。"))

    def test_a33_shell_syntax_is_advisory(self):
        card = turn_fixtures.decision(fence_shell="powershell", fence_langs=("powershell",))
        with patch.object(stop, "_fence_shell_gap", return_value="引用的殼層語法錯誤"):
            self.assert_disposition(self.verdict(card, "```powershell\n歷史錯誤\n```"))

    def test_a34_existing_length_note_stays_advisory(self):
        card = turn_fixtures.decision(turn_check=memspec.TURN_CHECK_LENGTH,
                                     turn_check_limit="10", on_hit="note")
        self.assert_disposition(self.verdict(card, "這是一段超過十個字的完整說明。", stop._turn()), False)

    def test_a35_retry_and_attribution_never_repeat_reply(self):
        for reason in ("owner claim has no exact quotation to verify",
                       "admission deadline exhausted; worker reaped"):
            with self.subTest(reason=reason), patch.object(
                    stop.rule_provenance, "claim_violation", return_value=reason):
                defects = []
                result = stop._handle(self.event(stop_hook_active=True), time.monotonic(), defects)
                self.assert_disposition(result)
                if not BASELINE:
                    notes = self.notes()
                    self.assertEqual(len(notes), 1, (notes, defects))
                    self.assertNotIn("\n", notes[0])
                    self.assertEqual(defects, [])

    def test_stop_attribution_timeout_is_advisory(self):
        with patch.object(stop.rule_provenance, "claim_violation",
                          return_value="admission deadline exhausted; worker reaped"):
            defects = []
            result = stop._handle(self.event(), time.monotonic(), defects)
            self.assert_disposition(result)
            if not BASELINE:
                self.assertEqual(len(self.notes()), 1, defects)
                self.assertEqual(defects, [])

    def test_a40_child_does_not_require_root_format_or_cleanup(self):
        card = turn_fixtures.decision(turn_check=memspec.TURN_CHECK_PROMPT_BLOCK)
        event = self.event("bounded result", hook_event_name=memspec.SUBAGENT_STOP_EVENT)
        turn = stop._turn(prompt="write a prompt")
        self.assert_disposition(self.verdict(card, "bounded result", turn, event))
        if not BASELINE:
            for kind in (memspec.TURN_CHECK_LENGTH, memspec.TURN_CHECK_PROMPT_BLOCK,
                         memspec.TURN_CHECK_OWNER_LANGUAGE, memspec.TURN_CHECK_FINISHED_AGENTS,
                         memspec.TURN_CHECK_UNVERIFIED_DELEGATION):
                with self.subTest(kind=kind):
                    self.assertIsNone(stop._turn_check_gap(card._replace(turn_check=kind),
                        "bounded result", turn, frozenset(), [], event=event))

    def test_late_block_is_never_emitted(self):
        with patch.object(stop, "expired", return_value=True):
            self.assertEqual(stop._emits({"decision": "block", "reason": "late"},
                                         time.monotonic()), BASELINE)

    @unittest.skipIf(BASELINE, "post-change adapter protocol verification")
    def test_adapter_json_roundtrip_both_hosts(self):
        import _hook_common as common
        (self.vault / "advisory.md").write_text(
            "---\nname: advisory\ndescription: ordinary advisory\nforbidden: [forbidden-text]\n---\nbody\n",
            encoding="utf-8")
        transcripts = {
            "claude": [{"type": "user", "message": {"content": "請交付結果"}}],
            "codex": [{"type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "請交付結果"}]}}],
        }
        for host, rows in transcripts.items():
            transcript = self.root / (host + ".jsonl")
            transcript.write_text("\n".join(json.dumps(row, ensure_ascii=False)
                                             for row in rows) + "\n", encoding="utf-8")
            for event_name in ("Stop", memspec.SUBAGENT_STOP_EVENT):
                with self.subTest(host=host, event=event_name):
                    event = self.event("forbidden-text", hook_event_name=event_name,
                                       transcript_path=str(transcript))
                    result = common.run_synthetic(Path(stop.__file__), event, self.config,
                        environment={"HOME": str(self.root), "USERPROFILE": str(self.root)},
                        clock=memspec.HOOK_CLOCK_FROZEN)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    output = json.loads(result.stdout) if result.stdout.strip() else {}
                    self.assertNotEqual(output.get("decision"), "block")


class StopNoteSaysWhatToDo(unittest.TestCase):
    """A bounded current-rule line; quotations and matched prose stay in the audit."""

    def test_short_fields_appear_whole(self):
        card = turn_fixtures.decision(key="短卡", advice="細節寫進檔案", quote="不要整段貼回來")
        note = stop._stop_note(card, "forbidden", "整段貼回")
        self.assertEqual(note, 'Epitype 提醒（短卡）：細節寫進檔案')
        self.assertNotIn("owner", note)

    def test_current_rule_gets_the_budget_without_replaying_history(self):
        key = "feedback_reasked_a_decided_item_auto_shutter_tier_with_a_long_tail"
        card = turn_fixtures.decision(key=key, advice="先查裁定紀錄再開問。" * 20,
                                      quote="已經裁過的事不要再問我。" * 20)
        note = stop._stop_note(card, "forbidden", "要不要改成自動快門" * 10)
        self.assertLessEqual(len(note), memspec.STOP_NOTE_MAX_CHARS)
        self.assertIn(key[:memspec.STOP_NOTE_KEY_MIN_CHARS - 1], note)
        self.assertIn('先查裁定紀錄', note)
        self.assertNotIn('已經裁過的事', note)
        self.assertNotIn('要不要改成', note)
        self.assertEqual(note, " ".join(note.split()), "leave_note would cut a note that it collapses")

    def test_a_card_without_owner_words_is_not_labelled_as_owner(self):
        card = turn_fixtures.decision(decided_by="ai-autonomous", advice="", quote="卡片說明文字")
        note = stop._stop_note(card, "forbidden", "")
        self.assertIn('請檢視這張現行規則', note)
        self.assertNotIn('卡片說明文字', note)

    def test_english_notice_and_empty_advice_use_the_language_table(self):
        card = turn_fixtures.decision(key="synthetic-rule", advice="", quote="source text")
        with patch.object(memspec, "STOP_NOTE_NOTICE", memspec._EN["STOP_NOTE_NOTICE"]), \
                patch.object(memspec, "STOP_NOTE_REVIEW_CURRENT_RULE",
                             memspec._EN["STOP_NOTE_REVIEW_CURRENT_RULE"]):
            self.assertEqual(stop._stop_note(card, "forbidden", "matched text"),
                             "Epitype reminder (synthetic-rule): Review this current rule.")

    def test_long_keys_preserve_the_advice_budget_in_both_languages(self):
        card = turn_fixtures.decision(key="synthetic-rule-" * 20,
                                     advice="Check the current evidence. " * 20)
        for template in (memspec.STOP_NOTE_NOTICE, memspec._EN["STOP_NOTE_NOTICE"]):
            with self.subTest(template=template), patch.object(memspec, "STOP_NOTE_NOTICE", template):
                note = stop._stop_note(card, "forbidden", "matched text")
                self.assertLessEqual(len(note), memspec.STOP_NOTE_MAX_CHARS)
                self.assertIn(card.key[:memspec.STOP_NOTE_KEY_MIN_CHARS - 1], note)
                self.assertIn(card.advice[:memspec.STOP_NOTE_DETAIL_MIN_CHARS - 1], note)
                self.assertNotIn("matched text", note)
                self.assertEqual(note, " ".join(note.split()))

    def test_the_hook_leaves_that_note(self):
        card = turn_fixtures.decision(forbidden=("forbidden-text",), advice="改說實際跑了什麼")
        notes = []
        with patch.object(stop, "_vaults", return_value=["unused"]), \
                patch.object(stop, "_decisions", return_value=[card]), \
                patch.object(stop, "_claim_marker", return_value=True), \
                patch.object(stop, "_audit"), \
                patch.object(stop, "leave_note", lambda config, session, text: notes.append(text)):
            self.assertIsNone(stop._verdict({"session_id": "note",
                                            "transcript_path": str(Path(__file__).resolve().with_suffix('.jsonl'))}, "forbidden-text",
                                            {"vaults": []}, time.monotonic(), []))
        self.assertEqual(len(notes), 1)
        self.assertIn("測試卡", notes[0])
        self.assertIn("改說實際跑了什麼", notes[0])
        self.assertNotIn("forbidden-text", notes[0])


@unittest.skipIf(BASELINE, "owner 2026-11-13 delivery block")
class DeliveryBlock(unittest.TestCase):
    """The one Stop veto: a delivery card with an owner-set blocking basis, once."""
    setUp = fixtures.ActionGuardRegression.setUp
    event = StopAdvisory.event
    verdict = StopAdvisory.verdict

    def card(self, **overrides):
        fields = dict(key="core-04", quote="CORE-04. 交付前照實說還有哪些沒驗證。",
                      require_when="已上線", require_text="沒驗證|還缺",
                      require_on_hit="block", source_status="REVIEWED",
                      source_blocking_basis=True)
        fields.update(overrides)
        return turn_fixtures.decision(**fields)

    def test_delivery_without_gap_blocks_with_the_rule_line(self):
        value = self.verdict(self.card(), "兩項都已上線，也實測過。")
        self.assertEqual(value["decision"], "block")
        self.assertIn("已上線", value["reason"])
        self.assertIn("CORE-04. 交付前照實說還有哪些沒驗證。", value["reason"])
        self.assertTrue(stop._emits(value, time.monotonic()))

    def test_delivery_that_states_its_gap_passes(self):
        self.assertIsNone(self.verdict(self.card(), "已上線。還缺真機那一項，沒驗證。"))

    def test_without_blocking_basis_or_review_it_stays_a_note(self):
        for overrides in ({"source_blocking_basis": False}, {"source_status": "UNVERIFIED"},
                          {"require_on_hit": ""}):
            with self.subTest(**overrides):
                self.assertIsNone(self.verdict(self.card(**overrides), "已上線。"))

    def test_only_the_final_message_and_not_fenced_code_is_the_delivery(self):
        # Mid-turn progress text is in the joined message; the final message has no gap.
        event = self.event("已上線。")
        self.assertEqual(self.verdict(self.card(), "還缺一項\n\n已上線。", event=event)["decision"],
                         "block")
        pasted = "下面這段直接貼上：\n```\n已上線\n```\n"
        self.assertIsNone(self.verdict(self.card(), pasted))

    def test_subagent_rewrite_and_late_blocks_never_veto(self):
        child = self.event("已上線。", hook_event_name=memspec.SUBAGENT_STOP_EVENT)
        self.assertIsNone(self.verdict(self.card(), "已上線。", event=child))
        with patch.object(stop, "_decisions", return_value=[self.card()]):
            self.assertIsNone(stop._handle(self.event("已上線。", stop_hook_active=True),
                                           time.monotonic(), []))
        with patch.object(stop, "expired", return_value=True):
            self.assertFalse(stop._emits({"decision": "block", "reason": "late"},
                                         time.monotonic()))


class DocumentedStopBehaviour(unittest.TestCase):
    """FAILURE_MODES §10 is what an operator reads to learn what Stop does: notes by
    default (2026-10-03) and the single delivery block the owner set on 2026-11-13."""

    def test_section_10_describes_the_note_and_the_one_delivery_block(self):
        text = (Path(__file__).resolve().parents[1] / "docs" / "FAILURE_MODES.md").read_text(
            encoding="utf-8")
        section = text.split("\n## 10. ", 1)[1].split("\n## 11. ", 1)[0]
        self.assertIn("never blocks", section)
        self.assertIn(memspec.STOP_NOTE_LOG_KIND, section)
        self.assertIn("require_on_hit: block", section)
        self.assertIn("at most once", section)
        self.assertNotIn("blocks the turn", section)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
