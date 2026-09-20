import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Shared fail-open mechanics for Claude hook adapters."""

import codecs
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from re import _constants as _re_constants
from re import _parser as _re_parser
import time

from epitype import capture_route, memspec

# 原生庫的解析規則搬到 epitype.capture_route：喚回（resolve_vaults）與捕捉落點
# （capture_vault）必須讀同一份清單，各寫一份就會出現「喚回看得到、卡卻寫到別的庫」。
NATIVE_PROJECTS_SUBPATH = capture_route.NATIVE_PROJECTS_SUBPATH
native_cwd_vaults = capture_route.native_cwd_vaults


def session_component(session_id, limit=128):
    """Filesystem-safe session identifier shared by every per-session marker."""
    text = session_id if isinstance(session_id, str) else ""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text).strip("._-")[:limit] or "nosession"


def temp_root():
    """暫存目錄，不載入 tempfile。

    `tempfile.gettempdir()` 會連帶把 shutil 一起拉進來，實測 10.7 ms——而每一次工具呼叫
    都要付這一份，只為了取一個路徑。環境變數查不到時才退回 tempfile，讓它仍然正確。"""
    for name in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value and os.path.isdir(value):
            return Path(value)
    import tempfile as _tempfile

    return Path(_tempfile.gettempdir())


def recall_marker_directory(session_id):
    return temp_root() / memspec.RECALL_MARKER_DIRECTORY / session_component(session_id)


def notice_marker_directory(session_id):
    return temp_root() / memspec.NOTICE_MARKER_DIRECTORY / session_component(session_id)


def _clear_marker_directory(directory):
    try:
        for marker in directory.iterdir():
            if marker.is_file() and not marker.is_symlink():
                marker.unlink()
        directory.rmdir()
    except OSError:
        pass


def clear_recall_markers(session_id):
    """Compaction drops the injected context, so the same-session dedupe is
    dropped with it: a correction injected before compaction must return after."""
    _clear_marker_directory(recall_marker_directory(session_id))


def clear_notice_markers(session_id):
    """The write gate blocks one (session, rule, file, content) exactly once. A
    replay — the exam runner asking the same question twice — must get the same
    answer both times, so the markers one run wrote are dropped again."""
    _clear_marker_directory(notice_marker_directory(session_id))


def event_session_id(event):
    value = event.get("session_id", event.get("sessionId", "")) if isinstance(event, dict) else ""
    return value if isinstance(value, str) else ""


def expired(started_at):
    import time

    return time.monotonic() - started_at >= memspec.HOOK_TIMEOUT_SECONDS


def read_event(stream):
    # 2026-09-16: Cursor (which loads this repo's hooks through its Claude-config
    # compatibility layer) writes a UTF-8 BOM before the JSON. `json.load` raises
    # on it, every adapter's `except Exception: pass` swallows the raise, and the
    # hook then runs, exits 0 and does nothing — measured across 46 invocations in
    # one Cursor session: every Epitype hook silent, zero output. Read the text and
    # drop a leading BOM before parsing; hosts that send none are unaffected.
    raw = stream.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    else:
        raw = raw.lstrip("\ufeff")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("hook input must be a JSON object")
    return _normalised_event(value)


def _normalised_event(value):
    """Translate a host dialect into the field names the adapters read.

    Cursor names the working directory `workspace_roots` (a list) and writes
    each entry URL-style, so a Windows path arrives as "/C:/project". Every
    vault lookup here reads `cwd`, so without this the hooks run against no
    vault and correctly emit nothing -- indistinguishable from a broken hook.
    An existing `cwd` always wins; a host that sends one is untouched.
    """
    if "cwd" in value:
        return value
    roots = value.get("workspace_roots")
    if not isinstance(roots, list):
        return value
    for root in roots:
        if not isinstance(root, str) or not root.strip():
            continue
        if len(root) > 2 and root[0] == "/" and root[2] == ":":
            root = root[1:]
        value["cwd"] = root
        break
    return value


def config_path():
    """設定檔該在哪。用來分辨「沒裝 Epitype」與「裝了但壞了」——前者安靜是對的，
    後者安靜就是本專案最不能容忍的那種壞掉。"""
    configured = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".epitype" / "config.json"
    )


def load_config(started_at):
    if expired(started_at):
        return None
    path = config_path()
    raw = path.read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("config must be an object")

    raw_vaults = value.get(memspec.CONFIG_VAULTS_FIELD)
    if not isinstance(raw_vaults, list) or not raw_vaults:
        raise ValueError("config vaults must be a non-empty list")
    vaults = []
    unavailable = []
    for item in raw_vaults:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("vault paths must be non-empty strings")
        vault = Path(item).expanduser().absolute()
        try:
            vault = vault.resolve()
            available = vault.is_dir()
        except (OSError, RuntimeError):
            available = False
        if not available:
            unavailable.append(vault)
        vaults.append(vault)

    raw_budget = value.get(
        memspec.CONFIG_BUDGET_BYTES_FIELD,
        memspec.HOOK_DEFAULT_BUDGET_BYTES,
    )
    if isinstance(raw_budget, bool) or not isinstance(raw_budget, int):
        raise ValueError("budget_bytes must be a positive integer")
    budget = raw_budget
    if budget <= 0:
        raise ValueError("budget_bytes must be a positive integer")
    if unavailable:
        print(f"Epitype degraded: {len(unavailable)} configured vault(s) unavailable; "
              "readable vaults remain searchable; hook governance writes paused.", file=sys.stderr)
    return {
        memspec.CONFIG_VAULTS_FIELD: vaults,
        "_unavailable_vaults": unavailable,
        memspec.CONFIG_BUDGET_BYTES_FIELD: min(
            budget,
            memspec.HOOK_DEFAULT_BUDGET_BYTES,
        ),
        memspec.DREAM_CONFIG_FIELD: dream_settings(value),
    }


def dream_settings(value):
    """夢的排程設定：模式與間隔，欄位缺了就用預設（預設 piggyback／24 小時）。
    值壞掉一律當 off——背景程序不得從垃圾值起跑。EPITYPE_DREAM_MODE 是單次覆寫，
    合成測試靠它保證永遠不起真程序。"""
    raw = value.get(memspec.DREAM_CONFIG_FIELD)
    raw = raw if isinstance(raw, dict) else {}
    mode = os.environ.get(memspec.DREAM_MODE_ENV) or raw.get(
        memspec.DREAM_MODE_FIELD, memspec.DREAM_DEFAULT_MODE
    )
    hours = raw.get(
        memspec.DREAM_INTERVAL_HOURS_FIELD, memspec.DREAM_DEFAULT_INTERVAL_HOURS
    )
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
        hours = memspec.DREAM_DEFAULT_INTERVAL_HOURS
    return {
        memspec.DREAM_MODE_FIELD: mode
        if mode in memspec.DREAM_MODES
        else memspec.DREAM_MODE_OFF,
        memspec.DREAM_INTERVAL_HOURS_FIELD: hours,
    }


def resolve_vaults(config, event, home=None):
    """Closest native cwd vault first, then the configured vaults, deduplicated."""
    vaults = native_cwd_vaults(event.get("cwd") if isinstance(event, dict) else None, home)
    for vault in config[memspec.CONFIG_VAULTS_FIELD]:
        if vault not in config.get("_unavailable_vaults", ()) and vault not in vaults:
            vaults.append(vault)
    return vaults


def scoped_vaults(config, event, home=None):
    """This project's own vault(s) plus the governance vault; every configured vault when
    the host sends no cwd. Another project's cards are noise in this session and are
    re-read on every later call. The Stop gate already uses this scope."""
    cwd = event.get("cwd") if isinstance(event, dict) else None
    if not isinstance(cwd, str) or not cwd.strip():
        return resolve_vaults(config, event, home)
    vaults = native_cwd_vaults(cwd, home)
    governance = governance_vault(config)
    if governance not in vaults:
        vaults.append(governance)
    return vaults


def governance_vault(config, *, for_write=False):
    """Return the configured ledger holder, falling back to the legacy first vault."""
    # A missing vault may have held the ledger; never reinterpret its absence
    # as permission to write governance state into a different vault.
    if for_write and config.get("_unavailable_vaults"):
        raise OSError("governance write destination cannot be verified")
    vaults = config[memspec.CONFIG_VAULTS_FIELD]
    return next(
        (vault for vault in vaults if (vault / memspec.WORK_LEDGER_FILENAME).is_file()),
        vaults[0],
    )


def capture_vault(config, event, home=None):
    """自動捕捉的落點：這場對話屬於哪個專案，卡就進那個專案的記憶庫。

    規則在 epitype.capture_route（離線回放與歸戶稽核共用同一份）；治理庫只在 cwd
    不屬於任何已登記專案庫時接手。
    """
    return capture_route.capture_vault(
        event.get("cwd") if isinstance(event, dict) else None,
        governance_vault(config, for_write=True), home
    )


_REPEAT_OPS = frozenset(
    (
        _re_constants.MAX_REPEAT,
        _re_constants.MIN_REPEAT,
        _re_constants.POSSESSIVE_REPEAT,
    )
)
_GROUPREF_OPS = frozenset(
    (
        _re_constants.GROUPREF,
        _re_constants.GROUPREF_EXISTS,
        _re_constants.GROUPREF_IGNORE,
        _re_constants.GROUPREF_LOC_IGNORE,
        _re_constants.GROUPREF_UNI_IGNORE,
    )
)


def _validate_regex_tree(items, inside_repeat=False):
    """Reject the shapes that can backtrack exponentially on a crafted message:
    a repetition or an alternation nested inside an unbounded repetition, and
    backreferences. A hung gate is killed by the host and the call proceeds, so
    such a pattern would be a bypass. Adjacent repetitions (`\\s+\\S+`) and anything
    under a bounded `?` stay accepted: at worst polynomial, and real rulings use
    them; the rejection itself is surfaced by the caller, never silent."""
    for opcode, argument in items:
        if opcode in _REPEAT_OPS:
            unbounded = argument[1] > 1
            if unbounded and inside_repeat:
                raise ValueError("pattern nests one repetition inside another")
            _validate_regex_tree(argument[2], inside_repeat=inside_repeat or unbounded)
        elif opcode == _re_constants.BRANCH:
            if inside_repeat:
                raise ValueError("pattern repeats an alternation")
            for branch in argument[1]:
                _validate_regex_tree(branch, inside_repeat=inside_repeat)
        elif opcode == _re_constants.SUBPATTERN:
            _validate_regex_tree(argument[-1], inside_repeat=inside_repeat)
        elif opcode in (_re_constants.ASSERT, _re_constants.ASSERT_NOT):
            _validate_regex_tree(argument[1], inside_repeat=inside_repeat)
        elif opcode == getattr(_re_constants, "ATOMIC_GROUP", object()):
            _validate_regex_tree(argument, inside_repeat=inside_repeat)
        elif opcode in _GROUPREF_OPS:
            raise ValueError("pattern backreferences are not supported")


def compile_pattern_or_literal(pattern):
    """(compiled, repaired) — a pattern that will not compile falls back to literal.

    A card whose pattern will not compile enforces nothing, and the author is usually
    not writing a regex at all: they wrote a phrase that happens to contain a bracket.
    Matching it literally is what they meant, and it can only ever match less than a
    working pattern would, so the repair cannot over-block.

    What this deliberately does not do is guess at a truncated pattern. `(a|b` is a
    half-written intention, and completing it would be choosing the rule's content on
    the author's behalf; the literal fallback there simply matches nothing, and the
    lint and the gate both say so out loud."""
    try:
        return compile_bounded_regex(pattern), False
    except Exception:
        pass
    try:
        return re.compile(re.escape(pattern[: memspec.FORBIDDEN_REGEX_MAX_CHARS])), True
    except Exception:
        return None, False


def compile_bounded_regex(pattern):
    """The shared validator for a decision card's `forbidden` patterns — the Stop
    gate and this gate's rule A compile through this one reading, so a pattern that
    is unusable at the end of a turn is unusable in a file write too."""
    if len(pattern) > memspec.FORBIDDEN_REGEX_MAX_CHARS:
        raise ValueError("pattern exceeds the length limit")
    parsed = _re_parser.parse(pattern, 0)
    _validate_regex_tree(parsed)
    return re.compile(pattern)


# 2026-09-06 真機實測：治理 vault 的 _GATE_LOG.jsonl 灌到 13,061 列（同一批壞卡每次呼叫
# 都補一列，三天沒消）。那條寫入路徑已隨 trigger 攔截退役（2026-09-09 U-J），上限留著：
# 稽核檔仍會長，超過就把整份改名成 .1（保留一份，不刪，不接力鏈成 .2 .3…）。
GATE_LOG_MAX_BYTES = 2 * 1024 * 1024


def declared_frontmatter(path, field, max_bytes):
    """Frontmatter lines of a card that declares `field` at the top level, else None.

    Only the head of the file is read: a card's frontmatter sits at the top, and a
    gate that read every card's body would cost more than the call it guards. A card
    whose frontmatter does not close inside that head is skipped rather than guessed
    at — half a card's fields could name a ruling that is not there.

    Shared by the Stop gate (`decision_key`) and the action guard (`guard_tool`) so a
    card cannot be a ruling to one gate and prose to the other. A field nested under
    a parent block is deliberately not a declaration: the gates read top-level fields
    only, and a card whose fields were wrapped one level deep enforces nothing —
    2026-09-16 three freshly written decision cards were silently disarmed exactly
    that way, so the indentation test here is the difference between armed and inert.
    """
    try:
        with path.open("rb") as stream:
            head = stream.read(max_bytes)
    except OSError:
        return None
    try:
        # A bounded read may cut a valid UTF-8 body character after the closing
        # boundary. Only an incomplete final character may wait for more bytes.
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        lines, closing = memspec.split_frontmatter(text)
    except UnicodeError:
        return None
    if lines is None or closing is None:
        return None
    declares = any(
        line[:1] not in " \t"
        and ":" in line
        and line.split(":", 1)[0].strip() == field
        for line in lines
    )
    return lines if declares else None


def sequence_fields(front_lines, wanted):
    """Values of the named top-level sequence fields, in card order.

    memspec.frontmatter_fields yields top-level scalars only, memsearch yields
    aliases only, and card_lint yields item counts only — none of the three yields a
    list like `forbidden` or `guard_all_of`. The primitives are still the shared ones
    (parse_scalar, memspec.split_flow_items), so a card cannot be one shape here and
    another shape to the lints. Both the block form (`key:` then `  - item`) and the
    flow form (`key: [a, b]`) are accepted."""
    wanted = tuple(wanted)
    values = {key: [] for key in wanted}
    parent = None
    for raw_line in front_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        leading = raw_line[: len(raw_line) - len(raw_line.lstrip())]
        if "\t" in leading:
            parent = None
            continue
        if leading:
            if parent and (stripped == "-" or stripped.startswith("- ")):
                item, _problem = memspec.parse_scalar(stripped[1:])
                if item:
                    values[parent].append(item)
            continue
        parent = None
        match = memspec.TOP_LEVEL_FIELD.match(raw_line)
        if match is None:
            continue
        key, raw_value = match.groups()
        if key not in wanted:
            continue
        inline = memspec.strip_inline_comment(raw_value).strip()
        if inline in memspec.BLOCK_SCALAR_STYLES:
            continue
        if inline.startswith("[") and inline.endswith("]"):
            for piece in memspec.split_flow_items(inline[1:-1]):
                item, _problem = memspec.parse_scalar(piece)
                if item:
                    values[key].append(item)
            continue
        if inline:
            item, _problem = memspec.parse_scalar(raw_value)
            if item:
                values[key].append(item)
            continue
        parent = key
    return values


def with_session(row, session_id):
    """Row plus session_id when the hook event actually carried one; omitted
    entirely otherwise so old-shaped log lines and new ones stay distinguishable."""
    if isinstance(session_id, str) and session_id:
        return {**row, "session_id": session_id}
    return row


def _rotate_gate_log_if_oversized(target):
    """Called with the log's file lock already held. A stat/replace failure is
    swallowed: a rotation that cannot happen must never block the audit write."""
    try:
        if target.stat().st_size <= GATE_LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(target, target.with_name(target.name + ".1"))
    except OSError:
        pass


def append_gate_log(vault, row, started_at):
    target = vault / memspec.GATE_LOG_FILENAME
    remaining = memspec.HOOK_TIMEOUT_SECONDS - (time.monotonic() - started_at)
    if remaining <= 0:
        raise TimeoutError("hook deadline reached")
    value = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **row,
    }
    with memspec.file_lock(target, min(0.25, remaining)) as acquired:
        if not acquired:
            raise OSError("gate audit lock unavailable")
        _rotate_gate_log_if_oversized(target)
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _notes_path(config, session_id):
    session = "".join(char for char in str(session_id or "") if char.isalnum() or char in "-_")
    if not session:
        return None
    return (Path(governance_vault(config, for_write=True)) / memspec.FTS_INDEX_DIRECTORY
            / memspec.STOP_NOTE_STATE_DIRECTORY / (session + ".txt"))


def leave_note(config, session_id, text):
    """回合閘留給下一則提問的一行提醒。超過上限就不再疊——提醒本身不能變成負擔。"""
    path = _notes_path(config, session_id)
    line = " ".join(str(text).split())[: memspec.STOP_NOTE_MAX_CHARS]
    if path is None or not line:
        return
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if line in existing or len(existing) >= memspec.STOP_NOTE_MAX_PENDING:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*existing, line]) + "\n", encoding="utf-8")


def take_notes(config, session_id):
    """取走並清掉這一場累積的提醒；沒有就回空串列。壞了當作沒有——這是提醒，不是關卡。"""
    try:
        path = _notes_path(config, session_id)
        if path is None or not path.is_file():
            return []
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        path.unlink()
        return lines[: memspec.STOP_NOTE_MAX_PENDING]
    except (OSError, ValueError):
        return []


def payload(event_name, context):
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }


def encode_payload(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def payload_fits(event_name, context, context_budget):
    if len(context.encode("utf-8")) > context_budget:
        return False
    encoded = encode_payload(payload(event_name, context)).encode("utf-8")
    return len(encoded) <= memspec.HOOK_MAX_OUTPUT_BYTES


def bounded_context(event_name, pieces, context_budget, required_first=False):
    """The longest prefix of `pieces` that fits the budget, in order: a piece
    is never skipped for a later one (a ledger read with holes is worse than a
    ledger cut short), and a cut is announced with the count of pieces left."""
    pieces = [piece for piece in pieces if isinstance(piece, str) and piece]
    selected = []
    for index, piece in enumerate(pieces):
        candidate = "\n".join(selected + [piece])
        if payload_fits(event_name, candidate, context_budget):
            selected.append(piece)
            continue
        if required_first and not selected:
            return None
        suffix = memspec.CONTEXT_TRUNCATED_SUFFIX.format(dropped=len(pieces) - index)
        while selected and not payload_fits(event_name, "\n".join(selected + [suffix]), context_budget):
            selected.pop()
        if selected:
            selected.append(suffix)
        break
    return "\n".join(selected) if selected else None


def emit(value):
    encoded = encode_payload(value)
    if len(encoded.encode("utf-8")) > memspec.HOOK_MAX_OUTPUT_BYTES:
        raise ValueError("hook output exceeds the hard byte limit")
    print(encoded)


def run_synthetic(script, event, config_path, arguments=(), environment=None):
    import subprocess

    # 合成測試永遠不得起背景夢：預設關掉，呼叫端要測通知行時再自己開回來。
    # 家目錄同理預設隔離：落點與喚回都會把 cwd 的原生專案庫算進來，而真機上 `C:\`
    # 是每個暫存 cwd 的祖先且它的原生庫就是治理庫——沒有這道隔離，一次 selftest
    # 就會把捕捉到的卡寫進 owner 的真庫。要測原生庫的呼叫端自己傳 HOME。
    isolated_home = os.fspath(Path(config_path).resolve().parent / "_synthetic_home")
    environment = {
        **os.environ,
        memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
        "HOME": isolated_home,
        "USERPROFILE": isolated_home,
        **(environment or {}),
    }
    environment[memspec.EPITYPE_CONFIG_ENV] = os.fspath(config_path)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, os.fspath(script), *arguments],
        input=json.dumps(event, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=memspec.HOOK_TIMEOUT_SECONDS + 10,
        check=False,
    )


def write_config(path, vaults, budget=memspec.HOOK_DEFAULT_BUDGET_BYTES):
    value = {
        memspec.CONFIG_VAULTS_FIELD: [os.fspath(item) for item in vaults],
        memspec.CONFIG_BUDGET_BYTES_FIELD: budget,
    }
    path.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding="utf-8",
    )
