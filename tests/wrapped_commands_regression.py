import sys; sys.dont_write_bytecode = True
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype.wrapped_commands import candidates


class WrappedCommands(unittest.TestCase):
    def test_literal_commands_and_quoted_decoys(self):
        self.assertEqual(candidates('await tools.exec_command({cmd: "git status", workdir: "x"});'), ['git status'])
        self.assertEqual(candidates("tools.exec_command({'cmd': 'echo ok'});"), ['echo ok'])
        self.assertEqual(candidates('// tools.exec_command({cmd:"decoy"})\n"tools.exec_command({cmd:decoy})"'), [])

    def test_dynamic_overrides_and_templates_are_not_guessed(self):
        for source in ('tools.exec_command({cmd: value})', 'tools.exec_command({cmd:"x"+value})',
                       'tools.exec_command({cmd:`${value}`})', 'tools.exec_command({cmd:"x", ...rest})',
                       'tools.exec_command({cmd:"x", cmd:"y"})'):
            self.assertEqual(candidates(source), [], source)


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
