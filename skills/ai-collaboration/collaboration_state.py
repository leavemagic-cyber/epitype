"""Local execution receipts, without models, network clients, or implicit body scans."""
from pathlib import Path
from contextlib import contextmanager
import errno
import hashlib
import json
import os
import subprocess
import tempfile


class RoundBusy(RuntimeError):
    pass


@contextmanager
def round_lock(path):
    """The OS releases this nonblocking lock after a crash; never deletes another job's lease."""
    with Path(path).open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RoundBusy(str(path)) from error
            raise
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def git_output(cwd, *args):
    result = subprocess.run(['git', '-C', str(cwd), *args], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    return result.stdout if result.returncode == 0 else None


def artifact_paths(outdir, tag, seats):
    outdir = Path(outdir).resolve()
    paths = [outdir / f'{tag}.lock', outdir / f'{tag}_sent.md', outdir / f'{tag}_status.json']
    for seat in seats:
        paths.extend(outdir / f'{tag}_{seat}{suffix}' for suffix in
                     ('.md', '.md.partial', '.failed.md', '.log', '.receipt.json'))
    return paths


def source_state(cwd, outdir, inputs=(), artifacts=()):
    """Read only declared bodies. Uncovered dirty source disables reuse, not work."""
    cwd, outdir = Path(cwd).resolve(), Path(outdir).resolve()
    files = {}
    for name in inputs:
        path = Path(name)
        path = (cwd / path).resolve() if not path.is_absolute() else path.resolve()
        files[str(path)] = file_digest(path) if path.is_file() else None
    head = git_output(cwd, 'rev-parse', 'HEAD')
    root_text = git_output(cwd, 'rev-parse', '--show-toplevel') if head else None
    root = Path(root_text.strip()).resolve() if root_text else cwd
    pathspec = [':/']
    if outdir != root and outdir.is_relative_to(root):
        pathspec.append(':(top,exclude)' + outdir.relative_to(root).as_posix())
    else:
        pathspec.extend(':(top,exclude)' + Path(path).resolve().relative_to(root).as_posix()
                        for path in artifacts if Path(path).resolve().is_relative_to(root))
    status = git_output(cwd, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--', *pathspec) if head else None
    dirty = set()
    if status is not None:
        entries = iter(status.split('\0'))
        for entry in entries:
            if not entry:
                continue
            dirty.add(str((root / entry[3:]).resolve()))
            if 'R' in entry[:2] or 'C' in entry[:2]:
                previous = next(entries, '')
                if previous:
                    dirty.add(str((root / previous).resolve()))
    known_files = bool(files) and all(value is not None or name in dirty for name, value in files.items())
    reusable = bool(head and status is not None and dirty.issubset(files)) or (head is None and known_files)
    return dict(cwd=str(cwd), head=head.strip() if head else None, files=files,
                status=digest(status.encode()) if status is not None else None,
                reusable=reusable, coverage='git-and-declared-inputs' if head and reusable else
                'declared-inputs-only' if reusable else 'unverified-source')


def request_key(spec, prompt, cwd, add_dirs, web, state, implementation):
    value = dict(schema=1, spec=spec, prompt_sha256=file_digest(prompt), cwd=str(Path(cwd).resolve()),
                 add_dirs=sorted(str(Path(p).resolve()) for p in add_dirs), web=bool(web), source=state,
                 implementation=implementation)
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


def valid_receipt(path, output, key):
    value = read_json(path)
    return bool(isinstance(value, dict) and value.get('status') == 'success' and value.get('request_key') == key
                and Path(output).is_file() and value.get('output_sha256') == file_digest(output))
