"""The compaction handoff states its cap and warns when it would come back as a path only.

2026-11-12: a project handoff over the 2048-byte resume cap came back after compaction as a
bare path; the rotation reminder never named the markers or the cap, and the only size
warning fired above 32 KB. Notices only; the write always proceeds.
"""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
import pretooluse_gate as gate  # noqa: E402
from epitype import compact_map, context_meter, memspec  # noqa: E402

CAP = memspec.COMPACT_RESUME_MAX_BYTES


def handoff(body):
    return f"# Handoff\n{compact_map.RESUME_BEGIN}\n{body}\n{compact_map.RESUME_END}\n\nhistory...\n"


class HandoffCapRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-handoff-cap-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve() / memspec.COMPACT_MAP_DIRECTORY
        self.folder.mkdir()
        self.path = self.folder / ("sess-abc123" + memspec.COMPACT_HANDOFF_SUFFIX)

    def test_resume_problem_matches_what_compaction_brings_back(self):
        cases = {
            "fits": handoff("下一步：合併同類卡"),
            "no markers": "# Handoff\n下一步：合併同類卡\n",
            "over cap": handoff("狀" * (CAP // 3)),
            "empty": handoff(""),
            "late": "x" * (CAP * 2) + handoff("下一步"),
        }
        for name, text in cases.items():
            self.path.write_text(text, encoding="utf-8")
            comes_back = compact_map.handoff_resume(self.path) is not None
            problem = compact_map.resume_problem(text)
            self.assertEqual(comes_back, problem is None, (name, problem))
        self.assertIn(str(CAP), compact_map.resume_problem(cases["over cap"]))

    def test_every_reminder_names_the_markers_and_the_cap(self):
        notices = [memspec.CONTEXT_METER_NOTICE, memspec.CONTEXT_METER_CODEX_NOTICE]
        reading = mock.Mock(tokens=150000)
        with mock.patch.object(context_meter, "_handoff", return_value=self.path), \
             mock.patch.object(context_meter, "_rearm_if_stale"):
            rotated = context_meter.rotation_notice(
                reading, 200000, self.folder.parent, "sess", "t.jsonl", self.folder,
                {"context_meter": {"handoff_tokens": 100000}})
        self.assertIsNotNone(rotated)
        notices.append(rotated[0])
        for text in notices:
            self.assertIn(compact_map.RESUME_BEGIN, text)
            self.assertIn(compact_map.RESUME_END, text)
            self.assertIn(str(CAP), text)

    def test_reminder_reports_a_handoff_that_would_come_back_as_a_path(self):
        self.path.write_text("# Handoff without markers\n", encoding="utf-8")
        self.assertIn(compact_map.RESUME_BEGIN, context_meter._handoff_size_notice(self.path))
        self.path.write_text(handoff("下一步"), encoding="utf-8")
        self.assertEqual(context_meter._handoff_size_notice(self.path), "")

    def test_write_gate_warns_on_the_handoff_and_nothing_else(self):
        over = handoff("狀" * (CAP // 3))
        note = gate._handoff_review(self.path, over)
        self.assertTrue(note and note.startswith(memspec.COMPACT_RESUME_WRITE_LABEL), note)
        self.assertIsNone(gate._handoff_review(self.path, handoff("下一步")))
        self.assertIsNone(gate._handoff_review(self.folder / "notes.md", over))
        self.assertIsNone(gate._handoff_review(self.path, None))


if __name__ == "__main__":
    unittest.main(argv=[argument for argument in sys.argv if argument != "--selftest"])
