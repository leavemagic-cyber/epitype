"""Completed tool calls with host result provenance, rather than command mentions."""
from dataclasses import dataclass
import json
import re

from . import transcript


@dataclass
class Call:
    call_id: str
    name: str
    payload: dict
    cwd: str
    started: int
    finished: int = 0
    success: bool | None = None
    response: object = None
    stamp: str = ""
    actor: str = ""
    result_kind: str = ""


def outcome(value):
    """Unknown remains unknown; explicit process failure outranks envelope success."""
    if isinstance(value, dict):
        if value.get('isError') is True or value.get('is_error') is True:
            return False
        if value.get('type') == 'tool_result' and 'tool_use_id' in value:
            # The result body can itself be a file containing exit_code/is_error.
            # Those data fields are not execution metadata for this invocation.
            return True
        for key in ('exit_code', 'exitCode', 'returncode'):
            if type(value.get(key)) is int:
                return value[key] == 0
        nested = [outcome(value[key]) for key in ('output', 'result', 'content', 'text') if key in value]
        if False in nested:
            return False
        if True in nested:
            return True
        if value.get('is_error') is False or value.get('isError') is False:
            return True
    elif isinstance(value, list):
        results = [outcome(item) for item in value]
        if False in results:
            return False
        if True in results:
            return True
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return outcome(parsed)
        # Anchored host process metadata, not an arbitrary mention of an exit code.
        value = value.split('Final output:', 1)[0]
        codes = re.findall(r'(?m)^(?:Process exited with code|Exit code:)\s*(-?\d+)\s*$', value)
        if codes:
            return all(int(code) == 0 for code in codes)
    return None


def calls(rows, cwd=''):
    """Preserve call/result IDs, cwd, and causal positions across owner turns.

    A later result cannot satisfy a prerequisite that was still pending when the
    action started. Reused IDs with different arguments invalidate the evidence.
    """
    indexed, ordered = {}, []
    for position, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get('isMeta') or row.get('isCompactSummary'):
            continue
        if isinstance(row.get('cwd'), str):
            cwd = row['cwd']
        payload = row.get('payload')
        if row.get('type') in ('session_meta', 'turn_context') and isinstance(payload, dict):
            if isinstance(payload.get('cwd'), str):
                cwd = payload['cwd']
        if row.get('type') == 'response_item' and isinstance(payload, dict):
            shape = payload.get('type')
            if shape in ('function_call', 'custom_tool_call', 'local_shell_call'):
                items = [(payload.get('call_id'), payload.get('name') or payload.get('tool_name') or shape,
                          transcript._call_input(payload))]
                results = []
            elif shape in ('function_call_output', 'custom_tool_call_output'):
                items, results = [], [(payload.get('call_id'), payload.get('output'))]
            else:
                continue
        else:
            message = row.get('message')
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            items = [(block.get('id'), block.get('name'), block.get('input')) for block in content
                     if row.get('type') == 'assistant' and isinstance(block, dict) and block.get('type') == 'tool_use']
            results = [(block.get('tool_use_id'), block) for block in content
                       if row.get('type') == 'user' and isinstance(block, dict) and block.get('type') == 'tool_result']
        agent=str(row.get('agentId') or row.get('agent_id') or ('UNVERIFIED-SIDECHAIN' if row.get('isSidechain') else ''))
        for identity, name, arguments in items:
            if not isinstance(identity, str) or not identity or not isinstance(arguments, dict):
                continue
            local_cwd = arguments.get('workdir') or arguments.get('cwd') or cwd
            prior = indexed.get((agent,identity))
            if prior is not None:
                if (prior.name, prior.payload, prior.cwd) != (name, arguments, local_cwd):
                    prior.success = None
                    prior.finished = -1
                continue
            call = Call(identity, str(name or ''), arguments, str(local_cwd or ''), position)
            call.stamp = str(row.get("timestamp") or "")
            call.actor = agent
            indexed[(agent,identity)] = call
            ordered.append(call)
        for identity, response in results:
            call = indexed.get((agent,identity))
            if call is None or call.finished == -1:
                continue
            status = outcome(response)
            if call.finished and (call.success != status or call.response != response):
                call.success, call.finished = None, -1
                continue
            meta=row.get("toolUseResult")
            if isinstance(meta,dict): call.result_kind=str(meta.get("type") or "")
            call.success, call.finished, call.response = status, position, response
    from .tool_forwarding import normalize
    return [normalize(call) for call in ordered]


def read(path):
    def rows():
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    return calls(rows())


def recent(path,cwd='',budget=1024*1024):
    """Bounded live completion lookup; missing prefix is never proof of absence."""
    with path.open('rb') as stream:
        stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-budget))
        body=stream.read()
    rows=[]
    for line in body.splitlines():
        try: rows.append(json.loads(line))
        except (ValueError,UnicodeError): continue
    return calls(rows,cwd)
