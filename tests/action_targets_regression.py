import sys; sys.dont_write_bytecode=True
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from epitype import action_targets as targets


class Targets(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.main=self.root/'main'; self.side=self.root/'side'
        (self.main/'.git/worktrees/side').mkdir(parents=True)
        (self.main/'.git/HEAD').write_text('ref: refs/heads/master')
        (self.main/'.git/worktrees/side/commondir').write_text('../..')
        self.side.mkdir()
        (self.side/'.git').write_text('gitdir: '+str(self.main/'.git/worktrees/side'))

    def test_main_identity_does_not_borrow_side_status(self):
        for host in ('Bash','exec_command'):
            self.assertEqual(targets.actions(host,{'command':'git merge side'},str(self.side)),[])
            action,=targets.actions(host,{'command':'git merge side'},str(self.main))
            self.assertEqual(action.target,targets.canonical(str(self.main)))
            status,=targets.actions(host,{'command':f'git -C "{self.side}" status'},str(self.main))
            self.assertNotEqual(status.target,action.target)

    def test_worktree_git_file_and_quoted_read_target(self):
        self.assertEqual(targets.repository(str(self.side))[1],targets.canonical(str(self.main)))
        read,=targets.actions('Bash',{'command':'Get-Content -LiteralPath "a b.md" -TotalCount 3'},str(self.main))
        write,=targets.actions('Edit',{'file_path':'a b.md'},str(self.main))
        self.assertEqual(read.target,write.target)
        self.assertNotEqual(write.target,targets.actions('Read',{'file_path':'different.md'},str(self.main))[0].target)

    def test_quoted_mentions_dynamic_paths_and_new_file_are_not_proof(self):
        for source in ('echo "git status"','echo "cat a.md"','foreach ($p in $paths) { Get-Content $p }', 'Get-Content "$p"'):
            self.assertEqual(targets.actions('Bash',{'command':source},str(self.main)),[])
        patch='*** Begin Patch\n*** Add File: new.md\n+new\n*** End Patch'
        self.assertEqual(targets.actions('apply_patch',{'input':patch},str(self.main)),[])

    def test_single_invocation_and_windows_operands(self):
        path=self.main/'a.md'; path.write_text('old')
        for source in ('cat a.md; false', 'cat a.md | head', 'if true; then cat a.md; fi',
                       'cat a.md && echo ok', 'cat a.md # comment', 'cat $(find .)'):
            self.assertEqual(targets.actions('Bash',{'command':source},str(self.main)),[])
        action,=targets.actions('exec_command',{'cmd':'Get-Content -LiteralPath '+str(path)},str(self.main))
        self.assertEqual(action.target,targets.canonical(str(path)))
        self.assertEqual(targets.actions('exec_command',{'cmd':'Remove-Item -LiteralPath '+str(path)},str(self.main)),[])
        fresh,=targets.actions('Write',{'file_path':'new.md'},str(self.main))
        self.assertEqual(fresh.kind,'file.create')


if __name__=='__main__':
    if '--selftest' in sys.argv: sys.argv.remove('--selftest')
    unittest.main()
