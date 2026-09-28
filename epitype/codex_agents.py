"""Codex 主線已完成、卻沒呼叫 close_agent 的直屬子代理。只讀 rollout，不寫任何東西。

規則依據只取官方原始碼 openai/codex rust-v0.156.0（codex-cli 0.156.0），不從紀錄推：

- close_agent 只在 multi-agent V1 註冊，參數是 `target`，值為 spawn_agent 回傳的 agent_id：
  https://github.com/openai/codex/blob/rust-v0.156.0/codex-rs/core/src/tools/spec_plan.rs
  （V1 分支 `registry.add_with_exposure(CloseAgentHandler, …)`；
  `required_child_management_tool_names` 的 V2 清單是 send_message/followup_task/
  interrupt_agent/list_agents，沒有 close_agent），
  https://github.com/openai/codex/blob/rust-v0.156.0/codex-rs/core/src/tools/handlers/multi_agents/close_agent.rs
  （`struct CloseAgentArgs { target: String }`）。
- V1 的 spawn_agent 回傳 `{"agent_id", "nickname"}`，wait_agent 回傳
  `{"status": {agent_id: 狀態}, "timed_out"}`；狀態是 pending_init/running/interrupted/
  shutdown/not_found 或 `{"completed": …}`／`{"errored": …}`：
  https://github.com/openai/codex/blob/rust-v0.156.0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs
  （spawn_agent_output_schema_v1、wait_output_schema_v1、agent_status_output_schema）。
- 「已完成」取 is_final 裡 completed 與 errored 兩種；pending_init/running/interrupted
  不是 final，shutdown/not_found 表示已經不在：
  https://github.com/openai/codex/blob/rust-v0.156.0/codex-rs/core/src/agent/status.rs
- 完成後仍佔資源：同一份 multi_agents_spec.rs 的 close_agent 說明
  "Completed agents remain open and count toward the concurrency limit until closed."

V2（spawn_agent 回傳 task_name）沒有 close_agent 可以叫，官方也沒寫完成的 V2 代理會佔名額，
所以一律不判；resume 前的代理是否還在也查不到官方說法，遇到第二個 session_meta 就重新算。
"""

from __future__ import annotations

import json
from pathlib import Path
import re


FINISHED = frozenset(("completed", "errored"))
LIVE = frozenset(("pending_init", "running", "interrupted"))
GONE = frozenset(("shutdown", "not_found"))
_TOOLS = frozenset(("spawn_agent", "wait_agent", "close_agent", "send_input", "resume_agent"))
_TOOL_BYTES = tuple(name.encode() for name in _TOOLS)
_CALL_ID = re.compile(rb'"call_id"\s*:\s*"([^"]+)"')
# rollout 每列都是 {"timestamp":…,"type":…,"payload":{"type":…}}；紀錄類型在列首幾百位元組內。
_HEAD = 512
_CHECK_EVERY = 2048


def _status(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        return next(iter(value))
    return None


def _object(value):
    """A tool output as a JSON object, or None."""
    if isinstance(value, list):
        value = "".join(item.get("text", "") for item in value
                        if isinstance(item, dict) and isinstance(item.get("text"), str))
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _tool(name):
    if not isinstance(name, str):
        return None
    name = name.rsplit(".", 1)[-1]
    return name if name in _TOOLS else None


def unclosed_finished(path, should_stop=lambda: False):
    """[(agent_id, nickname)] of V1 children that finished and were never closed.

    Returns [] for anything that is not a Codex rollout, and None when
    `should_stop()` turns true before the whole file is read (cannot decide).
    """
    states = {}
    nicknames = {}
    calls = {}
    closing = {}
    with Path(path).open("rb") as stream:
        first = stream.readline()
        if b'"session_meta"' not in first[:_HEAD]:
            return []
        for index, raw in enumerate(stream):
            if index % _CHECK_EVERY == 0 and should_stop():
                return None
            head = raw[:_HEAD]
            if b'"session_meta"' in head:
                states.clear()
                nicknames.clear()
                calls.clear()
                closing.clear()
                continue
            if b'"response_item"' not in head:
                continue
            if b'"function_call"' in head:
                if not any(name in raw[:2048] for name in _TOOL_BYTES):
                    continue
                payload = json.loads(raw).get("payload") or {}
                tool = _tool(payload.get("name"))
                call_id = payload.get("call_id")
                if tool is None or not isinstance(call_id, str):
                    continue
                arguments = _object(payload.get("arguments")) or {}
                target = arguments.get("target")
                target = target if isinstance(target, str) else None
                calls[call_id] = (tool, target)
                if tool in ("send_input", "resume_agent") and target in states:
                    states[target] = "live"
                elif tool == "close_agent" and target in states:
                    closing[call_id] = target
                continue
            if b'"function_call_output"' not in head:
                continue
            match = _CALL_ID.search(head)
            if match is None or match.group(1).decode("utf-8", "replace") not in calls:
                continue
            payload = json.loads(raw).get("payload") or {}
            tool, target = calls.get(payload.get("call_id"), (None, None))
            output = _object(payload.get("output"))
            if tool == "close_agent":
                # 官方回傳是 {"previous_status": …}；錯誤文字代表沒關成，代理仍在。
                if closing.pop(payload.get("call_id"), None) and output and \
                        "previous_status" in output:
                    states[target] = "gone"
                continue
            if output is None:
                continue
            if tool == "spawn_agent":
                agent_id = output.get("agent_id")
                if isinstance(agent_id, str) and agent_id and "task_name" not in output:
                    states[agent_id] = "live"
                    nickname = output.get("nickname")
                    nicknames[agent_id] = nickname if isinstance(nickname, str) else ""
            elif tool == "wait_agent" and isinstance(output.get("status"), dict):
                for agent_id, value in output["status"].items():
                    if agent_id not in states:
                        continue
                    key = _status(value)
                    if key in FINISHED:
                        states[agent_id] = "finished"
                    elif key in GONE:
                        states[agent_id] = "gone"
                    elif key in LIVE:
                        states[agent_id] = "live"
    # 沒等到回傳的 close_agent 當作已關：分不出來的時候不擋人。
    for target in closing.values():
        states[target] = "gone"
    return [(agent_id, nicknames.get(agent_id, "")) for agent_id, state in states.items()
            if state == "finished"]
