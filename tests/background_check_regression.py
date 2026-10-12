import sys; sys.dont_write_bytecode = True
"""A Bash call the host moved to the background must be checked before anything else.

Claude Code moves a foreground Bash call past its timeout to the background; the desktop
app gives background commands no time limit and a CLI's background shells outlive it. On
2026-10-12 one such runaway command, unwatched, started 1,712 processes. The PreToolUse
gate reads the tail of the thread's own transcript, finds the latest such call and denies
every other call until one looks at it: Read of its output, a Bash/PowerShell command
naming its id or output path, or TaskOutput/BashOutput/TaskStop/KillShell carrying its id.
Codex events are out of scope; a transcript that cannot be read allows the call with a
named defect.

The transcripts here are synthetic, in the host's row shape. One opt-in test reads a real
transcript (read-only) when EPITYPE_BACKGROUND_TRANSCRIPT names it and
EPITYPE_BACKGROUND_TASK names a task that was checked in it; the session id stays out of
the repo. Nothing here executes a shell command.
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
from epitype import memspec, tool_evidence
import _hook_common as common
import pretooluse_gate as pretool

REAL_TRANSCRIPT_ENV = "EPITYPE_BACKGROUND_TRANSCRIPT"
REAL_TASK_ENV = "EPITYPE_BACKGROUND_TASK"
TASK = "bcf3uhsr1"
OUTPUT = r"D:\Temp\claude\C--\0c4220d4\tasks\bcf3uhsr1.output"
SESSION = "background-check-test"


def host_text(task=TASK, output=OUTPUT, cwd_hint=True):
    """The host's own result text, as recorded in real transcripts (paths anonymised)."""
    text = (f"Command did not complete within its 120s timeout and was moved to the background "
            f"(ID: {task}). Output is being written to: {output}. You will be notified when it "
            "completes. To check interim output, use Read on that file path.")
    if cwd_hint:
        text += ("\nSession cwd remains D:\\work; directory changes made by the backgrounded "
                 "command do not apply to subsequent commands.")
    return text


def background_meta(task=TASK):
    return {"stdout": "", "stderr": "", "interrupted": False, "isImage": False,
            "backgroundTaskId": task, "timedOutAfterMs": 120000}


class Transcript:
    """Rows in Claude Code's transcript shape: a tool_use row, then its tool_result row."""

    def __init__(self, agent=None):
        self.rows = []
        self.agent = agent
        self.count = 0

    def _row(self, kind, content, **extra):
        row = {"type": kind, "message": {"role": kind, "content": content}, **extra}
        if self.agent:
            row.update(agentId=self.agent, isSidechain=True)
        self.rows.append(row)

    def call(self, name, arguments, result="ok", meta=None):
        self.count += 1
        identity = f"toolu_{self.count:04d}"
        self._row("assistant", [{"type": "tool_use", "id": identity, "name": name, "input": arguments}])
        extra = {} if meta is None else {"toolUseResult": meta}
        self._row("user", [{"type": "tool_result", "tool_use_id": identity, "content": result}], **extra)
        return self

    def background(self, task=TASK, output=OUTPUT, structured=True, text=True):
        return self.call("Bash", {"command": "python long_suite.py"},
                         result=host_text(task, output) if text else "",
                         meta=background_meta(task) if structured else None)

    def filler(self, count, size=900):
        for _ in range(count):
            self.call("Bash", {"command": "echo filler"}, result="x" * size,
                      meta={"stdout": "x" * size, "stderr": "", "interrupted": False})
        return self

    def write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in self.rows), encoding="utf-8")
        return path


def pending(transcript, actor=None):
    return pretool._background_pending(transcript.rows, actor)


class Detection(unittest.TestCase):
    """What counts as a call the host moved to the background, and what counts as checked."""

    def test_the_structured_result_with_the_host_text_gives_id_and_path(self):
        self.assertEqual(pending(Transcript().background()), {"id": TASK, "path": OUTPUT})

    def test_the_structured_result_alone_gives_the_id(self):
        self.assertEqual(pending(Transcript().background(text=False)), {"id": TASK, "path": ""})

    def test_the_host_text_alone_counts_where_rows_carry_no_structured_result(self):
        # Subagent transcripts mostly carry no toolUseResult (2026-10-13: 3 of 131 rows).
        self.assertEqual(pending(Transcript().background(structured=False)), {"id": TASK, "path": OUTPUT})

    def test_a_call_that_asked_for_the_background_is_not_counted(self):
        asked = Transcript().call(
            "Bash", {"command": "python long_suite.py", "run_in_background": True},
            result=f"Command running in background with ID: {TASK}. Output is being written to: {OUTPUT}.",
            meta={"stdout": "", "stderr": "", "interrupted": False, "backgroundTaskId": TASK})
        self.assertIsNone(pending(asked))

    def test_output_that_quotes_the_host_text_is_not_counted(self):
        quoted = host_text(cwd_hint=False) + "\n" + host_text("b2second", cwd_hint=False)
        self.assertIsNone(pending(Transcript().call("Bash", {"command": "grep -h moved *.jsonl"},
                                                    result=quoted)))
        self.assertIsNone(pending(Transcript().call("Bash", {"command": "cat note.txt"}, result=host_text(),
                                                    meta={"stdout": host_text(), "stderr": ""})))
        escaped = host_text(cwd_hint=False) + "\\n"  # raw JSON grepped from a transcript
        self.assertIsNone(pending(Transcript().call("Bash", {"command": "grep -o x"}, result=escaped)))

    def test_ordinary_results_and_other_tools_are_not_counted(self):
        self.assertIsNone(pending(Transcript().call("Bash", {"command": "ls"}, meta={"stdout": "a"})))
        self.assertIsNone(pending(Transcript().call("PowerShell", {"command": "Start-Sleep 999"},
                                                    result=host_text(), meta=background_meta())))

    def test_a_later_call_that_looks_at_it_clears_it(self):
        for name, arguments in (("Read", {"file_path": OUTPUT}),
                                ("Bash", {"command": f"tail -n 30 /tmp/{TASK}.output"}),
                                ("PowerShell", {"command": f"Get-Content '{OUTPUT}' -Tail 30"}),
                                ("TaskOutput", {"task_id": TASK, "block": False}),
                                ("TaskStop", {"task_id": TASK}),
                                ("BashOutput", {"bash_id": TASK}),
                                ("KillShell", {"shell_id": TASK})):
            with self.subTest(tool=name):
                self.assertIsNone(pending(Transcript().background().call(name, arguments)))

    def test_a_monitor_naming_the_id_or_the_output_path_clears_it(self):
        self.assertIsNone(pending(Transcript().background().call(
            "Monitor", {"command": f"until grep -q done /tmp/{TASK}.output; do sleep 5; done",
                        "description": "wait for the suite"})))
        log = r"D:\Temp\claude\job.log"
        self.assertIsNone(pending(Transcript().background(output=log).call(
            "Monitor", {"command": "until grep -q TOTAL /d/Temp/claude/job.log; do sleep 5; done"})))
        self.assertIsNone(pending(Transcript().background(output=log).call(
            "Monitor", {"description": "watch D:/TEMP/Claude/job.log"})))

    def test_a_monitor_on_another_file_does_not_clear_it(self):
        # 2026-10-13: a subagent watched the file its command redirected to, never the task.
        transcript = Transcript().background().call(
            "Monitor", {"command": "until grep -q TOTAL /d/Temp/full2.txt; do sleep 5; done"})
        self.assertEqual(pending(transcript)["id"], TASK)

    def test_a_call_before_the_result_or_about_something_else_does_not_clear_it(self):
        transcript = Transcript().call("Read", {"file_path": OUTPUT}).background()
        transcript.call("Bash", {"command": f"echo x{TASK}y"}).call("TaskOutput", {"task_id": "b0000000"})
        self.assertEqual(pending(transcript)["id"], TASK)

    def test_only_the_latest_background_call_counts(self):
        transcript = Transcript().background().background("b2second")
        self.assertEqual(pending(transcript)["id"], "b2second")
        transcript.call("TaskOutput", {"task_id": "b2second"})
        self.assertIsNone(pending(transcript))


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
        self.main = self.root / ".claude" / "projects" / "p" / (SESSION + ".jsonl")

    def subagent_file(self, agent):
        return self.main.with_suffix("") / "subagents" / f"agent-{agent}.jsonl"

    def pre(self, tool_name, tool_input, transcript=None, **extra):
        return {"hook_event_name": "PreToolUse", "session_id": SESSION, "cwd": str(self.root),
                "transcript_path": str(transcript or self.main), "tool_name": tool_name,
                "tool_input": tool_input, **extra}

    def unrelated(self, **extra):
        return self.pre("Edit", {"file_path": str(self.root / "a.py"), "old_string": "a", "new_string": "b"},
                        **extra)

    def call(self, event, defects=None):
        return pretool._handle(event, time.monotonic(), [] if defects is None else defects)

    @staticmethod
    def denial(value):
        output = (value or {}).get("hookSpecificOutput", {})
        if output.get("permissionDecision") != "deny":
            return None
        return output.get("permissionDecisionReason")

    def audit_rows(self):
        path = self.vault / memspec.GATE_LOG_FILENAME
        if not path.is_file():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [row for row in rows if row.get("rule") == memspec.BACKGROUND_CHECK_RULE]

    # -- denying ---------------------------------------------------------------------

    def test_a_call_that_is_not_a_check_is_denied_and_audited(self):
        Transcript().background().write(self.main)
        reason = self.denial(self.call(self.unrelated()))
        self.assertIsNotNone(reason)
        self.assertLessEqual(len(reason), memspec.BACKGROUND_CHECK_REASON_MAX_CHARS)
        for part in (TASK, OUTPUT, "TaskOutput"):
            self.assertIn(part, reason)
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["tool"], rows[0]["task"], rows[0]["session_id"]),
                         (memspec.ACTION_GUARD_LOG_KIND, "Edit", TASK, SESSION))

    def test_unrelated_reads_and_commands_are_denied(self):
        Transcript().background().write(self.main)
        for event in (self.pre("Read", {"file_path": str(self.root / "notes.md")}),
                      self.pre("Bash", {"command": "ls -la"}),
                      self.pre("Bash", {"command": f"echo x{TASK}y"}),
                      self.pre("TaskOutput", {"task_id": "b0000000"}),
                      self.pre("Agent", {"prompt": "carry on"})):
            with self.subTest(tool=event["tool_name"]):
                self.assertIsNotNone(self.denial(self.call(event)))

    def test_without_a_background_call_nothing_is_denied(self):
        Transcript().call("Bash", {"command": "ls"}, meta={"stdout": "a"}).write(self.main)
        self.assertIsNone(self.denial(self.call(self.unrelated())))
        self.assertEqual(self.audit_rows(), [])

    def test_the_reason_fits_with_the_longest_id_and_path(self):
        task = "b" * 64
        path = "D:\\" + "very-long-directory\\" * 40 + task + ".output"
        reason = pretool._background_reason({"id": task, "path": path})
        self.assertLessEqual(len(reason), memspec.BACKGROUND_CHECK_REASON_MAX_CHARS)
        self.assertIn(task, reason)

    def test_it_holds_without_a_readable_configuration(self):
        Transcript().background().write(self.main)
        self.config.write_text("{broken", encoding="utf-8")
        self.assertIsNotNone(self.denial(self.call(self.unrelated())))
        self.assertEqual(self.audit_rows(), [])

    def test_the_hook_entry_point_emits_the_deny_shape(self):
        Transcript().background().write(self.main)
        result = common.run_synthetic(Path(pretool.__file__), self.pre("Bash", {"command": "git status"}),
                                      self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual((output["hookEventName"], output["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn(TASK, output["permissionDecisionReason"])

    # -- the current call is the check -----------------------------------------------

    def assertCheckPasses(self, event, output=OUTPUT):
        Transcript().background(output=output).write(self.main)
        self.assertIsNone(self.denial(self.call(event)))

    def test_read_of_the_output_path_passes(self):
        self.assertCheckPasses(self.pre("Read", {"file_path": OUTPUT}))
        self.assertCheckPasses(self.pre("Read", {"file_path": OUTPUT.replace("\\", "/").upper()}))

    def test_bash_naming_the_id_or_the_path_in_git_bash_form_passes(self):
        self.assertCheckPasses(self.pre("Bash", {"command": f"tail -n 30 /tmp/tasks/{TASK}.output"}))
        self.assertCheckPasses(self.pre("Bash", {"command": "tail -n 30 /d/Temp/claude/job.log"}),
                               output=r"D:\Temp\claude\job.log")

    def test_powershell_naming_the_output_path_passes(self):
        self.assertCheckPasses(self.pre("PowerShell", {"command": f"Get-Content '{OUTPUT}' -Tail 30"}))

    def test_task_tools_carrying_the_id_pass(self):
        for tool, arguments in (("TaskOutput", {"task_id": TASK, "block": False}),
                                ("TaskStop", {"task_id": TASK}),
                                ("BashOutput", {"bash_id": TASK}),
                                ("KillShell", {"shell_id": TASK})):
            with self.subTest(tool=tool):
                self.assertCheckPasses(self.pre(tool, arguments))

    def test_a_monitor_on_the_task_passes_and_one_on_another_file_is_denied(self):
        self.assertCheckPasses(self.pre("Monitor", {"command": f"tail -f /tmp/tasks/{TASK}.output"}))
        self.assertCheckPasses(self.pre("Monitor", {"command": f"until grep -q x '{OUTPUT}'; do sleep 5; done"}))
        self.assertIsNotNone(self.denial(self.call(self.pre("Monitor", {"command": "tail -f /d/Temp/full2.txt"}))))

    def test_once_a_check_is_in_the_transcript_other_calls_pass(self):
        Transcript().background().call("TaskOutput", {"task_id": TASK}).write(self.main)
        self.assertIsNone(self.denial(self.call(self.unrelated())))

    # -- scope -----------------------------------------------------------------------

    def test_codex_events_are_never_denied(self):
        Transcript().background().write(self.main)
        self.assertIsNone(self.denial(self.call(self.pre("Bash", {"command": "ls"}, turn_id="t1"))))
        codex = self.root / ".codex" / "sessions" / "rollout-x.jsonl"
        Transcript().background().write(codex)
        self.assertIsNone(self.denial(self.call(self.unrelated(transcript=codex))))

    def test_a_subagent_reads_only_its_own_transcript(self):
        Transcript().background().write(self.main)
        Transcript(agent="a1b2").call("Bash", {"command": "ls"}).write(self.subagent_file("a1b2"))
        self.assertIsNone(self.denial(self.call(self.unrelated(agent_id="a1b2"))))
        Transcript(agent="c3d4").background("b2second").write(self.subagent_file("c3d4"))
        self.assertIn("b2second", self.denial(self.call(self.unrelated(agent_id="c3d4"))))
        # The event may also name the subagent's own file directly.
        self.assertIn("b2second", self.denial(self.call(self.unrelated(transcript=self.subagent_file("c3d4")))))
        # The main thread is held by its own task only.
        self.assertIn(TASK, self.denial(self.call(self.unrelated())))

    def test_the_main_thread_is_not_held_by_a_subagent_task_in_its_file(self):
        Transcript(agent="a1b2").background().write(self.main)  # sidechain rows in the main file
        self.assertIsNone(self.denial(self.call(self.unrelated())))
        self.assertIsNone(self.denial(self.call(self.unrelated(agent_id="zz99"))))

    def test_a_subagent_without_its_own_file_reads_its_rows_in_the_main_file(self):
        Transcript(agent="e5f6").background().write(self.main)
        self.assertIn(TASK, self.denial(self.call(self.unrelated(agent_id="e5f6"))))
        self.assertIn(TASK, self.denial(self.call(self.unrelated(agent_id="agent-e5f6"))))

    # -- bounded reading and failures ------------------------------------------------

    def test_a_background_call_outside_the_read_window_passes(self):
        limit = 16 * 1024
        Transcript().background().filler(40).write(self.main)
        self.assertGreater(self.main.stat().st_size, 2 * limit)
        defects = []
        with patch.object(memspec, "BACKGROUND_CHECK_TAIL_BYTES", limit):
            self.assertIsNone(self.denial(self.call(self.unrelated(), defects)))
        self.assertFalse(any("background check" in line for line in defects), defects)
        self.assertIsNotNone(self.denial(self.call(self.unrelated())))  # in range at the full limit

    def test_a_large_transcript_is_read_only_at_its_tail(self):
        Transcript().filler(400).background().write(self.main)
        self.assertGreater(self.main.stat().st_size, 2 * memspec.BACKGROUND_CHECK_TAIL_BYTES)
        reads = []
        original = Path.open

        def counting_open(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            if Path(path) == self.main:
                read = stream.read

                def counted(size=-1):
                    data = read(size)
                    reads.append(len(data))
                    return data
                stream.read = counted
            return stream

        with patch.object(Path, "open", counting_open):
            reason = self.denial(self.call(self.unrelated()))
        self.assertIn(TASK, reason)
        self.assertTrue(reads)
        self.assertLessEqual(sum(reads), memspec.BACKGROUND_CHECK_TAIL_BYTES)

    def test_reading_starts_at_a_whole_line(self):
        transcript = Transcript().background()
        self.main.parent.mkdir(parents=True)
        body = "".join(json.dumps(row) + "\n" for row in transcript.rows)
        self.main.write_text('{"cut": "' + "x" * 200 + '"}\n' + body, encoding="utf-8")
        rows = pretool._transcript_tail_rows(self.main, len(body.encode("utf-8")) + 50)
        self.assertEqual(rows, transcript.rows)

    def test_an_unreadable_transcript_passes_with_a_defect(self):
        defects = []
        self.assertIsNone(self.denial(self.call(self.unrelated(), defects)))  # no file at all
        self.assertTrue(any(line.startswith("background check UNVERIFIED: review failed (FileNotFoundError")
                            for line in defects), defects)

    def test_a_window_without_a_whole_line_passes_with_a_defect(self):
        Transcript().call("Bash", {"command": "echo"}, result="moved to the background " + "x" * 9000).write(self.main)
        defects = []
        with patch.object(memspec, "BACKGROUND_CHECK_TAIL_BYTES", 4096):
            self.assertIsNone(self.denial(self.call(self.unrelated(), defects)))
        self.assertTrue(any("background check UNVERIFIED: review failed (ValueError: no whole line" in line
                            for line in defects), defects)

    def test_a_parse_failure_passes_with_a_defect(self):
        Transcript().background().write(self.main)
        defects = []
        with patch.object(tool_evidence, "calls", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.denial(self.call(self.unrelated(), defects)))
        self.assertTrue(any("background check UNVERIFIED: review failed (RuntimeError" in line
                            for line in defects), defects)


@unittest.skipUnless(os.environ.get(REAL_TRANSCRIPT_ENV) and os.environ.get(REAL_TASK_ENV),
                     f"set {REAL_TRANSCRIPT_ENV} and {REAL_TASK_ENV} to check a real transcript")
class RealTranscript(unittest.TestCase):
    """Read-only: a real auto-backgrounded call that was later checked, then the same rows
    cut right after its result, which must be denied."""

    def test_checked_in_the_real_transcript_and_denied_once_the_check_is_cut(self):
        source = Path(os.environ[REAL_TRANSCRIPT_ENV])
        task = os.environ[REAL_TASK_ENV]
        rows = []
        with source.open("rb") as stream:
            for line in stream:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        found = [pretool._background_result(call) for call in tool_evidence.calls(rows)]
        self.assertIn(task, [item[0] for item in found if item])
        self.assertIsNone(pretool._background_pending(rows, ""))
        cut = next(index for index, row in enumerate(rows)
                   if row.get("type") == "user" and isinstance(row.get("toolUseResult"), dict)
                   and row["toolUseResult"].get("backgroundTaskId") == task)
        truncated = rows[: cut + 1]
        self.assertEqual(pretool._background_pending(truncated, "")["id"], task)
        with tempfile.TemporaryDirectory(prefix="epitype-background-real-") as scratch:
            copy = Path(scratch).resolve() / "cut.jsonl"
            copy.write_text("".join(json.dumps(row) + "\n" for row in truncated), encoding="utf-8")
            event = {"hook_event_name": "PreToolUse", "session_id": "real-check", "cwd": scratch,
                     "transcript_path": str(copy), "tool_name": "Edit",
                     "tool_input": {"file_path": "a.py", "old_string": "a", "new_string": "b"}}
            value = pretool._background_check_review(event, "Edit", time.monotonic())
        self.assertIn(task, value["hookSpecificOutput"]["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
