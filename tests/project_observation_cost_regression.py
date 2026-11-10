import sys; sys.dont_write_bytecode = True
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import capture_route, memspec


class ProjectObservationCostRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='epitype-observation-cost-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / '.claude/projects/native/memory'
        self.vault.mkdir(parents=True)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.trace = self.vault.parent / 'one.jsonl'
        self.trace.write_text(json.dumps({'cwd': str(self.project)}) + '\n', encoding='utf-8')
        capture_route.project_roots_of(self.vault)

    def test_loading_cached_origins_never_resolves_paths(self):
        cache = self.vault / memspec.FTS_INDEX_DIRECTORY / memspec.PROJECT_ROOTS_CACHE_FILENAME
        with patch.object(Path, 'resolve', side_effect=AssertionError('cache did filesystem lookup')):
            data = capture_route._project_roots_cache(cache, self.vault)
        self.assertEqual(data['roots'], [str(self.project)])
        self.assertEqual(data['files']['one.jsonl'][3], str(self.project))

    def test_one_scan_per_event_and_next_event_observes_new_collision(self):
        with patch.object(capture_route, '_project_roots_of',
                          wraps=capture_route._project_roots_of) as scan:
            with capture_route.observation_window():
                for _ in range(50):
                    self.assertTrue(capture_route.reference_owner_matches(self.vault, self.project))
                with capture_route.observation_window():
                    self.assertEqual(capture_route.project_roots_of(self.vault), [self.project])
                self.assertEqual(scan.call_count, 1)
            (self.vault.parent / 'two.jsonl').write_text(
                json.dumps({'cwd': str(self.root / 'second')}), encoding='utf-8')
            with capture_route.observation_window():
                self.assertFalse(capture_route.reference_owner_matches(self.vault, self.project))
            self.assertEqual(scan.call_count, 2)

    def test_exception_releases_window_and_callers_cannot_mutate_cached_roots(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with capture_route.observation_window():
                roots = capture_route.project_roots_of(self.vault)
                roots.clear()
                self.assertEqual(capture_route.project_roots_of(self.vault), [self.project])
                raise RuntimeError('fixture')
        self.assertIsNone(capture_route._OBSERVATIONS.get())
        (self.vault.parent / 'two.jsonl').write_text(
            json.dumps({'cwd': str(self.root / 'second')}), encoding='utf-8')
        self.assertEqual(len(capture_route.project_roots_of(self.vault)), 2)


if __name__ == '__main__':
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
