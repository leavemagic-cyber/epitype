"""Read literal nested shell commands as candidates, without evaluating code."""
import ast
import json
import re

_TOKENS = re.compile(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|\s+|.''')


def candidates(source):
    if not isinstance(source, str):
        return []
    tokens = [token for token in _TOKENS.findall(source)
              if not token.isspace() and not token.startswith(('//', '/*'))]
    found = []
    for i in range(len(tokens) - 6):
        if tokens[i:i+5] != ['tools', '.', 'exec_command', '(', '{']:
            continue
        depth, fields, end = 1, [], i + 5
        current = []
        for end in range(i + 5, len(tokens)):
            token = tokens[end]
            if token in ('{', '[', '('):
                depth += 1
            elif token in ('}', ']', ')'):
                depth -= 1
            if depth == 0:
                fields.append(current)
                break
            if token == ',' and depth == 1:
                fields.append(current)
                current = []
            else:
                current.append(token)
        if depth or end + 1 >= len(tokens) or tokens[end + 1] != ')':
            continue
        commands, unsafe = [], False
        for field in fields:
            if not field:
                continue
            if len(field) < 3 or field[1] != ':':
                unsafe = True  # Spread/computed/shorthand properties can override cmd.
                break
            key = field[0].strip('"\'')
            if key != 'cmd':
                continue
            if len(field) != 3 or not field[2].startswith(('"', "'")):
                unsafe = True
                break
            try:
                value = json.loads(field[2]) if field[2][0] == '"' else ast.literal_eval(field[2])
            except (ValueError, SyntaxError):
                unsafe = True
                break
            if isinstance(value, str):
                commands.append(value)
        if not unsafe and len(commands) == 1:
            found.append(commands[0])
    return found


def report(paths, limit=20):
    """Source pointers and digests; candidates never become observed tool calls."""
    import hashlib
    from . import transcript
    examples, count = [], 0
    for path in paths:
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line_number, line in enumerate(stream, 1):
                if 'exec_command' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                parts = transcript.turn_parts(row)
                if not parts:
                    continue
                for name, payload in parts[2]:
                    if name not in ('exec', 'functions.exec'):
                        continue
                    for command in candidates(payload.get('input')):
                        count += 1
                        if len(examples) < limit:
                            examples.append({'source': str(path), 'line': line_number,
                                'digest': hashlib.sha256(command.encode()).hexdigest()[:16]})
    return {'literal_candidates': count, 'examples': examples,
            'execution': 'UNVERIFIED：字面指令可能位於未執行分支，不能算成真呼叫或漏擋'}
