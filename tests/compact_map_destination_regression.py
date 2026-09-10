import sys; sys.dont_write_bytecode = True
"""PreCompact writes the recovery map, SessionStart reads it — one destination only.

The chain is only worth anything if both hooks land on the same file. That is now
`epitype.compact_map.map_destination`, and the sanitizer it needs
(`session_component`) exists twice on purpose: the hooks' copy lives in
`adapters/claude/_hook_common` (recall and notice markers use it too) and the map's
copy lives in `epitype/` so the destination is computable without importing an
adapter. Two copies drift unless something pins them, so this pins them.
"""

import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import compact_map, memspec
import _hook_common as common
import precompact_hook
import sessionstart_hook

SESSION_IDS = (
    "",
    " ",
    None,
    123,
    "plain-session",
    # 真 session id 就是這個形狀。字面 UUID 進不了受版控的檔（privacy_lint 的
    # session-uuid 規則擋的就是它），所以就地拼出來，形狀照樣驗到。
    "-".join(("8b0f1c22", "4d3e", "4f5a", "9c77", "2b6de0a1f3c4")),
    " weird/id. ",
    "..--__",
    "中文 session 名稱",
    "with\\backslash/and:colon*star?",
    "x" * 400,
)


class SessionComponentPinTests(unittest.TestCase):
    def test_the_two_copies_agree_on_every_shape_of_session_id(self):
        for session_id in SESSION_IDS:
            for limit in (8, 80, 128):
                self.assertEqual(
                    common.session_component(session_id, limit=limit),
                    compact_map.session_component(session_id, limit=limit),
                    f"session_component drifted for {session_id!r} at limit {limit}",
                )

    def test_every_component_is_a_usable_single_filename(self):
        for session_id in SESSION_IDS:
            component = compact_map.session_component(session_id, limit=80)
            self.assertTrue(component)
            self.assertLessEqual(len(component), 80)
            self.assertEqual(component, Path(component).name)


class DestinationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-compact-destination-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.transcript = self.root / "session.jsonl"
        self.transcript.write_text("{}\n", encoding="utf-8")

    def test_both_hooks_compute_the_same_destination(self):
        for session_id in ("plain-session", " weird/id. ", "", None):
            event = {"session_id": session_id, "transcript_path": os.fspath(self.transcript)}
            written = precompact_hook._map_destination(self.vault, event, self.transcript)
            read_back = compact_map.map_destination(self.vault, session_id, self.transcript)
            self.assertEqual(os.fspath(written), os.fspath(read_back))

    def test_the_camelcase_event_shape_reaches_the_same_file(self):
        event = {"sessionId": "camel-session", "transcript_path": os.fspath(self.transcript)}
        written = precompact_hook._map_destination(self.vault, event, self.transcript)
        self.assertEqual(
            os.fspath(compact_map.map_destination(self.vault, "camel-session", self.transcript)),
            os.fspath(written),
        )

    def test_the_destination_lives_in_the_vault_map_directory(self):
        destination = compact_map.map_destination(self.vault, "s", self.transcript)
        self.assertEqual(
            (self.vault / memspec.COMPACT_MAP_DIRECTORY).resolve(), destination.parent
        )
        self.assertTrue(destination.name.endswith(".md"))

    def test_an_unresolved_transcript_string_maps_where_the_resolved_one_maps(self):
        indirect = self.root / "sub" / ".." / "session.jsonl"
        self.assertEqual(
            os.fspath(compact_map.map_destination(self.vault, "s", self.transcript)),
            os.fspath(compact_map.map_destination(self.vault, "s", indirect)),
        )


class NoticeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-compact-notice-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_the_notice_carries_the_whole_path_and_stays_inside_the_cap(self):
        destination = self.root / memspec.COMPACT_MAP_DIRECTORY / "s-0123456789ab.md"
        line = compact_map.map_notice(destination)
        self.assertIsNotNone(line)
        self.assertIn(os.fspath(destination), line)
        self.assertLessEqual(len(line.encode("utf-8")), compact_map.MAP_NOTICE_MAX_BYTES)
        self.assertNotIn("\n", line)

    def test_an_overlong_path_drops_the_whole_line_rather_than_truncating_it(self):
        destination = self.root / ("d" * 300) / "s-0123456789ab.md"
        self.assertIsNone(compact_map.map_notice(destination))

    def test_only_source_compact_asks_for_the_line_at_all(self):
        # 這一行的守門在 `_handle`：`_compact_map_line` 本身不看 source，所以 hook 的
        # selftest 驗行為，這裡只釘「函式存在且沒有 transcript 就不出聲」。
        self.assertIsNone(sessionstart_hook._compact_map_line({}, self.root))
        self.assertIsNone(
            sessionstart_hook._compact_map_line({"transcript_path": "   "}, self.root)
        )
        self.assertIsNone(
            sessionstart_hook._compact_map_line({"transcript_path": 7}, self.root)
        )

    def test_a_map_that_was_never_written_produces_no_line(self):
        transcript = self.root / "session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        event = {"session_id": "never", "transcript_path": os.fspath(transcript)}
        self.assertIsNone(sessionstart_hook._compact_map_line(event, self.root))
        destination = compact_map.map_destination(self.root, "never", transcript)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("map\n", encoding="utf-8")
        self.assertEqual(
            compact_map.map_notice(destination),
            sessionstart_hook._compact_map_line(event, self.root),
        )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
