"""Index shaping rewrites MEMORY.md after the early host sync; the hosts must be refreshed."""
import sys
sys.dont_write_bytecode = True
import inspect
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import dream

EARLY = {"id": 14, "title": "host", "error": None, "seconds": 0.5, "wrote": ["claude: rules"]}


class IndexResyncAfterShaping(unittest.TestCase):
    def test_no_move_keeps_the_early_sync_and_does_not_resync(self):
        calls = []
        for shaping in ([], [{"status": "would-move"}], [{"status": "recorded"}, {"status": "abandoned"}]):
            result = dream._resync_hosts_after_shaping(EARLY, shaping, lambda: calls.append(1) or {})
            self.assertIs(result, EARLY)
        self.assertEqual(calls, [])

    def test_a_moved_index_resyncs_the_hosts(self):
        calls = []

        def resync():
            calls.append(1)
            return {"wrote": ["claude: index", "codex: index"]}

        result = dream._resync_hosts_after_shaping(EARLY, [{"status": "kept"}, {"status": "moved"}], resync)
        self.assertEqual(calls, [1])
        self.assertEqual(result["wrote"], ["claude: index", "codex: index"])
        self.assertIsNone(result["error"])
        self.assertTrue(result["resynced_after_shaping"])
        self.assertEqual((result["id"], result["title"]), (14, "host"))
        self.assertGreaterEqual(result["seconds"], 0.5)

    def test_a_failed_resync_keeps_the_first_pass_and_reports_it(self):
        def resync():
            raise OSError("locked")

        result = dream._resync_hosts_after_shaping(EARLY, [{"status": "moved"}], resync)
        self.assertEqual(result["wrote"], EARLY["wrote"])
        self.assertIn("index resync after shaping", result["error"])
        self.assertIn("locked", result["error"])
        self.assertIsNone(EARLY["error"])

    def test_main_resyncs_after_shaping_and_before_the_report(self):
        source = inspect.getsource(dream._main)
        shaping = source.index("shape_index(vault, today")
        resync = source.index("_resync_hosts_after_shaping(")
        report = source.index("report = build_report(")
        self.assertLess(shaping, resync)
        self.assertLess(resync, report)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
