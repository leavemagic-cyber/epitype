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


FREEFORM_INPUT_FIELD = "input"


def _call_input(payload):
    """Codex 的呼叫參數：`arguments` 是一串 JSON 文字，shell 呼叫另外放在 action 裡。

    `custom_tool_call` 的 `input` 是自由文字（這台機器上 Codex 的 `exec` 就是這種形狀，
    程式整段塞在裡面）。解析得出字典就用字典，解析不出來就**原文保留**在
    `FREEFORM_INPUT_FIELD` 底下——下游（回合閘、斷點、compact map、harvest）至少看得到
    Codex 實際送出去的是什麼。不去解析那段程式的語法猜欄位值：要做到那件事得先有一個
    直譯器，而猜錯的欄位值比看不到還糟。"""
    import json

    for field in ("arguments", FREEFORM_INPUT_FIELD):
        raw = payload.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if field == FREEFORM_INPUT_FIELD:
            return {FREEFORM_INPUT_FIELD: raw}
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


ASK_TOOL = "AskUserQuestion"
# The owner picked an option in the host's question prompt. The option text was
# written by the AI, so it proves the owner's decision, never the owner's words;
# only an affirmative pick approves rule text (see rule_provenance.validate).
OWNER_SELECTED = "S"


def _unflagged(item):
    return isinstance(item, dict) and not any(
        item.get(flag) for flag in ("isSidechain", "isMeta", "isCompactSummary"))


def ask_tool_uses(item):
    """{tool_use id: {input, sessionId, uuid}} for host question prompts this assistant record raised.

    The prompt's session and record uuid travel with it: an answer binds to the
    prompt only through sessionId and sourceToolAssistantUUID, never by its text.
    """
    if not _unflagged(item) or item.get("type") != "assistant":
        return {}
    message = item.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return {}
    return {block["id"]: {"input": block.get("input"), "sessionId": item.get("sessionId"),
                          "uuid": item.get("uuid")}
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
            and block.get("name") == ASK_TOOL and isinstance(block.get("id"), str) and block["id"]}


def tool_result_ids(item):
    """tool_use ids answered by this Claude user record's tool_result blocks."""
    if not isinstance(item, dict) or item.get("type") != "user":
        return []
    message = item.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block.get("tool_use_id") for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"]


def _label_covers(answer, labels):
    """Distinct label sequences whose ", " join is exactly `answer`; stops after two."""
    covers = []

    def walk(start, used):
        if len(covers) > 1:
            return
        for label in labels:
            if label in used or not answer.startswith(label, start):
                continue
            end = start + len(label)
            if end == len(answer):
                covers.append(used + (label,))
            elif answer.startswith(", ", end):
                walk(end + 2, used + (label,))

    walk(0, ())
    return covers


def _question_options(question):
    """{label: description} for one well-formed question, else None."""
    options = question.get("options")
    if not isinstance(options, list) or not isinstance(question.get("multiSelect", False), bool):
        return None
    found = {}
    for option in options:
        if not isinstance(option, dict):
            return None
        label, description = option.get("label"), option.get("description", "")
        if (not isinstance(label, str) or not label.strip() or label in found
                or not isinstance(description, str)):
            return None
        found[label] = description
    return found


def _ask_answer(item, asked):
    """(S selected / U typed, text) for an answer to a matched AskUserQuestion, else None.

    The answer binds to a prompt the caller saw earlier in the same transcript by
    tool_use id, session and the prompt record's uuid; text that merely looks
    like an answer proves nothing. Option text was written by the AI, so S text
    is only the picked labels and their descriptions, never the question, and an
    answer that names an option it cannot be rebuilt from is no source at all.
    """
    ids = tool_result_ids(item)
    if len(ids) != 1 or not isinstance(ids[0], str) or not isinstance(asked, dict):
        return None
    prompt = asked.get(ids[0])
    if not isinstance(prompt, dict):
        return None
    session, uuid = item.get("sessionId"), item.get("sourceToolAssistantUUID")
    if (not isinstance(session, str) or not session or session != prompt.get("sessionId")
            or not isinstance(uuid, str) or not uuid or uuid != prompt.get("uuid")):
        return None
    if any(isinstance(block, dict) and block.get("is_error") for block in item["message"]["content"]):
        return None
    tool_input = prompt.get("input")
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    result = item.get("toolUseResult")
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(questions, list) or not isinstance(answers, dict):
        return None
    selected, typed = [], []
    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("question"), str):
            return None
        options = _question_options(question)
        if options is None:
            return None
        if question["question"] not in answers:
            continue
        answer = answers[question["question"]]
        if not isinstance(answer, str):
            return None
        if not answer.strip():
            continue
        if question.get("multiSelect", False):
            covers = _label_covers(answer, list(options))
            picks = covers[0] if len(covers) == 1 else None
        else:
            picks = (answer,) if answer in options else None
        if picks:
            selected.append("\n".join(f"{pick}\n{options[pick]}".rstrip() for pick in picks))
        elif any(label in answer for label in options):
            # Names an AI-written option but is not exactly those options: whose words
            # each part is cannot be told, so the record proves nothing.
            return None
        else:
            typed.append(answer)
    if typed and not selected:
        return ("U", "\n".join(typed))
    if selected:
        # A typed answer beside AI-written options cannot be told apart from them
        # inside one record, so the whole record stays owner-selected.
        return (OWNER_SELECTED, "\n\n".join(selected + typed))
    return None


def source_record(item, asked=None):
    """(U host-user / Q human queue / S owner-selected / A assistant, text), or None.

    Only message records are consumed on Codex, not their duplicate event stream.
    Quoted content remains a quotation; structural provenance grants no action.
    A tool result is a source only as the owner's answer to an AskUserQuestion
    whose tool_use id is in `asked` (see ask_tool_uses); every other is None.
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
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "user" and isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            return _ask_answer(item, asked)
        role, text = ("U" if kind == "user" else "A"), message_text(message)
    return (role, text) if isinstance(text, str) and text.strip() else None
