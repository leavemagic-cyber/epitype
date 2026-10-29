"""Incremental, local owner-message search. Indexed hits are locators, not proof."""
import argparse
from contextlib import ExitStack, closing
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata
import zipfile
import zlib

try:
    from . import memspec, transcript
except ImportError:
    import memspec
    import transcript

try:
    import zstandard
except ImportError:
    zstandard = None

READ_ERRORS = (OSError, EOFError, ValueError, zipfile.BadZipFile, RuntimeError,
               NotImplementedError, zlib.error) + ((zstandard.ZstdError,) if zstandard else ())

VERSION = 1
CONTEXT_CHARS = 500
CHUNK_BYTES = 1024 * 1024
_TYPE = re.compile(rb'"(?:type|role)"\s*:\s*"([^"]+)"')
_TEXT = re.compile(rb'"type"\s*:\s*"(?:text|output_text)"')
_MESSAGE = re.compile(rb'"type"\s*:\s*"message"')
_ARGS = re.compile(r'<command-args>(.*?)</command-args>', re.S)
_WRAPPER = re.compile(r'^<([\w:-]+)(?:\s[^>]*)?>.*?</\1>\s*', re.S)


def default_path():
    return memspec.config_path().parent / "owner_index.sqlite3"


def normalize(text):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', text)).casefold()


def owner_text(text):
    text = text.strip()
    if text.startswith(('Caveat: The messages below', '# AGENTS.md',
                        'This session is being continued')):
        return ''
    while text.startswith(('《', '<')):
        if text.startswith('《'):
            end = text.find('》')
            if end < 0:
                return ''
            text = text[end + 1:].strip()
        else:
            args = _ARGS.search(text)
            if args:
                return args.group(1).strip()
            wrapper = _WRAPPER.match(text)
            if not wrapper:
                return ''
            text = text[wrapper.end():].strip()
    return text


def _script(origin):
    return isinstance(origin, str) and (origin == 'codex_exec' or origin.startswith('sdk-'))


def _connect(path, readonly=False):
    path = Path(path).resolve()
    if readonly:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _schema(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in (0, VERSION):
        raise ValueError('unsupported owner index version; build a new index')
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
            offset INTEGER NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, byte_offset INTEGER NOT NULL,
            role TEXT NOT NULL, timestamp TEXT NOT NULL, session_id TEXT NOT NULL,
            message_uuid TEXT NOT NULL, text TEXT NOT NULL, question TEXT NOT NULL,
            previous TEXT NOT NULL, record_sha256 TEXT NOT NULL,
            search_text TEXT NOT NULL, search_question TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS messages_source ON messages(source);
        CREATE INDEX IF NOT EXISTS messages_time ON messages(timestamp);
        CREATE VIRTUAL TABLE IF NOT EXISTS owner_fts USING fts5(
            search_text, search_question, content='messages', content_rowid='id',
            tokenize='trigram');
        CREATE TRIGGER IF NOT EXISTS messages_insert AFTER INSERT ON messages BEGIN
            INSERT INTO owner_fts(rowid, search_text, search_question)
            VALUES (new.id, new.search_text, new.search_question);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_delete AFTER DELETE ON messages BEGIN
            INSERT INTO owner_fts(owner_fts, rowid, search_text, search_question)
            VALUES ('delete', old.id, old.search_text, old.search_question);
        END;
    """)
    db.execute(f'PRAGMA user_version={VERSION}')


def default_roots():
    home = Path.home()
    configured = memspec.config_options().get('owner_index_roots')
    if isinstance(configured, list) and all(isinstance(p, str) for p in configured):
        return [Path(p).expanduser() for p in configured]
    projects = home / '.claude' / 'projects'
    return [*sorted(projects.glob('*/*.jsonl')), *sorted(projects.glob('*/*.jsonl.backup*')),
            home / '.codex' / 'sessions', home / '.codex' / 'archived_sessions']


def _supported(path):
    return (path.name.endswith(('.jsonl', '.jsonl.zst', '.zip'))
            or '.jsonl.backup' in path.name)


def _files(roots, errors):
    seen = set()
    for root in roots:
        root = Path(root).expanduser().resolve()
        try:
            paths = root.rglob('*') if root.is_dir() else [root]
            for path in paths:
                if not _supported(path) or path in seen or path.is_dir():
                    continue
                seen.add(path)
                yield path
        except OSError as exc:
            errors.append({'source': str(root), 'error': str(exc)})


def _fingerprint(stream, length):
    digest = hashlib.sha256()
    remaining = length
    stream.seek(0)
    while remaining:
        raw = stream.read(min(CHUNK_BYTES, remaining))
        if not raw:
            raise OSError('source shrank while its prefix was checked')
        digest.update(raw)
        remaining -= len(raw)
    return digest


def _question(item, asked):
    answers = item.get('toolUseResult')
    answers = answers.get('answers') if isinstance(answers, dict) else None
    if not isinstance(answers, dict):
        return ''
    questions = []
    for tool_id in transcript.tool_result_ids(item):
        prompt = asked.get(tool_id, {}).get('input')
        for question in prompt.get('questions', []) if isinstance(prompt, dict) else []:
            if isinstance(question, dict) and question.get('question') in answers:
                questions.append(question['question'])
    return '\n'.join(questions)


def _wanted(raw):
    kinds = set(_TYPE.findall(raw))
    if not kinds:
        return True  # malformed rows must be reported
    if b'session_meta' in kinds or b'attachment' in kinds or b'AskUserQuestion' in raw:
        return True
    if b'response_item' in kinds:
        return bool(_MESSAGE.search(raw))
    if b'user' in kinds and b'"tool_result"' in raw and b'"answers"' not in raw:
        return False
    return b'user' in kinds or b'assistant' in kinds and (
        bool(_TEXT.search(raw)) or bool(re.search(rb'"content"\s*:\s*"', raw)))


def _consume(db, stream, source, state, offset, size, digest, immutable, report):
    asked = state.setdefault('asked', {})
    position = offset
    while size is None or position < size:
        raw = stream.readline() if size is None else stream.readline(size - position)
        if not raw:
            break
        if not raw.endswith(b'\n') and not immutable:
            break  # live writers may not have finished this physical row yet
        start = position
        position += len(raw)
        digest.update(raw)
        report['bytes_parsed'] += len(raw)
        if not _wanted(raw):
            continue
        try:
            item = json.loads(raw.decode('utf-8-sig'))
        except (UnicodeError, ValueError):
            report['unreadable_rows'].append({'source': source, 'byte_offset': start})
            continue
        if not isinstance(item, dict):
            report['unreadable_rows'].append({'source': source, 'byte_offset': start})
            continue
        if item.get('type') == 'session_meta':
            payload = item.get('payload')
            if isinstance(payload, dict):
                state['origin'] = payload.get('originator', '')
                state['session'] = payload.get('id', '')
            continue
        asked.update(transcript.ask_tool_uses(item))
        record = transcript.source_record(item, asked)
        if record:
            role, text = record
            if role == 'A':
                state['previous'] = text[-CONTEXT_CHARS:]
            elif not _script(state.get('origin')) and not any(
                    _script(item.get(field)) for field in ('entrypoint', 'origin', 'originator')):
                # S is the selected AI-written option, not a verbatim owner utterance.
                text = text.strip() if role in ('S', 'Q') else owner_text(text)
                if text:
                    question = _question(item, asked)
                    db.execute('''INSERT INTO messages
                        (source, byte_offset, role, timestamp, session_id, message_uuid,
                         text, question, previous, record_sha256, search_text, search_question)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (source, start, role, str(item.get('timestamp') or ''),
                         str(item.get('sessionId') or state.get('session') or ''),
                         str(item.get('uuid') or item.get('id') or ''), text, question,
                         state.get('previous', ''), hashlib.sha256(raw).hexdigest(),
                         normalize(text), normalize(question)))
                    report['messages_added'] += 1
        # Answered prompts no longer need to occupy the resumable parser state.
        for tool_id in transcript.tool_result_ids(item):
            asked.pop(tool_id, None)
    return position


def _index_stream(db, stream, source, size, mtime, immutable, report, source_size=None):
    old = db.execute('SELECT * FROM sources WHERE path=?', (source,)).fetchone()
    if old and (immutable or (old['size'], old['mtime']) == (size, mtime)):
        report['unchanged'] += 1
        return
    offset, state, digest = 0, {}, hashlib.sha256()
    if old and not immutable and size >= old['size'] and size > old['offset']:
        # Metadata cannot distinguish an append from an append plus an earlier edit.
        digest = _fingerprint(stream, old['offset'])
        report['bytes_verified'] += old['offset']
        if digest.hexdigest() == old['digest']:
            offset, state = old['offset'], json.loads(old['state'])
        else:
            digest = hashlib.sha256()
    if not offset:
        if old and not immutable:
            stream.seek(0)
        db.execute('DELETE FROM messages WHERE source=?', (source,))
        report['reread' if old else 'new'] += 1
    else:
        report['appended'] += 1
    position = _consume(db, stream, source, state, offset, size, digest, immutable, report)
    stored_size = source_size if source_size is not None else (position if size is None else size)
    db.execute('INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?)',
               (source, stored_size, mtime, position,
                digest.hexdigest(), json.dumps(state, ensure_ascii=False)))


def update(path=None, roots=None, *, rebuild=False):
    """Commit each readable source separately; a failed source rolls back its rows."""
    started = time.monotonic()
    path = Path(path) if path is not None else default_path()
    report = dict(new=0, reread=0, appended=0, unchanged=0, messages_added=0,
                  bytes_parsed=0, bytes_verified=0, skipped_zst=0,
                  errors=[], unreadable_rows=[])
    with closing(_connect(path)) as db:
        _schema(db)
        if rebuild:
            db.execute('DELETE FROM messages')
            db.execute('DELETE FROM sources')
        db.commit()
        for file in _files(default_roots() if roots is None else roots, report['errors']):
            try:
                stat = file.stat()
                with ExitStack() as stack:
                    if file.suffix == '.zip':
                        bundle = stack.enter_context(zipfile.ZipFile(file))
                        members = [(f'{file}!{info.filename}', info)
                                   for info in bundle.infolist()
                                   if info.filename.endswith(('.jsonl', '.jsonl.zst'))]
                    else:
                        bundle, members = None, [(str(file), None)]
                    for source, member in members:
                        if source.endswith('.zst') and zstandard is None:
                            report['skipped_zst'] += 1
                            continue
                        old = db.execute('SELECT 1 FROM sources WHERE path=?', (source,)).fetchone()
                        immutable = bundle is not None or file.suffix == '.zst'
                        if immutable and old:
                            report['unchanged'] += 1
                            continue
                        added_before = report['messages_added']
                        try:
                            with ExitStack() as streams:
                                raw = streams.enter_context(bundle.open(member) if bundle else file.open('rb'))
                                size = member.file_size if member else stat.st_size
                                source_size = size
                                if source.endswith('.zst'):
                                    raw = streams.enter_context(zstandard.ZstdDecompressor().stream_reader(raw))
                                    raw = streams.enter_context(io.BufferedReader(raw))
                                    size = None
                                with db:
                                    _index_stream(db, raw, source, size, stat.st_mtime_ns,
                                                  immutable, report, source_size)
                        except READ_ERRORS as exc:
                            report['messages_added'] = added_before
                            report['errors'].append({'source': source, 'error': f'{type(exc).__name__}: {exc}'})
            except READ_ERRORS as exc:
                report['errors'].append({'source': str(file), 'error': f'{type(exc).__name__}: {exc}'})
        report['messages'] = db.execute('SELECT count(*) FROM messages').fetchone()[0]
        report['sources'] = db.execute('SELECT count(*) FROM sources').fetchone()[0]
    report.update(index=str(path.resolve()), seconds=round(time.monotonic() - started, 3),
                  size_bytes=path.stat().st_size)
    return report


def find(words, *, path=None, after='', roles=('U', 'Q', 'S'), limit=15):
    if after:
        after = date.fromisoformat(after).isoformat()
    if limit < 1 or not roles or any(role not in ('U', 'Q', 'S') for role in roles):
        raise ValueError('use a positive limit and roles U,Q,S')
    needles = [normalize(word) for word in words if normalize(word)]
    if not needles:
        raise ValueError('supply searchable words')
    clauses, params = ['timestamp >= ?', 'role IN (' + ','.join('?' for _ in roles) + ')'], [after, *roles]
    long = [needle for needle in needles if len(needle) >= 3]
    if long:
        clauses.append('id IN (SELECT rowid FROM owner_fts WHERE owner_fts MATCH ?)')
        params.append(' AND '.join('"' + needle + '"' for needle in long))
    for needle in needles:
        clauses.append('(instr(search_text, ?) > 0 OR instr(search_question, ?) > 0)')
        params.extend((needle, needle))
    params.append(limit)
    with closing(_connect(path if path is not None else default_path(), readonly=True)) as db:
        rows = db.execute('''SELECT source, byte_offset, role, timestamp, session_id,
            message_uuid, text, question, previous, record_sha256 FROM messages WHERE '''
            + ' AND '.join(clauses) + ' ORDER BY timestamp DESC, id DESC LIMIT ?', params)
        records = [dict(row, verified=False) for row in rows]
    return {'records': records, 'scope': memspec.OWNER_INDEX_SCOPE}


def main(argv=None):
    parser = argparse.ArgumentParser(description=memspec.OWNER_INDEX_HELP)
    parser.add_argument('action', choices=('build', 'update', 'find'))
    parser.add_argument('words', nargs='*')
    parser.add_argument('--index', type=Path, default=None)
    parser.add_argument('--root', type=Path, action='append', help=memspec.OWNER_INDEX_ROOT_HELP)
    parser.add_argument('--after', default='')
    parser.add_argument('--role', default='U,Q,S')
    parser.add_argument('--limit', type=int, default=15)
    args = parser.parse_args(argv)
    try:
        result = (find(args.words, path=args.index, after=args.after,
                       roles=tuple(args.role.split(',')), limit=args.limit)
                  if args.action == 'find' else
                  update(args.index, args.root, rebuild=args.action == 'build'))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f'ERROR {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
