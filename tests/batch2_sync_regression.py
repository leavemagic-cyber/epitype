import sys; sys.dont_write_bytecode = True
from pathlib import Path
import io
import json
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import capture_route, host_native, host_sync, memspec


class ConfiguredScope(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="e-b2-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.global_vault = self.home / "global"
        self.global_vault.mkdir()
        self.project = self.home / "shop"
        self.project.mkdir()
        self.vault = self.home / ".claude/projects" / capture_route.project_slug(self.project) / "memory"
        self.vault.mkdir(parents=True)
        (self.vault.parent / "session.jsonl").write_text(json.dumps(
            {"type": "user", "cwd": str(self.project)}) + "\n", encoding="utf-8")
        config = self.home / ".epitype/config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"vaults": [str(self.global_vault), str(self.vault)]}), encoding="utf-8")
        self.env = patch.dict("os.environ", {memspec.EPITYPE_CONFIG_ENV: str(config)})
        self.env.start()
        self.addCleanup(self.env.stop)
        for relative in (".claude/settings.json", ".claude/CLAUDE.md", ".codex/AGENTS.md"):
            path = self.home / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text("{}" if relative.endswith("json") else "owner text\n", encoding="utf-8")
        (self.vault / "fb-project.md").write_text(
            "---\nname: fb-project\ndescription: project\nhost_claude_env:\n"
            "  CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS: '3'\n"
            "host_source: https://code.claude.com/docs/en/env-vars\n---\nproject only\n", encoding="utf-8")
        (self.vault / "MEMORY.md").write_text("# project\nproject index\n", encoding="utf-8")

    def test_project_only_never_touches_user_files(self):
        paths = [self.home / p for p in (".claude/settings.json", ".claude/CLAUDE.md", ".codex/AGENTS.md")]
        before = [p.read_bytes() for p in paths]
        self.assertEqual(host_sync.contract_vaults([self.vault]), [])
        for operation in (host_sync.check, host_sync.apply, host_native.apply):
            operation([self.vault], home=self.home, output=io.StringIO())
            self.assertEqual([p.read_bytes() for p in paths], before)
        settings = json.loads((self.project / ".claude/settings.local.json").read_text())
        self.assertEqual(settings["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"], "3")

    def test_argument_order_and_project_ledger_do_not_select_global(self):
        (self.vault / memspec.WORK_LEDGER_FILENAME).write_text("ledger", encoding="utf-8")
        self.assertEqual(host_sync.contract_vaults([self.vault, self.global_vault]), [self.global_vault])

    def test_missing_configuration_does_not_promote_any_vault(self):
        with patch.object(memspec, "config_options", return_value={}):
            self.assertEqual(host_sync.contract_vaults([self.vault]), [])


if __name__ == "__main__":
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
