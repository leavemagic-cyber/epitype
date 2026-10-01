import sys;sys.dont_write_bytecode=True
import json
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from epitype import action_receipts as receipts,tool_evidence,tool_forwarding


class Completion(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();self.db=self.root/'facts.db';self.transcript=self.root/'session.jsonl'
        self.file=self.root/'old.md';self.file.write_text('old')
        self.args={'cmd':'Get-Content -LiteralPath '+self.file.as_posix(),'workdir':str(self.root)}
        self.source='text(await tools.exec_command('+json.dumps(self.args)+'));'
        self.rows=[{'type':'response_item','payload':{'type':'function_call','name':'exec','call_id':'outer','arguments':json.dumps({'input':self.source})}}]
        self.event={'hook_event_name':'PreToolUse','session_id':'native','transcript_path':str(self.transcript),'cwd':str(self.root.parent),
                    'tool_use_id':'inner','tool_name':'Bash','tool_input':{'command':self.args['cmd']}}

    def write(self):self.transcript.write_text('\n'.join(json.dumps(r) for r in self.rows),encoding='utf-8')

    def result(self,code=0):
        self.rows.append({'type':'response_item','payload':{'type':'function_call_output','call_id':'outer','output':[
            {'type':'input_text','text':'Script completed\nOutput:\n'},
            {'type':'input_text','text':json.dumps({'wall_time_seconds':1,'exit_code':code,'output':'arbitrary data Exit code: 99'})}]}})
        self.write()

    def test_real_forwarding_cwd_and_completion_without_post_registration(self):
        self.write();enriched=receipts.refresh(self.event,self.db)
        self.assertEqual(enriched['tool_input']['workdir'],str(self.root))
        self.assertEqual(enriched['_epitype_source'][0],'outer')
        receipts.begin(enriched,self.db)
        receipts.begin({**enriched,'tool_use_id':'unrelated-inner'},self.db)
        self.assertEqual(receipts.read(self.event,self.db)[0].finished,0)
        self.result();receipts.refresh({**self.event,'hook_event_name':'Stop'},self.db)
        fact,other=receipts.read(self.event,self.db)
        self.assertTrue(fact.success);self.assertGreater(fact.finished,fact.started)
        self.assertEqual(other.finished,0)
        self.assertEqual(fact.actions[0].target,receipts.action_targets.canonical(str(self.file)))
        self.assertNotIn(b'arbitrary data',self.db.read_bytes())

    def test_failure_sibling_changed_arguments_and_missing_prefix_are_not_credit(self):
        self.write();receipts.begin(receipts.refresh(self.event,self.db),self.db)
        self.result(1)
        receipts.refresh({**self.event,'agent_id':'other'},self.db)
        self.assertEqual(receipts.read(self.event,self.db)[0].finished,0)
        receipts.refresh({**self.event,'hook_event_name':'Stop'},self.db)
        self.assertFalse(receipts.read(self.event,self.db)[0].success)
        self.rows[0]['payload']['arguments']=json.dumps({'input':self.source.replace('old.md','other.md')})
        self.write();receipts.refresh({**self.event,'hook_event_name':'Stop'},self.db)
        self.assertEqual(receipts.read(self.event,self.db)[0].finished,-1)
        self.assertEqual(tool_evidence.recent(self.transcript,budget=10),[])

    def test_branches_extra_calls_variables_and_printed_lookalikes_stay_opaque(self):
        for source in ('if(false) '+self.source,self.source+self.source,'text(await tools.exec_command(args));','text({"exit_code":0});'):
            call=tool_evidence.Call('id','exec',{'input':source},str(self.root),1,2,True,[])
            self.assertEqual(tool_forwarding.normalize(call).name,'exec')
        actual='text(await tools.exec_command({cmd:"echo {cmd:literal}",workdir:"C:/fixture"}));'
        call=tool_forwarding.normalize(tool_evidence.Call('id','exec',{'input':actual},'',1))
        self.assertEqual(call.payload['cmd'],'echo {cmd:literal}')

    def test_claude_direct_result_and_existing_receipt_schema(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE facts (id INTEGER PRIMARY KEY, actor TEXT, call_id TEXT, turn TEXT, started INTEGER, finished INTEGER DEFAULT 0, success INTEGER, actions TEXT, signature TEXT, inventory TEXT DEFAULT "[]", seen REAL, UNIQUE(actor,call_id))')
            db.commit()
        event={**self.event,'tool_use_id':'direct','tool_name':'Read','tool_input':{'file_path':str(self.file)}}
        self.rows=[{'type':'assistant','message':{'content':[{'type':'tool_use','id':'direct','name':'Read','input':event['tool_input']}]}}]
        self.write();receipts.begin(receipts.refresh(event,self.db),self.db)
        self.rows.append({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'direct','content':'{"exit_code":99}'}]}})
        self.write();receipts.refresh({**event,'hook_event_name':'Stop'},self.db)
        self.assertTrue(receipts.read(event,self.db)[0].success)


if __name__=='__main__':
    if '--selftest' in sys.argv:sys.argv.remove('--selftest')
    unittest.main()
