"""AI 改壞的規則卡由 Epitype 自己重綁，改到 owner 原話或規則欄位的不綁。

改卡掛鉤（PostToolUse Write／Edit）與夢的夜間掃描走同一條判斷（rule_repair）。
"""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "adapters" / "claude"),
                str(Path(__file__).resolve().parent)]
import contextlib
import hashlib
from datetime import date, timedelta
import io
import json
import os
import runpy
import subprocess
import time
import unittest
from unittest.mock import patch

import action_guard_regression as guard
from epitype import dream, memspec, rule_pipeline, rule_provenance, rule_repair
import pretooluse_gate as gate

QUOTE = "以後不准刪測試"
BODY = "改測試要先說明理由。\n"
OLD_MTIME = 1_700_000_000


class RuleSelfRepair(unittest.TestCase):
    def setUp(self):
        guard.ActionGuardRegression.setUp(self)
        self.target = self.vault / "decision-no-delete-tests.md"

    def write_card(self, path=None, *, status="active", extra="", parts=True, body=BODY):
        """A reviewed owner directive, written the way a published card is.
        parts=False is a card reviewed before per-field digests existed."""
        path = path or self.target
        document = {
            "version": rule_provenance.VERSION, "key": "no-delete-tests", "kind": "owner-directive",
            "author": "owner", "text": QUOTE, "quote": QUOTE,
            "scope": {"level": "global", "project": None, "case": None, "actions": ["*"],
                      "conditions": [], "expiry": "unspecified", "exceptions": []},
            "supersedes": [],
            "current_review": {"at": "2026-11-01T00:00:00+00:00", "by": "lead", "basis": "審過整段解讀"}}
        text = ("---\nname: 不准刪測試\ndescription: 測試不能刪\ndecision_key: no-delete-tests\n"
                f"status: {status}\n{extra}decided_by: owner-explicit\nowner_quote: {QUOTE}\n"
                "forbidden:\n  - 刪測試\n---\n" + body)
        document = rule_pipeline.bind_payload(document, text, reviewer="lead",
                                              reviewed_at="2026-11-01T00:00:00+00:00")
        if not parts:
            document["payload_binding"].pop("parts", None)
            document["payload_binding"].pop("parts_version", None)
        line = rule_provenance.FIELD + ": " + json.dumps(rule_provenance.serialize(document), ensure_ascii=False)
        text = text.replace("---\n", "---\n" + line + "\n", 1)
        path.write_text(text, encoding="utf-8")
        if status == "active":
            self.assertEqual(self.status(path), rule_provenance.REVIEWED)
        return text

    def status(self, path=None):
        path = path or self.target
        text = path.read_text(encoding="utf-8-sig")
        fields, _problem = memspec.frontmatter_text(text)
        return rule_pipeline.inspect_card(fields, path, card_text=text).status

    def document(self, path=None):
        fields, _problem = memspec.frontmatter_text((path or self.target).read_text(encoding="utf-8-sig"))
        return rule_provenance.parse(fields)

    def ai_edit(self, old, new, path=None):
        """An AI Edit that already landed on disk, then the host's PostToolUse call."""
        path = path or self.target
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1)
        path.write_text(text.replace(old, new), encoding="utf-8")
        os.utime(path, (OLD_MTIME, OLD_MTIME))
        return self.post({"tool_name": "Edit", "tool_input": {
            "file_path": str(path), "old_string": old, "new_string": new}})

    def post(self, call):
        event = {"hook_event_name": "PostToolUse", "session_id": "self-repair-session",
                 "tool_use_id": "call-1", **call}
        output = io.StringIO()
        with patch.object(gate, "read_event", return_value=event), \
                patch.object(gate, "_STARTED_AT", time.monotonic()), \
                contextlib.redirect_stdout(output):
            self.assertEqual(gate.main(), 0)
        printed = output.getvalue().strip()
        return json.loads(printed)["hookSpecificOutput"]["additionalContext"] if printed else ""

    def test_body_only_edit_is_rebound_to_reviewed_with_auto_review_record(self):
        self.write_card()
        note = self.ai_edit("改測試要先說明理由。", "改測試要先說明理由，並附上證據。")
        self.assertEqual(self.status(), rule_provenance.REVIEWED)
        self.assertIn("已自動重綁", note)
        review = self.document()["current_review"]
        self.assertEqual(review["by"], memspec.RULE_SELF_REPAIR_REVIEWER)
        self.assertEqual(review["kind"], "auto-rebind-after-ai-edit")
        self.assertEqual(review["changed"], [rule_pipeline.PART_BODY])
        self.assertIs(review["quote_and_rule_fields_unchanged"], True)
        self.assertEqual(review["session"], "self-repair-session")
        self.assertEqual(review["prior"]["by"], "lead")
        self.assertEqual(self.document()["payload_binding"]["reviewed_by"], memspec.RULE_SELF_REPAIR_REVIEWER)

    def test_rebind_keeps_card_mtime(self):
        self.write_card()
        self.ai_edit("改測試要先說明理由。", "改測試前要先說明理由。")
        self.assertEqual(self.status(), rule_provenance.REVIEWED)
        self.assertEqual(self.target.stat().st_mtime, OLD_MTIME)

    def test_card_reviewed_before_field_digests_is_rebound_from_verified_pre_edit_text(self):
        self.write_card(parts=False)
        note = self.ai_edit("改測試要先說明理由。", "改測試要先說明理由（例外要有證據）。")
        self.assertEqual(self.status(), rule_provenance.REVIEWED, note)
        self.assertIn("parts", self.document()["payload_binding"])

    def test_rebind_keeps_bom_and_crlf_and_rewrites_only_the_provenance_line(self):
        text = self.write_card()
        self.target.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))
        raw = self.target.read_bytes()
        self.target.write_bytes(raw.replace("改測試要先說明理由。".encode(), "改測試要先講理由。".encode()))
        edited = self.target.read_bytes()
        self.post({"tool_name": "Edit", "tool_input": {
            "file_path": str(self.target), "old_string": "改測試要先說明理由。", "new_string": "改測試要先講理由。"}})
        self.assertEqual(self.status(), rule_provenance.REVIEWED)
        after = self.target.read_bytes()
        self.assertTrue(after.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(after.count(b"\r\n"), after.count(b"\n"))
        changed = [pair for pair in zip(edited.split(b"\r\n"), after.split(b"\r\n")) if pair[0] != pair[1]]
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0][1].startswith(rule_provenance.FIELD.encode()))

    def test_write_on_old_review_is_rebound_from_host_original_file(self):
        text = self.write_card(parts=False)
        changed = text.replace("改測試要先說明理由。", "改測試要先說明。")
        self.target.write_text(changed, encoding="utf-8")
        note = self.post({"tool_name": "Write", "tool_input": {"file_path": str(self.target), "content": changed},
                          "tool_response": {"type": "update", "originalFile": text}})
        self.assertEqual(self.status(), rule_provenance.REVIEWED, note)

    def test_write_without_pre_edit_text_on_old_review_is_not_guessed(self):
        text = self.write_card(parts=False)
        changed = text.replace("改測試要先說明理由。", "改測試要先說明。")
        self.target.write_text(changed, encoding="utf-8")
        note = self.post({"tool_name": "Write", "tool_input": {"file_path": str(self.target), "content": changed}})
        self.assertEqual(self.status(), rule_provenance.INVALID)
        self.assertIn("判斷不出這次改了哪些欄位", note)
        self.assertEqual(self.target.read_text(encoding="utf-8"), changed)

    def test_owner_quote_edit_stays_invalid_and_notice_names_the_field(self):
        self.write_card()
        note = self.ai_edit(f"owner_quote: {QUOTE}", "owner_quote: 以後盡量不要刪測試")
        self.assertEqual(self.status(), rule_provenance.INVALID)
        self.assertIn("owner_quote（owner 原話）", note)
        self.assertIn("不會自動重綁", note)
        self.assertIn("還原", note)
        self.assertEqual(self.document()["current_review"]["by"], "lead")

    def test_quote_rewritten_consistently_in_card_and_provenance_is_still_refused(self):
        text = self.write_card()
        document = self.document()
        document["quote"] = document["text"] = "以後盡量不要刪測試"
        line = rule_provenance.FIELD + ": " + json.dumps(rule_provenance.serialize(document), ensure_ascii=False)
        edited = "\n".join(line if row.startswith(rule_provenance.FIELD + ":") else row
                           for row in text.split("\n"))
        edited = edited.replace(f"owner_quote: {QUOTE}", "owner_quote: 以後盡量不要刪測試")
        self.target.write_text(edited, encoding="utf-8")
        note = self.post({"tool_name": "Write", "tool_input": {"file_path": str(self.target), "content": edited}})
        self.assertEqual(self.status(), rule_provenance.INVALID)
        self.assertIn("owner_quote（owner 原話）", note)
        self.assertIn("rule_provenance 內容", note)

    def test_forbidden_and_quoted_body_edits_are_refused(self):
        self.write_card()
        note = self.ai_edit("  - 刪測試", "  - 刪除全部測試")
        self.assertEqual(self.status(), rule_provenance.INVALID)
        self.assertIn("forbidden", note)
        self.write_card(body="owner 說「測試是證據」。\n")
        note = self.ai_edit("「測試是證據」", "「測試大概是證據」")
        self.assertEqual(self.status(), rule_provenance.INVALID)
        self.assertIn("引號引文", note)

    def test_superseded_card_is_left_untouched(self):
        text = self.write_card(status="superseded", extra="superseded_by: decision-new.md\n")
        edited = text.replace("改測試要先說明理由。", "改測試要先說明。")
        self.target.write_text(edited, encoding="utf-8")
        os.utime(self.target, (OLD_MTIME, OLD_MTIME))
        note = self.post({"tool_name": "Edit", "tool_input": {
            "file_path": str(self.target), "old_string": "改測試要先說明理由。", "new_string": "改測試要先說明。"}})
        self.assertEqual(note, "")
        self.assertEqual(self.target.read_text(encoding="utf-8"), edited)
        self.assertEqual(self.target.stat().st_mtime, OLD_MTIME)
        swept = dream._rule_self_repair(self.vault, True)
        self.assertEqual((swept["rebound"], swept["unrepaired"], swept["skipped"]), ([], [], 1))
        self.assertEqual(self.target.read_text(encoding="utf-8"), edited)

    def test_dream_rebinds_edits_made_outside_hooks_and_lists_only_unsafe_cards(self):
        safe = self.vault / "decision-safe.md"
        unsafe = self.vault / "decision-unsafe.md"
        for path in (safe, unsafe):
            text = self.write_card(path)
        safe.write_text(safe.read_text(encoding="utf-8").replace("改測試要先說明理由。", "改測試要先講理由。"),
                        encoding="utf-8")
        unsafe.write_text(unsafe.read_text(encoding="utf-8").replace(f"owner_quote: {QUOTE}", "owner_quote: 改了"),
                          encoding="utf-8")
        for path in (safe, unsafe):
            os.utime(path, (OLD_MTIME, OLD_MTIME))
        today = date(2026, 11, 13)
        context = {"apply_fixes": False}
        dry = dream._section_card_lint([self.vault], today, today - timedelta(days=30), {}, context)
        self.assertEqual(dry["counts"]["rules_rebindable"], 1)
        self.assertEqual(self.status(safe), rule_provenance.INVALID)  # 不套用就不寫
        section = dream._section_card_lint([self.vault], today, today - timedelta(days=30), {},
                                           {"apply_fixes": True})
        self.assertEqual(self.status(safe), rule_provenance.REVIEWED)
        self.assertEqual(safe.stat().st_mtime, OLD_MTIME)
        self.assertEqual(self.status(unsafe), rule_provenance.INVALID)
        self.assertEqual(section["counts"]["rules_rebound"], 1)
        review = dream.review_list({"sections": [{"id": 2, **section}]})
        listed = [item for item in review["items"] if item["group"] == memspec.RULE_SELF_REPAIR_DREAM_GROUP]
        self.assertEqual([Path(item["path"]).name for item in listed], [unsafe.name])
        self.assertIn("owner_quote", listed[0]["reason"])
        self.assertFalse(any(item["group"] == "卡片型別 FAIL" and Path(item["path"]).name == unsafe.name
                             for item in review["items"]))
        self.assertEqual(dream.review_list({"sections": [{"id": 2, **section}]})["total"], review["total"])

    def test_external_provenance_is_listed_only_when_broken_and_never_rewritten(self):
        text = self.write_card()
        document = self.document()
        source = self.root / "external-provenance.json"
        source.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        reference = json.dumps({"ref": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        line = rule_provenance.FIELD + ": " + json.dumps(reference)
        text = "\n".join(line if row.startswith(rule_provenance.FIELD + ":") else row for row in text.split("\n"))
        self.target.write_text(text, encoding="utf-8")
        self.assertEqual(self.status(), rule_provenance.REVIEWED)
        self.assertEqual(dream._rule_self_repair(self.vault, True)["unrepaired"], [])
        edited = text.replace("改測試要先說明理由。", "改測試要先講理由。")
        self.target.write_text(edited, encoding="utf-8")
        swept = dream._rule_self_repair(self.vault, True)
        self.assertEqual([item["path"] for item in swept["unrepaired"]], [self.target.name])
        self.assertIn("外部檔", swept["unrepaired"][0]["reason"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), edited)

    def test_codex_generated_memory_files_are_not_swept(self):
        codex = self.root / "home" / ".codex" / "memories"
        (codex / "rollout_summaries").mkdir(parents=True)
        generated = codex / "rollout_summaries" / "r.md"
        text = self.write_card(generated)
        generated.write_text(text.replace("改測試要先說明理由。", "改了。"), encoding="utf-8")
        before = generated.read_bytes()
        swept = dream._rule_self_repair(codex, True)
        self.assertEqual((swept["rebound"], swept["unrepaired"]), ([], []))
        self.assertEqual(generated.read_bytes(), before)


    def shim(self):
        """這個測試庫的 PostToolUse 墊片，照安裝時的樣子產生，指向這份 repo。"""
        from install import graft
        repo = Path(__file__).resolve().parents[1]
        epitype_home = self.root / "home" / ".epitype"
        (epitype_home / "hooks").mkdir(parents=True, exist_ok=True)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        (epitype_home / "config.json").write_text(json.dumps(dict(config, repo_root=str(repo))), encoding="utf-8")
        shim = epitype_home / "hooks" / "pretooluse.py"
        shim.write_bytes(graft._shim_payloads(repo)["pretooluse.py"])
        return shim

    def test_hook_fast_exit_never_skips_a_card_the_gate_would_check(self):
        fast = runpy.run_path(str(self.shim()), run_name="shim_fixture")["_post_write_off_vault"]
        config = gate.load_config(time.monotonic())
        targets = [str(self.target), "vault/" + self.target.name, str(self.vault / "nested" / "c.md"),
                   str(self.vault / "_ledger" / "c.md"), str(self.vault / ".git" / "c.md"),
                   str(self.vault / "MEMORY.md"), str(self.vault / "c.txt"), str(self.root / "c.md"),
                   str(self.root / "home" / ".claude" / "projects" / "C--w" / "memory" / "c.md")]
        skipped = []
        for raw in targets:
            event = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "cwd": str(self.root),
                     "tool_input": {"file_path": raw}}
            target = gate._write_target(event["tool_input"], event["cwd"])
            card = gate._vault_card_path(target, gate.resolve_vaults(config, event))
            if fast(json.dumps(event)):
                skipped.append(raw)
                self.assertIsNone(card, raw)
        self.assertEqual(len(skipped), 5, skipped)
        self.assertNotIn(str(self.target), skipped)

    def test_card_edit_through_the_installed_shim_is_rebound(self):
        shim = self.shim()
        self.write_card()
        old, new = "改測試要先說明理由。", "改測試要先說明理由，並附上證據。"
        self.target.write_text(self.target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        os.utime(self.target, (OLD_MTIME, OLD_MTIME))
        event = {"hook_event_name": "PostToolUse", "session_id": "shim-session", "cwd": str(self.root),
                 "tool_name": "Edit", "tool_input": {"file_path": str(self.target), "old_string": old,
                                                     "new_string": new}}
        done = subprocess.run([sys.executable, str(shim)], input=json.dumps(event).encode("utf-8"),
                              capture_output=True, env=dict(os.environ), timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        note = json.loads(done.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("已自動重綁", note)
        self.assertEqual(self.status(), rule_provenance.REVIEWED)
        self.assertEqual(self.target.stat().st_mtime, OLD_MTIME)

    def pre_edit(self, old, new, tool="Edit"):
        """寫卡前（PreToolUse）閘門給這次 Edit 的提示；Edit 還沒落盤。"""
        value = gate._handle({"hook_event_name": "PreToolUse", "tool_name": tool, "session_id": "pre-" + tool + new,
                              "tool_input": {"file_path": str(self.target), "old_string": old, "new_string": new}},
                             time.monotonic(), [])
        return json.dumps(value or {}, ensure_ascii=False)

    def test_pre_edit_warning_is_silent_when_the_edit_will_be_rebound(self):
        self.write_card()
        advice = self.pre_edit("改測試要先說明理由。", "改測試要先說明理由，並附上證據。")
        self.assertNotIn("INVALID", advice)
        # 這個工具寫完沒有改卡掛鉤，綁定會失效到夜間的夢才修，照舊先講。
        advice = self.pre_edit("改測試要先說明理由。", "改測試要先說明理由，並附上證據。", tool="edit_file")
        self.assertIn(rule_pipeline.PAYLOAD_UNBOUND_ISSUE, advice)

    def test_pre_edit_warning_names_rule_fields_that_will_not_be_rebound(self):
        self.write_card()
        advice = self.pre_edit("  - 刪測試\n", "  - 刪除測試\n")
        self.assertIn("寫完不會自動重綁", advice)
        self.assertIn("forbidden", advice)
        self.assertNotIn("bind_payload", advice)

    def test_ai_facing_notices_never_tell_the_ai_to_rebind_by_itself(self):
        for notice in (memspec.RULE_DROP_NOTICE, memspec.RULE_SELF_REPAIR_UNSAFE_NOTICE,
                       memspec.RULE_SELF_REPAIR_UNKNOWN_NOTICE, memspec.RULE_SELF_REPAIR_WRITE_UNSAFE):
            self.assertNotIn("bind_payload", notice)
            self.assertIn("交主責", notice)
        self.assertLessEqual(len(memspec.RULE_DROP_NOTICE.format(card="", why="")),
                             len("Epitype：規則卡  已停用（）；改字後請用 rule_pipeline.bind_payload 重綁，或還原改前備份。"))

    def test_dream_backfills_digests_without_changing_status_review_or_mtime(self):
        self.write_card(parts=False)
        os.utime(self.target, (OLD_MTIME, OLD_MTIME))
        before_bytes = self.target.read_bytes()
        before_status = self.status()
        before = self.document()
        dry = rule_repair.sweep(self.vault, apply=False)
        self.assertEqual(dry["backfillable"], [self.target.name])
        self.assertEqual(self.target.read_bytes(), before_bytes)
        done = rule_repair.sweep(self.vault, apply=True)
        self.assertEqual((done["backfilled"], done["rebound"], done["unrepaired"]), ([self.target.name], [], []))
        self.assertEqual(self.status(), before_status)
        self.assertEqual(self.target.stat().st_mtime, OLD_MTIME)
        after = self.document()
        binding = after["payload_binding"]
        self.assertEqual(binding.pop("parts_version"), rule_pipeline.PARTS_VERSION)
        text = self.target.read_text(encoding="utf-8")
        self.assertEqual(binding.pop("parts"), rule_pipeline.payload_parts(text, after))
        self.assertEqual(after, before)
        changed = [pair for pair in zip(before_bytes.splitlines(), self.target.read_bytes().splitlines())
                   if pair[0] != pair[1]]
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0][0].startswith(rule_provenance.FIELD.encode()))
        filled = self.target.read_bytes()
        again = rule_repair.sweep(self.vault, apply=True)
        self.assertEqual((again["backfilled"], again["backfillable"]), ([], []))
        self.assertEqual(self.target.read_bytes(), filled)

    def test_backfill_leaves_cards_whose_binding_no_longer_matches_untouched(self):
        self.write_card(parts=False)
        text = self.target.read_text(encoding="utf-8")
        self.target.write_text(text.replace("改測試要先說明理由。", "改測試前先說明。"), encoding="utf-8")
        os.utime(self.target, (OLD_MTIME, OLD_MTIME))
        edited = self.target.read_bytes()
        result = rule_repair.sweep(self.vault, apply=True)
        self.assertEqual((result["backfilled"], result["rebound"]), ([], []))
        self.assertEqual(self.target.read_bytes(), edited)
        self.assertEqual(self.target.stat().st_mtime, OLD_MTIME)
        self.assertEqual(self.status(), rule_provenance.INVALID)

    def test_after_backfill_dream_rebinds_a_body_edit_made_outside_hooks(self):
        self.write_card(parts=False)
        rule_repair.sweep(self.vault, apply=True)
        text = self.target.read_text(encoding="utf-8")
        self.target.write_text(text.replace("改測試要先說明理由。", "改測試前先說明。"), encoding="utf-8")
        result = rule_repair.sweep(self.vault, apply=True)
        self.assertEqual(result["rebound"], [self.target.name])
        self.assertEqual(self.status(), rule_provenance.REVIEWED)

if __name__ == "__main__":
    # run_all passes --selftest; drop it from sys.argv itself, since hook mains called by the tests read it too.
    while "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
