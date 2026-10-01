import sys; sys.dont_write_bytecode = True
import json
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import tool_evidence


class Evidence(unittest.TestCase):
    def test_both_host_result_ids_cwd_and_cross_turn_evidence(self):
        claude = [{'type':'assistant','cwd':'/project','message':{'content':[
            {'type':'tool_use','id':'a','name':'Read','input':{'file_path':'a.md'}}]}},
            {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'a','is_error':False,'content':'file'}]}},
            {'type':'user','message':{'content':'continue'}}]
        codex = [{'type':'turn_context','payload':{'cwd':'/project'}},
            {'type':'response_item','payload':{'type':'function_call','call_id':'a','name':'exec_command',
                'arguments':json.dumps({'cmd':'git status','workdir':'/other'})}},
            {'type':'response_item','payload':{'type':'function_call_output','call_id':'a',
                'output':json.dumps({'exit_code':0,'output':'clean'})}}]
        for rows,cwd in ((claude,'/project'),(codex,'/other')):
            call, = tool_evidence.calls(rows)
            self.assertTrue(call.success)
            self.assertEqual(call.cwd,cwd)
            self.assertLess(call.started,call.finished)

    def test_pending_error_unknown_and_out_of_order_result(self):
        for output,want in (({'exit_code':1},False),({'exit_code':0},True),('unknown',None),
                            ({'is_error':False,'content':'Exit code: 1'},False)):
            self.assertIs(tool_evidence.outcome(output),want)
        self.assertTrue(tool_evidence.outcome({'type':'tool_result','tool_use_id':'x','content':'read'}))
        self.assertTrue(tool_evidence.outcome({'type':'tool_result','tool_use_id':'x',
            'content':'{"exit_code":1,"is_error":true}'}))
        rows=[{'type':'response_item','payload':{'type':'function_call_output','call_id':'x','output':{'exit_code':0}}},
              {'type':'response_item','payload':{'type':'function_call','call_id':'x','name':'exec_command',
                'arguments':'{"cmd":"test"}'}}]
        call,=tool_evidence.calls(rows)
        self.assertIsNone(call.success)
        self.assertEqual(call.finished,0)

    def test_conflicting_duplicate_ids_do_not_borrow_success(self):
        def call(cmd):
            return {'type':'response_item','payload':{'type':'function_call','call_id':'x','name':'exec_command',
                'arguments':json.dumps({'cmd':cmd})}}
        result={'type':'response_item','payload':{'type':'function_call_output','call_id':'x','output':{'exit_code':0}}}
        item,=tool_evidence.calls([call('read'),result,call('other'),result])
        self.assertIsNone(item.success)
        self.assertEqual(item.finished,-1)


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
