"""Receipt identity for one model context, distinct from an audit session ID."""

import hashlib
from functools import lru_cache
import json
import os
from pathlib import Path
import time

try:
    from .capture import event_host
except ImportError:  # Standalone card-lint consumers share the same audience check.
    from capture import event_host


_CODEX_HEADER_MAX_BYTES = 32768
_CODEX_HEADER_SECONDS = 0.1


@lru_cache(maxsize=32)
def _codex_child_transcript(value):
    """Read only immutable session metadata; never scan a prompt or transcript tail."""
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.name.startswith('rollout-'):
        return False
    started = time.monotonic()
    try:
        with path.open('rb') as stream:
            first = stream.readline(_CODEX_HEADER_MAX_BYTES + 1)
        if len(first) > _CODEX_HEADER_MAX_BYTES or time.monotonic() - started > _CODEX_HEADER_SECONDS:
            return False
        record = json.loads(first)
        if not isinstance(record, dict):
            return False
        payload = record.get('payload')
        if record.get('type') != 'session_meta' or not isinstance(payload, dict):
            return False
        source = payload.get('source')
        parent = payload.get('parent_thread_id')
        return bool((isinstance(parent, str) and parent.strip())
                    or (isinstance(source, dict) and source.get('subagent')))
    except (OSError, ValueError, TypeError):
        return False


def is_subagent_transcript(value):
    """Recognize a child directory or a Codex transcript with native parent metadata."""
    if not isinstance(value, str):
        return False
    normalized = os.path.normpath(value.replace('\\', '/')).replace('\\', '/')
    return ('subagents' in normalized.casefold().split('/')[:-1]
            or _codex_child_transcript(normalized))


def is_subagent(event):
    return (isinstance(event, dict)
            and bool(event.get('agent_id') or event.get('agentId')
                     or is_subagent_transcript(event.get('transcript_path'))))


def identity(event):
    """Return no reusable receipt key when the event lacks a transcript identity.

    A shared parent session ID is not evidence that two agents saw the same text.
    Lexical path normalization avoids filesystem reads; aliases may cause a safe
    repeat, but must not authorize suppression of another context's memory.
    """
    if not isinstance(event, dict):
        return None
    transcript = event.get('transcript_path')
    if not isinstance(transcript, str) or not transcript.strip():
        return None
    path = Path(transcript).expanduser()
    if not path.is_absolute():
        cwd = event.get('cwd')
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return None
        path = Path(cwd) / path
    transcript = os.path.normcase(os.path.normpath(os.fspath(path)))
    session = event.get('session_id', event.get('sessionId', ''))
    session = session if isinstance(session, str) else ''
    agent = event.get('agent_id', event.get('agentId', ''))
    agent = agent if isinstance(agent, str) else ''
    packet = json.dumps([event_host(event), session, transcript, agent], separators=(',', ':'))
    return 'ctx-v1-' + hashlib.sha256(packet.encode('utf-8')).hexdigest()


def notification_identity(event):
    """A finished Claude subagent's findings belong to its receiving parent.

    SubagentStop carries the parent's transcript_path and the child's separate
    agent_transcript_path. The finished child has no next user prompt to consume
    a queued note; ordinary Stop events keep their own context identity.
    """
    if isinstance(event, dict) and event.get('hook_event_name') == 'SubagentStop':
        event = {key: value for key, value in event.items()
                 if key not in ('agent_id', 'agentId')}
    return identity(event)
