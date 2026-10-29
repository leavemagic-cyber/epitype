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
    from . import capture, memspec, transcript
except ImportError:
    import capture
    import memspec
    import transcript

try:
    import zstandard
except ImportError:
    zstandard = None

READ_ERRORS = (OSError, EOFError, ValueError, zipfile.BadZipFile, RuntimeError,
               NotImplementedError, zlib.error) + ((zstandard.ZstdError,) if zstandard else ())

VERSION = 2
CONTEXT_CHARS = 500
WINDOW_BYTES = 4096
REPLAY_HEADERS = (
    'The following is the Codex agent history whose request action you are assessing.',
    'The following is the Codex agent history added since your last approval assessment.',
)
CONTEXT_TAGS = (
    'in-app-browser-context', 'environment_context', 'system-reminder',
    'pasted_content', 'codex_internal_context', 'image',
    'heartbeat', 'hook_prompt', 'local-command-stdout', 'scheduled-task',
    'subagent_notification', 'side-sessions-event', 'codex_delegation',
    'epitype-evaluation', 'external_codex_apps_open_page',
    'external_codex_apps_writing_block_edits', 'recommended_plugins',
    'turn_aborted', 'create-pr-command', 'command-message',
)
CONTEXT_BLOCK = re.compile(
    # A self-closing image must not consume new input up to a later closing tag.
    r'<image\b[^>]*?/\s*>|'
    # Escaped nested examples must not close the actual outer host block.
    r'<(' + '|'.join(CONTEXT_TAGS) + r')\b[^>]*>(?:.*?</\1(?:\s[^>]*)?>|.*?&lt;/\1(?:\s.*?)?&gt;)'
    r'|</?image\b[^>]*>', re.DOTALL | re.IGNORECASE)


def default_path():
    return memspec.config_path().parent / "owner_index.sqlite3"


def normalize(text):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', text)).casefold()


def _question_reply(text):
    match = re.fullmatch(r'\s*<send_user_message_question_reply>\s*(.*?)\s*'
                         r'</send_user_message_question_reply>\s*', text, re.S)
    if match is None:
        return None
    try:
        replies = json.loads(match.group(1))
    except ValueError:
        return ('', '')
    if not isinstance(replies, list) or not replies or not all(
            isinstance(reply, dict) and isinstance(reply.get('questionItemId'), str)
            and isinstance(reply.get('question'), str) and isinstance(reply.get('answer'), str)
            for reply in replies):
        return ('', '')
    return ('\n'.join(reply['answer'] for reply in replies),
            '\n'.join(reply['question'] for reply in replies))


def owner_text(text, *, queued=False):
    text = text.strip()
    closing_tags = '|'.join(CONTEXT_TAGS)
    if (text.startswith('</')
            and re.fullmatch(r'(?:</(?:' + closing_tags + r'|input)\b[^>]*>\s*)+', text, re.I)
            and re.search(r'</(?:' + closing_tags + r')\b', text, re.I)):
        return ''
    reply = _question_reply(text)
    if reply is not None:
        return reply[0].strip()
    if text.startswith('<send_user_message_question_reply'):
        return ''
    if text.startswith('<realtime_delegation'):
        request = re.search(r'<input>(.*?)</input>', text, re.S)
        return owner_text(request.group(1)) if request else ''
    if text.startswith(('Caveat: The messages below', '# AGENTS.md',
                        'This session is being continued', *REPLAY_HEADERS,
                        '>>> SENDER USER MESSAGES START',
                        '>>> RETAINED USER INSTRUCTIONS START')):
        return ''
    if text.startswith('# Context from my IDE setup:'):
        request = re.search(r'(?m)^## My request(?: for Codex)?:[ \t]*', text)
        if request is None:
            return ''
        text = text[request.end():].strip()
    text = CONTEXT_BLOCK.sub('', text).strip()
    if re.match(r'<(?:' + '|'.join(CONTEXT_TAGS) + r')\b', text, re.I):
        return ''  # An unfinished host block cannot prove where new owner text starts.
    # A file list is host context only when paired with its request delimiter.
    text = re.sub(r'(?ms)^# Files mentioned by the user:\s*.*?(?:^## My request:[ \t]*\n?|\Z)', '', text)
    text = re.sub(r'(?m)^## My request(?: for Codex)?:[ \t]*\n?', '', text).strip()
    if text.casefold().startswith(memspec.MACHINE_TURN_PREFIXES):
        return ''
    if re.match(r'<(?:' + '|'.join(memspec.MACHINE_TURN_HARD_TAGS) + r')\b', text, re.I):
        return ''
    if text.startswith(('<command-name>', '<command-args>')):
        text = memspec.owner_typed_text(text)
    while text.startswith('《') and '》' in text:
        text = text[text.index('》') + 1:].strip()
    return capture.owner_reply(text).strip()


def _owner_record(item, asked):
    if item.get('type') == 'response_item':
        payload = item.get('payload')
        if isinstance(payload, dict) and payload.get('role') == 'user':
            content = payload.get('content')
            if isinstance(content, list):
                if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
                    return transcript.source_record(item, asked)
                texts = [b['text'] for b in content if isinstance(b, dict)
                         and b.get('type') in ('text', 'input_text') and isinstance(b.get('text'), str)]
                # Guardian requests replay past turns as many separate input_text blocks.
                if (any(isinstance(t, str) and t.lstrip().startswith(REPLAY_HEADERS) for t in texts)
                        or any(isinstance(t, str) and t.strip() == '>>> APPROVAL REQUEST END' for t in texts)):
                    return None
                content = [{'type': 'input_text', 'text': '\n'.join(texts)}]
                item = dict(item, payload=dict(payload, content=content))
    return transcript.source_record(item, asked)


def _script(origin):
    return isinstance(origin, str) and (origin == 'codex_exec' or origin.startswith('sdk-'))


def _guardian(payload):
    source = payload.get('source')
    subagent = source.get('subagent') if isinstance(source, dict) else None
    return (payload.get('thread_source') == 'guardian_review'
            or subagent == 'guardian'
            or isinstance(subagent, dict) and subagent.get('other') == 'guardian')


def _machine_session(payload):
    return (_guardian(payload) or _script(payload.get('originator'))
            or _script(payload.get('source')) or payload.get('source') == 'exec')


def _connect(path, readonly=False):
    path = Path(path).resolve()
    if readonly:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _schema(db, rebuild=False):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version == 1 and rebuild:
        db.executescript('''DROP TRIGGER IF EXISTS messages_insert;
            DROP TRIGGER IF EXISTS messages_delete;
            DROP TABLE IF EXISTS owner_fts;
            DROP TABLE IF EXISTS messages;
            DROP TABLE IF EXISTS sources;''')
        version = 0
    if version not in (0, VERSION):
        raise ValueError('unsupported owner index version; build a new index')
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
            offset INTEGER NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE, search_text TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS locators (
            id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id),
            source TEXT NOT NULL, byte_offset INTEGER NOT NULL,
            role TEXT NOT NULL, timestamp TEXT NOT NULL, session_id TEXT NOT NULL,
            message_uuid TEXT NOT NULL, question TEXT NOT NULL,
            previous TEXT NOT NULL, record_sha256 TEXT NOT NULL,
            search_question TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS locators_source ON locators(source);
        CREATE INDEX IF NOT EXISTS locators_time ON locators(timestamp);
        CREATE INDEX IF NOT EXISTS locators_message ON locators(message_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS owner_fts USING fts5(
            search_text, content='messages', content_rowid='id',
            tokenize='trigram');
        CREATE TRIGGER IF NOT EXISTS messages_insert AFTER INSERT ON messages BEGIN
            INSERT INTO owner_fts(rowid, search_text)
            VALUES (new.id, new.search_text);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_delete AFTER DELETE ON messages BEGIN
            INSERT INTO owner_fts(owner_fts, rowid, search_text)
            VALUES ('delete', old.id, old.search_text);
        END;
    """)
    db.execute(f'PRAGMA user_version={VERSION}')


def default_roots():
    home = Path.home()
    configured = memspec.config_options().get('owner_index_roots')
    if isinstance(configured, list) and all(isinstance(p, str) for p in configured):
        return [Path(p).expanduser() for p in configured]
    projects = home / '.claude' / 'projects'
    extra = memspec.config_options().get('owner_index_extra_roots')
    extra = [Path(p).expanduser() for p in extra] if isinstance(extra, list) else []
    return [*sorted(projects.glob('*/*.jsonl')), *sorted(projects.glob('*/*.jsonl.backup*')),
            home / '.codex' / 'sessions', home / '.codex' / 'archived_sessions', *extra]


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


def _fingerprint(stream, offset, report):
    length = min(offset, WINDOW_BYTES)
    stream.seek(offset - length)
    raw = stream.read(length)
    report['bytes_verified'] += len(raw)
    if len(raw) != length:
        raise OSError('source shrank while its boundary was checked')
    return hashlib.sha256(raw).hexdigest()


def _question(item, asked):
    payload = item.get('payload') if item.get('type') == 'response_item' else item.get('message')
    reply = _question_reply(transcript.message_text(payload))
    if reply is not None:
        return reply[1]
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


def _consume(db, stream, source, state, offset, size, immutable, report):
    asked = state.setdefault('asked', {})
    position = offset
    while size is None or position < size:
        raw = stream.readline() if size is None else stream.readline(size - position)
        if not raw:
            break
        report['bytes_parsed'] += len(raw)
        if not raw.endswith(b'\n') and not immutable:
            break  # live writers may not have finished this physical row yet
        start = position
        position += len(raw)
        try:
            item = json.loads(raw.decode('utf-8-sig'))
        except (UnicodeError, ValueError, RecursionError):
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
                state['machine'] = _machine_session(payload)
            continue
        if state.get('machine'):
            continue
        asked.update(transcript.ask_tool_uses(item))
        record = _owner_record(item, asked)
        if record:
            role, text = record
            if role == 'A':
                state['previous'] = text[-CONTEXT_CHARS:]
            elif not _script(state.get('origin')) and not any(
                    _script(item.get(field)) for field in ('entrypoint', 'origin', 'originator')):
                # S is the selected AI-written option, not a verbatim owner utterance.
                text = text.strip() if role == 'S' else owner_text(text, queued=role == 'Q')
                if text:
                    question = _question(item, asked)
                    payload = item.get('payload')
                    message_id = item.get('uuid') or item.get('id') or (
                        payload.get('id') if isinstance(payload, dict) else '')
                    inserted = db.execute('INSERT OR IGNORE INTO messages (text,search_text) VALUES (?,?)',
                                          (text, normalize(text))).rowcount
                    message_id_row = db.execute('SELECT id FROM messages WHERE text=?', (text,)).fetchone()[0]
                    db.execute('''INSERT INTO locators
                        (message_id, source, byte_offset, role, timestamp, session_id, message_uuid,
                         question, previous, record_sha256, search_question)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (message_id_row, source, start, role, str(item.get('timestamp') or ''),
                         str(item.get('sessionId') or state.get('session') or ''),
                         str(message_id or ''), question,
                         state.get('previous', ''), hashlib.sha256(raw).hexdigest(),
                         normalize(question)))
                    report['messages_added'] += inserted
                    report['locators_added'] += 1
        # Answered prompts no longer need to occupy the resumable parser state.
        for tool_id in transcript.tool_result_ids(item):
            asked.pop(tool_id, None)
    return position


def _index_stream(db, stream, source, size, mtime, immutable, report, source_size=None):
    old = db.execute('SELECT * FROM sources WHERE path=?', (source,)).fetchone()
    if old and (immutable or (old['size'], old['mtime']) == (size, mtime)):
        report['unchanged'] += 1
        return
    offset, state = 0, {}
    if old and not immutable and size > old['size']:
        # Transcripts are append-only; only the last small boundary is checked.
        if _fingerprint(stream, old['offset'], report) == old['digest']:
            offset, state = old['offset'], json.loads(old['state'])
    if not offset:
        if old and not immutable:
            stream.seek(0)
        db.execute('DELETE FROM locators WHERE source=?', (source,))
        report['reread' if old else 'new'] += 1
    else:
        report['appended'] += 1
        stream.seek(offset)
    position = _consume(db, stream, source, state, offset, size, immutable, report)
    digest = '' if immutable else _fingerprint(stream, position, report)
    stored_size = source_size if source_size is not None else (position if size is None else size)
    db.execute('INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?)',
               (source, stored_size, mtime, position,
                digest, json.dumps(state, ensure_ascii=False)))


def update(path=None, roots=None, *, rebuild=False):
    """Commit each readable source separately; a failed source rolls back its rows."""
    started = time.monotonic()
    path = Path(path) if path is not None else default_path()
    report = dict(new=0, reread=0, appended=0, unchanged=0, messages_added=0, locators_added=0,
                  bytes_parsed=0, bytes_verified=0, skipped_zst=0,
                  errors=[], unreadable_rows=[])
    with closing(_connect(path)) as db:
        _schema(db, rebuild)
        if rebuild:
            db.execute('DELETE FROM locators')
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
                        old = db.execute('SELECT size,mtime FROM sources WHERE path=?', (source,)).fetchone()
                        immutable = bundle is not None or file.suffix == '.zst'
                        if old and (immutable or (old['size'], old['mtime']) == (stat.st_size, stat.st_mtime_ns)):
                            report['unchanged'] += 1
                            continue
                        added_before = report['messages_added']
                        locators_before = report['locators_added']
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
                            report['locators_added'] = locators_before
                            report['errors'].append({'source': source, 'error': f'{type(exc).__name__}: {exc}'})
            except READ_ERRORS as exc:
                report['errors'].append({'source': str(file), 'error': f'{type(exc).__name__}: {exc}'})
        db.execute('DELETE FROM messages WHERE NOT EXISTS (SELECT 1 FROM locators WHERE message_id=messages.id)')
        db.commit()
        report['messages'], report['text_characters'] = db.execute(
            'SELECT count(*),coalesce(sum(length(text)),0) FROM messages').fetchone()
        report['locators'] = db.execute('SELECT count(*) FROM locators').fetchone()[0]
        report['sources'] = db.execute('SELECT count(*) FROM sources').fetchone()[0]
    report.update(index=str(path.resolve()), seconds=round(time.monotonic() - started, 3),
                  bytes_read=report['bytes_parsed'] + report['bytes_verified'],
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
    clauses, params = ['l.timestamp >= ?', 'l.role IN (' + ','.join('?' for _ in roles) + ')'], [after, *roles]
    for needle in needles:
        if len(needle) >= 3:
            clauses.append('(m.id IN (SELECT rowid FROM owner_fts WHERE owner_fts MATCH ?) '
                           'OR instr(l.search_question, ?) > 0)')
            params.extend(('"' + needle + '"', needle))
            continue
        clauses.append('(instr(m.search_text, ?) > 0 OR instr(l.search_question, ?) > 0)')
        params.extend((needle, needle))
    params.append(limit)
    with closing(_connect(path if path is not None else default_path(), readonly=True)) as db:
        rows = db.execute('''WITH matches AS (
            SELECT l.*, m.text, row_number() OVER (
                PARTITION BY m.id ORDER BY l.timestamp DESC,l.id DESC) AS rank
            FROM locators l JOIN messages m ON m.id=l.message_id WHERE '''
            + ' AND '.join(clauses) + ''')
            SELECT source,byte_offset,role,timestamp,session_id,message_uuid,text,
                question,previous,record_sha256,
                (SELECT count(*) FROM locators WHERE message_id=matches.message_id) AS locator_count
            FROM matches WHERE rank=1 ORDER BY timestamp DESC,id DESC LIMIT ?''', params)
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
