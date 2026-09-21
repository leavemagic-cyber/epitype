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

PatchFile = namedtuple("PatchFile", "op path additions complete")
"""op: add/update/delete；path: 封套寫的原字串；additions: 連續新增行切成的文字塊；
complete: additions 合起來是否就是寫入後的完整內容（只有 Add File 才可能為真）。"""


def _flush(runs, current):
    if current:
        runs.append("\n".join(current))
        return []
    return current


def _finish(op, path, runs, current, exact):
    current = _flush(runs, current)
    return PatchFile(op, path, tuple(runs), bool(exact and op == "add"))


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
                runs, current, batch = [], [], []
                exact = True
                continue
            header = next(
                ((prefix, kind) for prefix, kind in _HEADERS if stripped.startswith(prefix)),
                None,
            )
            if header is not None:
                if op is not None:
                    batch.append(_finish(op, path, runs, current, exact))
                    if len(found) + len(batch) >= MAX_FILES:
                        # 上限是延遲護欄，不是形狀判定：到這裡就收手，交出已經拆好的。
                        op = path = None
                        closed = True
                        break
                prefix, op = header
                path = stripped[len(prefix):].strip()
                runs, current, exact = [], [], True
                continue
            if op is None:
                continue
            if line.startswith("+"):
                current.append(line[1:])
                continue
            current = _flush(runs, current)
            if line.startswith(("-", " ")) or not stripped:
                # 刪除行與上下文行照 §11 的理由丟掉，但檔案內容仍然是完整可知的。
                if op == "add":
                    # Add File 的內容應該整段都是 `+`；出現別的東西就代表這一段讀不準。
                    exact = False
                continue
            if any(stripped.startswith(prefix) for prefix in _STRUCTURE_PREFIXES):
                continue
            if stripped.startswith("@@"):
                continue
            # 認不得的行：不猜它是內容還是雜訊，只記「這個檔判不準」。
            exact = False
        if closed:
            if op is not None:
                batch.append(_finish(op, path, runs, current, exact))
            found.extend(batch[: MAX_FILES - len(found)])
        if len(found) >= MAX_FILES:
            break
    return tuple(found)


def full_content(entry):
    """Add File 寫進磁碟後的完整內容，判不準就回 None。"""
    if entry.op != "add" or not entry.complete:
        return None
    return "\n".join(entry.additions)


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
