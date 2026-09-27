"""Post-result lifecycle coverage, including ownership and failed closure."""

import sys
sys.dont_write_bytecode = True

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
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

    def test_claude_chrome_create_close_and_remind_once(self):
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

    def test_uncertain_chrome_creation_clears_only_after_group_absence(self):
        tabs = json.dumps({"availableTabs": [{"tabId": 12}], "tabGroupId": 45})
        self.assertIn("無法", lifecycle.observe(post(
            "chrome-uncertain", "1", "mcp__claude-in-chrome__tabs_context_mcp",
            {"createIfEmpty": True}, mcp(tabs)
        ), self.path))
        self.assertIn({"kind": "unverified-chrome", "id": "1"},
                      lifecycle.status(self.path, "chrome-uncertain")["pending"])
        lifecycle.observe(post(
            "chrome-uncertain", "2", "mcp__claude-in-chrome__tabs_context_mcp",
            {}, mcp("No tab group exists for this session.")
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
        self.assertIn("無法確認", lifecycle.reminder(
            {"session_id": "one", "hook_event_name": "Stop"},
            self.path, "stop"))
        self.assertIsNone(lifecycle.reminder(
            {"session_id": "one", "hook_event_name": "Stop"},
            self.path, "stop"))

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


if __name__ == "__main__":
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
