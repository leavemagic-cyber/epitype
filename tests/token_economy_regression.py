import sys; sys.dont_write_bytecode = True
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import token_economy, memspec


class Replay(unittest.TestCase):
    def test_compaction_env_is_supported_and_has_bounded_percentage(self):
        front = ["host_claude_env:", "  CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: '30'"]
        values, problems = memspec.host_native_values(front)
        self.assertEqual(problems, [])
        self.assertEqual(values[memspec.HOST_CLAUDE_ENV_FIELD]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "30")
        for value in ("0", "101"):
            _, invalid = memspec.host_native_values([front[0], "  CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: '" + value + "'"])
            self.assertTrue(invalid)

    def test_cost_includes_extra_summary_and_preserves_growth(self):
        trace = [{"context": c, "output": 1} for c in (10, 20, 30, 40)]
        self.assertEqual(token_economy.replay_compaction(trace, 30, 5, 2),
                         {"estimated_tokens": 86, "extra_compactions": 1})

    def test_short_session_is_unchanged(self):
        self.assertEqual(token_economy.replay_compaction([{"context": 10, "output": 2}], 30, 5),
                         {"estimated_tokens": 12, "extra_compactions": 0})

    def test_invalid_threshold_is_not_a_saving(self):
        with self.assertRaises(ValueError):
            token_economy.replay_compaction([{"context": 10, "output": 0}], 5, 5)


if __name__ == "__main__":
    unittest.main()
