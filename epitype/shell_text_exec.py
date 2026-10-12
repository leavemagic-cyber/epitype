# -*- coding: utf-8 -*-
r"""找出一條 Bash 指令裡「Bash 會把文字當成指令執行」的地方。只讀字，不執行、不展開。

為什麼要擋：2026-10-12 一條 `python -c "…"` 的雙引號裡夾著 Markdown 的反引號路徑，Bash
做了命令替換；Git Bash 連沒有執行權限的 .md 都當腳本跑，文件裡的反引號又去執行別的檔，
連鎖到約 1,712 個 bash.exe、提交記憶體 44.6 GiB，全機卡死三小時。
解除條件：Claude Code 的 Bash 工具不再經 POSIX shell 執行指令，或 owner 撤銷。

三條規則，任一成立就回報：
  R1 未跳脫的反引號落在 Bash 會做命令替換的位置：未加引號處、雙引號內、$( )／$(( ))／
     ${ } 內、分隔符沒加引號的 here-document 本文。單引號、$'…'、註解、分隔符加了引號的
     here-document 本文裡不算；前面有一個本身沒被跳脫的反斜線也不算（`\\` 是字面反斜線，
     其後的反引號照樣替換）。
  R2 $( ) 或反引號區段的第一個字是文件／資料檔（memspec.SHELL_TEXT_EXEC_DOC_SUFFIXES）。
  R3 簡單指令的指令字是這類檔；或指令字是 bash、sh、source、. 而第一個非選項參數是。

做法是手寫的逐字掃描（線性時間，不用會回溯爆炸的正則）。只照 Bash 的引號、替換與
here-document 規則讀，不猜變數值、別名或函式；讀不懂的結構照字面往下讀，不丟例外。
長度、步數或巢狀深度超過上限時丟 ScanLimitExceeded，由呼叫端放行並記一行缺陷。
"""

from collections import namedtuple
import re

from epitype import memspec

Finding = namedtuple("Finding", "rule offset snippet")


class ScanLimitExceeded(ValueError):
    """指令太長、太深或步數用完：掃描結果不完整，呼叫端放行並說明。"""


# Bash 的 metacharacter：沒加引號時切斷一個字。\r 不在其中（Bash 也不把它當空白）。
_WORD_BREAK = frozenset(" \t\n;&|()<>")
_BLANKS = frozenset(" \t")
_DQ_ESCAPABLE = frozenset("$`\"\\\n")
_HEREDOC_ESCAPABLE = frozenset("$`\\\n")
_BACKTICK_ESCAPABLE = frozenset("$`\\")
# 這些保留字之後仍是指令位置；這些之後則不是（複合指令收尾，後面只剩重導向或分隔符）。
_KEEPS_COMMAND_POSITION = frozenset(("if", "then", "else", "elif", "do", "while", "until",
                                     "!", "{", "time"))
_ENDS_COMPOUND = frozenset(("fi", "done", "}"))
_SHELL_NAMES = frozenset(("bash", "sh"))
_SOURCE_NAMES = frozenset(("source", "."))
# bash 這幾個選項吃掉下一個參數；-c／-s 代表要跑的不是檔案。
_SHELL_OPTIONS_WITH_ARGUMENT = frozenset(("-o", "+o", "-O", "+O", "--rcfile", "--init-file"))
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]*\])?\+?=")
_ARRAY_OPENER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=\Z")


def find_text_exec(command):
    """回傳 [(rule, offset, snippet)]，依發現順序；沒有就是空串列。

    offset 是 command 裡的字元位置；反引號區段裡再找到的東西，offset 記在那個反引號上。"""
    if not isinstance(command, str) or not command:
        return []
    size = len(command.encode("utf-8", errors="replace"))
    if size > memspec.WRITE_GATE_MAX_CONTENT_BYTES:
        raise ScanLimitExceeded(
            f"command is {size} bytes, over the {memspec.WRITE_GATE_MAX_CONTENT_BYTES}-byte scan limit")
    scan = _Scan(command)
    scan.run()
    return list(scan.findings)


def is_document_path(word):
    """這個字（引號已去掉）是不是文件／資料檔的路徑或檔名。"""
    text = word.rstrip("\r").casefold()
    return bool(text) and not text.endswith(("/", "\\")) and text.endswith(
        memspec.SHELL_TEXT_EXEC_DOC_SUFFIXES)


class _Command:
    """一個簡單指令讀到哪裡：下一個字是不是指令字、是不是重導向的目標……"""

    __slots__ = ("command_position", "started", "redirect", "shell", "shell_start", "skip_next",
                 "options_done", "condition", "function_name")

    def __init__(self, command_position=True):
        self.reset(command_position)

    def reset(self, command_position):
        self.command_position = command_position
        # 這個簡單指令已經讀過字、指派或重導向：此後的 ( 在 Bash 是語法錯誤，不是子殼層。
        self.started = False
        self.redirect = False
        self.shell = None
        self.shell_start = 0
        self.skip_next = False
        self.options_done = False
        self.condition = False
        self.function_name = False


class _Scan:
    def __init__(self, text, root=None, base=None):
        self.text = text
        self.root = root or self
        self.base = base
        self.pending = []
        if root is None:
            self.findings = []
            self.seen = set()
            self.steps = 0
            self.budget = memspec.SHELL_TEXT_EXEC_STEP_BUDGET

    # -- bookkeeping -------------------------------------------------------------------

    def _step(self):
        root = self.root
        root.steps += 1
        if root.steps > root.budget:
            raise ScanLimitExceeded(f"scan step budget {root.budget} exhausted")

    @staticmethod
    def _depth(depth):
        if depth > memspec.SHELL_TEXT_EXEC_MAX_DEPTH:
            raise ScanLimitExceeded(
                f"nesting deeper than {memspec.SHELL_TEXT_EXEC_MAX_DEPTH} levels")

    def _add(self, rule, start, stop):
        limit = memspec.SHELL_TEXT_EXEC_SNIPPET_MAX_CHARS
        snippet = " ".join(self.text[start:min(stop, start + 4 * limit)].split())
        if len(snippet) > limit:
            snippet = snippet[:limit - 1] + "…"
        offset = start if self.base is None else self.base
        root = self.root
        if (rule, offset) in root.seen:
            return
        root.seen.add((rule, offset))
        root.findings.append(Finding(rule, offset, snippet))

    def run(self):
        self._list(0, len(self.text), 0, "top")

    # -- command lists -----------------------------------------------------------------

    def _list(self, pos, end, depth, kind, opener=None):
        """讀一串指令；kind 是 top／cmdsub／procsub／subshell／array／backtick。

        cmdsub、procsub、subshell、array 讀到對應的 `)` 就回傳它後面的位置。"""
        self._depth(depth)
        text = self.text
        closes = kind in ("cmdsub", "procsub", "subshell", "array")
        first_word = kind in ("cmdsub", "backtick")
        array = kind == "array"
        cases = []
        state = _Command(not array)

        def fresh():
            state.reset(not array and not (cases and cases[-1] in ("subject", "pattern")))

        while pos < end:
            self._step()
            char = text[pos]
            if char in _BLANKS:
                pos += 1
                continue
            if text.startswith("\\\n", pos):
                pos += 2  # 行尾反斜線接續下一行：不是字，也不是換行
                continue
            if char == "\n":
                pos += 1
                if self.pending:
                    pos = self._heredocs(pos, end, depth)
                fresh()
                continue
            if first_word and (char in _WORD_BREAK or char == "#"):
                first_word = False
            if char == "#":
                # 走到這裡一定是字首：字只會在 metacharacter 前結束。
                newline = text.find("\n", pos, end)
                pos = end if newline < 0 else newline
                continue
            if char == ")":
                if cases and cases[-1] == "pattern":
                    cases[-1] = "body"
                    state.reset(True)
                    pos += 1
                    continue
                if closes:
                    return pos + 1
                pos += 1
                state.reset(False)
                continue
            if char == "(":
                pos = self._open_paren(pos, end, depth, state, cases)
                continue
            if char == ";":
                width = 3 if text.startswith(";;&", pos) else 2 if text.startswith((";;", ";&"), pos) else 1
                pos += width
                if width > 1 and cases:
                    cases[-1] = "pattern"
                fresh()
                continue
            if char == "&":
                if text.startswith("&>", pos):
                    pos += 3 if text.startswith("&>>", pos) else 2
                    state.redirect = True
                    continue
                pos += 2 if text.startswith("&&", pos) else 1
                if not state.condition:
                    fresh()
                continue
            if char == "|":
                if cases and cases[-1] == "pattern":
                    pos += 1
                    continue
                pos += 2 if text.startswith(("||", "|&"), pos) else 1
                if not state.condition:
                    fresh()
                continue
            if char in "<>":
                pos = self._redirection(pos, end, depth, state)
                continue
            start = pos
            pos, word = self._word(pos, end, depth)
            if first_word:
                first_word = False
                if is_document_path(word):
                    self._add("R2", start if opener is None else opener, pos)
                    state.command_position = False
                    state.started = True
                    continue
            self._classify(state, cases, word, start, pos, end)
        return end

    def _open_paren(self, pos, end, depth, state, cases):
        text = self.text
        if cases and cases[-1] == "pattern":
            return pos + 1  # 樣式前可有可無的 (
        after = pos + 1
        while after < end and text[after] in _BLANKS:
            after += 1
        if after < end and text[after] == ")":
            # name () 是函式定義，後面接函式本體（又是指令位置）。
            state.reset(True)
            return after + 1
        if state.command_position and not state.started:
            if text.startswith("((", pos):
                pos = self._arithmetic(pos + 2, end, depth + 1)
            else:
                pos = self._list(pos + 1, end, depth + 1, "subshell")
        else:
            # 指令中途的 ( 是 Bash 語法錯誤（例如被當成指令送進來的 Python 呼叫）：整行不會
            # 執行，裡面沒有指令字可言；反引號照樣找，寫錯的反引號改掉不費事。
            pos = self._list(pos + 1, end, depth + 1, "array")
        state.command_position = False
        state.started = True
        return pos

    def _redirection(self, pos, end, depth, state):
        text = self.text
        state.started = True
        if text.startswith("<<<", pos):
            state.redirect = True  # here-string：後面那個字照一般引號規則讀
            return pos + 3
        if text.startswith("<<", pos):
            strip = text.startswith("<<-", pos)
            return self._heredoc_operator(pos + (3 if strip else 2), end, strip)
        if pos + 1 < end and text[pos + 1] == "(":
            # 程序替換 <( ) >( )：裡面是一串指令，整個算一個參數。
            pos = self._list(pos + 2, end, depth + 1, "procsub")
            state.command_position = False
            return pos
        pos += 1
        if pos < end and text[pos] in ">|&":
            pos += 1
        state.redirect = True
        return pos

    def _classify(self, state, cases, word, start, stop, end):
        literal = word.rstrip("\r")
        text = self.text
        state.started = True
        if state.redirect:
            state.redirect = False
            return
        if literal.isdigit() and stop < end and text[stop] in "<>":
            return  # 2>file 的 2
        if cases:
            phase = cases[-1]
            if phase == "subject":
                if literal == "in":
                    cases[-1] = "pattern"
                return
            if phase == "pattern":
                if literal == "esac":
                    cases.pop()
                    state.reset(False)
                return
        if state.condition:
            if literal == "]]":
                state.condition = False
            return
        if state.function_name:
            state.function_name = False
            state.command_position = True
            state.started = False
            return
        if state.command_position:
            if _ASSIGNMENT.match(word):
                return
            if literal in _KEEPS_COMMAND_POSITION:
                state.started = False
                return
            state.command_position = False
            if literal in _ENDS_COMPOUND:
                return
            if literal == "esac" and cases:
                cases.pop()
                return
            if literal == "case":
                cases.append("subject")
                return
            if literal in ("for", "select"):
                return
            if literal == "function":
                state.function_name = True
                return
            if literal == "[[":
                state.condition = True
                return
            if is_document_path(literal):
                self._add("R3", start, stop)
                return
            name = re.split(r"[\\/]", literal)[-1].casefold()
            if name.endswith(".exe"):
                name = name[:-4]
            if name in _SHELL_NAMES:
                state.shell, state.shell_start = "shell", start
            elif literal in _SOURCE_NAMES:
                state.shell, state.shell_start = "source", start
            return
        if not state.shell:
            return
        if state.skip_next:
            state.skip_next = False
            return
        if not state.options_done and literal == "--":
            state.options_done = True
            return
        if not state.options_done and len(literal) > 1 and literal[0] in "-+":
            if state.shell == "shell":
                if literal in _SHELL_OPTIONS_WITH_ARGUMENT:
                    state.skip_next = True
                elif not literal.startswith("--") and ("c" in literal[1:] or "s" in literal[1:]):
                    state.shell = None  # bash -c 字串／-s 讀標準輸入：要跑的不是檔案
            return
        if is_document_path(literal):
            self._add("R3", state.shell_start, stop)
        state.shell = None

    # -- words -------------------------------------------------------------------------

    def _word(self, pos, end, depth):
        """讀一個未加引號處的字；回傳 (結束位置, 去掉引號後的字)。展開照原文留著。"""
        text = self.text
        out = []
        while pos < end:
            self._step()
            char = text[pos]
            if char in _WORD_BREAK:
                if char == "(" and _ARRAY_OPENER.match("".join(out)):
                    after = self._list(pos + 1, end, depth + 1, "array")
                    out.append(text[pos:after])
                    pos = after
                    continue
                break
            if char == "\\":
                if pos + 1 < end:
                    if text[pos + 1] != "\n":
                        out.append(text[pos + 1])
                    pos += 2
                else:
                    out.append(char)
                    pos += 1
                continue
            if char == "'":
                close = text.find("'", pos + 1, end)
                close = end if close < 0 else close
                out.append(text[pos + 1:close])
                pos = min(close + 1, end)
                continue
            if char == '"':
                pos, literal = self._double_quoted(pos + 1, end, depth + 1)
                out.append(literal)
                continue
            if char == "`":
                after = self._backtick(pos, end, depth + 1, False)
                out.append(text[pos:after])
                pos = after
                continue
            if char == "$":
                pos, literal = self._dollar(pos, end, depth + 1, False)
                out.append(literal)
                continue
            out.append(char)
            pos += 1
        return pos, "".join(out)

    def _double_quoted(self, pos, end, depth):
        """pos 在開頭的 " 之後；回傳 (結束 " 之後的位置, 內容)。"""
        self._depth(depth)
        text = self.text
        out = []
        while pos < end:
            self._step()
            char = text[pos]
            if char == '"':
                return pos + 1, "".join(out)
            if char == "\\":
                if pos + 1 < end and text[pos + 1] in _DQ_ESCAPABLE:
                    if text[pos + 1] != "\n":
                        out.append(text[pos + 1])
                    pos += 2
                else:
                    out.append(char)
                    pos += 1
                continue
            if char == "`":
                after = self._backtick(pos, end, depth + 1, True)
                out.append(text[pos:after])
                pos = after
                continue
            if char == "$":
                pos, literal = self._dollar(pos, end, depth + 1, True)
                out.append(literal)
                continue
            out.append(char)
            pos += 1
        return end, "".join(out)

    def _dollar(self, pos, end, depth, in_double_quotes):
        """pos 在 $ 上；回傳 (結束位置, 字面)。"""
        self._depth(depth)
        text = self.text
        following = text[pos + 1] if pos + 1 < end else ""
        if following == "(":
            if text.startswith("((", pos + 1):
                after = self._arithmetic(pos + 3, end, depth + 1)
            else:
                after = self._list(pos + 2, end, depth + 1, "cmdsub", opener=pos)
            return after, text[pos:after]
        if following == "{":
            after = self._parameter(pos + 2, end, depth + 1, in_double_quotes)
            return after, text[pos:after]
        if not in_double_quotes and following == "'":
            after = pos + 2
            while after < end:
                self._step()
                if text[after] == "\\":
                    after += 2
                    continue
                if text[after] == "'":
                    break
                after += 1
            after = min(after + 1, end)
            return after, text[pos + 2:after - 1]
        if not in_double_quotes and following == '"':
            return self._double_quoted(pos + 2, end, depth + 1)
        return pos + 1, "$"

    def _parameter(self, pos, end, depth, in_double_quotes):
        """${ … }：pos 在 { 之後。雙引號裡的 ${ } 中，單引號是字面字元。"""
        self._depth(depth)
        text = self.text
        while pos < end:
            self._step()
            char = text[pos]
            if char == "}":
                return pos + 1
            if char == "\\":
                pos += 2
                continue
            if char == "'" and not in_double_quotes:
                close = text.find("'", pos + 1, end)
                pos = end if close < 0 else close + 1
                continue
            if char == '"':
                pos, _literal = self._double_quoted(pos + 1, end, depth + 1)
                continue
            if char == "`":
                pos = self._backtick(pos, end, depth + 1, in_double_quotes)
                continue
            if char == "$":
                pos, _literal = self._dollar(pos, end, depth + 1, in_double_quotes)
                continue
            pos += 1
        return end

    def _arithmetic(self, pos, end, depth):
        """$(( … )) 或 (( … ))：pos 在兩個 ( 之後。第一個字不是指令，所以不套 R2。"""
        self._depth(depth)
        text = self.text
        level = 0
        while pos < end:
            self._step()
            char = text[pos]
            if char == "(":
                level += 1
                pos += 1
                continue
            if char == ")":
                if level:
                    level -= 1
                    pos += 1
                    continue
                return pos + 2 if text.startswith("))", pos) else pos + 1
            if char == "\\":
                pos += 2
                continue
            if char == '"':
                pos, _literal = self._double_quoted(pos + 1, end, depth + 1)
                continue
            if char == "`":
                pos = self._backtick(pos, end, depth + 1, False)
                continue
            if char == "$":
                pos, _literal = self._dollar(pos, end, depth + 1, False)
                continue
            pos += 1
        return end

    def _backtick(self, pos, end, depth, in_double_quotes):
        """pos 在一個會被替換的反引號上：記 R1，再把裡面那段當指令重讀一次（R2／R3）。"""
        self._depth(depth)
        text = self.text
        body = []
        index = pos + 1
        close = None
        while index < end:
            self._step()
            char = text[index]
            if char == "\\" and index + 1 < end:
                following = text[index + 1]
                if following in _BACKTICK_ESCAPABLE or (in_double_quotes and following == '"'):
                    body.append(following)
                else:
                    body.append(char)
                    body.append(following)
                index += 2
                continue
            if char == "`":
                close = index
                break
            body.append(char)
            index += 1
        stop = end if close is None else close + 1
        self._add("R1", pos, stop)
        inner = _Scan("".join(body), root=self.root,
                      base=pos if self.base is None else self.base)
        inner._list(0, len(inner.text), depth + 1, "backtick")
        return stop

    # -- here-documents ----------------------------------------------------------------

    def _heredoc_operator(self, pos, end, strip):
        """<< 或 <<- 之後：讀分隔符，記下來等這一行結束再讀本文。"""
        text = self.text
        while pos < end and text[pos] in _BLANKS:
            pos += 1
        out = []
        quoted = False
        while pos < end and text[pos] not in _WORD_BREAK:
            self._step()
            char = text[pos]
            if char == "'":
                quoted = True
                close = text.find("'", pos + 1, end)
                close = end if close < 0 else close
                out.append(text[pos + 1:close])
                pos = min(close + 1, end)
            elif char == '"':
                quoted = True
                pos += 1
                while pos < end and text[pos] != '"':
                    self._step()
                    if text[pos] == "\\" and pos + 1 < end and text[pos + 1] in _DQ_ESCAPABLE:
                        pos += 1
                    out.append(text[pos])
                    pos += 1
                pos = min(pos + 1, end)
            elif char == "\\":
                quoted = True
                if pos + 1 < end:
                    out.append(text[pos + 1])
                pos += 2
            else:
                out.append(char)
                pos += 1
        if out or quoted:
            self.pending.append(("".join(out).rstrip("\r"), quoted, strip))
        return min(pos, end)

    def _heredocs(self, pos, end, depth):
        """pos 在換行之後：依序讀完這一行登記的 here-document 本文。"""
        text = self.text
        pending, self.pending = self.pending, []
        for delimiter, quoted, strip in pending:
            start = pos
            body_end = end
            while pos < end:
                self._step()
                newline = text.find("\n", pos, end)
                line_end = end if newline < 0 else newline
                line = text[pos:line_end]
                if strip:
                    line = line.lstrip("\t")
                if line.rstrip("\r") == delimiter:
                    body_end = pos
                    pos = end if newline < 0 else newline + 1
                    break
                pos = end if newline < 0 else newline + 1
            if not quoted:
                self._heredoc_body(start, body_end, depth + 1)
        return pos

    def _heredoc_body(self, pos, end, depth):
        """分隔符沒加引號：本文照雙引號的規則替換，但 " 是字面字元。"""
        self._depth(depth)
        text = self.text
        while pos < end:
            self._step()
            char = text[pos]
            if char == "\\":
                pos += 2 if pos + 1 < end and text[pos + 1] in _HEREDOC_ESCAPABLE else 1
                continue
            if char == "`":
                pos = self._backtick(pos, end, depth + 1, False)
                continue
            if char == "$":
                pos, _literal = self._dollar(pos, end, depth + 1, True)
                continue
            pos += 1
