"""One source contract for card admission and rule projections.

This validates stored rule data, never whether the owner may perform an action.
Unmigrated cards stay visible as unverified; a new interpretation cannot acquire
authority merely by copying an old approval stamp.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

from . import memspec, rule_provenance

_NORMATIVE = re.compile(
    r"必須|不得|一律|禁止|不可|不准|規則[：:]|原則[：:]|"
    r"\b(?:must|must not|never|required|shall)\b", re.IGNORECASE)
_RULE_TYPES = {"rule", "decision", "scar", "policy"}
_BEHAVIOUR_TYPES = {"feedback", "correction", "habit", "reference", "project"}
_NON_SEMANTIC = {"aliases", "updated", "updated_at", "source_agent"}
_EXECUTION_FIELDS = {"forbidden", "require_when", "require_text", "require_tool_before",
                     "pair_tools", "turn_check", "turn_check_limit", "turn_check_target",
                     "on_hit", "require_on_hit", "guard_tool", "guard_all_of",
                     "guard_requires", "guard_unless", "fence_shell", "fence_langs"}
_EXECUTION_FIELDS.update({'write_check', 'advise_tool', 'advise_all_of', 'advise_text',
                          'guard_when', 'audience', 'require_on_hit'})
_EXECUTION_FIELDS.update(memspec.HOST_NATIVE_FIELDS)
_STOP_DISCOVERY_FIELDS = {"decision_key", "forbidden", "require_when", "turn_check",
                          "fence_shell", "require_tool_before", "pair_tools"}


def is_rule(fields, card_type, text=""):
    if rule_provenance.FIELD in fields or card_type in _RULE_TYPES:
        return True
    if any(key in fields for key in _EXECUTION_FIELDS):
        return True
    if fields.get("owner_quote") or str(fields.get("handoff_rule", "")).casefold() == "true":
        return True
    if (str(fields.get("auto", "")).casefold() == "true"
            and card_type in memspec.EVENT_CARD_TYPES):
        # A captured event is historical evidence, not a published interpretation.
        return False
    return (card_type in {"feedback", "correction", "habit"}
            or card_type in _BEHAVIOUR_TYPES and bool(_NORMATIVE.search(text)))


def _meaning(text, *, include_source=True):
    lines, closing = memspec.split_frontmatter(text)
    body = "\n".join(text.splitlines()[closing + 1:]) if closing is not None else text
    # Aliases and dates are retrieval maintenance; descriptions, scope, author,
    # status and successor metadata affect the interpretation and stay in the key.
    excluded = _NON_SEMANTIC | ({rule_provenance.FIELD} if not include_source else set())
    fields, _problem = memspec.frontmatter_text(text)
    if (fields.get("decided_by") == memspec.OWNER_EXPLICIT_DECIDER
            and fields.get("owner_quote")
            and any(key in fields for key in _STOP_DISCOVERY_FIELDS)):
        # Stop uses these aliases to block questions as already decided. They
        # change execution scope and cannot share the retrieval-only exemption.
        excluded = excluded - {"aliases"}
    # Keep sequence/guard values as well as scalar fields. The shared frontmatter
    # reader hoists metadata before grouping, matching the actual consumers.
    blocks = {}
    key = None
    metadata_child_indent = None
    for line in lines or ():
        match = memspec.TOP_LEVEL_FIELD.match(line)
        if match:
            key = match.group(1)
            metadata_child_indent = None
            blocks.setdefault(key, [])
        elif key == memspec.METADATA_FIELD and line.strip() and not line.lstrip().startswith("#"):
            # This direct metadata date is maintenance, separate from rule meaning.
            # Keep every other metadata value, including deeper `modified` keys.
            indent = len(line) - len(line.lstrip(" "))
            if metadata_child_indent is None:
                metadata_child_indent = indent
            child = memspec.TOP_LEVEL_FIELD.match(line.lstrip(" "))
            if child and indent == metadata_child_indent and child.group(1) == "modified":
                value = memspec.strip_inline_comment(child.group(2)).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if rule_provenance._iso(value):
                    continue
        if key and key not in excluded:
            blocks[key].append(line.rstrip())
    data = {key: value for key, value in blocks.items() if key not in excluded}
    return json.dumps([data, body], ensure_ascii=False, sort_keys=True)


def bind_payload(document, card_text, *, reviewer, reviewed_at=None,
                 rationale="Reviewed the complete interpretation and execution fields."):
    """Freeze the exact reviewed interpretation; this hash is not semantic proof."""
    bound = copy.deepcopy(document)
    bound["payload_binding"] = {
        "sha256": hashlib.sha256(_meaning(card_text, include_source=False).encode()).hexdigest(),
        "reviewed_by": reviewer,
        "reviewed_at": reviewed_at or datetime.now(timezone.utc).isoformat(),
        "rationale": rationale}
    return bound


def payload_issues(document, card_text):
    binding = document.get("payload_binding", {})
    if not isinstance(binding, dict):
        return ["complete interpretation binding must be an object"]
    expected = hashlib.sha256(_meaning(card_text, include_source=False).encode()).hexdigest()
    if binding.get("sha256") != expected:
        return ["complete rule body and execution fields are not bound to this source review"]
    if not (isinstance(binding.get("reviewed_by"), str) and binding["reviewed_by"].strip()
            and rule_provenance._iso(binding.get("reviewed_at"))
            and isinstance(binding.get("rationale"), str) and binding["rationale"].strip()):
        return ["complete interpretation review must identify reviewer, date and rationale"]
    return []


def expired(document, now=None):
    value = document.get("scope", {}).get("expiry", "unspecified")
    if value == "unspecified":
        return False
    limit = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if limit.tzinfo is None:
        limit = limit.replace(tzinfo=timezone.utc)
    return limit <= (now or datetime.now(timezone.utc))


def supersession_issues(fields, card_path, *, card_text=None, seconds=5.0):
    """Verify reciprocal identities and both endpoint payloads without recursion.

    The prospective root can be supplied by a write caller; every other node is
    read from its actual card. A stored predecessor claim is never authority to
    retire an active card that does not point back to this successor.
    """
    try:
        document = rule_provenance.parse(fields)
    except (ValueError, TypeError):
        return ["supersession provenance is unreadable"]
    if fields.get("status") != "superseded" and not (document or {}).get("supersedes"):
        return ["superseded_by requires superseded status"] if fields.get("superseded_by") else []
    root = Path(card_path).resolve()
    deadline = time.monotonic() + seconds
    nodes, edges, pending, examined = {}, set(), [root], set()

    def remaining():
        if time.monotonic() >= deadline:
            raise ValueError("supersession verification budget exhausted; next check: verify this exact chain")

    def identity(value, current):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("supersession requires an explicit card identity")
        path = Path(value.strip())
        if not path.suffix:
            path = path.with_suffix(".md")
        return (current.parent / path).resolve() if not path.is_absolute() else path.resolve()

    def node(path):
        remaining()
        if path not in nodes:
            if path == root:
                data = fields
                text = card_text if card_text is not None else path.read_text(encoding="utf-8-sig")
                # Without the prospective snapshot, an old on-disk body cannot
                # establish the integrity of a newly proposed root.
                actual, problem = memspec.frontmatter_text(text)
                if actual != data:
                    raise ValueError("prospective supersession requires its complete card_text")
            else:
                text = path.read_text(encoding="utf-8-sig")
                data, problem = memspec.frontmatter_text(text)
            if problem:
                raise ValueError("supersession endpoint frontmatter is unreadable")
            doc = rule_provenance.parse(data)
            if doc is None:
                raise ValueError("both supersession endpoints require verified provenance")
            issues = payload_issues(doc, text)
            if issues:
                raise ValueError("; ".join(issues))
            nodes[path] = (data, doc)
        return nodes[path]

    def edge(prior, successor):
        remaining()
        if prior == successor:
            raise ValueError("supersession graph contains a self edge")
        old, _old_doc = node(prior)
        new, new_doc = node(successor)
        if old.get("status") != "superseded":
            raise ValueError("claimed predecessor is still active or not superseded")
        if identity(old.get("superseded_by"), prior) != successor:
            raise ValueError("predecessor must point to the exact claimed successor")
        declared = new_doc.get("supersedes")
        if not isinstance(declared, list) or prior not in {identity(value, successor) for value in declared}:
            raise ValueError("successor must explicitly list the exact prior card identity")
        pair = (prior, successor)
        if pair not in edges:
            # The physical identity has already been established above. Canonical
            # paths let the shared scope validator handle relative/cross-vault
            # names without changing the stored document or inventing an edge.
            canonical = copy.deepcopy(new_doc)
            canonical["supersedes"] = [str(identity(value, successor)) for value in declared]
            reviewed = dict(new, **{rule_provenance.FIELD: rule_provenance.serialize(canonical)})
            result = rule_provenance.validate_supersession(old, reviewed, prior, successor)
            if not result.verified:
                raise ValueError("; ".join(result.issues))
            remaining()
            edges.add(pair)
        pending.extend(path for path in pair if path not in examined)

    try:
        while pending:
            current = pending.pop()
            if current in examined:
                continue
            data, doc = node(current)
            examined.add(current)
            for value in doc.get("supersedes", []):
                edge(identity(value, current), current)
            if data.get("status") == "superseded":
                edge(current, identity(data.get("superseded_by"), current))
            elif data.get("superseded_by"):
                return ["superseded_by requires superseded status"]
            elif data.get("status", "active") != "active":
                return ["supersession endpoint is not active"]
        # Reciprocal metadata forms an undirected pair; only old -> new edges
        # count for cycle detection. Topological removal avoids Python recursion.
        indegree = {path: 0 for path in nodes}
        following = {path: set() for path in nodes}
        for prior, successor in edges:
            following[prior].add(successor)
            indegree[successor] += 1
        ready = [path for path, degree in indegree.items() if not degree]
        removed = 0
        while ready:
            remaining()
            current = ready.pop()
            removed += 1
            for successor in following[current]:
                indegree[successor] -= 1
                if not indegree[successor]:
                    ready.append(successor)
        return [] if removed == len(nodes) else ["supersession graph contains a cycle"]
    except (OSError, UnicodeError):
        return ["supersession endpoint does not exist or is unreadable"]
    except (ValueError, TypeError) as error:
        return [str(error)]


def admission(before, after, card_type, card_path):
    """Issues for a *new or changed interpretation*, not an action approval gate."""
    fields, _problem = memspec.frontmatter_text(after)
    old_fields, _problem = memspec.frontmatter_text(before)
    if (not is_rule(fields, card_type, after)
            and not (before and is_rule(old_fields, old_fields.get("type", card_type), before))):
        return []
    if before and _meaning(before) == _meaning(after):
        return []
    result = rule_provenance.validate(fields, card_path)
    if not result.verified:
        return ["規則來源 " + result.status + "：" + "; ".join(result.issues)]
    issues = payload_issues(result.document, after) + supersession_issues(fields, card_path, card_text=after)
    if issues:
        return ["規則來源 INVALID：" + "; ".join(issues)]
    return []


def inspect_card(fields, path, *, card_text=None):
    """One original-source, complete-payload and successor check for every caller."""
    result = rule_provenance.validate(fields, path)
    if rule_provenance.FIELD not in fields:
        check = legacy_integrity(fields, path, card_text=card_text)
        if check is not None:
            result.legacy_integrity = check.to_dict()
            if not check.allowed:
                result.status = rule_provenance.INVALID
                result.issues.extend(check.issues)
    if result.verified:
        try:
            text = card_text if card_text is not None else Path(path).read_text(encoding="utf-8-sig")
            issues = payload_issues(result.document, text) + supersession_issues(fields, path, card_text=text)
        except (ValueError, OSError) as error:
            issues = ["rule card cannot be verified: " + str(error)]
        if issues:
            result.status = rule_provenance.INVALID
            result.issues.extend(issues)
    return result


def legacy_integrity(fields, path, *, card_text=None):
    """Check frozen old meaning; matching never verifies authorship or scope."""
    if rule_provenance.FIELD in fields:
        return None
    text = card_text if card_text is not None else Path(path).read_text(encoding='utf-8-sig')
    current, _problem = memspec.frontmatter_text(text)
    from . import rule_legacy
    meaning_sha256 = hashlib.sha256(_meaning(text, include_source=False).encode()).hexdigest()
    check = rule_legacy.check(Path(path).resolve(), meaning_sha256)
    if not is_rule(current, current.get('type', ''), text):
        # Previously enrolled rules retain their identity even after a shell
        # removes all fields used for rule classification. A bad configured
        # baseline cannot prove that a path was never enrolled.
        if check.baseline_contains is False or not check.configured:
            return None
    return check


def projection(fields, path, *, legacy_text="", card_text=None):
    """Return source-labelled data; malformed supplied evidence never falls back."""
    payload = Path(path).read_bytes()
    snapshot = payload.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    if card_text is not None and card_text.replace('\r\n', '\n').replace('\r', '\n') != snapshot:
        raise ValueError(Path(path).name + ': rule card changed after its reviewed snapshot')
    current_fields, problem = memspec.frontmatter_text(snapshot)
    if problem:
        raise ValueError(Path(path).name + ': rule card snapshot INVALID: ' + problem)
    # Locators and caller headers are not authority. The provenance envelope
    # itself can change without changing its separately bound rule meaning.
    result = inspect_card(current_fields, path, card_text=snapshot)
    if (rule_provenance.FIELD in current_fields and not result.verified
            or result.status == rule_provenance.INVALID):
        raise ValueError(Path(path).name + ": rule provenance " + result.status + ": "
                         + "; ".join(result.issues))
    document = result.document
    text = document["text"] if result.verified else legacy_text
    if Path(path).read_bytes() != payload:
        raise ValueError(Path(path).name + ': rule card changed during source verification')
    provenance = result.to_dict()
    if hasattr(result, "legacy_integrity"):
        provenance["legacy_integrity"] = result.legacy_integrity
    return {"text": text, "provenance": provenance, "card_path": str(path),
            "source_sha256": hashlib.sha256(payload).hexdigest(),
            "supersedes": document.get("supersedes", []),
            "authorship": document.get("author", "UNVERIFIED"),
            "kind": document.get("kind", "legacy-unverified"),
            "expired": result.verified and expired(document), "document": document}


def render_projection(projected, *, card_path=None):
    """Render a projection without re-reading the record it just verified."""
    if projected["expired"]:
        return ""
    if projected["provenance"]["status"] != rule_provenance.VERIFIED:
        return memspec.RULE_SOURCE_LEGACY_REFERENCE + projected["text"]
    rendered = rule_provenance._render_document(projected["document"])
    if projected["document"]["kind"] == "ai-derived":
        rendered += "\n作者分類：AI 整理：" + projected["document"]["author"]
    rendered += "\n取代關係：" + ("、".join(projected["supersedes"]) or "無")
    card_path = card_path or projected.get("card_path")
    if card_path is not None:
        rendered += "\n來源卡：" + str(card_path)
    return rendered


def main(argv=None):
    parser = argparse.ArgumentParser(description="唯讀規則來源驗證；不授予工具動作權限")
    parser.add_argument("cards", nargs="+", type=Path)
    args = parser.parse_args(argv)
    rows = []
    for path in args.cards:
        fields, _problem = memspec.frontmatter_fields(path)
        result = inspect_card(fields, path)
        result_data = result.to_dict()
        if hasattr(result, "legacy_integrity"):
            result_data["legacy_integrity"] = result.legacy_integrity
        rows.append({"path": str(path), **result_data,
                     "expired": result.verified and expired(result.document)})
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return int(any(row["status"] != rule_provenance.VERIFIED for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
