"""Per-card synthetic original records for source-bound publication regressions."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from epitype import memspec, rule_pipeline, rule_provenance


def source_bound_card(text, card_path, *, source_root, case_id, host='codex'):
    """Keep fixture behavior intact; bind its own original record and scoped AI author."""
    card_path = Path(card_path)
    source_root = Path(source_root)
    source_root.mkdir(parents=True, exist_ok=True)
    fields, problem = memspec.frontmatter_text(text)
    if problem:
        raise ValueError('source fixture requires readable frontmatter: ' + problem)
    lines, closing = memspec.split_frontmatter(text)
    if closing is None or not text.lstrip('\ufeff').startswith('---\n'):
        raise ValueError('source fixture requires a complete LF frontmatter boundary')
    # This helper is for deliberate positive fixtures. Negative quote/source tests
    # construct their own records and must not call it to repair the negative.
    digest = hashlib.sha256((str(card_path) + '\n' + case_id + '\n' + text).encode()).hexdigest()
    sid = 'synthetic-rule-source-' + digest[:24]
    mid = 'synthetic-owner-' + digest[:24]
    quote = fields.get('owner_quote') or (
        '此合成測試只適用於 ' + case_id + ' 案：採用下列測試規則，'
        '由 synthetic-test-ai 整理，保留這個案例的執行條件。')
    source = source_root / (digest + '.jsonl')
    stamp = (json.loads(source.read_bytes().splitlines()[1])['timestamp'] if source.exists()
             else datetime.now(timezone.utc).isoformat())
    expiry = (datetime.fromisoformat(stamp)+timedelta(hours=1)).isoformat()
    interpretation = fields.get('text') or fields.get('description') or '合成測試執行整理。'
    message = quote + '\n待發布的合成測試規則資料：\n' + text
    message += '\n合成測試已核的候選：'+interpretation+' '+stamp+' '+expiry
    if host == 'claude':
        row = {'type': 'user', 'sessionId': sid, 'uuid': mid,
               'message': {'role': 'user', 'content': [{'type': 'text', 'text': message}]}}
    elif host == 'codex':
        row = {'type': 'response_item', 'payload': {'type': 'message', 'id': mid,
               'role': 'user', 'content': [{'type': 'input_text', 'text': message}]}}
    else:
        raise ValueError('unsupported synthetic source host')
    row['timestamp'] = stamp
    meta = (json.dumps({'type': 'session_meta', 'payload': {'id': sid}}, ensure_ascii=False) + '\n').encode()
    raw = (json.dumps(row, ensure_ascii=False) + '\n').encode()
    expected = meta + raw
    if source.exists():
        if source.read_bytes() != expected:
            raise ValueError('synthetic source identity collision')
    else:
        source.write_bytes(expected)
    # Identical retries reuse identical evidence bytes and review timestamps.
    document = {
        'version': 1, 'key': fields.get('decision_key') or ('synthetic-' + card_path.stem),
        'kind': 'ai-derived', 'author': 'synthetic-test-ai', 'quote': quote,
        'text': interpretation,
        'sources': [{'path': str(source), 'byte_offset': len(meta), 'line': 2,
            'record_sha256': hashlib.sha256(raw).hexdigest(), 'session_id': sid,
            'message_id': mid, 'role': 'U'}],
        'scope': {'level': 'case', 'project': 'epitype-pretooluse-selftest', 'case': case_id,
            'actions': ['驗證本案例的卡片發布與執行條件'], 'conditions': ['隔離合成測試'],
            'expiry': 'unspecified', 'exceptions': []},
        'scope_basis': {'source': 0, 'quote': quote, 'reviewed_by': 'synthetic-test-reviewer',
            'reviewed_at': stamp, 'rationale': 'Synthetic source declares this single isolated test case.'},
        'supersedes': [],
    }
    if rule_provenance._RESTRICTION.search(interpretation):
        document['restriction_evidence'] = {
            'kind': 'ai-derived', 'author': document['author'], 'challengeable': True,
            'text': interpretation, 'subject': interpretation,
            'basis': [{'quote': interpretation, 'observed_at': stamp, 'valid_until': expiry,
                       'source': document['sources'][0]}],
            'rationale': 'Isolated positive fixture has source-bound reviewed conditions.'}
    document = rule_pipeline.bind_payload(document, text, reviewer='synthetic-test-reviewer', reviewed_at=stamp)
    encoded = json.dumps(rule_provenance.serialize(document), ensure_ascii=False)
    return text.replace('---\n', '---\nrule_provenance: ' + encoded + '\n', 1)


def owner_bound_card(text, card_path, *, source_root, case_id):
    """Bind a guard fixture to a verified owner directive with global scope.

    Only a VERIFIED source may deny (CORE-08/CORE-25), so a fixture that pins a
    guard's deny mechanics needs one; the source is a synthetic human record."""
    card_path = Path(card_path)
    source_root = Path(source_root)
    source_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256((str(card_path) + '\n' + case_id + '\n' + text).encode()).hexdigest()
    sid = 'synthetic-owner-guard-' + digest[:24]
    mid = 'synthetic-owner-message-' + digest[:24]
    quote = '隔離測試 host 全域所有動作：採用 ' + case_id + ' 測試守衛規則。'
    source = source_root / (digest + '.jsonl')
    row = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'id': mid,
           'content': [{'type': 'input_text', 'text': quote}]}}
    meta = (json.dumps({'type': 'session_meta', 'payload': {'id': sid}}) + '\n').encode()
    raw = (json.dumps(row, ensure_ascii=False) + '\n').encode()
    if source.exists() and source.read_bytes() != meta + raw:
        raise ValueError('synthetic source identity collision')
    source.write_bytes(meta + raw)
    document = {'version': 1, 'key': 'synthetic-' + card_path.stem, 'kind': 'owner-directive',
        'author': 'owner', 'quote': quote, 'text': quote,
        'sources': [{'path': str(source), 'byte_offset': len(meta), 'line': 2,
            'record_sha256': hashlib.sha256(raw).hexdigest(), 'session_id': sid,
            'message_id': mid, 'role': 'U'}],
        'scope': {'level': 'global', 'project': None, 'case': None, 'actions': ['*'],
            'conditions': [], 'expiry': 'unspecified', 'exceptions': []},
        'scope_basis': {'source': 0, 'quote': quote, 'reviewed_by': 'synthetic-test-reviewer',
            'reviewed_at': '2026-10-10', 'rationale': 'The synthetic human source declares global scope.'},
        'supersedes': []}
    document = rule_pipeline.bind_payload(document, text, reviewer='synthetic-test-reviewer')
    encoded = json.dumps(json.dumps(document, ensure_ascii=False), ensure_ascii=False)
    return text.replace('---\n', '---\nrule_provenance: ' + encoded + '\n', 1)
