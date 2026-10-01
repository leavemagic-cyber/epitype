import sys; sys.dont_write_bytecode = True
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import lead_transfer


class Transfer(unittest.TestCase):
    def test_takeover_requires_different_session_and_current_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'handoff.md'
            path.write_text('目前完成到 A；接著 B。', encoding='utf-8')
            state = lead_transfer.prepare(path, 'old')
            self.assertFalse(lead_transfer.ready(path))
            with self.assertRaises(ValueError):
                lead_transfer.acknowledge(path, 'old', state['sha256'])
            with self.assertRaises(ValueError):
                lead_transfer.acknowledge(path, 'new', 'wrong')
            lead_transfer.acknowledge(path, 'new', state['sha256'])
            self.assertTrue(lead_transfer.ready(path))
            path.write_text('更改後的接續點', encoding='utf-8')
            self.assertFalse(lead_transfer.ready(path))
            with self.assertRaises(ValueError):
                lead_transfer.acknowledge(path, 'new', state['sha256'])


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
