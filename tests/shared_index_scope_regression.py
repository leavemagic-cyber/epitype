"""The shared index follows the host load limit too; old cap keys only produce a notice."""
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

    def test_old_shared_cap_key_blocks_nothing_and_says_it_is_unused(self):
        with patch.object(memspec, "config_options", return_value=self.config):
            for host in ("claude", "codex"):
                plan = self.plan(host)
                self.assertFalse(plan.problems)
                self.assertTrue(any("shared_index_cap_bytes" in n and "已不再使用" in n for n in plan.notices))
                self.assertFalse(self.plan(host, project=True).problems)

    def test_nightly_uses_host_limit_for_shared_and_project_alike(self):
        with patch.object(memspec, "config_options", return_value=self.config):
            result = dream._section_caps([self.project, self.shared], date.today(), None, self.config)
            self.assertFalse([e for e in result["examples"] if e.get("kind") == "index-file"])
            self.assertIn("已不再使用", result["note"])
            self.assertFalse(result["errors"])
        self.shared.joinpath("MEMORY.md").write_text("# Index\n" + "- x\n" * 200, encoding="utf-8")
        with patch.object(memspec, "config_options", return_value=self.config):
            result = dream._section_caps([self.project, self.shared], date.today(), None, self.config)
        entries = [e for e in result["examples"] if e.get("kind") == "index-file"]
        self.assertEqual([Path(e["path"]).parent for e in entries], [self.shared])
        self.assertEqual((entries[0]["cap"], entries[0]["cap_lines"], entries[0]["over_lines"]),
                         (25_000, 200, 1))

    def test_without_old_keys_there_is_no_notice(self):
        self.config.pop("shared_index_cap_bytes")
        with patch.object(memspec, "config_options", return_value=self.config):
            self.assertFalse(self.plan("codex").problems)
            self.assertFalse([n for n in self.plan("codex").notices if "已不再使用" in n])
            result = dream._section_caps([self.project, self.shared], date.today(), None, self.config)
            self.assertFalse(result["note"] and "已不再使用" in result["note"])


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
