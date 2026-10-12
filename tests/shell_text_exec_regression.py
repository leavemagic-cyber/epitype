import sys; sys.dont_write_bytecode = True
r"""Bash running text as a command: the PreToolUse gate denies it for Claude's Bash tool.

2026-10-12: Markdown backticks inside a double-quoted `python -c "..."` were command-
substituted; Git Bash ran the non-executable .md as a script and the backticks inside
it ran further files (~1,712 bash.exe, 44.6 GiB committed, three hours frozen). These
tests pin the three rules in epitype/shell_text_exec.py, the places Bash does NOT
substitute (single quotes, $'...', comments, quoted here-documents, \`), the Codex
exclusion (PowerShell, where a backtick is the escape character), and the fail-open
limits.

The incident's own command is read from its transcript by JSON parsing only when
EPITYPE_INCIDENT_TRANSCRIPT names that file (the session id stays out of the repo);
otherwise an equivalent synthetic command stands in. Nothing here executes a command.
"""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude"), str(ROOT / "tests")]
from epitype import memspec, shell_text_exec
from epitype.shell_text_exec import ScanLimitExceeded, find_text_exec
import _hook_common as common
import pretooluse_gate as pretool

INCIDENT_TRANSCRIPT_ENV = "EPITYPE_INCIDENT_TRANSCRIPT"
INCIDENT_LINE = 109841
INCIDENT_TOOL_USE_ID = "toolu_01YNK99YvQzD8bdczf1uYzpS"
# Same shape as the incident: Chinese prose with a backticked .md path, inside a
# double-quoted python -c, followed by an add and a commit.
SYNTHETIC_INCIDENT = (
    'cd C:/dev/example-repo && python -c "\n'
    "p='docs/RUNBOOK.md'; s=open(p,encoding='utf-8',newline='').read()\n"
    "b='**10-12 owner：下一個目標是準備上架**：順序與交接見 "
    "`C:/dev/example-audits/NEXT_LEAD_PROMPT.md` 檔首第 3 節；實機回報最優先。'\n"
    "open(p,'w',encoding='utf-8',newline='').write(s+b)\" && git add docs/RUNBOOK.md && "
    "git commit -q -m \"Record the owner's next goal\""
)


def incident_command():
    """The incident's command from its transcript, or None when it cannot be read."""
    path = os.environ.get(INCIDENT_TRANSCRIPT_ENV)
    if not path or not Path(path).is_file():
        return None
    with open(path, "rb") as stream:
        for number, raw in enumerate(stream, 1):
            if number == INCIDENT_LINE:
                row = json.loads(raw)
                break
        else:
            return None
    for block in row.get("message", {}).get("content", []):
        if isinstance(block, dict) and block.get("id") == INCIDENT_TOOL_USE_ID:
            return block["input"]["command"]
    return None


def rules(command):
    return [finding.rule for finding in find_text_exec(command)]


class Scanner(unittest.TestCase):
    """find_text_exec on its own: D = some finding, A = none."""

    def assertDenied(self, command, rule):
        self.assertIn(rule, rules(command), command)

    def assertAllowed(self, command):
        self.assertEqual(find_text_exec(command), [], command)

    def test_the_incident_is_caught_by_the_backtick_rule(self):
        commands = [SYNTHETIC_INCIDENT]
        real = incident_command()
        if real is not None:
            commands.append(real)
        for command in commands:
            with self.subTest(real=command is real):
                found = find_text_exec(command)
                self.assertEqual(found[0].rule, "R1")
                self.assertIn("NEXT_LEAD_PROMPT.md", found[0].snippet)
                # The backticked word is itself a document: R2 on the same spot.
                self.assertIn("R2", [finding.rule for finding in found])
                self.assertAllowed(command.replace("`", "\\`"))

    def test_the_real_incident_line_is_read_when_named(self):
        if not os.environ.get(INCIDENT_TRANSCRIPT_ENV):
            self.skipTest(f"{INCIDENT_TRANSCRIPT_ENV} not set; the synthetic command stands in")
        command = incident_command()
        self.assertIsNotNone(command, "the named transcript does not hold the incident line")
        self.assertIn("NEXT_LEAD_PROMPT.md", command)

    def test_commit_messages(self):
        self.assertDenied('git commit -m "Fix `foo` bug"', "R1")
        self.assertAllowed("git commit -m 'Fix `foo` bug'")
        self.assertAllowed('git commit -m "Fix \\`foo\\` bug"')
        # A single quote inside double quotes is a literal character.
        self.assertDenied('echo "it\'s `x`"', "R1")

    def test_here_documents(self):
        self.assertAllowed("cat <<'EOF'\n`x`\nEOF")
        self.assertDenied("cat <<EOF\n`x`\nEOF", "R1")
        self.assertAllowed('cat <<-"EOF"\n`x`\n\tEOF')
        self.assertAllowed("cat <<\\EOF\n`x`\nEOF")
        self.assertAllowed('cat <<E"O"F\n`x`\nEOF')
        # The usual commit form: a quoted here-document inside "$( )".
        self.assertAllowed("git commit -m \"$(cat <<'EOF'\nFix `foo`\n\nbody\nEOF\n)\"")
        self.assertDenied("git commit -m \"$(cat <<EOF\nFix `foo`\nEOF\n)\"", "R1")
        # Two here-documents on one line are read in order, each by its own quoting.
        self.assertAllowed("cat <<A <<'B'\nplain\nA\n`x`\nB\necho done")
        self.assertDenied("cat <<'A' <<B\n`y`\nA\n`x`\nB\necho done", "R1")
        # After the body ends, the next line is a command again.
        self.assertDenied("cat <<'EOF'\nplain\nEOF\necho \"`x`\"", "R1")
        # <<- strips leading tabs from the delimiter line.
        self.assertAllowed("cat <<-'EOF'\n\t`x`\n\tEOF\necho ok")
        # A here-string word follows the ordinary quoting rules.
        self.assertAllowed("cat <<< 'a `b`'")
        self.assertDenied('cat <<< "a `b`"', "R1")

    def test_crlf_commands(self):
        self.assertAllowed("cat <<'EOF'\r\n`x`\r\nEOF\r\necho ok\r\n")
        self.assertDenied("cat <<EOF\r\n`x`\r\nEOF\r\n", "R1")
        self.assertDenied("echo ok\r\ndocs/x.md\r\n", "R3")

    def test_real_command_substitution_is_allowed(self):
        self.assertAllowed("x=$(git rev-parse HEAD)")
        self.assertAllowed('"$(git log -1)"')
        self.assertAllowed("echo $((1 << 2)); echo ${HOME:-/tmp}")

    def test_backslashes(self):
        # \\ is a literal backslash, so the backtick after it still substitutes.
        self.assertDenied('echo "a\\\\`b`"', "R1")
        self.assertDenied("echo a\\\\`b`", "R1")
        self.assertAllowed("echo a\\`b\\`")

    def test_comments(self):
        self.assertAllowed("# comment with `x`")
        self.assertAllowed("echo hi # `x`")
        self.assertAllowed("echo hi;# `x`\necho ok")
        self.assertDenied("echo a#`x`", "R1")
        self.assertDenied("echo hi # `x`\necho \"`y`\"", "R1")

    def test_single_quote_forms(self):
        self.assertAllowed("$'a `b`'")
        self.assertAllowed("echo 'it''s `x`'")
        # Inside double quotes $' is not ANSI-C quoting.
        self.assertDenied('echo "$\'a `b`\'"', "R1")
        # Single quotes inside a double-quoted ${ } are literal; unquoted they quote.
        self.assertDenied("echo \"${x:-'`y`'}\"", "R1")
        self.assertAllowed("echo ${x:-'`y`'}")

    def test_nesting(self):
        self.assertDenied('"$(echo "`x`")"', "R1")
        self.assertAllowed("\"$(echo '`x`')\"")
        self.assertDenied('x=$(echo "$(printf %s "`y`")")', "R1")
        self.assertDenied("echo $(( `n` + 1 ))", "R1")
        self.assertDenied("echo ${x:-`y`}", "R1")

    def test_document_as_the_first_word_of_a_substitution(self):
        self.assertDenied("$(C:/dev/x.md)", "R2")
        self.assertDenied("echo \"$( docs/Notes.MD )\"", "R2")
        self.assertDenied("echo `README.md`", "R2")
        self.assertAllowed("$(cat x.md)")
        self.assertAllowed("$(< x.md)")
        # $(( )) is arithmetic: its first word is not a command.
        self.assertAllowed("echo $(( 1 + 2 ))")

    def test_document_as_the_command_word(self):
        self.assertDenied("docs/x.md", "R3")
        self.assertDenied("bash docs/x.md", "R3")
        self.assertDenied("source ./x.md", "R3")
        self.assertDenied(". x.md", "R3")
        self.assertDenied("C:/dev/x.md", "R3")
        self.assertDenied("cd x && ./README.md", "R3")
        self.assertDenied("FOO=1 docs/config.yaml", "R3")
        self.assertDenied("bash -x --norc -- docs/x.md", "R3")
        self.assertDenied("cd x && \\\n docs/x.md", "R3")
        self.assertDenied("[[ -f a.md ]] && a.md", "R3")
        self.assertDenied("function f { docs/x.md; }", "R3")
        self.assertDenied("time docs/x.md", "R3")
        self.assertDenied("(docs/x.md)", "R3")
        self.assertDenied("if (cd x && docs/x.md); then echo; fi", "R3")

    def test_a_paren_in_the_middle_of_a_command_opens_no_subshell(self):
        # Bash rejects these lines as syntax errors, so nothing in them runs as a command
        # (host_layer_regression sends Python statements like this one as Bash commands).
        self.assertAllowed("data=json.loads(Path('C:/x/settings.json').read_text()); print(data)")
        self.assertAllowed("echo foo(docs/x.md)")
        self.assertAllowed("x=1 (docs/x.md)")
        # A backtick in one is still named: it is text either way and costs nothing to fix.
        self.assertDenied('print(f"`x`")', "R1")

    def test_reading_documents_is_allowed(self):
        for command in (
            "cat docs/x.md", "python x.py", "./scripts/ci/gate.sh", "sed -n 1p x.md",
            "bash -c 'docs/x.md'", "bash -lc 'echo hi'", "bash scripts/run.sh",
            "> out.md echo hi", "cat <(sort a.txt) > out.md 2>&1", "cat < in.md",
            "for f in a.md b.md; do cat \"$f\"; done",
            "case $f in *.md) echo md;; *.txt|*.json) echo t;; esac",
            "x=$(case $f in a.md) echo y;; esac); echo $x",
            "arr=(a.md b.md); echo ${arr[0]}", "[[ -f a.md ]] && cat a.md",
            "f() { echo hi; }; f", "git log -- docs/x.md", "find . -name '*.md' -exec cat {} \\;",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_powershell_escapes_inside_bash_quotes(self):
        self.assertDenied('powershell -Command "a`nb"', "R1")
        self.assertAllowed("powershell -Command 'a`nb'")

    def test_snippet_is_short(self):
        found = find_text_exec('echo "`' + "x" * 500 + '`"')
        self.assertLessEqual(len(found[0].snippet), memspec.SHELL_TEXT_EXEC_SNIPPET_MAX_CHARS)

    def test_limits_raise_instead_of_guessing(self):
        oversized = "echo " + "a" * (memspec.WRITE_GATE_MAX_CONTENT_BYTES + 10)
        with self.assertRaises(ScanLimitExceeded):
            find_text_exec(oversized)
        with self.assertRaises(ScanLimitExceeded):
            find_text_exec("echo " + "$(" * 200 + "x" + ")" * 200)
        with patch.object(memspec, "SHELL_TEXT_EXEC_STEP_BUDGET", 50):
            with self.assertRaises(ScanLimitExceeded):
                find_text_exec("echo " + "a" * 200)
        # A command just under the size limit is scanned, not refused.
        near = "echo " + "a" * (memspec.WRITE_GATE_MAX_CONTENT_BYTES - 100) + " `x`"
        self.assertEqual(rules(near), ["R1"])

    def test_the_reason_fits_with_the_longest_snippet(self):
        snippet = "x" * memspec.SHELL_TEXT_EXEC_SNIPPET_MAX_CHARS
        for rule, why in memspec.SHELL_TEXT_EXEC_WHY.items():
            reason = memspec.SHELL_TEXT_EXEC_REASON.format(snippet=pretool._quoted(snippet), why=why)
            self.assertLessEqual(len(reason), 400, rule)


class Gate(unittest.TestCase):
    """The same check through the PreToolUse gate."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-shell-text-")
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

    def event(self, command, tool_name="Bash", **extra):
        return {"hook_event_name": "PreToolUse", "session_id": "shell-text-test",
                "transcript_path": str(self.root / ".claude" / "projects" / "p" / "s.jsonl"),
                "cwd": str(self.root), "tool_name": tool_name,
                "tool_input": {"command": command}, **extra}

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
        return [row for row in rows if row.get("rule") == memspec.SHELL_TEXT_EXEC_RULE]

    def test_the_incident_is_denied_with_a_plain_reason_and_an_audit_row(self):
        reason = self.denial(self.call(self.event(SYNTHETIC_INCIDENT)))
        self.assertIsNotNone(reason)
        self.assertLessEqual(len(reason), 400)
        self.assertIn("NEXT_LEAD_PROMPT.md", reason)
        self.assertIn(memspec.SHELL_TEXT_EXEC_WHY["R1"], reason)
        self.assertIn("$(...)", reason)
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["tool"], rows[0]["finding"], rows[0]["session_id"]),
                         (memspec.ACTION_GUARD_LOG_KIND, "Bash", "R1", "shell-text-test"))

    def test_the_escaped_form_passes(self):
        self.assertIsNone(self.denial(self.call(self.event(SYNTHETIC_INCIDENT.replace("`", "\\`")))))
        self.assertEqual(self.audit_rows(), [])

    def test_codex_events_are_never_checked(self):
        # Codex runs PowerShell on Windows, where ` is the escape character.
        self.assertIsNone(self.denial(self.call(self.event('echo "a`nb"', turn_id="t1"))))
        codex = self.event('echo "a`nb"')
        codex["transcript_path"] = str(self.root / ".codex" / "sessions" / "rollout-x.jsonl")
        self.assertIsNone(self.denial(self.call(codex)))

    def test_other_tools_are_never_checked(self):
        self.assertIsNone(self.denial(self.call(self.event('Write-Output "a`nb"', tool_name="PowerShell"))))

    def test_an_oversized_command_passes_with_a_defect(self):
        defects = []
        command = "echo " + "a" * (300 * 1024) + ' "`x`"'
        self.assertIsNone(self.denial(self.call(self.event(command), defects)))
        self.assertTrue(any(line.startswith("shell text check UNVERIFIED:") for line in defects), defects)

    def test_a_scanner_failure_passes_with_a_defect(self):
        defects = []
        with patch.object(shell_text_exec, "find_text_exec", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.denial(self.call(self.event('echo "`x`"'), defects)))
        self.assertTrue(any("shell text check UNVERIFIED: review failed (RuntimeError)" in line
                            for line in defects), defects)

    def test_it_holds_without_a_readable_configuration(self):
        self.config.write_text("{broken", encoding="utf-8")
        self.assertIsNotNone(self.denial(self.call(self.event('git commit -m "Fix `foo` bug"'))))
        self.assertEqual(self.audit_rows(), [])

    def test_the_hook_entry_point_emits_the_deny_shape(self):
        result = common.run_synthetic(Path(pretool.__file__), self.event(SYNTHETIC_INCIDENT), self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(set(value), {"hookSpecificOutput"})
        output = value["hookSpecificOutput"]
        self.assertEqual(set(output), {"hookEventName", "permissionDecision", "permissionDecisionReason"})
        self.assertEqual((output["hookEventName"], output["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn("NEXT_LEAD_PROMPT.md", output["permissionDecisionReason"])

    def test_the_hook_entry_point_lets_a_real_substitution_through(self):
        result = common.run_synthetic(
            Path(pretool.__file__), self.event("x=$(git rev-parse HEAD) && echo \"$x\""), self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        if result.stdout.strip():
            output = json.loads(result.stdout).get("hookSpecificOutput", {})
            self.assertNotEqual(output.get("permissionDecision"), "deny")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
