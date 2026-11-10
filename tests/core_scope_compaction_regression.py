"""Compact generated rules retain complete scopes and current text, with history on demand."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import copy
import json
import unittest
from epitype import core_gen as g, host_sync, memspec


class ScopeCompaction(unittest.TestCase):
    def rule(self, number, *, unavailable=False, **changes):
        scope={"level":"project","project":"named-project","case":None,"actions":["review"],
               "conditions":["before delivery"],"exceptions":["current owner changes the requirement"],"expiry":"unspecified"}
        scope.update(changes)
        document={"scope":scope,"author":"codex","key":f"test-{number}","kind":"ai-derived",
                  "quote":"Historical original remains in the source card.","text":f"Current obligation {number}."}
        return {"layer":"resident","order":number,"section":"execution","hosts":(),"vault":"source-vault",
                "path":f"rule-{number}.md","text":document['text'],
                "projection":{"document":document},
                "provenance":{"status":"UNVERIFIED" if unavailable else "VERIFIED","source_unavailable":unavailable}}

    def test_every_scope_reconstructs_including_null_and_different_exceptions(self):
        rows=[self.rule(1),self.rule(2,case="specific",actions=["upload"],conditions=[],exceptions=[]),
              self.rule(3,unavailable=True,level="global",project=None,actions=["*"])]
        original=copy.deepcopy(rows)
        recovery = {}
        rendered = g.assemble(rows, recovery=recovery)
        entries = host_sync._previous_rules(rendered, 'codex', recovery=recovery)
        for row, entry in zip(rows, entries):
            self.assertEqual(entry['scope'], row['projection']['document']['scope'])
            self.assertTrue(entry['previous_rendering'].startswith(row['text'] + '\n'))
            self.assertNotIn('範圍覆寫：', entry['previous_rendering'])
        self.assertEqual(rows,original)

    def test_current_rules_do_not_repeat_historical_quotations(self):
        rows=[self.rule(1),self.rule(2)]
        rendered=g.assemble(rows)
        self.assertNotIn(rows[0]['projection']['document']['quote'],rendered)
        self.assertIn('codex',rendered)
        for row in rows:
            self.assertIn(row['text'],rendered)
        self.assertIn('source-vault', rendered)
        self.assertNotIn('來源卡：', rendered)
        self.assertIn(memspec.CORE_GEN_HISTORY_ON_DEMAND.format(author='codex'),rendered)

    def test_unavailable_is_never_upgraded_to_verified(self):
        row=self.rule(1,unavailable=True)
        rendered=g.assemble([row])
        self.assertIn('UNVERIFIED',rendered)
        self.assertIn('UNVERIFIED：',rendered)
        self.assertNotIn('<!--r:',rendered)
        self.assertNotIn(memspec.CORE_GEN_VERIFIED_SOURCE,rendered)

    def test_held_and_legacy_rules_keep_their_existing_text(self):
        row=self.rule(1);row['previous_rendering']='Retained historical rendering.'
        self.assertIsNone(g._scope_defaults([row]))
        self.assertEqual(g._display_rule(row),row['previous_rendering'])

    def test_project_is_shared_but_conditions_stay_attached_to_each_rule(self):
        rows=[self.rule(i) for i in range(1,8)]
        text=g.assemble(rows)
        self.assertEqual(text.count('current owner changes the requirement'),7)
        self.assertEqual(text.count('before delivery'),7)
        self.assertEqual(text.count('named-project'),1)

    def test_held_rules_expand_their_original_scope_before_base_changes(self):
        rows=[self.rule(1),self.rule(2,case='original-case',exceptions=[])]
        recovery = {}
        previous=host_sync._previous_rules(g.assemble(rows, recovery=recovery),'codex', recovery=recovery)
        for row,entry in zip(rows,previous):
            self.assertEqual(entry['scope'],row['projection']['document']['scope'])
            self.assertIn('適用：', entry['previous_rendering'])
            self.assertFalse(entry['inherits_defaults'])
        held={**rows[1],**previous[1],"path":rows[1]["path"]}
        changed=self.rule(3,project='different-project',conditions=['different condition'])
        next_text=g.assemble([held,changed], recovery=recovery)
        recovered=host_sync._previous_rules(next_text,'codex', recovery=recovery)
        self.assertIn('original-case',recovered[0]['previous_rendering'])
        self.assertNotIn('different-project',recovered[0]['previous_rendering'])

    def test_plain_verified_rules_preserve_scopes_in_compact_rendering(self):
        row=self.rule(1,case='limited-case');row['core_display']='plain'
        recovery = {}
        text=g.assemble([row], recovery=recovery)
        previous=host_sync._previous_rules(text,'codex', recovery=recovery)
        self.assertIn('limited-case',previous[0]['previous_rendering'])


if __name__=='__main__': unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
