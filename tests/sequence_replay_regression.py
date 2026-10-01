import sys;sys.dont_write_bytecode=True
import json
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from epitype import sequence_replay


class Replay(unittest.TestCase):
    def write(self,rows):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'record.jsonl';path.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
        return sequence_replay.read(path)

    def call(self,id,name,args,result,**extra):
        return [{'type':'assistant','cwd':'C:/history/main','message':{'content':[{'type':'tool_use','id':id,'name':name,'input':args}]},**extra},
                {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':id,'content':result}]},**extra}]

    def test_historical_inventory_not_current_filesystem_identifies_main(self):
        rows=self.call('list','Bash',{'command':'git worktree list --porcelain'},'worktree C:/history/main\nHEAD abc\n\nworktree C:/history/side\nHEAD def\n')
        rows+=self.call('status','Bash',{'command':'git status --short'},'')
        rows+=self.call('merge','Bash',{'command':'git merge side'},'updated')
        result=sequence_replay.judgments(['@git.status => @git.main_update'],self.write(rows))
        self.assertEqual([j.verdict for j in result],['allow'])
        rows=self.call('merge','Bash',{'command':'git merge side'},'updated')
        result=self.write(rows)
        self.assertEqual(result.opaque,('merge',))
        self.assertEqual(sequence_replay.judgments(['@git.status => @git.main_update'],result),[])

    def test_unexecuted_program_and_missing_inner_receipt_not_green(self):
        rows=self.call('outer','exec',{'input':'if(false) await tool.read()'},'')
        rows+=self.call('change','Edit',{'file_path':'a.md'},'changed')
        self.assertEqual(sequence_replay.judgments(['@file.read => @file.change'],self.write(rows))[0].verdict,'unverified')
        rows=self.call('read','Read',{'file_path':'a.md'},'{"exit_code":1}')+self.call('change','Edit',{'file_path':'a.md'},'changed')
        self.assertEqual(sequence_replay.judgments(['@file.read => @file.change'],self.write(rows))[0].verdict,'allow')

    def test_new_file_and_other_actor_are_not_bad_evidence(self):
        rows=self.call('write','Write',{'file_path':'new.md'},'created')
        rows[-1]['toolUseResult']={'type':'create'}
        self.assertEqual(sequence_replay.judgments(['@file.read => @file.change'],self.write(rows)),[])
        rows=self.call('read','Read',{'file_path':'a.md'},'read',agentId='other')+self.call('edit','Edit',{'file_path':'a.md'},'changed')
        self.assertEqual(sequence_replay.judgments(['@file.read => @file.change'],self.write(rows))[0].verdict,'block')

    def test_nightly_counts_unknowns_and_window_without_false_zero(self):
        from epitype import compliance
        import time
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'rows.jsonl'
        rows=self.call('read','Read',{'file_path':'a.md'},'read')+self.call('edit','Edit',{'file_path':'a.md'},'changed')
        for row in rows:row['timestamp']='2026-09-30T00:00:00Z'
        path.write_text('\n'.join(json.dumps(row) for row in rows),encoding='utf-8')
        card=compliance.Rule('read-first','sequence',path,0,[], '', '', '', ('@file.read => @file.change',))
        report=sequence_replay.report([card],[path],since=0)
        self.assertEqual(report['counts'],{'allow':1})
        self.assertTrue(report['complete'])
        self.assertEqual(sequence_replay.report([card],[path],since=2000000000)['counts'],{})
        expired=sequence_replay.report([card],[path],deadline=time.monotonic()-1)
        self.assertFalse(expired['complete']);self.assertEqual(expired['read_files'],0)

    def test_nightly_reader_scope_and_opaque_zero_are_explicit(self):
        from epitype import compliance
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'rows.jsonl'
        rows=self.call('outer','exec',{'input':'if(false) await tool.read()'},'')
        rows+=self.call('write','Write',{'file_path':'unknown.md'},'written')
        rows+=self.call('edit','Edit',{'file_path':'a.md'},'changed',agentId='worker')
        for row in rows: row['timestamp']='2026-09-30T00:00:00Z'
        path.write_text('\n'.join(json.dumps(row) for row in rows),encoding='utf-8')
        card=compliance.Rule('root-only','sequence',path,0,[], '', '', '', ('@file.read => @file.change',),'root')
        report=sequence_replay.report([card],[path],since=0)
        self.assertEqual(report['counts'],{})
        self.assertEqual(report['opaque_calls'],1)
        self.assertEqual(report['unresolved_writes'],1)
        worker=sequence_replay.report([card._replace(audience='sub')],[path],since=0)
        self.assertEqual(worker['counts'],{'block':1})


if __name__=='__main__':
    if '--selftest' in sys.argv:sys.argv.remove('--selftest')
    unittest.main()
