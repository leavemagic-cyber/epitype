"""Fixed-load accounting: real files, nested costs, placement, privacy and host exports."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from epitype import fixed_load as f


class Accounting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="epitype-fixed-load-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home, self.cwd = self.root / "home", self.root / "project"
        self.cwd.mkdir()
        self.policy = f.load_policy()

    def file(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def agent(self, name="one", text="x" * 160):
        return self.file(self.home / f".claude/agents/{name}.md",
                         f"---\nname: {name}\ndescription: {text}\nmodel: selected\neffort: high\n---\nbody\n")

    def run_report(self, **kwargs):
        return f.report(home=self.home, cwd=self.cwd, policy=self.policy, **kwargs)

    def inventory(self, **changes):
        value = {"context": {"cwd": str(self.cwd), "project": f.identity(self.cwd),
                             "host_version": "test-1", "model_context": "selected-context",
                             "retention_state": "empty", "observation_version": "capture-1"},
                 "totals": {"tools": 41000, "mcp": 18000, "memory": 11000, "skills": 6000, "system": 4700},
                 "tools": [{"name": "PowerShell", "category": "tools", "state": "loaded", "tokens": 250}],
                 "retained_skill_tokens": 0}
        value.update(changes)
        return self.file(self.root / "runtime.json", json.dumps(value))

    def test_arithmetic_and_unknown_is_not_zero_success(self):
        value = self.run_report()
        self.assertEqual(value["target_total"], 49999)
        self.assertEqual(value["baseline_total"], 82559)
        self.assertEqual(value["runtime_status"], "RUNTIME_UNMEASURED")
        self.assertEqual(value["static_status"], "PARTIAL")
        self.assertIsNone(value["runtime"])

    def test_over_budget_path_scope_and_no_mutation(self):
        path = self.agent()
        before = path.read_bytes()
        value = self.run_report()
        issue = next(x for x in value["findings"] if x["code"] == "SOURCE_OVER_BUDGET")
        self.assertEqual((issue["actual"], issue["target"], issue["path"]), (40, 35, str(path)))
        self.assertTrue(any(x["code"] == "PLACEMENT_REVIEW" for x in value["findings"]))
        self.assertEqual(path.read_bytes(), before)

    def test_block_description_hidden_skill_and_conditional_body(self):
        self.file(self.cwd / ".claude/skills/large/SKILL.md",
                  "---\ndescription: >-\n  Two words\n  more words\n---\n" + "z" * 20000)
        hidden = self.file(self.cwd / ".claude/skills/manual/SKILL.md",
                           "---\ndescription: only explicit\ndisable-model-invocation: true\n---\nbody")
        value = self.run_report()
        large = next(s for s in value["sources"] if s["path"] != str(hidden))
        self.assertEqual(large["conditional_body_tokens"], 5000)
        self.assertLess(value["static_total"], 20)
        self.assertEqual(next(s for s in value["sources"] if s["path"] == str(hidden))["estimated_tokens"], 0)

    def test_alias_and_parent_runtime_are_not_double_counted(self):
        path = self.agent()
        alias = self.home / ".claude/agents/alias.md"
        try:
            alias.symlink_to(path)
        except OSError:
            self.skipTest("symlink unavailable; canonical duplicate covered below")
        self.policy["sources"] = [{"path": str(path), "kind": "agent", "category": "memory"}]
        value = self.run_report(runtime_path=self.inventory())
        self.assertEqual(len(value["sources"]), 1)
        self.assertEqual(len(value["sources"][0]["visibility_paths"]), 2)
        self.assertEqual(value["static_total"], 40)
        self.assertEqual(value["runtime"]["totals"]["tools"], 41000)

    def test_duplicate_declared_and_discovered_source(self):
        path = self.agent()
        self.policy["sources"] = [{"path": str(path), "kind": "agent", "declared_scope": "project"}]
        self.assertEqual(len(self.run_report()["sources"]), 1)

    def test_two_worktrees_do_not_qualify_as_two_projects_and_share_memory(self):
        repo, wt = self.cwd, self.root / "worktree"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture" + "@" + "example.invalid",
                        "-c", "core.hooksPath=", "commit", "--allow-empty", "-qm", "fixture"], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(f.identity(repo), f.identity(wt))
        path = self.agent()
        self.policy["sources"] = [{"path": str(path), "kind": "agent", "declared_scope": "user",
                                   "projects": [str(repo), str(wt)]}]
        self.assertTrue(any(x["code"] == "PLACEMENT_REVIEW" for x in self.run_report()["findings"]))
        from epitype.capture_route import project_slug
        index = self.file(self.home / ".claude/projects" / project_slug(str(repo)) / "memory/MEMORY.md", "index")
        other = f.report(home=self.home, cwd=wt, policy=self.policy)
        self.assertIn(str(index), [s["path"] for s in other["sources"]])
        self.assertEqual(other["memory_root"], str(repo))

    def test_slug_collision_preserves_both_projects(self):
        path = self.agent()
        projects = [str(self.root / "中文"), str(self.root / "測試")]
        self.policy["sources"] = [{"path": str(path), "kind": "agent", "projects": projects}]
        value = self.run_report()
        collision = next(x for x in value["unresolved"] if x["code"] == "AMBIGUOUS_PROJECT_SLUG")
        self.assertEqual(set(collision["paths"]), set(projects))

    def test_settings_privacy_and_toolsearch_deny_not_saving(self):
        secret = "PRIVATE_SENTINEL_SHOULD_NEVER_APPEAR"
        settings = {"env": {"TOKEN": secret}, "mcpServers": {"server": {"url": secret, "headers": {"key": secret}}},
                    "permissions": {"deny": ["ToolSearch", f"Bash({secret})"]}}
        self.file(self.home / ".claude/settings.json", json.dumps(settings))
        runtime = json.loads(self.inventory().read_text())
        runtime["tool_input"] = secret
        runtime["tools"][0]["schema"] = {"secret": secret}
        self.file(self.root / "runtime.json", json.dumps(runtime))
        value = self.run_report(runtime_path=self.root / "runtime.json")
        self.assertNotIn(secret, json.dumps(value))
        self.assertTrue(any(x["code"] == "TOOLSEARCH_DENIED_UPFRONT_MCP_RISK" for x in value["findings"]))
        self.assertEqual(value["runtime"]["totals"]["mcp"], 18000)
        self.file(self.root / "bad.json", '{"value":"' + secret)
        self.assertNotIn(secret, json.dumps(self.run_report(runtime_path=self.root / "bad.json")))

    def test_excluded_paths_are_never_opened(self):
        for name in (".credentials.json", ".codex/config.json", ".gemini/config.json", ".grok/x", "x_private/x", "Desktop/出國用/x"):
            path = self.file(self.root / name, "do not open")
            with self.assertRaises(ValueError), patch.object(Path, "read_text", side_effect=AssertionError("opened")):
                f._read(path)

    def test_retained_cost_and_required_capability_remain_nested(self):
        runtime = self.inventory(retained_skill_tokens=5000)
        value = self.run_report(runtime_path=runtime)
        self.assertEqual(value["runtime"]["retained_skill_tokens"], 5000)
        self.assertEqual(value["runtime"]["totals"]["skills"], 6000)
        self.assertEqual(value["runtime"]["tools"][0]["name"], "PowerShell")
        self.assertTrue(any(x["code"] == "CATEGORY_OVER_BUDGET" for x in value["findings"]))

    def test_compare_requires_same_host_cwd_and_retention(self):
        first = self.run_report(runtime_path=self.inventory())
        raw = json.loads(self.inventory().read_text())
        raw["totals"]["tools"] -= 1000
        self.file(self.root / "runtime.json", json.dumps(raw))
        second = self.run_report(runtime_path=self.root / "runtime.json")
        self.assertEqual(f.compare(first, second)["delta"]["tools"], -1000)
        second["runtime"]["context"]["retention_state"] = "invoked"
        self.assertEqual(f.compare(first, second)["status"], "INCOMPARABLE")

    def test_cli_failure_is_advisory_and_does_not_echo_parse_values(self):
        bad = self.file(self.root / "policy.json", "SECRET_PARSE_VALUE")
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(f.main(["--policy", str(bad)]), 0)
        self.assertIn("INCOMPLETE", stream.getvalue())
        self.assertNotIn("SECRET_PARSE_VALUE", stream.getvalue())

    def test_required_core_and_move_obligations_preserve_source(self):
        path = self.file(self.cwd / "CLAUDE.md", "<!-- EPITYPE RULES BEGIN -->\n" + "規" * 5000 + "\n<!-- EPITYPE RULES END -->")
        before = path.read_bytes()
        self.policy["sources"] = [{"path": str(path), "kind": "instruction", "move_to": str(self.root / "elsewhere"),
                                   "rollback": str(path), "author": "fixture", "purpose": "required rules"}]
        value = self.run_report()
        self.assertTrue(any(x["code"] == "CORE_OVER_BUDGET_PRESERVE_REQUIRED_RULES" for x in value["findings"]))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "elsewhere").exists())
        obligations = " ".join(value["sources"][0]["move_obligations"])
        for term in ("redirect", "reverse links", "quota", "router", "search", "consumers", "rollback"):
            self.assertIn(term, obligations)

    def test_invalid_runtime_does_not_turn_into_zero_cost_or_echo_payload(self):
        path = self.inventory()
        raw = json.loads(path.read_text())
        raw["tools"].append({**raw["tools"][0], "tokens": 999, "payload": "SECRET_SENTINEL"})
        self.file(path, json.dumps(raw))
        value = self.run_report(runtime_path=path)
        self.assertEqual(value["runtime_status"], "RUNTIME_UNMEASURED")
        self.assertIsNone(value["runtime"])
        self.assertNotIn("SECRET_SENTINEL", json.dumps(value))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
