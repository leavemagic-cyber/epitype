"""Record browser resources created by this agent from completed tool results.

The ledger deliberately does not read browser history or save page titles/URLs.
It only claims a resource is closed when a tool result confirms closure or a
complete, successful inventory no longer contains its ID. A record that cannot
be confirmed either way expires instead of waiting forever.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import time


DB_FILENAME = "control_lifecycle.sqlite3"
SCHEMA_VERSION = 2
# 無法確認的紀錄沒有可證明的關閉路徑；過了這段時間就到期（expired，不是 closed），
# 否則它會在每一則訊息重發提醒。
UNVERIFIED_TTL_SECONDS = 3600
# 每筆資源在各階段最多提醒幾次。prompt 階段不擋回合，留少量重試給預算放不下的那次。
REMINDER_LIMITS = {"tool": 1, "prompt": 3}
# calls 只用來擋同一個結果重送，一天足夠；其餘紀錄保留一週後刪除。清理最多每小時一次，
# 而且建表／升版後先等一個間隔，不在升版那一刻刪資料。
CALL_RETENTION_SECONDS = 24 * 3600
RETENTION_SECONDS = 7 * 24 * 3600
CLEANUP_INTERVAL_SECONDS = 3600
CHROME = "mcp__claude-in-chrome__"
CUA_JS = frozenset(("mcp__cua_repl__js", "mcp__cua_repl.js"))
CUA_RESET = frozenset(("mcp__cua_repl__js_reset", "mcp__cua_repl.js_reset"))
OTHER_CONTROL = ("mcp__Claude_Browser__", "mcp__computer-use__")
_CHROME_TRACKED = frozenset(("tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp"))
# 只有「開」類名稱會留下無法確認的紀錄；close/stop/reset 類不開任何紀錄。
_CONTROL_OPEN = re.compile(r"(?:create|open|start|launch)", re.I)
_TAB_CREATED = re.compile(r"Browser tab:\s*([A-Za-z0-9_-]+)\b")
_CHROME_CLOSED = re.compile(r"\bClosed tab\s+([A-Za-z0-9_-]+)\b", re.I)
_CREATE_CUA = re.compile(r"\bcua\.createBrowserTab\s*\(")
_CREATE_BROWSER = re.compile(r"\bcua\.createBrowserTab\s*\(\s*['\"](iab|chrome|edge)['\"]", re.I)
# 清單只有在「程式最後一個敘述就是這個呼叫本身」時才算完整：REPL 輸出的是最後一個值，
# 任何 filter/map/stringify 包裝或之後再印的東西都可能讓「清單裡沒有」變成假的關閉證據。
_FINAL_INVENTORY = re.compile(
    r"(?:^|[;{}\n])\s*(?:return\s+)?(?:[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*=\s*)?"
    r"await\s+cua\.(?:(?P<state>getState)\s*\(\s*\)|listTabs\s*\(\s*\{\s*browser\s*:\s*"
    r"(?P<quote>['\"])(?P<family>(?i:iab|chrome|edge))(?P=quote)\s*\}\s*\))\s*;?\s*$"
)


def database_path(config_file: Path) -> Path:
    return Path(config_file).parent / DB_FILENAME


def is_control_tool(name: str) -> bool:
    return (
        name.startswith(CHROME)
        or name in CUA_JS
        or name in CUA_RESET
        or name.startswith(OTHER_CONTROL)
    )


def is_tracked_tool(name) -> bool:
    """Only these results can change the ledger.

    PostToolUse fires for every screenshot, click and page read; anything else
    must return before the hook reads config or opens SQLite.
    """
    if not isinstance(name, str):
        return False
    if name.startswith(CHROME):
        return name[len(CHROME):] in _CHROME_TRACKED
    if name in CUA_JS or name in CUA_RESET:
        return True
    for prefix in OTHER_CONTROL:
        if name.startswith(prefix):
            return bool(_CONTROL_OPEN.search(name[len(prefix):]))
    return False


def _now():
    return time.time()


_TABLES = (
    "CREATE TABLE IF NOT EXISTS calls ("
    "session TEXT NOT NULL, call_id TEXT NOT NULL, seen REAL NOT NULL,"
    "PRIMARY KEY(session, call_id))",
    "CREATE TABLE IF NOT EXISTS resources ("
    "session TEXT NOT NULL, kind TEXT NOT NULL, resource_id TEXT NOT NULL,"
    "status TEXT NOT NULL, opened REAL NOT NULL, changed REAL NOT NULL,"
    "agent TEXT NOT NULL DEFAULT '',"
    "PRIMARY KEY(session, kind, resource_id))",
    "CREATE TABLE IF NOT EXISTS events ("
    "id INTEGER PRIMARY KEY, session TEXT NOT NULL, call_id TEXT NOT NULL,"
    "action TEXT NOT NULL, kind TEXT NOT NULL, resource_id TEXT NOT NULL,"
    "seen REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS session_state ("
    "session TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS alerts ("
    "session TEXT NOT NULL, stage TEXT NOT NULL, fingerprint TEXT NOT NULL,"
    "seen REAL NOT NULL, count INTEGER NOT NULL DEFAULT 1,"
    "PRIMARY KEY(session, stage, fingerprint))",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS calls_seen ON calls(seen)",
    "CREATE INDEX IF NOT EXISTS events_call ON events(session, call_id)",
    "CREATE INDEX IF NOT EXISTS events_seen ON events(seen)",
)


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _upgrade(db, version):
    """Bring a ledger written by an older schema to SCHEMA_VERSION in place."""
    if version < 2:
        # 空字串＝主線；子代理的呼叫帶 agent_id，提醒只給建立者。
        if "agent" not in _columns(db, "resources"):
            db.execute("ALTER TABLE resources ADD COLUMN agent TEXT NOT NULL DEFAULT ''")
        if "count" not in _columns(db, "alerts"):
            db.execute("ALTER TABLE alerts ADD COLUMN count INTEGER NOT NULL DEFAULT 1")
        if "updated" not in _columns(db, "session_state"):
            db.execute("ALTER TABLE session_state ADD COLUMN updated REAL NOT NULL DEFAULT 0")
        now = _now()
        db.execute("UPDATE session_state SET updated=? WHERE updated=0", (now,))
        db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('last_cleanup',?)", (now,))
        # v1 的提醒指紋是整組待處理清單，改成逐筆資源之後對不上，留著只會佔位。
        db.execute("DELETE FROM alerts")
        # v1 的無法確認紀錄以呼叫 ID 為鍵，或來自 close/stop/reset 類工具，依新規則沒有任何
        # 清除路徑，而且早已提醒過：升版時一次標成到期（expired，不冒充關閉）。已確認的
        # 開啟紀錄照舊保留。這只在 hook 自然開帳本時發生，唯讀查詢不會觸發。
        for session, kind, resource_id in list(db.execute(
            "SELECT session,kind,resource_id FROM resources "
            "WHERE status='open' AND kind LIKE 'unverified%'"
        )):
            _resource(db, session, "schema-v2", "expire", kind, resource_id, now)


def _connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5.0)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            # One writer initializes a new or pre-versioned ledger; other callers wait.
            db.execute("BEGIN IMMEDIATE")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version < SCHEMA_VERSION:
                    for statement in _TABLES:
                        db.execute(statement)
                    _upgrade(db, version)
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.commit()
            except Exception:
                db.rollback()
                raise
        return db
    except Exception:
        db.close()
        raise


@contextmanager
def _database(path: Path):
    db = _connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def _text_and_objects(response):
    texts, objects = [], []

    def visit(value, depth=0):
        if depth > 5:
            return
        if isinstance(value, str):
            if len(value) > 262144:
                return
            texts.append(value)
            candidates = [value.strip()]
            if "\nOutput:" in value:
                candidates.append(value.rsplit("\nOutput:", 1)[1].strip())
            elif value.startswith("Output:"):
                candidates.append(value[len("Output:"):].strip())
            for candidate in candidates:
                try:
                    parsed = json.loads(candidate)
                except (ValueError, TypeError):
                    continue
                if isinstance(parsed, (dict, list)):
                    objects.append(parsed)
            return
        if isinstance(value, list):
            for part in value[:100]:
                visit(part, depth + 1)
            return
        if isinstance(value, dict):
            if "availableTabs" in value or "browsers" in value:
                objects.append(value)
            for key in ("content", "structuredContent", "output", "result", "text"):
                if key in value:
                    visit(value[key], depth + 1)

    visit(response)
    return "\n".join(texts), objects


def _failed(response) -> bool:
    if isinstance(response, dict):
        if response.get("isError") is True or response.get("is_error") is True:
            return True
        content = response.get("content")
        if isinstance(content, list):
            return any(isinstance(item, dict) and
                       (item.get("isError") is True or item.get("is_error") is True)
                       for item in content)
    return False


def _agent(event):
    """The subagent that made this call; "" is the main thread."""
    value = event.get("agent_id", event.get("agentId"))
    return value[:128] if isinstance(value, str) else ""


def _state(db, session):
    row = db.execute("SELECT data FROM session_state WHERE session=?", (session,)).fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row[0])
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _save_state(db, session, state, now):
    db.execute(
        "INSERT INTO session_state(session,data,updated) VALUES(?,?,?) "
        "ON CONFLICT(session) DO UPDATE SET data=excluded.data,updated=excluded.updated",
        (session, json.dumps(state, separators=(",", ":")), now),
    )


def _cleanup(db, now):
    """Drop rows past retention, at most once per CLEANUP_INTERVAL_SECONDS."""
    row = db.execute("SELECT value FROM meta WHERE key='last_cleanup'").fetchone()
    if row is not None and now - row[0] < CLEANUP_INTERVAL_SECONDS:
        return
    db.execute("INSERT INTO meta(key,value) VALUES('last_cleanup',?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
    if row is None:
        return
    cutoff = now - RETENTION_SECONDS
    db.execute("DELETE FROM calls WHERE seen<?", (now - CALL_RETENTION_SECONDS,))
    db.execute("DELETE FROM events WHERE seen<?", (cutoff,))
    db.execute("DELETE FROM resources WHERE changed<?", (cutoff,))
    db.execute("DELETE FROM alerts WHERE seen<?", (cutoff,))
    db.execute("DELETE FROM session_state WHERE updated<?", (cutoff,))


def _fingerprint(kind, resource_id, opened):
    # 同一個 ID 重新打開是新的一段，opened 不同，提醒次數重新算。
    return json.dumps([kind, resource_id, opened], separators=(",", ":"))


def _resource(db, session, call_id, action, kind, resource_id, now):
    resource_id = str(resource_id)
    if not resource_id or len(resource_id) > 128:
        return
    old = db.execute(
        "SELECT status,opened FROM resources WHERE session=? AND kind=? AND resource_id=?",
        (session, kind, resource_id),
    ).fetchone()
    if action == "open":
        if old and old[0] == "open":
            return
        db.execute(
            "INSERT INTO resources(session,kind,resource_id,status,opened,changed) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(session,kind,resource_id) DO UPDATE "
            "SET status='open',opened=excluded.opened,changed=excluded.changed,agent=''",
            (session, kind, resource_id, "open", now, now),
        )
    elif action in ("close", "expire"):
        if not old or old[0] != "open":
            return
        db.execute(
            "UPDATE resources SET status=?,changed=? "
            "WHERE session=? AND kind=? AND resource_id=?",
            ("closed" if action == "close" else "expired", now, session, kind, resource_id),
        )
        db.execute("DELETE FROM alerts WHERE session=? AND fingerprint=?",
                   (session, _fingerprint(kind, resource_id, old[1])))
    else:
        return
    db.execute(
        "INSERT INTO events(session,call_id,action,kind,resource_id,seen) "
        "VALUES(?,?,?,?,?,?)",
        (session, call_id, action, kind, resource_id, now),
    )


def _expire(db, session, now):
    for kind, resource_id in list(db.execute(
        "SELECT kind,resource_id FROM resources WHERE session=? AND status='open' "
        "AND kind LIKE 'unverified%' AND opened<?",
        (session, now - UNVERIFIED_TTL_SECONDS),
    )):
        _resource(db, session, "expiry", "expire", kind, resource_id, now)


def _open_ids(db, session, kind):
    return {
        row[0] for row in db.execute(
            "SELECT resource_id FROM resources WHERE session=? AND kind=? AND status='open'",
            (session, kind),
        )
    }


def _chrome_context(objects):
    for value in objects:
        if not isinstance(value, dict) or not isinstance(value.get("availableTabs"), list):
            continue
        ids = {
            str(tab["tabId"]) for tab in value["availableTabs"]
            if isinstance(tab, dict) and isinstance(tab.get("tabId"), (str, int))
        }
        group = value.get("tabGroupId")
        return ids, str(group) if isinstance(group, (str, int)) else None
    return None


def _unverified_chrome(db, session, call_id, candidates, now):
    """Record an unattributable creation under each tab it could be.

    Keyed by tab ID, a later "Closed tab X" result can clear it; with no
    candidate the call ID is the only key and only expiry can clear it.
    """
    for tab_id in sorted(candidates) or [call_id]:
        _resource(db, session, call_id, "open", "unverified-chrome", tab_id, now)


def _observe_chrome(db, session, call_id, name, tool_input, result_text, objects, failed, state, now):
    context = _chrome_context(objects)
    no_group = "No tab group exists for this session" in result_text
    if failed:
        return
    # Chrome 的 availableTabs 只列本工作階段群組裡的分頁，不是整個瀏覽器的清單：分頁被拖出
    # 群組、群組被解散，都會讓它「不在清單」卻仍開著。所以分頁只認「Closed tab X」；
    # 群組不見只證明群組本身沒了。
    if name.endswith("tabs_context_mcp") or name.endswith("tabs_create_mcp"):
        previous = state.get("chrome_snapshot")
        if no_group:
            state["chrome_snapshot"] = []
            state["chrome_group_absent"] = True
            for group_id in _open_ids(db, session, "chrome-group"):
                _resource(db, session, call_id, "close", "chrome-group", group_id, now)
            return
        if context is None:
            return
        ids, group = context
        if name.endswith("tabs_context_mcp") and tool_input.get("createIfEmpty") is True:
            if state.get("chrome_group_absent") is True:
                created = ids
                if group:
                    _resource(db, session, call_id, "open", "chrome-group", group, now)
            elif isinstance(previous, list):
                created = ids - set(previous)
            else:
                created = set()
                _unverified_chrome(db, session, call_id, ids, now)
            for tab_id in created:
                _resource(db, session, call_id, "open", "chrome-tab", tab_id, now)
        elif name.endswith("tabs_create_mcp"):
            created = ids - set(previous) if isinstance(previous, list) else set()
            if not created and len(ids) == 1 and previous is None:
                created = ids
            if not created:
                _unverified_chrome(db, session, call_id,
                                   ids if previous is None else set(), now)
            for tab_id in created:
                _resource(db, session, call_id, "open", "chrome-tab", tab_id, now)
            if group and state.get("chrome_group_absent") is True:
                _resource(db, session, call_id, "open", "chrome-group", group, now)
        state["chrome_snapshot"] = sorted(ids)
        state["chrome_group_absent"] = False
    elif name.endswith("tabs_close_mcp"):
        tab_id = tool_input.get("tabId")
        tab_id = str(tab_id) if isinstance(tab_id, (str, int)) else ""
        closed = set(_CHROME_CLOSED.findall(result_text))
        if tab_id and tab_id in closed:
            _resource(db, session, call_id, "close", "chrome-tab", tab_id, now)
            _resource(db, session, call_id, "close", "unverified-chrome", tab_id, now)
            if isinstance(state.get("chrome_snapshot"), list):
                state["chrome_snapshot"] = [
                    owned for owned in state["chrome_snapshot"] if owned != tab_id
                ]
        if "Group is now empty (auto-removed)" in result_text:
            for group_id in _open_ids(db, session, "chrome-group"):
                _resource(db, session, call_id, "close", "chrome-group", group_id, now)
            state["chrome_group_absent"] = True
        if context is not None:
            state["chrome_snapshot"] = sorted(context[0])


def _only(values):
    """The single distinct value, or None when absent or ambiguous."""
    distinct = {json.dumps(value, sort_keys=True, default=str): value for value in values}
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def _cua_inventory(code, objects):
    """Tab IDs from a complete, unfiltered inventory, or None when unprovable.

    listTabs scoped by a variable or a numeric browser ID, a wrapped or filtered
    result, or several candidate lists in one output cannot prove closure.
    """
    match = _FINAL_INVENTORY.search(code)
    if match is None:
        return None
    if match.group("state"):
        value = _only(value for value in objects
                      if isinstance(value, dict) and isinstance(value.get("browsers"), list))
        if value is None or value.get("errors"):
            return None
        return {
            str(tab["id"]) for browser in value["browsers"]
            if isinstance(browser, dict)
            for tab in (browser.get("tabs") or [])
            if isinstance(tab, dict) and isinstance(tab.get("id"), (str, int))
        }, None
    value = _only(value for value in objects if isinstance(value, list) and all(
        isinstance(tab, dict) and isinstance(tab.get("id"), (str, int)) for tab in value
    ))
    if value is None:
        return None
    return {str(tab["id"]) for tab in value}, match.group("family").lower()


def _observe_cua(db, session, call_id, name, tool_input, result_text, objects, failed, now):
    if name in CUA_RESET:
        if not failed and "js kernel reset" in result_text:
            _resource(db, session, call_id, "close", "cua-controller", "repl", now)
        return
    code = tool_input.get("code")
    if not isinstance(code, str):
        return
    _resource(db, session, call_id, "open", "cua-controller", "repl", now)
    creates = len(_CREATE_CUA.findall(code))
    if creates:
        found = _TAB_CREATED.findall(result_text) if not failed else []
        browser = _CREATE_BROWSER.search(code)
        family = browser.group(1).lower() if browser else "unknown"
        if len(found) == creates and len(set(found)) == creates:
            for tab_id in found:
                _resource(db, session, call_id, "open", "cua-tab:" + family, tab_id, now)
        else:
            _resource(db, session, call_id, "open", "unverified-cua:" + family, call_id, now)
    if failed:
        return
    inventory = _cua_inventory(code, objects)
    if inventory is None:
        return
    seen_ids, family = inventory
    kinds = ["cua-tab:" + family] if family else ["cua-tab:iab", "cua-tab:chrome",
                                                  "cua-tab:edge", "cua-tab:unknown"]
    for kind in kinds:
        for owned in _open_ids(db, session, kind) - seen_ids:
            _resource(db, session, call_id, "close", kind, owned, now)
    if not seen_ids:
        unknown_kinds = ["unverified-cua:" + family] if family else (
            "unverified-cua:iab", "unverified-cua:chrome",
            "unverified-cua:edge", "unverified-cua:unknown")
        for kind in unknown_kinds:
            for unknown in _open_ids(db, session, kind):
                _resource(db, session, call_id, "close", kind, unknown, now)


def observe(event: dict, path: Path) -> str | None:
    """Consume one PostToolUse result; return one uncertainty notice if needed."""
    name = event.get("tool_name")
    session = event.get("session_id", event.get("sessionId"))
    call_id = event.get("tool_use_id")
    if not is_tracked_tool(name):
        return None
    if not isinstance(session, str) or not session or not isinstance(call_id, str) or not call_id:
        return "Epitype 無法追蹤這次瀏覽器操作：工具沒有提供本次呼叫的識別碼。"
    tool_input = event.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    response = event.get("tool_response")
    text, objects = _text_and_objects(response)
    failed = _failed(response)
    now = _now()
    with _database(path) as db:
        if db.execute(
            "SELECT 1 FROM calls WHERE session=? AND call_id=?", (session, call_id)
        ).fetchone():
            return None
        db.execute("INSERT INTO calls(session,call_id,seen) VALUES(?,?,?)",
                   (session, call_id, now))
        _cleanup(db, now)
        _expire(db, session, now)
        state = _state(db, session)
        if name.startswith(CHROME):
            _observe_chrome(db, session, call_id, name, tool_input, text, objects,
                            failed, state, now)
        elif name in CUA_JS or name in CUA_RESET:
            _observe_cua(db, session, call_id, name, tool_input, text, objects,
                         failed, now)
        elif name.startswith(OTHER_CONTROL):
            _resource(db, session, call_id, "open", "unverified-control", call_id, now)
        agent = _agent(event)
        if agent:
            # 這次呼叫開的資源歸這個子代理：主線與其他子代理不會收到它的收尾提醒，
            # 也就不會照著提醒去關別人的分頁。
            db.execute(
                "UPDATE resources SET agent=? WHERE session=? AND status='open' AND "
                "(kind,resource_id) IN (SELECT kind,resource_id FROM events "
                "WHERE session=? AND call_id=? AND action='open')",
                (agent, session, session, call_id),
            )
        _save_state(db, session, state, now)
        if db.execute(
            "SELECT 1 FROM events WHERE session=? AND call_id=? AND action='open' "
            "AND kind LIKE 'unverified%' LIMIT 1", (session, call_id)
        ).fetchone():
            return "Epitype 無法從這次工具結果確認瀏覽器開關；請查實際分頁清單並回報未驗範圍。"
    return None


def _pending(db, session, now, agent=None, creator="agent"):
    """Open rows, minus unverified ones already past expiry (read-only view).

    Rows are (kind, resource_id, opened, agent); agent=None means every creator.
    `creator` is "''" when reading a v1 ledger that has no agent column.
    """
    return [row for row in db.execute(
        f"SELECT kind,resource_id,opened,{creator} FROM resources WHERE session=? "
        "AND status='open' AND NOT (kind LIKE 'unverified%' AND opened<?) ORDER BY opened",
        (session, now - UNVERIFIED_TTL_SECONDS),
    ) if agent is None or row[3] == agent]


def _summary(pending):
    pending = [(row[0], row[1]) for row in pending]
    tabs = [value for kind, value in pending if kind in ("chrome-tab",) or
            kind.startswith("cua-tab:")]
    groups = [value for kind, value in pending if kind == "chrome-group"]
    controller = any(kind == "cua-controller" for kind, _ in pending)
    uncertain = any(kind.startswith("unverified") for kind, _ in pending)
    parts = []
    if tabs:
        parts.append("分頁 " + ", ".join(tabs[:3]) + (" 等" if len(tabs) > 3 else ""))
    if groups:
        parts.append("分頁群組 " + ", ".join(groups[:2]))
    if controller:
        parts.append("操控工作階段")
    if uncertain:
        parts.append("無法確認的瀏覽器操作")
    return "、".join(parts)


def finish_subagent(event: dict, path: Path) -> int:
    """Transfer only the finished agent's open resources to its root session."""
    session = event.get("session_id", event.get("sessionId"))
    agent = _agent(event)
    if not session or not agent or not path.is_file():
        return 0
    with _database(path) as db:
        return db.execute(
            "UPDATE resources SET agent='',changed=? WHERE session=? AND agent=? "
            "AND status='open'", (_now(), session, agent)
        ).rowcount


def reminder(event: dict, path: Path, stage: str, *, claim=True) -> str | None:
    """Remind about outstanding resources, at most REMINDER_LIMITS[stage] times each.

    A reminder is due only while some pending resource is still under its limit
    for this stage; a partial close or an old resource alone is not new. A caller
    may peek with claim=False and claim only after confirming the hook payload
    fits, so a dropped message does not use up a reminder.
    """
    limit = REMINDER_LIMITS.get(stage)
    if limit is None:
        raise ValueError(f"unknown reminder stage: {stage!r}")
    session = event.get("session_id", event.get("sessionId"))
    if not isinstance(session, str) or not session or not path.is_file():
        return None
    if stage == "tool" and is_control_tool(str(event.get("tool_name") or "")):
        return None
    now = _now()
    with _database(path) as db:
        if claim:
            _expire(db, session, now)
        pending = _pending(db, session, now, _agent(event))
        if not pending:
            return None
        marks = [_fingerprint(kind, resource_id, opened)
                 for kind, resource_id, opened, _ in pending]
        counts = dict(db.execute(
            "SELECT fingerprint,count FROM alerts WHERE session=? AND stage=?",
            (session, stage),
        ))
        if all(counts.get(mark, 0) >= limit for mark in marks):
            return None
        if claim:
            for mark in marks:
                db.execute(
                    "INSERT INTO alerts(session,stage,fingerprint,seen,count) "
                    "VALUES(?,?,?,?,1) ON CONFLICT(session,stage,fingerprint) "
                    "DO UPDATE SET seen=excluded.seen,count=count+1",
                    (session, stage, mark, now),
                )
        return (
            "Epitype 查到本次仍未確認收尾：" + _summary(pending) +
            "。用完請只關自己建立的資源並核對工具結果；若使用者已要求停止，"
            "不要為收尾再操作，改為如實回報。"
        )


def status(path: Path, session: str):
    """Read one session without creating, upgrading or pruning the ledger."""
    empty = {"session": session, "pending": [], "events": []}
    if not path.is_file():
        return empty
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as db:
        columns = _columns(db, "resources")
        if not columns or not _columns(db, "events"):
            return empty
        creator = "agent" if "agent" in columns else "''"
        pending = [
            {"kind": kind, "id": resource_id, **({"agent": agent} if agent else {})}
            for kind, resource_id, _, agent in _pending(db, session, _now(), creator=creator)
        ]
        events = [{"action": action, "kind": kind, "id": resource_id}
                  for action, kind, resource_id in db.execute(
                      "SELECT action,kind,resource_id FROM events WHERE session=? "
                      "ORDER BY id", (session,)
                  )]
    return {"session": session, "pending": pending, "events": events}


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Inspect browser resources recorded for one agent session"
    )
    parser.add_argument("--session", required=True, help="host session ID")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = Path(os.environ.get("EPITYPE_CONFIG", Path.home() / ".epitype" / "config.json"))
    result = status(database_path(config), args.session)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(_summary([(item["kind"], item["id"]) for item in result["pending"]])
              or "本次沒有待確認的瀏覽器資源")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
