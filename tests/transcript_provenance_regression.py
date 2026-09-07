"""Synthetic source identity, line fidelity and bounded recovery regressions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import compact_map
from install import scar_scan


def record(role, text, **fields):
    return {"type": role, "message": {"content": text}, **fields}


def queued(text, origin="human", **fields):
    return {"type": "attachment", "attachment": {
        "type": "queued_command", "prompt": text, "origin": {"kind": origin}}, **fields}


def candidates(rows):
    lines = [(i, json.dumps(row, ensure_ascii=False).encode("utf-8"))
             for i, row in enumerate(rows, 1)]
    users, assistants = compact_map._extract_candidates(lines)
    return users + assistants


class ProvenanceTests(unittest.TestCase):
    def test_human_queued_input_survives_without_promoting_other_origins(self):
        found = candidates([record("user", "Provisionally choose compact."),
                            queued("Compare spacious layouts as well."),
                            queued("AGENT-COMMAND", "agent"),
                            queued("SIDECHAIN", isSidechain=True),
                            queued("UNKNOWN", None)])
        self.assertEqual([(1, "U", "Provisionally choose compact."),
                          (2, "Q", "Compare spacious layouts as well.")], found)

    def test_summaries_meta_and_tool_results_are_not_owner_records(self):
        found = candidates([record("user", "SUMMARY", isCompactSummary=True),
                            record("user", "META", isMeta=True),
                            record("user", [{"type": "tool_result", "content": "OWNER"}]),
                            record("user", "Actual message")])
        self.assertEqual([(4, "U", "Actual message")], found)

    def test_each_assistant_pointer_names_its_own_physical_record(self):
        found = candidates([record("assistant", "Existing is not tested."),
                            {"type": "progress"},
                            record("assistant", "Tests are absent, implementation unknown.")])
        self.assertEqual([(1, "A", "Existing is not tested."),
                          (3, "A", "Tests are absent, implementation unknown.")], found)

    def test_codex_message_records_preserve_roles_without_event_duplicates(self):
        found = candidates([
            {"type": "event_msg", "payload": {"type": "user_message", "message": "request"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "request"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "answer"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "not owner text"}]}},
        ])
        self.assertEqual([(2, "U", "request"), (3, "A", "answer")], found)

    def test_character_clipping_is_explicit(self):
        found = candidates([record("user", "x" * 250 + " Do not deploy."),
                            record("assistant", "y" * 350)])
        self.assertTrue(all("[truncated]" in item[2] for item in found))

    def test_byte_clipping_never_looks_like_a_complete_record(self):
        rendered = compact_map._render_map("header", [(1, "U", "中文" * 90)], 100)
        self.assertLessEqual(len(rendered), 100)
        self.assertIn("[truncated]", rendered.decode("utf-8"))

    def test_malformed_records_do_not_hide_later_valid_records(self):
        rows = [None, [], {"type": "user", "message": []},
                {"type": "attachment", "attachment": []},
                record("user", "still readable")]
        self.assertEqual([(5, "U", "still readable")], candidates(rows))

    def test_map_discloses_scope_and_keeps_queue_source_line(self):
        with tempfile.TemporaryDirectory(prefix="epitype-provenance-") as temp_dir:
            root = Path(temp_dir).resolve()
            source = root / "source.jsonl"
            source.write_text(json.dumps(queued("Compare both variants.")) + "\n", encoding="utf-8")
            mapped = compact_map.build_map(source, root / "map.md", 2048).decode("utf-8")
            self.assertIn("［1｜Q｜Compare both variants.］", mapped)
            self.assertIn("定位", mapped)
            self.assertIn(str(source), mapped)

    def test_scar_proposals_share_source_identity(self):
        with tempfile.TemporaryDirectory(prefix="epitype-scar-source-") as temp_dir:
            source = Path(temp_dir).resolve() / "source.jsonl"
            rows = [record("user", "SUMMARY", isCompactSummary=True),
                    queued("Please preserve the actual correction."),
                    record("assistant", "ASSISTANT"), record("user", "Direct input")]
            source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            self.assertEqual(["Please preserve the actual correction.", "Direct input"],
                             list(scar_scan._records(source)))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
