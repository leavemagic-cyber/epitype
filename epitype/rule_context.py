"""Session rule pointers survive compaction without copying card bodies."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path
import re
import subprocess
import time
from . import memspec, rule_pipeline, rule_provenance


class _GitLookupBudgetExpired(Exception):
    """No lookup ran; lru_cache must not retain this temporary condition."""


@lru_cache(maxsize=128)
def _git_location(directory):
    path = Path(directory)
    if not any((parent / '.git').exists() for parent in (path, *path.parents)):
        return None
    remaining = min(0.3, rule_provenance.verification_deadline(0.3) - time.monotonic())
    if remaining <= 0:
        raise _GitLookupBudgetExpired
    try:
        result = subprocess.run(
            ['git', '-C', str(path), 'rev-parse', '--path-format=absolute',
             '--git-common-dir', '--show-toplevel'], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=remaining,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        rows = result.stdout.strip().splitlines()
        if result.returncode == 0 and len(rows) == 2:
            return str(Path(rows[0]).resolve()), path.relative_to(Path(rows[1]).resolve())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return False  # A failed Git lookup cannot prove a different project.


def _path_applicability(project, actual):
    try:
        source, current = Path(project).resolve(), Path(actual).resolve()
        if current.is_relative_to(source):
            return 'APPLICABLE', ''
        left, right = _git_location(str(source)), _git_location(str(current))
        if left is None or right is None:
            return 'OUTSIDE', 'cwd is outside source project'
        if left is False or right is False:
            return 'ADVISORY', 'Git project context is unavailable'
        if left and right and left[0] == right[0] and right[1].is_relative_to(left[1]):
            return 'APPLICABLE', ''
        return 'OUTSIDE', 'cwd is outside source project'
    except _GitLookupBudgetExpired:
        return 'ADVISORY', 'Git project context is unavailable'
    except (OSError, ValueError):
        return 'ADVISORY', 'project context is unavailable'


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
            if Path(str(project)).is_absolute() and Path(str(explicit)).is_absolute():
                state, reason = _path_applicability(project, explicit)
                if state != 'APPLICABLE':
                    return state, reason
            elif str(explicit).casefold() != str(project).casefold():
                return 'OUTSIDE', 'project does not match'
        elif cwd and Path(project).is_absolute():
            if not Path(str(cwd)).is_absolute():
                return 'ADVISORY', 'project context is unavailable'
            state, reason = _path_applicability(project, cwd)
            if state != 'APPLICABLE':
                return state, reason
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


def reference_applicability(fields, card, event=None, *, card_text=None):
    if memspec.is_pending_capture(card):
        return 'OUTSIDE', 'unreviewed events are explicit-search evidence only'
    event = event if isinstance(event, dict) else {}
    # Captures/task references bind to their exact cwd, never another worktree's
    # task or account merely because it shares a Git common directory.
    origin = fields.get('cwd')
    if origin and event.get('cwd'):
        try:
            if not Path(event['cwd']).resolve().is_relative_to(Path(origin).resolve()):
                return 'OUTSIDE', 'cwd differs from captured task origin'
            return 'APPLICABLE', ''
        except (OSError, ValueError):
            return 'ADVISORY', 'captured cwd is unavailable'
    state, reason = _declared_reference_applicability(fields, card, event, card_text=card_text)
    if state == 'ADVISORY' and str(fields.get('scope', '')).casefold() != 'global':
        from . import capture_route
        path = Path(card).resolve()
        vault = next((parent for parent in path.parents if parent.name == 'memory'), path.parent)
        if capture_route.reference_owner_matches(vault, event.get('cwd')) is False:
            return 'OUTSIDE', 'native collision/ancestor has no per-card project ownership'
    return state, reason


def _declared_reference_applicability(fields, card, event=None, *, card_text=None):
    """Use declared lookup ownership only; source-bound rules use their reviewed scope."""
    if str(fields.get('scope', '')).casefold() == 'global':
        return 'ADVISORY', 'lookup project is not declared'
    project = fields.get('project')
    if not project:
        # Compatibility for explicit legacy declarations, never prose inference.
        try:
            if card_text is None:
                with Path(card).open('rb') as stream:
                    raw = stream.read(memspec.RECALL_SOURCE_SCAN_BYTES + 1)
                if len(raw) > memspec.RECALL_SOURCE_SCAN_BYTES:
                    return 'ADVISORY', 'lookup project declaration exceeds scan window'
                card_text = raw.decode('utf-8-sig')
            from . import card_lint
            card_type, _fields = card_lint.card_type_of(Path(card).name, card_text, Path(card))
            if card_type not in ('reference', 'project', 'pending'):
                return 'ADVISORY', 'lookup project is not declared'
            _, closing = memspec.split_frontmatter(card_text)
            if closing is None:
                return 'ADVISORY', 'lookup project is not declared'
            declarations = []
            fence = None
            for line in card_text.lstrip('\ufeff').splitlines()[closing + 1:]:
                marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
                if marker:
                    run, suffix = marker.groups()
                    if fence is None:
                        fence = (run[0], len(run))
                    elif run[0] == fence[0] and len(run) >= fence[1] and not suffix.strip():
                        fence = None
                    continue
                if fence is not None:
                    continue
                if re.match(r'^\*\*(?:適用專案|Applies to project)\*\*\s*[:：]', line):
                    match = re.match(
                        r'^\*\*(?:適用專案|Applies to project)\*\*\s*[:：]\s*`([^`]+)`\s*(?:[。.]|$)', line)
                    declarations.append(match.group(1) if match else None)
            if len(declarations) != 1 or not declarations[0]:
                return 'ADVISORY', 'lookup project declaration is absent or ambiguous'
            project = declarations[0]
            if not Path(project).is_absolute():
                return 'ADVISORY', 'lookup project path is not absolute'
        except (OSError, UnicodeError, ValueError):
            return 'ADVISORY', 'lookup project context is unavailable'
    return applicability({'project': project, 'actions': ['*']}, event)


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


def snapshot_path(destination, context_id=None):
    target = Path(destination).with_suffix('.rules.json')
    if context_id:
        key = hashlib.sha256(str(context_id).encode('utf-8')).hexdigest()
        target = target.with_name(target.name.removesuffix('.rules.json') + '.' + key + '.rules.json')
    return target


def snapshot(vaults, session, destination, *, context_id=None):
    records = []
    for vault in vaults:
        try:
            cards = json.loads(state_path(vault, session).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(cards, list):
            records.extend({'vault': str(Path(vault).resolve()), 'card': card}
                           for card in cards if isinstance(card, str))
    target = snapshot_path(destination, context_id)
    if records or context_id:
        target.parent.mkdir(parents=True, exist_ok=True)
        value = ({'schema': 2, 'context_id': context_id, 'records': records}
                 if context_id else records)
        target.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return records


def restore(destination, allowed_vaults, budget=1800, *, event=None, diagnostics=None,
            context_id=None):
    target = snapshot_path(destination, context_id)
    try:
        if context_id and not target.is_file():
            # Read old envelopes only through the same identity check below.
            target = snapshot_path(destination)
        records = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    if isinstance(records, dict):
        if (not context_id or records.get('schema') != 2
                or records.get('context_id') != context_id):
            if diagnostics is not None:
                diagnostics.append('rule snapshot belongs to a different or unconfirmed model context')
            return ''
        records = records.get('records')
    elif context_id:
        if diagnostics is not None:
            diagnostics.append('legacy rule snapshot has no model-context proof; current cards remain searchable')
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
        if not memspec.card_is_current(fields):
            continue
        description = ' '.join(str(fields.get('description', '')).split())[:240]
        if rule_provenance.FIELD in fields:
            try:
                card_text = target.read_text(encoding='utf-8-sig')
                fields, problem = memspec.frontmatter_text(card_text)
                if problem:
                    raise ValueError('source-bound card has malformed frontmatter: ' + problem)
                projected = rule_pipeline.projection(fields, target, card_text=card_text, allow_deferred=True)
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
            if reference_applicability(fields, target, event)[0] == 'OUTSIDE':
                continue
            if not description:
                continue
            line = f"本場規則路由 [UNVERIFIED；{item['card']}]：{description}"
        if len('\n'.join([*lines, line]).encode('utf-8')) <= budget:
            lines.append(line)
        elif diagnostics is not None:
            diagnostics.append(f"{item['card']}: context budget omitted the complete labelled entry")
    return '\n'.join(lines)
