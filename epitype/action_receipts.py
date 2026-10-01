"""Normalized completed facts from existing Pre/PostToolUse hooks.

No resource counter and no raw commands, results, or file contents are stored.
A missing/failed result cannot become prerequisite evidence. Actors are isolated
by session, transcript and agent. Hosts need no hook-registration changes.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import json
import hashlib
from pathlib import Path
import sqlite3
import time

from . import action_targets, tool_evidence


@dataclass
class Fact:
    call_id: str
    started: int
    finished: int
    success: bool | None
    actions: tuple
    turn: str = ''
    inventory: tuple = ()
    actor: str = ""


def database_path(config_file):
    return Path(config_file).parent / 'action_receipts.sqlite3'


def actor(event):
    return json.dumps([event.get('session_id',''), event.get('transcript_path',''),
                       event.get('agent_id') or event.get('agentId') or ''], separators=(',',':'))


def projected(event):
    name=event.get('tool_name','')
    payload=event.get('tool_input')
    return action_targets.actions(name,payload,event.get('cwd',''))


@contextmanager
def _db(path):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path,timeout=.15)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, actor TEXT, call_id TEXT, turn TEXT, started INTEGER, finished INTEGER DEFAULT 0, success INTEGER, actions TEXT, signature TEXT, inventory TEXT DEFAULT "[]", seen REAL, UNIQUE(actor,call_id))')
        db.execute('CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)')
        db.execute('INSERT OR IGNORE INTO clock VALUES(1,0)')
        db.execute('CREATE INDEX IF NOT EXISTS facts_actor ON facts(actor,started)')
        db.execute('CREATE TABLE IF NOT EXISTS actors (actor TEXT PRIMARY KEY, complete INTEGER NOT NULL)')
        with db: yield db


def _tick(db):
    db.execute('UPDATE clock SET value=value+1 WHERE id=1')
    return db.execute('SELECT value FROM clock WHERE id=1').fetchone()[0]



def initialize(event,path):
    if not event.get('session_id'): return
    with _db(path) as db:
        db.execute('INSERT OR IGNORE INTO actors VALUES(?,?)',(actor(event),int(event.get('source')=='startup')))


def coverage(event,path):
    if not Path(path).exists(): return False
    with _db(path) as db:
        row=db.execute('SELECT complete FROM actors WHERE actor=?',(actor(event),)).fetchone()
    return bool(row and row[0])

def signature(event):
    data=json.dumps([event.get('tool_name'),event.get('tool_input')],sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def begin(event,path):
    identity=event.get('tool_use_id')
    if not identity or not event.get('session_id'): return False
    actions=projected(event)
    if not actions:
        name=str(event.get('tool_name','')).casefold().rsplit('.',1)[-1]
        if name not in ('bash','powershell','exec_command','local_shell_call','shell_command','grep','glob'): return False
        actions=[action_targets.Action('evidence.opaque','','')]

    encoded=json.dumps([(a.kind,a.target,a.repository) for a in actions],separators=(',',':'))
    with _db(path) as db:
        prior=db.execute('SELECT signature FROM facts WHERE actor=? AND call_id=?',(actor(event),identity)).fetchone()
        if prior:
            if prior[0]!=signature(event):
                db.execute('UPDATE facts SET success=NULL,finished=-1 WHERE actor=? AND call_id=?',(actor(event),identity))
            return False
        db.execute('INSERT INTO facts(actor,call_id,turn,started,actions,signature,seen) VALUES(?,?,?,?,?,?,?)',
                   (actor(event),identity,event.get('turn_id',''),_tick(db),encoded,signature(event),time.time()))
    return True


def text(response):
    if isinstance(response,str):
        try: return text(json.loads(response))
        except ValueError: return response
    if isinstance(response,dict):
        return '\n'.join(text(response[k]) for k in ('stdout','output','content','text') if k in response)
    if isinstance(response,list): return '\n'.join(text(item) for item in response)
    return ''


def inventory(response,cwd='', *, live=True):
    import re
    # Git's documented porcelain format is unambiguous even for spaces.
    body=text(response).replace(chr(0),chr(10))
    paths=re.findall(r'(?m)^worktree ([^\r\n\x00]+)',body)
    if not paths:
        # Git's documented default format: path, abbreviated HEAD, branch.
        paths=re.findall(r'(?m)^(.+?)\s+(?:[0-9a-f]{7,40}\s+(?:\[.*\]|\(detached HEAD\))|\(bare\)).*$',body)
    return tuple(filter(None,(action_targets.canonical(path,cwd,resolve=live) for path in paths)))


def status(event):
    response=event.get('tool_response')
    if not event.get('turn_id') and '.claude' in str(event.get('transcript_path','')).replace('\\','/').split('/'):
        # Claude PostToolUse runs on success. Result content is arbitrary data.
        return False if isinstance(response,dict) and response.get('is_error') is True else True
    # Codex PostToolUse also fires for nonzero Bash exits. Inspect execution
    # metadata, not an exit-code lookalike in the program's final output.
    if isinstance(response,str):
        try: response=json.loads(response)
        except ValueError:
            response=response.split('Final output:',1)[0].split('Output:',1)[0]
    if isinstance(response,dict):
        for key in ('exit_code','exitCode','returncode'):
            if type(response.get(key)) is int: return response[key]==0
        if response.get('isError') is True or response.get('is_error') is True: return False
        if response.get('isError') is False or response.get('is_error') is False: return True
        # Model-facing unified-exec headers can be inside an MCP text item.
        response=text(response).split('Final output:',1)[0]
    return tool_evidence.outcome(response)


def finish(event,path):
    identity=event.get('tool_use_id')
    name=str(event.get('tool_name','')).casefold().rsplit('.',1)[-1]
    if name not in ('read','write','edit','multiedit','apply_patch','bash','powershell','exec_command','local_shell_call','shell_command','grep','glob'):
        return False
    if not identity or not event.get('session_id') or not Path(path).exists(): return False
    with _db(path) as db:
        prior=db.execute('SELECT actions,finished,success,signature FROM facts WHERE actor=? AND call_id=?',(actor(event),identity)).fetchone()
        if prior is None:
            db.execute('INSERT OR REPLACE INTO actors VALUES(?,0)',(actor(event),))
            return False
        if prior[1]==-1: return False
        if prior[3]!=signature(event):
            db.execute('UPDATE facts SET finished=-1,success=NULL WHERE actor=? AND call_id=?',(actor(event),identity))
            return False
        success=status(event)
        if prior[1]:
            if prior[2] != success:
                db.execute('UPDATE facts SET finished=-1,success=NULL WHERE actor=? AND call_id=?',(actor(event),identity))
            return False
        scopes=inventory(event.get('tool_response'),event.get('cwd','')) if success is True and any(a[0]=='git.worktrees' for a in json.loads(prior[0])) else ()
        db.execute('UPDATE facts SET finished=?,success=?,inventory=? WHERE actor=? AND call_id=?',
                   (_tick(db),success,json.dumps(scopes),actor(event),identity))
    return True


def read(event,path):
    if not Path(path).exists(): return ()
    with _db(path) as db:
        rows=db.execute('SELECT call_id,started,finished,success,actions,turn,inventory FROM facts WHERE actor=? ORDER BY started',(actor(event),)).fetchall()
    return tuple(Fact(i,s,f,None if ok is None else bool(ok),
                      tuple(action_targets.Action(*a) for a in json.loads(actions)),turn,tuple(json.loads(scopes)))
                 for i,s,f,ok,actions,turn,scopes in rows)
