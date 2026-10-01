import sys; sys.dont_write_bytecode=True
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from epitype import action_receipts, action_sequence, action_targets


def fact(identity,kind,target,start,end,success=True,repo='',inventory=()):
    return action_receipts.Fact(identity,start,end,success,(action_targets.Action(kind,target,repo),),inventory=inventory)


class Sequence(unittest.TestCase):
    def test_all_three_prerequisites_require_same_target(self):
        for rule,left,right,repo,inventory in (
            ('@file.read => @file.change','file.read','file.change','',()),
            ('@git.status => @git.main_update','git.status','git.main_update','main',()),
            ('@git.worktrees => @directory.delete','git.worktrees','directory.delete','main',('main','side'))):
            target='main' if right!='directory.delete' else 'side'
            pre='main' if left=='git.worktrees' else target
            action=fact('action',right,target,3,4,repo=repo)
            valid=fact('before',left,pre,1,2,inventory=inventory)
            self.assertEqual(action_sequence.check([rule],[valid,action])[0].verdict,'allow')
            wrong=fact('other',left,'other',1,2,inventory=inventory)
            self.assertEqual(action_sequence.check([rule],[wrong,action])[0].verdict,'block')
            failed=fact('failed',left,pre,1,2,False,inventory=inventory)
            self.assertEqual(action_sequence.check([rule],[failed,action])[0].verdict,'block')

    def test_pending_out_of_order_changed_and_old_violation(self):
        rule='@file.read => @file.change'
        change=fact('action','file.change','file',3,4)
        pending=fact('pre','file.read','file',1,0,None)
        self.assertEqual(action_sequence.check([rule],[pending,change])[0].verdict,'unverified')
        late=fact('pre','file.read','file',1,5)
        self.assertEqual(action_sequence.check([rule],[late,change])[0].verdict,'block')
        old=fact('old','file.read','file',1,2)
        changed=fact('changed','file.change','file',3,4)
        new=fact('new','file.change','file',5,6)
        self.assertEqual(action_sequence.check([rule],[old,changed,new],{'new'})[0].verdict,'allow')
        status=fact('old','git.status','main',1,2)
        merged=fact('changed','git.main_update','main',3,4)
        again=fact('new','git.main_update','main',5,6)
        self.assertEqual(action_sequence.check(['@git.status => @git.main_update'],[status,merged,again],{'new'})[0].verdict,'block')
        self.assertEqual(action_sequence.check([rule],[change],set()),[])

    def test_unknown_relation_and_bad_selector_not_green(self):
        item=fact('x','directory.delete','outside',1,2)
        result,=action_sequence.check(['@git.worktrees => @directory.delete'],[item])
        self.assertEqual(result.verdict,'unverified')
        with self.assertRaises(ValueError): action_sequence.rule('@file.read => @unknown')


if __name__=='__main__':
    if '--selftest' in sys.argv: sys.argv.remove('--selftest')
    unittest.main()
