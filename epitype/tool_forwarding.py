"""Recognize a single literal awaited tool result, without evaluating JavaScript.

Branches, variables, multiple calls and user-printed lookalikes remain opaque.
"""
import json
import re
from dataclasses import replace


def _literal(source):
    # Quote only object keys outside JSON strings. Substitution inside a command
    # string would change the actual operand and invent evidence.
    pieces=[]; quoted=False; escaped=False; index=0
    while index<len(source):
        char=source[index]
        if quoted:
            pieces.append(char)
            if escaped: escaped=False
            elif char=='\\': escaped=True
            elif char=='"': quoted=False
            index+=1;continue
        if char=='"': quoted=True
        if char.isalpha() or char=='_':
            key=re.match(r'([A-Za-z_]\w*)\s*:',source[index:])
            if key:
                pieces.append(json.dumps(key[1])+':');index+=key.end();continue
        pieces.append(char);index+=1
    try: return json.loads(''.join(pieces))
    except ValueError: return None


def normalize(call):
    if call.name.casefold().rsplit('.',1)[-1] not in ('exec','functions_exec','functions__exec'):
        return call
    source=call.payload.get('input') or call.payload.get('code') or ''
    if not isinstance(source,str): return call
    match=re.fullmatch(r'\s*text\s*\(\s*await\s+tools\.(exec_command|apply_patch)\s*\(([\s\S]+)\)\s*\)\s*;?\s*',source)
    if not match: return call
    argument=_literal(match[2])
    if match[1]=='exec_command' and isinstance(argument,dict) and isinstance(argument.get('cmd'),str):
        name=match[1];payload=argument
    elif match[1]=='apply_patch' and isinstance(argument,str):
        name=match[1];payload={'input':argument}
    else: return call
    response=call.response
    if call.finished>0:
        # Exactly one forwarded native object follows the outer execution header.
        parts=response if isinstance(response,list) else []
        values=[]
        for item in parts:
            if not isinstance(item,dict) or item.get('type') not in ('text','input_text'): continue
            try: value=json.loads(item.get('text',''))
            except ValueError: continue
            if isinstance(value,dict): values.append(value)
        if len(values)!=1: return call
        response=values[0]
        if name=='exec_command' and not all(key in response for key in ('wall_time_seconds','output')):
            return call
    from .tool_evidence import outcome
    return replace(call,name=name,payload=payload,cwd=payload.get('workdir') or call.cwd,
                   response=response,success=outcome(response) if call.finished>0 else None)
