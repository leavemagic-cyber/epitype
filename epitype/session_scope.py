"""Local session project selection. Candidates are not card authority.

No session discovery: only a supplied, host-contained transcript header is read.
The host's legacy candidates remain intact when an optional source is unavailable.
"""
from dataclasses import dataclass
from pathlib import Path
import argparse
import json
import os
import sys
import stat
import tempfile
from . import capture_route, memspec

MAX_HEADER_BYTES = 32768
MAX_CONFIG_BYTES = 1024 * 1024
_EVENT_PROJECT = '_epitype_session_project'


class _ProjectEvidence(dict):
    # Serializes as an empty object; JSON input cannot forge this Python type.
    def __init__(self, project):
        super().__init__()
        self.project = project


@dataclass(frozen=True)
class Project:
    vault: Path
    roots: tuple[Path, ...]
    home: bool = False
    reason: str = "native"


@dataclass(frozen=True)
class Resolution:
    project: Project | None
    diagnostics: tuple[str, ...] = ()


def _path(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        return None
    try:
        path = Path(value).expanduser()
        # Relative/drive-relative paths cannot establish an ownership boundary.
        if not path.is_absolute() or str(path).startswith(('\\\\', '//')):
            return None
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _registered(config):
    return tuple(dict.fromkeys(p for item in config.get('vaults', ())
                              if (p := _path(item)) is not None and p.is_dir()))


def _governance(config):
    paths = [_path(p) for p in config.get('vaults', ())]
    paths = [p for p in paths if p is not None]
    return next((p for p in paths if (p / memspec.WORK_LEDGER_FILENAME).is_file()),
                paths[0] if paths else None)


def mappings(config):
    """Return valid (root, vault) pairs and visible rejected declarations."""
    diagnostics, pairs = [], []
    registered = _registered(config)
    raw = config.get('project_roots', {})
    if not isinstance(raw, dict):
        return [], ['project_roots 必須是物件；已忽略']
    for value, roots in raw.items():
        vault = _path(value)
        if vault not in registered:
            diagnostics.append(f'對應的記憶庫無法使用或未註冊： {value}')
            continue
        if not isinstance(roots, list):
            diagnostics.append(f'對應的專案根目錄必須是清單： {value}')
            continue
        for value in roots:
            root = _path(value)
            if root is None or not root.is_dir():
                diagnostics.append(f'對應的專案根目錄無法使用： {value}')
                continue
            if (root, vault) not in pairs:
                pairs.append((root, vault))
    owners = {}
    for root, vault in pairs:
        owners.setdefault(root, set()).add(vault)
    ambiguous = {root for root, vaults in owners.items() if len(vaults) > 1}
    diagnostics.extend(f'專案根目錄對應不明確，已忽略： {root}' for root in sorted(ambiguous))
    return [(r, v) for r, v in pairs if r not in ambiguous], diagnostics


def _native(cwd, home):
    try:
        return capture_route.native_cwd_vaults(cwd, home)
    except (OSError, ValueError, TypeError, RuntimeError):
        return []


def _home_vault(event, config, home, diagnostics):
    value = event.get('transcript_path')
    if not value:
        return None
    path = _path(value)
    base = _path(home if home is not None else Path.home())
    if path is None or base is None:
        diagnostics.append('會話起始專案不明：對話紀錄路徑無效')
        return None
    registered = _registered(config)
    # Resolve the host folders too: a junctioned ~/.claude must not silently
    # disable the home step (audit 2026-11-12).
    claude = _path(base / '.claude' / 'projects') or base / '.claude' / 'projects'
    if path.is_relative_to(claude):
        parts = path.relative_to(claude).parts
        # Main transcript, or the host's project/session/subagents transcript.
        valid = (len(parts) == 2 or
                 len(parts) == 4 and parts[2] == 'subagents')
        vault = (claude / parts[0] / 'memory').resolve() if parts else None
        if (valid and path.suffix == '.jsonl' and vault in registered
                and vault.is_relative_to(claude) and capture_route.holds_cards(vault)):
            return vault
        diagnostics.append('會話起始專案不明：Claude 對話紀錄未註冊或格式不符')
        return None
    codex = _path(os.environ.get('CODEX_HOME')) or _path(base / '.codex') or base / '.codex'
    if not (path.is_relative_to(codex / 'sessions') or
            path.is_relative_to(codex / 'archived_sessions')):
        diagnostics.append('會話起始專案不明：對話紀錄不在主機指定目錄內')
        return None
    if not path.name.startswith('rollout-') or path.suffix != '.jsonl':
        diagnostics.append('會話起始專案不明：Codex 對話紀錄格式不符')
        return None
    try:
        if not path.is_file():
            raise ValueError('not a regular transcript file')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('not a regular transcript file')
            raw = stream.readline(MAX_HEADER_BYTES + 1)
        if len(raw) > MAX_HEADER_BYTES:
            raise ValueError('oversized header')
        row = json.loads(raw)
        meta = row.get('payload') if isinstance(row, dict) else None
        if row.get('type') != 'session_meta' or not isinstance(meta, dict):
            raise ValueError('missing session metadata')
        # A parent id alone does not establish the parent's home. No other
        # session is opened to guess it (v2 C3).
        if meta.get('parent_thread_id') or isinstance(meta.get('source'), dict) and meta['source'].get('subagent'):
            diagnostics.append('會話起始專案不明：事件未能確認父會話的起始專案')
            return None
        start = _path(meta.get('cwd'))
        if start is None:
            raise ValueError('invalid session cwd')
        vaults = _native(str(start), base)
        # A lossy slug can name another project's vault; the existing owner
        # check decides, exactly as capture routing does (audit 2026-11-12).
        if (vaults and vaults[0] in registered
                and capture_route.reference_owner_matches(vaults[0], start) is not False):
            return vaults[0]
        diagnostics.append('會話起始專案不明：起始工作目錄沒有已註冊的原生記憶庫')
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        diagnostics.append('會話起始專案不明：Codex 檔頭無法讀取或格式不符')
    return None


_EVENT_RESOLUTION = '_epitype_session_resolution'


class _ResolutionMemo(dict):
    # Same trick as _ProjectEvidence: JSON input cannot forge this Python type.
    def __init__(self, key, resolution):
        super().__init__()
        self.key = key
        self.resolution = resolution


def resolve(event, config, home=None):
    """One resolution per hook event: callers ask several times per tool call."""
    if not isinstance(event, dict):
        return _resolve(event, config, home)
    key = (id(config), str(home), str(event.get('cwd')), str(event.get('transcript_path')))
    memo = event.get(_EVENT_RESOLUTION)
    if isinstance(memo, _ResolutionMemo) and memo.key == key:
        return memo.resolution
    resolution = _resolve(event, config, home)
    if isinstance(event, dict):
        event[_EVENT_RESOLUTION] = _ResolutionMemo(key, resolution)
    return resolution


def _native_shaped(vault):
    return vault.name == 'memory' and vault.parent.parent.name == 'projects'


def _resolve(event, config, home=None):
    diagnostics = []
    try:
        if not isinstance(event, dict) or not isinstance(config, dict):
            return Resolution(None, ('專案不明：事件或設定無效',))
        pairs, issues = mappings(config)
        diagnostics.extend(issues)
        governance = _governance(config)
        native_home = _home_vault(event, config, home, diagnostics)
        home_roots = None
        if native_home is not None and native_home != governance and _native_shaped(native_home):
            # Lossy folder names (dev/a-b vs dev/a/b) put two projects' transcripts
            # in one host folder; then location alone cannot name the project
            # (audit 2026-11-12). A failed lookup keeps the proven home (its
            # location still holds); only observed evidence of two roots drops it.
            try:
                home_roots = capture_route.project_roots_of(native_home)
            except (OSError, ValueError, RuntimeError):
                diagnostics.append('原生專案根目錄無法取得')
            if home_roots is not None and len(home_roots) > 1:
                diagnostics.append('會話起始專案不明：同一個對話紀錄資料夾對應到多個專案根目錄，不採用')
                native_home = None
        chosen = native_home if native_home != governance else None
        reason = "home" if chosen is not None else "native"
        cwd = _path(event.get('cwd'))
        if chosen is None and cwd is not None:
            matches = [(r, v) for r, v in pairs if cwd.is_relative_to(r) and v != governance]
            if matches:
                chosen = max(matches, key=lambda pair: len(pair[0].parts))[1]
                reason = "mapped"
        if chosen is None:
            chosen = next((p for p in _native(event.get('cwd'), home) if p != governance), None)
            # A lossy slug (dev/a-b vs dev/a/b) can name another project's
            # vault; same owner check as capture routing (audit 2026-11-12).
            # An unusable cwd (UNC, extended-length, relative) cannot pass the owner check either.
            if (chosen is not None and (cwd is None
                    or capture_route.reference_owner_matches(chosen, cwd) is False)):
                diagnostics.append('原生記憶庫的專案根目錄與目前目錄不符，不採用')
                chosen = None
        if chosen is None:
            return Resolution(None, tuple(diagnostics))
        roots = [r for r, v in pairs if v == chosen]
        native_roots = []
        if _native_shaped(chosen):
            # Custom vaults have no host transcripts; never glob or cache there.
            try:
                native_roots = (list(home_roots) if chosen == native_home and home_roots is not None
                                else capture_route.project_roots_of(chosen))
            except (OSError, ValueError, RuntimeError):
                diagnostics.append('原生專案根目錄無法取得')
            roots.extend(native_roots)
            # Legacy ASCII locator for a vault with no transcript yet: same
            # preconditions as capture_route._reference_owner_matches, so an
            # observed root is never overridden by a colliding slug.
            if (cwd is not None and not native_roots
                    and not any(chosen.parent.glob('*.jsonl'))):
                for parent in (cwd, *cwd.parents):
                    if (chosen.parent.name == capture_route.project_slug(parent)
                            and str(parent).isascii()):
                        roots.append(parent)
                        break
        return Resolution(Project(chosen, tuple(dict.fromkeys(roots)), chosen == native_home, reason),
                          tuple(diagnostics))
    except Exception as error:
        return Resolution(None, (*diagnostics, f'專案不明： {type(error).__name__}'))


def session_project(event, config, home=None):
    return resolve(event, config, home).project


def candidates(existing, event, config, home=None):
    """Preserve the caller's legacy result; add only the chosen session project."""
    result = list(existing)
    resolved = resolve(event, config, home)
    if isinstance(event, dict):
        # A typed in-process object, never accepted from JSON hook input.
        event[_EVENT_PROJECT] = _ProjectEvidence(resolved.project)
    if resolved.project and resolved.project.vault not in result:
        result.append(resolved.project.vault)
    return result


def card_in_project(card, event):
    """Widen only cards physically inside the selected project's vault."""
    evidence = event.get(_EVENT_PROJECT) if isinstance(event, dict) else None
    project = evidence.project if isinstance(evidence, _ProjectEvidence) else None
    if not isinstance(project, Project) or card is None:
        return False
    path = _path(card)
    cwd = _path(event.get('cwd'))
    if path is None or not path.is_relative_to(project.vault):
        return False
    return project.home or cwd is not None and any(cwd.is_relative_to(r) for r in project.roots)


def doctor_lines(config, home=None):
    try:
        return _doctor_lines(config, home)
    except Exception as error:  # doctor runs before graft validates the config
        return [f'專案範圍檢查無法執行：{type(error).__name__}']


def _doctor_lines(config, home=None):
    lines = ['主動回憶：' + ('開啟' if config.get('proactive_recall') is True else '關閉')]
    _, issues = mappings(config)
    lines.extend('專案根目錄：' + item for item in issues)
    base = Path(home) if home is not None else Path.home()
    if (base / '.claude' / 'hooks' / 'recall_router.json').is_file():
        lines.append('舊版路由設定：檔案存在；Epitype 回憶功能不使用此檔')
    return lines


def _read_config(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('設定檔超過 1 MiB')
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get('vaults'), list):
        raise ValueError('設定檔必須包含 vaults 清單')
    return value


def _write_config(path, config):
    # Same-directory replace, bounded lock held by the caller, preserve all
    # unknown keys. A failed Windows replace leaves the original intact.
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.roots-', delete=False) as stream:
            name = stream.name
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name is not None:
            try:
                Path(name).unlink(missing_ok=True)
            except OSError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description='查詢會話專案範圍，或管理專案根目錄與記憶庫的對應。')
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('EPITYPE_CONFIG',
                        str(Path.home() / '.epitype' / 'config.json'))))
    sub = parser.add_subparsers(dest='command', required=True)
    root_parser = sub.add_parser('roots')
    root_parser.add_argument('--config', type=Path, default=argparse.SUPPRESS)
    roots = root_parser.add_subparsers(dest='action', required=True)
    for action in ('add', 'remove'):
        p = roots.add_parser(action); p.add_argument('directory'); p.add_argument('--vault', required=True)
        p.add_argument('--config', type=Path, default=argparse.SUPPRESS)
    roots.add_parser('list').add_argument('--config', type=Path, default=argparse.SUPPRESS)
    p = sub.add_parser('scope'); p.add_argument('--cwd', default=os.getcwd()); p.add_argument('--transcript')
    p.add_argument('--config', type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == 'roots' and args.action in ('add', 'remove'):
            with memspec.file_lock(args.config, 1.0) as held:
                if not held:
                    raise ValueError('無法取得設定檔鎖定；未變更')
                config = _read_config(args.config)
                root, vault = _path(args.directory), _path(args.vault)
                if root is None or vault is None:
                    raise ValueError('專案根目錄與記憶庫必須使用本機絕對路徑')
                if args.action == 'add' and (not root.is_dir() or vault not in _registered(config)):
                    raise ValueError('專案根目錄必須存在，記憶庫必須已註冊且可使用')
                raw = config.setdefault('project_roots', {})
                if not isinstance(raw, dict):
                    raise ValueError('project_roots 格式不符；未變更')
                if args.action == 'add':
                    for key, values in raw.items():
                        if not isinstance(values, list):
                            raise ValueError('對應的專案根目錄格式不符；未變更')
                        if _path(key) != vault and any(_path(v) == root for v in values):
                            raise ValueError('專案根目錄已屬於另一個記憶庫；未變更')
                key = next((k for k in raw if _path(k) == vault), str(vault))
                values = raw.get(key, [])
                if not isinstance(values, list):
                    raise ValueError('對應的專案根目錄格式不符；未變更')
                values = [v for v in values if _path(v) != root]
                if args.action == 'add':
                    values.append(str(root))
                if values:
                    raw[key] = values
                else:
                    raw.pop(key, None)
                _write_config(args.config, config)
                print('專案根目錄：已儲存')
                return 0
        config = _read_config(args.config)
        if args.command == 'roots':
            print(json.dumps(config.get('project_roots', {}), ensure_ascii=False, indent=2))
            for issue in mappings(config)[1]:
                print('警告：' + issue)
        else:
            event = {'cwd': args.cwd, 'transcript_path': args.transcript}
            resolved = resolve(event, config)
            existing = _native(args.cwd, None)
            governance = _governance(config)
            if governance and governance not in existing:
                existing.append(governance)
            for vault in candidates(existing, event, config):
                reasons = []
                if vault in _native(args.cwd, None): reasons.append('目前目錄')
                if vault == governance: reasons.append('治理記憶庫')
                if resolved.project and vault == resolved.project.vault:
                    reasons.append({'home': '會話起始專案', 'mapped': '會話專案對應',
                                    'native': '會話原生專案'}[resolved.project.reason])
                print(str(vault) + ' [' + ', '.join(reasons) + ']')
            for issue in resolved.diagnostics:
                print('警告：' + issue)
        return 0
    except (OSError, ValueError, TypeError) as error:
        print('Epitype 範圍：' + str(error), file=sys.stderr)
        return 1
