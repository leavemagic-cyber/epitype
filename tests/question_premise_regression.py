import sys; sys.dont_write_bytecode = True
"""Delivery contracts, not an automated semantic-evidence grader."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import memsearch, memspec
import _hook_common as common
import recall_hook as recall
import sessionstart_hook as start
import pretooluse_gate as pretool
import stop_gate as stop


class QuestionPremiseRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-question-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {
            memspec.EPITYPE_CONFIG_ENV: str(self.config),
            memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        marker = patch.object(recall, "recall_marker_directory",
                              lambda session: self.root / "markers" / session)
        marker.start()
        self.addCleanup(marker.stop)

    @staticmethod
    def context(value):
        return value["hookSpecificOutput"]["additionalContext"] if value else ""

    @staticmethod
    def guide():
        return memspec.QUESTION_PREFLIGHT + "\n" + getattr(memspec, "TURN_CONTINUITY", "MISSING CONTINUITY PROCEDURE")

    def test_before_generation_without_hits_or_question_keywords(self):
        for prompt in ("繼續。", "Proceed with the design.", "42"):
            value = recall._handle({"prompt": prompt}, time.monotonic())
            self.assertEqual(self.context(value), self.guide())
            self.assertNotIn("decision", value)

    def test_option_and_choice_procedure_reaches_both_entry_paths(self):
        # Text delivery, not a semantic verdict about any model's answer.
        for hook, event in ((recall, {"prompt": "continue"}),
                            (start, {"source": "resume"})):
            context = self.context(hook._handle(event, time.monotonic()))
            for clause in (
                "Check each option label and description too",
                "Missing tests do not prove missing implementation or infeasibility",
                "without taking over a requested user choice",
            ):
                self.assertIn(clause, context)

    def test_repeat_turn_keeps_procedure_without_repeating_cards(self):
        (self.vault / "fact.md").write_text(
            "---\nname: fact\ndescription: fixturepremise\n---\n", encoding="utf-8")
        memsearch.build_index(self.vault)
        event = {"prompt": "fixturepremise", "session_id": "fixture-question"}
        markers = []
        first = self.context(recall._handle(event, time.monotonic(), markers))
        for session, digest in markers:
            recall._claim_marker(session, digest)
        self.assertIn("fact.md", first)
        self.assertEqual(self.context(recall._handle(event, time.monotonic())), self.guide())

    def test_session_entry_restores_procedure_ahead_of_index(self):
        (self.vault / memspec.MEMORY_INDEX_FILENAME).write_text("noise\n" * 2000, encoding="utf-8")
        for source in ("startup", "resume", "clear", "compact", None):
            context = self.context(start._handle({"source": source}, time.monotonic()))
            self.assertTrue(context.startswith(self.guide()))
            self.assertLessEqual(len(context.encode("utf-8")), memspec.HOOK_DEFAULT_BUDGET_BYTES)

    def test_budget_never_truncates_procedure(self):
        size = len(self.guide().encode("utf-8"))
        for budget in (size, size + 1, memspec.HOOK_DEFAULT_BUDGET_BYTES):
            common.write_config(self.config, [self.vault], budget=budget)
            value = recall._handle({"prompt": "continue"}, time.monotonic())
            self.assertEqual(self.context(value), self.guide())
            self.assertTrue(common.payload_fits("UserPromptSubmit", self.context(value), budget))
            for source in ("resume", "compact"):
                restored = start._handle({"source": source}, time.monotonic())
                self.assertTrue(self.context(restored).startswith(self.guide()))

    def test_small_budget_reports_omission_and_fails_open(self):
        common.write_config(self.config, [self.vault], budget=100)
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            value = recall._handle({"prompt": "continue"}, time.monotonic())
        self.assertIsNone(value)
        self.assertIn("preflight omitted", error.getvalue())

    def test_combined_json_escaping_budget_preserves_complete_output(self):
        guide = memspec.QUESTION_PREFLIGHT
        pieces = ["advisory", '- ' + '"' * 4600, "- short fact"]
        context = recall._bounded_recall(pieces, 8192, 1, 1, prefix=guide)
        self.assertIn("short fact", context)
        self.assertNotIn('"' * 4600, context)
        value = common.payload("UserPromptSubmit", guide + "\n" + context)
        self.assertLessEqual(len(common.encode_payload(value).encode("utf-8")), memspec.HOOK_MAX_OUTPUT_BYTES)
        with contextlib.redirect_stdout(io.StringIO()):
            common.emit(value)

    def test_invalid_input_and_deadline_do_not_authorize_or_block(self):
        for prompt in (None, {}, ""):
            self.assertIsNone(recall._handle({"prompt": prompt}, time.monotonic()))
        self.assertIsNone(recall._handle({"prompt": "continue"}, 0))

    def test_continuity_not_conditioned_on_followup_keywords(self):
        # Delivery only: none of these inputs is classified as authority or
        # completion by the hook. Correct next actions need model evaluation.
        for prompt in (
            "可以。", "順便問一下，原因是什麼？", "還有另一個錯誤。", "做完了？",
            "Diagnosis only; do not modify anything.", "Pause the work now.",
            "We need the owner's colour preference.", "All scoped deliverables passed.",
            "Quoted bad example: 'I finished the first step, so I stopped.'",
        ):
            value = recall._handle({"prompt": prompt}, time.monotonic())
            # Capture may recall a prior synthetic correction later in this
            # loop; the procedure must stay first and appear exactly once.
            self.assertTrue(self.context(value).startswith(self.guide()))
            self.assertEqual(self.context(value).count(self.guide()), 1)
            self.assertNotIn("decision", value)

    def test_question_only_budget_preserves_old_procedure(self):
        budget = len(memspec.QUESTION_PREFLIGHT.encode("utf-8"))
        common.write_config(self.config, [self.vault], budget=budget)
        for hook, event in ((recall, {"prompt": "continue"}), (start, {"source": "compact"})):
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                value = hook._handle(event, time.monotonic())
            self.assertEqual(self.context(value), memspec.QUESTION_PREFLIGHT)
            self.assertIn("continuity procedure omitted", error.getvalue())

    def test_no_new_stop_loop_or_false_completion_detector(self):
        for message in (
            "The first step is saved; the integration test is still pending.",
            "Here is the diagnosis. Implementation has not been done.",
            "Which colour do you prefer?", "Please approve this external upload.",
            "Paused as requested.", "All requested deliverables are verified.",
            "Example of a bad answer: 'I stopped after the first step.'",
        ):
            for active in (False, True):
                self.assertIsNone(stop._handle({"last_assistant_message": message,
                                               "stop_hook_active": active}, time.monotonic(), []))

    def test_no_keyword_gate_or_self_certification(self):
        # Bad premises ALSO pass these gates. This proves no NEW denial,
        # not semantic correctness. Model evaluation is a separate lane.
        texts = (
            "Both untested features together: which sequence do you prefer?",
            "Please look up the configured drive for me.",
            "Both paths passed integration tests; which layout do you prefer?",
            "Hypothetical feature requiring development: worth investing in?",
            "What genre do you enjoy? May I send this outside the workspace?",
            "Quoted failure example: 'Since sync is perfect, choose merge mode.'",
        )
        for text in texts:
            for name in ("AskUserQuestion", "request_user_input", "request_user_input_async"):
                value = pretool._handle({"tool_name": name, "tool_input": {
                    "questions": [{"question": text, "options": [{
                        "label": "Guaranteed cheapest",
                        "description": "Only the scheduler is missing; instant delivery.",
                    }]}], "verified": True,
                }}, time.monotonic())
                output = (value or {}).get("hookSpecificOutput", {})
                self.assertNotEqual(output.get("permissionDecision"), "deny")
                self.assertNotIn("updatedInput", output)
            self.assertIsNone(stop._handle({"last_assistant_message": text}, time.monotonic(), []))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
