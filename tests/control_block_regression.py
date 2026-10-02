"""An agent-created browser tab left open blocks the next non-browser tool call."""

import sys
sys.dont_write_bytecode = True

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from epitype import control_lifecycle as lifecycle

NOTEBOOK = "1AbCdEfGh_ijKLmn"
COLAB = f"https://colab.research.google.com/drive/{NOTEBOOK}?authuser=3"


def post(session, call_id, name, tool_input, response, agent=None):
    event = {
        "session_id": session,
        "tool_use_id": call_id,
        "hook_event_name": "PostToolUse",
        "tool_name": name,
        "tool_input": tool_input,
        "tool_response": response,
    }
    if agent:
        event["agent_id"] = agent
    return event


def pre(session, name, tool_input=None, agent=None, call_id="pre"):
    event = {"hook_event_name": "PreToolUse", "session_id": session, "tool_use_id": call_id,
             "tool_name": name, "tool_input": tool_input or {}}
    if agent:
        event["agent_id"] = agent
    return event


def mcp(*parts, error=False):
    return {"content": [{"type": "text", "text": part} for part in parts], "isError": error}


def listed(*tabs):
    """Real Chrome text listing: • tabId N: "title" ("url")."""
    lines = "".join(f'\n  • tabId {tab_id}: "{title}" ("{url}")' for tab_id, title, url in tabs)
    return "\n\nTab Context:\n- Available tabs:" + lines


OWNER_TAB = (456, "Owner page", "https://owner.example/")


def chrome_create(session, call_id, tab_id, agent=None):
    return post(session, call_id, "mcp__claude-in-chrome__tabs_create_mcp", {}, mcp(
        f"Created new tab. Tab ID: {tab_id}",
        listed(OWNER_TAB, (tab_id, "New Tab", "chrome://newtab/"))), agent)


def chrome_close(session, call_id, tab_id, *remaining):
    return post(session, call_id, "mcp__claude-in-chrome__tabs_close_mcp", {"tabId": tab_id}, mcp(
        f"Closed tab {tab_id}. {len(remaining)} tab(s) remain.", listed(*remaining)))


def chrome_context(session, call_id, *tabs):
    return post(session, call_id, "mcp__claude-in-chrome__tabs_context_mcp", {}, mcp(json.dumps({
        "availableTabs": [{"tabId": tab_id, "title": title, "url": url}
                          for tab_id, title, url in tabs],
        "tabGroupId": 9, "selectedTabId": tabs[0][0] if tabs else None})))


def keep_command(tab, authuser="3", notebook=NOTEBOOK):
    return {"command": f"python -m epitype lifecycle keep {tab} --authuser {authuser} --notebook {notebook}"}


def pane(text):
    return mcp(text)


def pane_json(value, prose):
    return mcp(json.dumps(value, indent=2) + "\n" + prose)


COLAB_ORIGIN = "https://colab.research.google.com"
NO_PREVIEW = ('No preview is open. Use preview_start with {"url": "https://…"} to open a browser tab '
              'at a URL, or with {"name": "…"} to start a dev server from .claude/launch.json.')


def _origin(url):
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0].split('?', 1)[0]}"


def pane_navigate_opening(session, call_id, url):
    """Real built-in browser navigate while the pane is closed (transcript 2026-09-10)."""
    return post(session, call_id, "mcp__Claude_Browser__navigate", {"url": url}, pane_json(
        {"serverId": "preview-local_x", "tabId": "seed", "reused": False, "type": "browser",
         "navOk": True},
        'Browser pane opened. Use serverId "preview-local_x" with read_page / computer / navigate.'))


def pane_navigate_open(session, call_id, tab_id, url):
    """Real navigate while the pane is open: only the origin, unquoted in the tab list."""
    origin = _origin(url)
    return post(session, call_id, "mcp__Claude_Browser__navigate", {"url": url}, mcp(
        f"navigated to {origin}",
        f"\n\n\nTab Context:\n- Executed on tabId: {tab_id}\n- Available tabs:\n"
        f"  • tabId {tab_id}: \"{url.split('://', 1)[1][:40]}\" ({origin})"))


class ControlBlockRegression(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="epitype-block-")
        root = Path(self.temporary.name)
        self.path = root / lifecycle.DB_FILENAME
        vault = root / "vault"
        vault.mkdir()
        config = root / "config.json"
        config.write_text(json.dumps({"vaults": [str(vault)], "budget_bytes": 4096}),
                          encoding="utf-8")
        scratch = root / "scratch"
        scratch.mkdir()
        self.environment = dict(os.environ)
        self.environment["EPITYPE_CONFIG"] = str(config)
        self.environment["TMPDIR"] = self.environment["TEMP"] = self.environment["TMP"] = str(scratch)

    def tearDown(self):
        self.temporary.cleanup()

    def hook(self, event):
        """Run the real PreToolUse/PostToolUse adapter; return its parsed output or None."""
        result = subprocess.run(
            [sys.executable, str(ROOT / "adapters" / "claude" / "pretooluse_gate.py")],
            input=json.dumps(event), capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=self.environment, cwd=ROOT, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("DEGRADED", result.stderr, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def prompt(self, session, text):
        """Run the real UserPromptSubmit adapter; return its parsed output or None."""
        result = subprocess.run(
            [sys.executable, str(ROOT / "adapters" / "claude" / "recall_hook.py")],
            input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": session,
                              "prompt": text}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=self.environment, cwd=ROOT, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("DEGRADED", result.stderr, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def decision(self, output):
        return ((output or {}).get("hookSpecificOutput") or {}).get("permissionDecision")

    def reason(self, output):
        return ((output or {}).get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")

    def context(self, output):
        return ((output or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")

    def blocked(self, session, name="Bash", agent=None):
        return lifecycle.blockers(pre(session, name, agent=agent), self.path)

    # 1. 開分頁→Bash 被擋；關分頁→放行（走真的 hook 行程）
    def test_open_tab_blocks_bash_until_its_close_is_confirmed(self):
        session = "open-close-" + uuid.uuid4().hex
        self.assertIsNone(self.hook(chrome_create(session, "c1", 101)))
        denied = self.hook(pre(session, "Bash", {"command": "git status"}))
        self.assertEqual(self.decision(denied), "deny")
        self.assertIn("Epitype 擋下 Bash", self.reason(denied))
        self.assertIn("101", self.reason(denied))
        self.assertIn("tabs_close_mcp", self.reason(denied))
        # 擋的時候仍能用瀏覽器工具與載入工具去收尾。
        for name in ("mcp__claude-in-chrome__tabs_close_mcp", "mcp__claude-in-chrome__computer",
                     "ToolSearch", "AskUserQuestion"):
            self.assertNotEqual(self.decision(self.hook(pre(session, name))), "deny", name)
        # 只呼叫 close 不算；要等到「Closed tab 101」的結果。
        self.hook(post(session, "c2", "mcp__claude-in-chrome__tabs_close_mcp", {"tabId": 101},
                       mcp("The \"tabs_close_mcp\" tool did not respond in time.", error=True)))
        self.assertEqual(self.decision(self.hook(pre(session, "Bash", {"command": "ls"}))), "deny")
        self.hook(chrome_close(session, "c3", 101, OWNER_TAB))
        allowed = self.hook(pre(session, "Bash", {"command": "git status"}))
        self.assertNotEqual(self.decision(allowed), "deny")
        self.assertEqual(lifecycle.status(self.path, session)["pending"], [])
        # 每次擋下都記在帳本，數得出擋了幾次。
        with closing(sqlite3.connect(self.path)) as db:
            count = db.execute("SELECT SUM(count) FROM alerts WHERE session=? AND stage='block'",
                               (session,)).fetchone()[0]
        self.assertEqual(count, 2)

    def test_every_non_browser_tool_is_blocked_and_reason_is_plain_chinese(self):
        session = "kinds"
        lifecycle.observe(chrome_create(session, "c1", 101), self.path)
        for name in ("Bash", "PowerShell", "Agent", "Task", "Write", "Edit", "Read", "Grep",
                     "WebFetch", "mcp__context7__query-docs"):
            self.assertEqual(self.blocked(session, name), [("chrome-tab", "101")], name)
        for name in ("mcp__claude-in-chrome__navigate", "mcp__Claude_Browser__tabs_close",
                     "mcp__computer-use__screenshot", "mcp__cua_repl__js", "ToolSearch",
                     "Skill", "AskUserQuestion", "TodoWrite", "TaskStop"):
            self.assertEqual(self.blocked(session, name), [], name)
        reason = lifecycle.block_reason([("chrome-tab", "101"), ("pane-tab", "seed")], "Write")
        self.assertIn("Epitype 擋下 Write", reason)
        self.assertIn("Chrome 分頁 101", reason)
        self.assertIn("內建瀏覽器分頁 seed", reason)
        self.assertIn("lifecycle keep", reason)

    # 2. owner 原有分頁、無法確認歸屬的分頁都不擋
    def test_owner_tabs_and_unattributable_tabs_never_block(self):
        session = "owner"
        # 清單裡的 456 不是這次建的：記都不記，更不會擋。
        lifecycle.observe(chrome_create(session, "c1", 101), self.path)
        lifecycle.observe(chrome_close(session, "c2", 101, OWNER_TAB), self.path)
        self.assertEqual(self.blocked(session), [])
        # 標準第一步 createIfEmpty 沒有先前快照，分不出是不是這次建的：只提醒、不擋。
        lifecycle.observe(post("first", "c1", "mcp__claude-in-chrome__tabs_context_mcp",
                               {"createIfEmpty": True},
                               mcp(json.dumps({"availableTabs": [{"tabId": 7, "url": "chrome://newtab/"}],
                                               "tabGroupId": 9}))), self.path)
        self.assertEqual(lifecycle.status(self.path, "first")["pending"],
                         [{"kind": "unverified-chrome", "id": "7"}])
        self.assertEqual(self.blocked("first"), [])
        self.assertIsNotNone(lifecycle.reminder(pre("first", "Bash"), self.path, "tool"))
        # 內建瀏覽器裡不是我們工具開的分頁（使用者自己開、頁面彈出）也不記。
        lifecycle.observe(post("pane-owner", "p1", "mcp__Claude_Browser__tabs_context", {},
                               pane_json({"browserOpen": True, "tabs": [
                                   {"tabId": "seed", "origin": "https://a.example", "isActive": True},
                                   {"tabId": "popup-2", "origin": "https://b.example", "isActive": False}]},
                                   "The Browser pane is currently displayed.")), self.path)
        self.assertEqual(self.blocked("pane-owner"), [])
        # 群組只有本場分頁時，群組紀錄本身不擋（混入別人分頁時不能整組關）。
        lifecycle.observe(post("group", "g1", "mcp__claude-in-chrome__tabs_context_mcp", {},
                               mcp("No tab group exists for this session.")), self.path)
        lifecycle.observe(post("group", "g2", "mcp__claude-in-chrome__tabs_context_mcp",
                               {"createIfEmpty": True},
                               mcp(json.dumps({"availableTabs": [{"tabId": 8}], "tabGroupId": 5}))),
                          self.path)
        lifecycle.observe(chrome_close("group", "g3", 8, OWNER_TAB), self.path)
        self.assertIn({"kind": "chrome-group", "id": "5"},
                      lifecycle.status(self.path, "group")["pending"])
        self.assertEqual(self.blocked("group"), [])

    def test_listing_lines_without_url_still_count(self):
        # 清單行沒有網址時照樣算在群組裡；只是不能拿來核對 Colab。
        session = "no-url"
        lifecycle.observe(post(session, "c1", "mcp__claude-in-chrome__tabs_create_mcp", {}, mcp(
            "Created new tab. Tab ID: 123",
            "Tab Context:\n- Executed on tabId: 123\n- Available tabs:\n"
            "  • tabId 456: Existing\n  • tabId 123: New Tab")), self.path)
        self.assertEqual(self.blocked(session), [("chrome-tab", "123")])
        self.assertEqual(lifecycle._listing(
            '- Available tabs:\n  • tabId 9: "a (b) (\\"c\\")" ("https://x.example/p")\n  • tabId 10: T', ()),
            {"9": "https://x.example/p", "10": None})

    def test_chrome_title_in_parentheses_is_not_a_url(self):
        listing = "\n\nTab Context:\n- Available tabs:\n  • tabId 5: Draft (v2)\n  • tabId 6: \"Doc\" (\"https://a.example/\")"
        self.assertEqual(lifecycle._listing(listing, ()), {"5": None, "6": "https://a.example/"})
        self.assertEqual(lifecycle._listing(listing, (), origins=True), {"5": "v2", "6": "https://a.example/"})

    def test_tab_out_of_reach_stops_blocking_but_stays_pending(self):
        session = "reach"
        lifecycle.observe(chrome_create(session, "c1", 101), self.path)
        self.assertEqual(self.blocked(session), [("chrome-tab", "101")])
        # 使用者手動關掉或拖出群組：清單裡沒有它了。代理碰不到，不再擋；但也不冒充已關閉。
        lifecycle.observe(chrome_context(session, "c2", OWNER_TAB), self.path)
        self.assertEqual(self.blocked(session), [])
        self.assertIn({"kind": "chrome-tab", "id": "101"},
                      lifecycle.status(self.path, session)["pending"])
        # 又出現在群組裡（被拖回來）就再擋。
        lifecycle.observe(chrome_context(session, "c3", (101, "New Tab", "chrome://newtab/")), self.path)
        self.assertEqual(self.blocked(session), [("chrome-tab", "101")])
        # Chrome 斷線：代理沒辦法關，不能卡死它。
        lifecycle.observe(post(session, "c4", "mcp__claude-in-chrome__tabs_context_mcp",
                               {"createIfEmpty": True},
                               mcp("⚠️ **Claude in Chrome is not connected**\n\nThe Chrome extension isn't reachable right now.")),
                          self.path)
        self.assertEqual(self.blocked(session), [])
        lifecycle.observe(chrome_context(session, "c5", (101, "New Tab", "chrome://newtab/")), self.path)
        self.assertEqual(self.blocked(session), [("chrome-tab", "101")])
        lifecycle.observe(post(session, "c6", "mcp__claude-in-chrome__tabs_context_mcp", {},
                               mcp("No tab group exists for this session.")), self.path)
        self.assertEqual(self.blocked(session), [])

    def test_subagent_tab_blocks_only_that_subagent(self):
        session = "split"
        lifecycle.observe(chrome_create(session, "s1", 201, agent="sub-1"), self.path)
        self.assertEqual(self.blocked(session, agent="sub-1"), [("chrome-tab", "201")])
        self.assertEqual(self.blocked(session), [])
        self.assertEqual(self.blocked(session, agent="sub-2"), [])
        # 子代理結束、分頁轉給主線：現在換主線要關。
        lifecycle.finish_subagent({"session_id": session, "agent_id": "sub-1"}, self.path)
        self.assertEqual(self.blocked(session), [("chrome-tab", "201")])

    # 3. 內建瀏覽器
    def test_built_in_browser_tabs_block_until_closed(self):
        session = "pane"
        lifecycle.observe(post(session, "p1", "mcp__Claude_Browser__preview_start",
                               {"url": "https://example.com"}, pane_json(
                                   {"serverId": "preview-local_x", "tabId": "seed", "reused": False,
                                    "type": "browser", "navOk": True},
                                   "Browser pane opened.")), self.path)
        lifecycle.observe(post(session, "p2", "mcp__Claude_Browser__tabs_create", {}, pane_json(
            {"serverId": "preview-local_x", "tabId": "tab-1", "reused": False, "type": "browser"},
            "Opened tab tab-1 in the background — the user's current tab stays in front.")), self.path)
        self.assertEqual(sorted(self.blocked(session)), [("pane-tab", "seed"), ("pane-tab", "tab-1")])
        self.assertIn("tabs_close", lifecycle.block_reason(self.blocked(session), "Bash"))
        lifecycle.observe(post(session, "p3", "mcp__Claude_Browser__tabs_close", {"tabId": "tab-1"},
                               pane("Closed tab tab-1.")), self.path)
        self.assertEqual(self.blocked(session), [("pane-tab", "seed")])
        lifecycle.observe(post(session, "p4", "mcp__Claude_Browser__tabs_close", {"tabId": "seed"}, pane(
            "Closed tab seed. That was the last tab, so the Browser pane is now closed — "
            "use `preview_start` to open it again.")), self.path)
        self.assertEqual(self.blocked(session), [])
        self.assertEqual(lifecycle.status(self.path, session)["pending"], [])
        # 內建瀏覽器關著時 navigate 回的是真實的開窗格 JSON（不是 Executed on tabId）。
        # 指定 tabId 的呼叫回「No preview is open」可能只是指錯對象，不算窗格關了。
        lifecycle.observe(pane_navigate_opening(session, "p4b", "https://a.example/"), self.path)
        lifecycle.observe(post(session, "p4c", "mcp__Claude_Browser__navigate",
                               {"tabId": "nope", "url": "https://a.example/"}, mcp(NO_PREVIEW, error=True)),
                          self.path)
        self.assertEqual(self.blocked(session), [("pane-tab", "seed")])
        lifecycle.observe(post(session, "p4d", "mcp__Claude_Browser__tabs_close", {"tabId": "seed"}, pane(
            "Closed tab seed. That was the last tab, so the Browser pane is now closed — "
            "use `preview_start` to open it again.")), self.path)
        self.assertIsNone(self.hook(pane_navigate_opening(session, "p5", "https://a.example/x")))
        self.assertEqual(self.blocked(session), [("pane-tab", "seed")])
        self.assertEqual(self.decision(self.hook(pre(session, "Bash", {"command": "ls"}))), "deny")
        # 窗格開著時 navigate 只回來源網域：沿用分頁，不新增紀錄。
        lifecycle.observe(pane_navigate_open(session, "p5b", "seed", "https://b.example/y"), self.path)
        self.assertEqual(self.blocked(session), [("pane-tab", "seed")])
        # tabs_context 是內建瀏覽器的完整清單：使用者把整個窗格關掉，清單就證明它關了。
        lifecycle.observe(post(session, "p6", "mcp__Claude_Browser__tabs_context", {}, pane_json(
            {"browserOpen": False, "tabs": []}, "The Browser pane isn't open yet, so there are no tabs.")),
            self.path)
        self.assertEqual(self.blocked(session), [])
        self.assertEqual(lifecycle.status(self.path, session)["pending"], [])
        # 沿用既有分頁（reused: true）不是這次建的。
        lifecycle.observe(post(session, "p7", "mcp__Claude_Browser__preview_start", {"url": "https://b"},
                               pane_json({"tabId": "seed", "reused": True, "type": "browser"},
                                         "Browser pane opened.")), self.path)
        self.assertEqual(lifecycle.status(self.path, session)["pending"], [])
        # 窗格沒開時工具回錯誤「No preview is open」：這本身就證明分頁都關了。
        lifecycle.observe(pane_navigate_opening(session, "p8", "https://c.example/"), self.path)
        self.assertEqual(self.blocked(session), [("pane-tab", "seed")])
        lifecycle.observe(post(session, "p9", "mcp__Claude_Browser__tabs_context", {}, mcp(
            NO_PREVIEW, error=True)), self.path)
        self.assertEqual(self.blocked(session), [])
        # 開本機檔案的 preview_start 沒有分頁 ID：只記成無法確認，不擋。
        lifecycle.observe(post(session, "p10", "mcp__Claude_Browser__preview_start",
                               {"url": "file:///C:/x/map.html"}, mcp(
                                   "opened file:///C:/x/map.html in the preview pane (files outside "
                                   "the project folder render as static snapshots)")), self.path)
        self.assertEqual(self.blocked(session), [])
        self.assertTrue(any(row["kind"] == "unverified-control"
                            for row in lifecycle.status(self.path, session)["pending"]))

    # 4. Colab 跑中 VM 宣告（可驗的豁免）
    def colab_tab(self, session, call_id="c1", tab_id=101):
        lifecycle.observe(chrome_create(session, call_id, tab_id), self.path)
        lifecycle.observe(chrome_context(session, call_id + "-ctx",
                                         (tab_id, "job.ipynb - Colab", COLAB)), self.path)

    def test_declared_running_vm_tab_is_exempt_through_the_hook(self):
        session = "colab-" + uuid.uuid4().hex
        self.assertIsNone(self.hook(chrome_create(session, "c1", 101)))
        self.hook(post(session, "c2", "mcp__claude-in-chrome__navigate",
                       {"tabId": 101, "url": COLAB},
                       mcp(f"Navigated to {COLAB}",
                           listed((101, "job.ipynb - Colab", COLAB)))))
        # 宣告那一行由 hook 登記、不放行執行，免得夾帶別的指令。
        declared = self.hook(pre(session, "Bash", keep_command(101), call_id="keep-1"))
        self.assertEqual(self.decision(declared), "deny")
        self.assertIn("已登記保留分頁 101", self.reason(declared))
        self.assertIn("不會實際執行", self.reason(declared))
        self.assertNotIn("Epitype 擋下", self.reason(declared))
        self.assertNotEqual(self.decision(self.hook(pre(session, "Bash", {"command": "git status"}))), "deny")
        self.assertEqual(lifecycle.status(self.path, session)["pending"],
                         [{"kind": "chrome-tab", "id": "101", "kept": True}])
        self.assertIn("keep", [row["action"] for row in lifecycle.status(self.path, session)["events"]])
        # 宣告的那一行指令本身讀回登記：hook 沒登記就回非 0。
        readback = subprocess.run(
            [sys.executable, "-m", "epitype", "lifecycle", "keep", "101", "--authuser", "3",
             "--notebook", NOTEBOOK], capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=self.environment, cwd=ROOT, timeout=15, check=False)
        self.assertEqual(readback.returncode, 0, readback.stderr)
        self.assertIn(session, readback.stdout)
        missing = subprocess.run(
            [sys.executable, "-m", "epitype", "lifecycle", "keep", "999", "--authuser", "3",
             "--notebook", NOTEBOOK], capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=self.environment, cwd=ROOT, timeout=15, check=False)
        self.assertEqual(missing.returncode, 1)
        # 分頁被導去別的網址：豁免作廢，再擋。
        self.hook(post(session, "c3", "mcp__claude-in-chrome__navigate",
                       {"tabId": 101, "url": "https://github.com/"},
                       mcp("Navigated to https://github.com/",
                           listed((101, "GitHub", "https://github.com/")))))
        self.assertEqual(self.decision(self.hook(pre(session, "Bash", {"command": "ls"}))), "deny")

    def test_keep_declaration_cannot_be_casual(self):
        session = "casual"
        # 不是 Colab 筆記本的分頁（例如空白 Colab 首頁）不能宣告。
        lifecycle.observe(chrome_create(session, "c1", 101), self.path)
        lifecycle.observe(chrome_context(session, "c2", (
            101, "Colab", "https://colab.research.google.com/notebooks/empty.ipynb?authuser=3")), self.path)
        accepted, message = lifecycle.declare_keep(pre(session, "Bash"), self.path, "101", "3", NOTEBOOK)
        self.assertFalse(accepted)
        self.assertIn("沒看到它開著 Colab 筆記本", message)
        # 帳號或筆記本 ID 對不上不行。
        self.colab_tab("mismatch")
        for authuser, notebook in (("0", NOTEBOOK), ("3", "1ZZZZZZZZZZZ")):
            accepted, message = lifecycle.declare_keep(pre("mismatch", "Bash"), self.path,
                                                       "101", authuser, notebook)
            self.assertFalse(accepted)
            self.assertIn("不符", message)
        # 不是本場自建的分頁（owner 的 456）不能宣告。
        accepted, message = lifecycle.declare_keep(pre("mismatch", "Bash"), self.path, "456", "3", NOTEBOOK)
        self.assertFalse(accepted)
        self.assertIn("不是本場自建", message)
        # 網址是 5 分鐘前看到的：先重新看一次。
        self.colab_tab("stale")
        later = lifecycle._now() + lifecycle.KEEP_EVIDENCE_SECONDS + 1
        with mock.patch.object(lifecycle, "_now", return_value=later):
            accepted, message = lifecycle.declare_keep(pre("stale", "Bash"), self.path, "101", "3", NOTEBOOK)
        self.assertFalse(accepted)
        self.assertIn("最近 5 分鐘", message)
        # 同一台 VM 只留一個分頁。
        self.colab_tab("twice", "a", 101)
        self.colab_tab("twice", "b", 102)
        self.assertTrue(lifecycle.declare_keep(pre("twice", "Bash"), self.path, "101", "3", NOTEBOOK)[0])
        accepted, message = lifecycle.declare_keep(pre("twice", "Bash"), self.path, "102", "3", NOTEBOOK)
        self.assertFalse(accepted)
        self.assertIn("每台 VM 只留一個分頁", message)
        self.assertEqual(self.blocked("twice"), [("chrome-tab", "102")])
        # 子代理不能替主線的分頁宣告。
        self.colab_tab("sub")
        self.assertFalse(lifecycle.declare_keep(pre("sub", "Bash", agent="sub-1"), self.path,
                                                "101", "3", NOTEBOOK)[0])

    def test_keep_command_must_stand_alone(self):
        for command in (
            f"python -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}",
            f"py -3 -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}",
            f'"C:/Program Files/Python313/python.exe" -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}',
            f"& python -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}",
            f"epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}",
        ):
            self.assertEqual(lifecycle.keep_request(pre("s", "Bash", {"command": command})),
                             ("101", "3", NOTEBOOK), command)
        for command in (
            f"python -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK} && git push",
            f"python -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}; rm -rf x",
            f"python -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK} | tee log",
            f"python -m epitype lifecycle keep 101 --authuser 3 --notebook $(cat id)",
            f"evil.exe -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}",
            f"echo python -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}",
            # PowerShell 會執行大括號裡的程式（審查實測過）。
            f".{{calc}}/python -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}",
            f'"$(calc)/python" -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}',
            f"(calc)/python -m epitype lifecycle keep 101 --authuser 3 --notebook {NOTEBOOK}",
        ):
            self.assertIsNone(lifecycle.keep_request(pre("s", "Bash", {"command": command})), command)
        self.assertIsNone(lifecycle.keep_request(pre("s", "Write", {"command": keep_command(101)["command"]})))
        # 夾帶其他指令的宣告不算宣告，照樣擋。
        session = "chained-" + uuid.uuid4().hex
        self.hook(chrome_create(session, "c1", 101))
        self.hook(chrome_context(session, "c2", (101, "job.ipynb - Colab", COLAB)))
        chained = self.hook(pre(session, "Bash", {
            "command": keep_command(101)["command"] + " && git status"}))
        self.assertEqual(self.decision(chained), "deny")
        self.assertIn("Epitype 擋下 Bash", self.reason(chained))
        rejected = self.hook(pre(session, "Bash", keep_command(101, notebook="1ZZZZZZZZZZZ")))
        self.assertEqual(self.decision(rejected), "deny")
        self.assertIn("不符", self.reason(rejected))
        # 登記了一個分頁，不能順便放過另一個還開著的。
        self.hook(chrome_create(session, "c3", 102))
        both = self.hook(pre(session, "Bash", keep_command(101)))
        self.assertEqual(self.decision(both), "deny")
        self.assertIn("已登記保留分頁 101", self.reason(both))
        self.assertIn("Epitype 擋下 Bash", self.reason(both))
        self.assertIn("102", self.reason(both))
        self.assertEqual(self.decision(self.hook(pre(session, "Bash", {"command": "ls"}))), "deny")

    def test_built_in_browser_colab_tab_is_proved_by_its_navigate(self):
        session = "pane-colab-" + uuid.uuid4().hex
        # 窗格清單只有來源網域；證據是 navigate 的網址加上結果確認導過去了。開窗格的 JSON 不說
        # 落在哪個網域（可能被導去登入頁），要等清單顯示它停在 Colab。
        self.hook(pane_navigate_opening(session, "p1", COLAB))
        early = self.hook(pre(session, "Bash", keep_command("seed"), call_id="keep-early"))
        self.assertIn("先呼叫 tabs_context", self.reason(early))
        self.hook(post(session, "p2", "mcp__Claude_Browser__tabs_context", {}, pane_json(
            {"browserOpen": True, "tabs": [{"tabId": "seed", "origin": COLAB_ORIGIN, "isActive": True}]},
            "")))
        declared = self.hook(pre(session, "Bash", keep_command("seed"), call_id="keep-p"))
        self.assertIn("已登記保留分頁 seed", self.reason(declared))
        self.assertNotEqual(self.decision(self.hook(pre(session, "Bash", {"command": "ls"}))), "deny")
        # 擋下理由叫代理呼叫 tabs_context：宣告之後再看到 Colab 網域，豁免不能因此作廢。
        self.hook(post(session, "p2b", "mcp__Claude_Browser__tabs_context", {}, pane_json(
            {"browserOpen": True, "tabs": [{"tabId": "seed", "origin": COLAB_ORIGIN, "isActive": True}]},
            "")))
        self.assertNotEqual(self.decision(self.hook(pre(session, "Bash", {"command": "ls"}))), "deny")
        # 開窗格後被導去登入頁：清單的網域不是 Colab，證據作廢。
        login = "pane-login-" + uuid.uuid4().hex
        lifecycle.observe(pane_navigate_opening(login, "l1", COLAB), self.path)
        lifecycle.observe(post(login, "l2", "mcp__Claude_Browser__tabs_context", {}, pane_json(
            {"tabs": [{"tabId": "seed", "origin": "https://accounts.google.com", "isActive": True}]},
            "")), self.path)
        self.assertIn("沒看到它開著 Colab 筆記本",
                      lifecycle.declare_keep(pre(login, "Bash"), self.path, "seed", "3", NOTEBOOK)[1])
        # 導去別的網域：豁免作廢，再擋。
        self.hook(pane_navigate_open(session, "p3", "seed", "https://x.com/somewhere"))
        self.assertEqual(self.decision(self.hook(pre(session, "Bash", {"command": "ls"}))), "deny")
        # 窗格開著時，navigate 回報落地網域對得上，才算看到筆記本網址。
        self.hook(pane_navigate_open(session, "p4", "seed", COLAB))
        self.assertTrue(lifecycle.declare_keep(pre(session, "Bash"), self.path, "seed", "3", NOTEBOOK)[0])
        other = "pane-mismatch-" + uuid.uuid4().hex
        lifecycle.observe(pane_navigate_opening(other, "q1", "https://example.com/"), self.path)
        lifecycle.observe(post(other, "q2", "mcp__Claude_Browser__navigate", {"url": COLAB}, mcp(
            "navigated to https://accounts.google.com",
            "\n\n\nTab Context:\n- Executed on tabId: seed\n- Available tabs:\n"
            "  • tabId seed: \"Sign in\" (https://accounts.google.com)")), self.path)
        accepted, message = lifecycle.declare_keep(pre(other, "Bash"), self.path, "seed", "3", NOTEBOOK)
        self.assertFalse(accepted)
        self.assertIn("沒看到它開著 Colab 筆記本", message)

    def test_owner_message_releases_tabs_a_session_cannot_close(self):
        session = "release-" + uuid.uuid4().hex
        self.hook(chrome_create(session, "c1", 101))
        self.assertIn("只回覆「放行分頁」四個字", self.reason(self.hook(pre(session, "Bash", {"command": "ls"}))))
        # 只認整則訊息就是這四個字：否定句、轉貼的段落、轉送的代理訊息都不算。
        for text in ('<agent-message from="peer">放行分頁</agent-message>', "不要放行分頁", "先別放行分頁",
                     "這個 session 沒有 Chrome 工具，放行分頁",
                     "代理說：請使用者只回覆「放行分頁」四個字。我等等自己去開 Chrome。"):
            self.prompt(session, text)
            self.assertEqual(self.decision(self.hook(pre(session, "Bash", {"command": "ls"}))), "deny", text)
        output = self.prompt(session, " 放行分頁。\n")
        self.assertIn("不再擋本場還開著的分頁 101",
                      ((output or {}).get("hookSpecificOutput") or {}).get("additionalContext", ""))
        self.assertNotEqual(self.decision(self.hook(pre(session, "Bash", {"command": "ls"}))), "deny")
        # 仍待收尾、仍會提醒；放行之後新開的分頁照樣擋。
        self.assertEqual(lifecycle.status(self.path, session)["pending"],
                         [{"kind": "chrome-tab", "id": "101"}])
        self.assertIn("release", [row["action"] for row in lifecycle.status(self.path, session)["events"]])
        self.hook(chrome_create(session, "c2", 102))
        self.assertEqual(self.blocked(session), [("chrome-tab", "102")])

    def test_keep_expires_and_reopened_id_starts_over(self):
        session = "ttl"
        self.colab_tab(session)
        self.assertTrue(lifecycle.declare_keep(pre(session, "Bash"), self.path, "101", "3", NOTEBOOK)[0])
        self.assertEqual(self.blocked(session), [])
        later = lifecycle._now() + lifecycle.KEEP_TTL_SECONDS + 1
        with mock.patch.object(lifecycle, "_now", return_value=later):
            self.assertEqual(self.blocked(session), [("chrome-tab", "101")])
        # 關掉後同一個 ID 重新開：舊宣告不沿用。
        lifecycle.observe(chrome_close(session, "x1", 101), self.path)
        lifecycle.observe(chrome_create(session, "x2", 101), self.path)
        self.assertEqual(self.blocked(session), [("chrome-tab", "101")])
        with closing(sqlite3.connect(self.path)) as db:
            state = json.loads(db.execute("SELECT data FROM session_state WHERE session=?",
                                          (session,)).fetchone()[0])
        self.assertEqual(state.get("kept"), {})

    def test_ledger_keeps_only_colab_account_and_notebook(self):
        session = "privacy"
        lifecycle.observe(chrome_create(session, "c1", 101), self.path)
        lifecycle.observe(chrome_context(session, "c2", (101, "Secret title", "https://secret.example/x?q=1"),
                                         OWNER_TAB), self.path)
        self.colab_tab(session, "c3", 102)
        data = self.path.read_bytes()
        for leaked in (b"secret.example", b"Secret title", b"owner.example", b"job.ipynb"):
            self.assertNotIn(leaked, data)
        self.assertIn(NOTEBOOK.encode(), data)

    def test_broken_ledger_fails_open(self):
        session = "broken"
        lifecycle.observe(chrome_create(session, "c1", 101), self.path)
        self.path.write_bytes(b"not a database")
        result = subprocess.run(
            [sys.executable, str(ROOT / "adapters" / "claude" / "pretooluse_gate.py")],
            input=json.dumps(pre(session, "Bash", {"command": "ls"})), capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=self.environment, cwd=ROOT,
            timeout=15, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("deny", result.stdout)
        self.assertIn("control lifecycle", result.stderr)


if __name__ == "__main__":
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
