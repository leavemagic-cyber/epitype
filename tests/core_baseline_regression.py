"""Core-rule size ratchet (owner 2026-11-17): growth vs the last accepted baseline, never a cap."""
import sys
sys.dont_write_bytecode = True
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import core_baseline as cb, core_gen, host_sync, memspec
import sessionstart_hook as start
from _hook_common import isolated_temp_root

GROWN_TEXT = "新增的合成常駐規則，用來確認變大時會被點名。"


class CoreBaseline(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-core-baseline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = core_gen._build_vault(self.root / "vault")
        self.out = self.root / "core.md"
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.config = {"vaults": [str(self.vault)], "dream": {"mode": "off"}}
        self.config_path = self.home / ".epitype" / "config.json"
        self.config_path.parent.mkdir()
        self.save_config()
        environment = patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                              "EPITYPE_CONFIG": str(self.config_path)})
        environment.start()
        self.addCleanup(environment.stop)

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def generate(self, **kwargs):
        result = core_gen.generate([self.vault], self.out, config=kwargs.pop("config", self.config), **kwargs)
        self.assertIn(result["status"], (core_gen.STATUS_WRITTEN, core_gen.STATUS_UNCHANGED), result.get("errors"))
        return result

    def grow(self):
        (self.vault / "grown.md").write_text(core_gen._card_text(
            "grown", "resident", "alpha", 130, GROWN_TEXT, None, None), encoding="utf-8", newline="\n")

    def baseline(self):
        return json.loads(cb.baseline_path(self.vault).read_text(encoding="utf-8"))

    def test_first_run_initialises_the_baseline_without_a_notice(self):
        result = self.generate()
        self.assertEqual(result["notices"], [])
        base = self.baseline()
        self.assertEqual(base["reason"], cb.INITIAL_REASON)
        self.assertEqual(base["host_loads"], core_gen.host_loads(self.out.read_text(encoding="utf-8")))
        self.assertEqual(base["sha256"], __import__("hashlib").sha256(self.out.read_bytes()).hexdigest())
        self.assertTrue(base["cards"] and base["accepted_at"] and base["accepted_by"])
        self.assertFalse(cb.pending_path(self.vault).exists())
        self.assertIsNone(cb.surface_line(self.vault))

    def test_growth_names_each_card_and_delta_writes_pending_and_still_writes_rules(self):
        self.generate()
        before = self.baseline()
        self.grow()
        result = self.generate()
        self.assertIn(GROWN_TEXT, self.out.read_text(encoding="utf-8"))  # never blocked
        report = result["baseline"]
        self.assertEqual(report["status"], cb.STATUS_GREW)
        grown = [item for item in report["growth"] if item["card"] == "grown.md"]
        self.assertEqual(len(grown), 1)
        self.assertEqual(grown[0]["delta"], len(GROWN_TEXT.encode("utf-8")))
        self.assertTrue(grown[0]["added"])
        self.assertEqual(grown[0]["approved_by"], "synthetic-pair")
        self.assertIn("grown +", result["notices"][-1])
        self.assertIn("epitype core-baseline --accept --reason", result["notices"][-1])
        pending = json.loads(cb.pending_path(self.vault).read_text(encoding="utf-8"))
        self.assertEqual(pending["growth"][0]["card"], "grown.md")
        self.assertTrue(all(delta > 0 for delta in pending["over"].values()))
        self.assertEqual(self.baseline(), before)  # growth never moves the baseline by itself
        # One extra byte counts: there is no tolerance.
        (self.vault / "grown.md").write_text(core_gen._card_text(
            "grown", "resident", "alpha", 130, GROWN_TEXT + "x", None, None), encoding="utf-8", newline="\n")
        again = self.generate()["baseline"]
        self.assertEqual(again["status"], cb.STATUS_GREW)

    def test_shrinking_back_updates_the_baseline_and_clears_pending(self):
        self.generate()
        self.grow()
        self.generate()
        self.assertTrue(cb.pending_path(self.vault).exists())
        (self.vault / "grown.md").unlink()
        (self.vault / "resident-a1.md").write_text(core_gen._card_text(
            "resident-a1", "resident", "alpha", 110, "短。", None, None), encoding="utf-8", newline="\n")
        result = self.generate()
        self.assertEqual(result["baseline"]["status"], cb.STATUS_SAME_OR_SMALLER)
        self.assertFalse(cb.pending_path(self.vault).exists())
        self.assertEqual(self.baseline()["host_loads"], core_gen.host_loads(self.out.read_text(encoding="utf-8")))
        self.assertEqual(self.baseline()["accepted_by"], cb.AUTO_BY)

    def test_dry_run_reports_but_writes_no_baseline(self):
        result = core_gen.generate([self.vault], self.out, config=self.config, dry_run=True)
        self.assertEqual(result["baseline"]["status"], cb.STATUS_INITIALIZED)
        self.assertFalse(cb.baseline_path(self.vault).exists())

    def test_accept_requires_a_reason_and_then_clears_pending(self):
        self.generate()
        self.grow()
        self.generate()
        out = io.StringIO()
        self.assertEqual(cb.main([str(self.vault), "--accept"], out), 2)
        self.assertEqual(cb.main([str(self.vault), "--accept", "--reason", "   "], out), 2)
        self.assertIn("理由", out.getvalue())
        with self.assertRaises(ValueError):
            cb.accept(self.vault, "", [], "")
        self.assertTrue(cb.pending_path(self.vault).exists())
        status = io.StringIO()
        self.assertEqual(cb.main([str(self.vault)], status), 0)
        self.assertTrue(status.getvalue().startswith("PENDING"))
        out = io.StringIO()
        self.assertEqual(cb.main([str(self.vault), "--accept", "--reason", "新規則每場都要", "--by", "owner"], out), 0)
        self.assertFalse(cb.pending_path(self.vault).exists())
        base = self.baseline()
        self.assertEqual((base["reason"], base["accepted_by"]), ("新規則每場都要", "owner"))
        self.assertIn("grown.md", base["cards"])
        self.assertEqual(self.generate()["notices"], [])

    def test_sessionstart_shows_the_pending_line_once_and_not_on_compact(self):
        self.generate()
        self.grow()
        self.generate()
        line = cb.surface_line(self.vault)
        self.assertTrue(line.startswith("核心規則比上次確認多"))
        value = start._handle({"source": "startup", "cwd": str(self.root)}, time.monotonic())
        context = value["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context.count("核心規則比上次確認多"), 1)
        value = start._handle({"source": "compact", "cwd": str(self.root)}, time.monotonic())
        context = (value or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertNotIn("核心規則比上次確認多", context)

    def test_old_core_cap_key_is_parsed_and_only_gives_a_notice(self):
        self.config[memspec.CONFIG_CORE_CAP_BYTES_FIELD] = 10
        self.save_config()
        result = self.generate(config=self.config)
        self.assertFalse(result["over_cap"])
        self.assertIsNone(result["cap"])
        self.assertTrue(any("core_cap_bytes 已不再使用" in n for n in result["notices"]))
        plan = host_sync.plan_for("claude", [self.vault], home=self.home)
        self.assertFalse(plan.problems)
        self.assertTrue(any("core_cap_bytes 已不再使用" in n for n in plan.notices))
        self.assertFalse(any("上限" in n and "常駐規則實際載入" in n for n in plan.notices))

    def test_sync_reports_growth_once_writes_rules_and_check_writes_nothing(self):
        out = io.StringIO()
        host_sync.apply([self.vault], hosts=["claude"], home=self.home, output=out)
        self.assertTrue(cb.baseline_path(self.vault).exists())
        self.assertNotIn("NOTE   core-baseline", out.getvalue())
        self.grow()
        out = io.StringIO()
        host_sync.check([self.vault], hosts=["claude"], home=self.home, output=out)
        self.assertEqual(out.getvalue().count("核心規則比上次確認多"), 1)
        self.assertFalse(cb.pending_path(self.vault).exists())
        out = io.StringIO()
        host_sync.apply([self.vault], hosts=["claude"], home=self.home, output=out)
        self.assertEqual(out.getvalue().count("核心規則比上次確認多"), 1)
        self.assertIn(GROWN_TEXT, (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertTrue(cb.pending_path(self.vault).exists())


    def test_one_view_two_vaults_core_gen_sync_and_accept_never_flap(self):
        # Auditor repro (flap.py): core-gen over [governance, project] stored the combined view,
        # sync lowered it to the governance view, and every later core-gen reported growth.
        project = core_gen._build_vault(self.root / "project")
        (project / "extra.md").write_text(core_gen._card_text(
            "extra", "resident", "alpha", 140, "專案庫的另一條常駐規則。", None, None), encoding="utf-8", newline="\n")
        self.config["vaults"] = [str(self.vault), str(project)]
        self.save_config()
        first = self.generate_over([self.vault, project])
        self.assertEqual(first["baseline"]["status"], cb.STATUS_INITIALIZED)
        loads = self.baseline()["host_loads"]
        host_sync.apply([self.vault, project], hosts=["claude"], home=self.home, output=io.StringIO())
        self.assertEqual(self.baseline()["host_loads"], loads)
        self.assertEqual(self.generate_over([self.vault, project])["baseline"]["status"], cb.STATUS_SAME_OR_SMALLER)
        self.assertEqual(cb.main(["--accept", "--reason", "x"], io.StringIO()), 0)
        self.assertEqual(self.baseline()["host_loads"], loads)
        self.assertEqual(self.generate_over([self.vault, project])["baseline"]["status"], cb.STATUS_SAME_OR_SMALLER)
        self.assertFalse(cb.pending_path(self.vault).exists())
        self.assertFalse(cb.baseline_path(project).exists())  # a project vault never gets its own
        # core-gen over the project vault alone measures nothing and writes nothing.
        self.assertIsNone(self.generate_over([project])["baseline"])
        self.assertFalse(cb.baseline_path(project).exists())
        out = io.StringIO()
        self.assertEqual(cb.main([str(project), "--accept", "--reason", "x"], out), 2)
        self.assertIn("治理庫", out.getvalue())
        self.assertFalse(cb.baseline_path(project).exists())

    def generate_over(self, vaults):
        result = core_gen.generate(vaults, self.out, config=self.config)
        self.assertIn(result["status"], (core_gen.STATUS_WRITTEN, core_gen.STATUS_UNCHANGED), result.get("errors"))
        return result

    def test_corrupt_or_deleted_baseline_is_reported_not_silently_reinitialised(self):
        self.generate()
        self.grow()
        self.generate()
        pending = cb.pending_path(self.vault).read_bytes()
        cb.baseline_path(self.vault).write_text("{", encoding="utf-8")
        result = self.generate()
        self.assertEqual(result["baseline"]["status"], cb.STATUS_BROKEN)
        self.assertIn("不自動重建", result["notices"][-1])
        self.assertEqual(cb.pending_path(self.vault).read_bytes(), pending)  # kept as it was
        self.assertEqual(cb.baseline_path(self.vault).read_text(encoding="utf-8"), "{")
        cb.baseline_path(self.vault).unlink()
        self.assertEqual(self.generate()["baseline"]["status"], cb.STATUS_BROKEN)
        self.assertFalse(cb.baseline_path(self.vault).exists())
        self.assertTrue(cb.pending_path(self.vault).exists())
        # Corrupt with no pending: still not a first run; a pending line is written to surface it.
        self.assertEqual(cb.main(["--accept", "--reason", "看過了，接受"], io.StringIO()), 0)
        cb.baseline_path(self.vault).write_text("[]", encoding="utf-8")
        self.assertEqual(self.generate()["baseline"]["status"], cb.STATUS_BROKEN)
        self.assertIn("讀不懂", cb.surface_line(self.vault))
        status = io.StringIO()
        cb.main([], status)
        self.assertTrue(status.getvalue().startswith("PENDING"))
        self.assertEqual(cb.main(["--accept", "--reason", "重新確認"], io.StringIO()), 0)
        self.assertFalse(cb.pending_path(self.vault).exists())
        self.assertEqual(self.generate()["baseline"]["status"], cb.STATUS_SAME_OR_SMALLER)
        self.assertFalse(list(cb.baseline_path(self.vault).parent.glob("*.tmp")))  # atomic writes leave no staging

    def test_sessionstart_reads_pending_where_writers_put_it_not_the_ledger_vault(self):
        other = core_gen._build_vault(self.root / "ledger")
        (other / memspec.WORK_LEDGER_FILENAME).write_text("# ledger\n", encoding="utf-8")
        self.config["vaults"] = [str(self.vault), str(other)]
        self.save_config()
        self.generate()
        self.grow()
        self.generate()
        self.assertTrue(cb.pending_path(self.vault).exists())
        self.assertEqual(cb.governance_vault(), self.vault)
        value = start._handle({"source": "startup", "cwd": str(self.root)}, time.monotonic())
        context = value["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context.count("核心規則比上次確認多"), 1)
        # global_vault moves every writer and the reader together.
        self.config["global_vault"] = str(other)
        self.save_config()
        self.assertEqual(cb.governance_vault(), other.resolve())
        self.assertIsNone(self.generate_over([self.vault])["baseline"])

if __name__ == "__main__":
    with isolated_temp_root():
        unittest.main(argv=[sys.argv[0]])
