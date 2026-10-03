"""Original record verification and safe rule lifecycle, in an isolated fixture."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import memspec, rule_provenance as provenance


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "owner.jsonl"
        self.sid = "synthetic-session-for-provenance-regression"
        self.quote = "這個專案的 A 案先整理規則，確認原文後再提送。"
        self.assistant = "是否只在這個專案的 A 案整理規則？"
        self.rows = [
            {"type": "session_meta", "payload": {"id": self.sid}},
            {"type": "response_item", "payload": {"type": "message", "id": "assistant-1",
              "role": "assistant", "content": [{"type": "output_text", "text": self.assistant}]}},
            {"type": "response_item", "payload": {"type": "message", "id": "owner-1",
              "role": "user", "content": [{"type": "input_text", "text": self.quote}]}},
        ]
        self.raw = [(json.dumps(row, ensure_ascii=False) + "\n").encode() for row in self.rows]
        self.path.write_bytes(b"".join(self.raw))
        self.document = {"version": 1, "key": "source-test-rule", "kind": "owner-directive",
            "author": "owner", "quote": self.quote, "text": self.quote,
            "sources": [self.source(2)], "scope": {"level": "case", "project": "synthetic-project",
            "case": "A", "actions": ["整理規則"], "conditions": ["確認原文"],
            "expiry": "unspecified", "exceptions": []},
            "scope_basis": {"source": 0, "quote": self.quote, "reviewed_by": "synthetic-reviewer",
            "reviewed_at": "2026-10-03", "rationale": "Synthetic fixture expressly limits the case."},
            "supersedes": []}

    def source(self, index, role="U"):
        return {"path": str(self.path), "byte_offset": sum(len(row) for row in self.raw[:index]),
                "line": index + 1, "record_sha256": hashlib.sha256(self.raw[index]).hexdigest(),
                "session_id": self.sid, "message_id": self.rows[index].get("uuid") or
                self.rows[index].get("payload", {}).get("id"),
                "role": role}

    def fields(self, document=None):
        document = copy.deepcopy(document or self.document)
        return {"rule_provenance": provenance.serialize(document), "decision_key": document["key"],
                "owner_quote": document["quote"], "text": document["text"],
                "decided_by": "owner-explicit" if document["kind"] == "owner-directive" else "ai-autonomous"}

    def verdict(self, document=None, **kwargs):
        return provenance.validate(self.fields(document), self.root / "rule.md", **kwargs)

    def replace_owner(self, text, kind="codex"):
        if kind == "claude":
            row = {"type": "user", "sessionId": self.sid, "uuid": "owner-1",
                   "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        elif kind == "queue":
            row = {"type": "attachment", "sessionId": self.sid, "uuid": "owner-1",
                   "attachment": {"type": "queued_command", "origin": {"kind": "human"}, "prompt": text}}
        else:
            row = {"type": "response_item", "payload": {"type": "message", "id": "owner-1",
                "role": "user", "content": [{"type": "input_text", "text": text}]}}
        self.rows[2] = row
        self.raw[2] = (json.dumps(row, ensure_ascii=False) + "\n").encode()
        self.path.write_bytes(b"".join(self.raw))
        role = "Q" if kind == "queue" else "U"
        self.document["sources"] = [self.source(2, role)]

    def add_owner_sources(self, count):
        for number in range(2, count + 1):
            row = copy.deepcopy(self.rows[2])
            row["payload"]["id"] = "owner-" + str(number)
            self.rows.append(row)
        self.raw = [(json.dumps(row, ensure_ascii=False) + "\n").encode() for row in self.rows]
        self.path.write_bytes(b"".join(self.raw))
        self.document["sources"] = [self.source(index) for index in range(2, len(self.rows))]

    def test_exact_owner_original_both_hosts_and_queue(self):
        for host in ("codex", "claude", "queue"):
            with self.subTest(host=host):
                self.replace_owner(self.quote, host)
                self.assertEqual(self.verdict().status, provenance.VERIFIED)
                result = self.verdict().to_dict()
                self.assertEqual(result["semantic_entailment"], "REVIEW_REQUIRED")
                self.assertFalse(result["action_authorization"])

    def test_fabricated_quote_and_editorial_prefix_are_rejected(self):
        for quote in ("不存在的原話", "只有這一次／已用畢：" + self.quote):
            with self.subTest(quote=quote):
                document = copy.deepcopy(self.document)
                document["quote"] = document["text"] = quote
                self.assertEqual(self.verdict(document).status, provenance.INVALID)

    def test_assistant_cannot_prove_owner_quote(self):
        document = copy.deepcopy(self.document)
        document["sources"] = [self.source(1, "A")]
        document["quote"] = document["text"] = self.assistant
        self.assertEqual(self.verdict(document).status, provenance.INVALID)

    def test_plain_user_echo_cannot_reauthor_an_earlier_assistant_phrase(self):
        self.rows[1]['payload']['content'] = [{'type': 'output_text', 'text': self.quote}]
        self.raw = [(json.dumps(row, ensure_ascii=False) + '\n').encode() for row in self.rows]
        self.path.write_bytes(b''.join(self.raw))
        self.document['sources'] = [self.source(2)]
        result = self.verdict()
        self.assertEqual(result.status, provenance.INVALID)
        self.assertTrue(any('first occurred' in issue for issue in result.issues))

    def test_existing_card_validator_requires_data_for_new_ai_timeout(self):
        document = copy.deepcopy(self.document)
        document.update(kind='ai-derived', author='codex', text='前置檢查超過10分鐘就取消')
        result = self.verdict(document)
        self.assertEqual(result.status, provenance.INVALID)
        self.assertTrue(any('restriction_evidence' in issue for issue in result.issues))

    def test_non_numeric_ai_ban_cannot_bypass_via_card_metadata(self):
        document = copy.deepcopy(self.document)
        document.update(kind='ai-derived', author='codex', text='校準資料不交dot')
        result = self.verdict(document)
        self.assertEqual(result.status, provenance.INVALID)
        self.assertTrue(any('restriction_evidence' in issue for issue in result.issues))

    def test_pasted_assistant_arrow_and_actual_owner_half(self):
        self.replace_owner("所有案都直接提送。《" + self.quote)
        self.assertEqual(self.verdict().status, provenance.VERIFIED)
        document = copy.deepcopy(self.document)
        document["quote"] = document["text"] = "所有案都直接提送。"
        self.assertEqual(self.verdict(document).status, provenance.INVALID)

    def test_injected_host_delegated_and_quoted_content_rejected(self):
        wrappers = (
            "<hook_prompt>" + self.quote + "</hook_prompt>",
            '<codex_internal_context source="goal">' + self.quote + '</codex_internal_context>',
            '<heartbeat>' + self.quote + '</heartbeat>',
            '<environment_context>' + self.quote + '</environment_context>',
            '# AGENTS.md instructions for C:/synthetic\n' + self.quote,
            'Message Type: NEW_TASK\nSender: agent\nPayload:\n' + self.quote,
            '> ' + self.quote,
            '```\n' + self.quote + '\n```',
        )
        for text in wrappers:
            with self.subTest(wrapper=text.splitlines()[0]):
                self.replace_owner(text)
                self.assertEqual(self.verdict().status, provenance.INVALID)

    def test_user_wrapper_with_actual_request_retains_only_request(self):
        self.replace_owner('<browser_context>機器資料</browser_context>\n## My request:\n' + self.quote)
        self.assertEqual(self.verdict().status, provenance.VERIFIED)

    def test_ambient_prefix_preserves_actual_owner_request_both_hosts(self):
        ambient = (
            '<environment_context>machine</environment_context>',
            '<browser_context>machine</browser_context>',
            '<in-app-browser-context tab="synthetic">machine</in-app-browser-context>',
            '<system-reminder>machine</system-reminder>',
            '<pasted_content id="synthetic">machine</pasted_content id="synthetic">',
            '<environment_context>machine</environment_context>\n'
            '<browser_context>machine</browser_context>',
        )
        for host in ("codex", "claude"):
            for wrapper in ambient:
                with self.subTest(host=host, wrapper=wrapper):
                    self.replace_owner(wrapper + '\n## My request:\n' + self.quote, host)
                    self.assertEqual(self.verdict().status, provenance.VERIFIED)

    def test_ambient_content_is_not_owner_and_cannot_hide_hard_wrappers(self):
        hard = (
            '<hook_prompt>automatic</hook_prompt>',
            '<hook_prompt/>',
            '<codex_internal_context source="goal">automatic</codex_internal_context>',
            '<heartbeat>automatic</heartbeat>',
            '<heartbeat/>',
            '<task-notification>automatic</task-notification>',
            '<agent-message>automatic</agent-message>',
            '<cross-session-message>automatic</cross-session-message>',
            '<tool_result>automatic</tool_result>',
            '<function_results>automatic</function_results>',
            '# AGENTS.md instructions for C:/synthetic',
            'Message Type: NEW_TASK\nSender: agent\nPayload:',
        )
        for host in ("codex", "claude"):
            for wrapper in hard:
                with self.subTest(host=host, wrapper=wrapper):
                    self.replace_owner('<environment_context>machine</environment_context>\n'
                                       + wrapper + '\n## My request:\n' + self.quote, host)
                    self.assertEqual(self.verdict().status, provenance.INVALID)
            for wrapper in ("environment_context", "browser_context", "in-app-browser-context"):
                with self.subTest(host=host, ambient=wrapper):
                    self.replace_owner('<' + wrapper + '>' + self.quote + '</' + wrapper + '>\n'
                                       '## My request:\n請核查，不要採用機器區塊裡的引文。', host)
                    self.assertEqual(self.verdict().status, provenance.INVALID)
                    self.replace_owner('<' + wrapper + '>machine\n## My request:\n' + self.quote, host)
                    self.assertEqual(self.verdict().status, provenance.INVALID)
                    self.replace_owner('<' + wrapper + '><' + wrapper + '>machine</' + wrapper + '>\n'
                                       + self.quote + '</' + wrapper + '>', host)
                    self.assertEqual(self.verdict().status, provenance.INVALID)

    def test_seventeen_original_records_have_no_arbitrary_count_limit(self):
        self.add_owner_sources(17)
        result = self.verdict()
        self.assertEqual(result.status, provenance.VERIFIED, result.issues)
        self.assertEqual(len(result.records), 17)
        self.assertEqual(len({source["record_sha256"] for source in result.records}), 17)

    def test_deadline_is_shared_across_original_records_with_next_check(self):
        self.add_owner_sources(17)
        clock = [0.0]
        budgets = []
        original = provenance._original

        def tick():
            instant = clock[0]
            clock[0] += 0.02
            return instant

        def traced_original(source, base, cache, budget):
            budgets.append(budget)
            return original(source, base, cache, budget)

        with patch.object(provenance.time, "monotonic", side_effect=tick), \
                patch.object(provenance, "_original", side_effect=traced_original):
            result = self.verdict(seconds=1.0)
        self.assertEqual(result.status, provenance.UNVERIFIED)
        self.assertGreater(len(result.records), 0)
        self.assertLess(len(result.records), 17)
        self.assertGreater(len(budgets), 1)
        self.assertTrue(all(budget is budgets[0] for budget in budgets))
        self.assertEqual(budgets[0].deadline, 1.0)
        self.assertIn("next check", " ".join(result.issues))
        self.assertLess(clock[0], 1.1)

    def test_input_read_budget_is_shared_and_exhaustion_is_unverified(self):
        self.add_owner_sources(17)
        first_reads = len(self.path.read_bytes()) + 1 + len(self.raw[2])
        limit = first_reads + len(self.path.read_bytes()) - 1
        result = self.verdict(max_bytes=limit)
        self.assertEqual(result.status, provenance.UNVERIFIED)
        self.assertEqual(len(result.records), 1)
        self.assertIn("next check", " ".join(result.issues))
        result = self.verdict(seconds=0)
        self.assertEqual(result.status, provenance.UNVERIFIED)
        self.assertEqual(result.records, [])
        self.assertIn("next check", " ".join(result.issues))

    def test_inline_assistant_quotation_is_not_a_new_owner_directive(self):
        for text in ('你剛說「' + self.quote + '」，這規則錯了。',
                     '你剛說 "' + self.quote + '"，這規則錯了。'):
            with self.subTest(text=text):
                self.replace_owner(text)
                self.assertEqual(self.verdict().status, provenance.INVALID)

    def test_summaries_meta_sidechain_and_tool_result_rejected(self):
        for flag in ("isCompactSummary", "isMeta", "isSidechain"):
            with self.subTest(flag=flag):
                self.replace_owner(self.quote)
                self.rows[2][flag] = True
                self.raw[2] = (json.dumps(self.rows[2], ensure_ascii=False) + "\n").encode()
                self.path.write_bytes(b"".join(self.raw))
                self.document["sources"] = [self.source(2)]
                self.assertEqual(self.verdict().status, provenance.INVALID)
        self.replace_owner(self.quote, "claude")
        self.rows[2]["message"]["content"].append({"type": "tool_result", "content": self.quote})
        self.raw[2] = (json.dumps(self.rows[2], ensure_ascii=False) + "\n").encode()
        self.path.write_bytes(b"".join(self.raw))
        self.document["sources"] = [self.source(2)]
        self.assertEqual(self.verdict().status, provenance.INVALID)

    def test_ai_derivation_is_labelled_and_cannot_claim_owner(self):
        document = copy.deepcopy(self.document)
        document.update(kind="ai-derived", author="codex", text="AI 建議先核來源再整理。")
        self.assertEqual(self.verdict(document).status, provenance.VERIFIED)
        output = provenance.render_entry(self.fields(document))
        self.assertIn("不是 Owner 逐字裁定", output)
        self.assertIn(document["text"], output)
        self.assertIn(self.quote, output)
        document["author"] = "owner"
        self.assertEqual(self.verdict(document).status, provenance.INVALID)

    def test_formal_owner_rewrite_and_different_generated_text_rejected(self):
        document = copy.deepcopy(self.document)
        document["text"] = "Owner 永遠限制所有案送件。"
        self.assertEqual(self.verdict(document).status, provenance.INVALID)
        fields = self.fields()
        fields["text"] = "另加一道門檻"
        self.assertEqual(provenance.validate(fields).status, provenance.INVALID)

    def test_each_scope_dimension_must_be_explicit(self):
        for name in provenance.SCOPE_KEYS:
            with self.subTest(field=name):
                document = copy.deepcopy(self.document)
                del document["scope"][name]
                self.assertEqual(self.verdict(document).status, provenance.INVALID)
        self.assertEqual(self.verdict().document["scope"]["expiry"], "unspecified")

    def test_short_ack_needs_context_without_inventing_a_case_only_limit(self):
        self.replace_owner("我同意")
        document = copy.deepcopy(self.document)
        document["quote"] = document["text"] = "我同意"
        document["scope_basis"]["quote"] = "我同意"
        self.assertEqual(self.verdict(document).status, provenance.INVALID)
        document["sources"].append(self.source(1, "A"))
        document["scope_basis"]["context_source"] = 1
        self.assertEqual(self.verdict(document).status, provenance.INVALID)
        document["scope_basis"]["context_quote"] = self.assistant
        self.assertEqual(self.verdict(document).status, provenance.VERIFIED)
        # Provenance validation does not invent a case-only authorization rule.
        # Whether the proposal supports this declared scope is reviewed separately.
        document["scope"].update(level="global", project=None, case=None)
        self.assertEqual(self.verdict(document).status, provenance.VERIFIED)

    def test_short_ack_cannot_use_future_or_another_sessions_proposal(self):
        self.replace_owner("我同意")
        document = copy.deepcopy(self.document)
        document["quote"] = document["text"] = "我同意"
        document["scope_basis"]["quote"] = "我同意"
        document["scope_basis"]["context_source"] = 1
        document["scope_basis"]["context_quote"] = self.assistant
        # Put the assistant proposal after the user's assent in the original.
        self.rows[1], self.rows[2] = self.rows[2], self.rows[1]
        self.raw[1], self.raw[2] = self.raw[2], self.raw[1]
        self.path.write_bytes(b"".join(self.raw))
        document["sources"] = [self.source(1, "U"), self.source(2, "A")]
        self.assertEqual(self.verdict(document).status, provenance.INVALID)

    def test_missing_original_is_unverified_not_guessed(self):
        self.path.unlink()
        result = self.verdict()
        self.assertEqual(result.status, provenance.UNVERIFIED)
        self.assertEqual(result.records, [])

    def test_legacy_missing_envelope_and_malformed_are_distinct(self):
        self.assertEqual(provenance.validate({"owner_quote": self.quote}).status, provenance.UNVERIFIED)
        self.assertEqual(provenance.validate({"rule_provenance": "{}"}).status, provenance.INVALID)
        self.assertEqual(provenance.validate({"rule_provenance": "[1]"}).status, provenance.INVALID)

    def test_record_hash_role_session_and_offset_fail_closed(self):
        changes = ({"record_sha256": "0" * 64}, {"role": "A"}, {"session_id": "different-session"},
                   {"byte_offset": 1}, {"message_id": "not-the-message"})
        for change in changes:
            with self.subTest(change=change):
                document = copy.deepcopy(self.document)
                document["sources"][0].update(change)
                self.assertEqual(self.verdict(document).status, provenance.INVALID)

    def test_cache_rechecks_even_when_size_and_mtime_preserved(self):
        cache = {}
        self.assertEqual(self.verdict(cache=cache).status, provenance.VERIFIED)
        stamp = self.path.stat()
        raw = self.path.read_bytes()
        self.path.write_bytes(raw.replace("確認原文".encode(), "未核原文".encode()))
        os.utime(self.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(self.path.stat().st_size, stamp.st_size)
        self.assertEqual(self.verdict(cache=cache).status, provenance.INVALID)

    def test_zip_exact_member_and_direct_line_locator(self):
        document = copy.deepcopy(self.document)
        del document["sources"][0]["byte_offset"]
        self.assertEqual(self.verdict(document).status, provenance.VERIFIED)
        archive = self.root / "cold.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("original/owner.jsonl", self.path.read_bytes())
        source = document["sources"][0]
        source.pop("path")
        source.update(archive=str(archive), member="original/owner.jsonl")
        self.assertEqual(self.verdict(document).status, provenance.VERIFIED)
        source["member"] = "missing.jsonl"
        self.assertEqual(self.verdict(document).status, provenance.INVALID)

    def test_supersession_preserves_key_and_case_with_explicit_edge(self):
        old = self.fields()
        document = copy.deepcopy(self.document)
        document["supersedes"] = ["old.md"]
        new = self.fields(document)
        self.assertEqual(provenance.validate_supersession(old, new, self.root / "old.md").status,
                         provenance.VERIFIED)
        for field, value in (("key", "different-key"), ("case", "B"), ("project", "other-project")):
            with self.subTest(field=field):
                changed = copy.deepcopy(document)
                if field == "key":
                    changed[field] = value
                else:
                    changed["scope"][field] = value
                self.assertEqual(provenance.validate_supersession(old, self.fields(changed),
                                 self.root / "old.md").status, provenance.INVALID)
        document["supersedes"] = []
        self.assertEqual(provenance.validate_supersession(old, self.fields(document),
                         self.root / "old.md").status, provenance.INVALID)

    def test_diagnostics_never_copy_unrelated_source_body(self):
        self.replace_owner(self.quote + "\nUNRELATED_PRIVATE_SYNTHETIC_DATA")
        serialized = json.dumps(self.verdict().to_dict(), ensure_ascii=False)
        self.assertNotIn("UNRELATED_PRIVATE_SYNTHETIC_DATA", serialized)
        self.assertNotIn(self.quote, serialized)

    def test_flat_frontmatter_serialization_roundtrip(self):
        scalar = provenance.serialize(self.document).replace("'", "''")
        text = "---\nrule_provenance: '" + scalar + "'\n---\nbody\n"
        fields, problem = memspec.frontmatter_text(text)
        self.assertIsNone(problem)
        self.assertEqual(provenance.parse(fields), self.document)
        self.assertEqual(provenance.validate(fields).status, provenance.VERIFIED)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main(verbosity=2)
