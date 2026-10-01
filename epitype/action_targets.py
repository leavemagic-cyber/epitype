"""Resolve observable file/Git actions to their actual target scope.

Only literal operands are evaluated. Unknown commands remain unverified; no shell
or script is executed by this parser. Current Git metadata is read for live hooks.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex

from . import patch_envelope


@dataclass(frozen=True)
class Action:
    kind: str
    target: str
    repository: str = ''
    checkout: str = ''


def canonical(value, cwd='', *, resolve=True):
    if not isinstance(value, str) or not value or any(part in value for part in ('$', '`', '*', '?')):
        return ''
    value = value.strip('"\'')
    if value == '~' or value.startswith(('~/', '~\\')):
        value = str(Path.home()) + value[1:]
    path = Path(value)
    if not path.is_absolute():
        if not cwd:
            return ''
        path = Path(cwd) / path
    try:
        return os.path.normcase(str(path.resolve()) if resolve else os.path.abspath(path))
    except (OSError, ValueError):
        return ''


def repository(path):
    """(checkout, main checkout) from Git's gitdir/commondir files, not .git.isdir."""
    if not path:
        return '', ''
    target = Path(path)
    for root in (target, *target.parents):
        git = root / '.git'
        if git.is_dir():
            return canonical(str(root)), canonical(str(root))
        if git.is_file():
            try:
                text = git.read_text(encoding='utf-8').strip()
                if not text.startswith('gitdir: '):
                    return '', ''
                directory = Path(text[len('gitdir: '):])
                if not directory.is_absolute():
                    directory = root / directory
                common = directory / 'commondir'
                git_common = directory / common.read_text(encoding='utf-8').strip() if common.is_file() else directory
                return canonical(str(root)), canonical(str(git_common.resolve().parent))
            except (OSError, ValueError):
                return '', ''
    return '', ''


def _words(source, powershell=False):
    try:
        if not powershell:
            quote=''
            for char in source:
                if char in (chr(34), chr(39)):
                    if quote==char: quote=''
                    elif not quote: quote=char
                elif char==chr(92) and not quote:
                    return []
        if powershell:
            # POSIX shlex consumes unquoted Windows backslashes as escapes.
            return [word[1:-1] if len(word)>1 and word[0]==word[-1] and word[0] in "\"'" else word
                    for word in shlex.split(source, posix=False)]
        return shlex.split(source, posix=True)
    except ValueError:
        return []


def _literal_commands(source):
    # A process exit code only proves one simple invocation succeeded. In a
    # multi-statement program a later success can hide an earlier failure.
    quote = ''
    for char in source.strip():
        if char in ('"', "'"):
            if quote == char: quote = ''
            elif not quote: quote = char
        elif char in ('$', '`') or (not quote and char in ';\n\r|&<>(){}#,'):
            return []
    if quote: return []
    return [source.strip()]


def actions(name, payload, cwd='', *, live=True):
    """Resolved actions; a shell mentioning a command is never an invocation."""
    normalize = lambda value, directory="": canonical(value, directory, resolve=live)
    payload = payload if isinstance(payload, dict) else {}
    cwd = payload.get('workdir') or payload.get('cwd') or cwd
    name = name.casefold().rsplit('.', 1)[-1]
    direct = {'read':'file.read', 'write':'file.change', 'edit':'file.change', 'multiedit':'file.change'}
    result = []
    if name in direct:
        target = normalize(payload.get('file_path') or payload.get('path'), cwd)
        if target:
            kind = direct[name]
            if name == 'write' and live and not Path(target).exists():
                kind = 'file.create'
            result.append(Action(kind, target, repository(target)[0] if live else ''))
        return result
    if name == 'apply_patch':
        source = payload.get('input') or payload.get('command') or payload.get('patch') or ''
        if isinstance(source, list):
            source = '\n'.join(str(item) for item in source)
        for entry in patch_envelope.parse(source):
            target = normalize(entry.path, cwd)
            # Creating a new file is not changing an existing named file.
            if target and entry.op != 'add':
                result.append(Action('file.change', target, repository(target)[0] if live else ''))
        return result
    if name not in ('bash', 'powershell', 'exec_command', 'local_shell_call', 'shell_command'):
        return result
    source = payload.get('cmd') or payload.get('command') or ''
    if not isinstance(source, str):
        return result
    # Complex shell programs require completed nested receipts, not guessed flow.
    if re.search(r'(?m)\b(?:foreach|function|while|for)\b|@\x27|@"', source):
        return result
    for command in _literal_commands(source):
        powershell = name == 'powershell' or 'powershell' in str(payload.get('shell', '')).casefold() or (name == 'exec_command' and 'bash' not in str(payload.get('shell', '')).casefold())
        words = _words(command, powershell)
        if not words:
            continue
        program = words[0].casefold()
        if program in ('cat', 'type', 'get-content'):
            operands = words[1:]
            if program == 'get-content':
                flag = next((i for i,w in enumerate(words) if w.casefold() in ('-literalpath','-path')), None)
                operands = [words[flag+1]] if flag is not None and flag+1<len(words) else words[1:2]
            for operand in operands:
                if operand.startswith('-'):
                    continue
                target = normalize(operand, cwd)
                if target:
                    result.append(Action('file.read',target,repository(target)[0] if live else ''))
        elif program == 'git':
            directory, index = cwd, 1
            while index < len(words) and words[index] == '-C':
                if index+1 == len(words):
                    break
                directory = normalize(words[index+1], directory)
                index += 2
            if index >= len(words) or not directory:
                continue
            sub = words[index]
            checkout, main = repository(directory) if live else (normalize(directory), '')
            if sub == 'status' and not any(w in ('-h','--help','--','-uno','--untracked-files=no') or not w.startswith('-') for w in words[index+1:]):
                result.append(Action('git.status',checkout or normalize(directory),main))
            elif words[index:index+3] == ['worktree','list','--porcelain'] or words[index:index+2] == ['worktree','list']:
                result.append(Action('git.worktrees',main or normalize(directory),main))
            elif sub in ('merge','cherry-pick','pull') and checkout and checkout == main and not any(w in ('--help','-h','--abort','--quit','--dry-run') for w in words[index+1:]):
                result.append(Action('git.main_update',main,main))
            elif sub == 'update-ref' and main:
                try:
                    branch = (Path(main)/'.git/HEAD').read_text(encoding='utf-8').strip().removeprefix('ref: ')
                except OSError:
                    branch = ''
                if branch and branch in words[index+1:]:
                    result.append(Action('git.main_update',main,main))
        elif program in ('rm', 'rmdir', 'remove-item'):
            operands = [w for w in words[1:] if not w.startswith('-')]
            for operand in operands:
                target = normalize(operand, cwd)
                if target and (not live or Path(target).is_dir()):
                    checkout,main=repository(target) if live else ('','')
                    result.append(Action('directory.delete',target,main,checkout))
    return list(dict.fromkeys(result))
