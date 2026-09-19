# -*- coding: utf-8 -*-
"""這一場到底打開過哪些檔——動手閘每次呼叫順手附記，回合閘結束時來讀。

為什麼不從對話紀錄回推：紀錄尾巴動輒好幾 MB，而回合閘要在三百毫秒內判完，不可能每
回合重新解析一次。動手閘本來就看得見每一次工具呼叫，附記檔名幾乎不花錢（只追加，不
讀不解析）。

只記檔名、不記路徑內容：這個檔的用途是比對「我說我查過的那個檔，這一場有沒有碰過」。
路徑寫法有絕對、相對、正斜線、反斜線好幾種，比對完整路徑會製造誤擋，而誤擋比漏擋貴。
"""

import os
import re
from pathlib import Path

from . import memspec

_PATH_REGEX = re.compile(memspec.TURN_CITED_PATH_PATTERN)


def _session(session_id):
    return "".join(char for char in str(session_id or "") if char.isalnum() or char in "-_")


def path_for(vault, session_id):
    """這一場的附記檔，或 None（沒有場次編號就沒有地方記）。"""
    session = _session(session_id)
    if not session:
        return None
    return (Path(vault) / memspec.FTS_INDEX_DIRECTORY / memspec.OPENED_DIRECTORY
            / (session + ".txt"))


def basenames(payload):
    """一段文字裡出現的檔名（小寫、去重），上限 memspec.TURN_CITED_MAX_PATHS。"""
    found = []
    seen = set()
    for match in _PATH_REGEX.finditer(str(payload or "")[: memspec.OPENED_PAYLOAD_MAX_CHARS]):
        name = match.group(0).replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if not name or name in seen:
            continue
        seen.add(name)
        found.append(name)
        if len(found) >= memspec.TURN_CITED_MAX_PATHS:
            break
    return found


def record(vault, session_id, payload):
    """把這次呼叫碰到的檔名追加進附記檔。失敗就安靜跳過——這是紀錄，不是關卡。

    只追加、不先讀：讀一次再寫一次會讓每個工具呼叫都多付一次解析成本。重複的檔名由
    讀的那一端用集合去掉。"""
    names = basenames(payload)
    if not names:
        return False
    target = path_for(vault, session_id)
    if target is None:
        return False
    try:
        if target.exists() and target.stat().st_size >= memspec.OPENED_MAX_BYTES:
            # 附記檔滿了就停手。這個檔是為了比對，不是帳本；長到要分頁就已經失去意義。
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(names) + "\n")
    except OSError:
        return False
    return True


def names(vault, session_id):
    """這一場附記過的所有檔名。讀不到就回空集合——沒有資料不等於沒有讀過。

    這個區別很重要：回合閘看到空集合時不准擋人，因為「動手閘沒註冊」與「真的沒讀過
    任何檔」在這裡長得一模一樣。"""
    target = path_for(vault, session_id)
    if target is None:
        return frozenset()
    try:
        with target.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - memspec.OPENED_MAX_BYTES))
            body = stream.read()
    except OSError:
        return frozenset()
    return frozenset(line.strip() for line in body.splitlines() if line.strip())
