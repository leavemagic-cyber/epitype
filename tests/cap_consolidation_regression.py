import sys
sys.dont_write_bytecode = True
"""Cap overflow reaches the existing dream pack/state/SessionStart path."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from datetime import date
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import cap_consolidation as cap, core_baseline, core_gen, dream, host_sync, memory_index, memspec
import sessionstart_hook as start
from _hook_common import isolated_temp_root, run_synthetic


class CapConsolidation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-cap-consolidation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".claude").mkdir()
        self.vault = core_gen._build_vault(self.root / "vault")
        # 先以 codex 卡 20 字建立「上次確認」的基準，再長到 200 字：變大才產整理材料。
        for host in ("claude", "codex"):
            self.vault.joinpath(host + ".md").write_text(core_gen._card_text(
                host, "resident", "host", 500, "字" * 20, None, None, hosts=(host,)), encoding="utf-8")
        base_rules, base_errors = core_gen.collect_rules([self.vault])
        self.assertFalse(base_errors)
        accepted = core_baseline.accept(self.vault, core_gen.assemble(base_rules), base_rules, "test baseline")
        for host, length in (("claude", 20), ("codex", 200)):
            self.vault.joinpath(host + ".md").write_text(core_gen._card_text(
                host, "resident", "host", 500, "字" * length, None, None, hosts=(host,)), encoding="utf-8")
        self.index = self.vault / memspec.MEMORY_INDEX_FILENAME
        self.index.write_text("# Index\n\n短條目\n這是比較長的索引條目\n", encoding="utf-8")
        rules, errors = core_gen.collect_rules([self.vault])
        self.assertFalse(errors)
        self.core_text = core_gen.assemble(rules)
        self.core = self.root / "core.md"
        self.core.write_text(self.core_text, encoding="utf-8", newline="\n")
        self.loads = core_gen.host_loads(self.core_text)
        self.growth = self.loads["codex"] - accepted["host_loads"]["codex"]
        self.assertEqual(self.loads["claude"], accepted["host_loads"]["claude"])
        self.config = {"vaults": [str(self.vault)], "core_files": [str(self.core)],
                       "dream": {"mode": "nightly", "interval_hours": 24}}
        # 小索引模擬超過宿主載入上限（真實為 200 行或 25,000 B，見 memory_index_regression）。
        limit = patch.object(memory_index, "HOST_INDEX_MAX_BYTES", 10)
        limit.start()
        self.addCleanup(limit.stop)
        self.config_path = self.home / ".epitype" / "config.json"
        self.config_path.parent.mkdir()
        self.save_config()
        environment = patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                              "EPITYPE_CONFIG": str(self.config_path)})
        environment.start()
        self.addCleanup(environment.stop)
        self.pack = self.vault / memspec.DREAM_DIRECTORY / memspec.DREAM_PACK_FILENAME
        self.json_pack = self.pack.with_name(memspec.DREAM_PACK_JSON_FILENAME)
        self.state_path = self.pack.with_name(memspec.DREAM_STATE_FILENAME)

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def section(self):
        return dream._section_caps([self.vault], date.today(), None, self.config)

    def run_dream(self):
        before = {p: p.read_bytes() for p in self.vault.glob("*.md")}
        # 完整 JSON 不再預設寫，要比對整份報告就明說 --json-out。
        self.assertEqual(dream.main(["--scheduled", "--json-out", str(self.json_pack)],
                                    output=io.StringIO()), 0)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        return json.loads(self.json_pack.read_text(encoding="utf-8"))

    def test_host_loads_reduction_and_all_sorted_source_rules(self):
        section = self.section()
        entry = next(e for e in section["examples"] if e.get("kind") == "core-rules")
        self.assertEqual(entry["host_loads"], self.loads)
        self.assertEqual(entry["reduce_bytes"], self.growth)
        self.assertEqual(entry["growth"][0]["card"], "codex.md")
        self.assertEqual(entry["growth"][0]["delta"], 180 * 3)
        self.assertEqual([h["host"] for h in entry["hosts"]], ["codex"])
        host = entry["hosts"][0]
        ranked = host["rules"]
        self.assertEqual([r["bytes"] for r in ranked], sorted((r["bytes"] for r in ranked), reverse=True))
        paths = {Path(r["card_path"]).name for r in ranked}
        self.assertIn("codex.md", paths)
        self.assertIn("floor-1.md", paths)
        self.assertNotIn("claude.md", paths)
        self.assertNotIn("situational-1.md", paths)
        self.assertEqual(host["unmapped_rules"], 0)
        self.assertEqual(sum(r["bytes"] for r in ranked) + host["overhead_bytes"], host["bytes"])
        for rule in ranked:
            self.assertEqual(rule["bytes"], len((rule["text"] + "\n").encode("utf-8")))

    def test_short_index_material_uses_raw_file_budget(self):
        entry = next(e for e in self.section()["examples"] if e.get("kind") == "index-file")
        size = self.index.stat().st_size
        self.assertEqual(entry["bytes"], size)
        self.assertEqual(entry["reduce_bytes"], size - 10)
        self.assertEqual(entry["items"][0]["text"], "這是比較長的索引條目")
        self.assertEqual(entry["items"][0]["line"], 4)
        self.assertEqual(len(entry["items"]), 2)

    def test_raw_index_counts_utf8_and_crlf(self):
        self.index.write_bytes("# Index\r\n- 短\r\n- 很長的一個條目\r\n".encode("utf-8"))
        memory_index.HOST_INDEX_MAX_BYTES = 5
        entry = next(e for e in self.section()["examples"] if e.get("kind") == "index-file")
        self.assertEqual(entry["reduce_bytes"], self.index.stat().st_size - 5)
        self.assertEqual(entry["items"][0]["line"], 3)
        self.assertEqual(entry["items"][0]["bytes"], len("- 很長的一個條目\r\n".encode("utf-8")))

    def test_drift_reports_actual_clause_without_claiming_a_current_source(self):
        drifted = self.core_text.replace("字" * 200, "舊" * 200)
        rules, _errors = core_gen.collect_rules([self.vault])
        ranked = cap.ranked_rules(drifted, "codex", rules)
        old = next(rule for rule in ranked if "舊" * 200 in rule["text"])
        self.assertIsNone(old["card_path"])
        # 手改／漂移檔的最後一條也可能沒有換行，不能憑空多算一個 byte。
        without_newline = "- Drifted clause without final newline."
        ranked = cap.ranked_rules(without_newline, "codex", [])
        self.assertEqual(ranked[0]["bytes"], len(without_newline.encode("utf-8")))

    def test_scheduled_markdown_json_and_state_deliver_same_material(self):
        report = self.run_dream()
        section = next(s for s in report["sections"] if s["id"] == 11)
        self.assertEqual(section["counts"]["over_cap"], 2)
        markdown = self.pack.read_text(encoding="utf-8")
        self.assertIn("宿主 codex 實際載入", markdown)
        self.assertIn(f"多 {self.growth} bytes", markdown)
        self.assertIn("codex.md +540", markdown)
        self.assertIn(str(self.vault / "codex.md"), markdown)
        self.assertIn("索引條目依長度降冪排列", markdown)
        self.assertIn(cap.NEXT_STEP, markdown)
        self.assertNotIn("前後對照表核准", markdown)
        self.assertNotIn("提高上限；夢不改檔", markdown)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state[cap.STATE_FIELD], cap.summary(section["examples"]))

    def test_direct_scheduled_script_uses_this_worktree_and_writes_existing_pack(self):
        result = subprocess.run([sys.executable, "epitype/dream.py", "--scheduled",
                                 "--json-out", str(self.json_pack)], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(self.json_pack.read_text(encoding="utf-8"))
        section = next(s for s in report["sections"] if s["id"] == 11)
        self.assertEqual(section["examples"][0]["kind"], "core-rules")
        self.assertTrue(section["examples"][0]["hosts"][0]["rules"])
        # 核心變大只留在報告；開場那一行歸待處理檔，夢的狀態不再帶它。
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertFalse([e for e in state.get(cap.STATE_FIELD) or () if e["kind"] == "core-rules"])

    def test_both_hosts_existing_sessionstart_entry_emits_one_action_line(self):
        self.run_dream()
        # 待處理檔是核心變大唯一的開場那一行；夢那一行只講短索引，不重講同一件事。
        self.assertEqual(core_baseline.check_vaults([self.vault], write=True)["status"], core_baseline.STATUS_GREW)
        original = self.state_path.read_bytes()
        for event in ({"source": "startup", "cwd": str(self.root)},
                      {"source": "startup", "workspace_roots": [str(self.root)]}):
            self.state_path.write_bytes(original)
            result = run_synthetic(Path(start.__file__), event, self.config_path,
                                   environment={memspec.DREAM_MODE_ENV: "nightly"})
            self.assertEqual(result.returncode, 0, result.stderr)
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            lines = [line for line in context.splitlines() if "整理材料：" in line]
            self.assertEqual(len(lines), 1)
            self.assertNotIn("常駐規則比上次確認多", lines[0])
            self.assertEqual(context.count("比上次確認多"), 1)
            self.assertIn(f"核心規則比上次確認多 {self.growth} 位元組", context)
            self.assertIn(str(self.pack) + " 第 11 節", lines[0])
            self.assertIn("短索引", lines[0])
            self.assertIn(cap.NEXT_STEP, lines[0])
            self.assertIsNone(start._dream_notice(self.vault, "startup", self.config["dream"]))

    def test_compact_off_and_truncated_output_do_not_consume_notice(self):
        self.run_dream()
        original = self.state_path.read_bytes()
        self.assertIsNone(start._dream_notice(self.vault, "compact", self.config["dream"]))
        self.assertIsNone(start._dream_notice(self.vault, "startup", {"mode": "off"}))
        self.assertEqual(original, self.state_path.read_bytes())
        with patch.object(start, "_startup_segments"), patch.object(start, "bounded_context", return_value=None):
            self.assertIsNone(start._handle({"source": "startup", "cwd": str(self.root)}, time.monotonic()))
        self.assertEqual(original, self.state_path.read_bytes())
        self.assertIn("整理材料：", start._dream_notice(self.vault, "startup", self.config["dream"]))

    def test_sync_notice_points_to_same_pack_and_does_not_block(self):
        out = io.StringIO()
        report = host_sync._report_core_growth([self.vault], self.home, out, write=False)
        self.assertEqual(report["status"], core_baseline.STATUS_GREW)
        lines = [line for line in out.getvalue().splitlines() if "core-baseline" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn(str(self.pack) + " 第 11 節", lines[0])
        self.assertIn(f"多 {self.growth} 位元組", lines[0])
        self.assertIn("codex +540", lines[0])
        self.assertIn(cap.NEXT_STEP, lines[0])
        # 不叫代理手動跑整趟夢（2026-11 實測照提示手動重跑 123 次）；補跑交給排程。
        self.assertNotIn("--scheduled", lines[0])
        self.assertNotIn("dream.py", lines[0])
        text = host_sync.rules_text([self.vault], "codex", self.home)
        plan = host_sync._plan("codex", "codex", None, self.root / "AGENTS.md",
                               lambda: {memspec.HOST_SYNC_RULES_REGION: text}, self.home)
        self.assertFalse(plan.problems)
        self.assertEqual(len(plan.regions), 1)

    def test_next_step_has_no_owner_approval_gate(self):
        # The owner is told afterwards; nothing waits on approval of the wording.
        steps = ["擬精簡稿", "Claude 與 Codex 核對意義不變", "規則管線寫入",
                 "寫入後告知 owner 改了什麼與前後 bytes"]
        positions = [cap.NEXT_STEP.find(step) for step in steps]
        self.assertNotIn(-1, positions, cap.NEXT_STEP)
        self.assertEqual(positions, sorted(positions))
        for gate in ("前後對照表", "核准後", "給 owner"):
            self.assertNotIn(gate, cap.NEXT_STEP)
        self.assertIn(cap.NEXT_STEP, dream.CAP_COMMAND)
        self.assertEqual(dream.CAP_COMMAND,
                         "依第 11 節排序清單" + cap.NEXT_STEP + "；夢不改規則卡、不提高上限")

    def test_under_or_exact_cap_has_no_material_or_notice(self):
        rules, _errors = core_gen.collect_rules([self.vault])
        core_baseline.accept(self.vault, self.core_text, rules, "test: current size accepted")
        memory_index.HOST_INDEX_MAX_BYTES = self.index.stat().st_size
        self.save_config()
        section = self.section()
        self.assertEqual(section["counts"]["over_cap"], 0)
        self.assertEqual(section["examples"], [])
        self.assertEqual(section["commands"], [])
        report = self.run_dream()
        self.assertNotIn("條文依載入長度降冪排列", self.pack.read_text(encoding="utf-8"))
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn(cap.STATE_FIELD, state)
        notice = start._dream_notice(self.vault, "startup", self.config["dream"])
        self.assertNotIn("整理材料：", notice or "")
        self.assertNotIn(cap.NEXT_STEP, "\n".join(report["next_steps"]))
        plan = host_sync._plan("codex", "codex", None, self.root / "AGENTS.md",
                               lambda: {"rules": core_gen.host_view(self.core_text, "codex")}, self.home)
        self.assertFalse(plan.notices)

    def test_ledger_holder_is_the_shared_material_destination(self):
        other = self.root / "governance"
        other.mkdir()
        other.joinpath(memspec.WORK_LEDGER_FILENAME).write_text("synthetic ledger", encoding="utf-8")
        self.config["vaults"].append(str(other))
        self.assertEqual(cap.material_path(self.config),
                         str(dream.dream_root(other) / memspec.DREAM_PACK_FILENAME) + " 第 11 節")

    def test_missing_vault_configuration_remains_a_notice_without_blocking_sync(self):
        with patch.object(memspec, "config_options", return_value={"core_cap_bytes": 10, "vaults": None}):
            plan = host_sync._plan("codex", "codex", None, self.root / "AGENTS.md",
                                   lambda: {"rules": self.core_text}, self.home)
        self.assertFalse(plan.problems)
        self.assertEqual(len(plan.regions), 1)
        self.assertIn("core_cap_bytes 已不再使用", plan.notices[0])
        self.assertIn("<治理庫>", cap.material_path({"vaults": None}))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    with isolated_temp_root():
        unittest.main()
