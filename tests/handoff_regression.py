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

    def test_a_session_without_an_id_writes_nothing(self):
        self.assertIsNone(handoff.update(self.vault, "", self.root / "no-such.jsonl"))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
