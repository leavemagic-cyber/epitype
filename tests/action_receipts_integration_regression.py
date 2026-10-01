import sys; sys.dont_write_bytecode=True
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import pretooluse_gate as pretool
import stop_gate
from epitype import action_receipts


class Integration(unittest.TestCase):
    setUp=fixtures.ActionGuardRegression.setUp
    write_card=fixtures.ActionGuardRegression.write_card

    def invoke(self,event):
        with patch.object(pretool,'read_event',return_value=event), patch.object(pretool,'_handle',return_value=None), patch.object(pretool,'_best_effort_record'):
            self.assertEqual(pretool.main(),0)

    def test_actual_inner_receipt_and_stop_both_hosts(self):
        self.write_card('read.md',name='read-first',require_tool_before=['@file.read => @file.change'],audience='root')
        file=self.root/'old.md'; file.write_text('old')
        for host in ('Claude','Codex'):
            for allowed in (False,True):
                session=f'{host}-{allowed}-{self.root.name}'; path=self.root/f'{session}.jsonl'
                meta={'session_id':session,'cwd':str(self.root),'transcript_path':str(path)}
                if host=='Claude':
                    meta['transcript_path']=str(self.root/'.claude'/f'{session}.jsonl'); path=Path(meta['transcript_path']);path.parent.mkdir(exist_ok=True)
                    rows=[{'type':'user','message':{'content':'do it'}}, {'type':'assistant','message':{'content':[{'type':'tool_use','id':'edit','name':'Edit','input':{'file_path':str(file)}}]}}]
                else:
                    meta['turn_id']='turn-1'
                    # The outer JS transcript does not expose the completed inner Edit.
                    rows=[{'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'do it'}]}},
                          {'type':'response_item','payload':{'type':'function_call','call_id':'outer','name':'functions_exec','arguments':'{"code":"await tool()"}'}}]
                path.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
                for identity,name in ([('read','Read')] if allowed else [])+[('edit','Edit')]:
                    event={**meta,'tool_use_id':identity,'tool_name':name,'tool_input':{'file_path':str(file)},'hook_event_name':'PreToolUse'}
                    self.invoke(event)
                    self.invoke({**event,'hook_event_name':'PostToolUse','tool_response':{'exit_code':0}})
                result=stop_gate._handle({**meta,'last_assistant_message':'done'},time.monotonic(),[])
                self.assertEqual(result is None,allowed,(host,allowed,result))
                # Historical violations do not permanently block a fresh owner turn.
                if host=='Claude': rows.append({'type':'user','message':{'content':'continue'}})
                else:
                    rows.append({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'continue'}]}});meta['turn_id']='turn-2'
                path.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
                self.assertIsNone(stop_gate._handle({**meta,'last_assistant_message':'next'},time.monotonic(),[]))

    def test_screenshot_fast_path_and_missing_result_are_explicit(self):
        with patch.object(pretool,'read_event',return_value={'hook_event_name':'PostToolUse','tool_name':'screenshot'}),patch.object(pretool,'load_config') as config:
            self.assertEqual(pretool.main(),0);config.assert_not_called()
        self.write_card('read.md',name='read-first',require_tool_before=['@file.read => @file.change'])
        path=self.root/'missing.jsonl';path.write_text('\n'.join(json.dumps(r) for r in [{'type':'user','message':{'content':'do it'}},
            {'type':'assistant','message':{'content':[{'type':'tool_use','id':'x','name':'Edit','input':{'file_path':'a.md'}}]}}]))
        defects=[]
        self.assertIsNone(stop_gate._handle({'session_id':'missing','transcript_path':str(path),'last_assistant_message':'done'},time.monotonic(),defects))
        self.assertTrue(any('UNVERIFIED' in defect for defect in defects))


if __name__=='__main__':
    if '--selftest' in sys.argv: sys.argv.remove('--selftest')
    unittest.main()
