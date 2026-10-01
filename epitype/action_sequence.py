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


def check(items,facts,active_ids=None):
    judgments=[]
    for item in items:
        pair=rule(item)
        if pair is None: continue
        prerequisite,mutation=pair
        for current in facts:
            if active_ids is not None and current.call_id not in active_ids: continue
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
                candidates=[]; unknown=False
                changed=0
                for prior in facts:
                    if prior.call_id==current.call_id or prior.actor!=current.actor: continue
                    for observed in prior.actions:
                        if mutation=='git.main_update' and observed.kind==mutation and observed.target==action.target and prior.success is True and 0<prior.finished<current.started:
                            changed=max(changed,prior.finished)
                        if observed.kind!=prerequisite or observed.target!=key: continue
                        if prior.started<current.started and (prior.success is None or not prior.finished):
                            unknown=True
                        if prior.success is True and 0<prior.finished<current.started:
                            if prerequisite=='git.worktrees' and not prior.inventory:
                                unknown=True
                            else: candidates.append(prior.finished)
                verdict='allow' if any(end>changed for end in candidates) else ('unverified' if unknown else 'block')
                reason='' if verdict=='allow' else ('先前工具結果未驗證' if verdict=='unverified' else '缺少同一目標在操作開始前成功完成的 '+prerequisite)
                judgments.append(Judgment(item,current.call_id,action.target,verdict,reason,current.actor))
    return judgments
