"""Execution-only fixtures: no external AI, credentials, network, or project state."""
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/ai-collaboration'
sys.path.insert(0, str(SKILL))
spec = importlib.util.spec_from_file_location('collaboration_runner_test', SKILL / 'seats.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from collaboration_state import RoundBusy, atomic_json, file_digest, read_json, round_lock, source_state


class RuntimeRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='_collaboration_test_', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        assert self.folder.is_relative_to(ROOT) and self.folder != ROOT
        self.records = self.folder / 'records'
        self.records.mkdir()
        self.source = self.folder / 'code.py'
        self.source.write_text('VALUE = 1\n', encoding='utf-8')
        self.git('init', '-q')
        self.git('add', '--', 'code.py')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=test@example.com', 'commit', '-qm', 'fixture', '--', 'code.py')
        self.prompt = self.records / 'prompt.md'
        self.prompt.write_text('review code.py at the current version', encoding='utf-8')
        self.counter = self.records / 'calls.txt'
        self.stub = self.records / 'fixture.py'
        self.stub.write_text('''from pathlib import Path
counter = Path(__file__).with_name('calls.txt')
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
print(('review evidence ' * 40) + str(n))
''', encoding='utf-8')

    def git(self, *args):
        p = subprocess.run(['git', '-C', str(self.folder), *args], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def command(self, seat, *args):
        return [sys.executable, '-B', str(self.stub)], None

    def one(self, spec='grok:fixture:high', inputs=None, web=False):
        with mock.patch.object(runner, 'command', side_effect=self.command):
            return runner.run_one(spec, self.prompt, self.records, 'R1', str(self.folder), [], web, 200,
                                  [str(self.source)] if inputs is None else inputs)

    def run_round(self, specs=None, inputs=None):
        a = SimpleNamespace(cwd=str(self.folder), seat=specs or ['grok:fixture:high'],
                            input=[str(self.source)] if inputs is None else inputs)
        with mock.patch.object(runner, 'command', side_effect=self.command), redirect_stdout(io.StringIO()):
            result = runner.run_seats(a, 'review current source', self.records, 'R1', 200)
        return result, read_json(self.records / 'R1_status.json')

    def bundle(self, template=None, **values):
        template = template or SKILL / 'templates/R2.md'
        defaults = dict(TOPIC='fixture', MODE='review', WORKTREE=str(self.folder), REV='fixture')
        defaults.update(values)
        a = SimpleNamespace(template=str(template), outdir=str(self.records), out=str(self.records/'packet.md'),
                            set=[f'{k}={v}' for k,v in defaults.items()], allow_partial=False)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = runner.bundle(a)
        return code, Path(a.out).read_text(encoding='utf-8') if Path(a.out).exists() else None

    def test_matching_result_reuses_exact_answer_without_another_process(self):
        self.assertEqual(self.one()['status'], 'success')
        self.assertEqual(self.one()['status'], 'reused')
        self.assertEqual(self.counter.read_text(), '1')

    def test_old_unreceipted_answer_is_not_accepted(self):
        (self.records/'R1_grok.md').write_text('obsolete ' * 100, encoding='utf-8')
        self.assertEqual(self.one()['status'], 'success')
        self.assertEqual(self.counter.read_text(), '1')

    def test_prompt_model_effort_and_web_changes_do_not_reuse(self):
        self.one()
        self.prompt.write_text('a new requirement', encoding='utf-8')
        self.assertEqual(self.one()['status'], 'success')
        self.assertEqual(self.one('grok:other-model:high')['status'], 'success')
        self.assertEqual(self.one('grok:other-model:xhigh')['status'], 'success')
        self.assertEqual(self.one('grok:other-model:xhigh', web=True)['status'], 'success')
        self.assertEqual(self.one('grok:other-model:xhigh', web=True)['status'], 'success')
        self.assertEqual(self.counter.read_text(), '6')

    def test_same_dirty_flags_and_timestamp_still_detect_different_content(self):
        self.source.write_text('VALUE = 2\n', encoding='utf-8')
        self.one()
        old_status = self.git('status', '--porcelain', '--', 'code.py')
        stat = self.source.stat()
        self.source.write_text('VALUE = 3\n', encoding='utf-8')
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(old_status, self.git('status', '--porcelain', '--', 'code.py'))
        self.assertEqual(self.one()['status'], 'success')
        self.assertEqual(self.counter.read_text(), '2')

    def test_tampered_answer_and_receipt_cannot_hit_cache(self):
        self.one()
        (self.records/'R1_grok.md').write_text('altered output ' * 40, encoding='utf-8')
        self.assertEqual(self.one()['status'], 'success')
        (self.records/'R1_grok.receipt.json').write_text('{corrupt', encoding='utf-8')
        self.assertEqual(self.one()['status'], 'success')
        self.assertEqual(self.counter.read_text(), '3')

    def test_uncovered_dirty_source_runs_but_is_not_reused(self):
        self.source.write_text('VALUE = 2\n', encoding='utf-8')
        self.assertEqual(self.one(inputs=[])['status'], 'success')
        self.assertEqual(self.one(inputs=[])['status'], 'success')
        self.assertEqual(self.counter.read_text(), '2')

    def test_nested_working_directory_and_untracked_inputs_are_bound(self):
        nested = self.folder/'nested'
        nested.mkdir()
        other = nested/'new.py'
        other.write_text('new source', encoding='utf-8')
        state = source_state(nested, self.records, [str(self.source), str(other)])
        self.assertTrue(state['reusable'])
        before = state['files'][str(other)]
        other.write_text('changed source', encoding='utf-8')
        self.assertNotEqual(before, source_state(nested, self.records, [str(self.source), str(other)])['files'][str(other)])

    def test_failed_provider_and_stale_old_answer_remain_partial(self):
        def fake(seat, *args):
            return ([sys.executable, '-B', str(self.stub)], None) if seat == 'grok' else ([sys.executable, '-B', '-c', 'raise SystemExit(7)'], None)
        (self.records/'R1_agy.md').write_text('STALE_ANSWER', encoding='utf-8')
        with mock.patch.object(self, 'command', side_effect=fake):
            code, status = self.run_round(['grok:fixture:high', 'agy:fixture:high'])
        self.assertEqual(code, 1)
        self.assertEqual(status['status'], 'partial')
        bundle_code, text = self.bundle()
        self.assertEqual(bundle_code, 2)
        self.assertIn('PARTIAL', text)
        self.assertNotIn('STALE_ANSWER', text)

    def test_missing_executable_reports_failure_without_losing_receipts(self):
        with mock.patch.object(self, 'command', return_value=([str(self.folder/'does-not-exist.exe')], None)):
            code, status = self.run_round()
        self.assertEqual(code, 1)
        self.assertEqual(status['status'], 'partial')
        self.assertEqual(status['results'][0]['status'], 'failed')

    def test_source_changed_during_run_is_preserved_and_not_signed_off(self):
        with self.stub.open('a', encoding='utf-8') as stream:
            stream.write(f'Path({str(self.source)!r}).write_text("VALUE = 9\\n")\n')
        code, status = self.run_round()
        self.assertEqual(code, 1)
        self.assertEqual(status['status'], 'source_changed')
        self.assertEqual(self.source.read_text(encoding='utf-8'), 'VALUE = 9\n')
        receipt = read_json(self.records/'R1_grok.receipt.json')
        self.assertEqual(receipt['status'], 'invalidated')
        self.source.write_text('VALUE = 1\n', encoding='utf-8')
        self.stub.write_text(self.stub.read_text(encoding='utf-8').split('Path(' + repr(str(self.source)))[0], encoding='utf-8')
        _, second = self.run_round()
        self.assertEqual(second['results'][0]['status'], 'success')
        self.assertEqual(self.counter.read_text(), '2')

    def test_outputs_at_git_root_do_not_mark_source_as_changed(self):
        self.stub.write_text("print('review evidence ' * 40)\n", encoding='utf-8')
        a = SimpleNamespace(cwd=str(self.folder),seat=['grok:fixture:high'],input=[str(self.source)])
        # Existing fixture files are declared; generated R1 artifacts are excluded individually.
        a.input.extend(str(p) for p in self.records.iterdir() if p.is_file())
        with mock.patch.object(runner,'command',side_effect=self.command), redirect_stdout(io.StringIO()):
            code = runner.run_seats(a,'review current source',self.folder,'R1',200)
        self.assertEqual(code,0)
        self.assertNotEqual(read_json(self.folder/'R1_status.json')['status'],'source_changed')

    def test_real_cli_preserves_failure_exit_code(self):
        wrapper = self.records/'cli_fixture.py'
        wrapper.write_text('''import importlib.util
import sys
from pathlib import Path
skill = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('cli_seats', skill / 'seats.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
runner.command = lambda *args: ([sys.executable, '-B', '-c', 'raise SystemExit(7)'], None)
sys.argv = ['seats.py', *sys.argv[2:]]
sys.exit(runner.main())
''', encoding='utf-8')
        result = subprocess.run([sys.executable,'-B',str(wrapper),str(SKILL),'run',str(self.prompt),str(self.records),
            '--cwd',str(self.folder),'--tag','R1','--input',str(self.source),'--seat','grok:fixture:high'],
            capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertEqual(read_json(self.records/'R1_status.json')['status'],'partial')

    def test_unfilled_underscore_field_preserves_previous_valid_prompt(self):
        template = self.records/'template.md'
        template.write_text('Question {RULED_OUT}', encoding='utf-8')
        (self.records/'packet.md').write_text('previous valid prompt', encoding='utf-8')
        code, text = self.bundle(template)
        self.assertEqual(code, 2)
        self.assertEqual(text, 'previous valid prompt')

    def test_braces_in_answer_are_not_treated_as_template_fields(self):
        template = self.records/'template.md'
        template.write_text('Question {BACKGROUND}', encoding='utf-8')
        code, text = self.bundle(template, BACKGROUND='code uses {NOT_A_FIELD}')
        self.assertEqual(code, 0)
        self.assertIn('{NOT_A_FIELD}', text)

    def test_two_three_and_four_party_packets_keep_every_actual_answer(self):
        for count in (1,2,3):
            results = []
            expected = []
            for seat in ('codex','grok','agy')[:count]:
                path = self.records/f'R1_{seat}.md'
                answer = f'FULL-{seat} ' + ('evidence ' * 200) + f'END-{seat}'
                path.write_text(answer, encoding='utf-8')
                expected.append(answer)
                results.append(dict(seat=seat,status='success',output=str(path),output_sha256=file_digest(path)))
            atomic_json(self.records/'R1_status.json', dict(status='complete',requested_seats=[r['seat'] for r in results],results=results))
            (self.records/'CHAIR_R1.md').write_text('FULL-CHAIR', encoding='utf-8')
            code, text = self.bundle()
            self.assertEqual(code, 0)
            self.assertIn('FULL-CHAIR', text)
            for answer in expected:
                self.assertIn(answer, text)

    def test_old_manual_material_is_labelled_and_remains_usable(self):
        (self.records/'CHAIR_R1.md').write_text('historical analysis', encoding='utf-8')
        code, text = self.bundle()
        self.assertEqual(code, 0)
        self.assertIn('UNATTESTED', text)
        self.assertIn('historical analysis', text)

    def test_kernel_lock_blocks_second_writer_then_releases(self):
        lock = self.records/'R1.lock'
        with round_lock(lock):
            with self.assertRaises(RoundBusy):
                with round_lock(lock):
                    self.fail('second writer entered')
            (self.records/'R1_sent.md').write_text('in-flight prompt', encoding='utf-8')
            a = SimpleNamespace(cwd=str(self.folder),seat=['grok:fixture:high'],input=[])
            with redirect_stderr(io.StringIO()):
                self.assertEqual(runner.run_seats(a,'replacement',self.records,'R1',200),3)
            self.assertEqual((self.records/'R1_sent.md').read_text(encoding='utf-8'),'in-flight prompt')
        with round_lock(lock):
            pass

    def test_duplicate_seat_cannot_overwrite_another_result(self):
        a = SimpleNamespace(cwd=str(self.folder),seat=['grok:a:high','grok:b:high'])
        with self.assertRaisesRegex(ValueError,'duplicate seat'):
            runner.run_seats(a,'review',self.records,'R1',200)


if __name__ == '__main__':
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"], verbosity=2)
