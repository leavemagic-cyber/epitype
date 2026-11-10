"""Synthetic, isolated SessionStart and first-tool median timings (no live vaults)."""
import sys
sys.dont_write_bytecode = True
import argparse
import json
import os
from pathlib import Path
import re
import statistics
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'adapters' / 'claude'), str(ROOT / 'tests')]
import _hook_common as common
import pretooluse_gate as gate
import sessionstart_hook as start
from epitype import compact_map, memspec
from rule_source_fixtures import owner_bound_card


def benchmark(rounds=5):
    with tempfile.TemporaryDirectory(prefix='epitype-startup-benchmark-') as directory:
        root = Path(directory).resolve()
        home = root / 'home'
        project = root / 'project'
        project.mkdir()
        slug = re.sub(r'[^A-Za-z0-9]', '-', str(project))
        native = home / '.claude' / 'projects' / slug / 'memory'
        governance = root / 'governance'
        other = root / 'other'
        vaults = [governance, native, other]
        for vault, count in zip(vaults, (500, 146, 815)):
            vault.mkdir(parents=True)
            for index in range(count):
                path = vault / ('card-%04d.md' % index)
                text = ('---\nname: synthetic card %d\ndescription: 2026-11-01 合成測試卡\n'
                        'last_verified_at: 2026-11-01\naliases: [合成]\nmetadata:\n'
                        '  type: feedback\n' % index)
                if index % 20 == 0:
                    text += 'guard_tool: Bash\nguard_all_of: [synthetic-danger]\non_hit: block\n'
                text += '---\n' + '合成測試內容。\n' * 20
                if index % 20 == 0:
                    text = owner_bound_card(text, path, source_root=root/'sources',
                                            case_id=vault.name+str(index))
                path.write_text(text, encoding='utf-8')
        (governance/memspec.WORK_LEDGER_FILENAME).write_text('synthetic ledger\n', encoding='utf-8')
        config = root / 'config.json'
        common.write_config(config, vaults)
        event = dict(source='startup', cwd=str(project), session_id='synthetic-benchmark',
                     transcript_path=str(root/'transcript.jsonl'))
        Path(event['transcript_path']).write_text('{}\n', encoding='utf-8')
        tool = dict(event, tool_name='Read', tool_input={'file_path': str(root/'sample.txt')})
        (root/'sample.txt').write_text('sample', encoding='utf-8')
        env = dict(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home/'.codex'),
                   EPITYPE_CONFIG=str(config), EPITYPE_DREAM_MODE='off',
                   TMP=str(root), TEMP=str(root), TMPDIR=str(root))
        with patch.dict(os.environ, env):
            # Lint maintenance is outside the hook measurement, if supported.
            try:
                from epitype import lint_result
            except ImportError:
                lint_result = None
            if lint_result:
                for vault in vaults[:2]:
                    lint_result.refresh(vault)
            result = dict(cards=1461, scoped_cards=646, rounds=rounds, timings={})
            for scenario in ('warm', 'cold', 'one-change'):
                samples = []
                for index in range(rounds):
                    # Complete locator preparation is excluded only for warm/change runs.
                    if scenario == 'cold':
                        for vault in vaults:
                            gate._guard_cache(vault).unlink(missing_ok=True)
                    else:
                        for vault in vaults:
                            gate._guards(vault, time.monotonic(), [], cap=gate._WARM_CAP)
                    if scenario == 'one-change':
                        with (native/'card-0001.md').open('a', encoding='utf-8') as stream:
                            stream.write('合成變更 %d\n' % index)
                    event['session_id'] = 'synthetic-benchmark-%s-%d' % (scenario, index)
                    tool['session_id'] = event['session_id']
                    tick = time.perf_counter()
                    trace = {}
                    start._handle(event, time.monotonic(), trace=trace)
                    session_ms = (time.perf_counter()-tick)*1000
                    if lint_result:
                        if scenario != 'one-change':
                            assert all(state in ('pass', 'fail') for state in trace['card_lint']), 'lint fixture is stale'
                        # A fast cold pass is useful only if it actually discovers
                        # the complete scope before the first tool call.
                        from epitype import cardscan
                        for vault in (native, governance):
                            cache = json.loads(gate._guard_cache(vault).read_text(encoding='utf-8'))
                            expected = {key: [mtime, size, ctime]
                                        for key, _path, mtime, size, ctime in cardscan.scan_vault(vault)}
                            assert cache['manifest'] == expected, 'incomplete warm scope'
                    tick = time.perf_counter()
                    gate._handle(tool, time.monotonic())
                    tool_ms = (time.perf_counter()-tick)*1000
                    samples.append([session_ms, tool_ms])
                result['timings'][scenario] = dict(
                    session_ms=round(statistics.median(s[0] for s in samples), 3),
                    first_tool_ms=round(statistics.median(s[1] for s in samples), 3),
                    samples=samples)
            # Exercise real recovery output, with full paths normalized for comparison.
            recovery = dict(event, source='compact')
            destination = compact_map.map_destination(governance, event['session_id'], event['transcript_path'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('synthetic map\n', encoding='utf-8')
            handoff = compact_map.handoff_destination(governance, event['session_id'], event['transcript_path'])
            handoff.write_text('<!-- EPITYPE RESUME BEGIN -->\nsynthetic working state\n'
                               '<!-- EPITYPE RESUME END -->\n', encoding='utf-8')
            result['compact_output'] = json.dumps(start._handle(recovery, time.monotonic()), ensure_ascii=False).replace(str(root), '<fixture>')
            return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.rounds), ensure_ascii=True, indent=2))
