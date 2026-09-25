# -*- coding: utf-8 -*-
"""把工具輸入文字裡的 `*** Begin Patch … *** End Patch` 封套當純文字拆開。

為什麼要有這支：這台機器上的 Codex（Desktop 0.155.0-alpha）沒有 `apply_patch` 工具，
它寫檔的方式是把封套夾在 `exec` 的程式文字裡，而那次呼叫進到動手閘時工具名已經是
`Bash`。真庫 `_GATE_LOG.jsonl` 的 40 列 `write_block` 全部來自 Claude、Codex 零列，
就是這個缺口的樣子：寫檔內容的兩條規則對 Codex 從未生效過。

只當文字處理，不執行任何東西：封套的邊界（`*** Begin Patch`／`*** End Patch`）與每
個檔的表頭都是固定字面，拆得出來。夾著封套的那段程式是什麼語言、怎麼跑，這支一概不
看也不猜——要判斷那個，就得先有一個直譯器，而那正是不能做的事。

只交出新增行：docs/FAILURE_MODES.md §11 反對拿整份 diff 去比對，理由是上下文行與刪除
行會命中這次寫入根本沒新增的樣式，那是誤擋。所以這支把 `+` 開頭的行單獨切出來，刪除
行與上下文行連交都不交出去。

「完整」與否分得很清楚：Add File 的新增行就是整個檔案的內容（`complete=True`），
Update File 不是——補丁裡看不到檔案現在長什麼樣，猜出來的結果會去判一張沒有人寫過的
卡。所以 Update File 一律 `complete=False`，內容契約那一條不判。
"""

from collections import namedtuple

BEGIN_MARKER = "*** Begin Patch"
END_MARKER = "*** End Patch"
_HEADERS = (
    ("*** Add File:", "add"),
    ("*** Update File:", "update"),
    ("*** Delete File:", "delete"),
)
# 封套內部的結構行，不是檔案內容：改名標記與檔尾標記。
_STRUCTURE_PREFIXES = ("*** Move to:", "*** End of File")
MAX_FILES = 40

PatchFile = namedtuple("PatchFile", "op path additions complete hunks", defaults=(None,))
"""op: add/update/delete；path: 封套寫的原字串；additions: 連續新增行切成的文字塊；
complete: additions 合起來是否就是寫入後的完整內容（只有 Add File 才可能為真）；
hunks: Update File 的各段（`Hunk`），讀不準或不是 Update File 就是 None。"""

Hunk = namedtuple("Hunk", "header old new eof")
"""header: `@@` 後面那一行定位字（沒有就是空字串）；old／new: 這一段套用前後的行；
eof: 這一段標了 `*** End of File`，要貼著檔尾找。"""


def _flush(runs, current):
    if current:
        runs.append("\n".join(current))
        return []
    return current


def _finish(op, path, runs, current, exact, hunks=None):
    """hunks: 解析中的各段（dict），None 代表這個檔的段落讀不準。"""
    current = _flush(runs, current)
    frozen = None
    if op == "update" and hunks:
        frozen = tuple(
            Hunk(hunk["header"], tuple(hunk["old"]), tuple(hunk["new"]), hunk["eof"])
            for hunk in hunks
            if hunk["old"] or hunk["new"]
        ) or None
    return PatchFile(op, path, tuple(runs), bool(exact and op == "add"), frozen)


def _new_hunk(header=""):
    return {"header": header, "old": [], "new": [], "eof": False}


def parse(text):
    """文字裡所有封套拆出來的檔案清單；沒有合法封套就回空 tuple。

    壞掉的封套（少了 `*** End Patch`）整個不算：結尾在哪都不知道，硬猜出來的範圍會把
    封套後面的東西一起當成新增行。不算不等於放行——上層看到空清單就是「這次判不了」，
    照既有作風標未知。
    """
    body = str(text or "")
    if BEGIN_MARKER not in body:
        return ()

    found = []
    lines = body.splitlines()
    index = 0
    total = len(lines)
    while index < total:
        if lines[index].strip() != BEGIN_MARKER:
            index += 1
            continue
        closed = False
        op = path = None
        runs, current, exact = [], [], True
        # Update File 的各段：上下文與刪除行只留在這裡，給 `apply_update` 定位用，
        # 不會混進 additions。None＝這個檔的段落讀不準，不准拿去套。
        hunks = []
        # 先收在這裡：封套沒有收尾就整個不算，所以中途拆出來的檔不能直接進 found。
        batch = []
        index += 1
        while index < total:
            line = lines[index]
            index += 1
            stripped = line.strip()
            if stripped == END_MARKER:
                closed = True
                break
            if stripped == BEGIN_MARKER:
                # 封套裡又開一個封套：形狀已經不對，這一段不當數。
                op = path = None
                runs, current, batch, hunks = [], [], [], []
                exact = True
                continue
            header = next(
                ((prefix, kind) for prefix, kind in _HEADERS if stripped.startswith(prefix)),
                None,
            )
            if header is not None:
                if op is not None:
                    batch.append(_finish(op, path, runs, current, exact, hunks))
                    if len(found) + len(batch) >= MAX_FILES:
                        # 上限是延遲護欄，不是形狀判定：到這裡就收手，交出已經拆好的。
                        op = path = None
                        closed = True
                        break
                prefix, op = header
                path = stripped[len(prefix):].strip()
                runs, current, exact, hunks = [], [], True, []
                continue
            if op is None:
                continue
            if line.startswith("+"):
                current.append(line[1:])
                if hunks is not None:
                    if not hunks:
                        hunks.append(_new_hunk())
                    hunks[-1]["new"].append(line[1:])
                continue
            current = _flush(runs, current)
            if line.startswith(("-", " ")) or not stripped:
                # 刪除行與上下文行照 §11 的理由丟掉，但檔案內容仍然是完整可知的。
                if op == "add":
                    # Add File 的內容應該整段都是 `+`；出現別的東西就代表這一段讀不準。
                    exact = False
                elif hunks is not None:
                    if not hunks:
                        hunks.append(_new_hunk())
                    # 全空的一行照 Codex 的寬鬆讀法當成空的上下文行。
                    body = line[1:] if line.startswith(("-", " ")) else ""
                    if not line.startswith("+"):
                        hunks[-1]["old"].append(body)
                    if not line.startswith("-"):
                        hunks[-1]["new"].append(body)
                continue
            if stripped.startswith("*** End of File"):
                if hunks:
                    hunks[-1]["eof"] = True
                continue
            if any(stripped.startswith(prefix) for prefix in _STRUCTURE_PREFIXES):
                # 改名：寫入後的內容落在另一個路徑，原路徑上套出來的結果不是任何人會讀到的檔。
                hunks = None
                continue
            if stripped.startswith("@@"):
                if hunks is not None:
                    hunks.append(_new_hunk(stripped[2:].strip()))
                continue
            # 認不得的行：不猜它是內容還是雜訊，只記「這個檔判不準」。
            exact = False
            hunks = None
        if closed:
            if op is not None:
                batch.append(_finish(op, path, runs, current, exact, hunks))
            found.extend(batch[: MAX_FILES - len(found)])
        if len(found) >= MAX_FILES:
            break
    return tuple(found)


def full_content(entry):
    """Add File 寫進磁碟後的完整內容，判不準就回 None。"""
    if entry.op != "add" or not entry.complete:
        return None
    return "\n".join(entry.additions)


def _seek(lines, pattern, start, at_end):
    """Codex 找段落的順序：逐字、去行尾空白、去兩端空白，找第一個對得上的位置。"""
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    last = len(lines) - len(pattern)
    starts = [last] if at_end and last >= start else []
    starts += list(range(start, last + 1))
    for normalise in (lambda value: value, str.rstrip, str.strip):
        wanted = [normalise(item) for item in pattern]
        for index in starts:
            if [normalise(item) for item in lines[index:index + len(pattern)]] == wanted:
                return index
    return None


def apply_update(entry, text):
    """Update File 套在 `text` 上之後的全文；套不上就回 None。

    只當文字處理，照 Codex apply_patch 的定位規則：`@@` 後的定位字先往下找，再從那裡
    找這一段的舊行；沒有舊行的段落接在檔尾。任何一段找不到就整個不算——那個補丁在
    Codex 那邊一樣套不上，猜一個位置出來會判到一份沒有人寫過的檔。
    """
    if entry.op != "update" or not entry.hunks or not isinstance(text, str):
        return None
    lines = text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    position = 0
    replacements = []
    for hunk in entry.hunks:
        if hunk.header:
            anchor = _seek(lines, [hunk.header], position, False)
            if anchor is None:
                return None
            position = anchor + 1
        if not hunk.old:
            replacements.append((len(lines), 0, list(hunk.new)))
            continue
        old, new = list(hunk.old), list(hunk.new)
        found = _seek(lines, old, position, hunk.eof)
        if found is None and old[-1] == "":
            old = old[:-1]
            new = new[:-1] if new and new[-1] == "" else new
            found = _seek(lines, old, position, hunk.eof)
        if found is None:
            return None
        replacements.append((found, len(old), new))
        position = found + len(old)
    for start, length, new in sorted(replacements, key=lambda item: item[0], reverse=True):
        lines[start:start + length] = new
    return "\n".join(lines) + "\n"


def _selftest():
    checks = []
    sample = "\n".join((
        BEGIN_MARKER,
        "*** Add File: new.md",
        "+alpha",
        "+beta",
        "*** Update File: old.md",
        "@@ def thing():",
        " context line",
        "-removed line",
        "+added line",
        "*** Delete File: gone.md",
        END_MARKER,
    ))
    files = parse(sample)
    checks.append(("三種操作各拆出一個檔", [item.op for item in files] == ["add", "update", "delete"]))
    checks.append(("Add File 的新增行合起來就是完整內容", full_content(files[0]) == "alpha\nbeta"))
    checks.append(("Update File 一律標未知", full_content(files[1]) is None and not files[1].complete))
    checks.append(("刪除行與上下文行不出現在新增行裡",
                   files[1].additions == ("added line",)
                   and all("removed" not in text and "context" not in text
                           for text in files[1].additions)))
    checks.append(("Delete File 沒有新增行", files[2].additions == ()))
    checks.append(("Update File 的段落留著上下文與刪除行，只給定位用",
                   files[1].hunks == (Hunk("def thing():", ("context line", "removed line"),
                                           ("context line", "added line"), False),)
                   and files[0].hunks is None and files[2].hunks is None))
    before = "top\ndef thing():\ncontext line\nremoved line\ntail\n"
    checks.append(("套得上就交出寫入後全文，CRLF 與 BOM 不影響定位",
                   apply_update(files[1], before) == "top\ndef thing():\ncontext line\nadded line\ntail\n"
                   and apply_update(files[1], "﻿" + before.replace("\n", "\r\n"))
                   == "top\ndef thing():\ncontext line\nadded line\ntail\n"))
    checks.append(("套不上、Add File、改名，一律回 None",
                   apply_update(files[1], "nothing here\n") is None
                   and apply_update(files[0], before) is None
                   and parse("\n".join((BEGIN_MARKER, "*** Update File: a.md", "*** Move to: b.md",
                                        "@@", " x", "+y", END_MARKER)))[0].hunks is None))
    appended = parse("\n".join((BEGIN_MARKER, "*** Update File: a.md", "+tail line", END_MARKER)))[0]
    checks.append(("沒有舊行的段落接在檔尾", apply_update(appended, "a\nb\n") == "a\nb\ntail line\n"))
    checks.append(("路徑照原字串留著", [item.path for item in files] == ["new.md", "old.md", "gone.md"]))

    broken = sample.replace(END_MARKER, "")
    checks.append(("少了收尾標記就整個不算", parse(broken) == ()))
    checks.append(("沒有封套就不掃", parse("print('*** not a patch')") == ()))
    checks.append(("空輸入不爆", parse(None) == () and parse("") == ()))

    dirty = "\n".join((BEGIN_MARKER, "*** Add File: odd.md", "+ok", "raw line", END_MARKER))
    entry = parse(dirty)[0]
    checks.append(("Add File 混進認不得的行就標未知",
                   entry.complete is False and full_content(entry) is None
                   and entry.additions == ("ok",)))

    # Codex 實際的形狀：封套夾在一段程式文字的 heredoc 裡，標記各自佔一行。
    wrapped = "await tools.exec({cmd: `apply_patch <<'EOF'\n%s\nEOF`});" % sample
    checks.append(("夾在別的文字裡一樣拆得出來",
                   [item.op for item in parse(wrapped)] == ["add", "update", "delete"]))
    checks.append(("壞封套夾在文字裡也不算",
                   parse("x = `\n%s\n`" % broken) == ()))

    many = [BEGIN_MARKER]
    for number in range(MAX_FILES + 5):
        many += ["*** Add File: f%d.md" % number, "+x"]
    many.append(END_MARKER)
    checks.append(("檔數有上限", len(parse("\n".join(many))) <= MAX_FILES))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(("PASS  " if ok else "FAIL  ") + name)
    print("patch_envelope selftest: %d/%d" % (len(checks) - len(failed), len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys

    sys.exit(_selftest() if "--selftest" in sys.argv[1:] else 0)
