"""Source validation, advisory writes and both generated host projections."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import copy
import hashlib
import json
import time
import unittest
from unittest.mock import patch

import action_guard_regression as guard
import rule_provenance_regression as source_fixture
from epitype import card_lint, core_gen, host_sync, memspec, owner_rules, rule_pipeline
import pretooluse_gate as gate


class RulePipeline(unittest.TestCase):
    def setUp(self):
        guard.ActionGuardRegression.setUp(self)
        original = source_fixture.ProvenanceTests(methodName="test_exact_owner_original_both_hosts_and_queue")
        original.setUp()
        self.addCleanup(original.doCleanups)
        self.original = original
        self.document = copy.deepcopy(original.document)
        self.target = self.vault / "rule-source-test.md"

    def card(self, document=None, **updates):
        document = copy.deepcopy(document or self.document)
        fields = {"name": "來源規則", "description": "核對原文的規則", "metadata": "\n  type: rule",
                  "layer": "resident", "section": "evidence", "order": "1",
                  "approved_by": "synthetic-reviewer", "approved_at": "2026-10-03",
                  "text": json.dumps(document["text"], ensure_ascii=False),
                  "owner_quote": json.dumps(document["quote"], ensure_ascii=False),
                  "decided_by": "owner-explicit", "aliases": "[source-test]",
                  "rule_provenance": json.dumps(json.dumps(document, ensure_ascii=False), ensure_ascii=False)}
        fields.update(updates)
        unbound = "---\n" + "\n".join(f"{key}: {value}" for key, value in fields.items()) + "\n---\n來源測試。\n"
        if fields.get("rule_provenance"):
            # Repeated reviews can share a clock tick; metadata validation must
            # not depend on a freshly generated timestamp changing the envelope.
            document = rule_pipeline.bind_payload(document, unbound, reviewer="synthetic-reviewer",
                                                 reviewed_at="2026-10-03T00:00:00+00:00")
            fields["rule_provenance"] = json.dumps(json.dumps(document, ensure_ascii=False), ensure_ascii=False)
        return "---\n" + "\n".join(f"{key}: {value}" for key, value in fields.items()) + "\n---\n來源測試。\n"

    def write_call(self, text, host):
        event = {"session_id": "source-admission-" + host}
        if host == "claude":
            event.update(tool_name="Write", tool_input={"file_path": str(self.target), "content": text})
        else:
            event.update(tool_name="apply_patch", tool_input={"input":
                f"*** Begin Patch\n*** Add File: {self.target}\n"
                + "\n".join("+" + line for line in text.splitlines()) + "\n*** End Patch"})
        return gate._handle(event, time.monotonic(), [])

    def test_real_write_gate_both_payloads_warn_for_invalid_source_without_denial(self):
        false = copy.deepcopy(self.document)
        false["quote"] = false["text"] = "Owner 從未說過的限制。"
        for host in ("claude", "codex"):
            for text in (self.card(false), self.card(rule_provenance="")):
                with self.subTest(host=host):
                    value = self.write_call(text, host)
                    self.assertNotEqual((value or {}).get("hookSpecificOutput", {}).get(
                        "permissionDecision"), "deny")
                    # Write warnings may be deduplicated, but source validity must
                    # still reject false authority before a rule is projected.
                    issues = rule_pipeline.admission("", text, "rule", self.target)
                    self.assertTrue(issues)
                    self.assertIn("來源", "\n".join(issues))

    def test_real_write_gate_both_payloads_accept_source_bound_rule(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                value = self.write_call(self.card(), host)
                if value:
                    self.assertNotEqual(value.get("hookSpecificOutput", {}).get("permissionDecision"), "deny", value)

    def test_temp_staging_resolves_relative_source_against_real_card(self):
        document = copy.deepcopy(self.document)
        (self.vault / "owner.jsonl").write_bytes(self.original.path.read_bytes())
        document["sources"][0]["path"] = "owner.jsonl"
        reason, _advice = gate._card_review("rule-source-test.md", self.card(document),
                                          before_text="", source_path=self.target)
        self.assertIsNone(reason)

    def test_legacy_maintenance_does_not_require_new_action_approval(self):
        before = self.card(rule_provenance="")
        before = before.replace("rule_provenance: \n", "")
        after = before.replace("aliases: [source-test]", "aliases: [source-test, 原文]")
        reason, _advice = gate._card_review("rule-source-test.md", after,
                                          before_text=before, source_path=self.target)
        self.assertIsNone(reason)
        changed = after.replace(self.document["text"], "必須每次先問 Owner")
        reason, _advice = gate._card_review("rule-source-test.md", changed,
                                          before_text=before, source_path=self.target)
        self.assertIn("來源", reason)

    def test_frozen_legacy_metadata_hash_stays_strict(self):
        before = '---\nname: synthetic\nmetadata:\n  type: rule\n  modified: 2026-09-24T08:01:40.599Z\n---\nKeep existing evidence.\n'
        after = before.replace('2026-09-24T08:01:40.599Z', '2026-10-03T13:54:05.506Z')
        frozen = json.dumps([{
            'name': ['name: synthetic'],
            'metadata': ['metadata:', '  type: rule', '  modified: 2026-09-24T08:01:40.599Z'],
        }, 'Keep existing evidence.'], ensure_ascii=False, sort_keys=True)
        # The frozen value comes from the original algorithm, not the new
        # default binding calculation. No baseline pin is migrated or relaxed.
        self.assertEqual(rule_pipeline._meaning(before, include_source=False,
                                               exclude_metadata_modified=False), frozen)
        fields, _ = memspec.frontmatter_text(before)
        with patch('epitype.rule_legacy.check') as check:
            rule_pipeline.legacy_integrity(fields, self.target, card_text=before)
            original_sha = check.call_args.args[1]
            self.assertEqual(original_sha, hashlib.sha256(frozen.encode()).hexdigest())
            rule_pipeline.legacy_integrity(fields, self.target, card_text=after)
            self.assertNotEqual(check.call_args.args[1], original_sha)
        self.assertEqual(rule_pipeline._meaning(before, include_source=False),
                         rule_pipeline._meaning(after, include_source=False))

    def test_ai_interpretation_and_owner_approval_are_distinct(self):
        document = copy.deepcopy(self.document)
        document.update(kind="ai-derived", author="synthetic-ai", text="依原文核對這個案子的規則。")
        self.target.write_text(self.card(document, handoff_rule="true", status="active"), encoding="utf-8")
        block = owner_rules.render([self.vault])
        self.assertIn("AI 執行整理（作者：synthetic-ai", block)
        self.assertIn(document["quote"], block)
        self.assertIn(document["text"], block)
        self.assertNotIn("原始來源尚未驗證", block)

    def test_all_core_entrypoints_share_exact_source_text_and_manifest(self):
        self.target.write_text(self.card(), encoding="utf-8")
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(errors)
        self.assertEqual(rules[0]["text"], self.document["text"])
        self.assertEqual(rules[0]["authorship"], "owner")
        self.assertEqual(rules[0]["provenance"]["status"], "VERIFIED")
        out = self.root / "core.md"
        result = core_gen.generate([self.vault], out, config={})
        self.assertIn(result["status"], ("written", "unchanged"))
        pack = json.loads(Path(result["pack"]).read_text(encoding="utf-8"))
        self.assertEqual(pack["cards"][0]["provenance"]["scope"], self.document["scope"])
        for host in ("codex", "claude"):
            self.assertEqual(host_sync.project_rules_text(self.vault, host),
                             core_gen.host_view(out.read_text(encoding="utf-8"), host).rstrip())
            with patch.object(host_sync, "contract_vaults", return_value=[self.vault]):
                self.assertEqual(host_sync.rules_text([self.vault], host),
                                 host_sync.project_rules_text(self.vault, host))

    def test_missing_original_is_advisory_in_core_and_both_host_views(self):
        self.target.write_text(self.card(), encoding="utf-8")
        out = self.root / "core.md"
        core_gen.generate([self.vault], out, config={})
        self.original.path.unlink()
        result = core_gen.generate([self.vault], out, config={})
        self.assertEqual(result["status"], "written", result)
        rendered = out.read_text(encoding="utf-8")
        self.assertIn(self.document["text"], rendered)
        self.assertIn("原始對話無法讀取", rendered)
        pack = json.loads(Path(result["pack"]).read_text(encoding="utf-8"))
        evidence = pack["cards"][0]["provenance"]
        self.assertEqual(evidence["status"], "UNVERIFIED")
        self.assertTrue(evidence["source_unavailable"])
        self.assertFalse(evidence["action_authorization"])
        self.assertNotEqual(core_gen.check([self.vault], out)["status"], core_gen.STATUS_UNREADABLE)
        for host in ("codex", "claude"):
            self.assertEqual(host_sync.project_rules_text(self.vault, host),
                             core_gen.host_view(rendered, host).rstrip())
        fields, _ = memspec.frontmatter_fields(self.target)
        with self.assertRaises(ValueError):
            rule_pipeline.projection(fields, self.target)

    def test_invalid_projection_preserves_prior_output_no_silent_partial_success(self):
        self.target.write_text(self.card(), encoding="utf-8")
        out = self.root / "core.md"
        core_gen.generate([self.vault], out, config={})
        previous = out.read_bytes()
        self.original.path.write_bytes(self.original.path.read_bytes().replace(
            self.original.quote.encode(), b"fabricated directive"))
        result = core_gen.generate([self.vault], out, config={})
        self.assertEqual(result["status"], core_gen.STATUS_UNREADABLE)
        self.assertEqual(out.read_bytes(), previous)
        self.assertEqual(core_gen.check([self.vault], out)["status"], core_gen.STATUS_UNREADABLE)
        notices = []
        self.assertEqual(host_sync.project_rules_text(self.vault, "codex", notices=notices), "")
        self.assertIn("未通過", notices[0])

    def test_missing_original_does_not_hide_changed_payload_or_revive_expiry(self):
        self.target.write_text(self.card(), encoding="utf-8")
        self.original.path.unlink()
        self.target.write_text(self.target.read_text(encoding="utf-8").replace(
            self.document["text"], "Extra unreviewed rule."), encoding="utf-8")
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(rules)
        self.assertTrue(any("INVALID" in error for error in errors))
        document = copy.deepcopy(self.document)
        document["scope"]["expiry"] = "2000-01-01"
        self.target.write_text(self.card(document), encoding="utf-8")
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(errors)
        self.assertFalse(rules)

    def test_missing_original_does_not_hide_another_source_identity_error(self):
        document = copy.deepcopy(self.document)
        missing = copy.deepcopy(document['sources'][0])
        missing['path'] = str(self.root / 'missing-original.jsonl')
        document['sources'][0]['record_sha256'] = '0' * 64
        document['sources'].append(missing)
        self.target.write_text(self.card(document), encoding='utf-8')
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(rules)
        self.assertTrue(any('INVALID' in error and 'identity' in error for error in errors))

    def test_missing_original_keeps_reciprocal_successor_without_reviving_predecessor(self):
        successor = self.vault / 'rule-source-successor.md'
        self.target.write_text(self.card(status='superseded', superseded_by=successor.name), encoding='utf-8')
        document = copy.deepcopy(self.document)
        document['supersedes'] = [self.target.name]
        successor.write_text(self.card(document), encoding='utf-8')
        self.original.path.unlink()
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(errors)
        self.assertEqual([row['path'] for row in rules], [successor.name])
        self.assertEqual(rules[0]['provenance']['status'], 'UNVERIFIED')
        self.target.write_text(self.card(status='active'), encoding='utf-8')
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertTrue(any('predecessor is still active' in error for error in errors))

    def test_source_lint_rejects_projection_mutation(self):
        self.target.write_text(self.card(text='"擴大為所有案件都必須問 Owner"'), encoding="utf-8")
        _kind, findings = card_lint.check_card(self.target, self.target.name)
        self.assertTrue(any(level == card_lint.FAIL and key == "rule-source" for level, key, _ in findings))

    def test_review_binding_covers_rule_text_and_sequence_execution_fields(self):
        before = self.card()
        mutations = [before.replace(self.document["text"], "必須每次另問Owner批准所有工作。"),
                     before.replace("\n---\n來源測試", "\nforbidden: [所有工作]\n---\n來源測試"),
                     before.replace("\n---\n來源測試", "\nforbidden:\n  - 所有工作\n---\n來源測試")]
        for after in mutations:
            with self.subTest(after=after[-80:]):
                self.assertTrue(rule_pipeline.admission(before, after, "rule", self.target))
                reason, _advice = gate._card_review(self.target.name, after, before_text=before,
                                                  source_path=self.target)
                self.assertIn("來源", reason)

    def test_metadata_modified_dates_do_not_change_reviewed_payload(self):
        before = self.card(metadata="\n  type: rule\n  modified: 2026-09-24T08:01:40.599Z")
        fields, _ = memspec.frontmatter_text(before)
        document = json.loads(fields["rule_provenance"])
        for replacement in ("2026-10-03T13:53:00.703Z", "2026-10-03T13:54:05.506Z",
                            '"2026-10-03T13:54:05.506Z"', "2026-10-03"):
            after = before.replace("2026-09-24T08:01:40.599Z", replacement)
            with self.subTest(replacement=replacement):
                self.assertEqual(rule_pipeline.payload_issues(document, after), [])
                self.assertEqual(rule_pipeline.admission(before, after, "rule", self.target), [])
        without = before.replace("  modified: 2026-09-24T08:01:40.599Z\n", "")
        self.assertEqual(rule_pipeline.payload_issues(document, without), [])

    def test_generated_payload_excludes_auxiliary_metadata(self):
        before = self.card(status="active", metadata="\n  type: rule\n  node_type: memory"
            "\n  modified: 2026-09-24T08:01:40.599Z\n  survey:"
            "\n    modified: 2026-09-24\n    scope: project")
        fields, _ = memspec.frontmatter_text(before)
        document = json.loads(fields["rule_provenance"])
        for old, new in (("type: rule", "type: decision"),
                         ("node_type: memory", "node_type: changed"),
                         ("    modified: 2026-09-24", "    modified: 2026-10-03"),
                         ("    scope: project", "    scope: global"),
                         ("核對原文的規則", "改變規則適用範圍"),
                         ("status: active", "status: superseded")):
            with self.subTest(field=old):
                self.assertEqual(rule_pipeline.payload_issues(document, before.replace(old, new)), [])

    def test_generated_payload_excludes_auxiliary_dates(self):
        before = self.card(metadata="\n  type: rule\n  modified: 2026-09-24")
        fields, _ = memspec.frontmatter_text(before)
        document = json.loads(fields["rule_provenance"])
        self.assertEqual(rule_pipeline.payload_issues(document,
            before.replace("  modified: 2026-09-24", "  modified: must stop every task")), [])
        top = self.card(modified="2026-09-24")
        fields, _ = memspec.frontmatter_text(top)
        document = json.loads(fields["rule_provenance"])
        self.assertEqual(rule_pipeline.payload_issues(document,
            top.replace("modified: 2026-09-24", "modified: 2026-10-03")), [])

    def test_field_only_and_rule_identity_downgrade_cannot_escape_source(self):
        field_only = "---\nname: 規範\nmetadata:\n  type: feedback\nforbidden: [文字]\n---\n"
        fields, _ = memspec.frontmatter_text(field_only)
        self.assertTrue(rule_pipeline.is_rule(fields, "feedback", field_only))
        self.assertTrue(rule_pipeline.admission("", field_only, "feedback", self.target))
        downgrade = "---\nname: 規範\nmetadata:\n  type: project\nauto: true\n---\n純描述\n"
        self.assertTrue(rule_pipeline.admission(self.card(), downgrade, "project", self.target))

    def test_malformed_binding_remains_invalid_without_blocking_write(self):
        before = self.card()
        fields, _ = memspec.frontmatter_text(before)
        document = json.loads(fields['rule_provenance'])
        document['payload_binding'] = 'not-an-object'
        replacement = json.dumps(json.dumps(document, ensure_ascii=False), ensure_ascii=False)
        after = before.replace(next(line for line in before.splitlines() if line.startswith('rule_provenance:')),
                               'rule_provenance: ' + replacement)
        for host in ('claude', 'codex'):
            value = self.write_call(after, host)
            self.assertNotEqual((value or {}).get('hookSpecificOutput', {}).get(
                'permissionDecision'), 'deny')
        self.assertIn('complete interpretation binding must be an object',
                      '\n'.join(rule_pipeline.admission(before, after, 'rule', self.target)))

    def test_superseded_missing_endpoint_cannot_hide_valid_rule(self):
        after = self.card(status="superseded", superseded_by="does-not-exist.md")
        self.assertTrue(rule_pipeline.admission(self.card(), after, "rule", self.target))
        self.target.write_text(after, encoding="utf-8")
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(rules)
        self.assertTrue(errors)

    def test_same_review_valid_supersession_and_invalid_retarget(self):
        before = self.card()
        successor = self.vault / "rule-source-successor.md"
        document = copy.deepcopy(self.document)
        document["supersedes"] = [self.target.name]
        successor.write_text(self.card(document), encoding="utf-8")
        after = self.card(status="superseded", superseded_by=successor.name)
        self.assertEqual(rule_pipeline.admission(before, after, "rule", self.target), [])
        self.target.write_text(after, encoding="utf-8")
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(errors)
        self.assertEqual([row["path"] for row in rules], [successor.name])
        retargeted = after.replace(successor.name, "does-not-exist.md")
        self.assertTrue(rule_pipeline.admission(after, retargeted, "rule", self.target))
        active_with_successor = self.card(superseded_by=successor.name)
        self.assertTrue(rule_pipeline.admission(before, active_with_successor, "rule", self.target))

    def test_explicit_expiry_and_scoped_authorship_reach_both_generated_hosts(self):
        document = copy.deepcopy(self.document)
        document.update(kind="ai-derived", author="synthetic-ai", text="核對原文。")
        self.target.write_text(self.card(document, handoff_rule="true", status="active"), encoding="utf-8")
        for host in ("claude", "codex"):
            block = host_sync.project_rules_text(self.vault, host)
            self.assertIn("synthetic-project", block)
            self.assertIn('"case":"A"', block)
            self.assertIn("AI 整理：synthetic-ai", block)
        document["scope"]["expiry"] = "2000-01-01"
        self.target.write_text(self.card(document, handoff_rule="true", status="active"), encoding="utf-8")
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(errors)
        self.assertFalse(rules)
        self.assertNotIn(document["text"], owner_rules.render([self.vault]))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
