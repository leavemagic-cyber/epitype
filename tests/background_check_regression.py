import sys; sys.dont_write_bytecode = True
"""A Bash call the host moved to the background must be checked before anything else.

Claude Code moves a foreground Bash call past its timeout to the background; the desktop
app gives background commands no time limit and a CLI's background shells outlive it. On
2026-10-12 one such runaway command, unwatched, started 1,712 processes. PostToolUse
records the task; PreToolUse then denies every call of that session (or subagent) until a
call looks at it: Read of its output, a Bash/PowerShell command naming its id or output
path, or TaskOutput/BashOutput/TaskStop/KillShell carrying its id. Codex events are out of
scope and any internal failure allows the call with a named defect.

Nothing here executes a shell command: the hook is called in-process or through its own
Python entry point with a JSON event.
"""
import json
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
import pretooluse_gate as pretool

TASK = "bcf3uhsr1"
OUTPUT = r"D:\Temp\claude\C--\0c4220d4\tasks\bcf3uhsr1.output"
# The host's own wording, as the model saw it on 2026-10-12 (path anonymised).
HOST_TEXT = (
    f"Command did not complete within its 120s timeout and was moved to the background "
    f"(ID: {TASK}). Output is being written to: {OUTPUT}. You will be notified when it "
    "completes. To check interim output, use Read on that file path."
)
SESSION = "background-check-test"


class Detection(unittest.TestCase):
    """What counts as a call the host moved to the background."""

    def post(self, response, **extra):
        return {"hook_event_name": "PostToolUse", "session_id": SESSION, "tool_name": "Bash",
                "transcript_path": "/srv/u/.claude/projects/p/s.jsonl",
                "tool_input": {"command": "sleep 999"}, "tool_response": response, **extra}

    def test_the_host_text_gives_the_id_and_the_output_path(self):
        self.assertEqual(pretool._moved_to_background(self.post(HOST_TEXT)), (TASK, OUTPUT))
        self.assertEqual(pretool._moved_to_background(self.post({"stdout": "", "message": HOST_TEXT})),
                         (TASK, OUTPUT))

    def test_the_structured_result_gives_the_id(self):
        response = {"stdout": "", "stderr": "", "interrupted": False,
                    "backgroundTaskId": TASK, "timedOutAfterMs": 120000}
        self.assertEqual(pretool._moved_to_background(self.post(response)), (TASK, ""))

    def test_a_call_that_asked_for_the_background_is_not_counted(self):
        self.assertIsNone(pretool._moved_to_background(self.post(
            {"stdout": "", "stderr": "", "interrupted": False, "backgroundTaskId": TASK})))
        self.assertIsNone(pretool._moved_to_background(self.post(
            f"Command running in background with ID: {TASK}. Output is being written to: {OUTPUT}.")))

    def test_ordinary_results_and_other_tools_are_not_counted(self):
        self.assertIsNone(pretool._moved_to_background(self.post({"stdout": "done", "stderr": ""})))
        self.assertIsNone(pretool._moved_to_background({**self.post(HOST_TEXT), "tool_name": "PowerShell"}))

    def test_codex_events_are_not_counted(self):
        self.assertIsNone(pretool._moved_to_background(self.post(HOST_TEXT, turn_id="t1")))
        codex = self.post(HOST_TEXT)
        codex["transcript_path"] = "/srv/u/.codex/sessions/2026/10/13/rollout-x.jsonl"
        self.assertIsNone(pretool._moved_to_background(codex))


class Gate(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-background-check-")
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

    def base(self, **extra):
        return {"session_id": SESSION, "cwd": str(self.root),
                "transcript_path": str(self.root / ".claude" / "projects" / "p" / "s.jsonl"), **extra}

    def post(self, response=HOST_TEXT, **extra):
        return {**self.base(**extra), "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "sleep 999"}, "tool_response": response}

    def pre(self, tool_name, tool_input, **extra):
        return {**self.base(**extra), "hook_event_name": "PreToolUse", "tool_name": tool_name,
                "tool_input": tool_input}

    def record(self, response=HOST_TEXT, **extra):
        return pretool._record_background(self.post(response, **extra), time.monotonic())

    def call(self, event, defects=None):
        return pretool._handle(event, time.monotonic(), [] if defects is None else defects)

    @staticmethod
    def denial(value):
        output = (value or {}).get("hookSpecificOutput", {})
        if output.get("permissionDecision") != "deny":
            return None
        return output.get("permissionDecisionReason")

    def state_path(self, agent=None):
        name = SESSION + (f"__{agent}" if agent else "")
        return self.vault / memspec.FTS_INDEX_DIRECTORY / memspec.BACKGROUND_CHECK_STATE_DIRECTORY / (name + ".json")

    def pending_ids(self, agent=None):
        path = self.state_path(agent)
        if not path.is_file():
            return []
        return [item["id"] for item in json.loads(path.read_text(encoding="utf-8"))["pending"]]

    def audit_rows(self):
        path = self.vault / memspec.GATE_LOG_FILENAME
        if not path.is_file():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [row for row in rows if row.get("rule") == memspec.BACKGROUND_CHECK_RULE]

    def unrelated(self):
        return self.pre("Edit", {"file_path": str(self.root / "a.py"), "old_string": "a", "new_string": "b"})

    # -- recording -------------------------------------------------------------------

    def test_a_hit_is_recorded_with_its_id_and_output_path(self):
        self.assertEqual(self.record(), TASK)
        state = json.loads(self.state_path().read_text(encoding="utf-8"))["pending"]
        self.assertEqual([(item["id"], item["path"]) for item in state], [(TASK, OUTPUT)])

    def test_the_same_task_is_recorded_once(self):
        self.record()
        self.record()
        self.assertEqual(self.pending_ids(), [TASK])

    def test_a_normal_result_records_nothing(self):
        self.assertIsNone(self.record({"stdout": "ok", "stderr": ""}))
        self.assertFalse(self.state_path().exists())

    def test_the_hook_entry_point_records_then_denies(self):
        posted = common.run_synthetic(Path(pretool.__file__), self.post(), self.config)
        self.assertEqual(posted.returncode, 0, posted.stderr)
        self.assertEqual(self.pending_ids(), [TASK])
        result = common.run_synthetic(Path(pretool.__file__), self.pre("Bash", {"command": "git status"}),
                                      self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual((output["hookEventName"], output["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn(TASK, output["permissionDecisionReason"])

    # -- denying ---------------------------------------------------------------------

    def test_a_call_that_is_not_a_check_is_denied_and_audited(self):
        self.record()
        reason = self.denial(self.call(self.unrelated()))
        self.assertIsNotNone(reason)
        self.assertLessEqual(len(reason), memspec.BACKGROUND_CHECK_REASON_MAX_CHARS)
        self.assertIn(TASK, reason)
        self.assertIn(OUTPUT, reason)
        self.assertIn("TaskOutput", reason)
        self.assertEqual(self.pending_ids(), [TASK])
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["tool"], rows[0]["task"], rows[0]["session_id"]),
                         (memspec.ACTION_GUARD_LOG_KIND, "Edit", TASK, SESSION))

    def test_unrelated_reads_and_commands_are_denied(self):
        self.record()
        for event in (self.pre("Read", {"file_path": str(self.root / "notes.md")}),
                      self.pre("Bash", {"command": "ls -la"}),
                      self.pre("Bash", {"command": f"echo x{TASK}y"}),  # not the id as a word
                      self.pre("TaskOutput", {"task_id": "b0000000"}),
                      self.pre("Agent", {"prompt": "carry on"})):
            with self.subTest(tool=event["tool_name"]):
                self.assertIsNotNone(self.denial(self.call(event)))
        self.assertEqual(self.pending_ids(), [TASK])

    def test_without_anything_pending_nothing_is_denied(self):
        self.assertIsNone(self.denial(self.call(self.unrelated())))
        self.assertEqual(self.audit_rows(), [])

    def test_the_reason_fits_with_the_longest_id_and_path(self):
        task = "b" * 64
        path = "C:\\" + "very-long-directory\\" * 40 + task + ".output"
        reason = pretool._background_reason([{"id": task, "path": path}, {"id": "b2", "path": ""}])
        self.assertLessEqual(len(reason), memspec.BACKGROUND_CHECK_REASON_MAX_CHARS)
        self.assertIn(task, reason)
        self.assertIn(memspec.BACKGROUND_CHECK_MORE.format(count=1), reason)

    # -- checking clears it ----------------------------------------------------------

    def assertClears(self, event):
        self.record()
        self.assertIsNone(self.denial(self.call(event)))
        self.assertEqual(self.pending_ids(), [])
        self.assertIsNone(self.denial(self.call(self.unrelated())))

    def test_read_of_the_output_path_clears_it(self):
        self.assertClears(self.pre("Read", {"file_path": OUTPUT}))

    def test_read_of_the_output_path_in_another_spelling_clears_it(self):
        self.assertClears(self.pre("Read", {"file_path": OUTPUT.replace("\\", "/").upper()}))

    def test_bash_naming_the_id_clears_it(self):
        self.assertClears(self.pre("Bash", {"command": f"tail -n 30 /tmp/tasks/{TASK}.output"}))

    def test_bash_naming_the_output_path_in_git_bash_form_clears_it(self):
        self.record(HOST_TEXT.replace(OUTPUT, r"C:\Temp\claude\job.log"))
        command = "tail -n 30 /c/Temp/claude/job.log"
        self.assertIsNone(self.denial(self.call(self.pre("Bash", {"command": command}))))
        self.assertEqual(self.pending_ids(), [])

    def test_powershell_naming_the_output_path_clears_it(self):
        self.assertClears(self.pre("PowerShell", {"command": f"Get-Content '{OUTPUT}' -Tail 30"}))

    def test_task_tools_carrying_the_id_clear_it(self):
        for tool, arguments in (("TaskOutput", {"task_id": TASK, "block": False}),
                                ("TaskStop", {"task_id": TASK}),
                                ("BashOutput", {"bash_id": TASK}),
                                ("KillShell", {"shell_id": TASK})):
            with self.subTest(tool=tool):
                self.assertClears(self.pre(tool, arguments))

    def test_one_check_clears_only_its_own_task(self):
        self.record()
        self.record(HOST_TEXT.replace(TASK, "b2second"))
        reason = self.denial(self.call(self.unrelated()))
        self.assertIn(memspec.BACKGROUND_CHECK_MORE.format(count=1), reason)
        self.assertIsNone(self.denial(self.call(self.pre("TaskOutput", {"task_id": "b2second"}))))
        self.assertEqual(self.pending_ids(), [TASK])
        self.assertIsNotNone(self.denial(self.call(self.unrelated())))

    def test_a_subagent_task_does_not_stop_the_main_thread(self):
        self.record(agent_id="agent-a")
        self.assertEqual(self.pending_ids("agent-a"), [TASK])
        self.assertIsNone(self.denial(self.call(self.unrelated())))
        sub = self.unrelated()
        sub["agent_id"] = "agent-a"
        self.assertIsNotNone(self.denial(self.call(sub)))

    # -- out of scope and failures ---------------------------------------------------

    def test_codex_events_are_never_recorded_or_denied(self):
        self.assertIsNone(self.record(turn_id="t1"))
        self.assertFalse(self.state_path().exists())
        self.record()
        self.assertIsNone(self.denial(self.call(self.pre("Bash", {"command": "ls"}, turn_id="t1"))))
        codex = self.unrelated()
        codex["transcript_path"] = str(self.root / ".codex" / "sessions" / "rollout-x.jsonl")
        self.assertIsNone(self.denial(self.call(codex)))
        self.assertEqual(self.pending_ids(), [TASK])

    def test_a_malformed_state_allows_the_call_with_a_defect(self):
        path = self.state_path()
        path.parent.mkdir(parents=True)
        path.write_text('{"pending": "broken"}', encoding="utf-8")
        defects = []
        self.assertIsNone(self.denial(self.call(self.unrelated(), defects)))
        self.assertTrue(any(line.startswith("background check UNVERIFIED: review failed (ValueError)")
                            for line in defects), defects)
        self.assertEqual(self.record(), TASK)  # recording replaces the malformed state

    def test_a_review_failure_allows_the_call_with_a_defect(self):
        self.record()
        defects = []
        with patch.object(pretool, "_checks_background", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.denial(self.call(self.unrelated(), defects)))
        self.assertTrue(any("background check UNVERIFIED: review failed (RuntimeError)" in line
                            for line in defects), defects)

    def test_a_recording_failure_is_named_and_the_hook_still_exits_zero(self):
        self.config.write_text("{broken", encoding="utf-8")
        result = common.run_synthetic(Path(pretool.__file__), self.post(), self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("background check UNVERIFIED: recording failed", result.stderr)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
