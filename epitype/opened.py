# -*- coding: utf-8 -*-
"""這一場到底打開過哪些檔——動手閘每次呼叫順手附記，回合閘結束時來讀。

為什麼不從對話紀錄回推：紀錄尾巴動輒好幾 MB，而回合閘要在三百毫秒內判完，不可能每
回合重新解析一次。動手閘本來就看得見每一次工具呼叫，附記檔名幾乎不花錢（只追加，不
讀不解析）。

只記檔名、不記路徑內容：這個檔的用途是比對「我說我查過的那個檔，這一場有沒有碰過」。
路徑寫法有絕對、相對、正斜線、反斜線好幾種，比對完整路徑會製造誤擋，而誤擋比漏擋貴。

證據分強弱（2026-09-22）：原本從工具輸入的**全文**抓檔名，於是
`Write-Output 'ghost.py'` 這種單純提及也被當成讀過——已複驗。現在分兩種：

- strong：來自結構化的路徑欄位（`file_path`／`path`／`file_paths` 那一類），也就是這
  次呼叫**指名**的目標。
- weak：從自由文字（命令列、glob、樣式）掃出來的檔名。兩種都留著，稽核價值沒變，但
  「我讀過 X」只有 strong 算數。

**誠實邊界**：動手閘看得到的是呼叫本身，看不到工具的**結果**。所以 strong 的語意是
「確實對這個目標提出過讀取」，不是「讀取成功」——檔案不存在、權限不足、工具自己失敗，
在這裡全都長得一樣。要確認成功得接 PostToolUse，那是後續、要 owner 重新給信任，這一
批沒做，也不要當成做到了。

舊格式相容：2026-09-22 之前寫下的附記檔每行只有檔名、沒有強弱標記，一律當 weak 讀。
不當 strong 是因為那些行確實無從分辨，而把分不出來的算成強證據就是替自己背書。
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


STRONG_MARK = "strong"
_FIELD_SEPARATOR = "\t"


def _split(line):
    """一列附記拆成 (檔名, 是否為強證據)。舊格式沒有欄位分隔，一律 weak。"""
    name, separator, mark = line.partition(_FIELD_SEPARATOR)
    return name.strip(), bool(separator) and mark.strip() == STRONG_MARK


def record(vault, session_id, payload, strong_payload=None):
    """把這次呼叫碰到的檔名追加進附記檔。失敗就安靜跳過——這是紀錄，不是關卡。

    `strong_payload` 是這次呼叫**指名**的目標（結構化路徑欄位）；`payload` 是自由
    文字。同一個檔名兩邊都出現時只寫強的那一列。

    只追加、不先讀：讀一次再寫一次會讓每個工具呼叫都多付一次解析成本。重複的檔名由
    讀的那一端用集合去掉。分隔用定位字元：檔名裡不可能有它，舊檔裡也沒有，所以舊行
    拆出來就是 weak，不會被誤讀成強證據。"""
    strong = basenames(strong_payload) if strong_payload else []
    weak = [name for name in basenames(payload) if name not in set(strong)]
    names = [name + _FIELD_SEPARATOR + STRONG_MARK for name in strong] + weak
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


def _rows(vault, session_id):
    target = path_for(vault, session_id)
    if target is None:
        return ()
    try:
        with target.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - memspec.OPENED_MAX_BYTES))
            body = stream.read()
    except OSError:
        return ()
    return tuple(_split(line) for line in body.splitlines() if line.strip())


def names(vault, session_id):
    """這一場附記過的所有檔名，強弱不分。稽核與診斷用。

    讀不到就回空集合——沒有資料不等於沒有讀過。這個區別很重要：回合閘看到空集合時
    不准擋人，因為「動手閘沒註冊」與「真的沒讀過任何檔」在這裡長得一模一樣。"""
    return frozenset(name for name, _strong in _rows(vault, session_id) if name)


def strong_names(vault, session_id):
    """這一場**指名**讀過的檔名（結構化路徑欄位來的）。

    回合閘的「宣稱讀過」只認這一份：自由文字裡提到一個檔名，不是打開過它。舊格式的
    附記檔一行都不會出現在這裡，因為它們分不出強弱。"""
    return frozenset(name for name, strong in _rows(vault, session_id) if name and strong)
