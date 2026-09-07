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
