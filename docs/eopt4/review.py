import sys
sys.dont_write_bytecode = True
sys.path.pop(0)
sys.stdout.reconfigure(encoding='utf-8')
import json
import hashlib
from pathlib import Path
import re
import statistics
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from epitype import memspec
from epitype import memsearch
before = json.loads((root / 'docs/eopt4/b-before.json').read_text(encoding='utf-8'))
after = json.loads((root / 'docs/eopt4/b-final.json').read_text(encoding='utf-8'))
def paths(run):
    context = (run['output'] or {}).get('hookSpecificOutput', {}).get('additionalContext', '')
    legend = next((line for line in context.splitlines() if line.startswith('vaults: ')), '')
    aliases = dict(re.findall(r'(V\d+)=(.*?)(?= V\d+=|$)', legend.removeprefix('vaults: ')))
    found = []
    for line in context.splitlines():
        if line.startswith('- '):
            located = line.rsplit(' | ', 1)[-1]
            alias, _, relative = located.partition('/')
            if alias in aliases:
                found.append(Path(aliases[alias]) / relative)
    return found
def pins(run):
    found = set()
    for path in paths(run):
        fields, _ = memspec.frontmatter_fields(path)
        if fields.get('status') == 'active' and fields.get('decision_key'):
            found.add(str(path))
    return found
lost_pins = []
for a, b in zip(before['outcomes'], after['outcomes']):
    for ar, br in zip(a['runs'], b['runs']):
        lost = pins(ar) - pins(br)
        if lost:
            lost_pins.append({'id': a['id'], 'repeat': br['repeat'], 'paths': sorted(lost)})
def manifest(sample):
    found = {}
    for number in range(1, 5):
        digest = hashlib.sha256()
        cards = memsearch.scan_cards(sample / f'V{number}')
        for name, path, *_ in cards:
            digest.update(name.encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
        found[f'V{number}'] = {'cards': len(cards), 'sha256': digest.hexdigest()}
    return found
old = json.loads((root / 'docs/eopt4/project-baseline.json').read_text(encoding='utf-8'))
process = json.loads((root / 'docs/eopt4/b-process-final.json').read_text(encoding='utf-8'))
process_before = json.loads((root / 'docs/eopt4/b-process-before.json').read_text(encoding='utf-8'))
new_hits = {r['id'] for r in after['outcomes'] if r['hook_hit']}
new_search = {r['id'] for r in after['outcomes'] if r['passed']}
review = {
    'reviewed_revision': after['revision'],
    'all_repeat_mean_bytes': {name: statistics.mean(run['injected_bytes'] for row in report['outcomes'] for run in row['runs'])
                              for name, report in (('core_before', before), ('core_after', after),
                                                   ('process_before', process_before), ('process_after', process))},
    'before_after_manifest_identical': before['manifest'] == after['manifest'],
    'actual_core_manifest_matches': manifest(root / '.eopt4b/sample') == after['manifest'],
    'actual_process_manifest_matches': (manifest(root / '.eopt4b/process-final') == process['manifest']
                                        if (root / '.eopt4b/process-final').is_dir() else None),
    'historical_original_manifest_identical': old['manifest'] == after['manifest'],
    'paired_repeat_pinned_checks': sum(len(row['runs']) for row in after['outcomes']),
    'lost_previously_delivered_pins': lost_pins,
    'lost_before_complete_card_cases': [r['id'] for r in before['outcomes'] if r['hook_hit'] and r['id'] not in new_hits],
    'lost_original_master_complete_card_cases': [r['id'] for r in old['outcomes'] if r['hook_hit'] and r['id'] not in new_hits],
    'lost_before_search_passes': [r['id'] for r in before['outcomes'] if r['passed'] and r['id'] not in new_search],
    'empty_core_outputs': sum(run['output'] is None for row in after['outcomes'] for run in row['runs']),
    'empty_process_outputs': sum(run['output'] is None for row in process['outcomes'] for run in row['runs']),
    'four_case_repeats': {row['id']: [{'repeat': run['repeat'], 'kind': run['delivery_kind'],
                                     'bytes': run['injected_bytes'], 'ms': run['hook_ms']}
                                    for run in row['runs']]
                          for row in after['outcomes'] if row['id'] in ('v2-03', 'v3-07', 'v3-09', 'v3-11')},
    'source_sha256_matches_checkout': all(hashlib.sha256((root / name).read_bytes()).hexdigest() == value
                                        for name, value in after['source_sha256'].items()),
}
(root / 'docs/eopt4/b-review.json').write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(review, ensure_ascii=False, indent=2))
