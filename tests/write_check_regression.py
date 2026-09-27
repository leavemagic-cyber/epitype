"""A card-declared PS 5.1 encoding check on real PreToolUse event shapes."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import card_lint, memspec
import pretooluse_gate
from _hook_common import run_synthetic, write_config


class WriteCheckRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-write-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.card = self.vault / "ps51.md"
        self.card.write_text(
            "---\nname: ps51\ndescription: PowerShell 5.1 需要 UTF-8 BOM\n"
            "last_verified_at: 2026-09-27\naliases: [PowerShell BOM]\n"
            "metadata:\n  type: feedback\nwrite_check: ps51_utf8_bom\n---\nBody\n",
            encoding="utf-8",
        )
        self.config = self.root / "config.json"
        write_config(self.config, [self.vault])

    def verdict(self, name, tool_input):
        event = {"hook_event_name": "PreToolUse", "tool_name": name,
                 "tool_input": tool_input, "cwd": str(self.root),
                 "session_id": uuid.uuid4().hex}
        result = run_synthetic(Path(pretooluse_gate.__file__), event, self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
        return payload.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_card_contract_and_new_files(self):
        _type, findings = card_lint.check_card(self.card, self.card.name)
        self.assertFalse(any(rule == "unarmed" or level == card_lint.FAIL
                             for level, rule, _reason in findings), findings)
        target = self.root / "new.ps1"
        self.assertTrue(self.verdict("Write", {"file_path": str(target), "content": "# 中文\n"}))
        self.assertFalse(self.verdict("Write", {"file_path": str(target),
                                                  "content": "\ufeff# 中文\n"}))
        self.assertFalse(self.verdict("Write", {"file_path": str(self.root / "new.txt"),
                                                  "content": "# 中文\n"}))

    def test_existing_script_keeps_bom(self):
        target = self.root / "existing.ps1"
        target.write_text("# 中文\nWrite-Output 1\n", encoding="utf-8")
        self.assertTrue(self.verdict("Edit", {"file_path": str(target),
                                                "old_string": "1", "new_string": "2"}))
        self.assertFalse(self.verdict("Write", {"file_path": str(target),
                                                   "content": "\ufeff# 中文\nWrite-Output 2\n"}))
        target.write_text("\ufeff# 中文\nWrite-Output 1\n", encoding="utf-8")
        self.assertFalse(self.verdict("Edit", {"file_path": str(target),
                                                   "old_string": "1", "new_string": "2"}))
        self.assertTrue(self.verdict("Write", {"file_path": str(target),
                                                "content": "# 中文\nWrite-Output 2\n"}))

    def test_codex_patch_envelope(self):
        target = self.root / "patch.ps1"
        plain = ("*** Begin Patch\n*** Add File: " + str(target)
                 + "\n+# 中文\n*** End Patch")
        bom = ("*** Begin Patch\n*** Add File: " + str(target)
               + "\n+\ufeff# 中文\n*** End Patch")
        self.assertTrue(self.verdict("Bash", {"command": plain}))
        self.assertFalse(self.verdict("Bash", {"command": bom}))

        target.write_text("# 中文\nWrite-Output 1\n", encoding="utf-8")
        update = ("*** Begin Patch\n*** Update File: " + str(target)
                  + "\n@@\n-Write-Output 1\n+Write-Output 2\n*** End Patch")
        self.assertTrue(self.verdict("Bash", {"command": update}))
        add_bom = ("*** Begin Patch\n*** Update File: " + str(target)
                   + "\n@@\n-# 中文\n+\ufeff# 中文\n*** End Patch")
        self.assertFalse(self.verdict("Bash", {"command": add_bom}))
        target.write_text("\ufeff# 中文\nWrite-Output 1\n", encoding="utf-8")
        self.assertFalse(self.verdict("Bash", {"command": update}))
        remove_bom = ("*** Begin Patch\n*** Update File: " + str(target)
                      + "\n@@\n-\ufeff# 中文\n+# 中文\n*** End Patch")
        self.assertTrue(self.verdict("Bash", {"command": remove_bom}))

    def test_invalid_or_nested_check_cannot_look_armed(self):
        self.card.write_text(self.card.read_text(encoding="utf-8").replace(
            "write_check: ps51_utf8_bom", "write_check: unknown"), encoding="utf-8")
        _type, findings = card_lint.check_card(self.card, self.card.name)
        self.assertTrue(any(level == card_lint.FAIL and rule == "write-check"
                            for level, rule, _reason in findings))
        self.card.write_text(self.card.read_text(encoding="utf-8").replace(
            "write_check: unknown", "  write_check: ps51_utf8_bom"), encoding="utf-8")
        _type, findings = card_lint.check_card(self.card, self.card.name)
        self.assertTrue(any(level == card_lint.FAIL and rule == "disarmed-field"
                            for level, rule, _reason in findings))

    def test_rule_is_card_driven(self):
        self.card.write_text(self.card.read_text(encoding="utf-8").replace(
            "write_check: ps51_utf8_bom", "status: superseded\nwrite_check: ps51_utf8_bom"),
            encoding="utf-8")
        target = self.root / "no-rule.ps1"
        self.assertFalse(self.verdict("Write", {"file_path": str(target), "content": "# 中文\n"}))
        self.card.unlink()
        self.assertFalse(self.verdict("Write", {"file_path": str(target), "content": "# 中文\n"}))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
