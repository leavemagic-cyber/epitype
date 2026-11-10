"""Complete card-lint snapshots, produced by maintenance and read by hooks.

Reading a result never imports the lint engine or validates examples. These
snapshots are accounting only; consumers still verify the rules they use.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from . import cardscan, memspec

VERSION = 1
FILENAME = 'card-lint-result.json'
MAX_AGE_SECONDS = 24 * 3600


def result_path(vault):
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY / FILENAME


def _stamp(path):
    info = Path(path).stat()
    return [info.st_mtime_ns, info.st_size, info.st_ctime_ns]


def _engine():
    # Examples call both gates. A changed validator must invalidate old results
    # even when the snapshot format and all cards remain unchanged.
    package = Path(__file__).resolve().parent
    files = sorted(package.glob('*.py'))
    files += [package.parent/'adapters'/'claude'/name
              for name in ('pretooluse_gate.py', 'stop_gate.py')]
    return {str(path): _stamp(path) for path in files}


def _manifest(vault):
    return {key: [mtime, size, ctime] for key, _path, mtime, size, ctime
            in cardscan.scan_vault(vault, strict=True)}


def _source_paths(vault, manifest, max_bytes):
    """Record external provenance dependencies during maintenance only."""
    paths = {str(memspec.config_path().expanduser().resolve())}
    baseline = memspec.config_options().get('rule_source_baseline')
    if isinstance(baseline, str) and baseline:
        paths.add(str(Path(baseline).expanduser().resolve()))

    def collect(value, base):
        if isinstance(value, dict):
            for key in ('path', 'archive'):
                if not isinstance(value.get(key), str):
                    continue
                path = Path(value[key]).expanduser()
                path = (path if path.is_absolute() else base/path).resolve()
                paths.add(str(path))
                if path.suffix.casefold() == '.jsonl':
                    paths.add(str(path.with_suffix('.jsonl.zst')))
            for child in value.values():
                collect(child, base)
        elif isinstance(value, list):
            for child in value:
                collect(child, base)

    for relative in manifest:
        if manifest[relative][1] > max_bytes:
            continue
        text = (vault/relative).read_text(encoding='utf-8-sig')
        fields, _problem = memspec.frontmatter_text(text)
        raw = fields.get('rule_provenance')
        if raw:
            try:
                collect(json.loads(raw), (vault/relative).parent)
            except (ValueError, TypeError):
                pass  # The lint report diagnoses malformed provenance.
    return sorted(paths)


def _dependencies(paths):
    result = {}
    for path in paths:
        try:
            result[path] = _stamp(path)
        except OSError:
            result[path] = None
    return result


def _complete(report, manifest):
    if not isinstance(report, dict) or report.get('timed_out') is not False:
        return False
    for name in ('total', 'fail', 'warn', 'info', 'fail_cards', 'oversized_skipped', 'native_docs_skipped'):
        value = report.get(name)
        if type(value) is not int or value < 0:
            return False
    return (report['oversized_skipped'] == 0
            and report['total'] + report['native_docs_skipped'] == len(manifest)
            and isinstance(report.get('cards'), list)
            and isinstance(report.get('vault_findings'), list))


def _write(vault, value):
    destination = result_path(vault)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name('.' + destination.name + '.tmp-' + str(os.getpid()))
    try:
        staging.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)


def refresh(vault, today=None):
    """Run the full per-card lint and examples; publish only a stable snapshot."""
    from . import card_lint, rule_examples

    vault = Path(vault).resolve(strict=True)
    value = dict(version=VERSION, complete=False, vault=str(vault))
    try:
        if any(module is None for module in rule_examples._gate_modules()):
            raise RuntimeError('example validators unavailable; lint result is incomplete')
        before = _manifest(vault)
        engine = _engine()
        dependencies = _dependencies(_source_paths(vault, before, card_lint.MAX_CARD_BYTES))
        report = card_lint.scan_vault(vault, today=today)
        after = _manifest(vault)
        value.update(report=report, manifest=before, engine=engine, dependencies=dependencies,
                     checked_at=time.time(), checked_date=(today or datetime.now(timezone.utc).date()).isoformat())
        value['complete'] = (before == after and engine == _engine()
                             and dependencies == _dependencies(dependencies)
                             and _complete(report, before))
    except Exception:
        _write(vault, value)
        raise
    _write(vault, value)
    return report


def read(vault, *, now=None):
    """Return explicit unknown/stale states; absence of findings is never proof."""
    vault = Path(vault).resolve()
    try:
        value = json.loads(result_path(vault).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'status': 'missing'}
    except (OSError, ValueError, UnicodeError):
        return {'status': 'incomplete'}
    if (not isinstance(value, dict) or value.get('complete') is not True
            or not isinstance(value.get('manifest'), dict)
            or not _complete(value.get('report'), value['manifest'])):
        return {'status': 'incomplete'}
    now = time.time() if now is None else now
    checked = value.get('checked_at')
    dependencies = value.get('dependencies')
    if (type(checked) not in (int, float) or not 0 <= now-checked < MAX_AGE_SECONDS
            or value.get('checked_date') != datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            or value.get('version') != VERSION or value.get('vault') != str(vault)
            or not isinstance(dependencies, dict)
            or str(memspec.config_path().expanduser().resolve()) not in dependencies):
        return {'status': 'stale'}
    try:
        current = (value.get('engine') == _engine() and value['manifest'] == _manifest(vault)
                   and dependencies == _dependencies(dependencies))
    except (OSError, ValueError):
        current = False
    if not current:
        return {'status': 'stale'}
    report = value['report']
    return {'status': 'fail' if report['fail'] else 'pass', 'report': report}
