import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Shared fail-open mechanics for Claude hook adapters."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from re import _constants as _re_constants
from re import _parser as _re_parser
import tempfile
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


def recall_marker_directory(session_id):
    return Path(tempfile.gettempdir()) / memspec.RECALL_MARKER_DIRECTORY / session_component(session_id)


def notice_marker_directory(session_id):
    return Path(tempfile.gettempdir()) / memspec.NOTICE_MARKER_DIRECTORY / session_component(session_id)


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
    value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("hook input must be a JSON object")
    return value


def load_config(started_at):
    if expired(started_at):
        return None
    configured = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".epitype" / "config.json"
    )
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
