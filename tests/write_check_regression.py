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

    def test_utf16_bom_is_recognised(self):
        # PowerShell 5.1 讀 UTF-16 LE/BE BOM 沒問題；別把它當成「沒有 UTF-8 BOM」而擋。
        for label, bom in (("le", "utf-16-le"), ("be", "utf-16-be")):
            target = self.root / f"u16-{label}.ps1"
            target.write_bytes(("﻿# 中文\nWrite-Output 1\n").encode(bom))
            # 現有檔已有 UTF-16 BOM，改後面的行不必動 BOM → 不擋。
            self.assertFalse(self.verdict("Edit", {"file_path": str(target),
                                                   "old_string": "1", "new_string": "2"}),
                             label)
            # 直接寫出帶 BOM 字元的新內容（`﻿` 開頭）也安全。
            self.assertFalse(self.verdict("Write", {"file_path": str(target),
                                                    "content": "﻿# 中文\n"}), label)

    def test_pure_ascii_needs_no_bom(self):
        # 沒有非 ASCII 字元時 UTF-8 與 cp950 位元組相同，編碼無關 → 不必 BOM、不擋。
        target = self.root / "ascii.ps1"
        self.assertFalse(self.verdict("Write", {"file_path": str(target),
                                                "content": "Write-Output 'hello'\n"}))
        # 一有非 ASCII 又沒 BOM 就回到要擋。
        self.assertTrue(self.verdict("Write", {"file_path": str(target),
                                               "content": "Write-Output '中文'\n"}))

    def test_module_and_manifest_suffixes_covered(self):
        for suffix in (".psm1", ".psd1"):
            target = self.root / f"mod{suffix}"
            self.assertTrue(self.verdict("Write", {"file_path": str(target),
                                                   "content": "# 中文\n"}), suffix)
            self.assertFalse(self.verdict("Write", {"file_path": str(target),
                                                    "content": "﻿# 中文\n"}), suffix)

    def test_large_write_is_not_released_early(self):
        # >256KB 的新內容不再提前放行：沒有 BOM 且含非 ASCII 仍要擋。
        big = "# 中文說明\n" + ("Write-Output 1\n" * 30000)
        self.assertGreater(len(big.encode("utf-8")), memspec.WRITE_GATE_MAX_CONTENT_BYTES)
        target = self.root / "big.ps1"
        self.assertTrue(self.verdict("Write", {"file_path": str(target), "content": big}))
        self.assertFalse(self.verdict("Write", {"file_path": str(target),
                                                "content": "﻿" + big}))

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
