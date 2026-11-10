"""Review the current rule, without opening historical conversations.

Original messages are optional reference material. A successful review checks
stored content, attribution, scope and retirement links, never action permission.
"""
import hashlib
import re
from datetime import datetime, timezone

from . import rule_provenance as provenance


def validate(fields, card_path=None):
    try:
        document = provenance.parse(fields)
    except (ValueError, TypeError, OSError):
        return provenance.Verification(provenance.INVALID, ['current rule metadata is unreadable'])
    if document is None:
        return provenance.Verification(provenance.UNVERIFIED, ['missing current content review; original quotation is optional'])
    result = provenance.Verification(provenance.INVALID, document=document)
    required = ('version', 'key', 'kind', 'author', 'text', 'scope', 'supersedes')
    missing = [name for name in required if name not in document]
    if missing:
        result.issues.append('missing current rule fields: ' + ', '.join(missing))
        return result
    if document['version'] != provenance.VERSION or document['kind'] not in ('owner-directive', 'ai-derived'):
        result.issues.append('unsupported rule version or kind')
    for name in ('key', 'author', 'text'):
        if not isinstance(document[name], str) or not document[name].strip():
            result.issues.append(name + ' must be a nonempty string')
    if result.issues:
        return result
    if provenance._SECRET.search(document['text']):
        result.issues.append('credential-shaped rule content is not admissible')
    owner = document['kind'] == 'owner-directive'
    if owner and (document['author'] != 'owner' or document['text'] != document.get('quote')):
        result.issues.append('owner-directive must retain exact quotation text and owner attribution')
    if not owner and document['author'].casefold() in ('owner', 'user', 'human', 'owner-explicit'):
        result.issues.append('AI-derived text cannot claim human authorship')
    for field, key in (('decision_key', 'key'), ('text', 'text')):
        if fields.get(field) and fields[field] != document[key]:
            result.issues.append(field + ' differs from current reviewed content')
    if fields.get('owner_quote') and fields['owner_quote'] != document.get('quote'):
        result.issues.append('stored quotations disagree; review this exact discrepancy')
    if not isinstance(document['supersedes'], list) or any(
            not isinstance(value, str) or not value.strip() for value in document['supersedes']):
        result.issues.append('supersedes must list explicit prior card identities')
    scope = document['scope']
    if not isinstance(scope, dict) or any(key not in scope for key in provenance.SCOPE_KEYS):
        result.issues.append('scope must explicitly declare ' + ', '.join(provenance.SCOPE_KEYS))
        return result
    if scope['level'] not in ('global', 'project', 'case', 'action'):
        result.issues.append('invalid scope level')
    if not all(scope[key] is None or isinstance(scope[key], str) for key in ('project', 'case')):
        result.issues.append('scope project and case must be explicit string or null')
    if scope['level'] != 'global' and not scope['project']:
        result.issues.append('non-global scope requires project')
    if scope['level'] == 'global' and (scope['project'] is not None or scope['case'] is not None):
        result.issues.append('global scope cannot conceal a project or case restriction')
    if scope['level'] == 'case' and not scope['case']:
        result.issues.append('case scope requires case')
    for key in ('actions', 'conditions', 'exceptions'):
        if not isinstance(scope[key], list) or any(not isinstance(value, str) or not value.strip() for value in scope[key]):
            result.issues.append('scope ' + key + ' must be an explicit string list')
    if not scope['actions']:
        result.issues.append('scope actions must not be empty')
    if scope['expiry'] != 'unspecified' and not provenance._iso(scope['expiry']):
        result.issues.append('expiry must be ISO or unspecified')
    # A declaration is reviewed with the complete payload below. A lexical match
    # alone still grants no blocking authority, and review never grants an action.
    approval = document.get('approval', {})
    adopted = (isinstance(approval, dict) and approval.get('by') == 'owner'
               and bool(document.get('quote')) and approval.get('quote') == document['quote']
               and re.search(r'同意|核准|批准|\b(?:approve[ds]?|agree[ds]?)\b',
                             provenance._owner_text(document['quote']), re.IGNORECASE)
               and approval.get('text_sha256') == hashlib.sha256(document['text'].encode('utf-8')).hexdigest())
    evidence = document.get('restriction_evidence')
    evidence_bound = False
    if evidence is not None:
        if (not isinstance(evidence, dict) or evidence.get('text') != document['text']
                or evidence.get('author') != document['author']):
            result.issues.append('restriction evidence differs from reviewed interpretation or author')
        else:
            basis = evidence.get('basis')
            evidence_bound = (evidence.get('challengeable') is True and isinstance(basis, list)
                              and bool(basis) and bool(evidence.get('rationale')))
            for item in basis if isinstance(basis, list) else ():
                if (not isinstance(item, dict) or not item.get('quote')
                        or not provenance._iso(item.get('observed_at'))
                        or not provenance._iso(item.get('valid_until'))):
                    evidence_bound = False
                    continue
                limit = datetime.fromisoformat(item['valid_until'].replace('Z', '+00:00'))
                if limit.tzinfo is None:
                    limit = limit.replace(tzinfo=timezone.utc)
                if limit <= datetime.now(timezone.utc):
                    evidence_bound = False
    result.blocking_basis = owner or adopted or evidence_bound
    if provenance._RESTRICTION.search(document['text']) or str(fields.get('on_hit', '')).casefold() == 'block':
        result.restriction_review = 'REVIEWED_BASIS' if result.blocking_basis else 'ADVISORY_ONLY'
    result.status = provenance.INVALID if result.issues else provenance.REVIEWED
    return result
