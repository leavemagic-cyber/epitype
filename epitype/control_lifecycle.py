"""Record browser resources created by this agent from completed tool results.

The ledger deliberately does not read browser history or save page titles/URLs;
the one exception is the authuser and notebook ID of an open Colab notebook tab
the agent created, which the running-VM exemption is checked against. It only
claims a resource is closed when a tool result confirms closure or a complete,
successful inventory no longer contains its ID. A record that cannot be
confirmed either way expires instead of waiting forever.

A confirmed, agent-created Chrome or built-in browser tab that is still open
blocks the agent's next non-browser tool call (owner 2026-10-03).
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import parse_qs, urlsplit


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
PANE = "mcp__Claude_Browser__"
CUA_JS = frozenset(("mcp__cua_repl__js", "mcp__cua_repl.js"))
CUA_RESET = frozenset(("mcp__cua_repl__js_reset", "mcp__cua_repl.js_reset"))
OTHER_CONTROL = (PANE, "mcp__computer-use__")
# navigate 的結果帶分頁清單與網址：Colab 保留宣告要拿它核對，內建瀏覽器沒開時它也會開出分頁。
_CHROME_TRACKED = frozenset(("tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp", "navigate"))
_PANE_TRACKED = frozenset(("tabs_create", "preview_start", "tabs_close", "tabs_context", "navigate"))
# 只有「開」類名稱會留下無法確認的紀錄；close/stop/reset 類不開任何紀錄。
_CONTROL_OPEN = re.compile(r"(?:create|open|start|launch)", re.I)
_TAB_CREATED = re.compile(r"Browser tab:\s*([A-Za-z0-9_-]+)\b")
_CHROME_CLOSED = re.compile(r"\bClosed tab\s+([A-Za-z0-9_-]+)\b", re.I)
_CHROME_CREATED = re.compile(r"^Created new tab\. Tab ID:\s*(\d+)\s*$", re.M)
_CHROME_DISCONNECTED = "Claude in Chrome is not connected"
_PANE_GONE = "the Browser pane is now closed"
_EXECUTED_ON = re.compile(r"Executed on tabId:\s*([A-Za-z0-9_-]+)")
# 「  • tabId 123: "標題" ("網址")」；網址取行尾最後一組括號，標題裡的括號不算。
_LISTED_TAB = re.compile(r"^[ \t]*\S*[ \t]*tabId[ \t]+([A-Za-z0-9_-]+)\b([^\n]*)$", re.M)
# Chrome 列完整網址並加引號；內建瀏覽器只列來源網域、不加引號。
_LISTED_URL = re.compile(r'\("([^"\s]*)"\)\s*$|\(([^"\s()]+)\)\s*$')
_NAVIGATED_TO = re.compile(r"^navigated to (\S+)", re.I | re.M)
_NO_PANE = "No preview is open"
COLAB_ORIGIN = "https://colab.research.google.com"
_COLAB_NOTEBOOK = re.compile(r"/drive/([A-Za-z0-9_-]{8,128})/?")

# 只有這兩種分頁的建立能從工具結果確認是本場建的；擋下非瀏覽器工具的只有它們。
# 分頁群組、無法確認的紀錄、Codex CUA 分頁仍只提醒。
BLOCKING_KINDS = ("chrome-tab", "pane-tab")
# 使用者整則訊息就是這四個字（不計空白標點），本場還開著的分頁就不再擋（仍會提醒）；給這個
# session 沒有能關分頁的瀏覽器工具時用。只比對整則：「不要放行分頁」或轉貼的長段落都不算。
RELEASE_PHRASE = "放行分頁"
_NOT_LETTERS = re.compile(r"[\W_]+")
# 擋住時仍要放行的工具：收尾本身要載入關分頁的工具、讀操作說明、問使用者、記待辦、停背景工作。
UNBLOCKED_TOOLS = frozenset((
    "ToolSearch", "Skill", "AskUserQuestion", "TodoWrite", "TaskCreate", "TaskUpdate",
    "TaskList", "TaskGet", "TaskStop", "EnterPlanMode", "ExitPlanMode",
))
SHELL_TOOLS = frozenset(("Bash", "PowerShell"))
# 保留宣告要拿最近幾分鐘內看到的網址核對；navigate 以外的點擊也可能換頁，舊網址不算數。
KEEP_EVIDENCE_SECONDS = 300
# Colab 單次執行階段最長約 24 小時；過了要重新宣告（重新核對網址）。
KEEP_TTL_SECONDS = 24 * 3600
_PYTHON_NAME = r"(?:python(?:\d+(?:\.\d+)?)?w?|py)(?:\.exe)?"
_KEEP_COMMAND = re.compile(
    r"(?:&\s*)?(?:(?:\"(?:[\w.:~ ()\\/-]*[\\/])?" + _PYTHON_NAME + r"\"|(?:[\w.:~\\/-]*[\\/])?" + _PYTHON_NAME +
    r")(?:\s+-3)?\s+-m\s+epitype|epitype(?:\.exe)?)\s+lifecycle\s+keep"
    r"\s+(?P<tab>[A-Za-z0-9_-]{1,64})\s+--authuser\s+(?P<authuser>\d{1,3})"
    r"\s+--notebook\s+(?P<notebook>[A-Za-z0-9_-]{8,128})",
    re.I,
)
_SHELL_META = re.compile(r"[;&|<>`\r\n]|\$\(")
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
    if name.startswith(PANE) and name[len(PANE):] in _PANE_TRACKED:
        return True
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
        # 擋下次數留著備查（到保留期限才刪）；提醒次數隨這一段結束清掉。
        db.execute("DELETE FROM alerts WHERE session=? AND fingerprint=? AND stage<>'block'",
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


def _listing(text, objects, origins=False):
    """{tab ID: URL or None} from the result's tab list, or None when it shows none.

    origins=True also reads the built-in browser's unquoted `(https://host)`; a
    Chrome title ending in `(v2)` must not read as a URL.
    """
    for value in objects:
        if isinstance(value, dict) and isinstance(value.get("availableTabs"), list):
            return {
                str(tab["tabId"]): tab["url"] if isinstance(tab.get("url"), str) else None
                for tab in value["availableTabs"]
                if isinstance(tab, dict) and isinstance(tab.get("tabId"), (str, int))
            }
    marker = text.rfind("Available tabs:")
    if marker < 0:
        return None
    found = {}
    for match in _LISTED_TAB.finditer(text[marker:]):
        url = _LISTED_URL.search(match.group(2))
        found[match.group(1)] = None if url is None else (
            url.group(1) if url.group(1) is not None else url.group(2) if origins else None)
    return found


def _colab(url):
    """(authuser, notebook ID) of a Colab Drive notebook URL, else None."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    match = _COLAB_NOTEBOOK.fullmatch(parts.path)
    if parts.scheme != "https" or host != "colab.research.google.com" or match is None:
        return None
    # 網址沒帶 authuser 時 Google 用第 0 個帳號。
    user = (parse_qs(parts.query).get("authuser") or ["0"])[0]
    return (user, match.group(1)) if user.isdigit() else None


def _key(kind, resource_id):
    return f"{kind}:{resource_id}"


def _marks(state, name):
    value = state.get(name)
    return value if isinstance(value, dict) else {}


def _note_urls(db, session, kind, listing, state, now):
    """Remember which of our open tabs show a Colab notebook.

    A kept tab whose latest listed URL is no longer the declared notebook loses
    its exemption; a listing line without a URL proves nothing either way. The
    built-in browser lists only each tab's origin: a Colab origin keeps what its
    navigate showed, any other origin voids it.
    """
    if not listing:
        return
    colab, kept = _marks(state, "colab"), _marks(state, "kept")
    owned = _open_ids(db, session, kind)
    for tab_id, url in listing.items():
        if tab_id not in owned or url is None:
            continue
        key = _key(kind, tab_id)
        if kind == "pane-tab" and _origin_only(url):
            if url.rstrip("/").lower() == COLAB_ORIGIN:
                if isinstance(colab.get(key), dict):
                    colab[key].pop("unconfirmed", None)
                continue
            found = None
        else:
            found = _colab(url)
        if found:
            colab[key] = {"authuser": found[0], "notebook": found[1], "seen": now}
        else:
            colab.pop(key, None)
        held = kept.get(key)
        if isinstance(held, dict) and (
                not found or [held.get("authuser"), held.get("notebook")] != list(found)):
            del kept[key]
    state["colab"], state["kept"] = colab, kept


def _origin_only(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(parts.scheme and parts.netloc) and parts.path in ("", "/") and not parts.query


def _same_origin(left, right):
    try:
        a, b = urlsplit(left), urlsplit(right)
    except ValueError:
        return False
    return bool(a.scheme and a.netloc) and (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower())


def _prune_marks(db, session, state):
    """Drop URL notes and exemptions whose tab is closed or was reopened since."""
    opened = {
        _key(kind, resource_id): when for kind, resource_id, when in db.execute(
            "SELECT kind,resource_id,opened FROM resources WHERE session=? AND status='open'",
            (session,),
        )
    }
    for name, stamp in (("colab", "seen"), ("kept", "opened"), ("released", "opened")):
        marks = _marks(state, name)
        for key in list(marks):
            mark = marks[key]
            if key not in opened or not isinstance(mark, dict) or not isinstance(
                    mark.get(stamp), (int, float)) or (
                    mark[stamp] < opened[key] if name == "colab" else mark[stamp] != opened[key]):
                del marks[key]
        if marks or name in state:
            state[name] = marks


def _observe_chrome(db, session, call_id, name, tool_input, result_text, objects, failed, state, now):
    if failed:
        return
    _chrome_ledger(db, session, call_id, name, tool_input, result_text, objects, state, now)
    # 擋人只算目前還在 Claude 群組裡、工具碰得到的分頁：被使用者關掉、拖出群組、群組解散或
    # Chrome 斷線時，代理已經沒辦法關它，再擋只會卡死；紀錄照舊待確認、照舊提醒。
    if _CHROME_DISCONNECTED in result_text or "No tab group exists for this session" in result_text:
        state["chrome_visible"] = []
        return
    listing = _listing(result_text, objects)
    if listing is not None:
        state["chrome_visible"] = sorted(listing)
        _note_urls(db, session, "chrome-tab", listing, state, now)


def _chrome_ledger(db, session, call_id, name, tool_input, result_text, objects, state, now):
    context = _chrome_context(objects)
    no_group = "No tab group exists for this session" in result_text
    # Chrome 的 availableTabs 只列本工作階段群組裡的分頁，不是整個瀏覽器的清單：分頁被拖出
    # 群組、群組被解散，都會讓它「不在清單」卻仍開著。所以分頁只認「Closed tab X」；
    # 群組不見只證明群組本身沒了。
    if name.endswith("tabs_context_mcp") or name.endswith("tabs_create_mcp"):
        previous = state.get("chrome_snapshot")
        created_result = set(_CHROME_CREATED.findall(result_text)) if name.endswith("tabs_create_mcp") else set()
        if len(created_result) == 1 and not no_group:
            _resource(db, session, call_id, "open", "chrome-tab", next(iter(created_result)), now)
        if no_group:
            state["chrome_snapshot"] = []
            state["chrome_group_absent"] = True
            for group_id in _open_ids(db, session, "chrome-group"):
                _resource(db, session, call_id, "close", "chrome-group", group_id, now)
            return
        if context is None:
            if name.endswith("tabs_create_mcp") and not created_result:
                _unverified_chrome(db, session, call_id, set(), now)
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


def _leading_json(text):
    """The JSON object a result starts with; built-in browser results append prose to it."""
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(stripped)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _observe_pane(db, session, call_id, short, tool_input, result_text, failed, state, now):
    """Built-in browser tabs live in the app's own pane, never in the owner's Chrome.

    tabs_context lists every pane tab, so a recorded tab missing from it is closed.
    Tab lists show only origins; the full URL a tab was sent to is the navigate
    input, counted only when the result confirms that navigation.
    """
    if failed:
        # 指定了 serverId／tabId 的呼叫可能只是指錯對象，不能拿來證明整個窗格關了。
        if (result_text.lstrip().startswith(_NO_PANE)
                and "serverId" not in tool_input and "tabId" not in tool_input):
            for owned in _open_ids(db, session, "pane-tab"):
                _resource(db, session, call_id, "close", "pane-tab", owned, now)
            state["pane_open"] = False
        return
    url = tool_input.get("url")
    value = _leading_json(result_text)
    tab_id = value.get("tabId") if value else None
    if short in ("tabs_create", "preview_start", "navigate") and isinstance(tab_id, (str, int)):
        # 內建瀏覽器關著時，這三個工具都回一段 JSON：reused=false 就是這次開出的分頁。
        if value.get("reused") is False:
            _resource(db, session, call_id, "open", "pane-tab", tab_id, now)
        state["pane_open"] = True
        if value.get("navOk") is True and isinstance(url, str):
            _note_urls(db, session, "pane-tab", {str(tab_id): url}, state, now)
            # 這段 JSON 不說最後落在哪個網域（可能被導去登入頁）：等清單顯示它停在 Colab 才算數。
            noted = _marks(state, "colab").get(_key("pane-tab", tab_id))
            if isinstance(noted, dict):
                noted["unconfirmed"] = True
        return
    if short in ("tabs_create", "preview_start"):
        _resource(db, session, call_id, "open", "unverified-control", call_id, now)
        return
    if short == "navigate":
        executed = set(_EXECUTED_ON.findall(result_text))
        # 確知內建瀏覽器關著時，navigate 會開出分頁；狀態不明時分不出是開新的還是沿用。
        if len(executed) == 1 and state.get("pane_open") is False:
            _resource(db, session, call_id, "open", "pane-tab", next(iter(executed)), now)
        if executed:
            state["pane_open"] = True
        landed = _NAVIGATED_TO.search(result_text)
        if (len(executed) == 1 and landed and isinstance(url, str)
                and _same_origin(landed.group(1), url)):
            _note_urls(db, session, "pane-tab", {next(iter(executed)): url}, state, now)
        _note_urls(db, session, "pane-tab", _listing(result_text, (), origins=True), state, now)
        return
    if short == "tabs_close":
        tab_id = tool_input.get("tabId")
        tab_id = str(tab_id) if isinstance(tab_id, (str, int)) else ""
        if tab_id and tab_id in set(_CHROME_CLOSED.findall(result_text)):
            _resource(db, session, call_id, "close", "pane-tab", tab_id, now)
        if _PANE_GONE in result_text:
            for owned in _open_ids(db, session, "pane-tab"):
                _resource(db, session, call_id, "close", "pane-tab", owned, now)
            state["pane_open"] = False
        return
    if short == "tabs_context":
        value = _leading_json(result_text)
        if value is None or not isinstance(value.get("tabs"), list):
            return
        listed = set() if value.get("browserOpen") is False else {
            str(tab["tabId"]) for tab in value["tabs"]
            if isinstance(tab, dict) and isinstance(tab.get("tabId"), (str, int))
        }
        for owned in _open_ids(db, session, "pane-tab") - listed:
            _resource(db, session, call_id, "close", "pane-tab", owned, now)
        state["pane_open"] = bool(listed)
        _note_urls(db, session, "pane-tab", {
            str(tab["tabId"]): tab["origin"] for tab in value["tabs"]
            if isinstance(tab, dict) and str(tab.get("tabId")) in listed
            and isinstance(tab.get("origin"), str)
        }, state, now)


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
        elif name.startswith(PANE) and name[len(PANE):] in _PANE_TRACKED:
            _observe_pane(db, session, call_id, name[len(PANE):], tool_input, text,
                          failed, state, now)
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
        _prune_marks(db, session, state)
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


def _summary(pending, kept=()):
    pending = [(row[0], row[1]) for row in pending]
    held = [value for kind, value in pending if (kind, value) in kept]
    pending = [row for row in pending if row not in kept]
    tabs = [value for kind, value in pending if kind in BLOCKING_KINDS or
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
    if held:
        parts.append("宣告保留的 Colab VM 分頁 " + ", ".join(held[:3]) + "（VM 跑完、結果核對後就關）")
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
        state = _state(db, session)
        kept = {(kind, resource_id) for kind, resource_id, opened, _ in pending
                if _kept(state, kind, resource_id, opened, now)}
        if stage == "tool":
            # 宣告保留的 VM 分頁是刻意留著的；工具階段不提醒，訊息階段才提醒用完要關。
            pending = [row for row in pending if (row[0], row[1]) not in kept]
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
            "Epitype 查到本次仍未確認收尾：" + _summary(pending, kept) +
            "。用完請只關自己建立的資源並核對工具結果；若使用者已要求停止，"
            "不要為收尾再操作，改為如實回報。"
        )


def _kept(state, kind, resource_id, opened, now):
    held = _marks(state, "kept").get(_key(kind, resource_id))
    return (isinstance(held, dict) and held.get("opened") == opened
            and isinstance(held.get("since"), (int, float))
            and now - held["since"] < KEEP_TTL_SECONDS)


def blockers(event: dict, path: Path, *, record=False):
    """Our confirmed tabs that must be closed before this non-browser call.

    Only the calling agent's own chrome-tab / pane-tab rows count. A Chrome tab
    missing from the latest group listing is out of the agent's reach (closed by
    someone else, dragged out, group dissolved, Chrome disconnected) and no longer
    blocks; a declared running-VM Colab tab is exempt until its TTL or its URL
    changes; tabs the owner released by message stop blocking. record=True counts
    each denial per resource in the ledger.
    """
    name = event.get("tool_name")
    session = event.get("session_id", event.get("sessionId"))
    if (not isinstance(name, str) or not name or is_control_tool(name)
            or name in UNBLOCKED_TOOLS or not isinstance(session, str) or not session
            or not path.is_file()):
        return []
    now = _now()
    with _database(path) as db:
        rows = [row for row in _pending(db, session, now, _agent(event))
                if row[0] in BLOCKING_KINDS]
        if not rows:
            return []
        state = _state(db, session)
        visible = state.get("chrome_visible")
        released = _marks(state, "released")
        found = [
            (kind, resource_id, opened) for kind, resource_id, opened, _ in rows
            if not (kind == "chrome-tab" and isinstance(visible, list)
                    and resource_id not in visible)
            and not _kept(state, kind, resource_id, opened, now)
            and (released.get(_key(kind, resource_id)) or {}).get("opened") != opened
        ]
        if record:
            for kind, resource_id, opened in found:
                db.execute(
                    "INSERT INTO alerts(session,stage,fingerprint,seen,count) "
                    "VALUES(?,'block',?,?,1) ON CONFLICT(session,stage,fingerprint) "
                    "DO UPDATE SET seen=excluded.seen,count=count+1",
                    (session, _fingerprint(kind, resource_id, opened), now),
                )
    return [(kind, resource_id) for kind, resource_id, _ in found]


def block_reason(rows, tool_name):
    chrome = [value for kind, value in rows if kind == "chrome-tab"]
    pane = [value for kind, value in rows if kind == "pane-tab"]

    def named(values):
        return ", ".join(values[:3]) + (" 等" if len(values) > 3 else "")

    parts = []
    if chrome:
        parts.append(f"Chrome 分頁 {named(chrome)}（用 tabs_close_mcp 關）")
    if pane:
        parts.append(f"內建瀏覽器分頁 {named(pane)}（用 tabs_close 關）")
    return (
        f"Epitype 擋下 {tool_name}：本場自建的" + "、".join(parts) + "還開著。"
        "先關掉自己建立的分頁、看到關閉結果再做別的事；之後要用再開新分頁。"
        "分頁其實已被關掉或不在群組裡時，呼叫 tabs_context_mcp／tabs_context 讓 Epitype 重新核對。"
        "若分頁正連著執行中的 Colab VM（關了 VM 會被收回），不要關：先讓 Epitype 看到它開著那本筆記本"
        "（Chrome 呼叫 tabs_context_mcp；內建瀏覽器用 navigate 打開該筆記本網址，"
        "若這次 navigate 才把窗格打開，再呼叫 tabs_context 確認它停在 Colab），再單獨送出 "
        "python -m epitype lifecycle keep <分頁ID> --authuser <N> --notebook <筆記本ID>"
        "（只向 Epitype 登記，不會實際執行）。這個 session 沒有能關分頁的瀏覽器工具時，"
        f"請使用者只回覆「{RELEASE_PHRASE}」四個字。使用者已要求停止時不要再操作，直接回報。"
    )


def keep_request(event: dict):
    """(tab, authuser, notebook) when this shell call is nothing but a keep declaration."""
    if event.get("tool_name") not in SHELL_TOOLS:
        return None
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None
    text = command.strip()
    body = text[1:] if text.startswith("&") else text
    if _SHELL_META.search(body):
        return None
    match = _KEEP_COMMAND.fullmatch(text)
    if match is None:
        return None
    return match.group("tab"), str(int(match.group("authuser"))), match.group("notebook")


def declare_keep(event: dict, path: Path, tab: str, authuser: str, notebook: str):
    """(accepted, message) for a running-VM keep declaration, checked against the ledger.

    The tab must be this agent's own open tab, and a listing seen within
    KEEP_EVIDENCE_SECONDS must show it on exactly this Colab notebook and account.
    One tab per notebook and account.
    """
    session = event.get("session_id", event.get("sessionId"))
    if not isinstance(session, str) or not session or not path.is_file():
        return False, f"Epitype 無法登記保留分頁 {tab}：本場沒有瀏覽器紀錄。"
    now = _now()
    with _database(path) as db:
        # 讀改寫同一份場次狀態：先拿寫鎖，並行的 PostToolUse 不會蓋掉這筆宣告。
        db.execute("BEGIN IMMEDIATE")
        rows = [row for row in _pending(db, session, now, _agent(event))
                if row[0] in BLOCKING_KINDS and row[1] == tab]
        if not rows:
            return False, (f"Epitype 拒絕保留分頁 {tab}：它不是本場自建且仍開著的分頁，"
                           "不能用保留宣告豁免。")
        kind, _, opened, _ = rows[0]
        key = _key(kind, tab)
        state = _state(db, session)
        seen = _marks(state, "colab").get(key)
        if not isinstance(seen, dict) or now - seen.get("seen", 0) > KEEP_EVIDENCE_SECONDS:
            return False, (f"Epitype 拒絕保留分頁 {tab}：最近 {KEEP_EVIDENCE_SECONDS // 60} 分鐘內"
                           "沒看到它開著 Colab 筆記本（/drive/…）的網址。Chrome 先呼叫 tabs_context_mcp、"
                           "內建瀏覽器先用 navigate 打開該筆記本網址，再宣告。")
        if seen.get("unconfirmed"):
            return False, (f"Epitype 拒絕保留分頁 {tab}：這次 navigate 才打開內建瀏覽器，看不出最後停在"
                           "哪個網域（可能被導去登入頁）。先呼叫 tabs_context 確認它停在 Colab，再宣告。")
        if [seen.get("authuser"), seen.get("notebook")] != [authuser, notebook]:
            return False, (f"Epitype 拒絕保留分頁 {tab}：宣告的 authuser／筆記本 ID 與分頁網址不符；"
                           "讀分頁網址核對後再宣告。")
        kept = _marks(state, "kept")
        live = {_key(k, r) for k, r, o, _ in _pending(db, session, now) if _kept(state, k, r, o, now)}
        for other in sorted(live - {key}):
            if [kept[other].get("authuser"), kept[other].get("notebook")] == [authuser, notebook]:
                return False, (f"Epitype 拒絕保留分頁 {tab}：同一台 VM 已保留分頁 "
                               f"{other.split(':', 1)[1]}，每台 VM 只留一個分頁。")
        kept[key] = {"opened": opened, "authuser": authuser, "notebook": notebook, "since": now}
        state["kept"] = kept
        _save_state(db, session, state, now)
        call_id = event.get("tool_use_id")
        db.execute(
            "INSERT INTO events(session,call_id,action,kind,resource_id,seen) VALUES(?,?,?,?,?,?)",
            (session, call_id if isinstance(call_id, str) else "keep", "keep", kind, tab, now),
        )
    return True, (f"Epitype 已登記保留分頁 {tab}（Colab authuser={authuser}、筆記本 {notebook}）"
                  "為執行中 VM 分頁：24 小時內、網址沒換就不擋。這行指令只用來登記，Epitype 已處理，"
                  "不會實際執行。VM 跑完、結果下載核對後就關掉它。")


def owner_release(event: dict, path: Path) -> list:
    """Stop blocking this session's open tabs when the owner's own message says so.

    For a session with no browser tool left to close them. The tabs stay pending
    and keep their reminders; a tab opened afterwards blocks again.
    """
    prompt = event.get("prompt")
    session = event.get("session_id", event.get("sessionId"))
    if (not isinstance(prompt, str) or _NOT_LETTERS.sub("", prompt) != RELEASE_PHRASE
            or not isinstance(session, str) or not session or not path.is_file()):
        return []
    now = _now()
    with _database(path) as db:
        db.execute("BEGIN IMMEDIATE")
        rows = [row for row in _pending(db, session, now) if row[0] in BLOCKING_KINDS]
        if not rows:
            return []
        state = _state(db, session)
        released = _marks(state, "released")
        for kind, resource_id, opened, _ in rows:
            released[_key(kind, resource_id)] = {"opened": opened}
            db.execute(
                "INSERT INTO events(session,call_id,action,kind,resource_id,seen) "
                "VALUES(?,?,?,?,?,?)", (session, "owner-release", "release", kind, resource_id, now),
            )
        state["released"] = released
        _save_state(db, session, state, now)
    return [(kind, resource_id) for kind, resource_id, _, _ in rows]


def release_notice(rows):
    return ("Epitype 依使用者指示，不再擋本場還開著的分頁 " + ", ".join(value for _, value in rows[:3])
            + (" 等" if len(rows) > 3 else "") + "；它們仍待收尾，之後能關時照樣要關。")


def kept_record(path: Path, tab: str, authuser: str, notebook: str, session=None):
    """The session that holds a live keep declaration matching these values, else None."""
    if not path.is_file():
        return None
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    now = _now()
    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as db:
        if not _columns(db, "session_state"):
            return None
        query = "SELECT session,data FROM session_state"
        rows = db.execute(query + " WHERE session=?", (session,)) if session else db.execute(query)
        for owner, data in rows:
            try:
                state = json.loads(data)
            except ValueError:
                continue
            if not isinstance(state, dict):
                continue
            for key, held in _marks(state, "kept").items():
                if (key.split(":", 1)[-1] == tab and isinstance(held, dict)
                        and [held.get("authuser"), held.get("notebook")] == [authuser, notebook]
                        and isinstance(held.get("since"), (int, float))
                        and now - held["since"] < KEEP_TTL_SECONDS):
                    return owner
    return None


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
        now = _now()
        state = _state(db, session) if _columns(db, "session_state") else {}
        pending = [
            {"kind": kind, "id": resource_id, **({"agent": agent} if agent else {}),
             **({"kept": True} if _kept(state, kind, resource_id, opened, now) else {})}
            for kind, resource_id, opened, agent in _pending(db, session, now, creator=creator)
        ]
        events = [{"action": action, "kind": kind, "id": resource_id}
                  for action, kind, resource_id in db.execute(
                      "SELECT action,kind,resource_id FROM events WHERE session=? "
                      "ORDER BY id", (session,)
                  )]
    return {"session": session, "pending": pending, "events": events}


def _keep_main(argv, path):
    """Read back a keep declaration; the PreToolUse hook is what checks and records it."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="epitype lifecycle keep",
        description="Confirm that a running-VM Colab tab exemption was recorded",
    )
    parser.add_argument("tab")
    parser.add_argument("--authuser", required=True)
    parser.add_argument("--notebook", required=True)
    parser.add_argument("--session", help="host session ID (default: any session)")
    args = parser.parse_args(argv)
    authuser = str(int(args.authuser)) if args.authuser.isdigit() else args.authuser
    owner = kept_record(path, args.tab, authuser, args.notebook, args.session)
    if owner is None:
        print("Epitype 沒有這筆保留登記。保留宣告由 PreToolUse hook 核對網址後才登記；"
              "hook 沒啟用或核對沒過時，分頁仍會擋。", file=sys.stderr)
        return 1
    print(f"Epitype 已登記保留分頁 {args.tab}（Colab authuser={authuser}、筆記本 {args.notebook}；"
          f"場次 {owner}）。VM 跑完、結果下載核對後就關掉它。")
    return 0


def main(argv=None):
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(errors="replace")
    config = Path(os.environ.get("EPITYPE_CONFIG", Path.home() / ".epitype" / "config.json"))
    if argv[:1] == ["keep"]:
        return _keep_main(argv[1:], database_path(config))
    parser = argparse.ArgumentParser(
        description="Inspect browser resources recorded for one agent session"
    )
    parser.add_argument("--session", required=True, help="host session ID")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = status(database_path(config), args.session)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(_summary([(item["kind"], item["id"]) for item in result["pending"]],
                       {(item["kind"], item["id"]) for item in result["pending"] if item.get("kept")})
              or "本次沒有待確認的瀏覽器資源")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
