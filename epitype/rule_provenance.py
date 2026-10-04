"""Source-bound rule data shared by admission, lint and generated entrypoints.

VERIFIED means exact quotation, structural identity and explicit reviewed scope.
It does not prove semantic entailment or grant permission to execute an action.
Legacy cards remain readable, but missing evidence is never upgraded to proof.
"""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
import zipfile

from . import memspec
from .capture import owner_replies
from .source_lookup import MAX_BYTES
from .transcript import source_record

FIELD = "rule_provenance"
VERSION = 1
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
INVALID = "INVALID"
SCOPE_KEYS = ("level", "project", "case", "actions", "conditions", "expiry", "exceptions")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SECRET = re.compile(memspec.CAPTURE_REJECT_PATTERN, re.IGNORECASE)
_SYNTHETIC = re.compile(
    r"<(?:hook_prompt|codex_internal_context|heartbeat|task-notification|agent-message|"
    r"cross-session-message|tool_result|function_results|environment_context)(?:\s|/?>)|^#\s+AGENTS\.md instructions|"
    r"^Message Type:\s*(?:NEW_TASK|MESSAGE|FINAL_ANSWER)", re.IGNORECASE | re.MULTILINE)
_AMBIENT_OPEN = re.compile(
    r"<(environment_context|browser_context|in-app-browser-context|system-reminder|pasted_content)"
    r"(?:\s[^<>]*?)?(/?)>", re.IGNORECASE)


class _ReadBudgetExhausted(OSError):
    pass


@dataclass
class _ReadBudget:
    deadline: float
    remaining: int
    origins: dict = field(default_factory=dict)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise _ReadBudgetExhausted("whole validation deadline exhausted")

    def read(self, stream, size, *, line=False):
        self.check()
        if self.remaining <= 0:
            raise _ReadBudgetExhausted("whole validation input read budget exhausted")
        allowed = min(size, self.remaining)
        raw = stream.readline(allowed) if line else stream.read(allowed)
        self.remaining -= len(raw)
        self.check()
        # A truncated line cannot become identity evidence. Header reads also
        # require enough budget to distinguish a clipped prefix from EOF.
        if allowed < size and len(raw) == allowed and not (line and raw.endswith(b"\n")):
            raise _ReadBudgetExhausted("whole validation input read budget exhausted")
        return raw


def _budget_unverified(result):
    result.status = UNVERIFIED
    result.issues.append(
        "whole validation time/input read budget exhausted; next check: rerun exact source "
        "locators with byte offsets or an explicitly larger verification resource budget")
    return result


@dataclass
class Verification:
    status: str
    issues: list[str] = field(default_factory=list)
    document: dict = field(default_factory=dict, repr=False)
    records: list[dict] = field(default_factory=list)
    source_unavailable: bool = False

    @property
    def verified(self):
        return self.status == VERIFIED

    def to_dict(self):
        # Original message bodies can contain unrelated private data.
        return {"status": self.status, "issues": list(self.issues),
                "source_unavailable": self.source_unavailable,
                "key": self.document.get("key"), "kind": self.document.get("kind"),
                "author": self.document.get("author"),
                "scope": self.document.get("scope"), "records": list(self.records),
                "semantic_entailment": "REVIEW_REQUIRED", "action_authorization": False}


def parse(fields):
    value = fields.get(FIELD)
    if isinstance(value, dict):
        result = value
    elif not isinstance(value, str) or not value.strip():
        return None
    else:
        result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("rule_provenance must be a JSON object")
    if "ref" in result:
        # Keep source quotations and dates in the retained source document,
        # while binding the exact English card to that reviewed evidence.
        if set(result) != {"ref", "sha256"} or not isinstance(result["ref"], str):
            raise ValueError("external rule provenance requires only ref and sha256")
        source = Path(result["ref"])
        if not source.is_absolute() or not _HASH.fullmatch(str(result["sha256"])):
            raise ValueError("external rule provenance needs an absolute path and SHA256")
        with source.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != result["sha256"]:
            raise ValueError("external rule provenance size or hash mismatch")
        result = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(result, dict) or "ref" in result:
            raise ValueError("external rule provenance must contain one complete document")
    return result


def serialize(document):
    """JSON scalar for existing flat frontmatter; callers still YAML-quote it."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _read_row(stream, source, budget):
    budget.check()
    stream.seek(0)
    offset = source.get("byte_offset")
    line = source.get("line")
    if offset is not None:
        if not _integer(offset):
            raise ValueError("invalid byte_offset")
        # ZIP decompression is streamed; never extract the whole archive.
        remaining = max(0, offset - 1)
        if not isinstance(stream, zipfile.ZipExtFile):
            stream.seek(remaining)
            remaining = 0
        while remaining:
            chunk = budget.read(stream, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("offset exceeds source")
            remaining -= len(chunk)
        if offset and budget.read(stream, 1) != b"\n":
            raise ValueError("offset is not a record boundary")
    else:
        if not _integer(line, 1):
            raise ValueError("source requires byte_offset or physical line")
        for _ in range(line - 1):
            raw = budget.read(stream, memspec.COMPACT_MAP_MAX_LINE_BYTES + 1, line=True)
            if not raw or len(raw) > memspec.COMPACT_MAP_MAX_LINE_BYTES:
                raise ValueError("source line unavailable or exceeds row budget")
    raw = budget.read(stream, memspec.COMPACT_MAP_MAX_LINE_BYTES + 1, line=True)
    if not raw or len(raw) > memspec.COMPACT_MAP_MAX_LINE_BYTES:
        raise ValueError("source row unavailable or exceeds row budget")
    return raw


def _session(header, item):
    found = item.get("sessionId") or item.get("session_id")
    if found:
        return str(found)
    for raw in header.splitlines():
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") == "session_meta" and isinstance(row.get("payload"), dict):
            return row["payload"].get("id")
        if row.get("sessionId"):
            return str(row["sessionId"])
    return None


def _original(source, base, cache, budget, *, stable_prefix=None):
    budget.check()
    path_value = source.get("archive") or source.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise ValueError("source path is required")
    path = Path(path_value).expanduser()
    path = (base / path).resolve() if not path.is_absolute() else path.resolve()
    archived = bool(source.get("archive"))
    if source.get("archive") and source.get("path"):
        raise ValueError("source cannot have both path and archive")
    if path.suffix.casefold() != (".zip" if archived else ".jsonl"):
        raise ValueError("source must be an original JSONL or ZIP member")
    before = path.stat()
    fingerprint = (str(path), before.st_size, before.st_mtime_ns)
    key = (fingerprint, serialize(source))
    # Never return a positive cached validation without rereading the row. A
    # writer can replace bytes while preserving file length and mtime.
    if archived:
        member = source.get("member")
        if not isinstance(member, str) or not member.casefold().endswith(".jsonl"):
            raise ValueError("ZIP source requires exact JSONL member")
        with zipfile.ZipFile(path) as archive:
            matches = [info for info in archive.infolist() if info.filename == member]
            if len(matches) != 1:
                raise ValueError("ZIP member is missing or ambiguous")
            with archive.open(matches[0]) as stream:
                header = budget.read(stream, 256 * 1024)
                raw = _read_row(stream, source, budget)
    else:
        with path.open("rb") as stream:
            header = budget.read(stream, 256 * 1024)
            raw = _read_row(stream, source, budget)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != fingerprint[1:]:
        if stable_prefix is None or archived:
            raise OSError("source changed while reading")
        _verify_stable_prefix(path, stable_prefix, budget)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != source.get("record_sha256"):
        raise ValueError("record hash does not match original source")
    item = json.loads(raw.decode("utf-8-sig"))
    record = source_record(item)
    if record is None:
        raise ValueError("source is meta, summary, tool output or delegated record")
    message = item.get("message", {})
    if isinstance(message, dict) and isinstance(message.get("content"), list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in message["content"]):
        raise ValueError("tool result user envelope is not a human source")
    role, text = record
    if source.get("role") != role:
        raise ValueError("declared source role does not match original")
    if _session(header, item) != source.get("session_id"):
        raise ValueError("session identity does not match original")
    message_id = source.get("message_id")
    if message_id is not None:
        payload = item.get("payload", {})
        actual = item.get("uuid") or item.get("id") or (
            payload.get("id") if isinstance(payload, dict) else None)
        if str(actual) != str(message_id):
            raise ValueError("message identity does not match original")
    budget.check()
    result = (role, text, digest)
    if cache is not None:
        cache[key] = result
    return result


def _owner_text_parts(text):
    # Ambient host data may precede a real human request. Remove only these
    # supported prefixes before rejecting synthetic directives; the generic
    # owner extractor must not erase a hard hook/delegation envelope first.
    text = text.strip()
    while opening := _AMBIENT_OPEN.match(text):
        if opening.group(2):
            text = text[opening.end():].lstrip()
            continue
        tag = re.escape(opening.group(1))
        tokens = re.finditer(r"<(/?)" + tag + r"(?:\s[^<>]*?)?(/?)>"
                             r"|&lt;/" + tag + r"(?:\s.*?)?&gt;",
                             text[opening.end():], re.IGNORECASE)
        depth = 1
        end = None
        for token in tokens:
            if token.group(1) == "/" or token.group(1) is None:
                depth -= 1
            elif not token.group(2):
                depth += 1
            if depth == 0:
                end = opening.end() + token.end()
                break
        if end is None:
            return []
        text = text[end:].lstrip()
    if _SYNTHETIC.search(text):
        return []
    parts = owner_replies(memspec.owner_typed_text(text))
    # Inline quoted/coded material is evidence data, not a new directive.
    return ["\n".join(line for line in re.sub(r"```[\s\S]*?```", "", part).splitlines()
                      if not line.lstrip().startswith(">")) for part in parts]


def _owner_text(text):
    return "\n\n".join(_owner_text_parts(text))


def _human_quote(text, quote):
    if not quote:
        return False
    for part in _owner_text_parts(text):
        # A quoted assistant sentence inside a user's reply remains a quotation.
        quoted_spans = [match.span() for match in memspec.GRANT_QUOTED_TEXT_REGEX.finditer(part)]
        start = part.find(quote)
        while start >= 0:
            if not any(left <= start and start + len(quote) <= right for left, right in quoted_spans):
                return True
            start = part.find(quote, start + 1)
    return False


# These are admission checks on claims, shared by the existing write and Stop
# gates. They never authorize actions, and unavailable evidence is not a pass.
CLAIM_MARKER = re.compile(r'<!--\s*EPITYPE CLAIM\s+(\{[^\r\n]*\})\s*-->')
_CLAIM_OPEN = re.compile(r'<!--\s*EPITYPE CLAIM\b')
_OWNER_CLAIM = re.compile(
    r'(?:owner|使用者|你)\s*(?:的\s*)?(?:已經?|曾經?|先前)?(?:原話|原句|逐字|裁定|規定|定的界線|'
    r'要求|指示|決定|定規|授權|同意|說過)'
    r'|owner\s+\d{1,4}[-/]\d{1,2}|你(?:說|定|訂)的(?=\s*(?:界線|規則|限制|要求|指示|原話|原句|裁定|[「『“]))'
    r'|(?:owner|user)(?:[’\x27]s)?\s+(?:(?:has|had|already|previously|explicitly)\s+)*'
    r'(?:words|rul(?:ing|ed|es?)|directives?|instructions?|said|decided|requested|requires?|approved|ordered)',
    re.IGNORECASE)
_RESTRICTION = re.compile(
    r'不准|禁止|不得|只能|只准|只用|不放|不交|不能(?:用|做|跑)|'
    r'(?:超過|逾).{0,40}(?:取消|停止|終止)|'
    r'(?:至少|最多|最少|固定)\s*\d|\b(?:must not|may only|can only|only allowed)\b|'
    r'\bnever\s+(?:use|run|submit|stop|cancel)\b|\b(?:at most|at least|maximum|minimum)\s*\d|'
    r'\b(?:cancel|stop|terminate)\b.{0,40}\b(?:after|over|exceeds?)\b', re.IGNORECASE)
_NEW_RULE = re.compile(r'記下規則|新增規則|以後|一律|今後|門檻|訂定|制定|主控自訂|我這邊的判斷|規則[：:]|\b(?:policy|new rule|from now on)\b', re.IGNORECASE)
_DURATION_UNITS = r'分鐘|秒|小時|seconds?|minutes?|hours?'
_THRESHOLD = re.compile(r'(?:超過|逾|\bafter|\bover|\bexceeds?)\s*(\d+(?:\.\d+)?)\s*('+_DURATION_UNITS+r')', re.IGNORECASE)
_DURATIONS = re.compile(r'(\d+(?:\.\d+)?)(?:\s*(?:[–—~～至到-]|to)\s*(\d+(?:\.\d+)?))?\s*('+_DURATION_UNITS+r')', re.IGNORECASE)
_UNITS = {'秒': 1, '分鐘': 60, '小時': 3600, 'second': 1, 'seconds': 1,
          'minute': 60, 'minutes': 60, 'hour': 3600, 'hours': 3600}
_CANCEL = re.compile(r'取消|停止|終止|\b(?:cancel|stop|terminate)\b', re.IGNORECASE)
_OPERATIVE_SUBJECT = re.compile(
    r'前置檢查|建置|工作(?:程序|流程)?|任務|批次|跑批|訓練|回放|重掃|驗收|'
    r'\b(?:preflight|build|job|task|batch|training|replay|worker|workflow)\b', re.IGNORECASE)
_CONTINUATION = re.compile(r'^(?:因此|所以|故(?:而)?|於是|因而|則|就|那麼|現在|以後|今後|依此)')


def _verify_stable_prefix(path, origin, budget):
    """Reread exactly the examined prefix; appending later records is harmless."""
    length = origin['prefix_bytes']
    if path.stat().st_size < length:
        raise OSError('source changed during attribution scan')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        remaining = length
        while remaining:
            chunk = budget.read(stream, min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError('source changed during attribution scan')
            digest.update(chunk)
            remaining -= len(chunk)
    if digest.hexdigest() != origin['prefix_sha256']:
        raise OSError('source changed during attribution scan')


def first_authorship(path, quotes, *, seconds=2.0, max_bytes=None, _budget=None, member=None):
    """Find first literal occurrences in original messages, never summaries.

    Scan the initial snapshot from byte zero, with bounded rows and a deadline,
    rather than a fixed file-size ceiling. Reread its examined complete prefix
    before returning evidence. Appends cannot alter earliest authorship; edits
    to that prefix invalidate it. Unresolved quotes are never positive evidence.
    """
    pending = set(quotes)
    found = {}
    budget = _budget or _ReadBudget(time.monotonic() + seconds,
                                   float('inf') if max_bytes is None else max_bytes)
    try:
        path = Path(path)
        if path.suffix.casefold() not in ('.jsonl', '.zip'):
            raise ValueError('original JSONL required')
        before = path.stat()
        from contextlib import ExitStack
        with ExitStack() as resources:
            if path.suffix.casefold() == '.zip':
                archive = resources.enter_context(zipfile.ZipFile(path))
                if (not isinstance(member, str) or not member.endswith('.jsonl')
                        or sum(info.filename == member for info in archive.infolist()) != 1):
                    raise ValueError('exact unique JSONL archive member required')
                stream = resources.enter_context(archive.open(member))
            else:
                stream = resources.enter_context(path.open('rb'))
            number = 0
            offset = 0
            digest = hashlib.sha256()
            snapshot_size = (archive.getinfo(member).file_size
                             if path.suffix.casefold() == '.zip' else before.st_size)
            while pending and offset < snapshot_size:
                raw = budget.read(stream, min(memspec.COMPACT_MAP_MAX_LINE_BYTES + 1,
                                              snapshot_size - offset), line=True)
                if not raw:
                    break
                number += 1
                if len(raw) > memspec.COMPACT_MAP_MAX_LINE_BYTES:
                    raise ValueError('oversized source row; earliest occurrence unknown')
                # A complete JSON value at EOF is valid without a trailing LF.
                # Only an actually truncated snapshot tail lacks evidence.
                try:
                    row = json.loads(raw.decode('utf-8-sig'))
                except (ValueError, UnicodeError):
                    if not raw.endswith(b'\n') and offset + len(raw) == snapshot_size:
                        break
                    raise
                digest.update(raw)
                record = source_record(row)
                if record:
                    role, body = record
                    for quote in tuple(pending):
                        if quote and quote in body:
                            payload = row.get('payload', {})
                            found[quote] = {
                                'role': role, 'human_authored': role in ('U', 'Q') and _human_quote(body, quote),
                                'line': number, 'byte_offset': offset,
                                'message_id': row.get('uuid') or row.get('id') or (
                                    payload.get('id') if isinstance(payload, dict) else None),
                                'record_sha256': hashlib.sha256(raw).hexdigest(),
                            }
                            pending.remove(quote)
                offset += len(raw)
                budget.check()
        after = path.stat()
        if path.suffix.casefold() == '.zip':
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OSError('source changed during attribution scan')
        else:
            prefix = {'prefix_bytes': offset, 'prefix_sha256': digest.hexdigest()}
            _verify_stable_prefix(path, prefix, budget)
            for origin in found.values():
                origin.update(prefix)
    except (OSError, ValueError, TypeError, UnicodeError, zipfile.BadZipFile) as exc:
        # No positive result survives an invalid or rewritten examined prefix.
        reason = ('read-budget-exhausted' if isinstance(exc, _ReadBudgetExhausted)
                  else 'source-changed' if str(exc) == 'source changed during attribution scan'
                  else 'source-prefix-unreadable-or-invalid')
        return {quote: {'status': UNVERIFIED, 'reason': reason} for quote in quotes}
    return {quote: ({'status': VERIFIED, **found[quote]} if quote in found
                    else {'status': UNVERIFIED}) for quote in quotes}


def _claim_source(source, quote, base, budget, *, human=False):
    """Resolve a line/ID citation through the same original-record verifier."""
    if not isinstance(source, dict) or not (_integer(source.get('line'), 1)
                                          or source.get('message_id')):
        raise ValueError('claim source needs a physical line or message ID')
    path = Path(source.get('path', '')).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if path.suffix.casefold() != '.jsonl':
        raise ValueError('prose claim requires original JSONL; archive cards use the existing card verifier')
    cached = budget.origins.get((str(path), quote))
    if cached is not None:
        fingerprint, origin = cached
        current = path.stat()
        if (current.st_size, current.st_mtime_ns) != fingerprint:
            if origin.get('status') != VERIFIED:
                raise ValueError('source changed during claim validation')
            _verify_stable_prefix(path, origin, budget)
    else:
        origin = first_authorship(path, [quote], _budget=budget)[quote]
    if origin['status'] != VERIFIED:
        raise ValueError('earliest source UNVERIFIED ('+origin.get('reason', 'occurrence-not-found')+'); supply readable original JSONL')
    # Citation must point to the first source, not a later user-shaped echo.
    if ('line' in source and source['line'] != origin['line']
            or 'message_id' in source and source['message_id'] != origin['message_id']):
        raise ValueError('citation differs from earliest original occurrence')
    if any(key in source and source[key] != origin[key] for key in ('record_sha256', 'role', 'byte_offset')):
        raise ValueError('citation hash/role/offset differs from original occurrence')
    locator = {**source, **{key: origin[key] for key in ('line', 'byte_offset', 'record_sha256', 'role')}}
    if not locator.get('session_id'):
        with path.open('rb') as stream:
            stream.seek(origin['byte_offset'])
            row = json.loads(budget.read(stream, memspec.COMPACT_MAP_MAX_LINE_BYTES+1, line=True))
            locator['session_id'] = _session(b'', row)
            if not locator['session_id']:
                stream.seek(0)
                locator['session_id'] = _session(budget.read(stream, 256*1024).decode('utf-8'), row)
    role, text, _digest = _original(locator, base, None, budget, stable_prefix=origin)
    if quote not in text or human and (role not in ('U', 'Q') or not _human_quote(text, quote)):
        raise ValueError('cited text is assistant/quoted material, not owner words')
    if human and not origin['human_authored']:
        raise ValueError('earliest occurrence is assistant/quoted material')
    return text


def _prime_claim_sources(claims, base, budget):
    """Batch each original prefix once per admission; rows are still hash-checked."""
    groups = {}
    for claim in claims:
        if not isinstance(claim, dict):
            raise ValueError('claim metadata must be an object')
        observations = [claim] if claim.get('kind') == 'owner-directive' else claim.get('basis', [])
        if not isinstance(observations, list):
            raise ValueError('restriction observations must be a list')
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError('invalid claim observation')
            source, quote = observation.get('source'), observation.get('quote')
            if not isinstance(source, dict) or not isinstance(quote, str) or not quote:
                continue
            path = Path(source.get('path', '')).expanduser()
            path = (base/path).resolve() if not path.is_absolute() else path.resolve()
            groups.setdefault(path, set()).add(quote)
    for path, quotes in groups.items():
        origins = first_authorship(path, quotes, _budget=budget)
        after = path.stat()
        for quote, origin in origins.items():
            budget.origins[(str(path), quote)] = (after.st_size, after.st_mtime_ns), origin


def _validate_claim(claim, base, budget):
    text = claim.get('text')
    if not isinstance(text, str) or not text.strip() or _SECRET.search(text):
        raise ValueError('claim requires nonempty, credential-free text')
    if claim.get('kind') == 'owner-directive':
        if claim.get('quote') != text:
            raise ValueError('owner directive must preserve exact words; interpretations are AI-derived')
        _claim_source(claim.get('source'), text, base, budget, human=True)
        return
    author = claim.get('author')
    if (claim.get('kind') != 'ai-derived' or not isinstance(author, str) or not author.strip()
            or author.casefold() in ('owner', 'user', 'human', 'owner-explicit')
            or claim.get('challengeable') is not True):
        raise ValueError('custom restriction must identify its AI author and be challengeable')
    subject = claim.get('subject')
    if not isinstance(subject, str) or not subject.strip() or subject not in text:
        raise ValueError('restriction needs an explicit subject shared by text and observations')
    basis = claim.get('basis')
    if not isinstance(basis, list) or not 1 <= len(basis) <= 4:
        raise ValueError('restriction needs 1..4 source-bound current observations')
    evidence = []
    for observation in basis:
        if not isinstance(observation, dict):
            raise ValueError('invalid restriction observation')
        quote = observation.get('quote')
        if not isinstance(quote, str) or subject not in quote:
            raise ValueError('observation must concern the same subject')
        checked = observation.get('observed_at')
        if not _iso(checked):
            raise ValueError('observation date must be explicit ISO')
        when = datetime.fromisoformat(checked.replace('Z', '+00:00'))
        if when.tzinfo is None:
            raise ValueError('observation date needs a timezone')
        from datetime import timezone
        if when > datetime.now(timezone.utc):
            raise ValueError('observation is future-dated')
        valid_until = observation.get('valid_until')
        if not _iso(valid_until):
            raise ValueError('observation needs source-bound validity expiry')
        expiry = datetime.fromisoformat(valid_until.replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry < datetime.now(timezone.utc) or expiry < when:
            raise ValueError('observation is expired; recheck current data')
        # The timestamp must occur in the cited original observation as well.
        body = _claim_source(observation.get('source'), quote, base, budget)
        if checked not in body or valid_until not in body:
            raise ValueError('observation timestamp/expiry is not source-bound')
        evidence.append(quote)
    # Attached evidence must not check the first threshold and license a second
    # contradictory duration or unexplained ratio in the same payload.
    for line in text.splitlines():
        for sentence in _sentences(line):
            budget.check()
            threshold = _THRESHOLD.search(sentence)
            if threshold:
                seconds = float(threshold[1])*_UNITS[threshold[2].casefold()]
                durations = [float(m[2] or m[1])*_UNITS[m[3].casefold()] for quote in evidence
                             for m in _DURATIONS.finditer(quote)]
                if not durations or seconds < max(durations):
                    raise ValueError('new timeout contradicts observed duration or has no comparable data')
                if _CANCEL.search(sentence):
                    condition = claim.get('cancel_condition')
                    if (not isinstance(condition, str) or not condition.strip() or condition not in sentence
                            or condition not in '\n'.join(evidence)):
                        raise ValueError('elapsed time alone cannot justify cancellation; source-bind a concrete fault condition')
                    for key in ('checkpoint', 'redo_cost', 'recovery'):
                        value = claim.get(key)
                        if not isinstance(value, str) or not value.strip() or value not in '\n'.join(evidence):
                            raise ValueError('cancellation needs source-bound '+key+' before stopping work')
            elif (_CANCEL.search(sentence) and re.search(r'超過|逾|\b(?:after|over|exceeds?)\b', sentence, re.IGNORECASE)):
                raise ValueError('timeout formula needs directly comparable duration data; an unexplained ratio is UNVERIFIED')
            elif not isinstance(claim.get('rationale'), str) or not claim['rationale'].strip():
                raise ValueError('non-numeric restriction needs a challengeable rationale')


def _owner_labels(line):
    # Asking the human to decide is not a claim that they already decided.
    return [match for match in _OWNER_CLAIM.finditer(line)
            if not re.search(r'(?:請|等|待|需要|交由|由|不是|並非|不屬於|不當作)\s*$', line[:match.start()])]


def _labelled_quotations(line, inherited=False):
    fragments = [(m.start(), m.end(), m[0][1:-1])
                 for m in memspec.GRANT_QUOTED_TEXT_REGEX.finditer(line)]
    # Backticks format quotations too. Keep their exact contents for lookup.
    fragments.extend((m.start(), m.end(), m['body']) for m in re.finditer(
        r'(?<!`)(?P<ticks>`+)(?!`)(?P<body>[^\r\n]*?)(?<!`)(?P=ticks)(?!`)', line))
    fragments.sort()
    labels = _owner_labels(line)
    selected = []
    for label in labels:
        following = next((m for m in fragments if m[0] >= label.end()), None)
        preceding = [m for m in fragments if m[1] <= label.start()]
        candidate = following or (preceding[-1] if preceding else None)
        if candidate is not None:
            selected.append(candidate)
    if inherited and not labels:
        selected = fragments
    return [m[2] for m in selected]


def _statement_data(body, budget=None):
    """Recognize actual expressions/statements, never a language-name permit."""
    import ast
    if budget is not None and budget.remaining == 0 and len(body) > 16*1024:
        raise _ReadBudgetExhausted('syntax exceeds bounded classification reserve')
    try:
        tree = ast.parse(body)
        return bool(tree.body) and all(not (isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Name)) for node in tree.body)
    except (SyntaxError, ValueError):
        return False


def _code_lines(lines, budget):
    """Each code line retains its role; comments and mixed prose remain visible."""
    import io
    import tokenize
    body = '\n'.join(lines)
    budget.check()
    if budget.remaining == 0 and len(body) > 16*1024:
        raise _ReadBudgetExhausted('syntax exceeds bounded classification reserve')
    try:
        if isinstance(json.loads(body), (dict, list)):
            return [('', 'data') for _ in lines]
    except (ValueError, TypeError):
        pass
    comments, literals = {}, set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(body).readline):
            budget.check()
            if token.type == tokenize.COMMENT:
                comments[token.start[0]-1] = token.string[1:]
            if token.type == tokenize.STRING:
                literals.update(range(token.start[0]-1, token.end[0]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    if _statement_data(body, budget):
        return [(comments[i], 'fenced-prose') if i in comments else ('', 'data')
                for i in range(len(lines))]
    result = []
    for i, line in enumerate(lines):
        budget.check()
        # A bounded subset of JS declarations has the same literal RHS syntax.
        statement = re.sub(r'^\s*(?:const|let|var)\s+(?=[A-Za-z_$])', '', line).rstrip('; ')
        if _statement_data(statement, budget):
            result.append((comments[i], 'fenced-prose') if i in comments else ('', 'data'))
        elif i in literals and not _owner_labels(re.sub(r'([\x22\x27]).*?\1', '', line)):
            # Unsupported syntax with authority-looking string data is undecidable,
            # rather than verified or a reason to block an ordinary code delivery.
            result.append((line, 'uncertain'))
        else:
            result.append((line, 'fenced-prose'))
    return result


def _role_lines(text, budget, card_metadata=False):
    lines = text.splitlines()
    result = [(line, 'prose') for line in lines]
    if card_metadata and lines and lines[0].lstrip('\ufeff') == '---':
        end = next((i for i, line in enumerate(lines[1:], 1) if line == '---'), None)
        if end is not None:
            result[:end+1] = [('', 'metadata')] * (end+1)
    index = 0
    while index < len(lines):
        budget.check()
        if result[index][1] == 'metadata':
            index += 1
            continue
        opening = re.match(r'^\s*(`{3,}|~{3,})(\w*)\s*$', lines[index])
        if opening:
            end = index + 1
            closing = re.compile(r'^\s*'+re.escape(opening[1][0])+r'{'+str(len(opening[1]))+r',}\s*$')
            while end < len(lines) and not closing.match(lines[end]):
                budget.check()
                end += 1
            result[index] = ('', 'data')
            result[index+1:end] = _code_lines(lines[index+1:end], budget)
            if end < len(lines):
                result[end] = ('', 'data')
            index = end
        index += 1
    return result


def _sentences(line):
    """Outer sentence boundaries; punctuation inside quotations is preserved."""
    stack, start, index = [], 0, 0
    pairs = {'「': '」', '『': '』', '“': '”', '‘': '’'}
    while index < len(line):
        char = line[index]
        if char == '`':
            end = index+1
            while end < len(line) and line[end] == '`':
                end += 1
            token = line[index:end]
            if stack and stack[-1] == token:
                stack.pop()
            elif not stack:
                stack.append(token)
            index = end
            continue
        if stack and char == stack[-1]:
            stack.pop()
        elif char in pairs:
            stack.append(pairs[char])
        elif char in ('"', "'") and not stack and (
                index == 0 or not line[index-1].isalnum()):
            stack.append(char)
        elif not stack and (char in '。！？；;!?'
                or char == '.' and (index+1 == len(line) or line[index+1].isspace())):
            yield line[start:index+1]
            start = index+1
        elif not stack and char in '，,':
            following = re.sub(r'^\s*(?:(?:但是|然而|接著|所以|但|現在|今天)\s*)?', '', line[index+1:])
            # Only an independent assertion starts another role. A subordinate
            # "over ten minutes, then cancel" condition stays in one unit.
            if (_OWNER_CLAIM.match(following) or _NEW_RULE.match(following) and _RESTRICTION.search(following)
                    or _OPERATIVE_SUBJECT.match(following) and _operative_cancel(following)):
                yield line[start:index+1]
                start = index+1
        index += 1
    if line[start:].strip():
        yield line[start:]


def _outside_quotes(text):
    outside = memspec.GRANT_QUOTED_TEXT_REGEX.sub('', text)
    return re.sub(r'(`+).*?\1', '', outside)


def _adopts_rule(text):
    outside = _outside_quotes(text)
    return bool(re.search(r'照做|遵守|依此|執行(?:這|此|該)|按(?:這|此|該)|現行裁定|'
                          r'(?:現在|以後|今後|但|已經|已).{0,8}採用', outside)
                or re.search(r'(?:因此|所以|故(?:而)?|於是|因而)', outside)
                and _RESTRICTION.search(outside))


def _request_unit(text):
    outside = _outside_quotes(text)
    return bool(re.match(r'^\s*(?:[^。；;]{0,30}[，,]\s*)?請\s*(?:owner|使用者|你)\s*'
                         r'(?:決定|判斷|確認|裁定)', outside, re.IGNORECASE)
                and not _owner_labels(outside) and not _adopts_rule(text))


def _history_unit(text):
    return bool(not _owner_labels(text) and not _operative_cancel(text)
                and _OPERATIVE_SUBJECT.search(text) and _CANCEL.search(text)
                and re.search(r'(?:已經?|曾經?)(?:被|直接|立即|一律)*\s*(?:取消|停止|終止)|'
                              r'(?:取消|停止|終止)(?:了|完畢|完成)', text))


def _reported_unit(text):
    # Exemption covers the report, never a following present adoption.
    if _adopts_rule(text):
        return False
    labels = _owner_labels(text)
    prefix = text[:labels[0].start()] if labels else text
    mistake = r'(?:誤稱|錯稱|錯誤(?:地)?宣稱|不實主張)'
    # Denying that a claim was mistaken affirms it; it is not an error report.
    if re.search(r'(?:沒有|並未|未曾|不是|並非|不曾|沒)\s*'+mistake, prefix):
        return False
    if re.search(mistake+r'\s*$', prefix):
        return True
    if re.fullmatch(r'\s*(?:錯誤紀錄|歷史反例|待更正)[：:][^「『“]*'+mistake+
                    r'\s*(?:「.*」|『.*』|“.*”)\s*[。.]?\s*', text):
        return True
    cited = re.match(r'^\s*(?:引用資料|歷史反例)[：:]\s*(.+?)\s*[。.]?$', text)
    if cited:
        value = cited[1].rstrip('。.')
        return bool(value and value[0] in '「『“' and value[-1] == {'「': '」', '『': '』', '“': '”'}[value[0]])
    # A quoted specimen being examined is data. The surrounding sentence must
    # describe that examination and contain no adoption or external authority.
    fragments = list(memspec.GRANT_QUOTED_TEXT_REGEX.finditer(text))
    if fragments:
        outside = memspec.GRANT_QUOTED_TEXT_REGEX.sub('', text)
        denied_owner = _OWNER_CLAIM.search(outside) and not _owner_labels(outside)
        if ((re.search(r'測例|反例|範例|引用|應(?:被)?拒絕|應擋|錯誤句', outside)
                or denied_owner and re.search(r'不是|並非|不屬於|不當作', outside))
                and not _owner_labels(outside) and not _NEW_RULE.search(outside)
                and not re.search(r'採用', outside)
                and not _operative_cancel(outside)):
            return True
    return False


@dataclass
class _ClaimUnit:
    number: int
    text: str
    role: str
    section: bool
    paragraph: bool

    @property
    def owner(self):
        return bool(_owner_labels(self.text)) or self.section

    @property
    def restriction(self):
        return bool(_RESTRICTION.search(self.text)) and (
            self.owner or self.paragraph or _NEW_RULE.search(self.text)
            or _operative_cancel(self.text) or _adopts_rule(self.text))

    @property
    def identity(self):
        return self.text, self.role, self.section, self.paragraph


def _logical_lines(text, budget, card_metadata):
    """Reconnect presentation lines before interpreting semantic roles."""
    pending, first, pending_role = [], 0, 'prose'
    separated = False
    originals = text.splitlines()
    for number, (line, role) in enumerate(_role_lines(text, budget, card_metadata)):
        budget.check()
        line = CLAIM_MARKER.sub('', line)
        line = re.sub(r'^\s*(?:>\s*)*(?:[-+*]\s+|\d+[.)]\s+)?', '', line)
        boundary = role in ('data', 'metadata', 'uncertain') or line.startswith(('#', '|'))
        if boundary:
            if pending:
                yield first, ' '.join(pending), pending_role
                pending = []
            yield number, originals[number] if role in ('data', 'metadata') else line, role
            separated = False
            continue
        if not line.strip():
            separated = True
            continue
        continuation = _CONTINUATION.match(line)
        if pending and (pending_role != role or separated and not continuation):
            yield first, ' '.join(pending), pending_role
            yield number, '', 'break'
            pending = []
        if pending and not continuation and re.search(r'[」』”’"`]\s*[。.]?$', pending[-1]):
            # A finished quotation keeps its edit identity when ordinary progress
            # is appended. The next unit still inherits paragraph authority.
            yield first, ' '.join(pending), pending_role
            pending = []
        if pending and (_OWNER_CLAIM.match(line) or _NEW_RULE.match(line)
                        and _RESTRICTION.search(line) or _operative_cancel(line)):
            previous = ' '.join(pending)
            # A complete new assertion is independent even without punctuation;
            # an unfinished report label still belongs to its following specimen.
            if not re.search(r'(?:誤稱|錯稱|錯誤(?:地)?宣稱|不實主張)\s*$', previous):
                yield first, previous, pending_role
                pending = []
        if not pending:
            first, pending_role = number, role
        pending.append(line.strip())
        separated = False
    if pending:
        yield first, ' '.join(pending), pending_role


def _role_units(text, budget, card_metadata=False):
    section = paragraph = False
    result = []
    antecedent = None
    headings = []
    for number, line, role in _logical_lines(text, budget, card_metadata):
        budget.check()
        if role in ('data', 'metadata'):
            # Raw data identity must differ when identical words become prose.
            result.append(_ClaimUnit(number, line, role, False, False))
            paragraph = False
            antecedent = None
            continue
        line = CLAIM_MARKER.sub('', line)
        if not line.strip():
            paragraph = False
            antecedent = None
            continue
        if role == 'uncertain':
            result.append(_ClaimUnit(number, line, role, False, False))
            paragraph = False
            antecedent = None
            continue
        heading = re.match(r'^\s*(#{1,6})\s+', line)
        if heading:
            depth = len(heading[1])
            while headings and headings[-1][0] >= depth:
                headings.pop()
            section = bool(_owner_labels(line)) or bool(headings and headings[-1][1])
            headings.append((depth, section))
            paragraph = False
            antecedent = None
        for sentence in _sentences(line):
            budget.check()
            if (antecedent is not None and antecedent.role in ('reported', 'request', 'history')
                    and _adopts_rule(sentence)):
                sentence = antecedent.text+' '+sentence
            current_role = ('request' if _request_unit(sentence) else
                            'reported' if _reported_unit(sentence) else
                            'history' if _history_unit(sentence) and not _adopts_rule(sentence) else role)
            inline = re.fullmatch(r'\s*(`+)(.*?)\1\s*[。.]?\s*', sentence)
            if inline and _statement_data(inline[2], budget):
                current_role = 'data'
            unit = _ClaimUnit(number, sentence, current_role, section, paragraph)
            result.append(unit)
            antecedent = unit
            if current_role not in ('reported', 'request', 'history', 'data') and unit.owner:
                paragraph = True
    return result


def _operative_cancel(line):
    """A conditional cancellation directive, not a report of an earlier action."""
    if (not _OPERATIVE_SUBJECT.search(line) or not _RESTRICTION.search(line)
            or not _CANCEL.search(line)):
        return False
    directives = re.finditer(
        r'(?:超過|逾).{0,40}(?:就|直接|一律|應|才|要|必須).{0,8}(?P<action>取消|停止|終止)(?!了|完畢|完成)|'
        r'\b(?:cancel|stop|terminate)\b.{0,40}\b(?:after|over|exceeds?)\b', line, re.IGNORECASE)
    for directive in directives:
        if directive['action'] and re.search(
                r'(?:已經?|曾經?)(?:被|直接|立即|一律)*\s*$', line[:directive.start('action')]):
            continue
        return True
    return False


def _has_claim_surface(text):
    # Ordinary technical limits and task scope carry no asserted authority.
    return bool(_OWNER_CLAIM.search(text) or _CLAIM_OPEN.search(text)
                or _NEW_RULE.search(text) and _RESTRICTION.search(text)
                or _operative_cancel(text.replace('\r', ' ').replace('\n', ' '))
                or _RESTRICTION.search(text) and _adopts_rule(text))


def _claim_worker_command():
    import sys
    return [sys.executable, '-B', '-m', 'epitype.rule_provenance', '--claim-worker']


def _unverified_notice(notices, detail):
    notices.append('[owner-attribution] authority UNVERIFIED: '+detail+
                   '; next check: inspect this exact text; continuing work is not verification')


def _active_units(text, budget, previous=None, card_metadata=False):
    from collections import Counter
    units = _role_units(text, budget, card_metadata)
    if previous is None:
        return units
    available = Counter(unit.identity for unit in _role_units(previous, budget, card_metadata))
    added = []
    for unit in units:
        budget.check()
        if available[unit.identity]:
            available[unit.identity] -= 1
        else:
            added.append(unit)
    return added


def _deadline_result(text, *, previous=None, card_metadata=False, notices=None, reaped=False):
    # Use the exact same role rules with a bounded reserve. An unresolved role
    # gets a visible warning; a known claim never turns into verified data.
    notices = [] if notices is None else notices
    fallback = _ReadBudget(time.monotonic()+0.1, 0)
    try:
        marker = any(_CLAIM_OPEN.search(line) for line, role in
                     _role_lines(text, fallback, card_metadata)
                     if role not in ('data', 'metadata'))
        units = _active_units(text, fallback, previous, card_metadata)
        known = any(unit.role in ('prose', 'fenced-prose') and (
            unit.restriction or unit.owner and not (
                unit.text.lstrip().startswith('#') and not _labelled_quotations(unit.text)
                and not _RESTRICTION.search(unit.text))) for unit in units)
        if marker or known:
            return ('[owner-attribution] admission deadline exhausted; authority UNVERIFIED'
                    + (' (worker reaped)' if reaped else ''))
        if any(unit.role == 'uncertain' and _has_claim_surface(unit.text) for unit in units):
            _unverified_notice(notices, 'text role could not be determined within admission budget')
    except _ReadBudgetExhausted:
        _unverified_notice(notices, 'text role classification deadline exhausted')
    return None


def _bounded_claim(text, transcript_path, options, started, notices):
    """The hook's CPU/IO deadline is enforced by an owned, reaped subprocess."""
    import subprocess
    remaining = options['seconds'] - (time.monotonic() - started)
    if remaining <= 0:
        return _deadline_result(text, previous=options['previous'],
            card_metadata=options['card_metadata'], notices=notices)
    packet = {'text': text, 'transcript_path': str(transcript_path) if transcript_path else None,
              **options, 'base': str(options['base']) if options['base'] else None,
              'seconds': remaining}
    try:
        run = subprocess.run(_claim_worker_command(),
                             input=json.dumps(packet, ensure_ascii=False),
                             text=True, encoding='utf-8', capture_output=True,
                             cwd=Path(__file__).resolve().parents[1], timeout=remaining)
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for this exact worker before raising.
        return _deadline_result(text, previous=options['previous'],
            card_metadata=options['card_metadata'], notices=notices, reaped=True)
    if run.returncode:
        raise RuntimeError('owner-attribution worker failed: exit '+str(run.returncode))
    result = json.loads(run.stdout)
    if (not isinstance(result, dict) or 'reason' not in result or result.get('reason') is not None
            and not isinstance(result['reason'], str) or not isinstance(result.get('notices'), list)
            or any(not isinstance(notice, str) for notice in result['notices'])):
        raise RuntimeError('owner-attribution worker returned invalid result')
    notices.extend(result['notices'])
    return result['reason']


def claim_violation(text, transcript_path=None, *, previous=None, require_citations=False,
                    base=None, seconds=2.0, card_metadata=False, hard_deadline=False, notices=None):
    """Return a blocking reason for new authority/restriction claims, else None.

    Owner quotations are checked automatically on every surface. Source markers
    remain supported but are optional for true quotes. Only asserted new rules,
    owner authority and operative cancellation require provenance. Hook callers
    use hard_deadline to contain all CPU work in a bounded worker; ordinary text
    without any claim surface returns immediately without source IO.
    """
    if not isinstance(text, str):
        return None
    notices = [] if notices is None else notices
    started = time.monotonic()
    if not _has_claim_surface(text):
        return None
    if hard_deadline:
        return _bounded_claim(text, transcript_path, dict(previous=previous,
            require_citations=require_citations, base=base, seconds=seconds,
            card_metadata=card_metadata), started, notices)
    budget = _ReadBudget(started+seconds, float('inf'))
    base = Path(base or Path.cwd()).resolve()
    claims = []
    try:
        lines = _role_lines(text, budget, card_metadata)
        prose = '\n'.join(line for line, role in lines if role not in ('data', 'metadata'))
        markers = list(CLAIM_MARKER.finditer(prose))
        if len(markers) != len(list(_CLAIM_OPEN.finditer(prose))):
            raise ValueError('malformed claim metadata')
        claims = [json.loads(match[1]) for match in markers]
        _prime_claim_sources(claims, base, budget)
        for claim in claims:
            _validate_claim(claim, base, budget)
        quotes = []
        for unit in _active_units(text, budget, previous, card_metadata):
            budget.check()
            if unit.role in ('data', 'metadata', 'reported', 'request', 'history'):
                continue
            if unit.role == 'uncertain':
                if _has_claim_surface(unit.text):
                    _unverified_notice(notices, 'text role could not be determined')
                continue
            line = unit.text
            if line.lstrip().startswith('#'):
                if not (_labelled_quotations(line) or _RESTRICTION.search(line)):
                    continue
            owner_claim = unit.owner
            restriction = unit.restriction
            if not owner_claim and not restriction:
                continue
            matching = []
            covered = {}
            for claim in claims:
                if claim['text'] in line:
                    covered[id(claim)] = claim['text']
                elif claim['kind'] == 'ai-derived' and line.strip() in {
                        sentence.strip() for part in claim['text'].splitlines()
                        for sentence in _sentences(part)}:
                    covered[id(claim)] = line
                else:
                    continue
                matching.append(claim)
            labelled = _labelled_quotations(line, unit.section) if owner_claim else []
            if owner_claim:
                matching = [claim for claim in matching if claim['kind'] == 'owner-directive']
            residual = line
            for claim in matching:
                residual = residual.replace(covered[id(claim)], '')
            if (matching and not _RESTRICTION.search(residual)
                    and all(quote in {claim['text'] for claim in matching} for quote in labelled)):
                continue
            if restriction and not owner_claim:
                raise ValueError('new restriction lacks AI attribution, challengeable label and current evidence')
            quoted = labelled
            if not quoted:
                raise ValueError('owner claim has no exact quotation to verify')
            # A genuine owner quotation cannot license an adjacent AI ban.
            unquoted = line
            for quote in quoted:
                unquoted = unquoted.replace(quote, '')
            if _RESTRICTION.search(unquoted):
                raise ValueError('restriction outside owner quotation needs independent AI evidence')
            quotes.extend(quoted)
        budget.check()
        if quotes:
            origins = first_authorship(transcript_path, quotes, _budget=budget)
            for quote in quotes:
                origin = origins[quote]
                if origin['status'] != VERIFIED:
                    raise ValueError('owner attribution UNVERIFIED ('+origin.get('reason', 'occurrence-not-found')+'): original occurrence not found/readable')
                if not origin['human_authored']:
                    raise ValueError(f'owner attribution rejected: first occurrence role={origin["role"]}, L{origin["line"]}')
        budget.check()
    except _ReadBudgetExhausted:
        return _deadline_result(text, previous=previous, card_metadata=card_metadata, notices=notices)
    except (ValueError, TypeError, OSError, KeyError, UnicodeError) as exc:
        return f'[owner-attribution] {exc}'
    return None


def _precedes(context, owner):
    if context.get("session_id") != owner.get("session_id"):
        return False
    for name in ("path", "archive", "member"):
        if context.get(name) != owner.get(name):
            return False
    for name in ("byte_offset", "line"):
        if _integer(context.get(name), 0 if name == "byte_offset" else 1) and _integer(
                owner.get(name), 0 if name == "byte_offset" else 1):
            return context[name] < owner[name]
    return False


def _iso(value):
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return True
    except (TypeError, ValueError):
        return False


def validate(fields, card_path=None, cache=None, *, seconds=5.0, max_bytes=MAX_BYTES):
    """Read original records; no source body appears in diagnostics or to_dict()."""
    budget = _ReadBudget(time.monotonic() + seconds, max_bytes)
    try:
        document = parse(fields)
    except (ValueError, TypeError):
        return Verification(INVALID, ["rule_provenance is not valid JSON"])
    if document is None:
        return Verification(UNVERIFIED, ["missing rule_provenance; legacy data is not source proof"])
    result = Verification(INVALID, document=document)
    required = ("version", "key", "kind", "author", "quote", "text", "sources", "scope",
                "scope_basis", "supersedes")
    missing = [name for name in required if name not in document]
    if missing:
        result.issues.append("missing provenance fields: " + ", ".join(missing))
        return result
    if document["version"] != VERSION or document["kind"] not in ("owner-directive", "ai-derived"):
        result.issues.append("unsupported provenance version or kind")
    for name in ("key", "author", "quote", "text"):
        if not isinstance(document[name], str) or not document[name].strip():
            result.issues.append(name + " must be a nonempty string")
    if result.issues:
        return result
    if _SECRET.search(document["quote"]) or _SECRET.search(document["text"]):
        result.issues.append("credential-shaped rule content is not admissible")
    owner = document["kind"] == "owner-directive"
    if owner and (document["author"] != "owner" or document["text"] != document["quote"]):
        result.issues.append("owner-directive must have owner author and exact quotation text")
    if not owner and document["author"].casefold() in ("owner", "user", "human", "owner-explicit"):
        result.issues.append("AI-derived text cannot claim human authorship")
    if fields.get("decision_key") and fields["decision_key"] != document["key"]:
        result.issues.append("decision_key differs from provenance key")
    if fields.get("owner_quote") and fields["owner_quote"] != document["quote"]:
        result.issues.append("owner_quote differs from original quotation")
    if fields.get("text") and fields["text"] != document["text"]:
        result.issues.append("generated text differs from provenance text")
    # decided_by records who approved a decision; author records who wrote its
    # interpretation. An owner-approved AI record remains AI-authored.
    if not isinstance(document["supersedes"], list) or any(
            not isinstance(target, str) or not target.strip() for target in document["supersedes"]):
        result.issues.append("supersedes must be an explicit list of prior card identities")
    scope = document["scope"]
    if not isinstance(scope, dict) or any(name not in scope for name in SCOPE_KEYS):
        result.issues.append("scope must explicitly declare " + ", ".join(SCOPE_KEYS))
        return result
    if scope["level"] not in ("global", "project", "case", "action"):
        result.issues.append("invalid scope level")
    if scope["level"] != "global" and not scope["project"]:
        result.issues.append("non-global scope requires project")
    if not all(scope[name] is None or isinstance(scope[name], str) for name in ("project", "case")):
        result.issues.append("scope project and case must be explicit string or null")
    if scope["level"] == "global" and (scope["project"] is not None or scope["case"] is not None):
        result.issues.append("global scope cannot conceal a project or case restriction")
    if scope["level"] == "case" and not scope["case"]:
        result.issues.append("case scope requires case")
    for name in ("actions", "conditions", "exceptions"):
        if not isinstance(scope[name], list) or any(not isinstance(value, str) or not value.strip()
                                                  for value in scope[name]):
            result.issues.append("scope " + name + " must be an explicit string list")
    if not scope["actions"]:
        result.issues.append("scope actions must not be empty")
    if scope["expiry"] != "unspecified" and not _iso(scope["expiry"]):
        result.issues.append("expiry must be ISO or unspecified; omission is not permanent consent")
    sources = document["sources"]
    if not isinstance(sources, list) or not sources:
        result.issues.append("sources must contain original record locators")
        return result
    original = []
    base = Path(card_path).resolve().parent if card_path else Path.cwd()
    unavailable = False
    unavailable_issues = 0
    for source in sources:
        if (not isinstance(source, dict) or not _HASH.fullmatch(str(source.get("record_sha256", "")))
                or not isinstance(source.get("session_id"), str) or not source.get("session_id")
                or source.get("role") not in ("U", "Q", "A")):
            result.issues.append("source requires hash, session_id and original role")
            continue
        try:
            record = _original(source, base, cache, budget)
            original.append(record)
            result.records.append({"role": record[0], "record_sha256": record[2]})
        except _ReadBudgetExhausted:
            return _budget_unverified(result)
        except OSError:
            unavailable = True
            unavailable_issues += 1
            result.issues.append("original source unavailable; next check: restore the exact source locator")
        except (ValueError, UnicodeError, zipfile.BadZipFile, KeyError):
            result.issues.append("original source identity, record boundary or role validation failed")
    if len(original) != len(sources):
        # A missing original is a publication notice, while an independently
        # observed identity or shape error remains an actual invalid record.
        result.source_unavailable = unavailable and len(result.issues) == unavailable_issues
        result.status = UNVERIFIED if result.source_unavailable else INVALID
        return result
    owner_sources = []
    for index, (role, text, _hash) in enumerate(original):
        try:
            budget.check()
        except _ReadBudgetExhausted:
            return _budget_unverified(result)
        if role in ("U", "Q") and _human_quote(text, document["quote"]):
            owner_sources.append(index)
    if not owner_sources:
        result.issues.append("quotation does not occur in original human-authored directive text")
    if owner:
        for index in owner_sources:
            source = sources[index]
            origin_path = Path(source.get('path') or source.get('archive')).expanduser()
            if not origin_path.is_absolute():
                origin_path = base / origin_path
            origin = first_authorship(origin_path,
                [document['quote']], _budget=budget, member=source.get('member'))[document['quote']]
            if origin['status'] != VERIFIED:
                unavailable = True
                result.issues.append('earliest authorship UNVERIFIED; next check: restore/read original prefix')
            elif not origin['human_authored']:
                result.issues.append('owner-directive quotation first occurred in assistant/quoted text')
    elif _RESTRICTION.search(document['text']):
        approval = document.get('approval', {})
        index = approval.get('source') if isinstance(approval, dict) else None
        explicitly_approved = (
            isinstance(approval, dict) and approval.get('by') == 'owner'
            and _integer(index) and index in owner_sources
            and approval.get('quote') == document['quote']
            and approval.get('text_sha256') == hashlib.sha256(document['text'].encode('utf-8')).hexdigest()
            and document.get('scope_basis', {}).get('source') == index
            and re.search(r'同意|核准|批准|\b(?:approve[ds]?|agree[ds]?)\b',
                          _owner_text(document['quote']), re.IGNORECASE)
        )
        constraint = document.get('restriction_evidence')
        try:
            if explicitly_approved:
                # Verified owner adoption differs from AI-created restrictions.
                # The interpretation remains AI-authored.
                pass
            elif not isinstance(constraint, dict) or constraint.get('text') != document['text']:
                raise ValueError('AI restriction requires restriction_evidence for the exact interpretation')
            elif constraint.get('author') != document['author']:
                raise ValueError('restriction evidence author differs from interpretation author')
            else:
                _validate_claim(constraint, base, budget)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            result.issues.append(str(exc))
    basis = document["scope_basis"]
    if not isinstance(basis, dict) or not all(name in basis for name in
                                              ("source", "quote", "reviewed_by", "reviewed_at", "rationale")):
        result.issues.append("scope requires source-bound review basis")
    else:
        index = basis["source"]
        if (not _integer(index) or index >= len(original) or not isinstance(basis["quote"], str)
                or not basis["quote"].strip() or original[index][0] not in ("U", "Q")
                or not _human_quote(original[index][1], basis["quote"])):
            result.issues.append("scope basis must quote a verified original human source")
        if (not isinstance(basis["reviewed_by"], str) or not basis["reviewed_by"].strip()
                or not _iso(basis["reviewed_at"]) or not isinstance(basis["rationale"], str)
                or not basis["rationale"].strip()):
            result.issues.append("scope review must identify reviewer, date and rationale")
        # An acknowledgement is not the proposal it approves.
        if re.fullmatch(r"\s*(?:我同意|同意|可以|好|ok|yes|approved?|agree)[。.!！\s]*",
                        document["quote"], re.IGNORECASE):
            context = basis.get("context_source")
            if not _integer(context) or context >= len(original) or original[context][0] != "A":
                result.issues.append("short consent requires original assistant proposal context")
            elif not any(_precedes(sources[context], sources[owner_index]) for owner_index in owner_sources):
                result.issues.append("consent context must precede consent in the same original session")
            elif (not isinstance(basis.get("context_quote"), str) or not basis["context_quote"].strip()
                  or basis["context_quote"] not in original[context][1]):
                result.issues.append("consent review must preserve the exact original proposal quotation")
    try:
        budget.check()
    except _ReadBudgetExhausted:
        return _budget_unverified(result)
    result.status = (UNVERIFIED if unavailable else INVALID) if result.issues else VERIFIED
    return result


def validate_supersession(before, after, before_path=None, after_path=None, cache=None,
                         *, allow_unavailable_sources=False):
    """A successor cannot silently swallow another key, project or case."""
    old = validate(before, before_path, cache)
    new = validate(after, after_path, cache)
    if not all(result.verified or allow_unavailable_sources and result.source_unavailable
               for result in (old, new)):
        return Verification(UNVERIFIED, ["both supersession endpoints require verified provenance"])
    changes = (old.document["key"] != new.document["key"] or any(
        old.document["scope"][name] != new.document["scope"][name]
        for name in ("level", "project", "case")))
    if changes:
        # Explicit reviewed owner changes may broaden, narrow or replace scope.
        # A copied approval may not silently do so.
        basis = new.document.get("supersession_basis", {})
        reviewed = new.document["scope_basis"]
        if not (isinstance(basis, dict) and basis.get("prior_key") == old.document["key"]
                and basis.get("prior_scope") == old.document["scope"]
                and basis.get("source") == reviewed["source"]
                and basis.get("quote") == reviewed["quote"]
                and isinstance(basis.get("reviewed_by"), str) and basis["reviewed_by"].strip()
                and _iso(basis.get("reviewed_at"))
                and isinstance(basis.get("rationale"), str) and basis["rationale"].strip()):
            return Verification(INVALID, ["scope or key change requires explicit source-bound supersession review"])
    if before_path and Path(before_path).name not in new.document["supersedes"] and str(
            before_path) not in new.document["supersedes"]:
        return Verification(INVALID, ["successor must explicitly list prior card identity"])
    if old.source_unavailable or new.source_unavailable:
        return Verification(UNVERIFIED, old.issues + new.issues, document=new.document,
                            records=new.records, source_unavailable=True)
    return new


def render_entry(fields, card_path=None, cache=None):
    """Emit labelled data from the same verified record; never rewrite its text."""
    result = validate(fields, card_path, cache)
    if not result.verified:
        raise ValueError("rule provenance " + result.status + ": " + "; ".join(result.issues))
    return _render_document(result.document)


def _render_document(document):
    """Format already verified data; admission and projection own verification."""
    if document["kind"] == "owner-directive":
        heading = "Owner 原話"
    else:
        heading = "AI 執行整理（作者：" + document["author"] + "；不是 Owner 逐字裁定）"
    scope = serialize(document["scope"])
    lines = [f"[{document['key']}] {heading}", "適用範圍：" + scope]
    if document["kind"] == "ai-derived":
        lines.append("原始 Owner 引句：" + document["quote"])
    lines.append(document["text"])
    return "\n".join(lines)


if __name__ == '__main__':
    import sys
    if sys.argv[1:] != ['--claim-worker']:
        raise SystemExit('only --claim-worker is supported')
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    notices = []
    reason = claim_violation(**json.load(sys.stdin), notices=notices)
    print(json.dumps({'reason': reason, 'notices': notices}, ensure_ascii=False))
