"""Native context estimates retain origin, accounting gaps, and deferral boundaries."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import tempfile
import unittest
from unittest.mock import patch
from epitype import fixed_load, native_context as n


class NativeContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.path = self.root / "session.jsonl"

    def categories(self, *, structured=False):
        return [{"name": name, "tokens": value, **({"kind": "used"} if structured else {})}
                for name, value in zip(("Messages", "System tools", "MCP tools", "Memory files", "Skills", "System prompt"),
                                       (10, 20, 30, 40, 50, 60))]

    def row(self, **kwargs):
        return {"cwd": str(self.cwd), "timestamp": "2026-11-09T01:00:00.000Z", "version": "2.1.317", **kwargs}

    def write(self, rows):
        self.path.write_text("".join(json.dumps(r)+"\n" for r in rows), encoding="utf-8")

    def observe(self):
        return n.observe(self.path, self.cwd, allowed=fixed_load.allowed)

    def structured(self):
        return self.row(type="system", subtype="local_command", contextUsage={
            "total_tokens": 210, "raw_max_tokens": 1000000,
            "categories": self.categories(structured=True) + [
                {"name": "MCP tools (deferred)", "kind": "deferred", "tokens": 75000},
                {"name": "Autocompact buffer", "kind": "buffer", "tokens": 33000}],
            "mcp_tools": [{"name": "SECRET_SENTINEL", "tokens": 999999}]})

    def legacy(self, *, caller=n.USAGE_TOOL):
        value = {"plan": {"private": "SECRET_SENTINEL"}, "context": {
            "status": "ok", "tokensUsed": 213, "contextWindow": 1000000, "categories": self.categories()}}
        return [self.row(type="assistant", message={"content": [
                    {"type": "tool_use", "id": "one", "name": caller, "input": {"secret": "SECRET_SENTINEL"}}]}),
                self.row(type="user", message={"content": [
                    {"type": "tool_result", "tool_use_id": "one", "content": [{"type": "text", "text": json.dumps(value)}]}]})]

    def test_deferred_catalog_and_buffer_never_inflate_used_cost(self):
        self.write([self.structured()])
        report = self.observe();value = report["observation"]
        self.assertEqual(value["reported_used_tokens"], 210)
        self.assertEqual(value["non_message_category_tokens"], 200)
        self.assertEqual(value["deferred_tokens"], 75000)
        self.assertEqual(value["unattributed_tokens"], 0)
        self.assertEqual(value["per_tool_loaded_state"], "UNREPORTED")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(report))

    def test_legacy_requires_actual_caller_and_preserves_unattributed_cost(self):
        self.write(self.legacy())
        value = self.observe()["observation"]
        self.assertEqual(value["unattributed_tokens"], 3)
        self.assertIsNone(value["deferred_tokens"])
        self.assertEqual(value["tool_deferral_status"], "UNREPORTED")
        self.assertEqual(value["category_accounting_status"], "UNATTRIBUTED")
        self.assertEqual(value["version"], "2.1.317")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(value))
        for rows in (self.legacy(caller="unrelated"), self.legacy()[1:]):
            self.write(rows)
            self.assertIsNone(self.observe()["observation"])

    def test_project_and_sidechain_records_do_not_replace_same_task_reading(self):
        other = self.structured();other["cwd"] = str(self.root / "foreign")
        side = self.structured();side["isSidechain"] = True
        self.write([other, side])
        self.assertEqual(self.observe()["status"], "NO_OBSERVATION_IN_WINDOW")

    def test_oversized_history_is_bounded_and_unknown_is_not_zero(self):
        self.write([self.structured(), {"payload": "x" * 2000}])
        with patch.object(n, "TAIL_BYTES", 512):
            report = self.observe()
        self.assertLessEqual(report["read_bytes"], 512)
        self.assertIsNone(report["observation"])

    def test_invalid_latest_record_does_not_erase_valid_reading(self):
        invalid = self.structured();invalid["contextUsage"]["categories"][0]["tokens"] = True
        self.write([self.structured(), invalid])
        self.assertEqual(self.observe()["observation"]["reported_used_tokens"], 210)

    def test_another_session_usage_does_not_become_this_session_estimate(self):
        rows = self.legacy()
        block = rows[1]["message"]["content"][0]["content"][0]
        value = json.loads(block["text"]);value["context"]["session"] = "another-session"
        block["text"] = json.dumps(value)
        self.write(rows)
        self.assertIsNone(self.observe()["observation"])

    def test_inconsistent_category_sum_is_visible(self):
        row = self.structured();row["contextUsage"]["total_tokens"] = 100
        self.write([row])
        value = self.observe()["observation"]
        self.assertEqual(value["unattributed_tokens"], -110)
        self.assertEqual(value["category_accounting_status"], "INCONSISTENT")

    def test_excluded_path_is_checked_before_open(self):
        with self.assertRaises(ValueError), patch.object(Path, "open", side_effect=AssertionError("opened")):
            n.observe(self.path, self.cwd, allowed=lambda _: False)

    def test_cli_passes_explicit_observation_without_upgrading_runtime_or_comparison(self):
        from contextlib import redirect_stdout
        import io
        self.write([self.structured()])
        with patch.object(fixed_load, "discover", return_value=[]), redirect_stdout(io.StringIO()) as stream:
            fixed_load.main(["--cwd", str(self.cwd), "--home", str(self.root / "home"),
                             "--native-context", str(self.path), "--json"])
        value = json.loads(stream.getvalue())
        self.assertEqual(value["native_context"]["status"], "NATIVE_ESTIMATE")
        self.assertEqual(value["runtime_status"], "RUNTIME_UNMEASURED")
        self.assertEqual(fixed_load.compare(value, value)["status"], "RUNTIME_UNMEASURED")


if __name__ == "__main__":
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
