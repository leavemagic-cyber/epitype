"""Record browser resources created by this agent from completed tool results.

The ledger deliberately does not read browser history or save page titles/URLs.
It only claims a resource is closed when a tool result confirms closure or a
complete, successful inventory no longer contains its ID.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import time


DB_FILENAME = "control_lifecycle.sqlite3"
SCHEMA_VERSION = 1
CHROME = "mcp__claude-in-chrome__"
CUA_JS = frozenset(("mcp__cua_repl__js", "mcp__cua_repl.js"))
CUA_RESET = frozenset(("mcp__cua_repl__js_reset", "mcp__cua_repl.js_reset"))
OTHER_CONTROL = ("mcp__Claude_Browser__", "mcp__computer-use__")
_TAB_CREATED = re.compile(r"Browser tab:\s*([A-Za-z0-9_-]+)\b")
_CHROME_CLOSED = re.compile(r"\bClosed tab\s+([A-Za-z0-9_-]+)\b", re.I)
_CREATE_CUA = re.compile(r"\bcua\.createBrowserTab\s*\(")
_CREATE_BROWSER = re.compile(r"\bcua\.createBrowserTab\s*\(\s*['\"](iab|chrome|edge)['\"]", re.I)
_LIST_BROWSER = re.compile(r"\bcua\.listTabs\s*\(\s*\{\s*browser\s*:\s*['\"](iab|chrome|edge)['\"]", re.I)


def database_path(config_file: Path) -> Path:
    return Path(config_file).parent / DB_FILENAME


def is_control_tool(name: str) -> bool:
    return (
        name.startswith(CHROME)
        or name in CUA_JS
        or name in CUA_RESET
        or name.startswith(OTHER_CONTROL)
    )


def _connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5.0)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            # One writer initializes a new or pre-versioned ledger; other callers wait.
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                    for statement in (
                        "CREATE TABLE IF NOT EXISTS calls ("
                        "session TEXT NOT NULL, call_id TEXT NOT NULL, seen REAL NOT NULL,"
                        "PRIMARY KEY(session, call_id))",
                        "CREATE TABLE IF NOT EXISTS resources ("
                        "session TEXT NOT NULL, kind TEXT NOT NULL, resource_id TEXT NOT NULL,"
                        "status TEXT NOT NULL, opened REAL NOT NULL, changed REAL NOT NULL,"
                        "PRIMARY KEY(session, kind, resource_id))",
                        "CREATE TABLE IF NOT EXISTS events ("
                        "id INTEGER PRIMARY KEY, session TEXT NOT NULL, call_id TEXT NOT NULL,"
                        "action TEXT NOT NULL, kind TEXT NOT NULL, resource_id TEXT NOT NULL,"
                        "seen REAL NOT NULL)",
                        "CREATE TABLE IF NOT EXISTS session_state ("
                        "session TEXT PRIMARY KEY, data TEXT NOT NULL)",
                        "CREATE TABLE IF NOT EXISTS alerts ("
                        "session TEXT NOT NULL, stage TEXT NOT NULL, fingerprint TEXT NOT NULL,"
                        "seen REAL NOT NULL, PRIMARY KEY(session, stage, fingerprint))",
                    ):
                        db.execute(statement)
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


def _state(db, session):
    row = db.execute("SELECT data FROM session_state WHERE session=?", (session,)).fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row[0])
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _save_state(db, session, state):
    db.execute(
        "INSERT INTO session_state(session,data) VALUES(?,?) "
        "ON CONFLICT(session) DO UPDATE SET data=excluded.data",
        (session, json.dumps(state, separators=(",", ":"))),
    )


def _resource(db, session, call_id, action, kind, resource_id, now):
    resource_id = str(resource_id)
    if not resource_id or len(resource_id) > 128:
        return
    old = db.execute(
        "SELECT status FROM resources WHERE session=? AND kind=? AND resource_id=?",
        (session, kind, resource_id),
    ).fetchone()
    if action == "open":
        if old and old[0] == "open":
            return
        db.execute(
            "INSERT INTO resources(session,kind,resource_id,status,opened,changed) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(session,kind,resource_id) DO UPDATE "
            "SET status='open',opened=excluded.opened,changed=excluded.changed",
            (session, kind, resource_id, "open", now, now),
        )
    elif action == "close":
        if not old or old[0] != "open":
            return
        db.execute(
            "UPDATE resources SET status='closed',changed=? "
            "WHERE session=? AND kind=? AND resource_id=?",
            (now, session, kind, resource_id),
        )
    else:
        return
    db.execute(
        "INSERT INTO events(session,call_id,action,kind,resource_id,seen) "
        "VALUES(?,?,?,?,?,?)",
        (session, call_id, action, kind, resource_id, now),
    )


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


def _observe_chrome(db, session, call_id, name, tool_input, result_text, objects, failed, state, now):
    context = _chrome_context(objects)
    no_group = "No tab group exists for this session" in result_text
    if failed:
        return
    if name.endswith("tabs_context_mcp") or name.endswith("tabs_create_mcp"):
        previous = state.get("chrome_snapshot")
        if no_group:
            state["chrome_snapshot"] = []
            state["chrome_group_absent"] = True
            for tab_id in _open_ids(db, session, "chrome-tab"):
                _resource(db, session, call_id, "close", "chrome-tab", tab_id, now)
            for group_id in _open_ids(db, session, "chrome-group"):
                _resource(db, session, call_id, "close", "chrome-group", group_id, now)
            for unknown in _open_ids(db, session, "unverified-chrome"):
                _resource(db, session, call_id, "close", "unverified-chrome", unknown, now)
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
                _resource(db, session, call_id, "open", "unverified-chrome", call_id, now)
            for tab_id in created:
                _resource(db, session, call_id, "open", "chrome-tab", tab_id, now)
        elif name.endswith("tabs_create_mcp"):
            created = ids - set(previous) if isinstance(previous, list) else set()
            if not created and len(ids) == 1 and previous is None:
                created = ids
            if not created:
                _resource(db, session, call_id, "open", "unverified-chrome", call_id, now)
            for tab_id in created:
                _resource(db, session, call_id, "open", "chrome-tab", tab_id, now)
            if group and state.get("chrome_group_absent") is True:
                _resource(db, session, call_id, "open", "chrome-group", group, now)
        for tab_id in _open_ids(db, session, "chrome-tab") - ids:
            _resource(db, session, call_id, "close", "chrome-tab", tab_id, now)
        state["chrome_snapshot"] = sorted(ids)
        state["chrome_group_absent"] = False
    elif name.endswith("tabs_close_mcp"):
        tab_id = tool_input.get("tabId")
        tab_id = str(tab_id) if isinstance(tab_id, (str, int)) else ""
        closed = set(_CHROME_CLOSED.findall(result_text))
        if tab_id and tab_id in closed:
            _resource(db, session, call_id, "close", "chrome-tab", tab_id, now)
            if isinstance(state.get("chrome_snapshot"), list):
                state["chrome_snapshot"] = [
                    owned for owned in state["chrome_snapshot"] if owned != tab_id
                ]
        if "Group is now empty (auto-removed)" in result_text:
            for group_id in _open_ids(db, session, "chrome-group"):
                _resource(db, session, call_id, "close", "chrome-group", group_id, now)
            state["chrome_group_absent"] = True
        if context is not None:
            ids, _ = context
            for owned in _open_ids(db, session, "chrome-tab") - ids:
                _resource(db, session, call_id, "close", "chrome-tab", owned, now)
            state["chrome_snapshot"] = sorted(ids)


def _cua_inventory(code, objects):
    if re.search(r"\bcua\.getState\s*\(", code):
        for value in objects:
            if not isinstance(value, dict) or not isinstance(value.get("browsers"), list):
                continue
            if value.get("errors"):
                return None
            return {
                str(tab["id"]) for browser in value["browsers"]
                if isinstance(browser, dict)
                for tab in (browser.get("tabs") or [])
                if isinstance(tab, dict) and isinstance(tab.get("id"), (str, int))
            }, None
    if re.search(r"\bcua\.listTabs\s*\(", code):
        match = _LIST_BROWSER.search(code)
        if match is None:
            # listTabs may be scoped by a variable. Its result is not a
            # complete cross-browser inventory, so it cannot prove closure.
            return None
        for value in reversed(objects):
            if isinstance(value, list) and all(
                isinstance(tab, dict) and isinstance(tab.get("id"), (str, int))
                for tab in value
            ):
                return {str(tab["id"]) for tab in value}, match.group(1).lower()
    return None


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
    if not isinstance(name, str) or not is_control_tool(name):
        return None
    if not isinstance(session, str) or not session or not isinstance(call_id, str) or not call_id:
        return "Epitype 無法追蹤這次瀏覽器操作：工具沒有提供本次呼叫的識別碼。"
    tool_input = event.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    response = event.get("tool_response")
    text, objects = _text_and_objects(response)
    failed = _failed(response)
    now = time.time()
    with _database(path) as db:
        if db.execute(
            "SELECT 1 FROM calls WHERE session=? AND call_id=?", (session, call_id)
        ).fetchone():
            return None
        db.execute("INSERT INTO calls(session,call_id,seen) VALUES(?,?,?)",
                   (session, call_id, now))
        state = _state(db, session)
        if name.startswith(CHROME):
            _observe_chrome(db, session, call_id, name, tool_input, text, objects,
                            failed, state, now)
        elif name in CUA_JS or name in CUA_RESET:
            _observe_cua(db, session, call_id, name, tool_input, text, objects,
                         failed, now)
        elif name.startswith(OTHER_CONTROL) and re.search(
            r"(?:create|open|close|start|stop|reset|launch)", name, re.I
        ):
            _resource(db, session, call_id, "open", "unverified-control", call_id, now)
        _save_state(db, session, state)
        if db.execute(
            "SELECT 1 FROM events WHERE session=? AND call_id=? AND action='open' "
            "AND kind LIKE 'unverified%' LIMIT 1", (session, call_id)
        ).fetchone():
            return "Epitype 無法從這次工具結果確認瀏覽器開關；請查實際分頁清單並回報未驗範圍。"
    return None


def _pending(db, session):
    return list(db.execute(
        "SELECT kind,resource_id FROM resources WHERE session=? AND status='open' "
        "ORDER BY opened", (session,),
    ))


def _summary(pending):
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


def reminder(event: dict, path: Path, stage: str, *, claim=True) -> str | None:
    """Remind once per outstanding episode at a switch away or Stop.

    A caller may peek with claim=False and claim only after confirming the hook
    payload fits, so a dropped message cannot consume the one-time reminder.
    """
    session = event.get("session_id", event.get("sessionId"))
    if not isinstance(session, str) or not session or not path.is_file():
        return None
    if stage == "tool" and is_control_tool(str(event.get("tool_name") or "")):
        return None
    if stage == "stop" and (
        event.get("stop_hook_active")
        or event.get("hook_event_name") == "SubagentStop"
    ):
        return None
    with _database(path) as db:
        pending = _pending(db, session)
        if not pending:
            return None
        fingerprint = json.dumps(list(db.execute(
            "SELECT kind,resource_id,opened FROM resources WHERE session=? "
            "AND status='open' ORDER BY opened", (session,)
        )), separators=(",", ":"))
        if db.execute(
            "SELECT 1 FROM alerts WHERE session=? AND stage=? AND fingerprint=?",
            (session, stage, fingerprint),
        ).fetchone():
            return None
        if claim:
            db.execute(
                "INSERT INTO alerts(session,stage,fingerprint,seen) VALUES(?,?,?,?)",
                (session, stage, fingerprint, time.time()),
            )
        return (
            "Epitype 查到本次仍未確認收尾：" + _summary(pending) +
            "。用完請只關自己建立的資源並核對工具結果；若使用者已要求停止，"
            "不要為收尾再操作，改為如實回報。"
        )


def status(path: Path, session: str):
    if not path.is_file():
        return {"session": session, "pending": [], "events": []}
    with _database(path) as db:
        pending = [{"kind": kind, "id": resource_id} for kind, resource_id
                   in _pending(db, session)]
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
