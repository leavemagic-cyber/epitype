"""A byte-order mark on hook input must not silently disable every hook.

2026-09-16: Cursor loads this repo's hooks through its Claude-config
compatibility layer and writes a UTF-8 BOM ahead of the JSON payload.
`json.load` raised on it, each adapter's top-level `except Exception: pass`
swallowed the raise, and the hook then ran, exited 0 and emitted nothing.
Measured in one real Cursor session: 46 Epitype hook invocations, every one
silent. The failure is invisible from the outside -- exit 0, no stderr -- so it
needs a test that pins the parse, not an integration check that would also pass
while the hooks do nothing.
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import io
import json
import unittest

from adapters.claude import _hook_common


BOM = "﻿"
EVENT = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}


class HookInputBomTests(unittest.TestCase):
    def test_text_stream_without_bom_still_parses(self):
        stream = io.StringIO(json.dumps(EVENT))
        self.assertEqual(_hook_common.read_event(stream), EVENT)

    def test_text_stream_with_bom_parses(self):
        stream = io.StringIO(BOM + json.dumps(EVENT))
        self.assertEqual(_hook_common.read_event(stream), EVENT)

    def test_byte_stream_with_bom_parses(self):
        stream = io.BytesIO(BOM.encode("utf-8") + json.dumps(EVENT).encode("utf-8"))
        self.assertEqual(_hook_common.read_event(stream), EVENT)

    def test_non_ascii_payload_survives_the_bom_strip(self):
        event = dict(EVENT, prompt="規則卡")
        stream = io.StringIO(BOM + json.dumps(event, ensure_ascii=False))
        self.assertEqual(_hook_common.read_event(stream)["prompt"], "規則卡")

    def test_non_object_json_still_rejected(self):
        # The BOM strip must not turn a malformed payload into a silent pass.
        for raw in ("[1, 2]", BOM + "[1, 2]"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    _hook_common.read_event(io.StringIO(raw))


class WorkspaceRootsTests(unittest.TestCase):
    """A host that names the working directory `workspace_roots` must still
    resolve a vault. Cursor sends that key, URL-style, and never sends `cwd`;
    every vault lookup reads `cwd`, so the hooks otherwise run against no vault
    and emit nothing, which looks exactly like a broken hook."""

    def _read(self, event):
        return _hook_common.read_event(io.StringIO(json.dumps(event, ensure_ascii=False)))

    def test_url_style_windows_root_becomes_cwd(self):
        event = self._read(dict(EVENT, workspace_roots=["/C:/Epitype/repo"]))
        self.assertEqual(event["cwd"], "C:/Epitype/repo")

    def test_posix_root_is_passed_through(self):
        event = self._read(dict(EVENT, workspace_roots=["/home/u/project"]))
        self.assertEqual(event["cwd"], "/home/u/project")

    def test_existing_cwd_is_never_overwritten(self):
        event = self._read(dict(EVENT, cwd="D:/real", workspace_roots=["/C:/other"]))
        self.assertEqual(event["cwd"], "D:/real")

    def test_empty_or_missing_roots_leave_cwd_absent(self):
        for roots in ([], [""], None, "not-a-list"):
            with self.subTest(roots=roots):
                payload = dict(EVENT)
                if roots is not None:
                    payload["workspace_roots"] = roots
                self.assertNotIn("cwd", self._read(payload))

    def test_first_usable_root_wins(self):
        event = self._read(dict(EVENT, workspace_roots=["", "/C:/second"]))
        self.assertEqual(event["cwd"], "C:/second")


def _selftest():
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(HookInputBomTests),
        loader.loadTestsFromTestCase(WorkspaceRootsTests),
    ])
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    status = "PASS" if failed == 0 else "FAIL"
    print("SELFTEST %s %d/%d" % (status, total - failed, total))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
