"""Configured shared-index limit at the actual global host payload boundary."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import unittest
from datetime import date
from unittest.mock import patch
from epitype import dream, host_sync, memspec


class SharedIndexScope(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.shared = self.root / "shared"
        self.project = self.root / "project"
        for vault in (self.shared, self.project):
            vault.mkdir()
        self.shared.joinpath("MEMORY.md").write_text("# Index\n\n" + "字" * 100 + "\n", encoding="utf-8")
        self.project.joinpath("MEMORY.md").write_text("# Project\n" + "字" * 1000, encoding="utf-8")
        self.config = {"vaults": [str(self.shared)], "shared_index_cap_bytes": 250}

    def plan(self, host, *, project=False):
        region = (memspec.HOST_SYNC_PROJECT_INDEX_REGION if project else memspec.HOST_SYNC_INDEX_REGION)
        payload = (host_sync.project_index_text(self.project) if project else
                   host_sync.index_text([self.project, self.shared]))
        return host_sync._plan(host, host, str(self.project) if project else None,
                               self.root / (host + ".md"), lambda: {region: payload}, self.root)

    def test_shared_overflow_blocks_both_global_hosts_without_limiting_project_index(self):
        with patch.object(memspec, "config_options", return_value=self.config):
            for host in ("claude", "codex"):
                self.assertTrue(any("shared_index_cap_bytes" in p for p in self.plan(host).problems))
                self.assertFalse(self.plan(host, project=True).problems)

    def test_nightly_measures_every_raw_file_with_one_cap(self):
        with patch.object(memspec, "config_options", return_value=self.config):
            result = dream._section_caps([self.project, self.shared], date.today(), None, self.config)
            entries = [e for e in result["examples"] if e.get("kind") == "index-file"]
            self.assertEqual(len(entries), 2)
            self.assertEqual({Path(e["path"]).parent for e in entries}, {self.shared, self.project})
            for entry in entries:
                self.assertEqual(entry["bytes"], Path(entry["path"]).stat().st_size)
                self.assertEqual(entry["cap"], 250)
            self.assertFalse(result["errors"])

    def test_default_cap_and_raw_header_cost_are_reported(self):
        self.config.pop("shared_index_cap_bytes")
        with patch.object(memspec, "config_options", return_value=self.config):
            self.assertFalse(self.plan("codex").problems)
        self.config["shared_index_cap_bytes"] = 250
        self.shared.joinpath("MEMORY.md").write_text("# " + "字" * 3000 + "\n短索引\n", encoding="utf-8")
        with patch.object(memspec, "config_options", return_value=self.config):
            self.assertFalse(self.plan("claude").problems)
            result = dream._section_caps([self.project, self.shared], date.today(), None, self.config)
            entries = [e for e in result["examples"] if e.get("kind") == "index-file"]
            self.assertEqual(len(entries), 2)
            shared = next(e for e in entries if Path(e["path"]).parent == self.shared)
            self.assertEqual(shared["bytes"], self.shared.joinpath("MEMORY.md").stat().st_size)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
