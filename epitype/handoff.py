# -*- coding: utf-8 -*-
"""每個回合收尾，把這一場的斷點覆寫成一個檔。

owner 2026-09-19 指出的問題：階段性進度做完了，卻只存在對話裡——session 一結束、
或者被停掉，那段工作就等於沒發生過。原本每一場只留一行摘要，救不了任何人。

這支不做摘要、不做判斷，只記機器看得到的事實：改過哪些檔、跑過哪些指令、最後一則
使用者訊息是什麼。判斷留給卡片，卡片是給結論用的；進度寫在這裡，兩者不要混。

覆寫而非附加：斷點描述的是「現在停在哪」，不是日誌。日誌另有其人。
"""
import json
import os
import time
from pathlib import Path

from . import memspec

DIRECTORY = "handoff"
STATE_SUFFIX = ".json"
RENDER_SUFFIX = ".md"
TAIL_BYTES = 400_000
MAX_FILES = 40
MAX_COMMANDS = 20
COMMAND_MAX_CHARS = 160
PROMPT_MAX_CHARS = 500
PATH_FIELDS = ("file_path", "notebook_path", "path", "target_file")


def _tail_rows(transcript_path, tail_bytes=TAIL_BYTES):
    try:
        path = Path(str(transcript_path))
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > tail_bytes:
                stream.seek(size - tail_bytes)
                stream.readline()
            lines = stream.read().decode("utf-8", "replace").splitlines()
    except (OSError, ValueError, TypeError):
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _harvest(rows):
    """(改過的檔, 跑過的指令, 最後一則使用者訊息)——只從這一段紀錄看得到的東西。

    兩個宿主的紀錄格式不同，判斷交給 epitype.transcript 那一支共用的正規化：2026-09-20
    Codex 審查抓到這裡自己寫了一份只認 Claude 的解析，餵 Codex 的紀錄進去，檔案、指令、
    提問全是空的——而空的斷點檔看起來就像「這一回合沒做什麼」。
    """
    try:
        from . import transcript as transcript_reader
    except ImportError:  # 直接當腳本跑
        import transcript as transcript_reader

    files, commands, prompt = [], [], ""
    for row in rows:
        if row.get("isSidechain"):
            continue
        parts = transcript_reader.turn_parts(row)
        if parts is None:
            continue
        kind, texts, tools = parts
        if kind == transcript_reader.USER:
            text = " ".join(" ".join(str(piece).split()) for piece in texts if piece)
            if text:
                prompt = text[:PROMPT_MAX_CHARS]
            continue
        for name, payload in tools:
            if not isinstance(payload, dict):
                continue
            for field in PATH_FIELDS:
                value = payload.get(field)
                if isinstance(value, str) and value.strip() and name not in ("Read", "Glob", "Grep"):
                    files.append(value.strip())
            command = payload.get("command")
            if isinstance(command, str) and command.strip():
                commands.append(" ".join(command.split())[:COMMAND_MAX_CHARS])
    return files, commands, prompt


def _merge(previous, found, cap):
    merged = list(previous or [])
    for item in found:
        if item in merged:
            merged.remove(item)
        merged.append(item)
    return merged[-cap:]


def render(state):
    lines = [
        "# 斷點 — %s" % state.get("session", "")[:8],
        "",
        "機器自動覆寫，每個回合一次。**不要手改**：下一回合就沒了。",
        "",
        "- 最後更新：%s" % state.get("updated", ""),
        "- 工作目錄：%s" % (state.get("cwd") or "（未知）"),
        "",
        "## 這一場改過的檔",
        "",
    ]
    files = state.get("files") or []
    lines.extend(["- `%s`" % item for item in files] or ["（沒有寫入任何檔）"])
    lines += ["", "## 這一場跑過的指令（最近 %d 個）" % MAX_COMMANDS, ""]
    commands = state.get("commands") or []
    lines.extend(["- `%s`" % item for item in commands] or ["（沒有跑過指令）"])
    lines += ["", "## 最後一則使用者訊息", "", "> %s" % (state.get("prompt") or "（無）"), ""]
    return "\n".join(lines)


SUBAGENT_DIRECTORY = "subagent_log"
SUBAGENT_TEXT_MAX_CHARS = 20_000


def record_subagent(vault, event, turn_texts=None, now=None):
    """把子代理結束時說的話落檔。回傳寫出去的檔案，沒東西可寫回 None。

    2026-09-19 實測：子代理的對話不在宿主的對話紀錄裡（母場紀錄那個欄位全是否），
    而它自己的工作檔是 0 位元組。也就是它說過的話**哪裡都沒有**——當下擋得住，事後
    查不到，夜間重放看不到，檢討機制對子代理整段是盲的。

    唯一看得見那句話的地方，就是這個時機本身（它收得到子代理的最後一段話）。所以在
    這裡落檔：一天一個檔，一次呼叫一列。這不是日誌癖，是讓檢討有東西可讀。
    """
    text = event.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        return None
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now or time.time()))
    row = {
        "timestamp": stamp,
        "kind": "subagent_say",
        "session_id": str(event.get("session_id") or ""),
        "cwd": str(event.get("cwd") or ""),
        "text": text[:SUBAGENT_TEXT_MAX_CHARS],
    }
    if turn_texts:
        # 2026-09-19 實測必須標清楚：這個時機拿到的紀錄路徑是**母場**的，子代理不在裡面。
        # 所以這幾段是母場當時在說什麼（子代理被派去做什麼的脈絡），不是子代理說的話。
        # 存成子代理的話會污染檢討語料——重放會把母場的句子算成子代理的行為。
        joined = "\n".join(str(item) for item in turn_texts if str(item).strip())
        row["parent_turn_text_count"] = len(turn_texts)
        if joined and joined.strip() != text.strip():
            row["parent_turn_text"] = joined[:SUBAGENT_TEXT_MAX_CHARS]
    folder = Path(vault) / memspec.FTS_INDEX_DIRECTORY / SUBAGENT_DIRECTORY
    target = folder / (time.strftime("%Y%m%d", time.localtime(now or time.time())) + ".jsonl")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        return target
    except OSError:
        return None


def update(vault, session_id, transcript_path, cwd=None, now=None):
    """把這一回合看得到的事實併進這一場的斷點檔。回傳寫出去的路徑，失敗回 None。"""
    session = "".join(char for char in str(session_id or "") if char.isalnum() or char in "-_")
    if not session:
        return None
    folder = Path(vault) / memspec.FTS_INDEX_DIRECTORY / DIRECTORY
    state_path = folder / (session + STATE_SUFFIX)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}

    files, commands, prompt = _harvest(_tail_rows(transcript_path))
    state["session"] = session
    state["cwd"] = cwd or state.get("cwd") or ""
    state["files"] = _merge(state.get("files"), files, MAX_FILES)
    state["commands"] = _merge(state.get("commands"), commands, MAX_COMMANDS)
    if prompt:
        state["prompt"] = prompt
    state["updated"] = time.strftime("%Y-%m-%d %H:%M %z", time.localtime(now or time.time()))

    try:
        folder.mkdir(parents=True, exist_ok=True)
        staging = state_path.with_name("." + state_path.name + ".tmp-%d" % os.getpid())
        staging.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(staging, state_path)
        render_path = folder / (session + RENDER_SUFFIX)
        staging = render_path.with_name("." + render_path.name + ".tmp-%d" % os.getpid())
        staging.write_text(render(state), encoding="utf-8")
        os.replace(staging, render_path)
        return render_path
    except OSError:
        return None
