"""Run the selected external participants (Codex, Grok, Antigravity) read-only on one prompt.

  python -B seats.py models
      Each seat's models right now. Codex's model and effort come from ~/.codex/config.toml (owner-maintained);
      Grok and Antigravity list theirs. Pick per task and pass explicitly; never hardcode a name (CORE-18).
  python -B seats.py quota [--agy-usage PATH]
      Codex weekly use and credits (last rate_limits record in ~/.codex/sessions); Antigravity pools when a
      project agy_usage.py is given. Grok has no reader: `ping` shows an exhausted balance (402).
  python -B seats.py ping --cwd DIR --seat SEAT:MODEL:EFFORT [--seat ...]
      A one-line prompt per seat, to see who answers before a real round.
  python -B seats.py run PROMPT OUTDIR --tag R1 --cwd DIR --seat SEAT:MODEL:EFFORT [--seat ...]
                     [--add-dir DIR ...] [--rules FILE] [--web]
      SEAT: codex | grok | agy. Writes OUTDIR/<tag>_<seat>.md, and OUTDIR/<tag>_sent.md with the exact text every
      seat got (prompt + rules). A non-empty existing output is kept and not rerun,
      so after a stop or a host restart rerun the same command. A failed run leaves <tag>_<seat>.failed.md and
      <tag>_<seat>.log (stderr tail); nothing is retried here: read the log first.
  python -B seats.py bundle TEMPLATE OUTDIR OUT [--set KEY=VALUE ...]
      Fill a round template: {ROUND1}/{ROUND2} get every answer of that round from OUTDIR word for word
      (CHAIR_R<n>.md and R<n>_<seat>.md), {RULING} gets OUTDIR/RULING.md; prints any placeholder left unfilled.
Read-only: Codex -s read-only; Grok plan mode with its shell, edit and agent tools removed (10-27: plan mode alone
still let Grok write through the shell); Antigravity plan mode. SEAT_RULES and any --rules file follow the prompt.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collaboration_state import RoundBusy, artifact_paths, atomic_json, file_digest, read_json, request_key, round_lock, source_state, valid_receipt

AGY = os.environ.get('AGY_EXE') or shutil.which('agy') or str(Path.home() / 'AppData/Local/agy/bin/agy.exe')
# npm installs Codex as codex.cmd, which CreateProcess does not find by the bare name.
CODEX = shutil.which('codex') or 'codex'
GROK = shutil.which('grok') or 'grok'
GROK_NO_WRITE = 'run_terminal_cmd,search_replace,write,StrReplace,Delete,task,Agent'
SEATS = ('codex', 'grok', 'agy')
SEAT_RULES = """

## Rules for this seat (appended by the collaboration runner)
- You are an independent review participant; the prompt defines the mode and participants. Read files only: run no shell commands,
  write or change no files. Your reply is saved as your answer.
- Never read credentials or secrets: *.p8, auth.json, oauth_creds.json, access_token*, *api_key*, rclone.conf,
  token files, private credential directories, or anything under .codex, .gemini, .grok.
- Separate facts (path:line, or a quoted source) from inferences; say what would disprove each inference.
- Agreement for its own sake is worthless; so is disagreement for its own sake. Zero findings is a valid answer.
- If you cannot check something, list it under "Not checked". Do not guess.
- Answer in the language and format the prompt asks for.
"""


def codex_config():
    cfg = Path.home() / '.codex' / 'config.toml'
    if not cfg.exists():
        return '?', '?'
    text = cfg.read_text(encoding='utf-8')
    found = {k: re.search(rf'^{k}\s*=\s*"([^"]+)"', text, re.M) for k in ('model', 'model_reasoning_effort')}
    return tuple(m.group(1) if m else '?' for m in found.values())


def models(_):
    model, effort = codex_config()
    print(f'codex: config.toml model={model} effort={effort}  ->  --seat codex:{model}:{effort}')
    for name, argv in (('grok', [GROK, 'models']), ('agy', [AGY, 'models'])):
        p = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120,
                           stdin=subprocess.DEVNULL)
        print(f'{name}:\n' + (p.stdout.strip() or p.stderr.strip()))


def quota(a):
    files = sorted(glob.glob(str(Path.home() / '.codex/sessions/**/rollout-*.jsonl'), recursive=True),
                   key=os.path.getmtime)
    limits, newest = None, None
    # Runs after the weekly window fills carry only a "premium" limit with no windows (11-07: the last 42 rollouts),
    # so walk back until a record has the weekly window, and say how old it is.
    for f in reversed(files):
        for line in reversed([l for l in open(f, encoding='utf-8', errors='replace') if '"rate_limits"' in l]):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:  # a rollout still being written can end mid-line
                continue
            rl = rec.get('payload', rec).get('rate_limits') or rec.get('rate_limits', {})
            newest = newest or rl
            if rl.get('primary'):
                limits, seen = rl, datetime.fromtimestamp(os.path.getmtime(f))
                break
        if limits:
            break
    if limits:
        prim = limits['primary']
        reset = f"{datetime.fromtimestamp(prim['resets_at']):%m-%d %H:%M}" if prim.get('resets_at') else '?'
        print(f"codex: weekly used {prim.get('used_percent')}% as of {seen:%m-%d %H:%M}, resets {reset}; "
              f"newest record credits {(newest.get('credits') or {}).get('balance')}")
    else:
        print('codex: no rate_limits record found; ping it')
    if a.agy_usage:
        p = subprocess.run([sys.executable, '-B', a.agy_usage], capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=300, stdin=subprocess.DEVNULL)
        print('agy:\n' + (p.stdout.strip() or p.stderr.strip()[-600:]))
    print('grok: no balance reader; ping shows an exhausted balance (402)')


def command(seat, model, effort, composed, part, cwd, add_dirs, web):
    if seat == 'codex':
        return [CODEX, 'exec', '--skip-git-repo-check', '-s', 'read-only', '-m', model, '-c',
                f'model_reasoning_effort={effort}', '-C', cwd, '-o', str(part), '-'], composed.read_text(encoding='utf-8')
    if seat == 'grok':
        return [GROK, '--prompt-file', str(composed), '-m', model, '--effort', effort, '--permission-mode', 'plan',
                '--cwd', cwd, '--disallowed-tools', GROK_NO_WRITE if web else GROK_NO_WRITE + ',web_search,web_fetch'], None
    # Antigravity has no flag that removes its edit tools (06-24 it edited source unasked); plan mode, the terminal
    # sandbox and the prompt hold it, and run_seats reports any change to a git cwd afterwards.
    argv = [AGY, '-p', f'Read {composed} and follow it exactly. Use only file reading tools; run no shell commands and '
            'write no files. Print the answer as your reply.', '--model', model, '--effort', effort, '--mode', 'plan',
            '--sandbox', '--add-dir', cwd, '--add-dir', str(composed.parent)]
    for d in add_dirs:
        argv += ['--add-dir', d]
    return argv, None


def run_one(spec, composed, outdir, tag, cwd, add_dirs, web, min_bytes, inputs=(), initial_state=None):
    seat, model, effort = spec.split(':')
    if seat not in SEATS:
        raise SystemExit(f'unknown seat {seat}; use one of {SEATS}')
    out = outdir / f'{tag}_{seat}.md'
    receipt = outdir / f'{tag}_{seat}.receipt.json'
    state = initial_state if initial_state is not None else source_state(cwd, outdir, inputs, artifact_paths(outdir, tag, [seat]))
    implementation = [file_digest(__file__), file_digest(Path(__file__).with_name('collaboration_state.py'))]
    key = request_key(spec, composed, cwd, add_dirs, web, state, implementation)
    if state['reusable'] and not web and out.exists() and out.stat().st_size > min_bytes and valid_receipt(receipt, out, key):
        return dict(status='reused', seat=seat, output=str(out), request_key=key, output_sha256=file_digest(out),
                    message=f'KEEP {seat} {out}')
    # Write beside the result and rename at the end, so a tracker never reads a half-written answer as done.
    part = outdir / f'{tag}_{seat}.md.partial'
    failed = outdir / f'{tag}_{seat}.failed.md'
    part.unlink(missing_ok=True)  # left by a killed run; Codex may fail before writing a new one
    argv, stdin = command(seat, model, effort, composed, part, str(Path(cwd).resolve()), add_dirs, web)
    start = time.time()
    io = {'input': stdin} if stdin is not None else {'stdin': subprocess.DEVNULL}
    p = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', **io)
    if seat != 'codex':
        part.write_text(p.stdout, encoding='utf-8', newline='\n')
    (outdir / f'{tag}_{seat}.log').write_text(f'model={model} effort={effort} rc={p.returncode}\n{p.stderr[-20000:]}',
                                              encoding='utf-8', newline='\n')
    text = part.read_text(encoding='utf-8', errors='replace').strip() if part.exists() else ''
    ok = p.returncode == 0 and len(text.encode('utf-8')) > min_bytes
    final = out if ok else failed
    if part.exists():
        part.replace(final)
    elif not ok:
        failed.write_text(p.stderr or 'no output', encoding='utf-8', newline='\n')
    if ok:
        failed.unlink(missing_ok=True)
        atomic_json(receipt, dict(status='success', request_key=key, output_sha256=file_digest(out),
                                 source=state, model=model, effort=effort, returncode=p.returncode))
    # A failure's cause is often a multi-line JSON error (Grok 402, Codex model_not_found): show its tail on one line.
    first = text.splitlines()[0][:120] if ok else ' '.join((p.stderr or text or 'EMPTY').split())[-300:]
    return dict(status='success' if ok else 'failed', seat=seat, output=str(final), request_key=key,
                output_sha256=file_digest(final) if final.is_file() else None, returncode=p.returncode,
                message=f'{"DONE" if ok else "FAIL"} {seat} rc={p.returncode} {int(time.time() - start)}s {final}\n  {first}')


def run_seats(a, prompt_text, outdir, tag, min_bytes):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', tag):
        raise ValueError('tag must be a filename component, without directory traversal')
    requested = [s.split(':', 1)[0] for s in a.seat]
    if len(requested) != len(set(requested)):
        raise ValueError('duplicate seat would overwrite another result; select each seat once')
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        with round_lock(outdir / f'{tag}.lock'):
            return _run_seats(a, prompt_text, outdir, tag, min_bytes, requested)
    except RoundBusy:
        print(f'BUSY: {tag} already has a writer; existing prompt and results preserved', file=sys.stderr)
        return 3


def _run_seats(a, prompt_text, outdir, tag, min_bytes, requested):
    extra = Path(a.rules).read_text(encoding='utf-8') if getattr(a, 'rules', None) else ''
    composed = outdir / f'{tag}_sent.md'  # the exact text every seat got
    composed.write_text(prompt_text + SEAT_RULES + ('\n' + extra if extra else ''), encoding='utf-8', newline='\n')
    inputs = list(getattr(a, 'input', []))
    if getattr(a, 'prompt', None):
        inputs.append(str(Path(a.prompt).resolve()))
    artifacts = artifact_paths(outdir, tag, requested)
    before = source_state(a.cwd, outdir, inputs, artifacts)
    status_path = outdir / f'{tag}_status.json'
    status = dict(schema=1, status='running', tag=tag, cwd=str(Path(a.cwd).resolve()),
                  requested_seats=requested, input=inputs, source_before=before, results=[],
                  verification='execution-and-artifacts-only; lead-verification-required')
    atomic_json(status_path, status)
    print(f'START {tag} {datetime.now():%m-%d %H:%M} seats={",".join(s.split(":")[0] for s in a.seat)}', flush=True)
    # No time limit: a long review is normal; judge a stuck seat by its log, not by elapsed time.
    with ThreadPoolExecutor(max_workers=len(a.seat)) as pool:
        jobs = [(s, pool.submit(run_one, s, composed, outdir, tag, a.cwd, getattr(a, 'add_dir', []),
                              getattr(a, 'web', False), min_bytes, inputs, before)) for s in a.seat]
        for spec, job in jobs:
            try:
                result = job.result()
            except (OSError, ValueError) as error:
                result = dict(status='failed', seat=spec.split(':', 1)[0], message=f'FAIL {type(error).__name__}: {error}')
            status['results'].append(result)
            atomic_json(status_path, status)
            print(result['message'], flush=True)
    after = source_state(a.cwd, outdir, inputs, artifacts)
    changed = before != after
    if changed:
        print('WRITE-DETECTED: source state changed during the run; inspect the actual diff and ownership, '
              'preserve concurrent changes, and revalidate affected answers.', flush=True)
        for result in status['results']:
            if result['status'] in ('success', 'reused'):
                receipt_path = outdir / f'{tag}_{result["seat"]}.receipt.json'
                receipt = read_json(receipt_path)
                if isinstance(receipt, dict) and receipt.get('request_key') == result.get('request_key'):
                    receipt.update(status='invalidated', reason='source_changed_during_round')
                    atomic_json(receipt_path, receipt)
                result['status'] = 'invalidated'
    failed = any(r['status'] not in ('success', 'reused') for r in status['results'])
    status.update(source_after=after, source_validation='changed' if changed else
                  'unchanged' if before['reusable'] else 'unverified',
                  status='source_changed' if changed else 'partial' if failed else 'complete')
    atomic_json(status_path, status)
    return 1 if failed or changed else 0


def git_status(cwd):
    p = subprocess.run(['git', '-C', str(cwd), 'status', '--porcelain'], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
    return p.stdout if p.returncode == 0 else None


def ping(a):
    with tempfile.TemporaryDirectory(prefix='seats_ping_') as d:
        return run_seats(a, 'Reply with the single word READY and nothing else.\n', Path(d), 'ping', 0)


def run(a):
    return run_seats(a, Path(a.prompt).read_text(encoding='utf-8'), Path(a.outdir), a.tag, 200)


def answers(outdir, n):
    """Every answer of round n, word for word: the chair's CHAIR_R<n>.md, then each seat that finished."""
    files = [('the coordinator', outdir / f'CHAIR_R{n}.md')] + [(s, outdir / f'R{n}_{s}.md') for s in SEATS]
    ledger = read_json(outdir / f'R{n}_status.json')
    results = {r['seat']: r for r in ledger.get('results', [])} if isinstance(ledger, dict) else (
        {} if (outdir / f'R{n}_status.json').exists() else None)
    parts = []
    for who, file in files:
        if not file.exists():
            continue
        if results is not None and who != 'the coordinator':
            result = results.get(who)
            if not result or result.get('status') not in ('success', 'reused') or result.get('output_sha256') != file_digest(file):
                continue
        parts.append(f'## Round {n} by {who}\n\n' + file.read_text(encoding='utf-8').strip())
    return '\n\n'.join(parts) if parts else None


def bundle(a):
    # Owner 11-07 asked that every seat see everything as fully as possible: later rounds carry all earlier answers
    # and the ruling verbatim, never a summary, and the chair does not copy them by hand.
    outdir = Path(a.outdir)
    text = Path(a.template).read_text(encoding='utf-8')
    values = dict(kv.split('=', 1) for kv in a.set)
    fields = set(re.findall(r'\{([A-Z][A-Z0-9_]*)\}', text))
    warnings = []
    partial = False
    for n in (1, 2):
        if f'{{ROUND{n}}}' in text:
            value = answers(outdir, n)
            if value is not None:
                values[f'ROUND{n}'] = value
            ledger = read_json(outdir / f'R{n}_status.json')
            if isinstance(ledger, dict):
                valid = {r['seat'] for r in ledger.get('results', []) if r.get('status') in ('success', 'reused')
                         and Path(r['output']).is_file() and r.get('output_sha256') == file_digest(r['output'])}
                missing = sorted(set(ledger.get('requested_seats', [])) - valid)
                if ledger.get('status') != 'complete' or missing:
                    partial = True
                    warnings.append(f'PARTIAL R{n}: status={ledger.get("status")}; missing={",".join(missing) or "none"}')
                if ledger.get('source_validation') == 'unverified':
                    warnings.append(f'UNVERIFIED_SOURCE R{n}: execution results do not prove source consistency')
            else:
                if (outdir / f'R{n}_status.json').exists():
                    partial = True
                    warnings.append(f'INVALID_RECEIPT R{n}: execution status cannot be read')
                else:
                    warnings.append(f'UNATTESTED R{n}: historical files without an execution receipt')
    if '{RULING}' in text:
        ruling = outdir / 'RULING.md'
        if ruling.exists():
            values['RULING'] = ruling.read_text(encoding='utf-8').strip()
    left = sorted(fields - values.keys())
    if left:
        print(f'INCOMPLETE: unfilled: {", ".join(left)}; output not replaced', file=sys.stderr)
        return 2
    text = re.sub(r'\{([A-Z][A-Z0-9_]*)\}', lambda m: values[m.group(1)], text)
    if warnings:
        text = '## Execution evidence\n\n' + '\n'.join('- ' + w for w in warnings) + '\n\n' + text
    Path(a.out).write_text(text, encoding='utf-8', newline='\n')
    print(f'{a.out}: {len(text.encode("utf-8"))} bytes' + ('; PARTIAL' if partial else ''))
    return 2 if partial and not getattr(a, 'allow_partial', False) else 0


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('models').set_defaults(fn=models)
    q = sub.add_parser('quota')
    q.add_argument('--agy-usage')
    q.set_defaults(fn=quota)
    b = sub.add_parser('bundle')
    b.add_argument('template')
    b.add_argument('outdir')
    b.add_argument('out')
    b.add_argument('--set', action='append', default=[], help='KEY=VALUE for a {KEY} placeholder')
    b.add_argument('--allow-partial', action='store_true', help='use explicitly labelled partial material; never marks it complete')
    b.set_defaults(fn=bundle)
    for name, fn in (('ping', ping), ('run', run)):
        s = sub.add_parser(name)
        if name == 'run':
            s.add_argument('prompt')
            s.add_argument('outdir')
            s.add_argument('--tag', required=True)
            s.add_argument('--add-dir', action='append', default=[])
            s.add_argument('--rules')
            s.add_argument('--web', action='store_true', help='Grok keeps web search and fetch')
            s.add_argument('--input', action='append', default=[], help='explicit source file whose content binds this result, repeatable')
        s.add_argument('--cwd', required=True)
        s.add_argument('--seat', action='append', required=True, help='SEAT:MODEL:EFFORT, all three explicit')
        s.set_defaults(fn=fn)
    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == '__main__':
    sys.exit(main())
