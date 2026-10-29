import sys; sys.dont_write_bytecode = True
"""Read live samples only into an isolated snapshot; measure ranking and hook output."""

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'adapters' / 'claude'), str(ROOT / 'tests')]
from epitype import memsearch, memspec
import _hook_common as common
import recall_hook as hook
import recall_regression as regression


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


def measure(root, corpus, repeats):
    vaults = [root / f'V{index}' for index in range(1, 5)]
    for vault in vaults:
        memsearch.build_index(vault)
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
            samples, contexts = [], []
            for _ in range(repeats):
                start = time.monotonic()
                with contextlib.redirect_stderr(io.StringIO()):
                    value = hook._recall({'prompt': question['prompt']}, start, config)
                samples.append((time.monotonic() - start) * 1000)
                context = (value or {}).get('hookSpecificOutput', {}).get('additionalContext', '')
                contexts.append(context)
            context = contexts[-1]
            outcome['hook_hit'] = any(f'/{name}' in context for name in question['expect'])
            outcome['injected_bytes'] = len(context.encode('utf-8'))
            outcome['hook_ms'] = statistics.median(samples)
            outcomes.append(outcome)
            durations.extend(samples)
            sizes.append(outcome['injected_bytes'])
    return {'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'manifest': json.loads((root / 'manifest.json').read_text(encoding='utf-8')),
            'repeats': repeats, 'cases': len(outcomes),
            'hits_at_8': sum(row['passed'] for row in outcomes),
            'hook_hits': sum(row['hook_hit'] for row in outcomes),
            'hook_median_ms': statistics.median(durations), 'hook_max_ms': max(durations),
            'mean_injected_bytes': statistics.mean(sizes), 'outcomes': outcomes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--create', action='store_true')
    parser.add_argument('--config', type=Path, default=Path.home() / '.epitype/config.json')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'tests/fixtures/recall_sessions_20261029.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.create:
        snapshot(args.snapshot, json.loads(args.config.read_text(encoding='utf-8')))
    report = measure(args.snapshot, json.loads(args.corpus.read_text(encoding='utf-8')), args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key not in ('outcomes', 'manifest')}, ensure_ascii=False))
    print('FAIL', ', '.join(row['id'] for row in report['outcomes'] if not row['passed']))


if __name__ == '__main__':
    main()
