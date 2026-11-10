import sys;sys.dont_write_bytecode=True
import json
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import recall_selection_regression as lookup_fixture
import current_content_review_regression as current_fixture
from epitype import memsearch, memspec, rule_provenance
import _hook_common as common
import recall_hook as recall

class DeliveryUnitRegression(unittest.TestCase):
    setUp=lookup_fixture.RecallSelectionRegression.setUp
    card=lookup_fixture.RecallSelectionRegression.card
    invoke=lookup_fixture.RecallSelectionRegression.invoke

    def test_third_position_description_upgrades_to_unseen_excerpt_then_stops_repeating(self):
        vault=self.vaults[0]
        names=['first','second','target']
        for name in names:
            self.card(vault,name,'sharedneedle')
            path=vault/(name+'.md')
            path.write_text(path.read_text(encoding='utf-8')+(
                'upgradeneedle unseen required detail.\n' if name=='target'
                else 'sharedneedle first supplied detail.\n'),encoding='utf-8')
        def candidates(location,prompt,**kwargs):
            chosen=['target'] if 'upgradeneedle' in prompt else names
            rows=[]
            if Path(location)==vault:
                for name in chosen:
                    rows.append({'name':name,'description':'sharedneedle','card_path':name+'.md',
                      'path':str(vault/(name+'.md')),'hit_fields':['description'],'matched_term_count':1})
            return {'results':rows,'terms':[prompt]}
        with patch.object(memsearch,'recall_index',candidates):
            first=self.invoke('partial-upgrade','sharedneedle')
            self.assertIn('target.md',first)
            self.assertNotIn('unseen required detail',first)
            second=self.invoke('partial-upgrade','upgradeneedle')
            self.assertIn('unseen required detail',second)
            self.assertEqual(self.invoke('partial-upgrade','upgradeneedle'),'')

    def test_source_projection_still_delivers_previously_unseen_card_prose(self):
        fixture=current_fixture.CurrentContent(methodName='test_new_rule_needs_no_original_quote_or_locator')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.card(layer='recall',suffix='\nCURRENT_DETAIL_MARKER required operative explanation.\n')
        config=fixture.vault/'config.json'
        common.write_config(config,[fixture.vault])
        memsearch.build_index(fixture.vault)
        from epitype import rule_provenance
        with patch.dict(__import__('os').environ,{memspec.EPITYPE_CONFIG_ENV:str(config)}), \
             patch.object(rule_provenance,'_original',side_effect=AssertionError('historical conversation IO')):
            first=self.invoke('source-prose','current-content',cwd=str(fixture.vault))
            self.assertIn(fixture.doc['text'],first)
            self.assertEqual(first.count(fixture.doc['text']),1)
            self.assertIn('newer user instructions',first)
            second=self.invoke('source-prose','CURRENT_DETAIL_MARKER',cwd=str(fixture.vault))
            self.assertIn('required operative explanation',second)
            self.assertEqual(self.invoke('source-prose','CURRENT_DETAIL_MARKER',cwd=str(fixture.vault)),'')

    def test_skipped_first_two_units_do_not_consume_third_cards_excerpt_quota(self):
        vault=self.vaults[0]
        names=['first','second','target']
        for name in names:
            self.card(vault,name,'sharedneedle')
            path=vault/(name+'.md')
            detail='required third detail' if name=='target' else 'already supplied detail'
            path.write_text(path.read_text(encoding='utf-8')+'\nsharedneedle '+detail+'\n',encoding='utf-8')
        def candidates(location,prompt,**kwargs):
            rows=[{'name':name,'description':'sharedneedle','card_path':name+'.md',
                   'path':str(vault/(name+'.md')),'hit_fields':['description'],'matched_term_count':1}
                  for name in names] if Path(location)==vault else []
            return {'results':rows,'terms':['sharedneedle']}
        with patch.object(memsearch,'recall_index',candidates):
            self.assertNotIn('required third detail',self.invoke('quota-progress','sharedneedle'))
            self.assertIn('required third detail',self.invoke('quota-progress','sharedneedle'))
            self.assertEqual(self.invoke('quota-progress','sharedneedle'),'')

    def test_owner_quote_does_not_replace_current_exception_in_first_delivery(self):
        import copy
        fixture=current_fixture.CurrentContent(methodName='test_new_rule_needs_no_original_quote_or_locator')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        document=copy.deepcopy(fixture.doc)
        document.update(kind='owner-directive',author='owner',text='好，照這樣做。',quote='好，照這樣做。')
        fixture.card(doc=document,layer='recall',description='EMERGENCY_NEEDLE 緊急清理仍須保留基本程式。',
            suffix='\nEMERGENCY_NEEDLE 必須保留維持電腦運作與重啟所需的基本程式。\n')
        config=fixture.vault/'config.json'
        common.write_config(config,[fixture.vault]);memsearch.build_index(fixture.vault)
        with patch.dict(__import__('os').environ,{memspec.EPITYPE_CONFIG_ENV:str(config)}):
            first=self.invoke('owner-exception','EMERGENCY_NEEDLE',cwd=str(fixture.vault))
            self.assertIn('緊急清理仍須保留基本程式',first)
            self.assertIn('AI整理',first)
            self.assertNotIn(document['quote'],first)
            self.assertIn('保留維持電腦運作與重啟所需的基本程式',first)
            self.assertEqual(self.invoke('owner-exception','EMERGENCY_NEEDLE',cwd=str(fixture.vault)),'')
        english=common.run_synthetic(Path(recall.__file__),
            {'session_id':'owner-exception-en','prompt':'EMERGENCY_NEEDLE','cwd':str(fixture.vault)},config,
            environment={memspec.EPITYPE_LANG_ENV:'en','TEMP':str(self.root),'TMP':str(self.root),'TMPDIR':str(self.root)})
        self.assertEqual(english.returncode,0,english.stderr)
        english_context=json.loads(english.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('Current card explanation (AI wording; recorded author: owner)',english_context)
        self.assertIn('緊急清理仍須保留基本程式',english_context)
        self.assertNotIn(document['quote'],english_context)

    def test_supersession_legend_never_claims_omitted_history_is_absent_in_either_language(self):
        from verification_deadline_regression import VerificationDeadlineRegression
        fixture=VerificationDeadlineRegression(methodName='test_exhaustion_is_unverified_and_current_text_is_available_for_lookup')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.chain()
        fields,_=memspec.frontmatter_fields(fixture.path)
        fixture.card(doc=json.loads(fields[rule_provenance.FIELD]),layer='recall')
        config=fixture.vault/'config.json'
        common.write_config(config,[fixture.vault]);memsearch.build_index(fixture.vault)
        for language,notice,wrong in (
            ('zh-TW','取代關係見來源卡','未標取代＝無'),
            ('en','supersession details are in the source card','omitted supersession = none'),
        ):
            with self.subTest(language=language):
                result=common.run_synthetic(Path(recall.__file__),
                    {'session_id':'supersession-legend-'+language,'prompt':'current-content','cwd':str(fixture.vault)},config,
                    environment={memspec.EPITYPE_LANG_ENV:language,'TEMP':str(self.root),'TMP':str(self.root),'TMPDIR':str(self.root)})
                self.assertEqual(result.returncode,0,result.stderr)
                context=json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
                self.assertIn(fixture.doc['text'],context)
                self.assertIn(notice,context)
                self.assertNotIn(wrong,context)

    def test_reviewed_body_change_updates_effective_version_and_delivery(self):
        from epitype import rule_pipeline
        fixture=current_fixture.CurrentContent(methodName='test_new_rule_needs_no_original_quote_or_locator')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        before=fixture.card(layer='recall',suffix='\nBODY_NEEDLE first operative exception.\n')
        old=rule_pipeline.effective_version(before)
        config=fixture.vault/'config.json'
        common.write_config(config,[fixture.vault]);memsearch.build_index(fixture.vault)
        with patch.dict(__import__('os').environ,{memspec.EPITYPE_CONFIG_ENV:str(config)}):
            self.assertIn('first operative exception',self.invoke('body-change','BODY_NEEDLE',cwd=str(fixture.vault)))
            after=fixture.card(layer='recall',suffix='\nBODY_NEEDLE revised operative exception.\n')
            self.assertNotEqual(old,rule_pipeline.effective_version(after))
            self.assertIn('revised operative exception',self.invoke('body-change','BODY_NEEDLE',cwd=str(fixture.vault)))

    def test_deferred_status_is_refreshed_after_verification_completes(self):
        from epitype import rule_pipeline
        fixture=current_fixture.CurrentContent(methodName='test_new_rule_needs_no_original_quote_or_locator')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.card(layer='recall')
        config=fixture.vault/'config.json'
        common.write_config(config,[fixture.vault]);memsearch.build_index(fixture.vault)
        with patch.dict(__import__('os').environ,{memspec.EPITYPE_CONFIG_ENV:str(config)}):
            with patch.object(rule_pipeline,'supersession_issues',
                              side_effect=rule_pipeline.VerificationDeferred('fixture deadline')):
                first=self.invoke('verification-refresh','current-content',cwd=str(fixture.vault))
            self.assertIn('未核',first)
            second=self.invoke('verification-refresh','current-content',cwd=str(fixture.vault))
            self.assertIn(fixture.doc['text'],second)
            self.assertNotIn('未核',second)

    def test_draft_delivery_filter_does_not_silence_lint_errors(self):
        from epitype import card_lint
        fixture=current_fixture.CurrentContent(methodName='test_new_rule_needs_no_original_quote_or_locator')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        for status in ('draft','deprecated'):
            fixture.card(status=status,enforce='invalid-tier')
            fields,_=memspec.frontmatter_fields(fixture.path)
            self.assertFalse(memspec.card_is_current(fields))
            _, findings=card_lint.check_card(fixture.path,fixture.path.name)
            self.assertTrue(any(level==card_lint.FAIL and field=='enforce' for level,field,_ in findings),findings)

if __name__=='__main__':unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
