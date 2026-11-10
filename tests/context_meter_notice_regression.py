"""Short handoff reminders must survive the real hook byte budgets."""
import sys
sys.dont_write_bytecode = True
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'adapters' / 'claude')]
from epitype import compact_map, context_meter, memspec
import _hook_common as common
import pretooluse_gate as pretool
import recall_hook as recall


class IsolatedHome(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='epitype-meter-notice-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {
            'HOME': str(self.home), 'USERPROFILE': str(self.home),
            memspec.EPITYPE_CONFIG_ENV: str(self.home / 'config.json'),
            memspec.CONTEXT_METER_WINDOW_ENV: '', memspec.CONTEXT_METER_PCT_ENV: '',
        })
        environment.start()
        self.addCleanup(environment.stop)


class HandoffNotices(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.root = self.home
        self.vault = self.root / 'memory'
        self.vault.mkdir()
        self.markers = self.root / 'markers'
        self.event = {'session_id': 'fixture', 'transcript_path': str(self.root / 'trace.jsonl')}
        self.handoff = compact_map.handoff_destination(
            self.vault, self.event['session_id'], self.event['transcript_path'])
        self.handoff.parent.mkdir(parents=True)
        self.reading = SimpleNamespace(host='claude', tokens=180000, model='fixture')
        self.options = {'context_meter': {}}
        self.config = {'vaults': [self.vault], 'budget_bytes': memspec.HOOK_DEFAULT_BUDGET_BYTES}
        for instrument in (
                patch.object(context_meter, 'measure', return_value=self.reading),
                patch.object(context_meter, 'threshold', return_value=(185000, 'override')),
                patch.object(context_meter, '_codex_limit_cached', return_value=210000),
                patch.object(context_meter, 'codex_due', return_value=(180000, 210000)),
                patch.object(memspec, 'config_options', return_value=self.options),
                patch.object(pretool, 'recall_marker_directory', return_value=self.markers),
                patch.object(recall, 'recall_marker_directory', return_value=self.markers),
                patch.object(pretool, 'config_path', return_value=self.root / 'config.json'),
                patch.object(pretool.control_lifecycle, 'reminder', return_value=None),
                patch.object(pretool, '_midturn_language_line', return_value=None)):
            instrument.start()
            self.addCleanup(instrument.stop)

    def write_size(self, size):
        # Multibyte text distinguishes actual bytes from character counts.
        self.handoff.write_bytes(('交' * (size // 3)).encode('utf-8') + b'x' * (size % 3))

    def notice(self, host, rotation=False):
        self.reading.host = host
        self.options['context_meter'] = {'handoff_tokens': 250000} if rotation else {}
        found = context_meter.notice(self.event, self.vault, self.markers)
        self.assertIsNotNone(found)
        return found[0]

    def test_overwrite_contents_and_exact_size_boundary_for_both_hosts_and_rotation(self):
        for host in ('claude', 'codex'):
            for rotation in (False, True):
                for size in (32767, 32768, 32769):
                    with self.subTest(host=host, rotation=rotation, size=size):
                        self.write_size(size)
                        line = self.notice(host, rotation)
                        for text in ('整份覆寫成短狀態', '仍有效的使用者要求', '決定與理由',
                                     '進度', '下一步', '未完成', '已排除的路', '確切路徑或指令',
                                     '不要保留舊內容再追加', '仍需的舊流水搬到同目錄另一檔'):
                            self.assertIn(text, line)
                        suffix = memspec.CONTEXT_METER_HANDOFF_SIZE_NOTICE.format(size=size)
                        if size > 32768:
                            self.assertTrue(line.endswith(suffix))
                        else:
                            self.assertNotIn('交接檔目前', line)
                        self.assertNotIn('每到里程碑', line)

    def test_missing_or_unstatable_handoff_omits_size_but_keeps_reminder(self):
        stat = Path.stat

        def unavailable(path, *args, **kwargs):
            if path == self.handoff:
                raise PermissionError('synthetic stat failure')
            return stat(path, *args, **kwargs)

        for host in ('claude', 'codex'):
            for rotation in (False, True):
                with self.subTest(host=host, rotation=rotation):
                    self.assertNotIn('交接檔目前', self.notice(host, rotation))
                    with patch.object(Path, 'stat', autospec=True, side_effect=unavailable):
                        self.assertNotIn('交接檔目前', self.notice(host, rotation))

    def test_both_hooks_emit_complete_reminders_with_size_and_reject_insufficient_budget(self):
        self.write_size(32769)
        codex_hooks = json.loads((ROOT / 'adapters' / 'codex' / 'hooks_template.json').read_text(encoding='utf-8'))
        codex_prompt_limit = codex_hooks['hooks']['UserPromptSubmit'][0]['hooks'][0]['additionalContextLimit']
        for host in ('claude', 'codex'):
            for rotation in (False, True):
                line = self.notice(host, rotation)
                if host == 'codex':
                    self.assertLessEqual(len(line.encode('utf-8')), codex_prompt_limit)
                for event_name in ('PreToolUse', 'UserPromptSubmit'):
                    for budget in (len(line.encode('utf-8')) - 1, memspec.HOOK_DEFAULT_BUDGET_BYTES):
                        with self.subTest(host=host, rotation=rotation, hook=event_name, budget=budget):
                            config = {**self.config, 'budget_bytes': budget}
                            pending = []
                            started = time.monotonic()
                            if event_name == 'PreToolUse':
                                value = pretool._allow_context(self.event, config=config,
                                    started_at=started, meter_claims=pending)
                            else:
                                value = recall._with_context_meter(
                                    None, config, self.event, started, pending)
                            if budget < len(line.encode('utf-8')):
                                self.assertEqual(pending, [])
                                self.assertIsNone(value)
                                self.assertFalse(self.markers.exists() and any(self.markers.iterdir()))
                                continue
                            self.assertEqual(len(pending), 1)
                            value, won, lost = common.claim_meter(value, event_name, pending)
                            try:
                                self.assertEqual(len(won), 1)
                                self.assertEqual(lost, [])
                                output = io.StringIO()
                                with contextlib.redirect_stdout(output):
                                    common.emit(value)
                                encoded = output.getvalue().rstrip('\n')
                                self.assertLessEqual(len(encoded.encode('utf-8')), memspec.HOOK_MAX_OUTPUT_BYTES)
                                self.assertEqual(json.loads(encoded)['hookSpecificOutput']['additionalContext'], line)
                            finally:
                                common.release_meter(won)

    def test_english_templates_keep_the_same_instructions_and_size_warning(self):
        self.write_size(32769)
        with contextlib.ExitStack() as stack:
            for name in ('CONTEXT_METER_NOTICE', 'CONTEXT_METER_CODEX_NOTICE',
                         'CONTEXT_METER_HANDOFF_SIZE_NOTICE'):
                stack.enter_context(patch.object(memspec, name, memspec._EN[name]))
            for host in ('claude', 'codex'):
                with self.subTest(host=host):
                    line = self.notice(host)
                    for text in ('overwrite the entire handoff', 'short state',
                                 'user requirements still in force', 'decisions and why', 'progress',
                                 'next step', 'unfinished work', 'ruled-out approaches',
                                 'exact paths or commands', 'Do not keep old content and append',
                                 'another file in the same directory', '32769 bytes', 'please shorten it'):
                        self.assertIn(text, line)


class WindowThresholds(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.vault = self.home / 'memory'
        self.vault.mkdir()
        self.state = context_meter.state_path(self.vault)
        self.window = memspec.CONTEXT_METER_WINDOW_ENV
        self.pct = memspec.CONTEXT_METER_PCT_ENV
        self.env = {self.window: '300000'}

    def threshold(self, env, options=None):
        return context_meter.threshold(options or {}, self.state, env, self.home)

    def test_window_needs_no_samples_and_pct_lowers_it(self):
        for window in ('100000', '300000', '1000000'):
            self.assertEqual(self.threshold({self.window: window}), (int(window), 'window'))
        self.assertEqual(self.threshold({**self.env, self.pct: '40'}), (120000, 'window'))
        self.assertEqual(self.threshold({**self.env, self.pct: '100'}), (300000, 'window'))
        for pct in ('0', '101', 'bad'):
            self.assertEqual(self.threshold({**self.env, self.pct: pct}), (300000, 'window'))
        self.assertEqual(self.threshold(self.env, {'context_meter': {'autocompact_tokens': 200000}}),
                         (200000, 'override'))

    def test_invalid_window_is_not_used_as_a_token_threshold(self):
        for value in ('99999', '1000001', '300000.5', 'true', '-300000', ''):
            with self.subTest(value=value):
                self.assertEqual(self.threshold({self.window: value}), (None, None))

    def test_lower_same_window_median_is_used_and_override_still_wins(self):
        context_meter.write_samples(self.state, [
            {'tokens': tokens, 'pct': '', 'window': '300000'}
            for tokens in (1, 2, 170000, 180000, 190000, 200000, 210000)])
        self.assertEqual(self.threshold(self.env), (190000, 'learned'))
        self.assertEqual(self.threshold(self.env, {'context_meter': {'autocompact_tokens': 250000}}),
                         (250000, 'override'))

    def test_higher_or_equal_same_window_median_uses_window(self):
        for tokens in (300000, 400000):
            with self.subTest(tokens=tokens):
                context_meter.write_samples(self.state, [
                    {'tokens': tokens, 'pct': '', 'window': '300000'}])
                self.assertEqual(self.threshold(self.env), (300000, 'window'))

    def test_pct_scaled_sample_is_compared_with_pct_scaled_window(self):
        for tokens, expected in ((180000, (72000, 'scaled')), (400000, (120000, 'window'))):
            with self.subTest(tokens=tokens):
                context_meter.write_samples(self.state, [
                    {'tokens': tokens, 'pct': '100', 'window': '300000'}])
                self.assertEqual(self.threshold({**self.env, self.pct: '40'}), expected)

    def test_no_usable_same_window_samples_falls_back_to_window(self):
        for samples in ([], [{'tokens': 10000, 'pct': ''}],
                        [{'tokens': 10000, 'pct': '', 'window': '400000'}],
                        [{'tokens': 10000, 'pct': '40', 'window': '300000'}]):
            with self.subTest(samples=samples):
                context_meter.write_samples(self.state, samples)
                self.assertEqual(self.threshold(self.env), (300000, 'window'))
        self.state.write_text('{broken', encoding='utf-8')
        self.assertEqual(self.threshold(self.env), (300000, 'window'))

    def test_settings_env_fallback_and_process_precedence(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        settings.write_text(json.dumps({'autoCompactWindow': 500000,
            'env': {self.window: '300000', self.pct: '40'}}), encoding='utf-8')
        self.assertEqual(self.threshold({}), (120000, 'window'))
        self.assertEqual(self.threshold({self.window: '400000'}), (160000, 'window'))
        self.assertEqual(self.threshold({self.window: ''}), (None, None))
        self.assertEqual(self.threshold({self.pct: ''}), (300000, 'window'))
        settings.write_text('{broken', encoding='utf-8')
        self.assertEqual(self.threshold({}), (None, None))

    def test_samples_do_not_cross_window_configuration_changes(self):
        old = {'tokens': 80000, 'pct': '40', 'at': ''}
        windowed = {**old, 'window': '300000'}
        self.assertEqual(context_meter.learned_threshold([old], '40', '300000'), (None, None))
        self.assertEqual(context_meter.learned_threshold([windowed], '40'), (None, None))
        self.assertEqual(context_meter.learned_threshold([windowed], '40', '400000'), (None, None))
        self.assertEqual(context_meter.learned_threshold([windowed], '20', '300000'), (40000, 'scaled'))
        context_meter.write_samples(self.state, [windowed])
        self.assertEqual(self.threshold({self.pct: '40'}), (None, None))
        self.assertEqual(self.threshold(self.env), (300000, 'window'))
        context_meter.write_samples(self.state, [old])
        self.assertEqual(self.threshold({self.pct: '40'}), (80000, 'learned'))

    def test_recorded_window_survives_state_round_trip(self):
        with patch.object(context_meter, 'current_tokens', return_value=295000):
            context_meter.record_autocompact(self.vault, 'synthetic.jsonl',
                                            environ=self.env, home=self.home)
        samples = context_meter.read_samples(self.state)
        self.assertEqual(samples[0]['window'], '300000')
        self.assertEqual(samples[0]['tokens'], 295000)
        self.assertEqual(self.threshold({}), (None, None))

    def test_status_reports_window_threshold_and_reminder_time(self):
        lines = []
        limit = context_meter.status({}, self.vault, environ={**self.env, self.pct: '40'},
                                     out=lines.append)
        self.assertEqual(limit, 120000)
        self.assertIn('source=window', lines[0])
        self.assertIn('window=300000', lines[0])
        self.assertIn('remind_at=116400', lines[0])

    def test_status_reports_lower_learned_and_scaled_thresholds(self):
        context_meter.write_samples(self.state, [
            {'tokens': 180000, 'pct': '100', 'window': '300000'}])
        for pct, expected, source in (('100', 180000, 'learned'), ('40', 72000, 'scaled')):
            with self.subTest(pct=pct):
                lines = []
                limit = context_meter.status({}, self.vault, environ={**self.env, self.pct: pct},
                                             out=lines.append)
                self.assertEqual(limit, expected)
                self.assertIn(f'source={source}', lines[0])
                self.assertIn(f'remind_at={int(expected * memspec.CONTEXT_METER_STAGE_RATIO)}', lines[0])

    def test_notice_uses_lower_model_cap_at_97_percent(self):
        context_meter.write_samples(self.state, [
            {'tokens': 180000, 'pct': '100', 'window': '300000'}])
        event = {'session_id': 'model-cap-fixture', 'transcript_path': str(self.home / 'trace.jsonl')}
        for pct, limit, source in (('100', 180000, 'learned'), ('40', 72000, 'scaled')):
            boundary = int(limit * memspec.CONTEXT_METER_STAGE_RATIO)
            for current in (boundary - 1, boundary):
                with self.subTest(pct=pct, current=current):
                    reading = SimpleNamespace(host='claude', tokens=current)
                    with patch.object(context_meter, 'measure', return_value=reading):
                        notice = context_meter.notice(event, self.vault, self.home / 'markers',
                            options={}, environ={**self.env, self.pct: pct})
                    if current < boundary:
                        self.assertIsNone(notice)
                    else:
                        path = compact_map.handoff_destination(self.vault, event['session_id'],
                                                              event['transcript_path'])
                        self.assertEqual(notice[0], context_meter.render(current, limit, source, path))

    def test_notice_and_handoff_start_at_97_percent_of_window(self):
        event = {'session_id': 'window-fixture', 'transcript_path': str(self.home / 'trace.jsonl')}
        for pct, limit in ((None, 300000), ('40', 120000)):
            env = self.env if pct is None else {**self.env, self.pct: pct}
            boundary = int(limit * memspec.CONTEXT_METER_STAGE_RATIO)
            for current in (boundary - 1, boundary):
                reading = SimpleNamespace(host='claude', tokens=current)
                with patch.object(context_meter, 'measure', return_value=reading):
                    notice = context_meter.notice(event, self.vault, self.home / 'markers',
                                                  options={}, environ=env, home=self.home)
                if current < boundary:
                    self.assertIsNone(notice)
                else:
                    path = compact_map.handoff_destination(self.vault, 'window-fixture', event['transcript_path'])
                    self.assertEqual(notice[0], context_meter.render(current, limit, 'window', path))
                    self.assertIn(str(path), notice[0])


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
