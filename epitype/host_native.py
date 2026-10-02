import sys; sys.dont_write_bytecode = True
"""把卡片上的宿主原生欄位寫進兩個宿主自己的設定檔：卡片是正本，設定是渲染結果。

兩個宿主本身就擋得住一部分規則（Claude 的 permissions.deny／env、Codex 的 config.toml
與 execpolicy 規則檔），但那些設定原本是手寫的，同一條規則在 deny、rules 檔、卡片好幾處
各寫一份，沒有正本。這裡讓卡片成為唯一正本，由 `epitype sync` 渲染過去。

寫法刻意保守，因為那幾個檔是使用者自己的：
  - 只動 Epitype 自己寫的鍵，狀態檔記住是哪些；卡片拿掉時只拿回自己寫的；
  - 使用者原本就設了、值又跟卡片不同的鍵不替他改，報衝突，由他決定正本；
  - 改寫之後重新解析整份檔，除了被管理的鍵，其餘內容必須一模一樣，否則一個位元組都不寫；
  - 先備份再原子寫入；Codex 規則寫進獨立檔，手寫的 .rules 不碰。

`host_sync` 的 check／apply／remove 會呼叫這裡，所以預設只比對、明確要求才寫，這條
規矩跟規則塊同步是同一條。
"""

from collections import namedtuple
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import time
import tomllib

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import cardscan, memspec

KIND_CLAUDE = "claude_settings"
KIND_CODEX_CONFIG = "codex_config"
KIND_CODEX_RULES = "codex_rules"
SECTION_ENV = "env"
SECTION_DENY = "deny"
SECTION_CONFIG = "keys"
SECTION_RULES = "rules"
# 值要改成卡片的樣子（set）、要拿掉（remove）、只要從狀態表忘掉（forget），或什麼都不做。
OK, MISSING, DRIFT, CONFLICT, STALE, DISOWN, RELEASE = (
    "ok", "missing", "drift", "conflict", "stale", "disown", "release")
BACKUPS_KEPT = 3

Target = namedtuple("Target", "host kind path label vaults root")
Entry = namedtuple("Entry", "section key want have card status")
Plan = namedtuple("Plan", "target entries problems damaged notes raw")


def _graft():
    """install/graft.py 的保留位元組 JSON 手術刀。用到才載入：它很重，熱路徑用不到。"""
    from install import graft

    return graft


def state_path(home=None):
    """跟規則塊同步的狀態檔放在同一個目錄（見 host_sync._state_path）。"""
    return (Path(home) if home else Path.home()) / ".epitype" / memspec.HOST_NATIVE_STATE_FILENAME


def _card_reference(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError('native ownership source must be a relative .md card path')
    parts = value.replace('\\', '/').split('/')
    if (PureWindowsPath(value).drive or PureWindowsPath(value).is_absolute()
            or PurePosixPath(value).is_absolute() or any(not p or p.startswith(('.', '_')) for p in parts)
            or not parts[-1].endswith('.md') or parts[-1] == 'MEMORY.md'):
        raise ValueError('native ownership source must be a visible relative .md card path')
    relative = '/'.join(parts)
    return relative.casefold() if os.name == 'nt' else relative


def load_state(home=None, *, strict=False):
    try:
        with io.open(state_path(home), encoding="utf-8") as stream:
            loaded = json.loads(stream.read())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    targets = loaded.get("targets") if isinstance(loaded, dict) else None
    if strict and not isinstance(targets, dict):
        raise ValueError('native ownership state has no valid targets object')
    if strict:
        for path, record in targets.items():
            if not isinstance(path, str) or not path or not isinstance(record, dict):
                raise ValueError('native ownership target record must be an object with a path')
            kind = record.get('kind')
            if kind not in (KIND_CLAUDE, KIND_CODEX_CONFIG, KIND_CODEX_RULES):
                raise ValueError('native ownership target kind is missing or invalid')
            if 'created' in record and (not isinstance(record['created'], list)
                    or any(not isinstance(key, str) or not key for key in record['created'])):
                raise ValueError('native ownership created containers must be a string list')
            if 'created_file' in record and type(record['created_file']) is not bool:
                raise ValueError('native ownership created_file must be a boolean')
            allowed = {SECTION_ENV, SECTION_DENY} if kind == KIND_CLAUDE else (
                {SECTION_CONFIG} if kind == KIND_CODEX_CONFIG else {SECTION_RULES})
            for section in (SECTION_ENV, SECTION_DENY, SECTION_CONFIG, SECTION_RULES):
                if section not in record:
                    continue
                entries = record[section]
                if not isinstance(entries, dict) or (section not in allowed and entries):
                    raise ValueError('native ownership section is invalid for its target kind')
                for key, previous in entries.items():
                    if not isinstance(key, str) or not key:
                        raise ValueError('native ownership key must be a nonempty string')
                    if section == SECTION_RULES:
                        if not isinstance(previous, str) or not previous:
                            raise ValueError('native ownership rule source must be a string')
                        _card_reference(previous)
                        continue
                    if (not isinstance(previous, dict)
                            or type(previous.get('owned')) is not bool
                            or not isinstance(previous.get('card'), str) or not previous['card']):
                        raise ValueError('native ownership entry needs a source and boolean owned')
                    _card_reference(previous['card'])
                    if section in (SECTION_ENV, SECTION_CONFIG) and 'value' not in previous:
                        raise ValueError('native ownership entry has no verified historical value')
                    if section == SECTION_ENV and (not isinstance(previous['value'], str)
                            or not previous['value']):
                        raise ValueError('native ownership environment value must be a nonempty string')
                    if section == SECTION_CONFIG and not (
                            isinstance(previous['value'], str) and bool(previous['value'].strip())
                            or type(previous['value']) is int and previous['value'] > 0):
                        raise ValueError('native ownership config value must match its native scalar producer')
            if kind == KIND_CODEX_RULES and (not isinstance(record.get('fingerprint'), str)
                    or re.fullmatch(r'[0-9a-f]{16}', record['fingerprint']) is None):
                raise ValueError('native ownership rule fingerprint is missing or invalid')
    return targets if isinstance(targets, dict) else {}


def _save_state(home, targets):
    from epitype.host_sync import atomic_write

    payload = {"version": memspec.HOST_NATIVE_STATE_VERSION, "targets": targets}
    atomic_write(state_path(home), json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))


def _key(path):
    return os.path.normcase(os.fspath(Path(path)))


# --------------------------------------------------------------------------- 卡片


def _native_card_paths(vault, deadline):
    """Use the shared privacy boundaries, but propagate incomplete scans.

    Search can tolerate an inaccessible subtree; rendering owned settings cannot
    interpret that subtree as proof that its cards were removed.
    """
    found, pending = [], [Path(vault)]
    while pending:
        if time.monotonic() >= deadline:
            raise TimeoutError('native source verification budget exhausted')
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    raise TimeoutError('native source verification budget exhausted')
                if entry.name.startswith(('_', '.')):
                    continue
                info = entry.stat(follow_symlinks=False)
                if cardscan._is_link(entry, info):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif (entry.is_file(follow_symlinks=False)
                        and entry.name.lower().endswith('.md')
                        and entry.name != memspec.MEMORY_INDEX_FILENAME):
                    found.append(Path(entry.path))
    return sorted(found, key=lambda path: path.relative_to(vault).as_posix().casefold())


def _provenance_declares_native(fields):
    from epitype import rule_provenance
    try:
        document = rule_provenance.parse(fields) or {}
    except (ValueError, TypeError, RecursionError):
        return False  # Known native identities are checked independently of this hint.
    scope = document.get('scope')
    actions = scope.get('actions', []) if isinstance(scope, dict) else []
    if isinstance(actions, str):
        actions = [actions]
    return isinstance(actions, list) and any(action in (
        'host_sync', 'host_native', 'host_settings', 'native_settings') for action in actions)


def collect(vaults, *, event=None, diagnostics=None, seconds=5.0, owned_cards=()):
    """({section: ...}, [問題])——這些庫裡還在生效的卡要求宿主做什麼。

    一張卡的欄位寫壞、兩張卡對同一個鍵要不同的值，這個庫的宿主原生設定就整批不寫：
    寫一半會讓那張壞卡原本生效的規則在設定裡被悄悄拿掉，而那正是它要擋的東西。"""
    desired = {SECTION_ENV: {}, SECTION_DENY: {}, SECTION_CONFIG: {}, SECTION_RULES: []}
    problems = []
    diagnostics = diagnostics if diagnostics is not None else []
    deadline = time.monotonic() + seconds
    from epitype import rule_legacy, rule_pipeline, rule_context
    membership = rule_legacy.native_membership()
    if not membership.allowed:
        return desired, ['INVALID native baseline: ' + '; '.join(membership.issues)]
    native_paths = membership.native_paths
    owned_matches = {reference: [] for reference in owned_cards}

    def timed_out():
        if time.monotonic() < deadline:
            return False
        problems.append('UNVERIFIED native source: verification budget exhausted; next check: retry this exact vault group')
        return True

    for vault in vaults:
        if timed_out():
            break
        vault = Path(vault)
        try:
            paths = _native_card_paths(vault, deadline)
        except OSError as exc:
            problems.append(memspec.HOST_NATIVE_CARD_UNREADABLE_REASON.format(
                card=vault, error=type(exc).__name__))
            continue
        for path in paths:
            if timed_out():
                return desired, problems
            relative = path.relative_to(vault).as_posix() if path.is_relative_to(vault) else path.name
            reference_key = _card_reference(relative)
            if reference_key in owned_matches:
                owned_matches[reference_key].append(path)
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                # An unreadable ordinary-named card may own existing native keys.
                problems.append(memspec.HOST_NATIVE_CARD_UNREADABLE_REASON.format(
                    card=relative, error=type(exc).__name__))
                continue
            front, closing = memspec.split_frontmatter(text)
            identity = (reference_key in owned_matches or native_paths is not None
                        and rule_legacy.normalize_path(path.resolve()) in native_paths)
            if front is None or not memspec.host_native_declared(front):
                # An enrolled card remains the same identity after a shell
                # removes the fields that used to classify it as a native rule.
                fields, _problem = memspec.frontmatter_text(text)
                if native_paths is not None and not identity and not _provenance_declares_native(fields):
                    continue
                try:
                    if 'rule_provenance' in fields:
                        projected = rule_pipeline.projection(fields, path, card_text=text)
                        if identity and native_paths is not None and not (
                                projected['expired'] or fields.get('status', '').strip() in memspec.STOP_GATE_SILENT_STATUSES
                                or memspec.card_expired(fields.get(memspec.VALID_UNTIL_FIELD))
                                or memspec.card_expired(fields.get(memspec.GRANT_EXPIRES_FIELD))):
                            raise ValueError('native source identity lost execution fields without verified retirement')
                    else:
                        integrity = rule_pipeline.legacy_integrity(fields, path, card_text=text)
                        if identity and native_paths is not None and integrity is None:
                            raise ValueError('owned native source has no frozen identity or verified retirement')
                        if integrity is not None and not integrity.allowed:
                            problems.append(memspec.HOST_NATIVE_CARD_INVALID_REASON.format(
                                card=relative, problem='; '.join(integrity.issues)))
                except (ValueError, OSError, UnicodeError) as exc:
                    problems.append(memspec.HOST_NATIVE_CARD_INVALID_REASON.format(
                        card=relative, problem=str(exc)))
                continue
            fields, problem = memspec.frontmatter_text(text)
            if closing is None or problem:
                problems.append(memspec.HOST_NATIVE_CARD_INVALID_REASON.format(
                    card=relative, problem=problem or 'unclosed native card frontmatter'))
                continue
            try:
                projected = rule_pipeline.projection(fields, path, card_text=text)
            except (ValueError, OSError, UnicodeError) as exc:
                problems.append(memspec.HOST_NATIVE_CARD_INVALID_REASON.format(
                    card=relative, problem=str(exc)))
                continue
            if timed_out():
                return desired, problems
            if fields.get(memspec.DECISION_STATUS_FIELD, "").strip() in memspec.STOP_GATE_SILENT_STATUSES:
                continue
            if (projected['expired'] or memspec.card_expired(fields.get(memspec.VALID_UNTIL_FIELD))
                    or memspec.card_expired(fields.get(memspec.GRANT_EXPIRES_FIELD))):
                continue
            scope = projected['document'].get('scope')
            if scope is not None:
                applicable, reason = rule_context.applicability(scope, event or {})
                if applicable == 'OUTSIDE':
                    continue
                if applicable == 'ADVISORY':
                    problems.append(memspec.HOST_NATIVE_CARD_INVALID_REASON.format(
                        card=relative, problem='UNVERIFIED rule scope: ' + reason))
                    continue
            if projected['provenance']['status'] != 'VERIFIED':
                diagnostics.append(f'{relative}: UNVERIFIED legacy source; compatibility is not Owner authorship or scope proof')
            values, invalid = memspec.host_native_values(front)
            if invalid:
                problems.extend(memspec.HOST_NATIVE_CARD_INVALID_REASON.format(
                    card=relative, problem=problem) for problem in invalid)
                continue
            for section, field in ((SECTION_ENV, memspec.HOST_CLAUDE_ENV_FIELD),
                                   (SECTION_CONFIG, memspec.HOST_CODEX_CONFIG_FIELD)):
                for key, value in values.get(field, {}).items():
                    earlier = desired[section].get(key)
                    if earlier is not None and earlier[0] != value:
                        problems.append(memspec.HOST_NATIVE_CARD_CONFLICT_REASON.format(
                            key=key, first=earlier[1], first_value=_shown(earlier[0]),
                            second=relative, second_value=_shown(value)))
                        continue
                    desired[section].setdefault(key, (value, relative))
            for rule in values.get(memspec.HOST_CLAUDE_DENY_FIELD, ()):
                desired[SECTION_DENY].setdefault(rule, relative)
            for pattern in values.get(memspec.HOST_CODEX_RULES_FIELD, ()):
                if all(pattern != known for known, _card in desired[SECTION_RULES]):
                    desired[SECTION_RULES].append((pattern, relative))
    for reference, matches in owned_matches.items():
        if len(matches) > 1 or native_paths is not None and not matches:
            problems.append('UNVERIFIED native ownership source: ' + reference
                            + (' is absent or excluded from its target vault group' if not matches
                               else ' is ambiguous across its target vault group'))
    timed_out()
    return desired, problems


def _shown(value):
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- 目標


def targets(vaults, hosts=None, home=None):
    """每個可能要寫的設定檔。全域庫的卡→使用者層；專案庫的卡→那個專案根（與規則塊
    同步同一套對應：`host_sync.project_targets`）。"""
    from epitype import host_sync

    base = Path(home) if home else Path.home()
    selected = list(hosts or host_sync.installed_hosts(home))
    governance = host_sync.contract_vaults(vaults, home)
    found = []
    if governance and "claude" in selected:
        found.append(Target("claude", KIND_CLAUDE,
                            base / ".claude" / memspec.HOST_NATIVE_CLAUDE_USER_SETTINGS,
                            f"native:claude/{memspec.HOST_NATIVE_CLAUDE_USER_SETTINGS}",
                            governance, None))
    if governance and "codex" in selected:
        found.extend(_codex_targets(base / ".codex", "native:codex", governance, None))
    project, _skipped = host_sync.project_targets(vaults, selected, home)
    for host, root, vault in project:
        root = Path(root)
        label = f"native:project:{root.name}"
        if host == "claude":
            found.append(Target(
                host, KIND_CLAUDE, root / ".claude" / memspec.HOST_NATIVE_CLAUDE_PROJECT_SETTINGS,
                f"{label}/.claude/{memspec.HOST_NATIVE_CLAUDE_PROJECT_SETTINGS}", [vault], root))
        elif host == "codex":
            found.extend(_codex_targets(root / ".codex", f"{label}/.codex", [vault], root))
    return found


def _codex_targets(directory, label, vaults, root):
    rules = memspec.HOST_NATIVE_CODEX_RULES_DIRECTORY + "/" + memspec.HOST_NATIVE_CODEX_RULES_FILENAME
    return [
        Target("codex", KIND_CODEX_CONFIG, directory / memspec.HOST_NATIVE_CODEX_CONFIG,
               f"{label}/{memspec.HOST_NATIVE_CODEX_CONFIG}", vaults, root),
        Target("codex", KIND_CODEX_RULES, directory / memspec.HOST_NATIVE_CODEX_RULES_DIRECTORY
               / memspec.HOST_NATIVE_CODEX_RULES_FILENAME, f"{label}/{rules}", vaults, root),
    ]


# --------------------------------------------------------------------------- 比對


def _read(path):
    """(原始位元組或 None, 去掉 BOM 的文字, 有沒有 BOM)。"""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "", False
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw, raw[3:].decode("utf-8") if bom else raw.decode("utf-8"), bom


def _parse(kind, text):
    if not text.strip():
        return {}
    if kind == KIND_CLAUDE:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("JSON root is not an object")
        return value
    return tomllib.loads(text)


def _status(want, have, record, present):
    owned = bool(record and record.get("owned"))
    if present and have == want:
        return OK
    if not present:
        return MISSING if not owned else DRIFT
    return DRIFT if owned else CONFLICT


def _stale(record, have, present):
    if not record.get("owned") or not present:
        return RELEASE
    # Never guess the previous value from the value the user has now.
    if 'value' not in record:
        raise ValueError('native ownership entry has no verified historical value')
    return STALE if have == record['value'] else DISOWN


def _lookup(parsed, dotted):
    """(值, 在不在, 容器形狀對不對)。"""
    node = parsed
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return None, False, True
        if not isinstance(node, dict):
            return None, False, False
    if not isinstance(node, dict):
        return None, False, False
    return node.get(parts[-1]), parts[-1] in node, True


def plan(target, desired, record):
    """這個設定檔要改什麼。record 是狀態表裡這個檔上次的紀錄（沒有就是 {}）。"""
    try:
        raw, text, _bom = _read(target.path)
    except (OSError, UnicodeError) as exc:
        return Plan(target, [], [memspec.HOST_NATIVE_UNREADABLE_REASON.format(
            error=f"{type(exc).__name__}: {exc}")], True, [], None)
    if target.kind == KIND_CODEX_RULES:
        return _plan_rules(target, desired, record, raw, text)
    try:
        parsed = _parse(target.kind, text)
    except (ValueError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return Plan(target, [], [memspec.HOST_NATIVE_UNREADABLE_REASON.format(
            error=f"{type(exc).__name__}: {exc}")], True, [], raw)
    entries, problems = [], []
    sections = ((SECTION_ENV, "env."), (SECTION_DENY, None)) if target.kind == KIND_CLAUDE else (
        (SECTION_CONFIG, ""),)
    for section, prefix in sections:
        remembered = record.get(section) or {}
        if section == SECTION_DENY:
            listed, present_ok, shape_ok = _lookup(parsed, "permissions.deny")
            if present_ok and not isinstance(listed, list):
                shape_ok = False
            if not shape_ok:
                problems.append(memspec.HOST_NATIVE_SHAPE_REASON.format(
                    key="permissions.deny", container="permissions.deny"))
                continue
            listed = listed or []
            for rule, card in desired[SECTION_DENY].items():
                present = rule in listed
                entries.append(Entry(section, rule, True, present, card,
                                     _status(True, present, remembered.get(rule), present)))
            for rule, previous in remembered.items():
                if rule not in desired[SECTION_DENY]:
                    entries.append(Entry(section, rule, None, rule in listed, previous.get("card"),
                                         _stale(dict(previous, value=True), True, rule in listed)))
            continue
        wanted = desired[SECTION_ENV if section == SECTION_ENV else SECTION_CONFIG]
        for key, (value, card) in wanted.items():
            have, present, shape_ok = _lookup(parsed, prefix + key)
            if not shape_ok:
                problems.append(memspec.HOST_NATIVE_SHAPE_REASON.format(
                    key=key, container=(prefix + key).rsplit(".", 1)[0]))
                continue
            entries.append(Entry(section, key, value, have, card,
                                 _status(value, have, remembered.get(key), present)))
        for key, previous in remembered.items():
            if key in wanted:
                continue
            have, present, _shape = _lookup(parsed, prefix + key)
            entries.append(Entry(section, key, previous.get("value"), have, previous.get("card"),
                                 _stale(previous, have, present)))
    for entry in entries:
        if entry.status == CONFLICT:
            problems.append(memspec.HOST_NATIVE_CONFLICT_REASON.format(
                key=_entry_name(target, entry), have=_shown(entry.have), card=entry.card,
                want=_shown(entry.want)))
    notes = [memspec.HOST_NATIVE_DISOWNED_NOTE.format(key=_entry_name(target, entry),
                                                       have=_shown(entry.have))
             for entry in entries if entry.status == DISOWN]
    return Plan(target, entries, problems, False, notes, raw)


def rules_text(desired):
    """規則檔的完整內容；卡片沒有任何規則就回空字串。"""
    if not desired[SECTION_RULES]:
        return ""
    lines = [memspec.HOST_NATIVE_RULES_HEADER]
    for pattern, card in desired[SECTION_RULES]:
        parts = []
        for choices in pattern:
            quoted = [json.dumps(choice, ensure_ascii=False) for choice in choices]
            parts.append(quoted[0] if len(quoted) == 1 else "[" + ", ".join(quoted) + "]")
        justification = json.dumps(
            memspec.HOST_NATIVE_RULES_JUSTIFICATION.format(card=card), ensure_ascii=False)
        lines.append(f"prefix_rule(pattern=[{', '.join(parts)}], decision=\"forbidden\", "
                     f"justification={justification})\n")
    return "".join(lines)


def _rule_display(pattern):
    return " ".join("|".join(choices) for choices in pattern)


def _plan_rules(target, desired, record, raw, text):
    wanted = rules_text(desired)
    ours = text.startswith(memspec.HOST_NATIVE_RULES_HEADER.splitlines()[0])
    if raw is not None and not ours:
        if wanted or record:
            return Plan(target, [], [memspec.HOST_NATIVE_RULES_FOREIGN_REASON.format(
                path=target.path)], True, [], raw)
        return Plan(target, [], [], False, [], raw)
    if not wanted:
        status = STALE if raw is not None else (RELEASE if record else OK)
    elif raw is None:
        status = MISSING
    else:
        status = OK if text.replace("\r\n", "\n") == wanted else DRIFT
    card = ", ".join(sorted({card for _pattern, card in desired[SECTION_RULES]}))
    # have 放的是卡片要的每一條（樣式, 卡片）：寫入後的狀態紀錄要記得每一條來自哪張卡。
    return Plan(target, [Entry(SECTION_RULES, memspec.HOST_NATIVE_CODEX_RULES_FILENAME, wanted,
                               list(desired[SECTION_RULES]), card, status)], [], False, [], raw)


def _entry_name(target, entry):
    if entry.section == SECTION_ENV:
        return f"env.{entry.key}"
    if entry.section == SECTION_DENY:
        return f"permissions.deny[{entry.key}]"
    return entry.key


def _drift_line(target, entry):
    name = _entry_name(target, entry)
    if entry.section == SECTION_RULES:
        if entry.status == STALE:
            return memspec.HOST_NATIVE_RULES_STALE_REASON.format(field=memspec.HOST_CODEX_RULES_FIELD)
        return memspec.HOST_NATIVE_RULES_DRIFT_REASON.format(count=len(entry.have))
    if entry.status in (MISSING, DRIFT) and entry.section == SECTION_DENY:
        return memspec.HOST_NATIVE_MISSING_ITEM_REASON.format(key=name, card=entry.card)
    if entry.status == MISSING:
        return memspec.HOST_NATIVE_MISSING_REASON.format(
            key=name, card=entry.card, want=_shown(entry.want))
    if entry.status == DRIFT:
        return memspec.HOST_NATIVE_DRIFT_REASON.format(
            key=name, card=entry.card, want=_shown(entry.want), have=_shown(entry.have))
    return memspec.HOST_NATIVE_STALE_REASON.format(key=name, card=entry.card)


def _plans(vaults, hosts=None, home=None):
    """([Plan], {vault 組: 問題})——只收「卡片有要求」或「上次寫過東西」的那些檔。"""
    try:
        state = load_state(home, strict=True)
    except (OSError, ValueError, UnicodeError) as exc:
        target = Target('', '', state_path(home), 'native:ownership-state', [], None)
        return [Plan(target, [], [f'UNVERIFIED ownership state: {type(exc).__name__}: {exc}; '
            'retry reading this exact state before rendering'], True, [], None)], {}
    collected = {}
    plans = []
    actual_targets = targets(vaults, hosts, home)
    def target_group(target):
        return (tuple(os.fspath(Path(vault)) for vault in target.vaults),
                _key(target.root) if target.root is not None else None)
    group_cards = {}
    for target in actual_targets:
        references = group_cards.setdefault(target_group(target), set())
        record = state.get(_key(target.path)) or {}
        for section in (SECTION_ENV, SECTION_DENY, SECTION_CONFIG, SECTION_RULES):
            for previous in record.get(section, {}).values():
                references.add(_card_reference(previous if section == SECTION_RULES else previous['card']))
    for target in actual_targets:
        group = target_group(target)
        first = group not in collected
        if first:
            diagnostics = []
            desired, problems = collect(target.vaults, event={
                'cwd': str(target.root) if target.root is not None else None,
                'tool_name': 'host_sync'}, diagnostics=diagnostics, owned_cards=group_cards[group])
            collected[group] = desired, problems, diagnostics
        desired, problems, diagnostics = collected[group]
        if problems and not first:
            continue  # 同一個庫的卡片問題只講一次；它的每一個目標都整批不寫
        record = state.get(_key(target.path)) or {}
        if record and record['kind'] != target.kind:
            plans.append(Plan(target, [], ['UNVERIFIED ownership state: target kind does not match its actual interface'], False, [], None))
            continue
        if target.kind == KIND_CLAUDE:
            wanted = desired[SECTION_ENV] or desired[SECTION_DENY]
        elif target.kind == KIND_CODEX_CONFIG:
            wanted = desired[SECTION_CONFIG]
        else:
            wanted = desired[SECTION_RULES]
        if problems:
            plans.append(Plan(target, [], list(problems), False, list(diagnostics), None))
            continue
        if not wanted and not record:
            continue
        planned = plan(target, desired, record)
        plans.append(planned._replace(notes=[*planned.notes, *(diagnostics if first else [])]))
    return plans, state


def _untrusted_note(target, home):
    """Codex 專案層的設定只在使用者信任那個專案時才載入；不講的話，寫進去等於沒寫。"""
    if target.root is None or target.host != "codex":
        return None
    config = (Path(home) if home else Path.home()) / ".codex" / memspec.HOST_NATIVE_CODEX_CONFIG
    try:
        projects = tomllib.loads(config.read_text(encoding="utf-8-sig")).get("projects") or {}
    except (OSError, ValueError, UnicodeError, tomllib.TOMLDecodeError):
        projects = {}
    wanted = _key(target.root)
    for name, settings in projects.items() if isinstance(projects, dict) else ():
        try:
            same = _key(Path(name).resolve()) == _key(Path(target.root).resolve())
        except OSError:
            same = _key(name) == wanted
        if same and isinstance(settings, dict) and settings.get("trust_level") == "trusted":
            return None
    return memspec.HOST_NATIVE_UNTRUSTED_NOTE.format(root=target.root)


def check(vaults, hosts=None, home=None, output=sys.stdout):
    """(漂移數, 拒寫數)。唯讀。"""
    plans, _state = _plans(vaults, hosts, home)
    drift = refused = 0
    for item in plans:
        label = item.target.label
        for problem in item.problems:
            print(f"REFUSE {label}: {problem}", file=output)
            refused += 1
        for note in item.notes:
            print(f"NOTE   {label}: {note}", file=output)
        for entry in item.entries:
            if entry.status in (MISSING, DRIFT, STALE):
                print(f"DRIFT  {label}: {_drift_line(item.target, entry)}", file=output)
                drift += 1
        if any(entry.status in (MISSING, DRIFT) for entry in item.entries):
            note = _untrusted_note(item.target, home)
            if note:
                print(f"NOTE   {label}: {note}", file=output)
    return drift, refused


def drift_lines(vaults, home=None):
    """doctor 用：(每一行, 有沒有漂移或拒寫)。唯讀。"""
    report = io.StringIO()
    drift, refused = check(vaults, home=home, output=report)
    lines = [line for line in report.getvalue().splitlines() if line.strip()]
    if not drift and not refused and lines:
        # Diagnostics must not hide the independent rendering result in doctor.
        lines.insert(0, 'PASS host-native settings match the cards; legacy source/authorship remains UNVERIFIED')
    return lines, bool(drift or refused)


# --------------------------------------------------------------------------- 改寫


def _json_navigate(graft, text, keys):
    """(父節點, 成員或 None)：沿著 keys 走到最後一層的那個成員。中途缺了回 (最深的物件, None, 剩下的鍵)。"""
    node = graft._parse_json(text)
    for depth, key in enumerate(keys):
        member = graft._member(node, key)
        if member is None:
            return node, None, keys[depth:]
        if depth == len(keys) - 1:
            return node, member, []
        if member.value.kind != "object":
            raise ValueError(f"{key} is not an object")
        node = member.value
    return node, None, []


def _json_set(text, keys, value):
    graft = _graft()
    node, member, rest = _json_navigate(graft, text, keys)
    if member is None:
        payload = value
        for inner in reversed(rest[1:]):
            payload = {inner: payload}
        if len(rest) == len(keys) and not node.members:
            # 空的頂層物件（含剛建的檔）：graft 的插入不會替第一個成員換行縮排，整份照
            # 兩格縮排寫出來；這個物件裡本來什麼都沒有，沒有使用者的位元組會被動到。
            rendered = json.dumps({rest[0]: payload}, ensure_ascii=False, indent=2)
            return text[: node.start] + rendered.replace("\n", graft._newline(text)) + text[node.end:]
        return graft._append_object_member(text, node, rest[0], payload)
    pretty = "\n" in text or "\r" in text
    rendered = graft._render_json(value, pretty, graft._line_indent(text, member.start),
                                  graft._newline(text))
    return text[: member.value.start] + rendered + text[member.value.end:]


def _json_remove(text, keys):
    graft = _graft()
    node, member, _rest = _json_navigate(graft, text, keys)
    if member is None:
        return text
    return graft._remove_object_member(text, node, node.members.index(member))


def _json_append(text, keys, item):
    graft = _graft()
    node, member, _rest = _json_navigate(graft, text, keys)
    if member is None:
        return _json_set(text, keys, [item])
    if member.value.kind != "array":
        raise ValueError(".".join(keys) + " is not an array")
    return graft._append_array_item(text, member.value, item)


def _json_drop_item(text, keys, item):
    graft = _graft()
    _node, member, _rest = _json_navigate(graft, text, keys)
    if member is None or member.value.kind != "array":
        return text
    indices = [index for index, node in enumerate(member.value.items)
               if json.loads(text[node.start:node.end]) == item]
    return graft._remove_array_items(text, member.value, indices) if indices else text


def _json_empty(text, keys):
    graft = _graft()
    _node, member, _rest = _json_navigate(graft, text, keys)
    if member is None:
        return False
    value = member.value
    return (value.kind == "object" and not value.members) or (value.kind == "array" and not value.items)


_TOML_HEADER = re.compile(r"^[ \t]*\[[ \t]*([A-Za-z0-9_-]+)[ \t]*\][ \t]*(?:#.*)?$")
_TOML_ANY_HEADER = re.compile(r"^[ \t]*\[\[?[^\]]+\]\]?[ \t]*(?:#.*)?$")


def _newline_of(text):
    return "\r\n" if "\r\n" in text else "\n"


def _table_span(lines, table):
    if not table:
        # Root assignments end at the first table, including array tables.
        end = next((index for index, line in enumerate(lines)
                    if _TOML_ANY_HEADER.match(line.rstrip("\r\n"))), len(lines))
        return -1, end
    start = None
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if start is None:
            match = _TOML_HEADER.match(body)
            if match and match.group(1) == table:
                start = index
        elif _TOML_ANY_HEADER.match(body):
            return start, index
    return (start, len(lines)) if start is not None else None


def _toml_value(value):
    return str(value) if isinstance(value, int) else json.dumps(value, ensure_ascii=False)


def _toml_set(text, dotted, value):
    table, separator, leaf = dotted.partition(".")
    if not separator:
        table, leaf = "", table
    newline = _newline_of(text)
    lines = text.splitlines(keepends=True)
    rendered = f"{leaf} = {_toml_value(value)}"
    span = _table_span(lines, table)
    if span is None:
        prefix = text
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        separator = newline if prefix.strip() else ""
        return prefix + separator + f"[{table}]" + newline + rendered + newline
    start, end = span
    assignment = re.compile(r"^([ \t]*" + re.escape(leaf) + r"[ \t]*=[ \t]*)(.*?)(\r?\n)?$", re.DOTALL)
    for index in range(start + 1, end):
        match = assignment.match(lines[index])
        if match is None:
            continue
        current = match.group(2)
        kept = memspec.strip_inline_comment(current)
        comment = current[len(kept):] if current.startswith(kept) else ""
        lines[index] = match.group(1) + _toml_value(value) + comment + (match.group(3) or "")
        return "".join(lines)
    last = start
    for index in range(start + 1, end):
        if lines[index].strip():
            last = index
    if last >= 0 and not lines[last].endswith(("\n", "\r")):
        lines[last] += newline
    lines.insert(last + 1, rendered + newline)
    return "".join(lines)


def _toml_remove(text, dotted, drop_table=False):
    table, separator, leaf = dotted.partition(".")
    if not separator:
        table, leaf = "", table
    lines = text.splitlines(keepends=True)
    span = _table_span(lines, table)
    if span is None:
        return text
    start, end = span
    assignment = re.compile(r"^[ \t]*" + re.escape(leaf) + r"[ \t]*=")
    for index in range(start + 1, end):
        if assignment.match(lines[index]):
            del lines[index]
            end -= 1
            break
    if table and drop_table and not any(
            line.strip() and not line.strip().startswith("#") for line in lines[start + 1:end]):
        # 只有這個表是 Epitype 建的才整塊拿掉；拿掉時連同我們當初在它前面加的那一行空白。
        del lines[start:end]
        if start > 0 and not lines[start - 1].strip():
            del lines[start - 1]
    return "".join(lines)


def _toml_table_empty(text, table):
    lines = text.splitlines(keepends=True)
    span = _table_span(lines, table)
    if span is None:
        return False
    start, end = span
    return not any(line.strip() and not line.strip().startswith("#") for line in lines[start + 1:end])


def _without(parsed, removals):
    """把被管理的鍵從解析結果裡拿掉的深複本：拿來比對「其餘內容一模一樣」。"""
    copy = json.loads(json.dumps(parsed, default=str))
    for keys, item in removals:
        node = copy
        for key in keys[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                break
        else:
            if item is None:
                node.pop(keys[-1], None)
            elif isinstance(node.get(keys[-1]), list):
                node[keys[-1]] = [value for value in node[keys[-1]] if value != item]
    return copy


def _prune_empty(value):
    """空的物件／表／清單一律視同不存在：Epitype 建了又拿掉的容器，與原本就沒有是同一件事。"""
    if isinstance(value, dict):
        pruned = {key: _prune_empty(item) for key, item in value.items()}
        return {key: item for key, item in pruned.items()
                if not (isinstance(item, (dict, list)) and not item)}
    return value


def _render(item, record):
    """(新內容, 新的狀態紀錄, 要刪檔嗎)。內容驗證不過就丟 ValueError。"""
    target = item.target
    raw, text, bom = _read(target.path)
    if raw != item.raw:
        raise ValueError('native target changed after planning; retry the current complete file')
    created = list(record.get("created") or [])
    created_file = bool(record.get("created_file")) or raw is None
    if target.kind == KIND_CODEX_RULES:
        entry = item.entries[0]
        if entry.status == STALE:
            return None, {}, True
        if entry.status == RELEASE:
            return None, {}, False
        return entry.want, {"kind": target.kind, SECTION_RULES: {
            _rule_display(pattern): card for pattern, card in entry.have},
            "fingerprint": _fingerprint(entry.want), "created_file": created_file}, False
    before = _parse(target.kind, text)
    updated = text if text.strip() else ("{\n}\n" if target.kind == KIND_CLAUDE else "")
    remembered = {section: dict(record.get(section) or {})
                  for section in (SECTION_ENV, SECTION_DENY, SECTION_CONFIG)}
    touched = []
    for entry in item.entries:
        section = entry.section
        if entry.status == CONFLICT:
            continue
        if entry.status in (DISOWN, RELEASE):
            remembered[section].pop(entry.key, None)
            continue
        owned = bool((remembered[section].get(entry.key) or {}).get("owned"))
        if target.kind == KIND_CLAUDE and section == SECTION_ENV:
            keys = ["env", entry.key]
            touched.append((keys, None))
            if entry.status in (MISSING, DRIFT):
                if "env" not in before and "env" not in created:
                    created.append("env")
                updated = _json_set(updated, keys, entry.want)
                owned = True
            elif entry.status == STALE:
                updated = _json_remove(updated, keys)
                remembered[section].pop(entry.key, None)
                if "env" in created and _json_empty(updated, ["env"]):
                    updated = _json_remove(updated, ["env"])
                    created.remove("env")
                continue
            remembered[section][entry.key] = {"value": entry.want, "card": entry.card, "owned": owned}
        elif target.kind == KIND_CLAUDE:
            keys = ["permissions", "deny"]
            touched.append((keys, entry.key))
            if entry.status in (MISSING, DRIFT):
                permissions = before.get("permissions")
                if permissions is None and "permissions" not in created:
                    created.append("permissions")
                elif (isinstance(permissions, dict) and "deny" not in permissions
                      and "permissions.deny" not in created and "permissions" not in created):
                    created.append("permissions.deny")
                updated = _json_append(updated, keys, entry.key)
                owned = True
            elif entry.status == STALE:
                updated = _json_drop_item(updated, keys, entry.key)
                remembered[section].pop(entry.key, None)
                if "permissions.deny" in created and _json_empty(updated, keys):
                    updated = _json_remove(updated, keys)
                    created.remove("permissions.deny")
                if "permissions" in created and _json_empty(updated, keys):
                    updated = _json_remove(updated, keys)
                if "permissions" in created and _json_empty(updated, ["permissions"]):
                    updated = _json_remove(updated, ["permissions"])
                    created.remove("permissions")
                continue
            remembered[section][entry.key] = {"card": entry.card, "owned": owned}
        else:
            keys = entry.key.split(".")
            touched.append((keys, None))
            table = keys[0] if len(keys) > 1 else ""
            if entry.status in (MISSING, DRIFT):
                if table and table not in before and table not in created:
                    created.append(table)
                updated = _toml_set(updated, entry.key, entry.want)
                owned = True
            elif entry.status == STALE:
                drop = bool(table) and table in created and len([
                    other for other in remembered[section]
                    if other.split(".")[0] == table and other != entry.key]) == 0
                updated = _toml_remove(updated, entry.key, drop_table=drop)
                remembered[section].pop(entry.key, None)
                if drop and not _table_span(updated.splitlines(keepends=True), table):
                    created.remove(table)
                continue
            remembered[section][entry.key] = {"value": entry.want, "card": entry.card, "owned": owned}
    after = _parse(target.kind, updated)
    _verify(item, before, after, touched)
    new_record = {"kind": target.kind, "created": created, "created_file": created_file}
    for section, values in remembered.items():
        if values:
            new_record[section] = values
    delete = created_file and not _prune_empty(after) and not any(remembered.values())
    return updated, new_record, delete


def _verify(item, before, after, touched):
    """改寫後的檔：被管理的鍵是卡片要的值，其餘內容與改寫前一模一樣。"""
    for entry in item.entries:
        if entry.status in (CONFLICT, DISOWN, RELEASE):
            continue
        if entry.section == SECTION_DENY:
            listed, _present, _shape = _lookup(after, "permissions.deny")
            present = isinstance(listed, list) and entry.key in listed
            if present != (entry.status != STALE):
                raise ValueError(f"permissions.deny[{entry.key}]")
            continue
        dotted = ("env." + entry.key) if entry.section == SECTION_ENV else entry.key
        have, present, _shape = _lookup(after, dotted)
        if entry.status == STALE:
            if present:
                raise ValueError(dotted)
        elif not present or have != entry.want:
            raise ValueError(dotted)
    if _prune_empty(_without(before, touched)) != _prune_empty(_without(after, touched)):
        raise ValueError("其餘內容變了")


def _backup(path):
    graft = _graft()
    stamp = memspec.HOST_NATIVE_BACKUP_TAG + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = graft._backup_name(path, stamp)
    shutil.copy2(path, backup)
    prefix = path.name + graft.BACKUP_INFIX + memspec.HOST_NATIVE_BACKUP_TAG
    try:
        stale = sorted((entry for entry in path.parent.iterdir()
                        if entry.name.startswith(prefix) and entry.is_file()),
                       key=lambda entry: entry.stat().st_mtime)
        for old in stale[:-BACKUPS_KEPT]:
            old.unlink()
    except OSError:
        pass
    return backup


def _fingerprint(text):
    return hashlib.sha256(str(text or "").replace("\r\n", "\n").encode("utf-8")).hexdigest()[:16]


def _reverts(item, record, text):
    """這次寫入裡，哪些是把「別人手改過的受管值」改回卡片值：[(鍵, 原值, 卡片值, 卡片)]。

    卡片自己改了值（檔案裡還是 Epitype 上次寫的那個）是更新，不是改回；只有檔案裡的值
    不等於 Epitype 上次寫的，才是有人手改過。"""
    found = []
    for entry in item.entries:
        if entry.status != DRIFT:
            continue
        if entry.section == SECTION_RULES:
            if record.get("fingerprint") and _fingerprint(text) != record["fingerprint"]:
                found.append((entry.key, memspec.HOST_NATIVE_REVERT_RULES_BEFORE,
                              memspec.HOST_NATIVE_REVERT_RULES_AFTER.format(count=len(entry.have)),
                              entry.card))
            continue
        name = _entry_name(item.target, entry)
        if entry.section == SECTION_DENY:
            found.append((name, memspec.HOST_NATIVE_REVERT_ABSENT,
                          memspec.HOST_NATIVE_REVERT_PRESENT, entry.card))
            continue
        previous = (record.get(entry.section) or {}).get(entry.key) or {}
        if "value" not in previous or entry.have != previous["value"]:
            found.append((name, _shown(entry.have), _shown(entry.want), entry.card))
    return found


def _remember(state, path_key, record):
    """一個鍵都不再管理的檔，從狀態表拿掉：留著的話，寫檔閘會繼續護著一個沒有主人的檔。"""
    if any(record.get(section) for section in (SECTION_ENV, SECTION_DENY, SECTION_CONFIG, SECTION_RULES)):
        state[path_key] = record
    else:
        state.pop(path_key, None)


def apply(vaults, hosts=None, home=None, output=sys.stdout):
    """(寫了幾個檔, 拒寫幾件)。衝突的那一個鍵不寫，同一個檔裡其餘的照寫。"""
    from epitype.host_sync import atomic_write

    plans, state = _plans(vaults, hosts, home)
    written = refused = 0
    for item in plans:
        label = item.target.label
        for problem in item.problems:
            print(f"REFUSE {label}: {problem}", file=output)
            refused += 1
        for note in item.notes:
            print(f"NOTE   {label}: {note}", file=output)
        if item.damaged or not item.entries:
            continue
        path_key = _key(item.target.path)
        record = state.get(path_key) or {}
        changing = [entry for entry in item.entries if entry.status in (MISSING, DRIFT, STALE)]
        try:
            updated, new_record, delete = _render(item, record)
            raw, current_text, bom = _read(item.target.path)
            if raw != item.raw:
                raise ValueError('native target changed during rendering; retry the current complete file')
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
            print(f"REFUSE {label}: " + memspec.HOST_NATIVE_VERIFY_REASON.format(
                detail=f"{type(exc).__name__}: {exc}"), file=output)
            refused += 1
            continue
        reverts = _reverts(item, record, current_text)
        try:
            if delete:
                if raw is not None:
                    item.target.path.unlink()
                state.pop(path_key, None)
                print(f"REMOVED {label}: 卡片已沒有要求，這個檔整份是 Epitype 建的，已刪除 "
                      f"{item.target.path}", file=output)
                written += 1
            elif changing:
                payload = updated.encode("utf-8")
                if bom:
                    payload = b"\xef\xbb\xbf" + payload
                if raw != payload:
                    backup = _backup(item.target.path) if raw is not None else None
                    atomic_write(item.target.path, payload)
                    written += 1
                    names = "、".join(_entry_name(item.target, entry) for entry in changing)
                    print(f"WROTE  {label}: {names} → {item.target.path}"
                          + (f"（備份 {backup.name}）" if backup else ""), file=output)
                    for key, before, after, card in reverts:
                        print(memspec.HOST_NATIVE_REVERT_LINE.format(
                            prefix=memspec.HOST_NATIVE_REVERT_PREFIX, label=label, key=key,
                            card=card, before=before, after=after), file=output)
                _remember(state, path_key, new_record)
            else:
                _remember(state, path_key, new_record)
        except OSError as exc:
            print(f"REFUSE {label}: 寫入失敗 {type(exc).__name__}: {exc}", file=output)
            refused += 1
            continue
        if any(entry.status in (MISSING, DRIFT) for entry in item.entries):
            note = _untrusted_note(item.target, home)
            if note:
                print(f"NOTE   {label}: {note}", file=output)
        try:
            _save_state(home, state)
        except OSError as exc:
            print(f"REFUSE {label}: 狀態檔寫不進去 {type(exc).__name__}: {exc}", file=output)
            refused += 1
    return written, refused


def remove_all(home=None, output=sys.stdout):
    """解除安裝：把 Epitype 寫過的鍵全部拿回來，使用者原有的一個都不動。回傳改了幾個檔。"""
    from epitype.host_sync import atomic_write

    try:
        state = load_state(home, strict=True)
    except (OSError, ValueError) as exc:
        print('REFUSE UNVERIFIED ownership state: ' + str(exc), file=output)
        return 0
    removed = 0
    empty = {SECTION_ENV: {}, SECTION_DENY: {}, SECTION_CONFIG: {}, SECTION_RULES: []}
    for path_key, record in sorted(state.items()):
        kind = record.get("kind")
        path = Path(path_key)
        if kind not in (KIND_CLAUDE, KIND_CODEX_CONFIG, KIND_CODEX_RULES):
            continue
        target = Target("", kind, path, f"native:{path}", [], None)
        item = plan(target, empty, record)
        if item.damaged:
            # 讀不動的檔不猜：狀態留著，修好之後再跑一次解除安裝還拿得回來。
            for problem in item.problems:
                print(f"REFUSE {target.label}: {problem}", file=output)
            continue
        if not any(entry.status == STALE for entry in item.entries):
            state.pop(path_key, None)
            continue
        try:
            updated, _new_record, delete = _render(item, record)
            raw, _text, bom = _read(path)
            if delete or (kind == KIND_CODEX_RULES and item.entries[0].status == STALE):
                if raw is not None:
                    path.unlink()
                print(f"REMOVED {target.label}: 整份是 Epitype 建的，已刪除", file=output)
            elif raw is not None and any(entry.status == STALE for entry in item.entries):
                payload = updated.encode("utf-8")
                payload = (b"\xef\xbb\xbf" + payload) if bom else payload
                if payload != raw:
                    _backup(path)
                    atomic_write(path, payload)
                print(f"REMOVED {target.label}: Epitype 寫的鍵已拿掉，其餘內容原樣保留", file=output)
            removed += 1
        except (OSError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
            print(f"REFUSE {target.label}: {type(exc).__name__}: {exc}", file=output)
            continue
        state.pop(path_key, None)
    try:
        _save_state(home, state)
    except OSError:
        pass
    return removed
