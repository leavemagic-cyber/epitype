import sys; sys.dont_write_bytecode = True
"""Offline selection regressions, with isolated homes, vaults and markers."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get("EPITYPE_SELECTION_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import memsearch, memspec
import _hook_common as common
import recall_hook as recall


# A card without a verified original source is recalled as a lookup hint, never as a
# ruling (6f6fce1, docs/RULE_PROVENANCE.md). These fixtures are such legacy cards; an
# active decision among them keeps its pinned seat and is told apart by its key and date.
LOOKUP = "- " + memspec.RECALL_LOOKUP_PREFIX


def _decision_line(line, key):
    return line.startswith(LOOKUP + key + memspec.RECALL_DECIDED_AT_SUFFIX.format(
        decided_at="2026-09-07"))


class RecallSelectionRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-selection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vaults = [self.root / "first", self.root / "second"]
        for vault in self.vaults:
            vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, self.vaults)
        for instrument in (
            patch.dict(os.environ, {
                memspec.EPITYPE_CONFIG_ENV: str(self.config),
                memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
                "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            }),
            patch.object(recall, "recall_marker_directory", lambda session: self.root / "markers" / session),
        ):
            instrument.start()
            self.addCleanup(instrument.stop)

    def card(self, vault, name, description="refillneedle", decision=False, aliases=()):
        authority = ("decision_key: " + name + "\nstatus: active\n"
                     "current_decision_at: 2026-09-07\ndecided_by: owner-explicit\n"
                     "owner_quote: Keep the synthetic policy\n") if decision else ""
        topics = 'aliases: [' + ', '.join(aliases) + ']\n' if aliases else ''
        (vault / (name + ".md")).write_text(
            f"---\nname: {name}\ndescription: {description}\n{topics}{authority}---\n",
            encoding="utf-8",
        )

    def populate(self, count=5):
        for index, vault in enumerate(self.vaults):
            for number in range(count):
                self.card(vault, f"fixture-{index}-{number}")
            memsearch.build_index(vault)

    def invoke(self, session="refill", prompt="refillneedle", cwd=None, **context):
        output = io.StringIO()
        event = {"prompt": prompt, "session_id": session,
                 "transcript_path": (str(self.root / '.claude/projects/fixture' / (session+'.jsonl'))
                                     if session else None)}
        if cwd is not None:
            event["cwd"] = cwd
        event.update(context)
        with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
             patch.object(sys, "argv", ["recall"]), \
             patch.object(recall, "_STARTED_AT", time.monotonic()), \
             contextlib.redirect_stdout(output):
            self.assertEqual(recall.main(), 0)
        if not output.getvalue():
            return ""
        return json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]

    @staticmethod
    def lines(context):
        return [line for line in context.splitlines() if line.startswith("- ")]

    def test_default_turn_does_not_search_but_still_captures(self):
        self.populate()
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value.pop(memspec.CONFIG_PROACTIVE_RECALL_FIELD)
        self.config.write_text(json.dumps(value), encoding="utf-8")
        with patch.object(memsearch, "recall_index", side_effect=AssertionError("unexpected proactive search")), \
             patch("epitype.capture.capture_transcript", return_value=1) as capture, \
             patch.object(recall, "_capture_event") as fallback:
            self.assertEqual(self.invoke(prompt="refillneedle"), "")
            capture.assert_called_once()
            self.assertEqual(capture.call_args.args[1]["prompt"], "refillneedle")
            fallback.assert_not_called()

    def test_default_turn_keeps_pending_notes(self):
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value.pop(memspec.CONFIG_PROACTIVE_RECALL_FIELD)
        self.config.write_text(json.dumps(value), encoding="utf-8")
        with patch.object(memsearch, "recall_index", side_effect=AssertionError("unexpected proactive search")), \
             patch.object(recall, "peek_notes", return_value=["Keep the current task state"]), \
             patch.object(recall, "consume_notes") as consume:
            self.assertIn("Keep the current task state", self.invoke())
            consume.assert_called_once()

    def test_resident_is_not_proactively_resent_but_remains_searchable(self):
        self.card(self.vaults[0], "resident", description="residentneedle")
        path = self.vaults[0] / "resident.md"
        path.write_text(path.read_text(encoding="utf-8").replace("---\n", "---\nlayer: resident\n", 1), encoding="utf-8")
        memsearch.build_index(self.vaults[0])
        self.assertEqual(self.invoke(prompt="residentneedle"), "")
        self.assertEqual(memsearch.query_index(self.vaults[0], "residentneedle")["count"], 1)

    def test_seen_cards_do_not_consume_new_slots(self):
        self.populate()
        first, second = self.lines(self.invoke()), self.lines(self.invoke())
        self.assertEqual((len(first), len(second)), (8, 2))
        self.assertTrue(set(first).isdisjoint(second))
        self.assertEqual(self.invoke(), "")  # procedure already sent this session (owner 2026-09-09)

    def test_existing_per_vault_candidate_cap_does_not_expand(self):
        self.populate(count=7)
        delivered = self.lines(self.invoke()) + self.lines(self.invoke())
        self.assertEqual(len(set(delivered)), 10)
        self.assertEqual(self.invoke(), "")  # procedure already sent this session (owner 2026-09-09)

    def test_delivered_authority_releases_slots_without_losing_priority(self):
        self.populate()
        for number in range(4):
            self.card(self.vaults[1], f"decision-{number}", decision=True)
        memsearch.build_index(self.vaults[1])
        first, second = self.lines(self.invoke()), self.lines(self.invoke())
        self.assertEqual((len(first), len(second)), (8, 6))
        self.assertTrue(all(any(_decision_line(line, f"decision-{number}") for number in range(4))
                            for line in first[:4]), first)
        self.assertFalse(any(_decision_line(line, "decision-") or "/decision-" in line
                             for line in second), second)
        self.assertFalse(any(memspec.DECISION_PREFIX in line for line in first + second))
        self.assertEqual(self.invoke(), "")  # procedure already sent this session (owner 2026-09-09)

    def test_card_is_not_resent_when_the_alias_numbering_changes(self):
        # 代號依這則提問用到哪些庫而編：beta 第一次是 V1，第二次變 V2。指紋若含代號就會整批重送。
        self.card(self.vaults[0], "alpha", "sharedneedle alphaneedle")
        self.card(self.vaults[1], "beta", "sharedneedle betaneedle")
        for vault in self.vaults:
            memsearch.build_index(vault)
        first = self.lines(self.invoke(prompt="betaneedle"))
        second = self.lines(self.invoke(prompt="sharedneedle"))
        self.assertTrue(any("/beta.md" in line for line in first), first)
        self.assertFalse(any("/beta.md" in line for line in second), second)
        self.assertTrue(any("/alpha.md" in line for line in second), second)

    def test_same_line_text_in_another_vault_is_a_different_card(self):
        line = "- sameneedle | V1/same.md"
        self.assertNotEqual(
            recall._card_identity(line, {"V1": "C:/vault-a"}),
            recall._card_identity(line, {"V1": "C:/vault-b"}),
        )
        self.assertEqual(
            recall._card_identity("- sameneedle | V1/same.md", {"V1": "C:/vault-a"}),
            recall._card_identity("- sameneedle | V2/same.md", {"V2": "C:/vault-a"}),
        )

    def test_new_matching_body_of_a_delivered_decision_is_not_hidden(self):
        vault = self.vaults[0]
        self.card(vault, "mutable-decision", "revisionneedle policy", decision=True)
        path = vault / "mutable-decision.md"
        revised_body = "revisionneedle current revised detail."
        original_body = "Initial unrelated detail.".ljust(len(revised_body))
        initial = path.read_text(encoding="utf-8") + original_body + "\n"
        path.write_text(initial, encoding="utf-8")
        memsearch.build_index(vault)
        first = self.invoke(session="revision", prompt="revisionneedle")
        self.assertIn("/mutable-decision.md", first)
        self.assertNotIn("current revised detail", first)
        self.assertEqual(self.invoke(session="revision", prompt="revisionneedle"), "")

        before = path.stat()
        path.write_text(initial.replace(original_body, revised_body), encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        # A same-size, preserved-mtime edit must still be read from the real source.
        self.assertEqual(path.stat().st_size, before.st_size)
        updated = self.invoke(session="revision", prompt="revisionneedle")
        self.assertIn("revisionneedle current revised detail", updated)
        self.assertEqual(self.invoke(session="revision", prompt="revisionneedle"), "")

    def test_changed_description_is_already_eligible_in_the_same_session(self):
        vault = self.vaults[0]
        self.card(vault, "mutable-description", "descriptionneedle previous detail", decision=True)
        memsearch.build_index(vault)
        self.assertIn("previous detail", self.invoke(session="description", prompt="descriptionneedle"))
        self.card(vault, "mutable-description", "descriptionneedle current detail", decision=True)
        memsearch.build_index(vault)
        self.assertIn("current detail", self.invoke(session="description", prompt="descriptionneedle"))

    def test_new_prompt_can_load_a_different_part_of_a_delivered_decision(self):
        vault = self.vaults[0]
        self.card(vault, "different-passage", "readfirstneedle policy", decision=True)
        path = vault / "different-passage.md"
        path.write_text(path.read_text(encoding="utf-8")
                        + "newtopicneedle detailneedle applies to the current case.\n", encoding="utf-8")
        memsearch.build_index(vault)
        first = self.invoke(session="different-passage", prompt="readfirstneedle")
        self.assertNotIn("applies to the current case", first)
        second = self.invoke(session="different-passage", prompt="newtopicneedle detailneedle")
        self.assertIn("newtopicneedle detailneedle applies to the current case", second)
        self.assertEqual(self.invoke(session="different-passage", prompt="newtopicneedle detailneedle"), "")

    def test_delivered_summary_is_not_downgraded_to_a_new_pointer(self):
        vault = self.vaults[0]
        self.card(vault, "summary-budget", "budgetneedle " + "長" * 180, decision=True)
        memsearch.build_index(vault)
        self.assertIn("/summary-budget.md", self.invoke(session="summary-budget", prompt="budgetneedle"))
        common.write_config(self.config, [vault], budget=500)
        self.assertEqual(self.invoke(session="summary-budget", prompt="budgetneedle"), "")

    def test_updated_decision_body_reaches_the_real_adapter_process(self):
        vault = self.vaults[0]
        self.card(vault, "process-decision", "processrevisionneedle policy", decision=True)
        path = vault / "process-decision.md"
        initial = path.read_text(encoding="utf-8") + "Initial unrelated detail.\n"
        path.write_text(initial, encoding="utf-8")
        memsearch.build_index(vault)
        temporary = self.root / "process-temp"
        temporary.mkdir()
        event = {"prompt": "processrevisionneedle", "session_id": "process-revision",
                 "transcript_path": str(self.root/'.claude/projects/fixture/process-revision.jsonl')}
        environment = {name: str(temporary) for name in ("TMPDIR", "TEMP", "TMP")}

        def invoke():
            result = common.run_synthetic(Path(recall.__file__), event, self.config,
                                          environment=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            return (json.loads(result.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")
                    if result.stdout.strip() else "")

        self.assertIn("/process-decision.md", invoke())
        path.write_text(initial.replace("Initial unrelated detail.",
                                        "processrevisionneedle current body."), encoding="utf-8")
        memsearch.build_index(vault)
        self.assertIn("processrevisionneedle current body", invoke())
        self.assertEqual(invoke(), "")

    def test_prompt_recall_reads_relevant_source_words_not_just_the_pointer(self):
        vault = self.vaults[0]
        (vault / "noise.md").write_text(
            "---\nname: noise\ndescription: 賞金規則\n---\n無關的帳號條件。\n",
            encoding="utf-8",
        )
        (vault / "case.md").write_text(
            "---\nname: case\ndescription: 案件執行規則\n---\n"
            + ("- 其他已驗證事項。\n" * 100)
            + "- 改名時須區分已送件與待回覆的賞金案件。\n"
            + "- 已付款才能標成實收；未核獎不得冒充收入。\n",
            encoding="utf-8",
        )
        memsearch.build_index(vault)
        delivered = self.invoke(session="source", prompt="賞金案件改名規則")
        self.assertIn("改名時須區分已送件與待回覆的賞金案件", delivered)
        self.assertIn("已付款才能標成實收", delivered)
        self.assertIn("V1/case.md", delivered)
        self.assertIn("V1/case.md", self.lines(delivered)[0])
        self.assertLessEqual(delivered.count("原文節錄 L"), memspec.RECALL_SOURCE_MAX_CARDS)
        self.assertEqual(self.invoke(session="source", prompt="賞金案件改名規則"), "")

    def test_active_decision_and_ordinary_card_both_get_current_source_passages(self):
        vault = self.vaults[0]
        (vault / "decision.md").write_text(
            "---\nname: decision\ndescription: rulingneedle current\n"
            "decision_key: source-read\nstatus: active\n"
            "current_decision_at: 2026-09-07\ndecided_by: owner-explicit\n"
            "owner_quote: Use the current rule\n---\n"
            "rulingneedle applies to every new case.\n",
            encoding="utf-8",
        )
        (vault / "ordinary.md").write_text(
            "---\nname: ordinary\ndescription: rulingneedle ordinary\n---\n"
            "rulingneedle records the current case stage.\n",
            encoding="utf-8",
        )
        memsearch.build_index(vault)
        delivered = self.invoke(session="decision-source", prompt="rulingneedle")
        self.assertIn("applies to every new case", delivered)
        self.assertIn("records the current case stage", delivered)
        self.assertEqual(delivered.count("原文節錄 L"), 2)

    def test_source_read_stays_inside_vault_and_does_not_claim_a_missing_file(self):
        vault = self.vaults[0]
        outside = self.root / "outside.md"
        outside.write_text("---\nname: outside\n---\n賞金改名不得讀到這裡\n", encoding="utf-8")
        self.assertEqual(recall._source_passage(outside, vault, ["賞金", "改名"]), "")
        self.assertEqual(recall._source_passage(vault / "missing.md", vault, ["賞金"]), "")
        retired = vault / "retired.md"
        retired.write_text("---\nname: retired\nstatus: superseded\n---\n賞金改名\n",
                           encoding="utf-8")
        self.assertEqual(recall._source_passage(retired, vault, ["賞金", "改名"]), "")
        current = vault / "current.md"
        current.write_text("---\nname: replacement\n---\n賞金改名\n", encoding="utf-8")
        self.assertEqual(recall._source_passage(current, vault, ["賞金", "改名"], "current"), "")

    def test_small_budget_falls_back_to_pointer_and_both_host_events_get_source(self):
        vault = self.vaults[0]
        (vault / "rule.md").write_text(
            "---\nname: rule\ndescription: caseword policy\n---\n"
            "- caseword before a title change, read the current case status. "
            + ("Relevant detail. " * 18) + "\n",
            encoding="utf-8",
        )
        memsearch.build_index(vault)
        common.write_config(self.config, [vault], budget=500)
        small = self.invoke(session="small", prompt="caseword title")
        self.assertIn("V1/rule.md", small)
        self.assertNotIn("原文節錄", small)
        common.write_config(self.config, [vault])
        for key in ("session_id", "sessionId"):
            with self.subTest(key=key):
                result = common.run_synthetic(
                    Path(recall.__file__),
                    {"prompt": "caseword title", key: "source-" + key},
                    self.config,
                )
                self.assertEqual(result.returncode, 0)
                context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
                self.assertIn("原文節錄", context)
                self.assertIn("read the current case status", context)

    def native_vault(self, cwd):
        from epitype import capture_route

        vault = (self.root / "home" / ".claude" / "projects" / capture_route.project_slug(cwd) / "memory")
        vault.mkdir(parents=True)
        return vault

    def test_unicode_native_project_reference_reaches_actual_recall_output(self):
        current = self.root/'中文專案'
        current.mkdir()
        here = self.native_vault(current)
        (here.parent/'session.jsonl').write_text(
            json.dumps({'cwd':str(current)}, ensure_ascii=False)+'\n', encoding='utf-8')
        self.card(here, 'own-history', 'unicoderecallneedle relevant precedent')
        memsearch.build_index(here)
        context = self.invoke(session='unicode-own', prompt='unicoderecallneedle', cwd=str(current))
        self.assertIn('own-history', context)
        self.assertIn('relevant precedent', context)

    def test_recall_stays_in_this_project_and_the_governance_vault(self):
        # vaults[0] holds the ledger, so it is the governance vault; vaults[1] is another project.
        (self.vaults[0] / memspec.WORK_LEDGER_FILENAME).write_text("# ledger\n", encoding="utf-8")
        cwd = str(self.root / "workspace")
        here = self.native_vault(cwd)
        for vault, name in ((self.vaults[0], "governance-card"), (self.vaults[1], "other-project-card"),
                            (here, "this-project-card")):
            self.card(vault, name, "scopeneedle")
            memsearch.build_index(vault)
        scoped = self.invoke(session="scoped", prompt="scopeneedle", cwd=cwd)
        self.assertIn("this-project-card", scoped)
        self.assertIn("governance-card", scoped)
        self.assertNotIn("other-project-card", scoped)
        unscoped = self.invoke(session="unscoped", prompt="scopeneedle")
        self.assertIn("other-project-card", unscoped)  # 沒有 cwd 判斷不出專案，照舊搜全部

    def test_shared_reference_declaring_another_project_is_not_delivered(self):
        vault = self.vaults[0]
        current, other = self.root / 'current', self.root / 'other'
        current.mkdir()
        other.mkdir()
        self.card(vault, 'project-payment', 'scopepaymentneedle payment policy')
        path = vault / 'project-payment.md'
        path.write_text(path.read_text(encoding='utf-8').replace(
            '---\n', '---\nmetadata:\n  type: reference\n', 1)
            + f'**適用專案**：`{other}`。Retained here to preserve links.\n', encoding='utf-8')
        self.card(vault, 'common-policy', 'scopepaymentneedle shared guidance', decision=True)
        memsearch.build_index(vault)
        outside = self.invoke(session='scope-out', prompt='scopepaymentneedle', cwd=str(current))
        self.assertNotIn('project-payment.md', outside)
        self.assertIn('common-policy', outside)
        inside = self.invoke(session='scope-in', prompt='scopepaymentneedle', cwd=str(other))
        self.assertIn('project-payment.md', inside)
        unknown = self.invoke(session='scope-unknown', prompt='scopepaymentneedle')
        self.assertIn('project-payment.md', unknown)
        # Ownership is read live, even when the search index still has old bytes.
        original = path.read_text(encoding='utf-8')
        changed = original.replace(str(other), str(current))
        length = max(len(original), len(changed))
        path.write_text(original.ljust(length), encoding='utf-8')
        memsearch.build_index(vault)
        stamp = path.stat()
        path.write_text(changed.ljust(length), encoding='utf-8')
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        refreshed = self.invoke(session='scope-live-body', prompt='scopepaymentneedle', cwd=str(current))
        self.assertIn('project-payment.md', refreshed)

    def test_explicit_reference_project_is_checked_before_ordinary_slots(self):
        vault = self.vaults[0]
        current, other = self.root / 'current', self.root / 'other'
        for number in range(5):
            self.card(vault, f'a-other-{number}', 'projectslotneedle')
            path = vault / f'a-other-{number}.md'
            path.write_text(path.read_text(encoding='utf-8').replace(
                '---\n', f'---\nmetadata:\n  type: reference\nproject: {other}\n', 1), encoding='utf-8')
        self.card(vault, 'z-common', 'projectslotneedle')
        memsearch.build_index(vault)
        value = self.invoke(session='scope-slots', prompt='projectslotneedle', cwd=str(current))
        self.assertNotIn('a-other-', value)
        self.assertIn('z-common.md', value)

    def test_verified_scope_is_checked_before_pinning_for_both_hosts(self):
        from rule_source_fixtures import source_bound_card
        vault = self.vaults[0]
        for host in ('claude', 'codex'):
            path = vault / f'source-project-{host}.md'
            text = ('---\nname: source-project-' + host + '\ndescription: sourcescopeneedle\n'
                    'decision_key: source-project-' + host + '\nstatus: active\n'
                    'current_decision_at: 2026-09-07\ndecided_by: owner-explicit\n'
                    'owner_quote: Use this isolated sourcescopeneedle case.\n---\n')
            path.write_text(source_bound_card(text, path, source_root=self.root / 'sources',
                                             case_id='source-scope', host=host), encoding='utf-8')
        memsearch.build_index(vault)
        outside = self.invoke(session='source-out', prompt='sourcescopeneedle',
                              project='another-project', case_id='source-scope')
        self.assertNotIn('source-project-', outside)
        inside = self.invoke(session='source-in', prompt='sourcescopeneedle',
                             project='epitype-pretooluse-selftest', case_id='source-scope')
        self.assertIn('source-project-claude.md', inside)
        self.assertIn('source-project-codex.md', inside)

    def test_rejected_search_seeds_do_not_smuggle_in_linked_cards(self):
        vault = self.vaults[0]
        for kind in ('foreign', 'raw-event', 'invalid-source', 'quiet', 'retired'):
            with self.subTest(kind=kind):
                needle = kind.replace('-', '') + 'expansionneedle'
                name = ('corrections/seed-event' if kind == 'raw-event' else 'seed-' + kind)
                seed = vault / (name + '.md')
                seed.parent.mkdir(parents=True, exist_ok=True)
                extra = (f'project: {self.root / "other"}\nmetadata:\n  type: reference\n'
                         if kind == 'foreign' else 'rule_provenance: "{}"\n' if kind == 'invalid-source' else '')
                if kind == 'quiet':
                    extra = 'metadata:\n  type: reference\n'
                if kind == 'retired':
                    extra = ('decision_key: retired-seed\nstatus: active\ncurrent_decision_at: 2026-09-07\n'
                             'decided_by: owner-explicit\nowner_quote: Keep the isolated seed policy.\n')
                seed.write_text(f'---\nname: {name}\ndescription: {needle}\n{extra}---\n'
                                f'[[linked-{kind}]]\n', encoding='utf-8')
                self.card(vault, 'linked-' + kind, 'Unrelated project detail')
                self.card(vault, 'global-' + kind, needle, decision=True)
                if kind == 'quiet':
                    health = vault / memspec.FTS_INDEX_DIRECTORY / memspec.RECALL_HEALTH_FILENAME
                    health.parent.mkdir(parents=True, exist_ok=True)
                    health.write_text(json.dumps({'version': recall.recall_quiet.HEALTH_VERSION,
                                                 'days': {}, 'quiet': [seed.name]}), encoding='utf-8')
                memsearch.build_index(vault)
                if kind == 'retired':
                    stamp = seed.stat()
                    seed.write_text(seed.read_text(encoding='utf-8').replace('status: active', 'status: closed'), encoding='utf-8')
                    os.utime(seed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                context = self.invoke(session='seed-' + kind, prompt=needle, cwd=str(self.root / 'here'))
                self.assertIn('global-' + kind + '.md', context)
                self.assertNotIn(seed.name, context)
                self.assertNotIn('linked-' + kind + '.md', context)

    def test_valid_links_and_independent_direct_matches_remain_available(self):
        vault = self.vaults[0]
        self.card(vault, 'valid-seed', 'validexpansionneedle')
        seed = vault / 'valid-seed.md'
        seed.write_text(seed.read_text(encoding='utf-8') + '[[valid-neighbour]]\n', encoding='utf-8')
        self.card(vault, 'valid-neighbour', 'Related by a valid source link')
        self.card(vault, 'foreign-seed', 'directexpansionneedle')
        seed = vault / 'foreign-seed.md'
        seed.write_text(seed.read_text(encoding='utf-8').replace(
            '---\n', f'---\nproject: {self.root / "other"}\n', 1)
            + '[[independent-target]]\n', encoding='utf-8')
        self.card(vault, 'independent-target', 'directexpansionneedle')
        memsearch.build_index(vault)
        linked = self.invoke(session='valid-links', prompt='validexpansionneedle', cwd=str(self.root / 'here'))
        self.assertIn('valid-seed.md', linked)
        self.assertIn('valid-neighbour.md', linked)
        direct = self.invoke(session='direct-link', prompt='directexpansionneedle', cwd=str(self.root / 'here'))
        self.assertIn('independent-target.md', direct)
        self.assertNotIn('foreign-seed.md', direct)

    def test_reviewed_global_scope_wins_over_legacy_reference_project_text(self):
        from rule_source_fixtures import owner_bound_card
        vault = self.vaults[0]
        path = vault / 'global-source.md'
        text = ('---\nname: global-source\ndescription: globalscopeneedle\n'
                'metadata:\n  type: reference\nproject: ' + str(self.root / 'other') + '\n---\n'
                f'**適用專案**：`{self.root / "other"}`。\n')
        path.write_text(owner_bound_card(text, path, source_root=self.root / 'sources',
                                        case_id='global-scope'), encoding='utf-8')
        memsearch.build_index(vault)
        self.assertIn('global-source.md', self.invoke(session='global-source', prompt='globalscopeneedle',
                                                    cwd=str(self.root / 'here')))

    def test_an_exact_card_copy_in_a_later_vault_is_not_shown_again(self):
        self.card(self.vaults[0], "shared-name", "dupeneedle first")
        (self.vaults[1] / "shared-name.md").write_bytes((self.vaults[0] / "shared-name.md").read_bytes())
        for vault in self.vaults:
            memsearch.build_index(vault)
        lines = [line for line in self.lines(self.invoke(session="dupe", prompt="dupeneedle"))
                 if "shared-name.md" in line]
        self.assertEqual(len(lines), 1, lines)

    def test_same_filename_with_different_current_summaries_keeps_both_cards(self):
        self.card(self.vaults[0], "shared-name", "dupeneedle first requirement")
        self.card(self.vaults[1], "shared-name", "dupeneedle second requirement")
        for vault in self.vaults:
            memsearch.build_index(vault)
        context = self.invoke(session="distinct-summaries", prompt="dupeneedle")
        self.assertIn("first requirement", context)
        self.assertIn("second requirement", context)
        self.assertEqual(len(self.lines(context)), 2)

    def test_same_filename_and_summary_with_different_bodies_keeps_both_cards(self):
        for vault, detail in zip(self.vaults, ("first detail", "second detail")):
            self.card(vault, "shared-name", "dupeneedle summary")
            with (vault / "shared-name.md").open("a", encoding="utf-8") as stream:
                stream.write(f"dupeneedle {detail}.\n")
            memsearch.build_index(vault)
        context = self.invoke(session="distinct-bodies", prompt="dupeneedle")
        self.assertIn("first detail", context)
        self.assertIn("second detail", context)
        self.assertEqual(len(self.lines(context)), 2)

    def test_same_basename_in_different_subdirectories_keeps_both_cards(self):
        vault = self.vaults[0]
        for folder in ("alpha", "beta"):
            (vault / folder).mkdir()
            self.card(vault, folder + "/shared-name", "dupeneedle " + folder)
        memsearch.build_index(vault)
        context = self.invoke(session="distinct-subdirectories", prompt="dupeneedle")
        self.assertIn("/alpha/shared-name.md", context)
        self.assertIn("/beta/shared-name.md", context)

    def test_identical_bytes_in_different_relative_paths_remain_distinct(self):
        vault = self.vaults[0]
        self.card(vault, "shared-name", "dupeneedle copy")
        (vault / "nested").mkdir()
        (vault / "nested/shared-name.md").write_bytes((vault / "shared-name.md").read_bytes())
        memsearch.build_index(vault)
        context = self.invoke(session="distinct-relative-paths", prompt="dupeneedle")
        self.assertEqual(len(self.lines(context)), 2)
        self.assertIn("/nested/shared-name.md", context)

    def test_nightly_quiet_list_skips_ordinary_cards_but_never_decisions(self):
        self.card(self.vaults[0], "noisy", "quietneedle")
        self.card(self.vaults[0], "keeper", "quietneedle")
        self.card(self.vaults[0], "ruling", "quietneedle", decision=True)
        # Only information cards are eligible for the nightly quiet list.
        path = self.vaults[0] / "noisy.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "description: quietneedle", "description: quietneedle\nmetadata:\n  type: reference"),
            encoding="utf-8")
        memsearch.build_index(self.vaults[0])
        health = self.vaults[0] / memspec.FTS_INDEX_DIRECTORY / memspec.RECALL_HEALTH_FILENAME
        from epitype import recall_quiet
        health.write_text(json.dumps({"version": recall_quiet.HEALTH_VERSION,
                                     "days": {}, "quiet": ["noisy.md", "ruling.md"]}), encoding="utf-8")
        delivered = self.invoke(prompt="quietneedle")
        self.assertNotIn("/noisy.md", delivered)
        self.assertIn("/keeper.md", delivered)
        self.assertIn("/ruling.md", delivered)

    def test_absent_session_has_no_persistent_dedupe(self):
        self.populate()
        self.assertEqual(self.invoke(session=""), self.invoke(session=""))
        self.assertEqual(len(self.lines(self.invoke(session=""))), 8)

    def test_quiet_reference_promoted_to_behavior_is_recalled(self):
        from datetime import date
        from epitype import recall_quiet

        vault = self.vaults[0]
        self.card(vault, "promoted", "promotionneedle")
        path = vault / "promoted.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "description: promotionneedle", "description: promotionneedle\nmetadata:\n  type: reference"),
            encoding="utf-8")
        recall_quiet.update(vault, {"promoted.md": [10, 0]}, date.today())
        self.assertIn("promoted.md", recall_quiet.quiet_cards(vault))
        path.write_text(path.read_text(encoding="utf-8").replace(
            "description: promotionneedle", "description: promotionneedle\nforbidden: [unsafe]"),
            encoding="utf-8")
        memsearch.build_index(vault)
        self.assertIn("/promoted.md", self.invoke(prompt="promotionneedle"))

    def test_stronger_query_evidence_in_later_vault_gets_first_slot(self):
        for vault in self.vaults:
            for number in range(5):
                self.card(vault, f"weak-{vault.name}-{number}", "selectneedle", aliases=('selectneedle',))
        third = self.root / "third"
        third.mkdir()
        self.vaults.append(third)
        common.write_config(self.config, self.vaults)
        self.card(third, "answer", "selectneedle detailneedle")
        for vault in self.vaults:
            memsearch.build_index(vault)
        lines = self.lines(self.invoke(prompt="selectneedle detailneedle"))
        self.assertEqual(len(lines), 8)
        self.assertIn("V3/answer.md", lines[0])

    def test_equal_evidence_interleaves_local_ranks_before_vault_ties(self):
        self.populate()
        lines = self.lines(self.invoke())
        self.assertEqual(["V1/" in line for line in lines[:4]], [True, False, True, False])

    def test_single_vault_keeps_existing_search_order(self):
        self.populate()
        common.write_config(self.config, [self.vaults[0]])
        hits = memsearch.recall_index(self.vaults[0], "refillneedle")["results"]
        lines = self.lines(self.invoke())
        self.assertEqual([line.rsplit("/", 1)[-1] for line in lines],
                         [hit["card_path"] for hit in hits])

    def test_a_captured_quote_consumes_no_slot_however_well_it_matches(self):
        """U-H: 原話事件檔連弱席都不佔，普通卡照樣補滿；決策卡不受影響。"""
        quotes = self.vaults[0] / memspec.RULING_DIRECTORY
        quotes.mkdir()
        (quotes / "ruling-20260907-quote.md").write_text(
            "---\nname: ruling-20260907-quote\n"
            "description: selectneedle detailneedle owner 原話\n---\nselectneedle detailneedle\n",
            encoding="utf-8",
        )
        for number in range(6):
            self.card(self.vaults[0], f"plain-{number}", "selectneedle detailneedle")
        for vault in self.vaults:
            memsearch.build_index(vault)
        lines = self.lines(self.invoke(prompt="selectneedle detailneedle"))
        self.assertEqual(len(lines), memspec.FTS_TOP_K)
        self.assertFalse(any("ruling-20260907-quote" in line for line in lines))

    def test_authority_precedes_stronger_ordinary_query_evidence(self):
        self.card(self.vaults[0], "policy", "selectneedle", decision=True, aliases=('selectneedle',))
        self.card(self.vaults[1], "answer", "selectneedle detailneedle")
        for vault in self.vaults:
            memsearch.build_index(vault)
        lines = self.lines(self.invoke(prompt="selectneedle detailneedle"))
        self.assertTrue(_decision_line(lines[0], "policy"), lines)
        self.assertTrue(any("V2/answer.md" in line for line in lines[1:]), lines)
        self.assertFalse(any(memspec.DECISION_PREFIX in line for line in lines))

    def test_whole_short_card_fills_space_after_oversized_ordinary_hit(self):
        self.card(self.vaults[0], "long", "packneedle " + "長" * 100)
        self.card(self.vaults[0], "short", "packneedle")
        memsearch.build_index(self.vaults[0])
        hits = memsearch.recall_index(self.vaults[0], "packneedle")["results"]
        hits.sort(key=lambda hit: hit["card_path"] != "long.md")
        with patch.object(recall.memsearch, "recall_index", return_value={"results": hits}), \
             patch.object(recall, "scoped_vaults", return_value=[self.vaults[0]]):
            # Budget for the header, the long card's pointer and the whole small card. An
            # oversized hit is delivered as an explicit pointer, never as a cut half line
            # (6f6fce1, docs/RULE_PROVENANCE.md); the small card still gets the space after it.
            full = self.invoke(session="")
            header = full.split("\n- ", 1)[0]
            short_line = next(line for line in self.lines(full) if line.endswith("V1/short.md"))
            pointer = LOOKUP + "V1/long.md"
            budget = len("\n".join([header, pointer, short_line]).encode("utf-8"))
            common.write_config(self.config, [self.vaults[0]], budget=budget)
            packed = self.invoke()
            self.assertEqual(self.lines(packed), [pointer, short_line])
            self.assertNotIn("長", packed)
            self.assertLessEqual(len(packed.encode("utf-8")), budget)
            common.write_config(self.config, [self.vaults[0]])
            retried = self.invoke()
            self.assertIn("長" * 100 + " | V1/long.md", retried)
            self.assertNotIn("V1/short.md", retried)

    def test_oversized_pinned_policy_is_not_bypassed_by_small_ordinary_card(self):
        # The pinned line carries the decision's description, so that is what is made oversized.
        self.card(self.vaults[0], "policy", "packneedle " + "長" * 500, decision=True)
        self.card(self.vaults[0], "short", "packneedle")
        memsearch.build_index(self.vaults[0])
        common.write_config(self.config, [self.vaults[0]], budget=700)
        packed = self.invoke(prompt="packneedle")
        # Over budget the pinned seat becomes an explicit pointer that still comes first;
        # no cut half ruling, and the small card never takes its place.
        lines = self.lines(packed)
        self.assertEqual(lines[0], LOOKUP + "V1/policy.md")
        self.assertNotIn("長", packed)
        self.assertNotIn(memspec.DECISION_PREFIX, packed)
        common.write_config(self.config, [self.vaults[0]])
        self.assertIn("長" * 500 + " | V1/policy.md", self.invoke(prompt="packneedle"))

    def test_merge_keeps_each_vaults_order_even_when_coverage_varies(self):
        groups = [[(1, "a"), (3, "b")], [], [(2, "c"), (0, "d")]]
        self.assertEqual(list(recall._merge_ordinary(groups)), ["c", "a", "b", "d"])

    def test_each_vaults_top_card_precedes_another_vaults_second_card(self):
        groups = [[(1, "project-first"), (1, "project-second")],
                  [(8, "governance-first"), (7, "governance-second")]]
        self.assertEqual(list(recall._merge_ordinary(groups)),
                         ["governance-first", "project-first", "governance-second", "project-second"])

    def test_successor_hints_follow_ranked_ordinary_cards_and_preserve_pins(self):
        def hit(path, active=False):
            return {'path': path, 'status': 'active' if active else ''}
        result = {'results': [hit('winner'), hit('ranked-pin', True), hit('runner-up')],
                  'replacement_results': [hit('hint'), hit('successor-pin', True)]}
        with patch.object(recall, '_frontmatter_fields', return_value={'status': 'active', 'decision_key': 'pin'}):
            self.assertEqual([value['path'] for value in recall._recall_candidates(result)],
                             ['successor-pin', 'ranked-pin', 'winner', 'runner-up', 'hint'])

    def test_active_project_record_does_not_jump_ahead_of_search_winner(self):
        result = {'results': [{'path': 'winner'}],
                  'replacement_results': [{'path': 'project', 'status': 'active'}]}
        with patch.object(recall, '_frontmatter_fields', return_value={'status': 'active', 'type': 'project'}):
            self.assertEqual([value['path'] for value in recall._recall_candidates(result)], ['winner', 'project'])

    def test_budget_sweep_keeps_whole_cards_header_and_authority_prefix(self):
        header = memspec.UNTRUSTED_ADVISORY + "\nvaults: V1=synthetic"
        pinned = ["- POLICY-" + "裁定" * 15, "- POLICY-" + "規則" * 30]
        ordinary = ["- CARD-" + ('"\\條文' * size) for size in (3, 150, 5, 7, 9, 11, 13, 15)]
        for budget in range(80, 2200, 37):
            with self.subTest(budget=budget):
                context = recall._bounded_recall([header, *pinned, *ordinary], budget, 3, 1)
                if not context:
                    continue
                self.assertTrue(context.startswith(header))
                self.assertTrue(common.payload_fits("UserPromptSubmit", context, budget))
                selected = self.lines(context)
                self.assertLessEqual(len(selected), memspec.RECALL_TOTAL_MAX_LINES)
                self.assertTrue(all(line in pinned + ordinary for line in selected))
                if any(line in ordinary for line in selected):
                    self.assertEqual(selected[:2], pinned)
                if pinned[1] in selected:
                    self.assertIn(pinned[0], selected)

    def test_recall_metadata_excludes_generic_words_and_query_output_is_unchanged(self):
        self.card(self.vaults[0], "meta", "selectneedle detailneedle the")
        memsearch.build_index(self.vaults[0])
        hit = memsearch.recall_index(self.vaults[0], "the selectneedle detailneedle")["results"][0]
        self.assertEqual(hit["matched_term_count"], 2)
        self.assertNotIn("matched_term_count", memsearch.query_index(self.vaults[0], "selectneedle")["results"][0])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] if "--selftest" in sys.argv else None)
