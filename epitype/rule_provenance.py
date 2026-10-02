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
from .capture import owner_reply
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

    @property
    def verified(self):
        return self.status == VERIFIED

    def to_dict(self):
        # Original message bodies can contain unrelated private data.
        return {"status": self.status, "issues": list(self.issues),
                "key": self.document.get("key"), "kind": self.document.get("kind"),
                "author": self.document.get("author"),
                "scope": self.document.get("scope"), "records": list(self.records),
                "semantic_entailment": "REVIEW_REQUIRED", "action_authorization": False}


def parse(fields):
    value = fields.get(FIELD)
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("rule_provenance must be a JSON object")
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


def _original(source, base, cache, budget):
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
        raise OSError("source changed while reading")
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


def _owner_text(text):
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
            return ""
        text = text[end:].lstrip()
    if _SYNTHETIC.search(text):
        return ""
    text = owner_reply(memspec.owner_typed_text(text))
    # Inline quoted/coded material is evidence data, not a new directive.
    text = re.sub(r"```[\s\S]*?```", "", text)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def _human_quote(text, quote):
    text = _owner_text(text)
    if not quote or quote not in text:
        return False
    # A quoted assistant sentence inside a user's reply remains a quotation.
    quoted_spans = [match.span() for match in memspec.GRANT_QUOTED_TEXT_REGEX.finditer(text)]
    start = text.find(quote)
    while start >= 0:
        if not any(left <= start and start + len(quote) <= right for left, right in quoted_spans):
            return True
        start = text.find(quote, start + 1)
    return False


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
            result.issues.append("original source unavailable; next check: restore the exact source locator")
        except (ValueError, UnicodeError, zipfile.BadZipFile, KeyError):
            result.issues.append("original source identity, record boundary or role validation failed")
    if len(original) != len(sources):
        result.status = UNVERIFIED if unavailable else INVALID
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
    result.status = INVALID if result.issues else VERIFIED
    return result


def validate_supersession(before, after, before_path=None, after_path=None, cache=None):
    """A successor cannot silently swallow another key, project or case."""
    old = validate(before, before_path, cache)
    new = validate(after, after_path, cache)
    if not old.verified or not new.verified:
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
