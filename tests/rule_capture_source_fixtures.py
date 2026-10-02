"""Scoped synthetic originals and a calendar-only clock for capture selftests."""
import hashlib
import json
from pathlib import Path

from epitype import capture, memspec, rule_pipeline, rule_provenance
from rule_source_fixtures import source_bound_card

STAMP = '2026-10-03T00:00:00Z'


def reviewed_capture_event(found, vault, event, *, source_root, replay=None, host='claude',
                           digest_override=None, owner_quote=None):
    """Review the actual formatter's complete payload in its own case/project."""
    kind, body, summary, digest_source = found
    digest = digest_override or capture.grant_digest(digest_source)
    event_id, origin, _session = capture.event_identity(event, digest)
    stamp = replay.stamp if replay is not None else STAMP
    name = f"{kind}-{stamp[:10].replace('-', '')}-{digest}-{event_id}"
    label = 'owner ruling auto-captured' if kind == 'ruling' else capture.CAPTURE_KINDS[kind][2]
    text = capture.capture_card_text(name, label, body, summary, stamp, event, event_id, origin, replay)
    directory = memspec.RULING_DIRECTORY if kind == 'ruling' else capture.CAPTURE_KINDS[kind][0]
    path = Path(vault) / directory / (name + '.md')
    bound = source_bound_card(text, path, source_root=source_root, case_id=name, host=host)
    fields, _problem = memspec.frontmatter_text(bound)
    document = json.loads(fields[rule_provenance.FIELD])
    source = Path(document['sources'][0]['path'])
    meta, raw = source.read_bytes().splitlines(keepends=True)
    row = json.loads(raw)
    project = str(event.get('cwd') or Path(vault).resolve())
    scope_quote = f'此隔離合成測試只適用於專案 {project} 中 {name} 案的卡片捕捉與喚回；不得擴為全域。'
    # The real human half follows the preserved assistant/question/context body.
    # No derived description or assistant question becomes the Owner quotation.
    quote = owner_quote or capture.owner_reply(capture.owner_side(body, summary))
    message = row['message'] if host == 'claude' else row['payload']
    message['content'][0]['text'] = ('待發布的完整合成卡片資料：\n' + text
        + '\nOwner 原句：' + quote + '\n' + scope_quote)
    raw = (json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8')
    scoped_source = source.with_name('scoped-capture-' + source.name)
    expected = meta + raw
    if scoped_source.exists():
        if scoped_source.read_bytes() != expected:
            raise ValueError('synthetic capture source identity collision')
    else:
        scoped_source.write_bytes(expected)
    document['sources'][0]['path'] = str(scoped_source)
    document['sources'][0]['record_sha256'] = hashlib.sha256(raw).hexdigest()
    document['quote'] = quote
    document['author'] = 'epitype-capture (AI)'
    document['scope'] = {'level': 'case', 'project': project, 'case': name,
        'actions': ['此案例的卡片捕捉與喚回'], 'conditions': ['隔離合成測試'],
        'expiry': 'unspecified', 'exceptions': []}
    document['scope_basis']['quote'] = scope_quote
    document['scope_basis']['rationale'] = 'Original declares this single isolated case and project.'
    document = rule_pipeline.bind_payload(document, text,
        reviewer='synthetic-capture-reviewer', reviewed_at=document['scope_basis']['reviewed_at'])
    result = dict(event)
    result[rule_provenance.FIELD] = document
    return result


def calendar_wrapper(script, destination):
    """Run the actual hook with only capture's UTC calendar fixed, never its gate."""
    script = Path(script).resolve()
    destination = Path(destination)
    destination.write_text(
        'import runpy, sys, time\n'
        f'sys.path[:0] = [{str(script.parents[2])!r}, {str(script.parent)!r}]\n'
        'from epitype import capture\n'
        'class CaptureCalendar:\n'
        '    def __getattr__(self, name): return getattr(time, name)\n'
        '    def strftime(self, format, *args):\n'
        f'        return {STAMP!r} if format == "%Y-%m-%dT%H:%M:%SZ" else time.strftime(format, *args)\n'
        'capture.time = CaptureCalendar()\n'
        f'runpy.run_path({str(script)!r}, run_name="__main__")\n', encoding='utf-8')
    return destination
