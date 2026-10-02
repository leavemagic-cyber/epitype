"""Session rule pointers survive compaction without copying card bodies."""
import hashlib
import json
from pathlib import Path
from . import memspec, rule_pipeline, rule_provenance


def applicability(scope, event=None):
    """Match explicit source scope; missing machine context is advisory, not global."""
    event = event if isinstance(event, dict) else {}
    context = event.get('rule_context')
    context = context if isinstance(context, dict) else {}
    project = scope.get('project', '')
    if project:
        explicit = context.get('project') or event.get('project')
        cwd = event.get('cwd')
        if explicit:
            if str(explicit).casefold() != str(project).casefold():
                return 'OUTSIDE', 'project does not match'
        elif cwd and Path(project).is_absolute():
            try:
                if not Path(cwd).resolve().is_relative_to(Path(project).resolve()):
                    return 'OUTSIDE', 'cwd is outside source project'
            except (OSError, ValueError):
                return 'ADVISORY', 'project context is unavailable'
        else:
            return 'ADVISORY', 'project context is unavailable'
    case = scope.get('case', '')
    if case:
        actual = context.get('case') or event.get('case_id')
        if not actual:
            return 'ADVISORY', 'case context is unavailable'
        if actual != case:
            return 'OUTSIDE', 'case does not match'
    # These clauses are human language, not predicates that a Stop event can
    # certify. Preserve them in the reference without inventing an evaluator.
    if (scope.get('actions') != ['*']
            or any(scope.get(name) for name in ('conditions', 'exceptions'))):
        return 'ADVISORY', 'action/condition/exception clauses require contextual interpretation'
    return 'APPLICABLE', ''


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


def restore(destination, allowed_vaults, budget=1800, *, event=None, diagnostics=None):
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
        if rule_provenance.FIELD in fields:
            try:
                card_text = target.read_text(encoding='utf-8-sig')
                fields, problem = memspec.frontmatter_text(card_text)
                if problem:
                    raise ValueError('source-bound card has malformed frontmatter: ' + problem)
                projected = rule_pipeline.projection(fields, target, card_text=card_text)
                if projected['expired']:
                    continue
                state, reason = applicability(projected['document']['scope'], event)
                if state == 'OUTSIDE':
                    continue
                line = rule_pipeline.render_projection(projected)
                if state == 'ADVISORY':
                    line = '限定參考（執行範圍未由本場 context 確認）：' + line
                    if diagnostics is not None:
                        diagnostics.append(f"{item['card']}: {reason}")
            except (ValueError, OSError) as error:
                if diagnostics is not None:
                    diagnostics.append(f"{item['card']}: {error}")
                continue
        else:
            try:
                integrity = rule_pipeline.legacy_integrity(fields, target)
                if integrity is not None and not integrity.allowed:
                    if diagnostics is not None:
                        diagnostics.append(f"{item['card']}: " + '; '.join(integrity.issues))
                    continue
            except (ValueError, OSError) as error:
                if diagnostics is not None:
                    diagnostics.append(f"{item['card']}: {error}")
                continue
            if not description:
                continue
            line = f"本場規則路由 [UNVERIFIED；{item['card']}]：{description}"
        if len('\n'.join([*lines, line]).encode('utf-8')) <= budget:
            lines.append(line)
        elif diagnostics is not None:
            diagnostics.append(f"{item['card']}: context budget omitted the complete labelled entry")
    return '\n'.join(lines)
