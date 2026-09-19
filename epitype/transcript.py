"""Structural provenance for source lookup; roles do not establish authority."""


def message_text(message):
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(block["text"] for block in content if isinstance(block, dict)
                   and block.get("type") in ("text", "input_text", "output_text")
                   and isinstance(block.get("text"), str))


USER = "user"
ASSISTANT = "assistant"
TOOL_RESULT = "tool_result"


def _call_input(payload):
    """Codex 的呼叫參數：`arguments` 是一串 JSON 文字，shell 呼叫另外放在 action 裡。"""
    import json

    raw = payload.get("arguments")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    action = payload.get("action")
    if isinstance(action, dict):
        command = action.get("command")
        if isinstance(command, list):
            return {"command": " ".join(str(part) for part in command)}
        if isinstance(command, str):
            return {"command": command}
    return {}


def turn_parts(item):
    """一列紀錄拆成 (種類, 文字清單, 工具名清單)；不是這三種就回 None。

    兩個宿主的紀錄長得完全不一樣，而回合閘要問的事情是一樣的：這一列是誰說的、說了
    什麼、有沒有叫工具。2026-09-20 Codex 審查抓到回合閘與斷點檔各自只認 Claude 那一種，
    餵 Codex 的格式進去，提問、內容、派工事實全是空的——而空的看起來就像「沒有問題」。

    種類：user（真人提問）、assistant（代理說話或叫工具）、tool_result（工具回傳）。
    """
    if not isinstance(item, dict):
        return None
    kind = item.get("type")

    if kind == "response_item":                      # Codex
        payload = item.get("payload")
        if not isinstance(payload, dict):
            return None
        shape = payload.get("type")
        if shape == "message":
            role = payload.get("role")
            if role not in (USER, ASSISTANT):
                return None
            return (role, [message_text(payload)], [])
        if shape in ("function_call", "custom_tool_call", "local_shell_call"):
            name = payload.get("name") or payload.get("tool_name") or shape
            return (ASSISTANT, [], [(str(name), _call_input(payload))])
        if shape in ("function_call_output", "custom_tool_call_output"):
            return (TOOL_RESULT, [], [])
        return None

    message = item.get("message")                    # Claude
    content = message.get("content") if isinstance(message, dict) else None
    if kind == USER:
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == TOOL_RESULT for block in content
        ):
            return (TOOL_RESULT, [], [])
        if isinstance(content, str):
            return (USER, [content], [])
        if isinstance(content, list):
            return (USER, [str(block.get("text") or "") for block in content
                           if isinstance(block, dict) and block.get("type") == "text"], [])
        return (USER, [], [])
    if kind != ASSISTANT or not isinstance(content, list):
        return None
    texts, tools = [], []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text") or "").strip():
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            payload = block.get("input")
            tools.append((str(block.get("name") or ""),
                          payload if isinstance(payload, dict) else {}))
    return (ASSISTANT, texts, tools)


def source_record(item):
    """(U host-user / Q human queue / A assistant, text), or None for non-source rows.

    Only message records are consumed on Codex, not their duplicate event stream.
    Quoted content remains a quotation; structural provenance grants no action.
    """
    if not isinstance(item, dict) or any(item.get(flag) for flag in
                                       ("isSidechain", "isMeta", "isCompactSummary")):
        return None
    kind = item.get("type")
    if kind == "attachment":
        attachment = item.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
            return None
        origin = attachment.get("origin")
        if not isinstance(origin, dict) or origin.get("kind") != "human":
            return None
        role, text = "Q", attachment.get("prompt")
        if isinstance(text, list):
            text = message_text({"content": text})
    else:
        message = item.get("message")
        if kind == "response_item":
            message = item.get("payload")
            if not isinstance(message, dict) or message.get("type") != "message":
                return None
            kind = message.get("role")
        if kind not in ("user", "assistant"):
            return None
        role, text = ("U" if kind == "user" else "A"), message_text(message)
    return (role, text) if isinstance(text, str) and text.strip() else None
