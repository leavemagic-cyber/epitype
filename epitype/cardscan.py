# -*- coding: utf-8 -*-
"""走訪一個記憶庫裡的卡片檔——只用 os 與 stat，不碰資料庫。

為什麼要單獨一個模組：兩道閘每一次工具呼叫都要走訪卡片，而走訪這件事本身只需要
`os.scandir`。以前它跟全文檢索住在同一個模組裡，於是每一次呼叫都順便把 sqlite3 載進來
（實測 12.3 ms），而閘門一次都沒用到它。搬出來之後，搜尋那一側照樣從這裡取用，行為
不變，閘門那一側少付一份。

這是整段搬家，不是重寫：判準（隱私過濾、連結處理、排序、跳過索引檔）逐條照舊。重寫一份
會讓兩邊對「哪些檔算卡片」慢慢產生分歧，而分歧的那一天不會有人發現。

庫是隱私邊界：符號連結、Windows junction，以及任何以 `_` 或 `.` 開頭的路徑段，既不進入
也不列出，所以一個連結形狀的項目沒辦法讓庫外的檔案變成可搜。連結從目錄項目本身判斷，
不解析任何路徑——2026-09-04 的回歸：每個項目都 resolve 一次，在忙碌的機器上一次掛鉤要
四秒。
"""

import os
import stat
from pathlib import Path

try:
    from . import memspec
except ImportError:  # 直接當腳本跑
    import memspec

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


def _is_link(entry, info):
    # Windows 的 junction 在 os.DirEntry 眼中不是 symlink；目錄列表帶的 reparse 屬性
    # 兩種都認得出來，而且不必解析任何東西。
    return entry.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def scan_vault(vault):
    """每一張卡：(庫內相對 posix 路徑, 路徑, mtime_ns, 大小, ctime_ns)。

    建立時間一起帶回來，呼叫端就不必為了它再 stat 一次；Windows 的目錄列表本來
    就有這一欄，不多花任何系統呼叫。"""
    found = []
    pending = [(os.fspath(vault), "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith(("_", ".")):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if _is_link(entry, info):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append((entry.path, prefix + name + "/"))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if not name.lower().endswith(".md") or name == memspec.MEMORY_INDEX_FILENAME:
                        continue
                    found.append((prefix + name, Path(entry.path), info.st_mtime_ns,
                                  info.st_size, info.st_ctime_ns))
        except OSError:
            continue
    found.sort(key=lambda item: item[0].casefold())
    return found


def markdown_files(vault):
    return [item[1] for item in scan_vault(vault)]


def card_files(vault):
    """只要路徑的那一種走訪（各種 lint 共用同一道隱私過濾）。"""
    return markdown_files(Path(vault).resolve())


def scan_cards(vault):
    """四欄版，給既有呼叫端；閘門用 scan_vault 的五欄版。"""
    return [item[:4] for item in scan_vault(Path(vault).resolve())]
