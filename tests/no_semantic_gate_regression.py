import sys; sys.dont_write_bytecode = True
"""No product-side semantic gate: neither hook invents a denial from wording.

The pre-generation procedures this file used to validate were removed by owner
ruling 2026-09-09 (FAILURE_MODES §30). What survives is the negative half, and
it still guards the gates that stayed: a question tool must not be denied for
its premises, and a completion-sounding turn must not be blocked for its words.
"""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import memspec
import _hook_common as common
import recall_hook as recall
import pretooluse_gate as pretool
import stop_gate as stop


class NoSemanticGateRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-nogate-")
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

    def test_a_prompt_with_no_hit_adds_nothing(self):
        # Owner 2026-09-09: no product-side guidance text on any prompt.
        for prompt in ("繼續。", "Proceed with the design.", "42"):
            self.assertIsNone(recall._handle({"prompt": prompt}, time.monotonic()))

    def test_invalid_input_and_deadline_do_not_authorize_or_block(self):
        for prompt in (None, {}, ""):
            self.assertIsNone(recall._handle({"prompt": prompt}, time.monotonic()))
        self.assertIsNone(recall._handle({"prompt": "continue"}, 0))

    def test_no_stop_loop_or_false_completion_detector(self):
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
