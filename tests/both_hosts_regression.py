# -*- coding: utf-8 -*-
"""兩個宿主的紀錄格式，回合閘與斷點檔都要看得見同一件事。

2026-09-20 Codex 審查抓到：回合閘與斷點檔各自寫了一份只認 Claude 的解析。餵 Codex 的
格式進去，提問、內容、派工事實、改過的檔、跑過的指令全是空的——而空的看起來就像
「這一回合沒發生什麼」，不像「我讀不懂這個格式」。

所以這裡每一題都跑兩次：同一件事，兩種格式，結果必須一樣。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "adapters" / "claude"))

import io
import json
import tempfile
import unittest

from epitype import handoff, transcript

import stop_gate


def claude_rows(prompt, answer, tool_name, command):
    return [
        {"type": "user", "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": tool_name,
             "input": {"command": command}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": answer}]}},
    ]


def codex_rows(prompt, answer, tool_name, command):
    return [
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text",
                                                           "text": prompt}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": tool_name,
                                              "arguments": json.dumps({"command": command})}},
        {"type": "response_item", "payload": {"type": "function_call_output",
                                              "output": "ok"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text",
                                                           "text": answer}]}},
    ]


class BothHostsReadTheSame(unittest.TestCase):
    PROMPT = "請詳細說明這一段"
    ANSWER = "好的，這是說明。"
    COMMAND = "python tests/run_all.py"

    def write(self, rows):
        directory = Path(tempfile.mkdtemp(prefix="epitype-hosts-"))
        self.addCleanup(lambda: None)
        path = directory / "t.jsonl"
        with io.open(path, "w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def shapes(self, tool_name="Bash"):
        return {
            "claude": claude_rows(self.PROMPT, self.ANSWER, tool_name, self.COMMAND),
            "codex": codex_rows(self.PROMPT, self.ANSWER, tool_name, self.COMMAND),
        }

    def test_the_turn_gate_sees_prompt_text_and_calls_in_both(self):
        for host, rows in self.shapes().items():
            with self.subTest(host=host):
                turn = stop_gate._turn_context(self.write(rows))
                self.assertEqual(turn.prompt.strip(), self.PROMPT)
                self.assertEqual([piece.strip() for piece in turn.texts], [self.ANSWER])
                self.assertEqual(turn.calls_after_dispatch, 1)

    def test_a_dispatch_is_visible_in_both(self):
        for host, rows in self.shapes(tool_name="Task").items():
            with self.subTest(host=host):
                turn = stop_gate._turn_context(self.write(rows))
                self.assertEqual(turn.dispatch, "Task")
                self.assertEqual(turn.calls_after_dispatch, 0)

    def test_the_length_check_does_not_misread_a_request_for_detail(self):
        # Codex 審查實測到的那一條：使用者要求詳細，程式卻讀不到，反而把長回答擋下來。
        card = decision_for_length()
        for host, rows in self.shapes().items():
            with self.subTest(host=host):
                turn = stop_gate._turn_context(self.write(rows))
                self.assertIsNone(stop_gate._length_gap(card, "話" * 400, turn))

    def test_the_breakpoint_file_records_the_command_in_both(self):
        for host, rows in self.shapes().items():
            with self.subTest(host=host):
                files, commands, prompt = handoff._harvest(rows)
                self.assertEqual(prompt, self.PROMPT)
                self.assertIn(self.COMMAND, commands)

    def test_a_tool_result_row_is_recognised_in_both(self):
        claude = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
        codex = {"type": "response_item", "payload": {"type": "function_call_output",
                                                      "output": "ok"}}
        for host, row in (("claude", claude), ("codex", codex)):
            with self.subTest(host=host):
                kind, _texts, _tools = transcript.turn_parts(row)
                self.assertEqual(kind, transcript.TOOL_RESULT)


def decision_for_length():
    return stop_gate._Decision(
        key="長度", decided_at="2026-09-20", quote="", forbidden=(), aliases=(),
        path=Path("."), decided_by="owner-explicit", require_when="", require_text="",
        advice="", applies_to="", turn_check="length", turn_check_limit="100")


def _selftest():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(BothHostsReadTheSame)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
