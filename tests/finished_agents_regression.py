"""finished_agents_closed：Codex 主線結束前，已完成的直屬子代理必須已用 close_agent 關閉。

測試資料照官方 rust-v0.156.0 的輸出格式（見 epitype/codex_agents.py 的出處），
不是照紀錄猜的；V2 樣本只用來確認不判。
"""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import codex_agents, memspec
import stop_gate
from _hook_common import run_synthetic, write_config


def item(payload):
    return {"timestamp": "2026-09-29T00:00:00.000Z", "type": "response_item", "payload": payload}


def meta():
    return {"timestamp": "2026-09-29T00:00:00.000Z", "type": "session_meta",
            "payload": {"id": "root-session"}}


def call(call_id, name, arguments):
    return item({"type": "function_call", "name": name, "namespace": "multi_agent_v1",
                 "arguments": json.dumps(arguments), "call_id": call_id})


def output(call_id, value):
    text = value if isinstance(value, str) else json.dumps(value)
    return item({"type": "function_call_output", "call_id": call_id, "output": text})


def spawn(call_id, agent_id, nickname):
    return [call(call_id, "spawn_agent", {"message": "task"}),
            output(call_id, {"agent_id": agent_id, "nickname": nickname})]


def wait(call_id, statuses, timed_out=False):
    return [call(call_id, "wait_agent", {"targets": list(statuses), "timeout_ms": 30000}),
            output(call_id, {"status": statuses, "timed_out": timed_out})]


def close(call_id, agent_id, result=None):
    rows = [call(call_id, "close_agent", {"target": agent_id})]
    if result is not False:
        rows.append(output(call_id, result or {"previous_status": {"completed": "done"}}))
    return rows


class FinishedAgentsClosed(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-agents-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.decision = SimpleNamespace(
            key="close-finished-agents", turn_check="finished_agents_closed",
            advice="")

    def rollout(self, *groups, name=None):
        rows = [meta()]
        for group in groups:
            rows.extend(group if isinstance(group, list) else [group])
        path = self.root / (name or f"rollout-{uuid.uuid4().hex}.jsonl")
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                        encoding="utf-8")
        return path

    def gap(self, path, **extra):
        event = {"hook_event_name": "Stop", "session_id": "s", "transcript_path": str(path)}
        event.update(extra)
        return stop_gate._turn_check_gap(self.decision, "完成。", stop_gate._turn(),
                                         frozenset(), [], event=event, started_at=None)

    def test_name_is_a_registered_turn_check(self):
        self.assertEqual(memspec.TURN_CHECK_FINISHED_AGENTS, "finished_agents_closed")
        self.assertIn(memspec.TURN_CHECK_FINISHED_AGENTS, memspec.TURN_CHECK_NAMES)

    def test_finished_and_closed_passes(self):
        path = self.rollout(spawn("1", "agent-a", "Ada"),
                            wait("2", {"agent-a": {"completed": "report"}}),
                            close("3", "agent-a"))
        self.assertEqual(codex_agents.unclosed_finished(path), [])
        self.assertIsNone(self.gap(path))

    def test_finished_not_closed_blocks_with_names(self):
        path = self.rollout(spawn("1", "agent-a", "Ada"), spawn("2", "agent-b", None),
                            wait("3", {"agent-a": {"completed": "report"},
                                       "agent-b": {"completed": None}}),
                            close("4", "agent-b"))
        self.assertEqual(codex_agents.unclosed_finished(path), [("agent-a", "Ada")])
        reason = self.gap(path)
        self.assertIn("agent-a", reason)
        self.assertIn("Ada", reason)
        self.assertIn("close_agent", reason)
        self.assertNotIn("agent-b", reason)

    def test_errored_not_closed_blocks(self):
        path = self.rollout(spawn("1", "agent-e", None),
                            wait("2", {"agent-e": {"errored": "403 Forbidden"}}))
        self.assertIn("agent-e", self.gap(path))

    def test_failed_close_does_not_count(self):
        path = self.rollout(spawn("1", "agent-a", None),
                            wait("2", {"agent-a": {"completed": "x"}}),
                            close("3", "agent-a", result="agent with id agent-a not found"))
        self.assertIn("agent-a", self.gap(path))

    def test_running_or_interrupted_never_blocks(self):
        path = self.rollout(spawn("1", "agent-r", None), spawn("2", "agent-i", None),
                            wait("3", {}, timed_out=True),
                            wait("4", {"agent-i": "interrupted", "agent-r": "running"}))
        self.assertIsNone(self.gap(path))
        # 給完成的代理新工作，它就又在跑；直到下一次回報完成前都不算。
        again = self.rollout(spawn("1", "agent-a", None),
                             wait("2", {"agent-a": {"completed": "x"}}),
                             call("3", "send_input", {"target": "agent-a", "message": "more"}))
        self.assertIsNone(self.gap(again))

    def test_subagent_stop_passes(self):
        path = self.rollout(spawn("1", "agent-a", None),
                            wait("2", {"agent-a": {"completed": "x"}}))
        self.assertIsNotNone(self.gap(path))
        self.assertIsNone(self.gap(path, hook_event_name=memspec.SUBAGENT_STOP_EVENT))
        self.assertIsNone(self.gap(path, agent_id="agent-7"))

    def test_claude_transcript_passes(self):
        path = self.root / "claude.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in (
            {"type": "user", "message": {"content": "做完再關"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Agent", "input": {"prompt": "x"}}]}},
        )) + "\n", encoding="utf-8")
        self.assertEqual(codex_agents.unclosed_finished(path), [])
        self.assertIsNone(self.gap(path))

    def test_v2_session_is_not_judged(self):
        # 官方 rust-v0.156.0 的 V2 工具組沒有 close_agent：要求它關也做不到，所以不判。
        path = self.rollout(
            call("1", "spawn_agent", {"task_name": "worker", "message": "x"}),
            output("1", {"task_name": "/root/worker"}),
            item({"type": "agent_message", "author": "/root/worker", "recipient": "/root",
                  "content": [{"type": "input_text", "text": "Message Type: FINAL_ANSWER\n"}]}),
            call("2", "list_agents", {}),
            output("2", {"agents": [{"agent_name": "/root/worker",
                                     "agent_status": {"completed": "done"}}]}))
        self.assertIsNone(self.gap(path))

    def test_agents_before_a_resume_are_not_judged(self):
        path = self.rollout(spawn("1", "agent-a", None),
                            wait("2", {"agent-a": {"completed": "x"}}), meta())
        self.assertIsNone(self.gap(path))

    def test_deadline_and_errors_fail_open(self):
        path = self.rollout(spawn("1", "agent-a", None),
                            wait("2", {"agent-a": {"completed": "x"}}))
        self.assertIsNone(codex_agents.unclosed_finished(path, lambda: True))
        self.assertIsNone(self.gap(self.root / "missing.jsonl"))
        broken = self.root / "broken.jsonl"
        broken.write_text(json.dumps(meta()) + "\n"
                          + '{"type":"response_item","payload":{"type":"function_call",'
                            '"name":"spawn_agent", broken\n', encoding="utf-8")
        self.assertIsNone(self.gap(broken))

    def test_installed_stop_path_blocks_once_card_is_armed(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / "close-agents.md").write_text(
            "---\nname: close-finished-agents\ndescription: Close finished subagents\n"
            "turn_check: finished_agents_closed\n---\nBody\n", encoding="utf-8")
        config = self.root / "config.json"
        write_config(config, [vault])
        cases = (
            (self.rollout(spawn("1", "agent-a", "Ada"),
                          wait("2", {"agent-a": {"completed": "x"}})), True),
            (self.rollout(spawn("1", "agent-a", "Ada"),
                          wait("2", {"agent-a": {"completed": "x"}}),
                          close("3", "agent-a")), False),
        )
        for path, blocked in cases:
            event = {"session_id": f"agents-{uuid.uuid4().hex}", "hook_event_name": "Stop",
                     "stop_hook_active": False, "last_assistant_message": "完成。",
                     "transcript_path": str(path)}
            result = run_synthetic(
                Path(stop_gate.__file__), event, config,
                environment={"HOME": str(self.root), "USERPROFILE": str(self.root)},
                clock=memspec.HOOK_CLOCK_FROZEN)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout) if result.stdout.strip() else {}
            self.assertEqual(value.get("decision") == "block", blocked, result.stdout)
            if blocked:
                self.assertIn("agent-a", value["reason"])


if __name__ == "__main__":
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
