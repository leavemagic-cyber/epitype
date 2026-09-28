"""Post-result lifecycle coverage, including ownership and failed closure."""

import sys
sys.dont_write_bytecode = True

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from epitype import control_lifecycle as lifecycle


def post(session, call_id, name, tool_input, response):
    return {
        "session_id": session,
        "tool_use_id": call_id,
        "hook_event_name": "PostToolUse",
        "tool_name": name,
        "tool_input": tool_input,
        "tool_response": response,
    }


def mcp(*parts, error=False):
    return {"content": [{"type": "text", "text": part} for part in parts],
            "isError": error}


class ControlLifecycleRegression(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="epitype-control-")
        self.path = Path(self.temporary.name) / lifecycle.DB_FILENAME

    def tearDown(self):
        self.temporary.cleanup()

    def adapter_environment(self, config_text=None):
        """A throwaway config beside self.path, so adapters write only this ledger."""
        vault = Path(self.temporary.name) / "vault"
        vault.mkdir(exist_ok=True)
        config = Path(self.temporary.name) / "config.json"
        config.write_text(config_text if config_text is not None else json.dumps(
            {"vaults": [str(vault)], "budget_bytes": 4096}), encoding="utf-8")
        environment = dict(os.environ)
        environment["EPITYPE_CONFIG"] = str(config)
        scratch = Path(self.temporary.name) / "scratch"
        scratch.mkdir(exist_ok=True)
        environment["TMPDIR"] = environment["TEMP"] = environment["TMP"] = str(scratch)
        return environment

    def run_adapter(self, adapter, event, environment):
        return subprocess.run(
            [sys.executable, str(ROOT / "adapters" / "claude" / adapter)],
            input=json.dumps(event), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=environment,
            cwd=ROOT, timeout=15, check=False,
        )

    def test_untracked_control_result_skips_config_and_ledger(self):
        # 截圖、點擊每步都觸發 PostToolUse；它們改不了帳本，不該讀設定也不該寫 SQLite。
        screenshot = post("fast", "1", "mcp__claude-in-chrome__computer",
                          {"action": "screenshot"}, mcp("ok"))
        self.assertIsNone(lifecycle.observe(screenshot, self.path))
        self.assertFalse(self.path.exists())
        self.assertFalse(lifecycle.is_tracked_tool("mcp__claude-in-chrome__navigate"))
        self.assertFalse(lifecycle.is_tracked_tool("mcp__computer-use__screenshot"))
        self.assertTrue(lifecycle.is_tracked_tool("mcp__claude-in-chrome__tabs_close_mcp"))
        self.assertTrue(lifecycle.is_tracked_tool("mcp__cua_repl__js"))
        # 設定檔壞掉時，只有真的會讀設定的呼叫才會報解析失敗：未追蹤的工具連讀都不讀。
        environment = self.adapter_environment("not json")
        skipped = self.run_adapter("pretooluse_gate.py", screenshot, environment)
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertEqual(skipped.stderr.strip(), "")
        self.assertEqual(skipped.stdout.strip(), "")
        tracked = self.run_adapter("pretooluse_gate.py", post(
            "fast", "2", "mcp__claude-in-chrome__tabs_context_mcp", {},
            mcp("No tab group exists for this session.")), environment)
        self.assertEqual(tracked.returncode, 0, tracked.stderr)
        self.assertIn("JSONDecodeError", tracked.stderr)

    def test_claude_chrome_create_close_and_remind_once(self):
        # 這條先問過「No tab group exists」才建群組，是少數情況；實際的標準第一步直接
        # createIfEmpty，見 test_real_chrome_flow_end_to_end_leaves_nothing_pending。
        self.assertIsNone(lifecycle.observe(post(
            "claude", "1", "mcp__claude-in-chrome__tabs_context_mcp", {},
            mcp("No tab group exists for this session.")
        ), self.path))
        response = json.dumps({
            "availableTabs": [{"tabId": 1693796224, "url": "https://secret.example/x"}],
            "tabGroupId": 610802618,
        })
        self.assertIsNone(lifecycle.observe(post(
            "claude", "2", "mcp__claude-in-chrome__tabs_context_mcp",
            {"createIfEmpty": True}, mcp(response)
        ), self.path))
        pending = lifecycle.status(self.path, "claude")["pending"]
        self.assertEqual({(row["kind"], row["id"]) for row in pending}, {
            ("chrome-tab", "1693796224"), ("chrome-group", "610802618")})
        away = {"session_id": "claude", "tool_name": "Read"}
        self.assertIn("1693796224", lifecycle.reminder(away, self.path, "tool"))
        self.assertIsNone(lifecycle.reminder(away, self.path, "tool"))
        self.assertIsNone(lifecycle.observe(post(
            "claude", "3", "mcp__claude-in-chrome__tabs_close_mcp",
            {"tabId": 1693796224}, mcp(
                "Closed tab 1693796224. Group is now empty (auto-removed).",
                "\n\nTab Context:\n- Available tabs:\n")
        ), self.path))
        report = lifecycle.status(self.path, "claude")
        self.assertEqual(report["pending"], [])
        self.assertEqual([row["action"] for row in report["events"]],
                         ["open", "open", "close", "close"])
        self.assertNotIn(b"secret.example", self.path.read_bytes())

    def test_uncertain_chrome_creation_clears_on_close_not_group_absence(self):
        tabs = json.dumps({"availableTabs": [{"tabId": 12}], "tabGroupId": 45})
        self.assertIn("無法", lifecycle.observe(post(
            "chrome-uncertain", "1", "mcp__claude-in-chrome__tabs_context_mcp",
            {"createIfEmpty": True}, mcp(tabs)
        ), self.path))
        # 無法歸屬的建立以候選分頁 ID 記錄，之後的「Closed tab 12」才對得上。
        self.assertIn({"kind": "unverified-chrome", "id": "12"},
                      lifecycle.status(self.path, "chrome-uncertain")["pending"])
        # 群組不見了可能是使用者解散群組，分頁仍開著：這不是關閉確認。
        lifecycle.observe(post(
            "chrome-uncertain", "2", "mcp__claude-in-chrome__tabs_context_mcp",
            {}, mcp("No tab group exists for this session.")
        ), self.path)
        self.assertEqual(lifecycle.status(self.path, "chrome-uncertain")["pending"],
                         [{"kind": "unverified-chrome", "id": "12"}])
        lifecycle.observe(post(
            "chrome-uncertain", "3", "mcp__claude-in-chrome__tabs_close_mcp",
            {"tabId": 12}, mcp("Closed tab 12.")
        ), self.path)
        self.assertEqual(lifecycle.status(self.path, "chrome-uncertain")["pending"], [])

    def test_reopened_id_gets_a_new_reminder(self):
        event = {"session_id": "reopened", "tool_name": "Read"}
        create = 'await cua.createBrowserTab("iab", "about:blank");'
        lifecycle.observe(post("reopened", "1", "mcp__cua_repl__js",
                               {"code": create}, mcp("Browser tab: 1")), self.path)
        self.assertIsNotNone(lifecycle.reminder(event, self.path, "tool", claim=False))
        self.assertIsNotNone(lifecycle.reminder(event, self.path, "tool"))
        self.assertIsNone(lifecycle.reminder(event, self.path, "tool"))
        lifecycle.observe(post("reopened", "2", "mcp__cua_repl__js",
                               {"code": 'await cua.listTabs({browser:"iab"});'},
                               mcp("[]")), self.path)
        lifecycle.observe(post("reopened", "3", "mcp__cua_repl__js",
                               {"code": create}, mcp("Browser tab: 1")), self.path)
        self.assertIsNotNone(lifecycle.reminder(event, self.path, "tool"))

    def test_cua_reset_does_not_close_tab_and_inventory_does(self):
        created = post(
            "codex", "1", "mcp__cua_repl__js",
            {"code": 'let tab = await cua.createBrowserTab("iab", "about:blank", '
                     '{ visible: false });'},
            mcp("Browser tab: 1, Title: \"about:blank\", URL: \"about:blank\".")
        )
        self.assertIsNone(lifecycle.observe(created, self.path))
        self.assertIsNone(lifecycle.observe(created, self.path))
        self.assertEqual(len(lifecycle.status(self.path, "codex")["events"]), 2)
        self.assertIsNone(lifecycle.observe(post(
            "codex", "2", "mcp__cua_repl__js_reset", {}, mcp("js kernel reset")
        ), self.path))
        self.assertEqual(lifecycle.status(self.path, "codex")["pending"],
                         [{"kind": "cua-tab:iab", "id": "1"}])
        self.assertIsNone(lifecycle.observe(post(
            "codex", "3", "mcp__cua_repl__js",
            {"code": "await tab.close();"}, mcp("")
        ), self.path))
        self.assertIn({"kind": "cua-tab:iab", "id": "1"},
                      lifecycle.status(self.path, "codex")["pending"])
        self.assertIsNone(lifecycle.observe(post(
            "codex", "4", "mcp__cua_repl__js",
            {"code": 'await cua.listTabs({ browser: "iab" });'},
            mcp("Wall time: 0.0724 seconds\nOutput: []")
        ), self.path))
        self.assertEqual(lifecycle.status(self.path, "codex")["pending"],
                         [{"kind": "cua-controller", "id": "repl"}])
        self.assertIsNone(lifecycle.observe(post(
            "codex", "5", "mcp__cua_repl__js_reset", {}, mcp("js kernel reset")
        ), self.path))
        self.assertEqual(lifecycle.status(self.path, "codex")["pending"], [])

    def test_failure_and_user_owned_tabs_do_not_clear_our_tab(self):
        lifecycle.observe(post(
            "owner", "1", "mcp__cua_repl__js",
            {"code": "await cua.getState();"},
            mcp(json.dumps({"browsers": [
                {"type": "extension", "tabs": [{"id": "user-tab",
                  "url": "https://private.example/"}]},
                {"type": "iab", "tabs": []}]}))
        ), self.path)
        self.assertEqual(lifecycle.status(self.path, "owner")["pending"],
                         [{"kind": "cua-controller", "id": "repl"}])
        lifecycle.observe(post(
            "owner", "2", "mcp__cua_repl__js",
            {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
            mcp("Browser tab: mine, Title: blank.")
        ), self.path)
        lifecycle.observe(post(
            "owner", "3", "mcp__cua_repl__js",
            {"code": 'await cua.listTabs({browser:"iab"});'},
            mcp("[]", error=True)
        ), self.path)
        self.assertIn({"kind": "cua-tab:iab", "id": "mine"},
                      lifecycle.status(self.path, "owner")["pending"])
        self.assertNotIn(b"private.example", self.path.read_bytes())

    def test_ambiguous_scoped_inventory_cannot_clear_other_browsers(self):
        lifecycle.observe(post("scope", "1", "mcp__cua_repl__js",
                               {"code": 'await cua.createBrowserTab("chrome", "about:blank");'},
                               mcp("Browser tab: chrome-1")), self.path)
        lifecycle.observe(post("scope", "2", "mcp__cua_repl__js",
                               {"code": "await cua.listTabs({ browser: selectedBrowser });"},
                               mcp("[]")), self.path)
        self.assertIn({"kind": "cua-tab:chrome", "id": "chrome-1"},
                      lifecycle.status(self.path, "scope")["pending"])

    def test_status_lists_every_pending_resource(self):
        for index in range(20):
            lifecycle.observe(post(
                "many", str(index), "mcp__cua_repl__js",
                {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
                mcp(f"Browser tab: {index}")
            ), self.path)
        report = lifecycle.status(self.path, "many")
        self.assertEqual(len([row for row in report["pending"]
                              if row["kind"] == "cua-tab:iab"]), 20)

    def test_unknown_creation_warns_and_sessions_stay_separate(self):
        warning = lifecycle.observe(post(
            "one", "1", "mcp__cua_repl__js",
            {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
            mcp("operation succeeded without resource id")
        ), self.path)
        self.assertIn("無法", warning)
        self.assertEqual(lifecycle.status(self.path, "two")["pending"], [])
        # Stop 階段的提醒已在 68438f4 拿掉（Codex 會把它顯示成使用者要處理的 Hook 反饋），
        # 這裡改走實際存在的切換工具階段。
        self.assertIn("無法確認", lifecycle.reminder(
            {"session_id": "one", "tool_name": "Read"}, self.path, "tool"))
        self.assertIsNone(lifecycle.reminder(
            {"session_id": "one", "tool_name": "Read"}, self.path, "tool"))
        with self.assertRaises(ValueError):
            lifecycle.reminder({"session_id": "one", "hook_event_name": "Stop"},
                               self.path, "stop")

    def test_parallel_distinct_results_not_lost(self):
        def one(index):
            lifecycle.observe(post(
                "parallel", str(index), "mcp__cua_repl__js",
                {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
                mcp(f"Browser tab: {index}, Title: blank.")
            ), self.path)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, range(8)))
        pending = lifecycle.status(self.path, "parallel")["pending"]
        self.assertEqual(len([row for row in pending if row["kind"] == "cua-tab:iab"]), 8)

    def test_unversioned_ledger_keeps_existing_calls(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TABLE calls (session TEXT NOT NULL, call_id TEXT NOT NULL, "
                "seen REAL NOT NULL, PRIMARY KEY(session, call_id))"
            )
            db.execute("INSERT INTO calls VALUES('legacy', 'old', 1.0)")
            db.commit()
        lifecycle.observe(post(
            "legacy", "new", "mcp__cua_repl__js",
            {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
            mcp("Browser tab: new")
        ), self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],
                             lifecycle.SCHEMA_VERSION)
            self.assertEqual(db.execute(
                "SELECT call_id FROM calls WHERE session='legacy' ORDER BY call_id"
            ).fetchall(), [("new",), ("old",)])

    def test_failed_schema_open_releases_database_file(self):
        self.path.write_bytes(b"invalid sqlite file")
        with self.assertRaises(sqlite3.DatabaseError):
            lifecycle.observe(post(
                "broken", "1", "mcp__cua_repl__js",
                {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
                mcp("Browser tab: 1")
            ), self.path)
        self.path.unlink()

    def test_tight_hook_budget_keeps_reminder_for_next_call(self):
        session = "budget-" + uuid.uuid4().hex
        vault = Path(self.temporary.name) / "vault"
        vault.mkdir()
        config = Path(self.temporary.name) / "config.json"
        config.write_text(json.dumps({"vaults": [str(vault)], "budget_bytes": 40}),
                          encoding="utf-8")
        environment = dict(os.environ)
        environment["EPITYPE_CONFIG"] = str(config)
        scratch = Path(self.temporary.name) / "scratch"
        scratch.mkdir()
        environment["TMPDIR"] = environment["TEMP"] = environment["TMP"] = str(scratch)
        lifecycle.observe(post(
            session, "1", "mcp__cua_repl__js",
            {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
            mcp("Browser tab: budget-tab")
        ), self.path)
        event = {"hook_event_name": "PreToolUse", "session_id": session,
                 "tool_name": "Read", "tool_input": {"path": "notes.txt"}}

        def invoke():
            return subprocess.run(
                [sys.executable, str(ROOT / "adapters" / "claude" / "pretooluse_gate.py")],
                input=json.dumps(event), capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=environment,
                cwd=ROOT, timeout=15, check=False,
            )

        first = invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("收尾提醒超出", first.stderr)
        self.assertNotIn("budget-tab", first.stdout)
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": session,
                  "prompt": "繼續"}
        tight_prompt = subprocess.run(
            [sys.executable, str(ROOT / "adapters" / "claude" / "recall_hook.py")],
            input=json.dumps(prompt), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=environment,
            cwd=ROOT, timeout=15, check=False,
        )
        self.assertEqual(tight_prompt.returncode, 0, tight_prompt.stderr)
        self.assertNotIn("budget-tab", tight_prompt.stdout)
        config.write_text(json.dumps({"vaults": [str(vault)], "budget_bytes": 4096}),
                          encoding="utf-8")
        second = invoke()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("budget-tab", second.stdout)
        recovered_prompt = subprocess.run(
            [sys.executable, str(ROOT / "adapters" / "claude" / "recall_hook.py")],
            input=json.dumps(prompt), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=environment,
            cwd=ROOT, timeout=15, check=False,
        )
        self.assertEqual(recovered_prompt.returncode, 0, recovered_prompt.stderr)
        self.assertIn("budget-tab", recovered_prompt.stdout)

    def test_actual_post_pre_and_stop_adapters(self):
        session = "adapter-" + uuid.uuid4().hex
        vault = Path(self.temporary.name) / "vault"
        vault.mkdir()
        config = Path(self.temporary.name) / "config.json"
        config.write_text(json.dumps({"vaults": [str(vault)], "budget_bytes": 4096}),
                          encoding="utf-8")
        environment = dict(os.environ)
        environment["EPITYPE_CONFIG"] = str(config)
        scratch = Path(self.temporary.name) / "scratch"
        scratch.mkdir()
        environment["TMPDIR"] = environment["TEMP"] = environment["TMP"] = str(scratch)

        def invoke(adapter, event):
            result = subprocess.run(
                [sys.executable, str(ROOT / "adapters" / "claude" / adapter)],
                input=json.dumps(event), capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=environment,
                cwd=ROOT, timeout=15, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("DEGRADED", result.stderr, result.stderr)
            return json.loads(result.stdout) if result.stdout.strip() else None

        created = post(
            session, "a", "mcp__cua_repl__js",
            {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
            mcp("Browser tab: test-tab, Title: blank.")
        )
        self.assertIsNone(invoke("pretooluse_gate.py", created))
        self.assertIn({"kind": "cua-tab:iab", "id": "test-tab"},
                      lifecycle.status(self.path, session)["pending"])
        pre = invoke("pretooluse_gate.py", {
            "hook_event_name": "PreToolUse", "session_id": session,
            "tool_name": "Read", "tool_input": {"path": "notes.txt"},
        })
        self.assertIn("test-tab", pre["hookSpecificOutput"]["additionalContext"])
        stop = invoke("stop_gate.py", {
            "hook_event_name": "Stop", "session_id": session,
            "stop_hook_active": False, "last_assistant_message": "完成。",
        })
        self.assertIsNone(stop)
        self.assertIn({"kind": "cua-tab:iab", "id": "test-tab"},
                      lifecycle.status(self.path, session)["pending"])
        next_prompt = invoke("recall_hook.py", {
            "hook_event_name": "UserPromptSubmit", "session_id": session,
            "prompt": "繼續",
        })
        self.assertIn("test-tab", next_prompt["hookSpecificOutput"]["additionalContext"])
        repeated_prompt = invoke("recall_hook.py", {
            "hook_event_name": "UserPromptSubmit", "session_id": session,
            "prompt": "再看看",
        })
        self.assertIn("test-tab", repeated_prompt["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(invoke("pretooluse_gate.py", post(
            session, "b", "mcp__cua_repl__js",
            {"code": 'await cua.listTabs({browser:"iab"});'}, mcp("[]")
        )))
        self.assertIsNone(invoke("pretooluse_gate.py", post(
            session, "c", "mcp__cua_repl__js_reset", {}, mcp("js kernel reset")
        )))
        self.assertEqual(lifecycle.status(self.path, session)["pending"], [])
        self.assertIsNone(invoke("stop_gate.py", {
            "hook_event_name": "Stop", "session_id": session,
            "stop_hook_active": False, "last_assistant_message": "完成。",
        }))
        closed_prompt = invoke("recall_hook.py", {
            "hook_event_name": "UserPromptSubmit", "session_id": session,
            "prompt": "下一件事",
        })
        context = (closed_prompt or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertNotIn("test-tab", context)

    def test_first_create_if_empty_candidate_clears_on_confirmed_close(self):
        # 標準第一步沒有先前快照：分不出群組是這次建的還是本來就有，只能記成無法確認，
        # 但工具回報「Closed tab X」就是關閉確認，不能讓這筆永遠掛著。
        context = json.dumps({"availableTabs": [{"tabId": 1723269028, "url": "https://a.example/"}],
                              "tabGroupId": 1299612253})
        self.assertIn("無法", lifecycle.observe(post(
            "first", "1", "mcp__claude-in-chrome__tabs_context_mcp",
            {"createIfEmpty": True}, mcp(context)
        ), self.path))
        self.assertIn({"kind": "unverified-chrome", "id": "1723269028"},
                      lifecycle.status(self.path, "first")["pending"])
        self.assertIsNone(lifecycle.observe(post(
            "first", "2", "mcp__claude-in-chrome__tabs_close_mcp", {"tabId": 1723269028},
            mcp("Closed tab 1723269028. Group is now empty (auto-removed).",
                "\n\nTab Context:\n- Available tabs:\n")
        ), self.path))
        self.assertEqual(lifecycle.status(self.path, "first")["pending"], [])
        away = {"session_id": "first", "tool_name": "Read"}
        self.assertIsNone(lifecycle.reminder(away, self.path, "tool"))
        self.assertIsNone(lifecycle.reminder(away, self.path, "prompt"))

    def test_close_type_control_tools_open_nothing(self):
        for index, name in enumerate(("mcp__Claude_Browser__tabs_close",
                                      "mcp__Claude_Browser__preview_stop",
                                      "mcp__computer-use__reset_session")):
            self.assertIsNone(lifecycle.observe(post(
                "closing", str(index), name, {"tabId": "t"}, mcp("done")
            ), self.path), name)
        self.assertEqual(lifecycle.status(self.path, "closing")["pending"], [])

    def test_unverified_control_expires_instead_of_waiting_forever(self):
        self.assertIn("無法", lifecycle.observe(post(
            "expiring", "1", "mcp__Claude_Browser__tabs_create", {}, mcp("Opened a tab")
        ), self.path))
        self.assertIn({"kind": "unverified-control", "id": "1"},
                      lifecycle.status(self.path, "expiring")["pending"])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE resources SET opened=opened-?,changed=changed-? "
                       "WHERE session='expiring'", (2 * 3600, 2 * 3600))
            db.commit()
        self.assertEqual(lifecycle.status(self.path, "expiring")["pending"], [])
        away = {"session_id": "expiring", "tool_name": "Read"}
        self.assertIsNone(lifecycle.reminder(away, self.path, "tool"))
        self.assertIsNone(lifecycle.reminder(away, self.path, "prompt"))
        # 到期不是關閉確認：事件記成 expire，不冒充 close。
        lifecycle.observe(post("expiring", "2", "mcp__cua_repl__js_reset", {},
                               mcp("js kernel reset")), self.path)
        actions = [row["action"] for row in lifecycle.status(self.path, "expiring")["events"]]
        self.assertEqual(actions, ["open", "expire"])

    def test_tool_reminder_is_once_per_resource(self):
        away = {"session_id": "dedupe", "tool_name": "Read"}
        create = 'await cua.createBrowserTab("iab", "about:blank");'
        lifecycle.observe(post("dedupe", "1", "mcp__cua_repl__js", {"code": create},
                               mcp("Browser tab: A")), self.path)
        self.assertIn("A", lifecycle.reminder(away, self.path, "tool"))
        lifecycle.observe(post("dedupe", "2", "mcp__cua_repl__js", {"code": create},
                               mcp("Browser tab: B")), self.path)
        self.assertIn("B", lifecycle.reminder(away, self.path, "tool"))
        lifecycle.observe(post("dedupe", "3", "mcp__cua_repl__js",
                               {"code": 'await cua.listTabs({browser:"iab"});'},
                               mcp(json.dumps([{"id": "B"}]))), self.path)
        self.assertEqual({row["id"] for row in lifecycle.status(self.path, "dedupe")["pending"]},
                         {"B", "repl"})
        # 只關掉 A 不是新狀況：B 與操控工作階段都已經提醒過一次。
        self.assertIsNone(lifecycle.reminder(away, self.path, "tool"))

    def test_prompt_reminders_stop_after_limit(self):
        environment = self.adapter_environment()
        session = "prompt-cap-" + uuid.uuid4().hex
        lifecycle.observe(post(
            session, "1", "mcp__cua_repl__js",
            {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
            mcp("Browser tab: capped-tab")
        ), self.path)
        seen = []
        for index in range(5):
            result = self.run_adapter("recall_hook.py", {
                "hook_event_name": "UserPromptSubmit", "session_id": session,
                "prompt": f"第 {index} 則",
            }, environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            seen.append("capped-tab" in result.stdout)
        self.assertEqual(seen, [True, True, True, False, False])
        self.assertIn({"kind": "cua-tab:iab", "id": "capped-tab"},
                      lifecycle.status(self.path, session)["pending"])

    def test_real_chrome_flow_end_to_end_leaves_nothing_pending(self):
        # 照實際紀錄的順序：第一步就是 createIfEmpty，沒有先問「No tab group exists」。
        environment = self.adapter_environment()
        session = "chrome-real-" + uuid.uuid4().hex

        def result(*parts):
            return [{"type": "text", "text": part} for part in parts]

        def tab_context(*tab_ids):
            return json.dumps({"availableTabs": [
                {"tabId": tab_id, "title": "New Tab", "url": "chrome://newtab/"}
                for tab_id in tab_ids], "tabGroupId": 1299612253})

        steps = [
            ("1", "tabs_context_mcp", {"createIfEmpty": True},
             result(tab_context(1723269028),
                    "\n\nTab Context:\n- Available tabs:\n  • tabId 1723269028: \"New Tab\"")),
            ("2", "tabs_create_mcp", {},
             result(tab_context(1723269028, 1723269031),
                    "\n\nTab Context:\n- Available tabs:\n  • tabId 1723269028\n  • tabId 1723269031")),
            ("3", "tabs_close_mcp", {"tabId": 1723269031},
             result("Closed tab 1723269031.",
                    "\n\nTab Context:\n- Available tabs:\n  • tabId 1723269028: \"New Tab\"")),
            ("4", "tabs_close_mcp", {"tabId": 1723269028},
             result("Closed tab 1723269028. Group is now empty (auto-removed).",
                    "\n\nTab Context:\n- Available tabs:\n")),
        ]
        for call_id, tool, tool_input, response in steps:
            done = self.run_adapter("pretooluse_gate.py", post(
                session, call_id, "mcp__claude-in-chrome__" + tool, tool_input, response
            ), environment)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stderr.strip(), "")
        self.assertEqual(lifecycle.status(self.path, session)["pending"], [])
        prompt = self.run_adapter("recall_hook.py", {
            "hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": "下一件事",
        }, environment)
        self.assertEqual(prompt.returncode, 0, prompt.stderr)
        self.assertNotIn("瀏覽器", prompt.stdout)
        self.assertNotIn("收尾", prompt.stdout)
        away = self.run_adapter("pretooluse_gate.py", {
            "hook_event_name": "PreToolUse", "session_id": session,
            "tool_name": "Read", "tool_input": {"path": "notes.txt"},
        }, environment)
        self.assertEqual(away.returncode, 0, away.stderr)
        self.assertNotIn("收尾", away.stdout)


    def owned_iab_tab(self, session, tab_id="keep"):
        lifecycle.observe(post(session, "create", "mcp__cua_repl__js",
                               {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
                               mcp(f"Browser tab: {tab_id}")), self.path)

    def test_filtered_or_trailing_inventory_cannot_prove_closure(self):
        # 清單被篩過、或最後一個值不是清單本身時，「清單裡沒有」證明不了「已關閉」。
        cases = [
            ('const t = await cua.listTabs({browser:"iab"}); '
             'JSON.stringify(t.filter(x => x.id !== "keep"))',
             mcp("Wall time: 0.05 seconds\nOutput: []")),
            ('JSON.stringify((await cua.listTabs({ browser: "iab" }))'
             '.filter(x => x.url.includes("x")))',
             mcp("Wall time: 0.05 seconds\nOutput: []")),
            ('await cua.listTabs({browser:"iab"}); console.log("[]");',
             mcp("Wall time: 0.05 seconds\nOutput:", json.dumps([{"id": "keep"}]), "[]")),
            ('const s = await cua.getState(); '
             'JSON.stringify({browsers: s.browsers.filter(b => b.type !== "iab")})',
             mcp("Output: " + json.dumps({"browsers": [{"type": "extension", "tabs": []}]}))),
            ('await cua.listTabs({browser:"iab"});',
             mcp("Wall time: 0.05 seconds\nOutput:", json.dumps([{"id": "keep"}]),
                 json.dumps([{"id": "other"}]))),
        ]
        for index, (code, response) in enumerate(cases):
            session = f"filtered-{index}"
            self.owned_iab_tab(session)
            lifecycle.observe(post(session, "list", "mcp__cua_repl__js", {"code": code},
                                   response), self.path)
            self.assertIn({"kind": "cua-tab:iab", "id": "keep"},
                          lifecycle.status(self.path, session)["pending"], code)

    def test_bare_final_inventory_still_proves_closure(self):
        for index, code in enumerate((
            "await tab.close(); await cua.listTabs({browser:'iab'});",
            'globalThis.tabsNow = await cua.listTabs({ browser: "iab" })',
            "await cua.getState();",
        )):
            session = f"bare-{index}"
            self.owned_iab_tab(session)
            output = (json.dumps({"browsers": [{"type": "iab", "tabs": []}]})
                      if "getState" in code else "[]")
            lifecycle.observe(post(session, "list", "mcp__cua_repl__js", {"code": code},
                                   mcp("Wall time: 0.05 seconds\nOutput:", output)), self.path)
            self.assertEqual(lifecycle.status(self.path, session)["pending"],
                             [{"kind": "cua-controller", "id": "repl"}], code)

    def test_chrome_tab_missing_from_group_is_not_closed(self):
        # Chrome 的清單只列群組內分頁：拖出群組或群組解散都會讓它「不在清單」，分頁卻還開著。
        lifecycle.observe(post("drag", "1", "mcp__claude-in-chrome__tabs_context_mcp", {},
                               mcp("No tab group exists for this session.")), self.path)
        lifecycle.observe(post("drag", "2", "mcp__claude-in-chrome__tabs_context_mcp",
                               {"createIfEmpty": True},
                               mcp(json.dumps({"availableTabs": [{"tabId": 1}],
                                               "tabGroupId": 9}))), self.path)
        lifecycle.observe(post("drag", "3", "mcp__claude-in-chrome__tabs_create_mcp", {},
                               mcp(json.dumps({"availableTabs": [{"tabId": 1}, {"tabId": 2}],
                                               "tabGroupId": 9}))), self.path)
        lifecycle.observe(post("drag", "4", "mcp__claude-in-chrome__tabs_context_mcp", {},
                               mcp(json.dumps({"availableTabs": [{"tabId": 1}],
                                               "tabGroupId": 9}))), self.path)
        self.assertIn({"kind": "chrome-tab", "id": "2"},
                      lifecycle.status(self.path, "drag")["pending"])
        lifecycle.observe(post("drag", "5", "mcp__claude-in-chrome__tabs_close_mcp",
                               {"tabId": 1}, mcp("Closed tab 1.", json.dumps(
                                   {"availableTabs": [], "tabGroupId": 9}))), self.path)
        self.assertEqual(lifecycle.status(self.path, "drag")["pending"],
                         [{"kind": "chrome-group", "id": "9"}, {"kind": "chrome-tab", "id": "2"}])
        lifecycle.observe(post("drag", "6", "mcp__claude-in-chrome__tabs_context_mcp", {},
                               mcp("No tab group exists for this session.")), self.path)
        # 群組不見只證明群組沒了；被拖出去的分頁 2 仍待確認。
        self.assertEqual(lifecycle.status(self.path, "drag")["pending"],
                         [{"kind": "chrome-tab", "id": "2"}])

    def test_subagent_cannot_take_the_main_thread_reminder(self):
        # 子代理拿到主線的提醒，會照著「用完請關」去關主線的分頁；主線自己反而收不到。
        self.owned_iab_tab("shared", "main-tab")
        subagent = {"session_id": "shared", "agent_id": "sub-1", "tool_name": "Read"}
        main = {"session_id": "shared", "tool_name": "Read"}
        self.assertIsNone(lifecycle.reminder(subagent, self.path, "tool"))
        self.assertIn("main-tab", lifecycle.reminder(main, self.path, "tool"))

    def test_subagent_resources_remind_only_that_subagent(self):
        created = post("split", "sub-create", "mcp__cua_repl__js",
                       {"code": 'await cua.createBrowserTab("iab", "about:blank");'},
                       mcp("Browser tab: sub-tab"))
        created["agent_id"] = "sub-1"
        lifecycle.observe(created, self.path)
        main = {"session_id": "split", "tool_name": "Read"}
        other = {"session_id": "split", "agent_id": "sub-2", "tool_name": "Read"}
        owner = {"session_id": "split", "agent_id": "sub-1", "tool_name": "Read"}
        self.assertIsNone(lifecycle.reminder(main, self.path, "tool"))
        self.assertIsNone(lifecycle.reminder({"session_id": "split", "prompt": "x"},
                                             self.path, "prompt"))
        self.assertIsNone(lifecycle.reminder(other, self.path, "tool"))
        self.assertIn("sub-tab", lifecycle.reminder(owner, self.path, "tool"))
        self.assertIn({"kind": "cua-tab:iab", "id": "sub-tab", "agent": "sub-1"},
                      lifecycle.status(self.path, "split")["pending"])

    def test_ledger_prunes_old_rows_and_keeps_recent_ones(self):
        # 正式帳本 25 小時累積兩千多列 calls：帳本要有保留期限，不能無限長大。
        day = 24 * 3600
        start = 1_000_000.0

        def at(moment, session, call_id, code, output):
            with mock.patch.object(lifecycle, "_now", return_value=moment):
                lifecycle.observe(post(session, call_id, "mcp__cua_repl__js",
                                       {"code": code}, mcp(output)), self.path)

        create = 'await cua.createBrowserTab("iab", "about:blank");'
        listing = 'await cua.listTabs({browser:"iab"});'
        at(start, "stale", "1", create, "Browser tab: old")
        at(start + 60, "stale", "2", listing, "Wall time: 0.01 seconds\nOutput: []")

        def count(table, session=None):
            with closing(sqlite3.connect(self.path)) as db:
                if session is None:
                    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                return db.execute(f"SELECT count(*) FROM {table} WHERE session=?",
                                  (session,)).fetchone()[0]

        self.assertEqual(count("calls", "stale"), 2)
        at(start + 2 * day, "fresh", "a", create, "Browser tab: new")
        # 去重只需要短時間：一天前的呼叫紀錄先清掉，事件與資源仍保留供查詢。
        self.assertEqual(count("calls", "stale"), 0)
        self.assertEqual(count("events", "stale"), 3)
        self.assertEqual(count("resources", "stale"), 2)
        at(start + 8 * day, "fresh", "b", listing,
           "Wall time: 0.01 seconds\nOutput: " + json.dumps([{"id": "new"}]))
        for table in ("calls", "events", "resources", "session_state", "alerts"):
            self.assertEqual(count(table, "stale"), 0, table)
        self.assertEqual(count("calls", "fresh"), 1)
        self.assertIn({"kind": "cua-tab:iab", "id": "new"},
                      lifecycle.status(self.path, "fresh")["pending"])
        # 清理最多每小時一次：緊接著的呼叫不重跑。
        with closing(sqlite3.connect(self.path)) as db:
            last = db.execute("SELECT value FROM meta WHERE key='last_cleanup'").fetchone()[0]
        at(start + 8 * day + 60, "fresh", "c", listing,
           "Wall time: 0.01 seconds\nOutput: " + json.dumps([{"id": "new"}]))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT value FROM meta WHERE key='last_cleanup'").fetchone()[0], last)
        self.assertEqual(last, start + 8 * day)

    def test_doctor_post_tool_use_probe_reaches_the_ledger(self):
        # 健康檢查若用未追蹤的工具當探針，觀察器一開頭就回去，PASS 什麼也證明不了。
        from install import graft

        self.assertEqual(graft.CONTROL_LEDGER_FILENAME, lifecycle.DB_FILENAME)
        self.assertTrue(lifecycle.is_tracked_tool(graft._POSTTOOLUSE_PROBE["tool_name"]))
        home = Path(self.temporary.name) / "home"
        hooks = home / graft.CONFIG_DIRECTORY / graft.HOOK_DIRECTORY
        hooks.mkdir(parents=True)
        for name, payload in graft._shim_payloads(ROOT).items():
            (hooks / name).write_bytes(payload)
        (home / graft.CONFIG_DIRECTORY / "config.json").write_text(json.dumps(
            {"repo_root": str(ROOT), "vaults": [str(home)]}), encoding="utf-8")
        report = io.StringIO()
        graft._synthetic_health(home, ROOT, report)
        self.assertIn("HOOK PostToolUse: PASS", report.getvalue())
        untracked = dict(graft._POSTTOOLUSE_PROBE, tool_name="SyntheticRead")
        report = io.StringIO()
        with mock.patch.object(graft, "_POSTTOOLUSE_PROBE", untracked):
            graft._synthetic_health(home, ROOT, report)
        self.assertIn("HOOK PostToolUse: FAIL", report.getvalue())
        self.assertIn("REASON PostToolUse: ledger-not-written", report.getvalue())

    def test_v1_ledger_upgrade_expires_unclearable_records_once(self):
        # v1 留下的無法確認紀錄（以呼叫 ID 為鍵、或來自 close 類工具）依新規則永遠清不掉；
        # 升版只在 hook 自然開帳本時處理一次，唯讀的查詢指令不得升版。
        opened = time.time() - 60
        with closing(sqlite3.connect(self.path)) as db:
            for statement in (
                "CREATE TABLE calls (session TEXT NOT NULL, call_id TEXT NOT NULL, "
                "seen REAL NOT NULL, PRIMARY KEY(session, call_id))",
                "CREATE TABLE resources (session TEXT NOT NULL, kind TEXT NOT NULL, "
                "resource_id TEXT NOT NULL, status TEXT NOT NULL, opened REAL NOT NULL, "
                "changed REAL NOT NULL, PRIMARY KEY(session, kind, resource_id))",
                "CREATE TABLE events (id INTEGER PRIMARY KEY, session TEXT NOT NULL, "
                "call_id TEXT NOT NULL, action TEXT NOT NULL, kind TEXT NOT NULL, "
                "resource_id TEXT NOT NULL, seen REAL NOT NULL)",
                "CREATE TABLE session_state (session TEXT PRIMARY KEY, data TEXT NOT NULL)",
                "CREATE TABLE alerts (session TEXT NOT NULL, stage TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, seen REAL NOT NULL, "
                "PRIMARY KEY(session, stage, fingerprint))",
            ):
                db.execute(statement)
            db.executemany("INSERT INTO resources VALUES(?,?,?,?,?,?)", [
                ("old", "unverified-control", "toolu_close", "open", opened, opened),
                ("old", "unverified-chrome", "call-9", "open", opened, opened + 1),
                ("old", "cua-tab:iab", "7", "open", opened, opened + 2),
                ("old", "cua-tab:iab", "6", "closed", opened, opened + 3),
            ])
            db.execute("INSERT INTO alerts VALUES('old','tool','[[\"cua-tab:iab\"]]',?)",
                       (opened,))
            db.execute("INSERT INTO session_state VALUES('old','{}')")
            db.execute("PRAGMA user_version=1")
            db.commit()
        before = lifecycle.status(self.path, "old")
        self.assertEqual({(row["kind"], row["id"]) for row in before["pending"]}, {
            ("unverified-control", "toolu_close"), ("unverified-chrome", "call-9"),
            ("cua-tab:iab", "7")})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        note = lifecycle.reminder({"session_id": "old", "tool_name": "Read"}, self.path, "tool")
        self.assertIn("7", note)
        self.assertNotIn("無法確認", note)
        after = lifecycle.status(self.path, "old")
        self.assertEqual(after["pending"], [{"kind": "cua-tab:iab", "id": "7"}])
        self.assertEqual(sorted(row["action"] for row in after["events"]), ["expire", "expire"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],
                             lifecycle.SCHEMA_VERSION)
            self.assertEqual(db.execute(
                "SELECT status FROM resources WHERE resource_id='toolu_close'").fetchone()[0],
                "expired")
            self.assertEqual(db.execute(
                "SELECT count(*) FROM alerts WHERE fingerprint LIKE '[[%'").fetchone()[0], 0)

if __name__ == "__main__":
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
