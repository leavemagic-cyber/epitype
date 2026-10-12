import sys; sys.dont_write_bytecode = True
"""RAMGuard limit alerts reach the start of every new Claude and Codex conversation.

RAMGuard (C:\\projects\\ramguard\\ramguard_ai_jobs.py) appends an alert row with kind
ai_session_process_limit or ai_session_memory_limit to alerts.jsonl when an AI session hits
its process or memory limit. The shared SessionStart hook reads only the file's tail, lists
the last 7 days' unacknowledged hits in one line of at most 300 characters and asks the AI
to tell the owner and offer to raise the limit; `python -m epitype ramguard-ack` records the
newest alert as acknowledged. Every failure means no line, never a broken opening.
"""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import cli, memspec, ramguard_notice as notice
import _hook_common as common
import sessionstart_hook as sessionstart

DAY = 24 * 60 * 60
NOW = time.time()


def process_alert(age_days, ai="claude", limit=400, active=400):
    epoch = round(NOW - age_days * DAY, 3)
    return {"event": "alert", "kind": "ai_session_process_limit", "rule": "ai_session_process_limit",
            "ts": "2026-10-12T23:20:00+08:00", "ts_epoch": epoch, "session_kind": ai,
            "root_pid": 100, "limit": limit, "active_processes": active, "action": "log"}


def memory_alert(age_days, ai="codex", limit_mib=8192):
    epoch = round(NOW - age_days * DAY, 3)
    return {"event": "alert", "kind": "ai_session_memory_limit", "rule": "ai_session_memory_limit",
            "ts": "2026-10-12T23:20:00+08:00", "ts_epoch": epoch, "session_kind": ai,
            "root_pid": 200, "job_commit_limit_mib": limit_mib, "job_commit_mib": 7900, "action": "log"}


def other_alert(age_days):
    return {"event": "alert", "kind": "mem_growth", "pid": 1, "name": "x.exe", "ts_epoch": NOW - age_days * DAY}


class Fixture(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-ramguard-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.state = self.root / "dot-epitype"
        self.state.mkdir()
        self.config = self.state / "config.json"
        self.alerts = self.root / "logs" / "alerts.jsonl"
        self.alerts.parent.mkdir()
        self.write_config(str(self.alerts))
        environment = patch.dict(os.environ, {
            memspec.EPITYPE_CONFIG_ENV: str(self.config),
            memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def write_config(self, alerts_path):
        common.write_config(self.config, [self.vault])
        value = json.loads(self.config.read_text(encoding="utf-8"))
        if alerts_path is not None:
            value[memspec.CONFIG_RAMGUARD_ALERTS_FIELD] = alerts_path
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def write_alerts(self, rows, prefix=""):
        self.alerts.write_text(prefix + "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def line(self, now=NOW):
        return notice.startup_notice(common.load_config(time.monotonic()), now=now)

    def ack(self):
        output = io.StringIO()
        code = notice.main([], output=output)
        return code, output.getvalue()


class Parsing(Fixture):

    def test_limit_alerts_are_parsed_and_other_rows_skipped(self):
        lines = [json.dumps(process_alert(1)), "not json ai_session_", json.dumps(other_alert(1)),
                 json.dumps(memory_alert(2)),
                 json.dumps({**process_alert(1), "ts_epoch": None, "ts": "2026-10-12T23:20:00+08:00"})]
        alerts = notice.parse_alerts(lines)
        self.assertEqual([(a["kind"], a["ai"], a["limit"]) for a in alerts],
                         [("ai_session_process_limit", "claude", 400),
                          ("ai_session_memory_limit", "codex", 8192),
                          ("ai_session_process_limit", "claude", 400)])
        # The ISO ts stands in when ts_epoch is absent: 2026-10-12T15:20:00Z.
        self.assertEqual(alerts[2]["epoch"], datetime(2026, 10, 12, 15, 20, tzinfo=timezone.utc).timestamp())

    def test_the_line_names_ai_kind_count_latest_time_and_limit(self):
        self.write_alerts([process_alert(3), process_alert(1), memory_alert(2), other_alert(0)])
        text = self.line()
        self.assertIsNotNone(text)
        self.assertLessEqual(len(text), memspec.RAMGUARD_NOTICE_MAX_CHARS)
        self.assertIn("Claude 程序數 2 次", text)
        self.assertIn("Codex 記憶體 1 次", text)
        self.assertIn("上限 400）", text)
        self.assertIn("上限 8192 MiB", text)
        self.assertIn(notice._stamp(process_alert(1)["ts_epoch"]), text)
        self.assertIn("問要不要調高上限", text)
        self.assertIn("python -m epitype ramguard-ack", text)
        # Newest group first.
        self.assertLess(text.index("Claude"), text.index("Codex"))

    def test_many_groups_still_fit_in_three_hundred_characters(self):
        rows = [process_alert(1, ai=f"agent-kind-{n}-with-a-long-name") for n in range(10)]
        rows += [memory_alert(1, ai=f"other-kind-{n}-with-a-long-name") for n in range(10)]
        self.write_alerts(rows)
        text = self.line()
        self.assertLessEqual(len(text), memspec.RAMGUARD_NOTICE_MAX_CHARS)
        self.assertIn("另有", text)
        self.assertIn("ramguard-ack", text)


class Window(Fixture):

    def test_only_the_last_seven_days_count(self):
        self.write_alerts([process_alert(8), process_alert(7.5)])
        self.assertIsNone(self.line())
        self.write_alerts([process_alert(8), process_alert(6.9)])
        self.assertIn("Claude 程序數 1 次", self.line())

    def test_the_window_moves_with_the_clock(self):
        self.write_alerts([process_alert(1)])
        self.assertIsNotNone(self.line())
        self.assertIsNone(self.line(now=NOW + 7 * DAY))


class Acknowledge(Fixture):

    def test_ack_silences_the_alerts_it_covers_and_not_later_ones(self):
        self.write_alerts([process_alert(2), memory_alert(1)])
        self.assertIsNotNone(self.line())
        code, printed = self.ack()
        self.assertEqual(code, 0, printed)
        self.assertIn("已確認", printed)
        state = json.loads((self.state / memspec.RAMGUARD_ACK_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(state["acked_through_epoch"], memory_alert(1)["ts_epoch"])
        self.assertIsNone(self.line())
        self.write_alerts([process_alert(2), memory_alert(1), process_alert(0.5)])
        text = self.line()
        self.assertIn("Claude 程序數 1 次", text)
        self.assertNotIn("Codex", text)

    def test_ack_through_the_epitype_cli(self):
        self.write_alerts([process_alert(1)])
        with patch("sys.stdout", new=io.StringIO()) as printed:
            self.assertEqual(cli.main(["ramguard-ack"]), 0)
        self.assertIn("已確認", printed.getvalue())
        self.assertIsNone(self.line())

    def test_ack_with_nothing_to_acknowledge_writes_nothing(self):
        self.write_alerts([other_alert(1)])
        code, printed = self.ack()
        self.assertEqual(code, 0)
        self.assertIn("沒有可確認", printed)
        self.assertFalse((self.state / memspec.RAMGUARD_ACK_FILENAME).exists())

    def test_ack_without_the_setting_or_the_file_says_why(self):
        self.write_config(None)
        code, printed = self.ack()
        self.assertEqual(code, 2)
        self.assertIn(memspec.CONFIG_RAMGUARD_ALERTS_FIELD, printed)
        self.write_config(str(self.root / "missing.jsonl"))
        code, printed = self.ack()
        self.assertEqual(code, 2)
        self.assertIn("不存在", printed)
        self.assertFalse((self.state / memspec.RAMGUARD_ACK_FILENAME).exists())


class Absent(Fixture):

    def test_no_setting_means_no_line(self):
        self.write_alerts([process_alert(1)])
        self.write_config(None)
        self.assertIsNone(self.line())
        self.write_config("   ")
        self.assertIsNone(self.line())

    def test_a_missing_file_means_no_line(self):
        self.write_config(str(self.root / "missing.jsonl"))
        self.assertIsNone(self.line())

    def test_a_broken_file_or_ack_state_means_no_error(self):
        self.write_config(str(self.alerts.parent))  # a directory, not a file
        self.assertIsNone(self.line())
        self.write_config(str(self.alerts))
        self.alerts.write_bytes(b"\xff\xfe garbage ai_session_ {\n")
        self.assertIsNone(self.line())
        self.write_alerts([process_alert(1)])
        (self.state / memspec.RAMGUARD_ACK_FILENAME).write_text("{broken", encoding="utf-8")
        self.assertIsNotNone(self.line())  # an unreadable ack is "nothing acknowledged"
        with patch.object(notice, "read_tail", side_effect=PermissionError("locked")):
            self.assertIsNone(self.line())


class Bounded(Fixture):

    def test_a_large_file_is_read_only_at_its_tail(self):
        filler = json.dumps(other_alert(1)) + "\n"
        head = json.dumps(process_alert(1, ai="codex")) + "\n"
        body = filler * (2 * memspec.RAMGUARD_TAIL_BYTES // len(filler))
        tail = json.dumps(process_alert(2, ai="claude")) + "\n"
        self.alerts.write_text(head + body + tail, encoding="utf-8")
        self.assertGreater(self.alerts.stat().st_size, 2 * memspec.RAMGUARD_TAIL_BYTES)
        reads = []
        original = Path.open

        def counting_open(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            if Path(path) == self.alerts:
                read = stream.read

                def counted(size=-1):
                    data = read(size)
                    reads.append(len(data))
                    return data
                stream.read = counted
            return stream

        with patch.object(Path, "open", counting_open):
            text = self.line()
        self.assertLessEqual(sum(reads), memspec.RAMGUARD_TAIL_BYTES)
        self.assertIn("Claude 程序數 1 次", text)
        self.assertNotIn("Codex", text)  # the head row is outside the tail

    def test_a_cut_line_at_the_start_of_the_tail_is_dropped(self):
        self.alerts.write_text("x" * 100 + "\n" + json.dumps(process_alert(1)) + "\n", encoding="utf-8")
        lines = notice.read_tail(self.alerts, max_bytes=self.alerts.stat().st_size - 50)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["kind"], "ai_session_process_limit")


class SessionStart(Fixture):

    def event(self, source="startup", **extra):
        return {"hook_event_name": "SessionStart", "session_id": "ramguard-test", "source": source,
                "cwd": str(self.root), "transcript_path": str(self.root / ".claude" / "projects" / "p" / "s.jsonl"),
                **extra}

    def context(self, event):
        value = sessionstart._handle(event, time.monotonic())
        return (value or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_a_new_claude_or_codex_conversation_opens_with_the_line(self):
        self.write_alerts([process_alert(1)])
        claude = self.context(self.event())
        self.assertTrue(claude.splitlines()[0].startswith("⚠ RAMGuard"), claude)
        codex = self.event(turn_id="t0")
        codex["transcript_path"] = str(self.root / ".codex" / "sessions" / "rollout-x.jsonl")
        self.assertTrue(self.context(codex).splitlines()[0].startswith("⚠ RAMGuard"))

    def test_a_compacted_conversation_is_not_a_new_one(self):
        self.write_alerts([process_alert(1)])
        self.assertNotIn("RAMGuard", self.context(self.event(source="compact")))

    def test_a_failure_never_breaks_the_opening(self):
        self.write_alerts([process_alert(1)])
        with patch.object(notice, "startup_notice", side_effect=RuntimeError("boom")):
            self.assertNotIn("RAMGuard", self.context(self.event()))

    def test_the_hook_entry_point_carries_the_line(self):
        self.write_alerts([process_alert(1)])
        result = common.run_synthetic(Path(sessionstart.__file__), self.event(), self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Claude 程序數 1 次", context)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
