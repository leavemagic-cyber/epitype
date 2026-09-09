"""Generated reading views: classification, links, fingerprint, lock, and the
recall/lint contracts that the two views are only useful if they keep.

The four properties this file exists to pin (2026-09-09 Claude↔Codex convergence):
`closed` moves the view and nothing else — the card stays searchable; a card that
needs a status but has none is listed for review instead of being passed off as
confirmed-current; every managed card lands in exactly one view; and the generator
never writes MEMORY.md.
"""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import unittest

from epitype import card_lint, memsearch, memspec, views


def _card(path, frontmatter, body="body"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8", newline="\n")


class ViewsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="epitype-views-regression-")
        self.vault = Path(self.directory.name).resolve() / "vault"
        self.vault.mkdir()
        _card(
            self.vault / "project-running.md",
            "name: project-running\ndescription: 2026-09-01 缺 status 的專案卡\n"
            "aliases:\n  - 進行中\nmetadata:\n  type: project",
        )
        _card(
            self.vault / "project-finished.md",
            "name: project-finished\ndescription: 2026-09-01 已結案專案 closedneedle\n"
            f"{memspec.DECISION_STATUS_FIELD}: {memspec.CLOSED_CARD_STATUS}\n"
            f"{memspec.CLOSED_AT_FIELD}: 2026-09-09\n{memspec.CLOSED_BY_FIELD}: claude\n"
            f"{memspec.CLOSED_EVIDENCE_FIELD}: 驗收於 2026-09-09\n"
            "aliases:\n  - 結案\nmetadata:\n  type: project",
            "closedneedle 這張結案卡的知識仍要搜得到。",
        )
        _card(
            self.vault / "decision-now.md",
            "name: decision-now\ndescription: 2026-09-01 現行裁定\n"
            "decision_key: k-view\n"
            f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
            "current_decision_at: 2026-09-01\ndecided_by: three-way\naliases:\n  - 現行",
        )
        _card(
            self.vault / "decision-past.md",
            "name: decision-past\ndescription: 2026-08-01 已被取代的裁定\n"
            "decision_key: k-view\n"
            f"{memspec.DECISION_STATUS_FIELD}: {memspec.SUPERSEDED_DECISION_STATUS}\n"
            f"{memspec.SUPERSEDED_BY_FIELD}: decision-now.md\n"
            "current_decision_at: 2026-08-01\ndecided_by: three-way\naliases:\n  - 舊裁定",
        )
        _card(
            self.vault / "feedback-standing.md",
            "name: feedback-standing\ndescription: 2026-09-01 回饋卡不套結案\n"
            "aliases:\n  - 回饋",
        )
        _card(
            self.vault / "grants" / "grant one (a).md",
            "name: grant one\ndescription: owner grant auto-captured 2026-09-02: 可以\n"
            "captured_at: 2026-09-02T07:37:47Z\nsession_id: synthetic",
        )
        (self.vault / memspec.MEMORY_INDEX_FILENAME).write_text(
            "# hand-written entry point\n", encoding="utf-8", newline="\n"
        )
        self.current_path, self.closed_path = views.view_paths(self.vault)

    def tearDown(self):
        self.directory.cleanup()

    def _generate(self, **kwargs):
        kwargs.setdefault("stamp", "2026-09-09T00:00Z")
        return views.generate(self.vault, **kwargs)

    def _managed(self):
        return {relative for relative, _path, _mtime, _size in memsearch.scan_cards(self.vault)}

    def test_state_by_type_and_每張卡剛好出現一次(self):
        result = self._generate()
        current = self.current_path.read_text(encoding="utf-8")
        closed = self.closed_path.read_text(encoding="utf-8")

        self.assertEqual(result["total"], len(self._managed()))
        self.assertEqual(result["current"] + result["closed"] + result["review"], result["total"])
        listed = [
            line for text in (current, closed)
            for line in text.splitlines() if line.startswith("- [")
        ]
        self.assertEqual(len(listed), result["total"])
        self.assertEqual(views.listed_paths(self.vault), self._managed())

        # project closed 與 decision superseded 都降到第三層，各自帶自己的理由。
        self.assertIn("- [project-finished](../../project-finished.md)", closed)
        self.assertIn("closed 2026-09-09 by claude", closed)
        self.assertIn("- [decision-past](../../decision-past.md)", closed)
        self.assertIn("superseded_by: decision-now.md", closed)
        # 回饋卡與事件卡不套結案；缺 status 的專案卡列待複查而不是冒充現用。
        self.assertIn("- [feedback-standing](../feedback-standing.md)", current)
        self.assertIn("- [project-running](../project-running.md) — project｜", current)
        self.assertEqual(result["review"], 1)
        self.assertNotIn("project-running", closed)

    def test_決策段列出全部現行決策帶_key_與日期(self):
        result = self._generate()
        current = self.current_path.read_text(encoding="utf-8")
        self.assertEqual(result["decisions"], 1)
        self.assertIn(memspec.VIEWS_DECISION_HEADING.format(count=1), current)
        self.assertIn(
            "- [decision-now](../decision-now.md) — k-view｜2026-09-01｜", current
        )

    def test_連結從視圖所在目錄解得開(self):
        self._generate()
        for text, base in (
            (self.current_path.read_text(encoding="utf-8"), self.current_path.parent),
            (self.closed_path.read_text(encoding="utf-8"), self.closed_path.parent),
        ):
            targets = [
                line.split("](", 1)[1].split(")", 1)[0]
                for line in text.splitlines()
                if line.startswith("- [")
            ]
            self.assertTrue(targets)
            for target in targets:
                for character, encoded in views._LINK_ESCAPES.items():
                    target = target.replace(encoded, character)
                self.assertTrue((base / target).resolve().is_file(), target)

    def test_指紋沒變不重寫_卡片變了才重寫(self):
        self.assertEqual(self._generate()["status"], "written")
        first = self.current_path.read_text(encoding="utf-8")
        self.assertEqual(
            views.generate(self.vault, stamp="2026-09-09T09:00Z")["status"], "unchanged"
        )
        self.assertEqual(self.current_path.read_text(encoding="utf-8"), first)

        _card(self.vault / "feedback-added.md", "name: feedback-added\ndescription: 2026-09-09 新卡")
        second = views.generate(self.vault, stamp="2026-09-09T10:00Z")
        self.assertEqual(second["status"], "written")
        self.assertIn(
            "- [feedback-added](../feedback-added.md)",
            self.current_path.read_text(encoding="utf-8"),
        )

    def test_同庫並行拿不到鎖就不寫半份(self):
        self._generate()
        before = self.current_path.read_text(encoding="utf-8")
        _card(self.vault / "feedback-racing.md", "name: feedback-racing\ndescription: 2026-09-09 併發")
        with memspec.file_lock(views._lock_target(self.vault), 5.0) as held:
            self.assertTrue(held)
            busy = views.generate(self.vault, lock_timeout=0.0, stamp="2026-09-09T11:00Z")
        self.assertEqual(busy["status"], "lock-busy")
        self.assertEqual(self.current_path.read_text(encoding="utf-8"), before)

    def test_生成器不碰手寫的_MEMORY_md(self):
        before = (self.vault / memspec.MEMORY_INDEX_FILENAME).read_bytes()
        self._generate()
        self.assertEqual((self.vault / memspec.MEMORY_INDEX_FILENAME).read_bytes(), before)
        self.assertNotIn(memspec.MEMORY_INDEX_FILENAME, self._managed())

    def test_closed_只改目錄位置_仍搜得到(self):
        self._generate()
        self.assertEqual(memsearch.build_index(self.vault)["status"], "built")
        hit = memsearch.query_index(self.vault, "closedneedle")
        self.assertEqual(
            {item["path"] for item in hit["results"]},
            {str((self.vault / "project-finished.md").resolve())},
        )
        self.assertEqual(hit["guidance"], [])
        # 對照組：superseded 才轉向繼任卡，closed 不轉向。
        superseded = memsearch.query_index(self.vault, "decision-past")
        self.assertNotIn(
            str((self.vault / "decision-past.md").resolve()),
            {item["path"] for item in superseded["results"]},
        )

    def test_deep_lint_報目錄漏卡與索引漏卡並折入決策規則(self):
        bare = card_lint.scan_vault(self.vault, deep=True)
        rules = {(item["level"], item["rule"]) for item in bare["vault_findings"]}
        self.assertIn((card_lint.WARN, "views"), rules)
        self.assertIn((card_lint.WARN, "search-index"), rules)

        self._generate()
        memsearch.build_index(self.vault)
        clean = card_lint.scan_vault(self.vault, deep=True)
        self.assertEqual(
            [item for item in clean["vault_findings"] if item["rule"] in ("views", "search-index")],
            [],
        )

        # 目錄生成後又多一張卡＝目錄與索引都漏了它，兩條各報一次並帶修法。
        _card(self.vault / "feedback-late.md", "name: feedback-late\ndescription: 2026-09-09 晚到的卡")
        stale = card_lint.scan_vault(self.vault, deep=True)
        reasons = {
            item["rule"]: item["reason"]
            for item in stale["vault_findings"]
            if item["rule"] in ("views", "search-index")
        }
        self.assertIn("feedback-late.md", reasons.get("views", ""))
        self.assertIn("epitype/views.py", reasons.get("views", ""))
        self.assertIn("feedback-late.md", reasons.get("search-index", ""))
        self.assertIn("memsearch.py build", reasons.get("search-index", ""))

    def test_deep_lint_抓同一_decision_key_兩張現行卡(self):
        _card(
            self.vault / "decision-duplicate.md",
            "name: decision-duplicate\ndescription: 2026-09-09 第二張現行卡\n"
            "decision_key: k-view\n"
            f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
            "current_decision_at: 2026-09-09\ndecided_by: three-way\naliases:\n  - 重複",
        )
        report = card_lint.scan_vault(self.vault, deep=True)
        decisions = [item for item in report["vault_findings"] if item["rule"] == "decisions"]
        self.assertTrue(
            any(item["level"] == card_lint.FAIL and "2 張現行卡" in item["reason"] for item in decisions),
            decisions,
        )
        self.assertEqual(
            report["fail"],
            card_lint.scan_vault(self.vault)["fail"]
            + sum(1 for item in report["vault_findings"] if item["level"] == card_lint.FAIL),
        )

    def test_deep_lint_抓斷掉的取代鏈(self):
        _card(
            self.vault / "decision-dangling.md",
            "name: decision-dangling\ndescription: 2026-09-09 指向不存在的繼任卡\n"
            "decision_key: k-gone\n"
            f"{memspec.DECISION_STATUS_FIELD}: {memspec.SUPERSEDED_DECISION_STATUS}\n"
            f"{memspec.SUPERSEDED_BY_FIELD}: decision-nowhere.md\n"
            "current_decision_at: 2026-09-09\ndecided_by: three-way\naliases:\n  - 斷鏈",
        )
        report = card_lint.scan_vault(self.vault, deep=True)
        self.assertTrue(
            any(
                item["rule"] == "decisions"
                and item["level"] == card_lint.FAIL
                and "指向不存在的卡" in item["reason"]
                for item in report["vault_findings"]
            ),
            report["vault_findings"],
        )

    def test_空庫也生成兩份視圖(self):
        empty = Path(self.directory.name).resolve() / "empty"
        empty.mkdir()
        result = views.generate(empty, stamp="2026-09-09T00:00Z")
        current_path, closed_path = views.view_paths(empty)
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["total"], 0)
        self.assertTrue(current_path.is_file() and closed_path.is_file())
        self.assertIn(memspec.VIEWS_EMPTY_SECTION, closed_path.read_text(encoding="utf-8"))
        self.assertEqual(views.listed_paths(empty), set())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
