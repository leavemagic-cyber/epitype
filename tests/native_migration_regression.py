import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import tempfile
import unittest
from epitype import host_native, native_migration


class Migration(unittest.TestCase):
    def test_literal_only(self):
        for text in ('import os', 'prefix_rule(pattern=make(), decision="forbidden")', 'prefix_rule(pattern=["git"], decision="allow")'):
            with self.assertRaises((ValueError, SyntaxError)):
                native_migration.prefix_patterns(text)

    def fixture(self, root):
        vault = root / 'vault'
        vault.mkdir()
        (root / '.claude').mkdir()
        (root / '.codex/rules').mkdir(parents=True)
        (root / '.epitype').mkdir()
        (root / '.epitype/config.json').write_text(json.dumps({'vaults': [str(vault)]}), encoding='utf-8')
        (root / '.claude/settings.json').write_text(json.dumps({'permissions': {'deny': ['Bash(git clean*)', 'Read(other)']}, 'unrelated': True}), encoding='utf-8')
        (root / '.codex/config.toml').write_text('', encoding='utf-8')
        (vault / 'guard.md').write_text('---\nname: guard\nhost_source: https://developers.openai.com/codex/rules\nhost_claude_deny:\n  - Bash(git clean*)\nhost_codex_rules:\n  - git clean\n---\n', encoding='utf-8')
        legacy = root / '.codex/rules/legacy.rules'
        legacy.write_text('prefix_rule(pattern=["git", "clean"], decision="forbidden")\n', encoding='utf-8')
        host_native.apply([vault], home=root)
        return vault, legacy

    def test_adopt_exact_without_changing_settings(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            vault, legacy = self.fixture(root)
            settings = root / '.claude/settings.json'
            before = settings.read_bytes()
            self.assertEqual(native_migration.adopt_existing([vault], home=root, legacy_rules=legacy), 1)
            self.assertEqual(settings.read_bytes(), before)
            self.assertFalse(legacy.exists())
            self.assertTrue(legacy.with_suffix('.rules.migrated').exists())
            state = host_native.load_state(root)
            denies = state[host_native._key(settings)]['deny']
            self.assertTrue(denies['Bash(git clean*)']['owned'])
            self.assertNotIn('Read(other)', denies)

    def test_mismatch_keeps_legacy_and_ownership(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            vault, legacy = self.fixture(root)
            legacy.write_text('prefix_rule(pattern=["taskkill"], decision="forbidden")\n', encoding='utf-8')
            before = host_native.state_path(root).read_bytes()
            with self.assertRaises(ValueError):
                native_migration.adopt_existing([vault], home=root, legacy_rules=legacy)
            self.assertEqual(host_native.state_path(root).read_bytes(), before)
            self.assertTrue(legacy.exists())

    def test_original_43_and_10_preserved(self):
        fixture = Path(__file__).parent / 'fixtures/native_migration'
        before = json.loads((fixture / 'denies.json').read_text(encoding='utf-8'))
        desired, problems = host_native.collect([fixture])
        self.assertEqual(problems, [])
        self.assertEqual(len(before), 43)
        self.assertEqual(set(before), set(desired['deny']))
        generated = native_migration.prefix_patterns(host_native.rules_text(desired))
        self.assertEqual(len(generated), 10)
        self.assertEqual(generated, native_migration.prefix_patterns((fixture / 'legacy.rules').read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
