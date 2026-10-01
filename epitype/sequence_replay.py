"""Replay the same target relations using ID-correlated historical results.

Current filesystem metadata is never treated as a historical repository snapshot.
Completed worktree inventories establish past checkout ownership. Opaque programs
stay unverified, including outer code-mode JavaScript with no nested receipts.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from . import action_receipts, action_sequence, action_targets, tool_evidence


@dataclass
class Replay:
    calls: tuple
    facts: tuple
    opaque: tuple
    unresolved: tuple = ()


def _within(path,root):
    try: return Path(path).is_relative_to(root)
    except (ValueError,TypeError): return False


def read(path):
    calls=tool_evidence.read(Path(path))
    mappings={}; facts=[]; opaque=[]; unresolved=[]
    for call in calls:
        # Resolve gitdir identity from the recorded inventory rather than today's
        # .git paths, which can have moved or disappeared since the operation.
        actions=action_targets.actions(call.name,call.payload,call.cwd,live=False)
        inventory=action_receipts.inventory(call.response,call.cwd,live=False) if call.success is True and any(a.kind=='git.worktrees' for a in actions) else ()
        if inventory:
            for checkout in inventory: mappings[(call.actor,checkout)]=inventory[0]
        def ownership(path):
            roots=[root for agent,root in mappings if agent==call.actor and _within(path,root)]
            checkout=max(roots,key=len) if roots else ''
            return checkout,mappings.get((call.actor,checkout),'')
        if call.name.casefold()=='write':
            if call.result_kind=='create': actions=[action_targets.Action('file.create',a.target,a.repository) for a in actions]
            elif call.result_kind not in ('update','overwrite'):
                actions=[]; unresolved.append(call.call_id)
        normalized=[]
        for action in actions:
            checkout,main=ownership(action.target)
            if action.kind=='git.status': action=action_targets.Action(action.kind,checkout or action.target,main)
            elif action.kind=='git.worktrees': action=action_targets.Action(action.kind,main or action.target,main)
            elif action.kind=='directory.delete': action=action_targets.Action(action.kind,action.target,main)
            normalized.append(action)
        # live=False deliberately cannot identify a historical main update.
        # A recorded inventory plus a literal invocation supplies that identity.
        name=call.name.casefold().rsplit('.',1)[-1]
        source=call.payload.get('cmd') or call.payload.get('command') or ''
        if isinstance(source,str) and name in ('bash','powershell','exec_command','shell_command','local_shell_call'):
            for command in action_targets._literal_commands(source):
                words=action_targets._words(command,name in ('powershell','exec_command'))
                directory=call.cwd; index=1
                if words and words[0]=='git':
                    while index+1<len(words) and words[index]=='-C':
                        directory=action_targets.canonical(words[index+1],directory,resolve=False);index+=2
                    checkout,main=ownership(action_targets.canonical(directory,resolve=False))
                    if index<len(words) and words[index] in ('merge','cherry-pick','pull'):
                        if checkout and checkout==main and not any(w in ('--help','-h','--abort','--quit','--dry-run') for w in words[index+1:]):
                            normalized.append(action_targets.Action('git.main_update',main,main))
                        elif not main: opaque.append(call.call_id)
            if not action_targets._literal_commands(source): opaque.append(call.call_id)
        elif name in ('exec','functions_exec','functions.exec','functions__exec','grep','glob') or name.endswith('_exec'):
            opaque.append(call.call_id)
        facts.append(action_receipts.Fact(call.call_id,call.started,call.finished,call.success,tuple(normalized),inventory=inventory,actor=call.actor))
    return Replay(tuple(calls),tuple(facts),tuple(opaque),tuple(unresolved))


def judgments(items,replay):
    result=[]
    by_id={(fact.call_id,fact.actor):fact for fact in replay.facts}
    opaque=set(replay.opaque)
    first={}
    for call in replay.calls:
        if call.call_id in opaque: first.setdefault(call.actor,call.started)
    for judgment in action_sequence.check(items,replay.facts):
        current=by_id[(judgment.call_id,judgment.actor)]
        # Absence is not proved if an earlier opaque program could contain the
        # prerequisite. Do not claim its literal candidate actually ran.
        uncertainty=first.get(current.actor,current.started)>=0 and first.get(current.actor,current.started)<current.started
        if judgment.verdict=='block' and uncertainty:
            judgment=action_sequence.Judgment(judgment.rule,judgment.call_id,judgment.target,'unverified','先前包裝／複雜程式的內層執行未驗證',judgment.actor)
        result.append(judgment)
    return result


def report(cards,paths,since=None,until=None,deadline=None):
    from collections import Counter
    import time
    from . import compliance
    counts=Counter();by_card={};read_files=0
    if not cards: return {'cards':0,'read_files':0,'counts':{},'by_card':{},'complete':True}
    for path in paths:
        if deadline is not None and time.monotonic()>=deadline: break
        replay=read(path);read_files+=1
        by_id={(call.call_id,call.actor):call for call in replay.calls}
        for card in cards:
            measured=by_card.setdefault(card.card,Counter())
            for result in judgments(card.fragments,replay):
                call=by_id[(result.call_id,result.actor)]
                if not call.stamp or not compliance._in_window(call.stamp,since,until): continue
                if card.mtime and compliance._epoch_of(call.stamp)<card.mtime: continue
                measured[result.verdict]+=1;counts[result.verdict]+=1
    return {'cards':len(cards),'read_files':read_files,'counts':dict(counts),
            'by_card':{key:dict(value) for key,value in by_card.items()},'complete':read_files==len(paths)}
