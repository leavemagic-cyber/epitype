"""Pinned legacy identity/hash boundary; no live configuration or rule sources."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1])]
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from epitype import rule_legacy, rule_pipeline


class LegacyBaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="epitype-rule-baseline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.card = self.root / "rule-test.md"
        self.baseline = self.root / "baseline.json"
        self.text = "---\nmetadata:\n  type: rule\ndescription: 核對當前結果\nupdated_at: 2026-10-01\naliases: [核對]\nrequire_when: 結果\nrequire_text: 讀回\n---\n保留核對結果。\n"
        self.digest = self.meaning(self.text)
        self.write_baseline(rule_legacy.build_baseline({str(self.card): self.digest}))

    @staticmethod
    def meaning(text):
        return hashlib.sha256(rule_pipeline._meaning(text, include_source=False).encode()).hexdigest()

    def write_baseline(self, document):
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.baseline.write_bytes(raw)
        self.options = {rule_legacy.PATH_OPTION: str(self.baseline),
                        rule_legacy.PIN_OPTION: hashlib.sha256(raw).hexdigest()}

    def check(self, path=None, digest=None, options=None):
        return rule_legacy.check(path or self.card, digest or self.digest,
                                 options=self.options if options is None else options)

    def test_unconfigured_legacy_is_compatible_and_explicitly_unverified(self):
        result = self.check(options={})
        self.assertEqual(result.status, rule_legacy.UNVERIFIED)
        self.assertTrue(result.allowed)
        self.assertFalse(result.matched)
        self.assertIn("UNVERIFIED", result.issues[0])
        self.assertEqual(result.to_dict()["source_verification"], "UNVERIFIED")
        self.assertFalse(result.to_dict()["action_authorization"])

    def test_path_and_pin_must_be_configured_together(self):
        for options in ({rule_legacy.PATH_OPTION: str(self.baseline)},
                        {rule_legacy.PIN_OPTION: self.options[rule_legacy.PIN_OPTION]}):
            with self.subTest(options=options):
                result = self.check(options=options)
                self.assertEqual(result.status, rule_legacy.INVALID)
                self.assertFalse(result.allowed)
                self.assertIn("both", result.issues[0])

    def test_configured_match_is_not_owner_or_semantic_verification(self):
        result = self.check()
        self.assertTrue(result.allowed)
        self.assertTrue(result.matched)
        self.assertEqual(result.status, rule_legacy.MATCHED)
        self.assertEqual(result.issues, [])
        self.assertEqual(result.to_dict()["source_verification"], "UNVERIFIED")
        self.assertEqual(result.to_dict()["semantic_entailment"], "REVIEW_REQUIRED")

    def test_shell_written_body_and_execution_changes_fail_on_read(self):
        self.card.write_text(self.text, encoding="utf-8")
        self.assertTrue(self.check().matched)
        for text in (self.text.replace("保留核對結果。", "每次都要請 Owner 批准。"),
                     self.text.replace("require_when: 結果", "require_when: 所有操作"),
                     self.text.replace("require_text: 讀回", "require_text: 先請批准"),
                     self.text.replace("description: 核對當前結果", "description: 一律停工")):
            with self.subTest(text=text):
                self.card.write_text(text, encoding="utf-8")
                result = self.check(digest=self.meaning(self.card.read_text(encoding="utf-8")))
                self.assertFalse(result.allowed)
                self.assertIn("meaning differs", result.issues[0])

    def test_retrieval_and_date_maintenance_use_same_caller_meaning_hash(self):
        maintained = self.text.replace("updated_at: 2026-10-01", "updated_at: 2026-10-03")
        maintained = maintained.replace("aliases: [核對]", "aliases: [核對, 讀回]")
        self.assertEqual(self.meaning(maintained), self.digest)
        self.assertTrue(self.check(digest=self.meaning(maintained)).matched)

    def test_source_migration_does_not_require_baseline_reenrollment(self):
        migrated = self.text.replace("---\n", "---\nrule_provenance: synthetic-source-metadata\n", 1)
        self.assertEqual(self.meaning(migrated), self.digest)
        self.assertTrue(self.check(digest=self.meaning(migrated)).matched)
        self.assertTrue(self.check().matched)

    def test_stop_discovery_alias_is_execution_meaning_not_routing(self):
        decided = self.text.replace("---\n", "---\ndecided_by: owner-explicit\nowner_quote: synthetic\ndecision_key: testing\n", 1)
        self.write_baseline(rule_legacy.build_baseline({str(self.card): self.meaning(decided)}))
        changed = decided.replace("aliases: [核對]", "aliases: [核對, 所有問題]")
        self.assertNotEqual(self.meaning(decided), self.meaning(changed))
        self.assertFalse(self.check(digest=self.meaning(changed)).allowed)

    def test_new_card_is_rejected_without_creating_or_editing_baseline(self):
        before = self.baseline.read_bytes()
        result = self.check(path=self.root / "new-rule.md")
        self.assertFalse(result.allowed)
        self.assertIn("no automatic enrollment", result.issues[0])
        self.assertEqual(self.baseline.read_bytes(), before)
        self.assertFalse((self.root / "new-rule.md").exists())

    def test_fresh_read_detects_deletion_after_success(self):
        self.assertTrue(self.check().matched)
        self.baseline.unlink()
        result = self.check()
        self.assertFalse(result.allowed)
        self.assertIn("cannot be read", result.issues[0])

    def test_valid_json_tampering_with_same_size_mtime_and_matching_new_hash_is_rejected(self):
        self.assertTrue(self.check().matched)
        st = self.baseline.stat()
        raw = self.baseline.read_bytes()
        changed_digest = ("0" if self.digest[0] != "0" else "1") + self.digest[1:]
        altered = raw.replace(self.digest.encode(), changed_digest.encode())
        self.assertEqual(len(raw), len(altered))
        json.loads(altered)
        self.baseline.write_bytes(altered)
        os.utime(self.baseline, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.baseline.stat().st_mtime_ns, st.st_mtime_ns)
        self.assertEqual(self.baseline.stat().st_size, st.st_size)
        result = self.check(digest=changed_digest)
        self.assertFalse(result.allowed)
        self.assertIn("fixed rule_source_baseline_sha256", result.issues[0])
        self.assertEqual(self.options[rule_legacy.PIN_OPTION], hashlib.sha256(raw).hexdigest())

    def test_repeated_read_never_caches_success_or_failure(self):
        raw = self.baseline.read_bytes()
        self.assertTrue(self.check().matched)
        self.baseline.write_bytes(b"corrupted")
        self.assertFalse(self.check().allowed)
        self.baseline.write_bytes(raw)
        self.assertTrue(self.check().matched)

    def test_default_config_options_are_read_each_time(self):
        with patch.object(rule_legacy.memspec, "config_options", side_effect=[self.options, {}]) as config:
            self.assertTrue(rule_legacy.check(self.card, self.digest).matched)
            self.assertEqual(rule_legacy.check(self.card, self.digest).status, rule_legacy.UNVERIFIED)
            self.assertEqual(config.call_count, 2)

    def test_bad_pin_values_are_invalid(self):
        for value in (None, "", "x" * 64, "0" * 63, "0" * 64 + "\n", 42):
            with self.subTest(value=value):
                options = dict(self.options, **{rule_legacy.PIN_OPTION: value})
                result = self.check(options=options)
                self.assertFalse(result.allowed)
                self.assertIn(rule_legacy.PIN_OPTION, result.issues[0])

    def test_bad_baseline_path_values_are_invalid(self):
        for value in (None, "", "baseline.json", "C:baseline.json", "\x00baseline", 42):
            with self.subTest(value=value):
                options = dict(self.options, **{rule_legacy.PATH_OPTION: value})
                result = self.check(options=options)
                self.assertFalse(result.allowed)

    def test_wrong_pin_and_missing_file_fail_closed(self):
        options = dict(self.options, **{rule_legacy.PIN_OPTION: "0" * 64})
        self.assertFalse(self.check(options=options).allowed)
        options = dict(self.options, **{rule_legacy.PATH_OPTION: str(self.root / "missing.json")})
        self.assertFalse(self.check(options=options).allowed)

    def test_invalid_current_meaning_hash_and_relative_rule_paths_are_rejected(self):
        for digest in ("not-a-hash", "x" * 64, "0" * 64 + "\n", 42):
            with self.subTest(digest=digest):
                self.assertFalse(rule_legacy.check(self.card, digest, options=self.options).allowed)
        for path in ("rule-test.md", "C:rule-test.md", "\\rule-test.md", None):
            with self.subTest(path=path):
                self.assertFalse(rule_legacy.check(path, self.digest, options=self.options).allowed)

    def test_baseline_schema_version_entries_hash_and_paths_are_validated(self):
        cases = [[], {"version": 2, "entries": {}}, {"version": True, "entries": {}},
                 {"version": 1.0, "entries": {}}, {"version": 1},
                 {"version": 1, "entries": []}, {"version": 1, "entries": {"relative.md": self.digest}},
                 {"version": 1, "entries": {str(self.card): "x" * 64}},
                 {"version": 1, "entries": {}, "auto_enroll": True}]
        for document in cases:
            with self.subTest(document=document):
                self.write_baseline(document)
                result = self.check()
                self.assertFalse(result.allowed)
                self.assertIn("invalid legacy baseline", result.issues[0])

    def test_malformed_json_and_duplicate_keys_are_rejected_even_with_correct_pin(self):
        for raw in (b"{broken", b'{"version":1,"version":1,"entries":{}}',
                    ('{"version":1,"entries":{' + json.dumps(str(self.card)) + ':"' + self.digest + '",'
                     + json.dumps(str(self.card)) + ':"' + self.digest + '"}}').encode(), b"\xff"):
            with self.subTest(raw=raw):
                self.baseline.write_bytes(raw)
                self.options[rule_legacy.PIN_OPTION] = hashlib.sha256(raw).hexdigest()
                self.assertFalse(self.check().allowed)

    def test_windows_case_slashes_parent_segments_and_unc_identity(self):
        windows = r"C:\Vault\Rule.md"
        self.write_baseline(rule_legacy.build_baseline({windows: self.digest}))
        for path in (r"c:\vault\RULE.md", "C:/Vault/Rule.md", r"C:\Vault\temp\..\Rule.md",
                     r"\\?\C:\Vault\Rule.md"):
            with self.subTest(path=path):
                self.assertTrue(self.check(path=path).matched)
        self.write_baseline(rule_legacy.build_baseline({r"\\Server\Share\Rule.md": self.digest}))
        self.assertTrue(self.check(path="//server/share/RULE.md").matched)
        self.assertTrue(self.check(path=r"\\?\UNC\Server\Share\Rule.md").matched)
        self.assertTrue(self.check(path=r"\\?\unc\SERVER\SHARE\Rule.md").matched)

    def test_windows_path_normalization_does_not_merge_different_unicode_names(self):
        self.assertNotEqual(rule_legacy.normalize_path(r"C:\Vault\Straße.md"),
                            rule_legacy.normalize_path(r"C:\Vault\Strasse.md"))

    def test_duplicate_normalized_windows_membership_is_invalid(self):
        document = {"version": 1, "entries": {r"C:\Vault\Rule.md": self.digest,
                                                   "c:/vault/RULE.md": self.digest}}
        self.write_baseline(document)
        self.assertFalse(self.check(path=r"C:\Vault\Rule.md").allowed)
        with self.assertRaises(ValueError):
            rule_legacy.build_baseline(document["entries"])

    def test_bom_and_uppercase_hex_are_accepted_without_semantic_claim(self):
        raw = b"\xef\xbb\xbf" + json.dumps({"version": 1, "entries": {str(self.card): self.digest.upper()}}).encode()
        self.baseline.write_bytes(raw)
        self.options[rule_legacy.PIN_OPTION] = hashlib.sha256(raw).hexdigest().upper()
        self.assertTrue(self.check(digest=self.digest.upper()).matched)

    def test_empty_explicit_baseline_admits_no_legacy_cards(self):
        self.write_baseline(rule_legacy.build_baseline({}))
        self.assertFalse(self.check().allowed)

    def test_generator_cli_stdout_only_does_not_read_cards_or_adopt_configuration(self):
        entries_file = self.root / "selected-entries.json"
        selected = {str(self.root / "does-not-exist.md"): self.digest}
        entries_file.write_text(json.dumps(selected), encoding="utf-8")
        baseline_before = self.baseline.read_bytes()
        captured = io.StringIO()
        with patch.object(rule_legacy.memspec, "config_options", side_effect=AssertionError("no config adoption")), redirect_stdout(captured):
            self.assertEqual(rule_legacy.main(["--entries-json", str(entries_file)]), 0)
        self.assertEqual(json.loads(captured.getvalue()), rule_legacy.build_baseline(selected))
        self.assertEqual(self.baseline.read_bytes(), baseline_before)
        self.assertFalse((self.root / "does-not-exist.md").exists())

    def test_invalid_config_object_is_diagnosed(self):
        self.assertFalse(self.check(options=[]).allowed)

    def test_excessively_nested_pinned_json_is_diagnosed_not_accepted(self):
        raw = b"[" * 3000 + b"0" + b"]" * 3000
        self.baseline.write_bytes(raw)
        self.options[rule_legacy.PIN_OPTION] = hashlib.sha256(raw).hexdigest()
        result = self.check()
        self.assertFalse(result.allowed)
        self.assertIn("invalid legacy baseline", result.issues[0])


if __name__ == "__main__":
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
