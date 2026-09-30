"""Count observed rewrite triggers; useful content needs an explicit review."""
from collections import Counter
from pathlib import Path
from . import memsearch, memspec


def report(vaults, rows):
    policies = {}
    for vault in dict.fromkeys(Path(vault) for vault in vaults):
        for path in memsearch.card_files(vault):
            fields, _problem = memspec.frontmatter_fields(path)
            key = fields.get('decision_key') or fields.get('name') or path.stem
            policies[key] = fields
    counts = Counter()
    replay = Counter()
    notes = Counter()
    for row in rows:
        key = row.label
        if not key:
            continue
        if row.kind == 'stop_note':
            notes[key] += 1
        elif row.kind == 'stop_block':
            counts[key] += 1
            fields = policies.get(key, {})
            note = (fields.get('on_hit') == 'note'
                    or row.rule == 'require' and fields.get('require_on_hit') == 'note')
            if not note:
                replay[key] += 1
    return [{'card': key, 'observed_blocks': counts[key], 'same_record_policy_blocks': replay[key],
             'observed_notes': notes[key], 'useful_content': 'UNVERIFIED：擋下帳不含後續內容，不能以改寫字數推定效益'}
            for key in sorted(counts.keys() | notes.keys())]
