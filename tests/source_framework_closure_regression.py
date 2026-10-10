"""Complete original-source fixtures for reciprocal supersession and core display."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import core_gen, host_sync, memspec, rule_pipeline, rule_provenance


class FrameworkClosure(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-framework-closure-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.documents = {}

    def card(self, name, *, host="codex", kind="owner-directive", case="A", key="closure-rule",
             status="active", supersedes=(), superseded_by=None, prior=None, write=True):
        path = self.vault / (name + ".md")
        quote = f"在合成專案 synthetic-project 案件 {case}，所有動作採用 {name} 測試規則。"
        if supersedes:
            quote += "本卡取代 " + "、".join(supersedes) + "。"
        if prior:
            quote += f"原案件 {prior['scope']['case']} 由新案件 {case} 明確取代。"
        sid, mid = "synthetic-closure-" + name, "owner-" + name
        if host == "claude":
            row = {"type": "user", "sessionId": sid, "uuid": mid,
                   "message": {"role": "user", "content": [{"type": "text", "text": quote}]}}
        else:
            row = {"type": "response_item", "payload": {"type": "message", "id": mid,
                   "role": "user", "content": [{"type": "input_text", "text": quote}]}}
        header = (json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n").encode()
        raw = (json.dumps(row, ensure_ascii=False) + "\n").encode()
        source = self.root / (name + "-source.jsonl")
        source.write_bytes(header + raw)
        document = {"version": 1, "key": key, "kind": kind,
            "author": "owner" if kind == "owner-directive" else "synthetic-closure-ai",
            "quote": quote, "text": quote if kind == "owner-directive" else "這是 AI 整理的測試規則。",
            "sources": [{"path": str(source), "byte_offset": len(header), "line": 2,
                "record_sha256": hashlib.sha256(raw).hexdigest(), "session_id": sid,
                "message_id": mid, "role": "U"}],
            "scope": {"level": "case", "project": "synthetic-project", "case": case,
                "actions": ["*"], "conditions": [], "expiry": "unspecified", "exceptions": []},
            "scope_basis": {"source": 0, "quote": quote, "reviewed_by": "synthetic-closure-reviewer",
                "reviewed_at": "2026-10-03", "rationale": "This original expressly states the synthetic case."},
            "supersedes": list(supersedes)}
        if prior:
            document["supersession_basis"] = {
                "prior_key": prior["key"], "prior_scope": copy.deepcopy(prior["scope"]),
                **{name: document["scope_basis"][name] for name in
                   ("source", "quote", "reviewed_by", "reviewed_at", "rationale")}}
        fields = {"name": name, "description": "Synthetic source closure fixture.", "metadata": {"type": "rule"},
            "layer": "resident", "section": "evidence", "order": "1", "status": status,
            "approved_by": "synthetic-closure-reviewer", "approved_at": "2026-10-03",
            "decided_by": "owner-explicit", "owner_quote": quote,
            "text": document["text"], "aliases": "[closure]"}
        if superseded_by is not None:
            fields["superseded_by"] = superseded_by
        text = "---\n" + "\n".join("metadata:\n  type: rule" if name == "metadata"
                                     else f"{name}: {json.dumps(value, ensure_ascii=False)}"
                                     for name, value in fields.items()) + "\n---\nSynthetic full body.\n"
        document = rule_pipeline.bind_payload(document, text, reviewer="synthetic-closure-reviewer")
        text = text.replace("---\n", "---\nrule_provenance: "
            + json.dumps(json.dumps(document, ensure_ascii=False), ensure_ascii=False) + "\n", 1)
        self.documents[name] = document
        if write:
            path.write_text(text, encoding="utf-8")
        return path, text

    def issues(self, path, text=None, **kwargs):
        text = text if text is not None else path.read_text(encoding="utf-8")
        fields, problem = memspec.frontmatter_text(text)
        self.assertIsNone(problem)
        return rule_pipeline.supersession_issues(fields, path, card_text=text, **kwargs)

    def test_active_successor_rejects_missing_predecessor_full_envelope(self):
        path, _ = self.card("new", supersedes=["absent-prior.md"])
        result = rule_pipeline.inspect_card(memspec.frontmatter_fields(path)[0], path)
        self.assertEqual(result.status, "INVALID")
        self.assertTrue(any("endpoint" in issue for issue in result.issues))

    def test_active_predecessor_cannot_be_silently_replaced(self):
        self.card("old")
        path, _ = self.card("new", supersedes=["old.md"])
        self.assertIn("still active", " ".join(self.issues(path)))

    def test_reciprocal_chain_has_real_sources_and_binding_both_hosts(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                old, _ = self.card("old", host=host, status="superseded", superseded_by="new.md")
                new, _ = self.card("new", host=host, supersedes=["old.md"])
                self.assertEqual(self.issues(old), [])
                self.assertEqual(self.issues(new), [])
                self.assertEqual(rule_pipeline.inspect_card(memspec.frontmatter_fields(new)[0], new).status,
                                 "VERIFIED")

    def test_predecessor_payload_and_original_source_are_checked(self):
        for invalid in ("body", "source"):
            with self.subTest(invalid=invalid):
                old, text = self.card("old", status="superseded", superseded_by="new.md")
                new, _ = self.card("new", supersedes=["old.md"])
                if invalid == "body":
                    old.write_text(text + "新增沒有原源的限制。\n", encoding="utf-8")
                    self.assertIn("bound", " ".join(self.issues(new)))
                else:
                    source = Path(self.documents["old"]["sources"][0]["path"])
                    source.write_bytes(source.read_bytes().replace(b"old", b"bad"))
                    self.assertIn("provenance", " ".join(self.issues(new)))

    def test_reciprocal_identity_mismatch_and_self_edges_rejected(self):
        self.card("old", status="superseded", superseded_by="different.md")
        new, _ = self.card("new", supersedes=["old.md"])
        self.assertIn("exact claimed successor", " ".join(self.issues(new)))
        new, _ = self.card("self", status="superseded", supersedes=["self.md"], superseded_by="self.md")
        self.assertIn("self edge", " ".join(self.issues(new)))

    def test_reciprocal_cycle_rejected_without_recursion(self):
        a, _ = self.card("a", status="superseded", supersedes=["b.md"], superseded_by="b.md")
        self.card("b", status="superseded", supersedes=["a.md"], superseded_by="a.md")
        self.assertIn("cycle", " ".join(self.issues(a)))
        self.assertIn("budget", " ".join(self.issues(a, seconds=0)))
        with rule_provenance.unbounded_verification():
            unbounded = " ".join(self.issues(a, seconds=0))
        self.assertIn("cycle", unbounded)
        self.assertNotIn("budget", unbounded)

    def test_explicit_owner_scope_change_uses_existing_source_review(self):
        self.card("old", status="superseded", superseded_by="new.md")
        new, _ = self.card("new", case="B", supersedes=["old.md"], prior=self.documents["old"])
        self.assertEqual(self.issues(new), [])
        new, _ = self.card("new", case="B", supersedes=["old.md"])
        self.assertIn("scope or key change", " ".join(self.issues(new)))

    def test_prospective_root_requires_exact_body_and_preserves_relative_identity(self):
        other = self.root / "prior-vault"
        other.mkdir()
        self.vault, current = other, self.vault
        self.card("old", status="superseded", superseded_by="../vault/new.md")
        self.vault = current
        new, text = self.card("new", supersedes=["../prior-vault/old.md"], write=False)
        self.assertFalse(new.exists())
        self.assertEqual(self.issues(new, text), [])

    def test_core_uses_shared_formatter_quote_edges_and_pointer_both_hosts(self):
        self.card("old", status="superseded", superseded_by="new.md")
        new, _ = self.card("new", kind="ai-derived", supersedes=["old.md"])
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertEqual(errors, [])
        self.assertEqual(len(rules), 1)
        expected = rule_pipeline.render_projection(rules[0]["projection"], card_path=new)
        text = core_gen.assemble(rules)
        self.assertIn(expected, text)
        self.assertIn(self.documents["new"]["quote"], text)
        self.assertIn("synthetic-closure-ai", text)
        self.assertIn("適用範圍：", text)
        self.assertIn("取代關係：old.md", text)
        self.assertIn("來源卡：" + str(new), text)
        for host in ("codex", "claude"):
            self.assertEqual(host_sync.project_rules_text(self.vault, host),
                             core_gen.host_view(text, host).rstrip())

    def test_legacy_legend_keeps_original_and_actual_cap(self):
        path = self.vault / "legacy.md"
        original = "Legacy test MUST retain this exact original directive."
        path.write_text("---\nname: legacy\ndescription: legacy\nmetadata:\n  type: rule\nlayer: resident\n"
                        "section: evidence\norder: 1\napproved_by: synthetic-reviewer\n"
                        "approved_at: 2026-10-03\ntext: " + original + "\n---\nbody\n", encoding="utf-8")
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertEqual(errors, [])
        text = core_gen.assemble(rules)
        self.assertIn("來源未核的舊卡：本生成包共 1 條", text)
        self.assertIn(str(core_gen.pack_path(self.vault)), text)
        self.assertIn("- " + original, text)
        out = self.root / "never-written-core.md"
        result = core_gen.generate([self.vault], out, dry_run=True, config={"core_cap_bytes": 1})
        # The core size cap is a reminder since 6f6fce1: the dry run keeps its own status and
        # reports the overflow instead of refusing.
        self.assertEqual(result["status"], core_gen.STATUS_DRY_RUN)
        self.assertTrue(result["over_cap"])
        self.assertEqual(result["cap"], 1)
        self.assertTrue(result["offenders"])
        self.assertFalse(out.exists())
        self.assertEqual(result["measured"], len(text.encode("utf-8")))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main(verbosity=2)
