"""Explicit adoption of already rendered native restrictions, without widening ownership."""
import ast
from pathlib import Path

from epitype import host_native, memspec


def prefix_patterns(text):
    """Read the official literal prefix_rule subset without executing a rules file."""
    patterns = []
    for statement in ast.parse(text).body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            raise ValueError('unsupported rules statement')
        call = statement.value
        if not isinstance(call.func, ast.Name) or call.func.id != 'prefix_rule' or call.args:
            raise ValueError('unsupported rules call')
        values = {item.arg: ast.literal_eval(item.value) for item in call.keywords}
        if values.get('decision') != 'forbidden' or not values.get('pattern'):
            raise ValueError('migration requires explicit forbidden prefixes')
        pattern = values['pattern']
        if not isinstance(pattern, list):
            raise ValueError('invalid prefix pattern')
        normalized = []
        for token in pattern:
            choices = [token] if isinstance(token, str) else token
            if not isinstance(choices, list) or not choices or not all(isinstance(x, str) for x in choices):
                raise ValueError('invalid prefix alternatives')
            normalized.append(tuple(sorted(set(choices))))
        patterns.append(tuple(normalized))
    return set(patterns)


def adopt_existing(vaults, home=None, legacy_rules=None):
    """Take ownership only after cards and existing host output have equal restrictions.

    Call after ordinary sync has rendered the candidate. A legacy Codex file is
    archived only when its entire forbidden prefix set equals the generated one.
    Unrelated user settings and other .rules files remain outside this operation.
    """
    state_file = host_native.state_path(home)
    with memspec.file_lock(state_file, 2.0) as acquired:
        if not acquired:
            raise TimeoutError('native migration lock unavailable')
        plans, state = host_native._plans(vaults, home=home)
        if any(p.problems or p.damaged or any(e.status != host_native.OK for e in p.entries) for p in plans):
            raise ValueError('sync must have no native drift before adoption')
        legacy = Path(legacy_rules) if legacy_rules else None
        original = legacy.read_bytes() if legacy else None
        if legacy:
            generated = [p for p in plans if p.target.kind == host_native.KIND_CODEX_RULES and p.target.root is None]
            if len(generated) != 1 or prefix_patterns(original.decode('utf-8-sig')) != prefix_patterns(generated[0].target.path.read_text(encoding='utf-8-sig')):
                raise ValueError('legacy and generated forbidden prefixes differ')
            archive = legacy.with_suffix(legacy.suffix + '.migrated')
            if archive.exists():
                raise ValueError('migration archive already exists')
        adopted = 0
        for plan in plans:
            key = host_native._key(plan.target.path)
            record = dict(state.get(key) or {})
            if plan.target.kind != host_native.KIND_CLAUDE:
                continue
            denies = dict(record.get(host_native.SECTION_DENY) or {})
            for entry in plan.entries:
                if entry.section == host_native.SECTION_DENY:
                    denies[entry.key] = {'card': entry.card, 'owned': True}
                    adopted += 1
            record[host_native.SECTION_DENY] = denies
            state[key] = record
        host_native._save_state(home, state)
        if legacy:
            # Recheck immediately before rename; retain an exact recoverable copy.
            if legacy.read_bytes() != original:
                raise ValueError('legacy rules changed during adoption')
            legacy.rename(archive)
        return adopted


def adopt_values(vaults, expected, home=None):
    """Explicit legacy-key takeover with exact before values; does not write settings.

    expected is a list of {path, section, key, before}. Only officially supported
    card-declared env/config keys can be adopted; unrelated keys stay unowned.
    Call ordinary sync afterward to apply the card policy.
    """
    with memspec.file_lock(host_native.state_path(home), 2.0) as acquired:
        if not acquired:
            raise TimeoutError('native migration lock unavailable')
        plans, state = host_native._plans(vaults, home=home)
        candidates = {}
        for plan in plans:
            conflicts = {memspec.HOST_NATIVE_CONFLICT_REASON.format(
                key=host_native._entry_name(plan.target, entry), have=host_native._shown(entry.have),
                card=entry.card, want=host_native._shown(entry.want))
                for entry in plan.entries if entry.status == host_native.CONFLICT}
            if plan.damaged or any(problem not in conflicts for problem in plan.problems):
                raise ValueError('invalid native migration plan')
            for entry in plan.entries:
                if entry.section in (host_native.SECTION_ENV, host_native.SECTION_CONFIG):
                    candidates[(host_native._key(plan.target.path), entry.section, entry.key)] = (plan, entry)
        selected, seen = [], set()
        for item in expected:
            key = (host_native._key(Path(item['path'])), item['section'], item['key'])
            if key in seen or key not in candidates:
                raise ValueError('migration key must be unique and declared by an official card')
            seen.add(key)
            plan, entry = candidates[key]
            parsed = host_native._parse(plan.target.kind, host_native._read(plan.target.path)[1])
            dotted = ('env.' + entry.key) if entry.section == host_native.SECTION_ENV else entry.key
            have, present, _shape = host_native._lookup(parsed, dotted)
            if not present or have != item['before']:
                raise ValueError('legacy value changed since review')
            selected.append((key, entry))
        for (path, section, key), entry in selected:
            record = dict(state.get(path) or {})
            section_record = dict(record.get(section) or {})
            section_record[key] = {'card':entry.card, 'owned':True}
            record[section] = section_record
            state[path] = record
        host_native._save_state(home, state)
        return len(selected)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('vaults', nargs='+')
    parser.add_argument('--legacy-rules')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-values', type=Path)
    args = parser.parse_args()
    if not args.apply:
        parser.error('adoption requires --apply after epitype sync --apply')
    if args.expected_values:
        import json
        print('ADOPTED VALUES', adopt_values(args.vaults,
              json.loads(args.expected_values.read_text(encoding='utf-8'))))
    else:
        print('ADOPTED', adopt_existing(args.vaults, legacy_rules=args.legacy_rules))
