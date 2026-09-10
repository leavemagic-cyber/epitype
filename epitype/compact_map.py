import sys; [getattr(stream, 'reconfigure', lambda **_: None)(encoding='utf-8', errors='replace') for stream in (sys.stdout, sys.stderr)]

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

try:
    from .transcript import message_text as _text_content, source_record
    from .memspec import (
        COMPACT_MAP_ASSISTANT_MAX_CHARS,
        COMPACT_MAP_DEFAULT_BUDGET_BYTES,
        COMPACT_MAP_DIRECTORY,
        COMPACT_MAP_MAX_LINE_BYTES,
        COMPACT_MAP_TAIL_BYTES,
        COMPACT_MAP_USER_MAX_CHARS,
    )
except ImportError:  # Direct script execution keeps the U1 CLI contract.
    from transcript import message_text as _text_content, source_record
    from memspec import (
        COMPACT_MAP_ASSISTANT_MAX_CHARS,
        COMPACT_MAP_DEFAULT_BUDGET_BYTES,
        COMPACT_MAP_DIRECTORY,
        COMPACT_MAP_MAX_LINE_BYTES,
        COMPACT_MAP_TAIL_BYTES,
        COMPACT_MAP_USER_MAX_CHARS,
    )


# 2026-09-01 實測事故：對話壓縮時未落檔的結論會遺失，導致後續無法可靠續接；
# 規則：以純程式抄出 2KB 地圖，壓縮後按圖回 transcript 撈原文，不全量重讀。


# 壓縮後把地圖交回模型的那一行（U-R1，2026-09-10）。PreCompact 印的 context 在兩邊
# 宿主都到不了模型（Claude Code 的 PreCompact 不能注入；Codex 0.153 的 PreCompactOutcome
# 只有 Continue／Stopped），所以改由 SessionStart 在 source=compact 時端回來。
MAP_NOTICE_TEMPLATE = '壓縮前原文地圖：{path}；需要原文時讀它按行號回撈。'
# 整行上限按 UTF-8 位元組計。超限＝整行不注，路徑絕不截斷：半條路徑讀不回任何東西，
# 而讀不回來的指標比沒有指標更貴（Codex round 1 COUNTER，2026-09-10）。
MAP_NOTICE_MAX_BYTES = 240
_SESSION_COMPONENT_UNSAFE = re.compile(r'[^A-Za-z0-9._-]')


def session_component(session_id, limit=128):
    """Filesystem-safe session identifier.

    Byte-for-byte the same rule as `adapters/claude/_hook_common.session_component`;
    the map destination has to be computable from `epitype/` alone (PreCompact writes
    it, SessionStart reads it), and `tests/compact_map_destination_regression.py`
    pins the two implementations to each other so neither can drift.
    """
    text = session_id if isinstance(session_id, str) else ''
    return _SESSION_COMPONENT_UNSAFE.sub('_', text).strip('._-')[:limit] or 'nosession'


def map_destination(vault, session_id, transcript_path):
    """Where this session's recovery map lives: one file per (session, transcript).

    Pure function on purpose — PreCompact writes here and SessionStart reads here,
    and the two must land on the same path without sharing any state but the event.
    """
    component = session_component(session_id, limit=80)
    transcript = Path(transcript_path).expanduser().resolve()
    digest = hashlib.sha256(os.fspath(transcript).encode('utf-8')).hexdigest()[:12]
    return (Path(vault) / COMPACT_MAP_DIRECTORY / f'{component}-{digest}.md').resolve()


def map_notice(destination):
    """The one line, or None when the absolute path makes it exceed the byte cap."""
    line = MAP_NOTICE_TEMPLATE.format(path=os.fspath(destination))
    if len(line.encode('utf-8')) > MAP_NOTICE_MAX_BYTES:
        return None
    return line


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return number


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

    for line_number, raw_line in numbered_lines:
        if raw_line.endswith(b'\r'):
            raw_line = raw_line[:-1]
        if len(raw_line) > COMPACT_MAP_MAX_LINE_BYTES:
            continue
        try:
            item = json.loads(raw_line.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
        source = source_record(item)
        if source is None:
            continue
        role, text = source
        limit = COMPACT_MAP_ASSISTANT_MAX_CHARS if role == 'A' else COMPACT_MAP_USER_MAX_CHARS
        if len(text) > limit:
            text = text[:limit] + ' [truncated]'
        target = assistants if role == 'A' else users
        target.append((line_number, role, _one_line(text)))
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
        '｜僅定位，非完整記憶或裁定；U=使用者記錄 Q=人類排隊 A=助理'
        f'｜原文查找器={Path(__file__).with_name("source_lookup.py").resolve()}'
        '（python 執行；來源檔 --line N --limit 1；尾窗加 --offset 起始位元）'
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

        marker = b' [truncated]'
        allowed_text = remaining - len(prefix) - len(suffix) - len(marker)
        if allowed_text < 0:
            break
        output.extend(prefix)
        output.extend(_utf8_prefix_bytes(text, allowed_text))
        output.extend(marker)
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

            # U-R1：PreCompact 寫、SessionStart 讀，同一個 (session, transcript)
            # 必須落在同一個檔上；不同 session 或不同 transcript 必須分開。
            vault = root / 'vault'
            same_a = map_destination(vault, 'sess-1', transcript)
            same_b = map_destination(vault, 'sess-1', os.fspath(transcript))
            other_session = map_destination(vault, 'sess-2', transcript)
            other_transcript = map_destination(vault, 'sess-1', tail_source)
            checks.append((
                'map_destination is stable per session and transcript, and separates both',
                same_a == same_b
                and same_a.parent == (vault / COMPACT_MAP_DIRECTORY).resolve()
                and same_a != other_session
                and same_a != other_transcript
                and same_a.name.startswith('sess-1-')
                and len(same_a.stem.rsplit('-', 1)[1]) == 12,
            ))
            checks.append((
                'a hostile or missing session id still yields one safe filename',
                map_destination(vault, ' weird/id. ', transcript).name.startswith('weird_id-')
                and map_destination(vault, None, transcript).name.startswith('nosession-'),
            ))

            # 240 B（UTF-8）是整行的上限，超限整行不注、路徑絕不截斷。
            short_notice = map_notice(same_a)
            long_path = Path(os.fspath(root / ('d' * 260) / 'map.md'))
            checks.append((
                'the notice carries the whole path under the byte cap, and is dropped whole over it',
                short_notice is not None
                and os.fspath(same_a) in short_notice
                and len(short_notice.encode('utf-8')) <= MAP_NOTICE_MAX_BYTES
                and map_notice(long_path) is None,
            ))
    except Exception as exc:
        print(f'SELFTEST ERROR {type(exc).__name__}: {exc}', file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 12
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
