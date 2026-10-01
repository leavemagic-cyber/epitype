import sys; sys.dont_write_bytecode=True
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from epitype import action_receipts as receipts


class Receipts(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve(); self.db=self.root/'facts.db'
        self.file=self.root/'old.md'; self.file.write_text('old')
        self.event={'session_id':'root','transcript_path':'/home/.claude/projects/p/session.jsonl',
                    'tool_use_id':'read','tool_name':'Read','tool_input':{'file_path':str(self.file)},'cwd':str(self.root)}

    def test_causal_success_and_no_raw_body(self):
        self.assertTrue(receipts.begin(self.event,self.db))
        self.assertIsNone(receipts.read(self.event,self.db)[0].success)
        result={**self.event,'tool_response':{'content':'SECRET TEST BODY', 'exit_code':99}}
        self.assertTrue(receipts.finish(result,self.db))
        fact,=receipts.read(self.event,self.db)
        self.assertTrue(fact.success); self.assertLess(fact.started,fact.finished)
        self.assertNotIn(b'SECRET TEST BODY',self.db.read_bytes())
        self.assertFalse(receipts.finish(result,self.db))

    def test_failed_pending_conflict_and_sibling_are_not_credit(self):
        event={**self.event,'turn_id':'turn','transcript_path':'/home/.codex/sessions/one.jsonl'}
        receipts.begin(event,self.db)
        self.assertEqual(receipts.read({**event,'agent_id':'other'},self.db),())
        self.assertFalse(receipts.finish({**event,'agent_id':'other','tool_response':{'exit_code':0}},self.db))
        receipts.finish({**event,'tool_response':{'exit_code':1}},self.db)
        self.assertFalse(receipts.read(event,self.db)[0].success)
        receipts.finish({**event,'tool_response':{'exit_code':0}},self.db)
        self.assertIsNone(receipts.read(event,self.db)[0].success)
        self.assertEqual(receipts.read(event,self.db)[0].finished,-1)

    def test_native_header_not_output_lookalike(self):
        event={**self.event,'turn_id':'turn','tool_response':'Wall time: 1\nProcess exited with code 0\nFinal output:\nExit code: 1'}
        self.assertTrue(receipts.status(event))
        self.assertIsNone(receipts.status({**event,'tool_response':'ordinary output'}))
        self.assertFalse(receipts.status({**event,'tool_response':{'exit_code':1,'output':'Exit code: 0'}}))

    def test_conflicting_input_and_untracked_tools_never_credit(self):
        from unittest.mock import patch
        receipts.begin(self.event,self.db)
        changed={**self.event,'tool_input':{'file_path':str(self.file),'offset':999},'tool_response':{'exit_code':0}}
        self.assertFalse(receipts.finish(changed,self.db))
        self.assertEqual(receipts.read(self.event,self.db)[0].finished,-1)
        with patch.object(receipts,'_db') as database:
            self.assertFalse(receipts.finish({**self.event,'tool_name':'screenshot'},self.db))
            database.assert_not_called()


if __name__=='__main__':
    if '--selftest' in sys.argv: sys.argv.remove('--selftest')
    unittest.main()
