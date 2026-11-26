"""Check prerequisites against completed facts for the same concrete target."""
from dataclasses import dataclass

KINDS=frozenset(('file.read','file.change','git.status','git.main_update','git.worktrees','directory.delete'))


@dataclass(frozen=True)
class Judgment:
    rule: str
    call_id: str
    target: str
    verdict: str
    reason: str = ''
    actor: str = ''


def rule(item):
    left,sep,right=item.partition('=>')
    if not sep or not left.strip() or not right.strip(): raise ValueError('順序必須是 Y => X')
    left,right=left.strip(),right.strip()
    if not (left.startswith('@') or right.startswith('@')): return None
    if not left.startswith('@') or not right.startswith('@') or left[1:] not in KINDS or right[1:] not in KINDS:
        raise ValueError('證據順序 selector 必須使用已支援的 @action.kind')
    supported={('file.read','file.change'),('git.status','git.main_update'),('git.worktrees','directory.delete')}
    pair=(left[1:],right[1:])
    if pair not in supported: raise ValueError('證據順序尚未支援這組目標關係')
    return pair


@dataclass(frozen=True)
class Index:
    facts: object
    completed: dict
    unknown: dict
    updates: dict
    opaque: dict
    inventories: dict
    created: dict
    uncertain_updates: dict
    ordered: tuple
    positions: dict


def prepare(facts):
    """Index completed facts once for every rule checked against the same facts.

    A long session holds tens of thousands of facts; rebuilding this per card made
    one Stop pass scale with cards x session history and outrun the hook deadline.
    A pending result must retain its causal boundary. `facts` must not change
    while the index is in use."""
    completed={}; unknown={}; updates={}; opaque={}; inventories={}; created={};uncertain_updates={}
    ordered=tuple(facts); positions={}
    for position,fact in enumerate(ordered):
        positions.setdefault(fact.call_id,[]).append(position)
        for action in fact.actions:
            key=(action.kind,action.target,fact.actor)
            if action.kind=='file.create' and fact.success is True and fact.finished>0:
                created[(action.target,fact.actor)]=min(created.get((action.target,fact.actor),fact.finished),fact.finished)
            if action.kind=='evidence.opaque': opaque.setdefault(fact.actor,[]).append(fact.started)
            if action.kind=='git.worktrees' and fact.success is True and fact.finished>0 and fact.inventory:
                inventories.setdefault(key,[]).append((fact.finished,fact.inventory))
            if fact.success is True and fact.finished>0:
                if action.kind!='git.worktrees' or fact.inventory:
                    completed.setdefault(key,[]).append(fact.finished)
                else:
                    unknown[key]=min(unknown.get(key,fact.started),fact.started)
                if action.kind=='git.main_update':
                    updates.setdefault((action.target,fact.actor),[]).append(fact.finished)
            elif fact.success is None or fact.finished==0:
                unknown[key]=min(unknown.get(key,fact.started),fact.started)
                if action.kind=='git.main_update':
                    uncertain_updates.setdefault((action.target,fact.actor),[]).append((fact.started,fact.finished))
    for values in (*completed.values(),*updates.values(),*opaque.values(),*inventories.values()): values.sort()
    return Index(facts,completed,unknown,updates,opaque,inventories,created,uncertain_updates,ordered,positions)


def check(items,facts,active_ids=None, *, covered=True, prepared=None):
    """Judge each @-rule in `items`; `prepared` is `prepare(facts)` reused across calls.

    An index built from other facts is never trusted: it is rebuilt from `facts`."""
    from bisect import bisect_left
    pairs=[(item,pair) for item,pair in ((item,rule(item)) for item in items) if pair is not None]
    if not pairs: return []
    if prepared is None or prepared.facts is not facts:
        prepared=prepare(facts)
    facts=prepared.ordered
    if active_ids is not None:
        # Only this turn's calls are judged; find them by id instead of walking the
        # whole history once per rule. Fact order is kept.
        facts=[facts[position] for position in sorted(
            position for call_id in set(active_ids) for position in prepared.positions.get(call_id,()))]
    completed=prepared.completed; unknown=prepared.unknown; updates=prepared.updates
    opaque=prepared.opaque; inventories=prepared.inventories; created=prepared.created
    uncertain_updates=prepared.uncertain_updates
    judgments=[]
    for item,pair in pairs:
        prerequisite,mutation=pair
        for current in facts:
            if current.success is False or current.finished<0: continue
            for action in current.actions:
                if action.kind!=mutation: continue
                if current.success is not True or not current.finished:
                    judgments.append(Judgment(item,current.call_id,action.target,'unverified','操作結果尚未確認',current.actor))
                    continue
                key=action.repository if prerequisite=='git.worktrees' else action.target
                if not key:
                    judgments.append(Judgment(item,current.call_id,action.target,'unverified','無法確認相關 repo',current.actor))
                    continue
                identity=(prerequisite,key,current.actor)
                candidates=completed.get(identity,())
                index=bisect_left(candidates,current.started)
                end=candidates[index-1] if index else 0
                changes=updates.get((action.target,current.actor),()) if mutation=='git.main_update' else ()
                index=bisect_left(changes,current.started)
                changed=changes[index-1] if index else 0
                uncertainty=not covered or unknown.get(identity,current.started)<current.started
                uncertainty=uncertainty or any(position<current.started for position in opaque.get(current.actor,()))
                if mutation=='git.main_update' and any(start<current.started and (not finish or finish>=end)
                    for start,finish in uncertain_updates.get((action.target,current.actor),())):
                    end=0;uncertainty=True
                if prerequisite=='git.worktrees' and end:
                    eligible=[scope for finished,scope in inventories.get(identity,()) if finished==end]
                    # The inventory must contain a worktree overlapping this concrete
                    # deletion target. A newly created linked worktree is not covered
                    # by an older inventory that omitted it.
                    from pathlib import Path
                    overlap=any(Path(action.target).is_relative_to(path) or Path(path).is_relative_to(action.target)
                                for scopes in eligible for path in scopes)
                    if not overlap: end=0;uncertainty=True
                    if action.checkout and not any(action.checkout in scope for scope in eligible):
                        end=0;uncertainty=True
                if prerequisite=='file.read' and not end:
                    from pathlib import Path
                    own_draft=created.get((action.target,current.actor),current.started)<current.started
                    native_candidate=Path(action.target).name.casefold() in ('memory.md','claude.md','agents.md')
                    # A fresh self-authored draft or host-loaded source is not proof
                    # of an unread external file. Their applicability stays explicit.
                    uncertainty=uncertainty or own_draft or native_candidate
                verdict='allow' if end>changed else ('unverified' if uncertainty else 'block')
                reason='' if verdict=='allow' else ('歷史／目標比較的證據未完整' if verdict=='unverified' else '缺少同一目標在操作開始前成功完成的 '+prerequisite)
                judgments.append(Judgment(item,current.call_id,action.target,verdict,reason,current.actor))
    return judgments
