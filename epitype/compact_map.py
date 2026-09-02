import sys; [getattr(stream, 'reconfigure', lambda **_: None)(encoding='utf-8', errors='replace') for stream in (sys.stdout, sys.stderr)]

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

try:
    from .memspec import (
        COMPACT_MAP_ASSISTANT_MAX_CHARS,
        COMPACT_MAP_DEFAULT_BUDGET_BYTES,
        COMPACT_MAP_MAX_LINE_BYTES,
        COMPACT_MAP_TAIL_BYTES,
        COMPACT_MAP_USER_MAX_CHARS,
    )
except ImportError:  # Direct script execution keeps the U1 CLI contract.
    from memspec import (
        COMPACT_MAP_ASSISTANT_MAX_CHARS,
        COMPACT_MAP_DEFAULT_BUDGET_BYTES,
        COMPACT_MAP_MAX_LINE_BYTES,
        COMPACT_MAP_TAIL_BYTES,
        COMPACT_MAP_USER_MAX_CHARS,
    )


# 2026-09-01 實測事故：對話壓縮時未落檔的結論會遺失，導致後續無法可靠續接；
# 規則：以純程式抄出 2KB 地圖，壓縮後按圖回 transcript 撈原文，不全量重讀。


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return number


def _text_content(message):
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get('type') != 'text':
            continue
        text = block.get('text')
        if isinstance(text, str):
            parts.append(text)
    return ''.join(parts)


def _one_line(text):
    return text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', ' ⏎ ')


def _tail_lines(path):
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        file_size = stream.tell()
        requested_start = max(0, file_size - COMPACT_MAP_TAIL_BYTES)
        stream.seek(requested_start)
        data = stream.read(COMPACT_MAP_TAIL_BYTES)

    window_start = requested_start
    number_mode = 'absolute'
    if requested_start:
        number_mode = 'tail'
        newline = data.find(b'\n')
        if newline < 0:
            return [], file_size, file_size, number_mode
        window_start += newline + 1
        data = data[newline + 1:]

    raw_lines = data.split(b'\n')
    if raw_lines and raw_lines[-1] == b'':
        raw_lines.pop()
    return list(enumerate(raw_lines, start=1)), window_start, file_size, number_mode


def _extract_candidates(numbered_lines):
    users = []
    assistants = []
    pending_assistant_line = None
    pending_assistant_text = ''

    for line_number, raw_line in numbered_lines:
        if raw_line.endswith(b'\r'):
            raw_line = raw_line[:-1]
        if len(raw_line) > COMPACT_MAP_MAX_LINE_BYTES:
            continue
        try:
            item = json.loads(raw_line.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(item, dict) or item.get('isSidechain') is True:
            continue

        role = item.get('type')
        if role not in ('user', 'assistant'):
            continue
        text = _text_content(item.get('message'))
        if not text.strip():
            continue

        if role == 'user':
            if pending_assistant_line is not None:
                assistants.append((
                    pending_assistant_line,
                    'A',
                    _one_line(pending_assistant_text),
                ))
                pending_assistant_line = None
                pending_assistant_text = ''
            users.append(
                (
                    line_number,
                    'U',
                    _one_line(text[:COMPACT_MAP_USER_MAX_CHARS]),
                )
            )
        else:
            pending_assistant_line = line_number
            pending_assistant_text = (
                pending_assistant_text + text
            )[-COMPACT_MAP_ASSISTANT_MAX_CHARS:]

    if pending_assistant_line is not None:
        assistants.append((
            pending_assistant_line,
            'A',
            _one_line(pending_assistant_text),
        ))
    return users, assistants


def _utf8_prefix_bytes(text, byte_limit):
    if byte_limit <= 0:
        return b''
    encoded = text.encode('utf-8')
    if len(encoded) <= byte_limit:
        return encoded
    return encoded[:byte_limit].decode('utf-8', errors='ignore').encode('utf-8')


def _header(source, generated_at, numbered_lines, window_start, number_mode):
    if numbered_lines:
        covered = f'{numbered_lines[0][0]}-{numbered_lines[-1][0]}'
    else:
        covered = 'empty'
    text = (
        f'來源檔={source}｜產生時間={generated_at}'
        f'｜涵蓋行號範圍={covered}'
    )
    if number_mode == 'tail':
        text += f'｜行號基準=尾窗｜尾窗起始位元={window_start}'
    return text


def _render_map(header, candidates, budget_bytes):
    header_bytes = (header + '\n').encode('utf-8')
    if len(header_bytes) > budget_bytes:
        return _utf8_prefix_bytes(header + '\n', budget_bytes)

    output = bytearray(header_bytes)
    for line_number, role, text in candidates:
        prefix = f'［{line_number}｜{role}｜'.encode('utf-8')
        suffix = '］\n'.encode('utf-8')
        remaining = budget_bytes - len(output)
        if remaining < len(prefix) + len(suffix):
            break

        text_bytes = text.encode('utf-8')
        if len(prefix) + len(text_bytes) + len(suffix) <= remaining:
            output.extend(prefix)
            output.extend(text_bytes)
            output.extend(suffix)
            continue

        allowed_text = remaining - len(prefix) - len(suffix)
        output.extend(prefix)
        output.extend(_utf8_prefix_bytes(text, allowed_text))
        output.extend(suffix)
        break
    return bytes(output)


def build_map(transcript, out, budget_bytes):
    transcript = Path(transcript)
    out = Path(out)
    if transcript.resolve() == out.resolve():
        raise ValueError('transcript and output paths must differ')

    numbered_lines, window_start, _, number_mode = _tail_lines(transcript)
    users, assistants = _extract_candidates(numbered_lines)
    candidates = list(reversed(users)) + list(reversed(assistants))
    generated_at = datetime.now().astimezone().isoformat(timespec='seconds')
    header = _header(
        transcript.resolve(),
        generated_at,
        numbered_lines,
        window_start,
        number_mode,
    )
    payload = _render_map(header, candidates, budget_bytes)

    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(f'.{out.name}.tmp-{os.getpid()}')
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, out)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return payload


def _json_line(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix='compact-map-') as temp_dir:
            root = Path(temp_dir).resolve()
            transcript = root / 'synthetic.jsonl'
            output = root / 'map.txt'
            lines = [
                _json_line({
                    'type': 'user',
                    'message': {'content': '第一位 user says hello'},
                }),
                _json_line({
                    'type': 'assistant',
                    'message': {'content': [
                        {'type': 'text', 'text': '分析段。'},
                        {'type': 'tool_use', 'text': 'ignore me'},
                        {'type': 'text', 'text': 'Final 結論。'},
                    ]},
                }),
                _json_line({
                    'type': 'user',
                    'isSidechain': True,
                    'message': {'content': 'SIDECHAIN-MARKER'},
                }),
                '{broken json',
                _json_line({'type': 'progress', 'message': {'content': 'skip'}}),
                _json_line({
                    'type': 'user',
                    'message': {'content': [
                        {'type': 'text', 'text': '第二位 '},
                        {'type': 'text', 'text': 'user English'},
                    ]},
                }),
                _json_line({
                    'type': 'assistant',
                    'message': {'content': '前文。\n最後結論 END'},
                }),
                _json_line({
                    'type': 'user',
                    'message': {'content': 'OVERLONG-' + 'x' * COMPACT_MAP_MAX_LINE_BYTES},
                }),
            ]
            transcript.write_text('\n'.join(lines) + '\n', encoding='utf-8')

            payload = build_map(transcript, output, 4096)
            mapped = payload.decode('utf-8')
            checks.append((
                'mixed user and assistant extraction',
                '第一位 user says hello' in mapped
                and '第二位 user English' in mapped
                and '分析段。Final 結論。' in mapped
                and '最後結論 END' in mapped,
            ))
            checks.append(('sidechain skipped', 'SIDECHAIN-MARKER' not in mapped))
            checks.append(('malformed JSON tolerated', output.is_file()))
            checks.append((
                'newest first with user priority',
                mapped.index('第二位 user English') < mapped.index('第一位 user says hello')
                < mapped.index('最後結論 END') < mapped.index('分析段。Final 結論。'),
            ))
            checks.append((
                'source line numbers recover records',
                '［1｜U｜第一位 user says hello］' in mapped
                and '［2｜A｜分析段。Final 結論。］' in mapped
                and '［6｜U｜第二位 user English］' in mapped
                and '［7｜A｜前文。 ⏎ 最後結論 END］' in mapped,
            ))
            checks.append(('overlong line skipped', 'OVERLONG-' not in mapped))

            tight = build_map(transcript, root / 'tight.txt', 127)
            checks.append((
                'budget hard limit',
                len(tight) <= 127 and tight.decode('utf-8') is not None,
            ))
            clipped = _utf8_prefix_bytes('中🙂文 English', 5)
            checks.append((
                'UTF-8 clipping stays decodable',
                clipped.decode('utf-8') is not None,
            ))

            tail_source = root / 'tail.jsonl'
            early = _json_line({
                'type': 'user',
                'message': {'content': 'EARLY-MARKER'},
            }).encode('utf-8') + b'\n'
            late = _json_line({
                'type': 'user',
                'message': {'content': 'LATE-MARKER'},
            }).encode('utf-8') + b'\n'
            filler = b'{}\n'
            repeat = COMPACT_MAP_TAIL_BYTES // len(filler) + 128
            with tail_source.open('wb') as stream:
                stream.write(early)
                stream.write(filler * repeat)
                stream.write(late)
            tail_map = build_map(tail_source, root / 'tail-map.txt', 4096).decode('utf-8')
            checks.append((
                'tail window excludes synthetic prefix',
                'EARLY-MARKER' not in tail_map
                and 'LATE-MARKER' in tail_map
                and '行號基準=尾窗' in tail_map,
            ))
    except Exception as exc:
        print(f'SELFTEST ERROR {type(exc).__name__}: {exc}', file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 9
    status = 'PASS' if passed == total and len(checks) == total else 'FAIL'
    print(f'SELFTEST {status} {passed}/{total}')
    if status != 'PASS':
        for name, ok in checks:
            if not ok:
                print(f'FAILED: {name}', file=sys.stderr)
    return 0 if status == 'PASS' else 1


def _parser():
    parser = argparse.ArgumentParser(description='Build a bounded compact recovery map')
    parser.add_argument('--selftest', action='store_true')
    commands = parser.add_subparsers(dest='command')
    build = commands.add_parser('build')
    build.add_argument('--transcript', required=True)
    build.add_argument('--out', required=True)
    build.add_argument(
        '--budget-bytes',
        type=_positive_int,
        default=COMPACT_MAP_DEFAULT_BUDGET_BYTES,
    )
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.command != 'build':
        parser.print_help(sys.stderr)
        return 2
    try:
        build_map(args.transcript, args.out, args.budget_bytes)
    except (OSError, ValueError) as exc:
        print(f'ERROR {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
