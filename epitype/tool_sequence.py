"""Literal, per-turn tool ordering; never a cross-hook running counter."""
from . import memspec


def applies(audience, event):
    sub = bool(event.get('agent_id') or event.get('agentId')
               or event.get('hook_event_name') == 'SubagentStop')
    return audience in ('', 'all') or audience == ('sub' if sub else 'root')


def matches(selector, call):
    tool, _separator, fragment = selector.strip().partition('::')
    name, payload = call
    if not memspec.action_guard_tool_matches(tool, name.casefold()):
        return False
    text = '\n'.join(value for key, value in payload.items()
                     if isinstance(value, str) and key not in memspec.ACTION_GUARD_IGNORED_FIELDS)
    return not fragment or fragment in text


def gap(before, pairs, calls):
    for item in before:
        prerequisite, separator, action = item.partition('=>')
        if not separator or not prerequisite.strip() or not action.strip():
            raise ValueError('require_tool_before 必須是 Y => X')
        seen = False
        for call in calls:
            # One shell call is not evidence of ordering within its command text.
            if matches(action, call) and not seen:
                return f'缺少先做的工具：{prerequisite.strip()}（之後做了 {action.strip()}）'
            seen = seen or matches(prerequisite, call)
    for item in pairs:
        opening, separator, closing = item.partition('=>')
        if not separator or not opening.strip() or not closing.strip():
            raise ValueError('pair_tools 必須是 開啟 => 關閉')
        pending = 0
        for call in calls:
            if matches(opening, call):
                pending += 1
            if matches(closing, call) and pending:
                pending -= 1
        if pending:
            return f'工具尚未配對收尾：{opening.strip()} → {closing.strip()}，{pending} 次'
    return None
