"""Actual subprocess isolation preserves live inputs and source rejection."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import importlib.util,json,os,tempfile,unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('suite_environment_runner',ROOT/'tests/run_all.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)

class SelftestEnvironment(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='epitype-harness-regression-')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve()
        self.live=self.root/'live-config.json';self.live.write_text('{"sentinel":"preserve"}\n',encoding='utf-8')
        self.environment=dict(os.environ,EPITYPE_CONFIG=str(self.live),EPITYPE_SUITE_PARENT_PROBE='preserve')

    def probe(self,name='probe.py',fail=False):
        source='''import json,os,sys
from pathlib import Path
sys.path.insert(0,ROOT)
from epitype import memspec,rule_pipeline
cfg=Path(os.environ['EPITYPE_CONFIG']);home=Path.home();temp=Path(os.environ['TEMP'])
card=home/'unproved-rule.md'
issues=rule_pipeline.admission('', '---\\ntype: rule\\ntext: Never guess.\\n---\\nNever guess.\\n', 'rule', card)
print(json.dumps({'config':str(cfg),'temp':str(temp),'home':str(home),'options':memspec.config_options(),
    'code_home':os.environ['CODEX_HOME'],'parent_probe':os.environ['EPITYPE_SUITE_PARENT_PROBE'],
    'new_rule_refused':bool(issues),'all_issues':issues}))
sys.exit(EXIT)
'''.replace('ROOT',repr(str(ROOT))).replace('EXIT','1' if fail else '0')
        path=self.root/name;path.write_text(source,encoding='utf-8')
        outcome,error=runner._run_selftest(self.root,Path(name),self.environment)
        self.assertIsNone(error);self.assertIsNotNone(outcome)
        return outcome,json.loads(outcome.stdout)

    def test_actual_child_ignores_live_config_and_preserves_new_rule_gate(self):
        before=self.live.read_bytes();outcome,data=self.probe()
        self.assertEqual(outcome.returncode,0,outcome.stderr)
        self.assertEqual(data['options'],{})
        self.assertTrue(Path(data['config']).is_relative_to(Path(data['temp'])))
        self.assertEqual(Path(data['config']),Path(data['home'])/'.epitype/config.json')
        self.assertEqual(Path(data['code_home']),Path(data['home'])/'.codex')
        self.assertEqual(data['parent_probe'],'preserve')
        self.assertTrue(data['new_rule_refused'],data['all_issues'])
        self.assertEqual(self.live.read_bytes(),before)
        self.assertEqual(self.environment['EPITYPE_CONFIG'],str(self.live))
        self.assertFalse(Path(data['temp']).exists())

    def test_independent_children_and_failure_cleanup(self):
        first,a=self.probe('first.py');second,b=self.probe('second.py',fail=True)
        self.assertEqual(first.returncode,0);self.assertEqual(second.returncode,1)
        self.assertNotEqual(a['home'],b['home'])
        self.assertFalse(Path(a['temp']).exists());self.assertFalse(Path(b['temp']).exists())
        self.assertEqual(self.live.read_text(encoding='utf-8'),'{"sentinel":"preserve"}\n')

if __name__=='__main__':
    if '--selftest' in sys.argv:sys.argv.remove('--selftest')
    unittest.main()
