import sys; sys.dont_write_bytecode = True
from pathlib import Path
import unittest
import tempfile
import json
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

    def test_output_limits_are_host_specific_official_keys(self):
        values, errors = memspec.host_native_values([
            "host_claude_env:", "  BASH_MAX_OUTPUT_LENGTH: '15000'",
            "host_codex_config:", "  tool_output_token_limit: 5000"])
        self.assertEqual(errors, [])
        self.assertEqual(values[memspec.HOST_CLAUDE_ENV_FIELD]["BASH_MAX_OUTPUT_LENGTH"], "15000")
        self.assertEqual(values[memspec.HOST_CODEX_CONFIG_FIELD]["tool_output_token_limit"], 5000)

    def test_codex_content_blocks_measure_text_without_image_payloads(self):
        with tempfile.TemporaryDirectory(prefix="economy-") as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(json.dumps({"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "output": [{"type": "input_text", "text": "x" * 24000},
                    {"type": "input_image", "image_url": "omitted"}]}}), encoding="utf-8")
            report = token_economy.codex_output_report([path])
            self.assertEqual(report["observed_estimated_tokens"], 6000)
            self.assertEqual(report["output_limit_replay"], {"10000": 6000, "5000": 5000})


    def test_rotation_replay_charges_handoff_and_deduplicates_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            row = {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": 12},
                "last_token_usage": {"input_tokens": 10, "output_tokens": 2}}}}
            path.write_text(json.dumps(row) + "\n" + json.dumps(row), encoding="utf-8")
            self.assertEqual(token_economy.codex_usage_trace(path), [{"context": 10, "output": 2}])
        report = token_economy.rotation_report([{"context": 10, "output": 2}], 30, 20, 5)
        self.assertEqual(report["before"]["estimated_tokens"], 12)
        self.assertEqual(report["after"]["estimated_tokens"], 12)


if __name__ == "__main__":
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
