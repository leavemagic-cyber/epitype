import sys; sys.dont_write_bytecode = True
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import context_meter


class Rotation(unittest.TestCase):
    def test_each_host_early_point_and_one_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for host, limit, current in (('claude', 300000, 180000), ('codex', 210000, 126000)):
                markers = root / host
                markers.mkdir()
                options = {'context_meter': {'handoff_tokens': 250000}}
                reading = SimpleNamespace(host=host, tokens=current - 1)
                self.assertIsNone(context_meter.rotation_notice(reading, limit, root, 's', root/'trace', markers, options))
                reading.tokens = current
                found = context_meter.rotation_notice(reading, limit, root, 's', root/'trace', markers, options)
                self.assertIn('有界檢查點', found[0])
                self.assertIn('接手', found[0])
                self.assertTrue(context_meter.claim(markers, found[1], found[2]))
                self.assertIsNone(context_meter.rotation_notice(reading, limit, root, 's', root/'trace', markers, options))
                self.assertIsNone(context_meter.rotation_notice(reading, limit, root, 's', root/'trace', markers, {}))


if __name__ == '__main__':
    unittest.main()
