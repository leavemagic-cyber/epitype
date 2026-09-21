import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""專案庫的規則要送到專案根，不然 Codex 進到那個專案等於沒有規則。

2026-09-20 實測事故：一場 Codex 在 `C:\\projects\\自主賺錢方案` 跑，讀不到那個專案的規則
卡。Codex 開場只讀全域 `~/.codex/AGENTS.md`（只收治理庫）與專案根的 `AGENTS.md`，而那個
專案根沒有 AGENTS.md。Claude Code 沒出事只是因為宿主自己會載入 cwd 專案庫的 MEMORY.md。

這裡驗的是那條送達路徑：專案根反查（靠對話紀錄的 cwd，不是名冊）、兩個檔的內容分工、
舊標記就地換、使用者原有的位元組不動、拿得回來。
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epitype import capture_route, host_sync, memspec

RULE_CARD = (
    "---\nname: rule-{stem}\ndescription: 2026-09-21 {stem}\nlayer: resident\n"
    "section: execution\norder: 10\ntext: {text}\ndecided_by: owner-explicit\n"
    "approved_by: owner\napproved_at: 2026-09-21\naliases: [{stem}]\n"
    "metadata:\n  type: rule\n---\nbody\n"
)
PROJECT_RULE_TEXT = "Ship nothing from this project without a receipt."
RULES_BEGIN, RULES_END = memspec.HOST_SYNC_PROJECT_MARKERS[
    memspec.HOST_SYNC_PROJECT_RULES_REGION]
INDEX_BEGIN, INDEX_END = memspec.HOST_SYNC_PROJECT_MARKERS[
    memspec.HOST_SYNC_PROJECT_INDEX_REGION]
LEGACY_RULES_BEGIN, LEGACY_RULES_END = memspec.HOST_SYNC_PROJECT_LEGACY_MARKERS[
    memspec.HOST_SYNC_PROJECT_RULES_REGION]
LEGACY_INDEX_BEGIN, LEGACY_INDEX_END = memspec.HOST_SYNC_PROJECT_LEGACY_MARKERS[
    memspec.HOST_SYNC_PROJECT_INDEX_REGION]


@contextlib.contextmanager
def _fake_home(home):
    """整段期間 Path.home() 指向假家：這一支會呼叫 apply，真機的契約檔碰不得。"""
    saved = {name: os.environ.get(name) for name in ("USERPROFILE", "HOME")}
    os.environ["USERPROFILE"] = os.fspath(home)
    os.environ["HOME"] = os.fspath(home)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class ProjectSync(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="epitype-projsync-")).resolve()
        self.addCleanup(self._cleanup)
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir(parents=True)
        # 治理庫：拿著工作帳本的那一個，規則走全域宿主檔。
        self.governance = self.root / "governance"
        self.governance.mkdir()
        (self.governance / memspec.WORK_LEDGER_FILENAME).write_text("# 帳本\n", encoding="utf-8")
        (self.governance / "rule-floor.md").write_text(
            RULE_CARD.format(stem="floor", text="Honesty governs this contract."),
            encoding="utf-8")
        (self.governance / memspec.HOST_SYNC_INDEX_FILENAME).write_text(
            "# 通用入口\n\n- [一張卡](a.md)\n", encoding="utf-8")

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def _project(self, name, cwd=None, cards=True, index=True, transcript=True):
        """一個專案根＋宿主替它開的原生庫（可選：規則卡、MEMORY.md、對話紀錄）。"""
        project = self.root / name
        project.mkdir(parents=True, exist_ok=True)
        vault = (self.home / ".claude" / "projects"
                 / capture_route.project_slug(project) / "memory")
        vault.mkdir(parents=True)
        if cards:
            (vault / "rule-receipt.md").write_text(
                RULE_CARD.format(stem="receipt", text=PROJECT_RULE_TEXT), encoding="utf-8")
        if index:
            (vault / memspec.HOST_SYNC_INDEX_FILENAME).write_text(
                f"# {name} 入口\n\n- [收據](receipt.md)\n", encoding="utf-8")
        if transcript:
            (vault.parent / "session.jsonl").write_text(
                json.dumps({"type": "user", "cwd": os.fspath(cwd if cwd is not None else project)})
                + "\n", encoding="utf-8")
        return project, vault

    def _apply(self, vaults):
        report = io.StringIO()
        with _fake_home(self.home):
            code = host_sync.apply(vaults, home=self.home, output=report)
        return code, report.getvalue()

    def _check(self, vaults):
        report = io.StringIO()
        with _fake_home(self.home):
            code = host_sync.check(vaults, home=self.home, output=report)
        return code, report.getvalue()

    # (a) 有規則卡＋MEMORY.md：AGENTS.md 兩塊、CLAUDE.md 只有規則塊
    def test_a_project_gets_rules_in_both_files_and_index_only_in_agents(self):
        project, vault = self._project("money")
        code, report = self._apply([self.governance, vault])
        self.assertEqual(code, host_sync.EXIT_OK, report)
        agents = (project / "AGENTS.md").read_text(encoding="utf-8")
        claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(PROJECT_RULE_TEXT, agents)
        self.assertIn(PROJECT_RULE_TEXT, claude)
        self.assertIn("- [收據](receipt.md)", agents)
        # 索引塊只在 AGENTS.md：Claude Code 自己會載入專案庫的 MEMORY.md。
        self.assertNotIn(INDEX_BEGIN, claude)
        self.assertNotIn("- [收據](receipt.md)", claude)
        # MEMORY.md 的第一行標題不跟著進去（同全域索引的做法）。
        self.assertNotIn("# money 入口", agents)
        # 治理庫的規則不會混進專案檔，專案規則也不會混進全域宿主檔。
        self.assertNotIn("Honesty governs this contract.", agents)
        self.assertNotIn(
            PROJECT_RULE_TEXT,
            host_sync.host_path("codex", self.home).read_text(encoding="utf-8"))
        for text in (agents, claude):
            self.assertEqual(text.count(RULES_BEGIN), 1)
            self.assertEqual(text.count(RULES_END), 1)

    # (b) 只有 MEMORY.md、沒有規則卡：AGENTS.md 只有索引塊，CLAUDE.md 根本不建
    def test_a_vault_without_rule_cards_writes_only_the_index_and_no_claude_file(self):
        project, vault = self._project("notes", cards=False)
        code, report = self._apply([self.governance, vault])
        self.assertEqual(code, host_sync.EXIT_OK, report)
        agents = (project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(INDEX_BEGIN, agents)
        self.assertNotIn(RULES_BEGIN, agents)
        self.assertFalse((project / "CLAUDE.md").exists(),
                         "沒有規則卡就沒有東西要寫進 CLAUDE.md，不該建一個空殼檔")

    # (c) 治理庫不產生專案目標
    def test_the_governance_vault_never_gets_a_project_target(self):
        project, vault = self._project("money")
        # 治理庫本身就長在原生專案庫的位置時也一樣：它的規則走全域宿主檔。
        native_governance = (self.home / ".claude" / "projects" / "C--gov" / "memory")
        native_governance.mkdir(parents=True)
        (native_governance / memspec.WORK_LEDGER_FILENAME).write_text("# 帳本\n", encoding="utf-8")
        (native_governance / "session.jsonl".replace("session", "s")).write_text(
            json.dumps({"cwd": os.fspath(self.root)}) + "\n", encoding="utf-8")
        targets, _skipped = host_sync.project_targets(
            [native_governance, vault], home=self.home)
        self.assertEqual({os.fspath(item[2]) for item in targets}, {os.fspath(vault)})

    # (d) 對話紀錄無 cwd 或 cwd 目錄不存在 → 跳過並印 SKIP
    def test_a_vault_whose_project_root_cannot_be_found_is_skipped_out_loud(self):
        gone = self.root / "gone"
        _project, vault = self._project("gone", cwd=gone)
        gone.rmdir()
        _blind, blind_vault = self._project("blind", transcript=False)
        code, report = self._check([self.governance, vault, blind_vault])
        self.assertIn(f"SKIP   project {vault}: {host_sync.PROJECT_SKIP_NO_ROOT}", report)
        self.assertIn(f"SKIP   project {blind_vault}: {host_sync.PROJECT_SKIP_NO_ROOT}", report)
        self.assertNotEqual(code, host_sync.EXIT_REFUSED)
        targets, skipped = host_sync.project_targets(
            [self.governance, vault, blind_vault], home=self.home)
        self.assertEqual(targets, [])
        self.assertEqual(len(skipped), 2)

    # (e) 舊的 titan 標記就地換成產品標記，只留一組
    def test_the_private_script_markers_are_replaced_in_place(self):
        project, vault = self._project("titan")
        (project / "AGENTS.md").write_text(
            "# titan\n\n我自己的開頭\n\n"
            f"{LEGACY_RULES_BEGIN}\n舊的規則字\n{LEGACY_RULES_END}\n\n"
            f"{LEGACY_INDEX_BEGIN}\n舊的索引字\n{LEGACY_INDEX_END}\n",
            encoding="utf-8")
        code, report = self._apply([self.governance, vault])
        self.assertEqual(code, host_sync.EXIT_OK, report)
        agents = (project / "AGENTS.md").read_text(encoding="utf-8")
        for marker in (LEGACY_RULES_BEGIN, LEGACY_RULES_END,
                       LEGACY_INDEX_BEGIN, LEGACY_INDEX_END, "舊的規則字", "舊的索引字"):
            self.assertNotIn(marker, agents)
        self.assertEqual(agents.count(RULES_BEGIN), 1)
        self.assertEqual(agents.count(INDEX_BEGIN), 1)
        self.assertIn("我自己的開頭", agents)
        self.assertIn(PROJECT_RULE_TEXT, agents)

    # (f)(g) 使用者原有的字逐位元組留著、第一次寫前留備份、再跑一次不動檔
    def test_the_users_own_bytes_survive_and_a_second_apply_changes_nothing(self):
        project, vault = self._project("money")
        own = "# 我的專案\n\n這幾行是我自己寫的。\n"
        agents_path = project / "AGENTS.md"
        agents_path.write_text(own, encoding="utf-8")
        own_bytes = agents_path.read_bytes()
        code, report = self._apply([self.governance, vault])
        self.assertEqual(code, host_sync.EXIT_OK, report)
        after = agents_path.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(own.rstrip("\n")), after[:120])
        backup = agents_path.with_name(agents_path.name + memspec.HOST_SYNC_BACKUP_SUFFIX)
        self.assertEqual(backup.read_bytes(), own_bytes,
                         "第一次寫之前要留一份原檔的位元組副本，且不經任何正規化")
        before_bytes = agents_path.read_bytes()
        code, second = self._apply([self.governance, vault])
        self.assertEqual(code, host_sync.EXIT_OK, second)
        self.assertIn("已經一致", second)
        self.assertEqual(agents_path.read_bytes(), before_bytes,
                         "沒有東西要改的時候不該重寫檔案")
        self.assertEqual(self._check([self.governance, vault])[0], host_sync.EXIT_OK)

    # (h) remove 拿掉兩塊，其餘不變；我們從無到有建的檔才刪得掉
    def test_remove_takes_the_blocks_out_and_leaves_the_rest_alone(self):
        project, vault = self._project("money")
        own = "# 我的專案\n\n這幾行是我自己寫的。\n"
        agents_path = project / "AGENTS.md"
        agents_path.write_text(own, encoding="utf-8")
        own_bytes = agents_path.read_bytes()
        # 原本就有、但只有空白的檔：拿掉區塊之後剩空白，仍然是使用者的檔，不刪。
        blank_project, blank_vault = self._project("blank")
        blank_agents = blank_project / "AGENTS.md"
        blank_agents.write_text("\n", encoding="utf-8")
        self._apply([self.governance, vault, blank_vault])
        report = io.StringIO()
        with _fake_home(self.home):
            host_sync.remove(home=self.home, output=report,
                             vaults=[self.governance, vault, blank_vault])
        agents = agents_path.read_text(encoding="utf-8")
        for marker in (RULES_BEGIN, RULES_END, INDEX_BEGIN, INDEX_END, PROJECT_RULE_TEXT):
            self.assertNotIn(marker, agents)
        self.assertEqual(agents_path.read_bytes(), own_bytes, "其餘內容要逐位元組留著")
        # CLAUDE.md 是我們從無到有建的（使用者從沒有過這個檔）：拿掉區塊就沒東西了，刪掉。
        claude_path = project / "CLAUDE.md"
        self.assertFalse(claude_path.exists(), "我們自己建的孤兒檔不該留在別人的專案根")
        self.assertTrue(blank_agents.is_file(), "使用者原本就有的檔，剩空白也不刪")
        self.assertIn("REMOVED project:money/AGENTS.md", report.getvalue())
        self.assertIn("REMOVED project:money/CLAUDE.md", report.getvalue())

    # 祖先判斷看的是所有裝著卡的庫反查得出的根，不是只有這次產生出來的目標
    def test_a_vault_with_cards_below_blocks_the_root_above(self):
        above, above_vault = self._project("work")
        below, below_vault = self._project(
            os.path.join("work", "child"), cards=False, index=False)
        # 底下那個庫裝著卡，但沒有規則卡也沒有 MEMORY.md，自己不會成為目標；上層照樣不
        # 准寫——保護不該取決於底下那個庫這次剛好有沒有東西要寫。
        (below_vault / "note.md").write_text(
            "---\nname: note\ndescription: 2026-09-21 一張不是規則的卡\n---\nbody\n",
            encoding="utf-8")
        code, report = self._apply([self.governance, above_vault])
        self.assertEqual(code, host_sync.EXIT_OK, report)
        self.assertIn(f"SKIP   project {above}: 是其他專案根", report)
        self.assertIn(os.fspath(below), report)
        self.assertFalse((above / "AGENTS.md").exists())
        self.assertFalse((above / "CLAUDE.md").exists())

    # 空殼不是庫：專案自己的子資料夾被開過一場，不該讓那個專案的根永遠寫不進去
    def test_an_empty_shell_below_does_not_block_the_root_above(self):
        above, above_vault = self._project("work")
        self._project(os.path.join("work", "child"), cards=False, index=False)
        code, report = self._apply([self.governance, above_vault])
        self.assertEqual(code, host_sync.EXIT_OK, report)
        self.assertNotIn("SKIP", report)
        self.assertIn(PROJECT_RULE_TEXT, (above / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue((above / "CLAUDE.md").is_file())

    # 兩個庫指到同一個專案根：全部跳過，不對同一個檔產生兩組目標
    def test_two_vaults_pointing_at_one_root_are_all_skipped(self):
        same, same_vault = self._project("same")
        _other, other_vault = self._project("other", cwd=same)
        _top, top_vault = self._project("top", cwd=self.root)
        vaults = [self.governance, same_vault, other_vault, top_vault]
        code, report = self._apply(vaults)
        self.assertEqual(code, host_sync.EXIT_OK, report)
        self.assertIn(f"SKIP   project {same}: 有 2 個庫指到同一個專案根", report)
        for vault in (same_vault, other_vault):
            self.assertIn(os.fspath(vault), report)
        self.assertIn("--audit", report)
        self.assertFalse((same / "AGENTS.md").exists())
        self.assertFalse((same / "CLAUDE.md").exists())
        # 同根那一組被剔除，不代表它們的根不算數：上層目錄照樣要被祖先規則擋下。
        self.assertIn(f"SKIP   project {self.root}: 是其他專案根", report)
        self.assertFalse((self.root / "AGENTS.md").exists())
        targets, skipped = host_sync.project_targets(vaults, home=self.home)
        self.assertEqual(targets, [])
        self.assertEqual(sorted(os.fspath(item[0]) for item in skipped),
                         sorted([os.fspath(same), os.fspath(self.root)]))

    # 通案：專案根是另一個專案根的上層目錄就整個跳過（兩個宿主都會往上讀祖先目錄）
    def test_a_project_root_above_another_one_is_skipped(self):
        above, above_vault = self._project("work")
        below, below_vault = self._project(os.path.join("work", "child"))
        code, report = self._apply([self.governance, above_vault, below_vault])
        self.assertEqual(code, host_sync.EXIT_OK, report)
        self.assertIn(f"SKIP   project {above}: ", report)
        self.assertIn("是其他專案根", report)
        self.assertIn(os.fspath(below), report)
        self.assertFalse((above / "AGENTS.md").exists(),
                         "寫進上層會被底下每個專案一起載入")
        self.assertFalse((above / "CLAUDE.md").exists())
        self.assertIn(PROJECT_RULE_TEXT, (below / "AGENTS.md").read_text(encoding="utf-8"))
        targets, skipped = host_sync.project_targets(
            [self.governance, above_vault, below_vault], home=self.home)
        self.assertEqual({os.fspath(item[1]) for item in targets}, {os.fspath(below)})
        self.assertEqual([os.fspath(item[0]) for item in skipped], [os.fspath(above)])

    # (i) 要寫的內容自己含標記 → 拒寫，一個位元組都不動
    def test_content_carrying_a_marker_is_refused(self):
        project, vault = self._project("money")
        (vault / memspec.HOST_SYNC_INDEX_FILENAME).write_text(
            f"# money 入口\n\n{RULES_BEGIN}\n", encoding="utf-8")
        code, report = self._apply([self.governance, vault])
        self.assertEqual(code, host_sync.EXIT_REFUSED, report)
        self.assertIn("REFUSE project:money/AGENTS.md", report)
        agents = project / "AGENTS.md"
        # 規則塊仍寫得進去（一塊有問題不連累另一塊），但索引塊不能進。
        self.assertNotIn(INDEX_BEGIN, agents.read_text(encoding="utf-8"))

    def test_the_project_root_comes_from_the_transcript_not_a_registry(self):
        project, vault = self._project("money")
        self.assertEqual(capture_route.project_root_of(vault), project)
        # cwd 指的目錄不在了就回 None：沒有第二套猜法。
        (vault.parent / "session.jsonl").write_text(
            json.dumps({"cwd": os.fspath(self.root / "never")}) + "\n", encoding="utf-8")
        self.assertIsNone(capture_route.project_root_of(vault))
        # 一份紀錄都沒有時，呼叫端自己決定那算什麼（孤兒庫盤點當它是活的）。
        (vault.parent / "session.jsonl").unlink()
        self.assertIsNone(capture_route.project_root_of(vault))
        self.assertEqual(
            capture_route.project_root_of(vault, no_transcript="none"), "none")


def _selftest():
    suite = unittest.TestLoader().loadTestsFromTestCase(ProjectSync)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
