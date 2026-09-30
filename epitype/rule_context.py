"""Session rule pointers survive compaction without copying card bodies."""
import hashlib
import json
from pathlib import Path
from . import memspec


def state_path(vault, session):
    key = hashlib.sha256(str(session).encode('utf-8')).hexdigest()[:24]
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY / 'session_rules' / (key + '.json')


def record(vault, session, card):
    if not session or not card:
        return
    root = Path(vault).resolve()
    target = (root / str(card)).resolve()
    if target.suffix != '.md':
        target = Path(str(target) + '.md')
    if not target.is_relative_to(root) or not target.is_file():
        return
    path = state_path(root, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with memspec.file_lock(path, 0.1) as acquired:
        if not acquired:
            return
        try:
            rows = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            rows = []
        if not isinstance(rows, list):
            rows = []
        card_id = target.relative_to(root).as_posix()
        rows = [item for item in rows if isinstance(item, str) and item != card_id]
        path.write_text(json.dumps([*rows[-99:], card_id], ensure_ascii=False), encoding='utf-8')


def snapshot(vaults, session, destination):
    records = []
    for vault in vaults:
        try:
            cards = json.loads(state_path(vault, session).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(cards, list):
            records.extend({'vault': str(Path(vault).resolve()), 'card': card}
                           for card in cards if isinstance(card, str))
    target = Path(destination).with_suffix('.rules.json')
    if records:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    return records


def restore(destination, allowed_vaults, budget=1800):
    try:
        records = json.loads(Path(destination).with_suffix('.rules.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    if not isinstance(records, list):
        return ''
    roots = {str(Path(vault).resolve()) for vault in allowed_vaults}
    lines = []
    for item in reversed(records):
        if not isinstance(item, dict) or item.get('vault') not in roots:
            continue
        root = Path(item['vault'])
        target = (root / str(item.get('card', ''))).resolve()
        if not target.is_relative_to(root):
            continue
        fields, _problem = memspec.frontmatter_fields(target)
        if (memspec.card_expired(fields.get('valid_until'))
                or fields.get(memspec.DECISION_STATUS_FIELD) in memspec.STOP_GATE_SILENT_STATUSES):
            continue
        description = ' '.join(str(fields.get('description', '')).split())[:240]
        if not description:
            continue
        line = f"本場規則 [{item['card']}]：{description}"
        if len('\n'.join([*lines, line]).encode('utf-8')) <= budget:
            lines.append(line)
    return '\n'.join(lines)
