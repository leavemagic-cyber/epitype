"""Current content is primary; originals are an explicit, honest audit path."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'adapters/claude'),str(Path(__file__).parent)]
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from epitype import core_gen, memspec, rule_pipeline, rule_provenance, rule_review
import rule_pipeline_regression as fixtures
import rule_source_consumers_regression as consumers


class CurrentContent(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='epitype-current-content-')
        self.addCleanup(temp.cleanup)
        self.vault=Path(temp.name)
        self.path=self.vault/'rule-current.md'
        self.doc={'version':1,'key':'current-rule','kind':'ai-derived','author':'codex',
            'text':'在相關工作前核對現行要求，保留明列例外。',
            'scope':{'level':'project','project':str(self.vault),'case':None,'actions':['*'],
                'conditions':[],'expiry':'unspecified','exceptions':['newer user instructions']},
            'supersedes':[]}

    def card(self, doc=None, suffix='', **fields):
        doc=copy.deepcopy(doc or self.doc)
        parts={'name':'current-rule','description':'current-content review',
            'layer':'resident','section':'execution','order':'1','enforce':'text',
            'approved_by':'codex','approved_at':'2026-11-09',
            'text':json.dumps(doc['text'],ensure_ascii=False),'metadata':'\n  type: rule'}
        parts.update(fields)
        text='---\n'+'\n'.join(f'{k}: {v}' for k,v in parts.items())+'\n---\n'+doc['text']+suffix+'\n'
        doc=rule_pipeline.bind_payload(doc,text,reviewer='codex',
            rationale='Reviewed actual meaning, scope, exceptions and current task context.')
        text=text.replace('---\n','---\nrule_provenance: '+json.dumps(json.dumps(doc,ensure_ascii=False),ensure_ascii=False)+'\n',1)
        self.path.write_text(text,encoding='utf-8')
        return text

    def checked(self,text=None,**options):
        text=text or self.path.read_text(encoding='utf-8')
        f,_=memspec.frontmatter_text(text)
        return rule_pipeline.inspect_card(f,self.path,card_text=text,**options)

    def test_new_rule_needs_no_original_quote_or_locator(self):
        text=self.card()
        with patch.object(rule_provenance,'_original',side_effect=AssertionError('historical IO')):
            checked=self.checked(text)
            self.assertEqual(checked.status,rule_provenance.REVIEWED)
            self.assertEqual(checked.to_dict()['source_audit_status'],'NOT_REQUESTED')
            self.assertFalse(rule_pipeline.admission('',text,'rule',self.path))
            rules,errors=core_gen.collect_rules([self.vault])
            self.assertFalse(errors)
            self.assertEqual(len(rules),1)
            rendered=core_gen.assemble(rules)
            self.assertIn(self.doc['text'],rendered)
            self.assertNotIn('來源已核 VERIFIED',rendered)
            self.assertNotIn('原始 Owner 引句',rendered)

    def test_missing_large_or_wrong_historic_locator_does_not_block_current_content(self):
        doc=copy.deepcopy(self.doc)
        doc.update(quote='歷史備查句',sources=[{'path':str(self.vault/'missing.jsonl'),
            'byte_offset':900_000_000,'record_sha256':'0'*64,'role':'U','session_id':'old'}])
        text=self.card(doc)
        with patch.object(rule_provenance,'_original',side_effect=AssertionError('historical IO')):
            self.assertTrue(self.checked(text).verified)
            self.assertFalse(rule_pipeline.admission('',text,'rule',self.path))
        audit=self.checked(text,audit_originals=True)
        self.assertFalse(audit.verified)

    def test_changed_content_or_execution_fields_requires_current_review(self):
        text=self.card()
        changed=text.replace('enforce: text','enforce: text\non_hit: block')
        self.assertEqual(self.checked(changed).status,rule_provenance.INVALID)
        self.assertTrue(rule_pipeline.admission(text,changed,'rule',self.path))

    def test_wrong_attribution_and_scope_still_fail(self):
        doc=copy.deepcopy(self.doc);doc['author']='owner'
        self.assertFalse(self.checked(self.card(doc)).verified)
        doc=copy.deepcopy(self.doc);doc['scope']['level']='global'
        self.assertFalse(self.checked(self.card(doc)).verified)

    def test_ai_pattern_does_not_gain_blocking_authority(self):
        doc=copy.deepcopy(self.doc);doc['text']='禁止在未核對時繼續全部工作。'
        checked=self.checked(self.card(doc,on_hit='block'))
        self.assertTrue(checked.verified)
        self.assertFalse(checked.blocking_basis)
        self.assertEqual(checked.restriction_review,'ADVISORY_ONLY')

    def test_expiry_and_retirement_still_apply(self):
        doc=copy.deepcopy(self.doc);doc['scope']['expiry']='2020-01-01T00:00:00+00:00'
        text=self.card(doc);f,_=memspec.frontmatter_text(text)
        projected=rule_pipeline.projection(f,self.path)
        self.assertTrue(projected['expired'])
        self.assertEqual(rule_pipeline.render_projection(projected),'')
        text=self.card(status='superseded',superseded_by='missing.md')
        self.assertFalse(self.checked(text).verified)

    def test_claims_are_not_forced_to_read_transcript(self):
        with patch.object(rule_provenance,'first_authorship',side_effect=AssertionError('historical IO')):
            self.assertIsNone(rule_provenance.claim_violation('Owner 說「照現在的要求處理」。',self.vault/'old.jsonl'))
        claim={'kind':'ai-derived','author':'owner','text':'偽裝文字'}
        self.assertIsNotNone(rule_provenance.claim_violation('<!-- EPITYPE CLAIM '+json.dumps(claim)+' -->'))

    def test_explicit_audit_retains_original_role_and_hash_checks(self):
        fixture=fixtures.RulePipeline(methodName='test_real_write_gate_both_payloads_accept_source_bound_rule')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        text=fixture.card();fixture.target.write_text(text,encoding='utf-8')
        f,_=memspec.frontmatter_text(text)
        self.assertTrue(rule_pipeline.inspect_card(f,fixture.target,audit_originals=True).verified)
        raw=fixture.original.path.read_bytes()
        fixture.original.path.write_bytes(raw.replace(b'"role": "user"',b'"role": "tool"'))
        self.assertFalse(rule_pipeline.inspect_card(f,fixture.target,audit_originals=True).verified)
        self.assertEqual(rule_pipeline.inspect_card(f,fixture.target).status,rule_provenance.REVIEWED)

    def test_actual_write_entries_both_hosts_do_not_reopen_originals(self):
        fixture=fixtures.RulePipeline(methodName='test_real_write_gate_both_payloads_accept_source_bound_rule')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        text=self.card()
        with patch.object(rule_provenance,'_original',side_effect=AssertionError('historical IO')):
            for host in ('claude','codex'):
                value=fixture.write_call(text,host)
                self.assertNotEqual((value or {}).get('hookSpecificOutput',{}).get('permissionDecision'),'deny')

    def test_stop_pretool_and_compaction_use_current_content_without_source_io(self):
        fixture=consumers.SourceConsumers(methodName='test_pattern_only_ai_guard_reminds_but_owner_guard_can_block')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        text=fixture.card().replace('---\n','---\nguard_tool: Bash\nguard_all_of: [badold]\non_hit: block\n',1)
        fields,_=memspec.frontmatter_text(text)
        before=fields['rule_provenance']
        doc=rule_pipeline.bind_payload(rule_provenance.parse(fields),text,reviewer='synthetic-reviewer')
        text=text.replace(json.dumps(before,ensure_ascii=False),json.dumps(json.dumps(doc,ensure_ascii=False),ensure_ascii=False),1)
        fixture.target.write_text(text,encoding='utf-8')
        with patch.object(rule_provenance,'_original',side_effect=AssertionError('historical IO')):
            found=fixture.decisions()
            self.assertEqual(len(found),1)
            self.assertEqual(found[0].source_status,'REVIEWED')
            self.assertEqual(found[0].source_original_quote,'')
            self.assertIn('badold',fixture.restore())
            self.assertEqual(consumers.decision_lint.lint_vault(fixture.vault).exit_code,0)
            import time
            result=consumers.pretool._handle({'tool_name':'Bash','tool_input':{'command':'echo badold'},
                'session_id':'current-review-synthetic'},time.monotonic(),[])
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')


if __name__=='__main__':unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
