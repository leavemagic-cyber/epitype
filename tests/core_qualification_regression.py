"""A configured promotion threshold counts independent projects, not aliases or incidents."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
import tempfile
import unittest
from unittest.mock import patch
from epitype import core_gen, core_qualification as q, host_sync


class CoreQualification(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.projects=[{'id':str(i),'root':str(self.root/str(i)),'reason':f'Applicable to independent workflow {i}.'} for i in range(3)]

    def fields(self,projects=None):
        return {'core_qualification':json.dumps({'version':1,'projects':self.projects if projects is None else projects,
            'reviewed_by':'test-reviewer','reviewed_at':'2026-11-09'})}

    def test_three_independent_projects_pass_and_two_do_not(self):
        with patch('epitype.fixed_load.identity',side_effect=lambda p:str(p)):
            self.assertEqual(q.inspect(self.fields(),3,{})['distinct_projects'],3)
            with self.assertRaises(ValueError):q.inspect(self.fields(self.projects[:2]),3,{})

    def test_aliases_and_worktrees_cannot_raise_the_project_count(self):
        with patch('epitype.fixed_load.identity',return_value='same-common-git-dir'):
            with self.assertRaises(ValueError):q.inspect(self.fields(),3,{})
        self.projects[2]['id']=self.projects[0]['id']
        with patch('epitype.fixed_load.identity',side_effect=lambda p:str(p)):
            with self.assertRaises(ValueError):q.inspect(self.fields(),3,{})

    def test_roots_are_resolved_once_per_collection(self):
        identities={}
        with patch('epitype.fixed_load.identity',side_effect=lambda p:str(p)) as lookup:
            q.inspect(self.fields(),3,identities);q.inspect(self.fields(),3,identities)
        self.assertEqual(lookup.call_count,3)

    def test_missing_reason_or_explicit_invalid_policy_is_not_accepted(self):
        for value in (True,-1,'3',None):
            with self.assertRaises(ValueError):q.minimum({q.CONFIG_FIELD:value})
        self.assertEqual(q.minimum({}),0)
        self.projects[0]['reason']=''
        with self.assertRaises(ValueError):q.inspect(self.fields(),3,{})

    def test_global_core_requires_evidence_but_project_rules_do_not(self):
        card=self.root/'rule.md'
        card.write_text('---\nname: test\nlayer: resident\ntext: Keep the current requirement.\nmetadata:\n  type: rule\n---\nbody\n',encoding='utf-8')
        projection={'expired':False,'text':'Keep the current requirement.','provenance':{'scope':{'level':'global'}},
                    'source_sha256':'source','authorship':'AI','kind':'ai-derived','supersedes':[]}
        with patch('epitype.memsearch.scan_cards',return_value=[('rule.md',card,0,0)]), \
                patch('epitype.rule_pipeline.projection',return_value=projection):
            rules,errors=core_gen.collect_rules([self.root],minimum_projects=3)
            self.assertEqual(rules,[]);self.assertTrue(errors)
            projection['provenance']['scope']['level']='project'
            rules,errors=core_gen.collect_rules([self.root],minimum_projects=3)
            self.assertEqual(len(rules),1);self.assertEqual(errors,[])

    def test_previous_core_does_not_grandfather_missing_project_qualification(self):
        previous='## B. resident\n### test\n<!--r:'+core_gen.rule_identity('rule.md')+' 1 1-->\n- Old unqualified rule.\n'
        failures=[(str(self.root),'rule.md','[core qualification] missing evidence',{'text':'Old unqualified rule.'})]
        def collect(vaults,failures=None,**kwargs):
            failures.extend([(str(self.root),'rule.md','[core qualification] missing evidence',{})])
            return [],['missing evidence']
        notices=[]
        with patch.object(core_gen,'collect_rules',side_effect=collect):
            output=host_sync._rules_text([self.root],'codex',previous,notices,minimum_projects=3)
        self.assertNotIn('Old unqualified rule.',output)
        self.assertTrue(notices)


if __name__=='__main__':unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
