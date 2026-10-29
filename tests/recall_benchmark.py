import sys; sys.dont_write_bytecode = True
"""Measure ranking and hook output in an isolated snapshot with an explicit corpus."""

import argparse
import contextlib
import hashlib
import io
import inspect
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import tempfile
import time
from unittest.mock import patch

RUN_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get('EPITYPE_BENCHMARK_ROOT', RUN_ROOT)).resolve()
sys.path[:0] = [str(ROOT), str(ROOT / 'adapters' / 'claude'), str(RUN_ROOT / 'tests')]
from epitype import memsearch, memspec
import _hook_common as common
import recall_hook as hook
import recall_regression as regression


def delivered_kind(context, vault, expected):
    legend = next((line for line in context.splitlines() if line.startswith(memspec.RECALL_LEGEND_PREFIX)), '')
    aliases = dict(re.findall(r'(V\d+)=(.*?)(?= V\d+=|$)', legend))
    for line in context.splitlines():
        if not line.startswith('- '):
            continue
        located = line.rsplit(' | ', 1)[-1].removeprefix('- ')
        alias, slash, relative = located.partition('/')
        if not slash or alias not in aliases or Path(aliases[alias]).resolve() != vault.resolve():
            continue
        if not any(token.casefold() in regression._identifiers(relative, '') for token in expected):
            continue
        prefix = line.rsplit(' | ', 1)[0]
        pointer = (' | ' not in line or prefix in (
            '- 查找線索（來源未核；非授權或新增禁令）', '- 繼任')
            or line.startswith('- 已核來源指路'))
        return 'pointer' if pointer else 'card'
    return None


def snapshot(destination, config):
    manifests = {}
    for index, raw in enumerate(config['vaults'], 1):
        target = destination / f'V{index}'
        target.mkdir(parents=True)
        digest = hashlib.sha256()
        count = 0
        for relative, source, _mtime, _size in memsearch.scan_cards(Path(raw).resolve()):
            payload = source.read_bytes()
            copied = target / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_bytes(payload)
            digest.update(relative.encode('utf-8') + b'\0' + hashlib.sha256(payload).digest())
            count += 1
        manifests[f'V{index}'] = {'cards': count, 'sha256': digest.hexdigest()}
    (destination / 'manifest.json').write_text(json.dumps(manifests, indent=2), encoding='utf-8')


def measure(root, corpus, repeats, scope='all', cold=False, process=False):
    root = root.resolve()
    source_hashes = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                     for path in ('epitype/memsearch.py', 'epitype/memspec.py', 'epitype/rule_provenance.py', 'adapters/claude/recall_hook.py')}
    vaults = [root / f'V{index}' for index in range(1, 5)]
    build_options = ({'prepare_sources': True}
                     if 'prepare_sources' in inspect.signature(memsearch.build_index).parameters else {})
    for vault in vaults:
        memsearch.build_index(vault, **build_options)
    config_path = root / 'config.json'
    common.write_config(config_path, vaults)
    outcomes, durations, sizes = [], [], []
    with patch.dict(os.environ, {memspec.EPITYPE_CONFIG_ENV: str(config_path),
                               'HOME': str(root / 'home'), 'USERPROFILE': str(root / 'home')}), \
         patch.object(hook, '_capture_event'), \
         patch.object(hook, 'recall_marker_directory', lambda session: root / 'markers' / session):
        config = common.load_config(time.monotonic())
        for question in corpus['questions']:
            vault = root / question['vault_id']
            outcome = regression.run_question(vault, question)
            question_config = dict(config)
            if scope == 'project':
                question_config[memspec.CONFIG_VAULTS_FIELD] = list(dict.fromkeys((vault, vaults[0])))
            samples, contexts, runs = [], [], []
            for repeat in range(repeats):
                if cold and hasattr(memsearch, '_alias_identity'):
                    memsearch._alias_identity.cache_clear()
                start = time.monotonic()
                event = {'prompt': question['prompt'], 'session_id': f'benchmark-{time.monotonic_ns()}'}
                errors = io.StringIO()
                if process:
                    runtime_config = root / 'process-config.json'
                    common.write_config(runtime_config, question_config[memspec.CONFIG_VAULTS_FIELD])
                    temporary = root / 'tmp'
                    temporary.mkdir(exist_ok=True)
                    environment = dict(os.environ, EPITYPE_CONFIG=str(runtime_config),
                                       TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
                    start = time.monotonic()
                    child = subprocess.run(
                        [sys.executable, str(ROOT / 'adapters/claude/recall_hook.py')],
                        input=json.dumps(event),
                        capture_output=True, text=True, encoding='utf-8', env=environment, check=True)
                    value = json.loads(child.stdout) if child.stdout.strip() else None
                    errors.write(child.stderr)
                else:
                    with contextlib.redirect_stderr(errors):
                        value = hook._recall(event, start, question_config)
                samples.append((time.monotonic() - start) * 1000)
                context = (value or {}).get('hookSpecificOutput', {}).get('additionalContext', '')
                contexts.append(context)
                runs.append({'repeat': repeat + 1, 'session_id': event['session_id'],
                             'hook_ms': samples[-1], 'injected_bytes': len(context.encode('utf-8')),
                             'delivery_kind': delivered_kind(context, vault, question['expect']),
                             'output': value, 'stderr': errors.getvalue()})
            context = contexts[-1]
            kind = delivered_kind(context, vault, question['expect'])
            outcome['hook_hit'] = kind == 'card'
            outcome['hook_pointer_hit'] = kind == 'pointer'
            outcome['injected_bytes'] = len(context.encode('utf-8'))
            outcome['hook_ms'] = statistics.median(samples)
            outcome['runs'] = runs
            outcomes.append(outcome)
            print(f"{question['id']}: rank={outcome.get('rank')} hook={outcome['hook_hit']} "
                  f"{outcome['hook_ms']:.0f} ms {outcome['injected_bytes']} bytes", flush=True)
            (root / 'progress.json').write_text(json.dumps(outcomes, ensure_ascii=False), encoding='utf-8')
            durations.extend(samples)
            sizes.append(outcome['injected_bytes'])
    return {'revision': os.environ.get('EPITYPE_BENCHMARK_REF') or subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'scope': scope, 'cold_alias_cache': cold,
            'latency_mode': 'hook-process' if process else 'recall-core',
            'hook_match_method': 'selected-vault-and-final-card-path; pointers counted separately',
            'source_sha256': source_hashes,
            'manifest': json.loads((root / 'manifest.json').read_text(encoding='utf-8')),
            'repeats': repeats, 'cases': len(outcomes),
            'hits_at_8': sum(row['passed'] for row in outcomes),
            'hook_hits': sum(row['hook_hit'] for row in outcomes),
            'hook_hits_all_repeats': sum(all(run['delivery_kind'] == 'card' for run in row['runs']) for row in outcomes),
            'complete_card_runs': sum(run['delivery_kind'] == 'card' for row in outcomes for run in row['runs']),
            'hook_pointer_hits': sum(row['hook_pointer_hit'] for row in outcomes),
            'hook_median_ms': statistics.median(durations), 'hook_max_ms': max(durations),
            'mean_injected_bytes': statistics.mean(sizes), 'outcomes': outcomes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--create', action='store_true')
    parser.add_argument('--config', type=Path, default=Path.home() / '.epitype/config.json')
    parser.add_argument('--corpus', type=Path,
                        default=os.environ.get('EPITYPE_RECALL_CORPUS'),
                        help='local corpus JSON; alternatively set EPITYPE_RECALL_CORPUS (skipped when absent)')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--scope', choices=('all', 'project'), default='all')
    parser.add_argument('--cold', action='store_true', help='clear pure alias parsing cache before every measured call')
    parser.add_argument('--process', action='store_true', help='run the actual hook process; capture may write into the disposable snapshot')
    parser.add_argument('--case', action='append', default=[], help='measure only these fixed corpus case IDs')
    args = parser.parse_args()
    if args.corpus is None:
        print('SKIP: provide --corpus or EPITYPE_RECALL_CORPUS for a local benchmark')
        return
    if args.create:
        snapshot(args.snapshot, json.loads(args.config.read_text(encoding='utf-8')))
    corpus = json.loads(args.corpus.read_text(encoding='utf-8'))
    if args.case:
        corpus['questions'] = [row for row in corpus['questions'] if row['id'] in args.case]
        if {row['id'] for row in corpus['questions']} != set(args.case):
            parser.error('unknown case ID')
    report = measure(args.snapshot, corpus, args.repeats, args.scope, args.cold, args.process)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key not in ('outcomes', 'manifest')}, ensure_ascii=False))
    print('FAIL', ', '.join(row['id'] for row in report['outcomes'] if not row['passed']))


if __name__ == '__main__':
    main()
