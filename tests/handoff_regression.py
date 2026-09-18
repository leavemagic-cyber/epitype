"""斷點檔：進度不能只存在對話裡（owner 2026-09-19）。"""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import tempfile
import unittest

from epitype import handoff


def row(**fields):
    return json.dumps(fields, ensure_ascii=False)


def said(tool_name, payload):
    return row(type="assistant", message={"content": [
        {"type": "tool_use", "name": tool_name, "input": payload}]})


def asked(text):
    return row(type="user", message={"content": text})


class HandoffState(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-handoff-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()

    def transcript(self, lines, name="turn.jsonl"):
        path = self.root / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def update(self, path, session="sess-1", cwd="C:/work"):
        return handoff.update(self.vault, session, path, cwd)

    def read(self, session="sess-1"):
        return (self.vault / ".epitype" / handoff.DIRECTORY / (session + ".md")).read_text(
            encoding="utf-8")

    def test_it_records_what_changed_what_ran_and_the_last_question(self):
        path = self.transcript([
            asked("把這個修好"),
            said("Write", {"file_path": "C:/work/a.py", "content": "x"}),
            said("Bash", {"command": "python -m pytest  -q"}),
        ])
        self.assertIsNotNone(self.update(path))
        text = self.read()
        self.assertIn("C:/work/a.py", text)
        self.assertIn("python -m pytest -q", text)
        self.assertIn("把這個修好", text)
        self.assertIn("C:/work", text)

    def test_reading_a_file_is_not_changing_it(self):
        path = self.transcript([
            said("Read", {"file_path": "C:/work/only-read.py"}),
            said("Write", {"file_path": "C:/work/written.py", "content": "x"}),
        ])
        self.update(path)
        text = self.read()
        self.assertNotIn("only-read.py", text)
        self.assertIn("written.py", text)

    def test_a_later_turn_adds_to_the_same_breakpoint_without_losing_the_earlier_one(self):
        # 斷點是覆寫的，但內容要累積：一場工作的價值在整場，不在最後一個回合。
        first = self.transcript([said("Write", {"file_path": "C:/work/first.py", "content": "x"})],
                                "one.jsonl")
        self.update(first)
        second = self.transcript([said("Write", {"file_path": "C:/work/second.py", "content": "x"})],
                                 "two.jsonl")
        self.update(second)
        text = self.read()
        self.assertIn("first.py", text)
        self.assertIn("second.py", text)

    def test_the_list_stays_bounded(self):
        lines = [said("Write", {"file_path": "C:/work/f%d.py" % index, "content": "x"})
                 for index in range(handoff.MAX_FILES + 10)]
        self.update(self.transcript(lines))
        text = self.read()
        self.assertNotIn("C:/work/f0.py`", text)
        self.assertIn("C:/work/f%d.py" % (handoff.MAX_FILES + 9), text)

    def test_an_unreadable_transcript_still_leaves_a_breakpoint(self):
        # 讀不到紀錄不代表這一場沒有發生；空的斷點也比沒有斷點好。
        self.assertIsNotNone(self.update(self.root / "no-such.jsonl"))
        self.assertIn("斷點", self.read())

    def test_what_a_subagent_said_is_written_down(self):
        # 2026-09-19 實測：子代理的話不在宿主的對話紀錄裡，它自己的工作檔是 0 位元組。
        # 當下擋得住、事後查不到，檢討對子代理整段就是盲的。
        target = handoff.record_subagent(
            self.vault,
            {"hook_event_name": "SubagentStop", "session_id": "parent-1",
             "cwd": "C:/work", "last_assistant_message": "全套測試都過了，所以已經上線。"},
        )
        self.assertIsNotNone(target)
        rows = [json.loads(line) for line in
                Path(target).read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "subagent_say")
        self.assertIn("已經上線", rows[0]["text"])
        self.assertEqual(rows[0]["session_id"], "parent-1")

    def test_the_parent_s_own_words_are_not_filed_as_the_subagent_s(self):
        # 這個時機拿到的紀錄路徑是母場的，子代理不在裡面。2026-09-19 第一版把母場那幾段
        # 存成子代理說的話——重放會把母場的句子算成子代理的行為，等於自己造假資料。
        target = handoff.record_subagent(
            self.vault,
            {"session_id": "parent-1", "last_assistant_message": "子代理的結論"},
            turn_texts=["母場在派工前說的話", "母場的另一段"],
        )
        row = json.loads(Path(target).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["text"], "子代理的結論")
        self.assertIn("parent_turn_text", row)
        self.assertNotIn("turn_text", row)
        self.assertIn("母場", row["parent_turn_text"])

    def test_several_subagents_in_one_day_append_rather_than_overwrite(self):
        for index in range(3):
            handoff.record_subagent(
                self.vault,
                {"session_id": "parent-1", "last_assistant_message": "第 %d 個回報" % index})
        folder = self.vault / ".epitype" / handoff.SUBAGENT_DIRECTORY
        lines = [line for path in folder.glob("*.jsonl")
                 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)

    def test_a_subagent_that_said_nothing_writes_nothing(self):
        self.assertIsNone(handoff.record_subagent(
            self.vault, {"session_id": "parent-1", "last_assistant_message": "   "}))

    def test_a_session_without_an_id_writes_nothing(self):
        self.assertIsNone(handoff.update(self.vault, "", self.root / "no-such.jsonl"))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
