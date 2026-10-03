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
from epitype import memspec

BASELINE = "--baseline" in sys.argv
if BASELINE:
    sys.argv.remove("--baseline")


class StopAdvisory(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp

    def event(self, text="一般回覆", **extra):
        return {"session_id": "advisory-" + self.root.name,
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
                    self.assertEqual(len(defects), 1)
                    self.assertNotIn("\n", defects[0])

    def test_stop_attribution_timeout_is_advisory(self):
        with patch.object(stop.rule_provenance, "claim_violation",
                          return_value="admission deadline exhausted; worker reaped"):
            defects = []
            result = stop._handle(self.event(), time.monotonic(), defects)
            self.assert_disposition(result)
            if not BASELINE:
                self.assertEqual(len(defects), 1)

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


if __name__ == "__main__":
    unittest.main()
