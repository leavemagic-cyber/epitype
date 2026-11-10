"""Partial rule sync, read migration and lossless shared provenance defaults."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import copy
import io
import unittest
from unittest.mock import patch

import rule_pipeline_regression as fixture
from epitype import core_gen, host_sync, rule_provenance


class RuleSyncDedup(unittest.TestCase):
    def setUp(self):
        fixture.RulePipeline.setUp(self)
        self.recovery = {}
        self.rules_text = lambda *args, **kwargs: host_sync.rules_text(*args, recovery=self.recovery, **kwargs)
        self.project_rules_text = lambda *args, **kwargs: host_sync.project_rules_text(*args, recovery=self.recovery, **kwargs)

    card = fixture.RulePipeline.card

    def test_sync_keeps_bad_card_prior_text_updates_other_card_and_reports_failure(self):
        self.target.write_text(self.card(), encoding="utf-8")
        other = self.vault / "rule-other.md"
        other.write_text(self.card(), encoding="utf-8")
        with patch.object(host_sync, "contract_vaults", return_value=[self.vault]):
            for host in ("claude", "codex"):
                current = self.rules_text([self.vault], host)
                bad = self.target.read_text(encoding="utf-8").replace(self.document["text"], "UNAPPROVED CHANGE")
                self.target.write_text(bad, encoding="utf-8")
                other.write_text(self.card(order="2", section="updated-section"), encoding="utf-8")
                notices = []
                result = self.rules_text([self.vault], host, current=current, notices=notices)
                self.assertIn(self.document["text"], result)
                self.assertNotIn("UNAPPROVED CHANGE", result)
                self.assertIn("updated-section", result)
                self.assertIn("未通過", "\n".join(notices))
                self.assertIn(self.target.name, "\n".join(notices))
                self.assertIn("保留", "\n".join(notices))
                self.assertIn("未計為核實", result)
                self.assertEqual(self.rules_text([self.vault], host, current=result), result)
                self.target.write_text(self.card(), encoding="utf-8")

    def test_apply_and_subsequent_check_preserve_unverified_prior_entry(self):
        self.home = self.root / "home"
        for directory in (".claude", ".codex"):
            (self.home / directory).mkdir(parents=True, exist_ok=True)
        self.target.write_text(self.card(), encoding="utf-8")
        with patch.object(host_sync, "contract_vaults", return_value=[self.vault]), \
             patch.object(host_sync, "project_targets", return_value=([], [])), \
             patch.object(host_sync.host_native, "apply", return_value=(0, 0)), \
             patch.object(host_sync.host_native, "check", return_value=(0, 0)):
            self.assertEqual(host_sync.apply([self.vault], home=self.home, output=io.StringIO()), 0)
            self.target.write_text(self.card().replace(self.document["text"], "INVALID NEW TEXT"), encoding="utf-8")
            output = io.StringIO()
            self.assertEqual(host_sync.apply([self.vault], home=self.home, output=output), 0)
            self.assertIn("未通過", output.getvalue())
            for host in ("claude", "codex"):
                actual = host_sync.host_path(host, self.home).read_text(encoding="utf-8")
                self.assertIn(self.document["text"], actual)
                self.assertNotIn("INVALID NEW TEXT", actual)
            self.assertEqual(host_sync.check([self.vault], home=self.home, output=io.StringIO()), 0)

    def test_no_prior_bad_card_is_omitted_while_good_card_generates(self):
        self.target.write_text(self.card().replace(self.document["text"], "INVALID"), encoding="utf-8")
        (self.vault / "rule-good.md").write_text(self.card(), encoding="utf-8")
        notices = []
        result = host_sync.project_rules_text(self.vault, "codex", notices=notices)
        self.assertIn(self.document["text"], result)
        self.assertNotIn("- INVALID", result)
        self.assertIn("省略", "\n".join(notices))

    def test_recovery_write_failure_leaves_host_untouched(self):
        self.home = self.root / 'home'
        (self.home / '.codex').mkdir(parents=True)
        target = host_sync.host_path('codex', self.home)
        target.write_bytes(b'Original host text.\n')
        self.target.write_text(self.card(), encoding='utf-8')
        original_write = host_sync.atomic_write

        def write(path, data):
            if path.parent.name == 'host_rule_recovery':
                raise OSError('synthetic state write failure')
            original_write(path, data)

        with patch.object(host_sync, 'contract_vaults', return_value=[self.vault]), \
             patch.object(host_sync, 'project_targets', return_value=([], [])), \
             patch.object(host_sync.host_native, 'apply', return_value=(0, 0)), \
             patch.object(host_sync, 'atomic_write', side_effect=write):
            result = host_sync.apply([self.vault], hosts=['codex'], home=self.home, output=io.StringIO())
        self.assertEqual(result, host_sync.EXIT_REFUSED)
        self.assertEqual(target.read_bytes(), b'Original host text.\n')

    def test_unchanged_apply_repairs_missing_manifest_then_bad_card_recovers(self):
        self.home = self.root / 'home'
        (self.home / '.codex').mkdir(parents=True)
        self.target.write_text(self.card(), encoding='utf-8')
        with patch.object(host_sync, 'contract_vaults', return_value=[self.vault]), \
             patch.object(host_sync, 'project_targets', return_value=([], [])), \
             patch.object(host_sync.host_native, 'apply', return_value=(0, 0)):
            def apply():
                return host_sync.apply([self.vault], hosts=['codex'], home=self.home, output=io.StringIO())
            self.assertEqual(apply(), host_sync.EXIT_OK)
            target = host_sync.host_path('codex', self.home)
            before = target.read_bytes()
            current = host_sync._current_rules(before.decode('utf-8'), 'rules')
            state = host_sync._recovery_path(core_gen.recovery_key(current), self.home)
            state.unlink()
            self.assertEqual(apply(), host_sync.EXIT_OK)
            self.assertTrue(state.is_file())
            self.assertEqual(target.read_bytes(), before)
            self.target.write_text(self.card().replace(self.document['text'], 'INVALID'), encoding='utf-8')
            self.assertEqual(apply(), host_sync.EXIT_OK)
            self.assertIn(self.document['text'], target.read_text(encoding='utf-8'))

    def test_old_renderer_source_path_and_plain_core_id_are_recoverable(self):
        document = copy.deepcopy(self.document)
        document.update(kind="ai-derived", author="synthetic-ai", text="CORE-01. Keep evidence.")
        before = self.card(document)
        self.target.write_text(before, encoding="utf-8")
        old = "## B. resident\n### evidence\n- CORE-01. Prior rule.\n"
        self.target.write_text(before.replace("CORE-01. Keep evidence.", "CORE-01. Tampered."), encoding="utf-8")
        result = host_sync.project_rules_text(self.vault, "codex", current=old)
        self.assertIn("CORE-01. Prior rule.", result)
        old = "## B. resident\n### evidence\n- Prior arbitrary rule.\n來源卡：" + str(self.target) + "\n"
        self.assertIn("Prior arbitrary rule.", host_sync.project_rules_text(self.vault, "codex", current=old))

    def test_old_plain_rule_can_use_the_prior_text_in_its_source_document(self):
        before = self.card()
        self.target.write_text("\n".join('text: "INVALID"' if line.startswith('text:') else line
                                          for line in before.splitlines()) + "\n", encoding="utf-8")
        current = "## B. resident\n### evidence\n- " + self.document["text"] + "\n"
        self.assertIn(self.document["text"], host_sync.project_rules_text(self.vault, "codex", current=current))

    def test_shared_defaults_keep_each_exception_and_source_path(self):
        self.target.write_text(self.card(), encoding="utf-8")
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(errors)
        rows = []
        default_scope = {"level": "global", "project": None, "case": None, "actions": ["*"],
                         "conditions": [], "exceptions": [], "expiry": "unspecified"}
        for index in range(4):
            row = copy.deepcopy(rules[0])
            row.update(path=f"rule-{index}.md", text=f"Rule {index}.")
            row["provenance"].update(status="UNVERIFIED", source_unavailable=True)
            row["projection"]["document"].update(author="claude", scope=copy.deepcopy(default_scope))
            rows.append(row)
        rows[1]["projection"]["document"]["author"] = "codex"
        rows[2]["projection"]["document"]["scope"]["actions"] = ["special action"]
        rows[3]["vault"] = str(self.root / "another-vault")
        text = core_gen.assemble(rows)
        self.assertEqual(text.count("本次尚未完成原文核對"), 1)
        self.assertEqual(text.count("卡片記載作者：codex"), 1)
        self.assertIn("special action", text)
        self.assertIn(rows[3]['vault'], text)
        for row in rows:
            self.assertIn(row["text"], text)

    def test_retaining_the_last_unavailable_rule_keeps_its_shared_author_and_scope(self):
        self.target.write_text(self.card(), encoding="utf-8")
        self.original.path.unlink()
        before = self.project_rules_text(self.vault, "codex")
        self.target.write_text(self.card().replace(self.document["text"], "INVALID"), encoding="utf-8")
        after = self.project_rules_text(self.vault, "codex", current=before)
        self.assertIn("卡片記載作者：owner", after)
        recovered = host_sync._previous_rules(after, 'codex', recovery=self.recovery)
        self.assertEqual(recovered[0]['scope'], self.document['scope'])
        self.assertNotIn('適用範圍（卡片記載）：', after)
        self.assertIn(self.document["text"], after)
        self.assertEqual(self.project_rules_text(self.vault, "codex", current=after), after)

    def test_bad_entry_markers_do_not_swallow_adjacent_prior_rules(self):
        self.target.write_text(self.card().replace(self.document["text"], "INVALID"), encoding="utf-8")
        bad = "## B. resident\n<!-- epitype-rule:" + core_gen.rule_identity(self.target.name) + " 2 1 -->\n- prior\n"
        with self.assertRaisesRegex(ValueError, "epitype-rule"):
            host_sync.project_rules_text(self.vault, "codex", current=bad)

    def test_valid_cards_can_repair_old_entry_metadata_without_recovery(self):
        self.target.write_text(self.card(), encoding="utf-8")
        current = "## B. resident\n<!-- epr:broken -->\n- old\n"
        result = host_sync.project_rules_text(self.vault, "codex", current=current)
        self.assertIn(self.document["text"], result)
        self.assertNotIn("epr:broken", result)

    def test_prior_multiline_text_keeps_headings_bullets_and_trailing_spaces(self):
        prior_text = "First line\n### Embedded heading\n- Bullet\nLast line  "
        before = self.card()
        self.target.write_text(before, encoding="utf-8")
        rows, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(errors)
        rows[0].update(text=prior_text, core_display="plain")
        current = core_gen.host_view(core_gen.assemble(rows, recovery=self.recovery), "codex").rstrip("\n")
        self.assertIn(prior_text, current)
        self.target.write_text("\n".join('text: "INVALID"' if line.startswith('text:') else line
                                          for line in before.splitlines()) + "\n", encoding="utf-8")
        after = self.project_rules_text(self.vault, "codex", current=current)
        self.assertIn(prior_text, after)
        self.assertEqual(self.project_rules_text(self.vault, "codex", current=after), after)

    def test_recovery_reads_scope_from_a_valid_card_and_never_from_display_prose(self):
        self.target.write_text(self.card(), encoding='utf-8')
        before = self.project_rules_text(self.vault, 'codex')
        changed = copy.deepcopy(self.document)
        changed['scope']['conditions'] = ['only before sample delivery']
        self.target.write_text(self.card(changed), encoding='utf-8')
        entry = host_sync._previous_rules(before, 'codex', recovery=self.recovery)[0]
        self.assertEqual(entry['scope'], changed['scope'])
        self.assertIn('only before sample delivery', entry['previous_rendering'])

    def test_invalid_card_scope_cannot_replace_the_last_accepted_scope(self):
        self.target.write_text(self.card(), encoding='utf-8')
        before = self.project_rules_text(self.vault, 'codex')
        self.target.write_text(self.card().replace(self.document['text'], 'UNBOUND CHANGE'), encoding='utf-8')
        entry = host_sync._previous_rules(before, 'codex', recovery=self.recovery)[0]
        self.assertEqual(entry['scope'], self.document['scope'])
        self.assertIn(self.document['text'], entry['previous_rendering'])



if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
