"""Prompt delivery card: real transcript shapes and both sides of the Stop check."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import memspec
import stop_gate
from _hook_common import run_synthetic, write_config


class PromptBlockRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-prompt-block-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.decision = SimpleNamespace(
            key="prompt-in-chat", turn_check=memspec.TURN_CHECK_PROMPT_BLOCK,
            advice="請給可直接複製的內容。")

    def turn(self, kind):
        path = self.root / f"{kind}.jsonl"
        if kind == "claude":
            rows = [
                {"type": "user", "message": {"content": "給我 PROMPT，貼到新對話"}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"path": "source.md"}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "content": "read result"}]}},
            ]
        else:
            rows = [
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": "給我 PROMPT，貼到新對話"}]}},
                {"type": "response_item", "payload": {
                    "type": "custom_tool_call", "name": "functions.exec", "input": "read source"}},
                {"type": "response_item", "payload": {
                    "type": "custom_tool_call_output", "output": "read result"}},
            ]
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                        encoding="utf-8")
        return stop_gate._turn_context(path)

    def test_both_hosts_require_copyable_block(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                turn = self.turn(host)
                self.assertIn("給我 PROMPT", turn.prompt)
                self.assertIsNotNone(stop_gate._turn_check_gap(
                    self.decision, "提示詞在 C:/tmp/prompt.txt。", turn, frozenset(), []))
                self.assertIsNone(stop_gate._turn_check_gap(
                    self.decision, "請直接複製：\n````text\n你是審查者。\n````",
                    turn, frozenset(), []))
                self.assertIsNotNone(stop_gate._turn_check_gap(
                    self.decision, "```text\n```", turn, frozenset(), []))
                self.assertIsNotNone(stop_gate._turn_check_gap(
                    self.decision, "```text\n  \n```", turn, frozenset(), []))

    def test_unrelated_question_is_allowed(self):
        turn = stop_gate._turn(prompt="請解釋 PROMPT 是什麼")
        self.assertIsNone(stop_gate._turn_check_gap(
            self.decision, "它是給模型的指示文字。", turn, frozenset(), []))

    def test_installed_stop_path_reads_card_and_transcript(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / "prompt.md").write_text(
            "---\nname: prompt-in-chat\ndescription: Prompt delivery\n"
            "turn_check: prompt_code_block\n---\nBody\n", encoding="utf-8")
        config = self.root / "config.json"
        write_config(config, [vault])
        for host in ("claude", "codex"):
            self.turn(host)
            base = {"session_id": f"prompt-{host}-{uuid.uuid4().hex}", "hook_event_name": "Stop",
                    "stop_hook_active": False,
                    "transcript_path": str(self.root / f"{host}.jsonl")}
            for content, blocked in (("檔案在 C:/tmp/prompt.txt。", True),
                                     ("````text\n你是審查者。\n````", False)):
                event = dict(base, last_assistant_message=content)
                result = run_synthetic(
                    Path(stop_gate.__file__), event, config,
                    environment={"HOME": str(self.root), "USERPROFILE": str(self.root)},
                    clock=memspec.HOOK_CLOCK_FROZEN)
                self.assertEqual(result.returncode, 0, result.stderr)
                verdict = json.loads(result.stdout) if result.stdout.strip() else {}
                self.assertEqual(verdict.get("decision") == "block", blocked,
                                 f"{host}: {verdict}")


if __name__ == "__main__":
    unittest.main()
