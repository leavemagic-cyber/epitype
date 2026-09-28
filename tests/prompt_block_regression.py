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

    def turn(self, kind, prompt="給我 PROMPT，貼到新對話"):
        path = self.root / f"{kind}.jsonl"
        if kind == "claude":
            rows = [
                {"type": "user", "message": {"content": prompt}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"path": "source.md"}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "content": "read result"}]}},
            ]
        else:
            rows = [
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": prompt}]}},
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

    def test_owner_typed_text_rejects_machine_wrappers(self):
        # 機器產生、被包成 user 角色的內容不是 owner 親口說的。
        for wrapper in (
            "[Subagent hand-back] 例：write 到 PROMPT.md 時附提示詞。",
            'Another Claude session sent a message:\n<agent-message from="a">\n'
            "[Subagent hand-back] give me a prompt 是子代理的話。",
            "<task-notification>\n<task-id>x</task-id> write a prompt\n</task-notification>",
            "<system-reminder>\ngive me a prompt\n</system-reminder>",
            "Stop hook feedback:\n請給一份 prompt。",
            "<command-name>/goal</command-name> 寫一份 prompt",
            "<environment_context>\n<cwd>C:\\x</cwd> give a prompt\n</environment_context>",
        ):
            self.assertEqual(memspec.owner_typed_text(wrapper), "", wrapper[:40])
        # owner 親打的仍原樣保留。
        self.assertEqual(memspec.owner_typed_text("給我一份 prompt"), "給我一份 prompt")

    def test_subagent_report_is_not_an_owner_prompt_request(self):
        # A2：子代理回報命中「write ... prompt」樣式，owner 當回合沒要提示詞 → 不擋。
        handback = (
            'Another Claude session sent a message:\n<agent-message from="a">\n'
            "[Subagent hand-back] 例：write 到 PROMPT.md 時附提示詞。回報完成。")
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                turn = self.turn(host, prompt=handback)
                self.assertIn("Subagent hand-back", turn.prompt)
                self.assertEqual(memspec.owner_typed_text(turn.prompt), "")
                self.assertIsNone(stop_gate._turn_check_gap(
                    self.decision, "已整合並驗證，結果如下（無 code block）。",
                    turn, frozenset(), []))

    def test_owner_prompt_request_still_blocks_on_both_hosts(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                turn = self.turn(host, prompt="給我一份 prompt 貼到新對話")
                self.assertIsNotNone(stop_gate._turn_check_gap(
                    self.decision, "提示詞在 C:/tmp/prompt.txt。", turn, frozenset(), []))

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
