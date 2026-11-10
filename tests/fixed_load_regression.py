"""Fixed-load accounting: real files, nested costs, placement, privacy and host exports."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from epitype import fixed_load as f
from _fixed_load_fixtures import isolate_catalogs


class Accounting(unittest.TestCase):
    def test_native_user_and_project_skill_roots_are_discovered_and_watched(self):
        user = self.file(self.home / '.agents/skills/shared/SKILL.md', '---\nname: shared\ndescription: collaboration\n---\nbody')
        project = self.file(self.cwd / '.agents/skills/local/SKILL.md', '---\nname: local\ndescription: local workflow\n---\nbody')
        value = self.run_report()
        paths = {s['path'] for s in value['sources']}
        self.assertIn(str(user), paths)
        self.assertIn(str(project), paths)
        watched = {row['path'] for row in value['catalog_directories']}
        self.assertIn(str(user.parent), watched)
        self.assertIn(str(project.parent), watched)

    def test_native_skill_root_does_not_load_unrelated_agent_or_instruction_files(self):
        self.file(self.home / '.agents/CLAUDE.md', 'not a native skill')
        self.file(self.home / '.agents/agents/unrelated.md', '---\ndescription: not a native agent catalog\n---\n')
        self.assertEqual(self.run_report()['sources'], [])

    def test_shared_skill_alias_is_counted_once_with_both_visibility_paths(self):
        target = self.file(self.home / '.agents/skills/shared/SKILL.md', '---\nname: shared\ndescription: shared source\n---\nbody').parent
        alias = self.home / '.claude/skills/shared'
        alias.parent.mkdir(parents=True)
        self.assertTrue(target.resolve().is_relative_to(self.root))
        self.assertTrue(alias.resolve().is_relative_to(self.root))
        if sys.platform == 'win32':
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            p = subprocess.run(['powershell','-NoProfile','-Command',
                f'New-Item -ItemType Junction -Path {quote(alias)} -Target {quote(target)} | Out-Null'],
                capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(p.returncode,0,p.stderr)
        else:
            alias.symlink_to(target,target_is_directory=True)
        sources = self.run_report()['sources']
        self.assertEqual(len(sources),1)
        self.assertEqual(set(sources[0]['visibility_paths']), {str(target/'SKILL.md'),str(alias/'SKILL.md')})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="epitype-fixed-load-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        catalogs = isolate_catalogs(self.root)
        catalogs.start()
        self.addCleanup(catalogs.stop)
        self.home, self.cwd = self.root / "home", self.root / "project"
        home = patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        home.start()
        self.addCleanup(home.stop)
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

    def test_local_exclusions_match_casefolded_whole_path_parts(self):
        self.file(self.home / '.epitype/fixed_load_exclude.txt', '  SaMpLe-HiDdEn  \n\n')
        hidden = self.file(self.home / '.agents/skills/sample-hidden/SKILL.md', 'excluded')
        visible = self.file(self.home / '.agents/skills/sample-hidden-extra/SKILL.md', 'visible')
        with self.assertRaisesRegex(ValueError, 'excluded source'):
            f._read(hidden)
        self.assertEqual(f._read(visible), 'visible')
        sources = f.discover(self.home, self.cwd, [], [])
        self.assertEqual([row['path'] for row in sources], [str(visible)])
        for name in ('.credentials.json', '.codex', '.gemini', '.grok', 'sample_PRIVATE'):
            self.assertFalse(f.allowed(self.root / name / 'notes.md'))

    def test_missing_local_exclusions_are_empty(self):
        visible = self.file(self.home / '.agents/skills/sample-visible/SKILL.md', 'visible')
        self.assertFalse((self.home / '.epitype/fixed_load_exclude.txt').exists())
        self.assertEqual(f._read(visible), 'visible')

    def test_invalid_or_unreadable_local_exclusions_do_not_break_reads(self):
        visible = self.file(self.root / 'sample-visible.md', 'visible')
        config = self.file(self.home / '.epitype/fixed_load_exclude.txt', '')
        config.write_bytes(b'\xff\xfe\xff')
        self.assertEqual(f._read(visible), 'visible')
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == config:
                raise PermissionError('synthetic unreadable list')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            self.assertEqual(f._read(visible), 'visible')
            self.assertFalse(f.allowed(self.root / '.codex' / 'notes.md'))

    def test_arithmetic_and_unknown_is_not_zero_success(self):
        value = self.run_report()
        self.assertEqual(value["target_total"], 49999)
        self.assertEqual(value["baseline_total"], 82559)
        self.assertEqual(value["runtime_status"], "RUNTIME_UNMEASURED")
        self.assertEqual(value["static_status"], "PARTIAL")
        self.assertIsNone(value["runtime"])

    def test_configured_placement_threshold_and_compatibility_source(self):
        path = self.agent()
        row = {"path": str(path), "kind": "agent", "physical_scope": "user",
               "declared_scope": "user", "projects": [str(self.cwd), str(self.root / "second")]}
        for minimum, expected in ((2, False), (3, True)):
            self.policy["placement"]["promotion_min_distinct_projects"] = minimum
            source, findings = f._source(row, self.policy)
            self.assertEqual(any(x["code"] == "PLACEMENT_REVIEW" for x in findings), expected)
            self.assertEqual(source["placement_minimum"], minimum)
            self.assertEqual(source["placement_minimum_source"], "configured")
        self.policy["placement"].pop("promotion_min_distinct_projects")
        source, _ = f._source(row, self.policy)
        self.assertEqual(source["placement_minimum_source"], "compatibility-default")
        self.assertEqual(source["placement_minimum"], 2)

    def test_invalid_explicit_placement_minimum_is_not_silently_replaced(self):
        for minimum in (0, -1, True, "3", None, 2.5):
            self.policy["placement"]["promotion_min_distinct_projects"] = minimum
            with self.assertRaises(ValueError):
                self.run_report()
            path = self.file(self.root / "invalid-policy.json", json.dumps(self.policy))
            with self.assertRaises(ValueError):
                f.load_policy(path)

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
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
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
        self.file(self.home / '.claude.json', json.dumps({'projects': {
            repo.as_posix(): {'mcpServers': {'root': {}}},
            str(wt): {'mcpServers': {'worktree': {}}},
            str(self.root): {'mcpServers': {'unrelated_ancestor': {}}}}}))
        root_report = self.run_report()
        worktree_report = f.report(home=self.home, cwd=wt, policy=self.policy)
        self.assertEqual(root_report['settings'][0]['project_server_count'], 1)
        self.assertEqual(worktree_report['settings'][0]['project_server_count'], 2)

    def test_slug_collision_preserves_both_projects(self):
        path = self.agent()
        projects = [str(self.root / "中文"), str(self.root / "測試")]
        self.policy["sources"] = [{"path": str(path), "kind": "agent", "projects": projects}]
        value = self.run_report()
        collision = next(x for x in value["unresolved"] if x["code"] == "AMBIGUOUS_PROJECT_SLUG")
        self.assertEqual(set(collision["paths"]), set(projects))

    def test_user_and_current_project_mcp_registrations_are_counted_without_payloads(self):
        secret = 'NEVER_EXPORT_MCP_CREDENTIAL'
        self.file(self.home / '.claude.json', json.dumps({
            'oauthAccount': {'private': secret},
            'mcpServers': {'global': {'type': 'http', 'url': secret, 'headers': {'key': secret}}},
            'projects': {str(self.cwd): {'mcpServers': {'local': {'command': secret, 'env': {'KEY': secret}}}},
                         str(self.root / 'other'): {'mcpServers': {'foreign': {'url': secret}}}}}))
        value = self.run_report()
        source = next(row for row in value['settings'] if row['path'] == str(self.home / '.claude.json'))
        self.assertEqual((source['user_server_count'], source['project_server_count'], source['server_count']), (1, 1, 2))
        self.assertNotIn(secret, json.dumps(value))
        self.assertNotIn(str(self.root / 'other'), json.dumps(source))
        self.assertIn('MCP_DEFINITIONS_UNMEASURED', {row['code'] for row in value['unresolved']})
        self.assertEqual(value['runtime_status'], 'RUNTIME_UNMEASURED')

    def test_invalid_claude_profile_does_not_report_zero_or_reveal_parser_input(self):
        self.file(self.home / '.claude.json', '{"PRIVATE_MCP_PARSE_SENTINEL":')
        value = self.run_report()
        source = next(row for row in value['settings'] if row['path'] == str(self.home / '.claude.json'))
        self.assertIsNone(source['server_count'])
        self.assertIn('MCP_SETTINGS_UNREADABLE_OR_INVALID', {row['code'] for row in value['unresolved']})
        self.assertNotIn('PRIVATE_MCP_PARSE_SENTINEL', json.dumps(value))

    def test_invalid_mcp_shapes_are_unknown_but_foreign_project_shapes_are_ignored(self):
        for payload in ({'mcpServers': []}, {'mcpServers': {'bad': 'PRIVATE_SHAPE'}},
                        {'projects': []}, {'projects': {str(self.cwd): {'mcpServers': []}}}):
            with self.subTest(payload=payload):
                self.file(self.home / '.claude.json', json.dumps(payload))
                value = self.run_report()
                self.assertIsNone(value['settings'][0]['server_count'])
                self.assertIn('MCP_SETTINGS_UNREADABLE_OR_INVALID', {x['code'] for x in value['unresolved']})
                self.assertNotIn('PRIVATE_SHAPE', json.dumps(value))
        self.file(self.home / '.claude.json', json.dumps({'projects': {str(self.root / 'other'): 'PRIVATE_SHAPE'}}))
        value = self.run_report()
        self.assertEqual(value['settings'][0]['server_count'], 0)
        self.assertNotIn('MCP_SETTINGS_UNREADABLE_OR_INVALID', {x['code'] for x in value['unresolved']})

    def test_foreign_project_paths_are_not_resolved_or_opened(self):
        foreign = self.root / 'offline-foreign'
        profile = self.file(self.home / '.claude.json', json.dumps({'projects': {
            str(foreign): {'mcpServers': {'foreign': {}}}, str(self.cwd): {'mcpServers': {'local': {}}}}}))
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == foreign:
                raise AssertionError('foreign path was accessed')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'resolve', resolve):
            row = f._mcp_metadata(profile, self.cwd, self.cwd)
        self.assertEqual(row['status'], 'PRESENT')
        self.assertEqual(row['project_server_count'], 1)

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
        for name in (".credentials.json", ".codex/config.json", ".gemini/config.json", ".grok/x", "x_private/x", "Desktop/credentials_private/x"):
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

    def test_crlf_index_budget_counts_original_file_bytes(self):
        from epitype import memory_index
        vault = self.root / "vault"
        path = self.file(vault / "MEMORY.md", "")
        path.write_bytes(b"# Index\r\n" + b"- x\r\n" * 310)
        value = self.run_report(vaults=[vault])
        issue = next(x for x in value["findings"] if x["code"] == "INDEX_OVER_BUDGET")
        self.assertEqual(issue["actual"], path.stat().st_size)
        self.assertEqual(memory_index.lint(vault, {})["status"], "over-cap")

    def test_isolation_preserves_synthetic_parent_inheritance(self):
        parent = self.file(self.root / ".claude/agents/parent.md", "---\ndescription: parent\n---\n")
        settings = self.file(self.root / ".claude/settings.json", "{}")
        value = self.run_report()
        self.assertEqual([s["path"] for s in value["sources"]], [str(parent)])
        self.assertEqual([s["path"] for s in value["settings"]], [str(self.home / '.claude.json'), str(settings)])
        self.assertEqual(value['settings'][0]['status'], 'ABSENT')
        self.assertEqual(value['settings'][0]['server_count'], 0)
        for row in value["catalog_directories"]:
            path = Path(row["path"])
            self.assertTrue(self.root in path.parents)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
