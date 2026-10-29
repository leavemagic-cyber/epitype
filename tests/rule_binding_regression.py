"""Generated rule payload scope and persistent legacy read migration."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import copy
import hashlib
import json
import os
import subprocess
import unittest

import rule_pipeline_regression as fixture
from epitype import memspec, rule_legacy, rule_pipeline, rule_provenance


class RuleBindings(unittest.TestCase):
    setUp = fixture.RulePipeline.setUp
    card = fixture.RulePipeline.card

    def fields(self, text):
        return memspec.frontmatter_text(text)[0]

    def legacy_payload(self, text):
        fields = self.fields(text)
        document = json.loads(fields["rule_provenance"])
        document["payload_binding"].pop("version")
        document["payload_binding"]["sha256"] = hashlib.sha256(
            rule_pipeline._meaning(text, include_source=False).encode()).hexdigest()
        return self.with_document(text, document), document

    def with_document(self, text, document):
        return "\n".join("rule_provenance: " + json.dumps(rule_provenance.serialize(document), ensure_ascii=False)
                         if line.startswith("rule_provenance:") else line for line in text.splitlines()) + "\n"

    def test_v2_binding_accepts_all_auxiliary_edits_but_rejects_text_and_scope(self):
        before = self.card()
        document = json.loads(self.fields(before)["rule_provenance"])
        after = before.replace("核對原文的規則", "另一個說明").replace("[source-test]", "[new-alias]")
        after += "\n[[related-card]]\n\n2026-10-29：補充歷史。\n"
        self.assertEqual(rule_pipeline.payload_issues(document, after), [])
        self.assertEqual(rule_pipeline.admission(before, after, "rule", self.target), [])
        self.assertTrue(rule_pipeline.payload_issues(document, after.replace(document["text"], "changed")))
        changed = copy.deepcopy(document)
        changed["scope"]["actions"] = ["changed"]
        self.assertTrue(rule_pipeline.payload_issues(changed, after))

    def test_old_inline_binding_migrates_persistently_without_changing_card_or_binding(self):
        before, document = self.legacy_payload(self.card())
        self.assertEqual(rule_pipeline.payload_issues(document, before), [])
        after = before.replace("核對原文的規則", "新說明") + "\n[[related]]\n2026-10-29：歷史。\n"
        self.target.write_text(after, encoding="utf-8")
        # A fresh interpreter must use the on-read migration, not process memory.
        run = subprocess.run([sys.executable, "-c",
            "from pathlib import Path; from epitype import memspec,rule_pipeline; import json,sys; "
            "t=Path(sys.argv[1]).read_text(encoding='utf-8'); f,_=memspec.frontmatter_text(t); "
            "sys.exit(bool(rule_pipeline.payload_issues(json.loads(f['rule_provenance']),t)))", str(self.target)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.target.read_text(encoding="utf-8"), after)
        self.assertTrue(rule_pipeline.payload_issues(document, after.replace(document["text"], "changed")))

    def test_old_binding_cannot_migrate_an_already_changed_payload(self):
        before, document = self.legacy_payload(self.card())
        self.assertTrue(rule_pipeline.payload_issues(document, before.replace(document["text"], "changed")))
        document["payload_binding"]["reviewed_by"] = ""
        self.assertTrue(rule_pipeline.payload_issues(document, before))

    def test_scope_keeps_existing_sequence_execution_values_bound(self):
        before = self.card(forbidden="\n  - original action", hosts="\n  - codex")
        document = json.loads(self.fields(before)["rule_provenance"])
        self.assertTrue(rule_pipeline.payload_issues(document, before.replace("original action", "different action")))
        self.assertTrue(rule_pipeline.payload_issues(document, before.replace("  - codex", "  - claude")))

    def test_external_binding_uses_the_same_read_migration(self):
        before, document = self.legacy_payload(self.card())
        source = self.root / "provenance.json"
        source.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        reference = {"ref": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        before = self.with_document(before, reference)
        self.target.write_text(before, encoding="utf-8")
        self.assertTrue(rule_pipeline.inspect_card(self.fields(before), self.target).verified)
        after = before + "\n2026-10-29：補充歷史。\n"
        self.target.write_text(after, encoding="utf-8")
        self.assertTrue(rule_pipeline.inspect_card(self.fields(after), self.target).verified)

    def test_frozen_baseline_migrates_only_after_match_and_keeps_pin_and_membership(self):
        before = "\n".join(line for line in self.card().splitlines()
                           if not line.startswith("rule_provenance:")) + "\n"
        self.target.write_text(before, encoding="utf-8")
        digest = hashlib.sha256(rule_pipeline._meaning(before, include_source=False,
            exclude_metadata_modified=False).encode()).hexdigest()
        baseline = self.root / "baseline.json"
        baseline.write_text(json.dumps(rule_legacy.build_baseline({str(self.target): digest})), encoding="utf-8")
        config = Path(os.environ[memspec.EPITYPE_CONFIG_ENV])
        options = json.loads(config.read_text(encoding="utf-8"))
        options.update(rule_source_baseline=str(baseline),
                       rule_source_baseline_sha256=hashlib.sha256(baseline.read_bytes()).hexdigest())
        config.write_text(json.dumps(options), encoding="utf-8")
        self.assertTrue(rule_pipeline.legacy_integrity(self.fields(before), self.target, card_text=before).matched)
        original_baseline = baseline.read_bytes()
        after = before.replace("核對原文的規則", "新說明") + "\n[[related]]\n2026-10-29：歷史。\n"
        self.assertTrue(rule_pipeline.legacy_integrity(self.fields(after), self.target, card_text=after).matched)
        self.assertFalse(rule_pipeline.legacy_integrity(self.fields(after), self.vault / "new.md", card_text=after).allowed)
        changed = after.replace(self.document["text"], "changed")
        self.assertFalse(rule_pipeline.legacy_integrity(self.fields(changed), self.target, card_text=changed).allowed)
        self.assertEqual(baseline.read_bytes(), original_baseline)
        baseline.write_bytes(original_baseline + b" ")
        self.assertFalse(rule_pipeline.legacy_integrity(self.fields(after), self.target, card_text=after).allowed)

    def test_approval_text_sha256_is_still_exact(self):
        document = copy.deepcopy(self.document)
        quote = "我核准這個專案 A 案的規則文字。"
        self.original.replace_owner(quote)
        document.update(kind="ai-derived", author="synthetic-ai", text="Never submit without evidence.", quote=quote)
        document["sources"] = [self.original.source(2)]
        document["scope_basis"]["quote"] = quote
        document["approval"] = {"by": "owner", "source": 0, "quote": quote,
                                "text_sha256": hashlib.sha256(document["text"].encode()).hexdigest()}
        text = self.card(document)
        self.target.write_text(text, encoding="utf-8")
        self.assertTrue(rule_pipeline.inspect_card(self.fields(text), self.target).verified)
        document["approval"]["text_sha256"] = "0" * 64
        text = self.card(document)
        self.target.write_text(text, encoding="utf-8")
        self.assertFalse(rule_pipeline.inspect_card(self.fields(text), self.target).verified)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
