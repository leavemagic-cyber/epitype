"""夢第 11 節自己走 memory-index（plan／apply_plan）精簡超過上限的手寫 MEMORY.md。

搬成的不再列給人；memory_index 本身補上的安全關卡（規劃後被改就放棄、多重集合核對、
每次備份、來源綁定／INVALID 不寫）一起在這裡測。
"""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collections import Counter
from datetime import date
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from epitype import card_io, dream, memory_index, memspec, rule_pipeline

TODAY = date(2026, 11, 13)
BOM = b"\xef\xbb\xbf"
CAP = 600
APPENDED = "- [另一場剛寫的](new_card.md)\r\n".encode("utf-8")


def _nonblank(raw):
    return Counter(line.rstrip(b"\r\n") for line in raw.splitlines() if line.strip())


def _section(vault, config, apply=True):
    return dream._section_caps([vault], TODAY, None, config, {"apply_fixes": apply})


def _review_paths(section):
    report = {"today": TODAY.isoformat(), "sections": [{"id": 11, "title": "上限檢查", "error": None, **section}]}
    return [item["path"] for item in dream.review_list(report)["items"]]


def _rendered(section):
    lines = []
    dream._render_section(lines, {"id": 11, "title": "上限檢查", "error": None, **section})
    return "\n".join(lines)


class SlimTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="epitype-dream-index-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault = self.root / "memory"
        self.vault.mkdir()
        self.index = self.vault / memspec.MEMORY_INDEX_FILENAME
        self.full = self.vault / memory_index.FULL_FILENAME
        self.config = {}
        # 小庫模擬宿主載入上限（真實為 200 行或 25,000 B，見 memory_index_regression）。
        limit = patch.object(memory_index, "HOST_INDEX_MAX_BYTES", CAP)
        limit.start()
        self.addCleanup(limit.stop)
        lines = ["# 專案記憶入口", "", "**先讀：** C:/dev/project/計劃.md；主計劃有記的先讀再答。", ""]
        for n in range(14):
            name = f"feedback_rule_{n:02d}.md"
            (self.vault / name).write_text("---\nname: x\n---\nbody\n", encoding="utf-8")
            lines.append(f"- [第 {n} 條規定：做事前先查卡片與現況]({name}) — 2026-11-{n + 1:02d}")
        lines.append(lines[4])  # 重複行也要照次數留下
        self.raw = BOM + "\r\n".join(lines).encode("utf-8") + b"\r\n"
        self.index.write_bytes(self.raw)

    def _backups(self):
        root = self.vault / memspec.DREAM_DIRECTORY / memory_index.BACKUP_DIRECTORY
        return sorted(root.iterdir()) if root.exists() else []

    def test_over_cap_index_is_slimmed_through_memory_index_and_every_line_survives(self):
        self.assertGreater(len(self.raw), CAP)
        section = _section(self.vault, self.config)

        self.assertEqual((section["counts"]["over_cap"], section["counts"]["slimmed"]), (0, 1))
        [moved] = section["slimmed"]
        short, full = self.index.read_bytes(), self.full.read_bytes()
        self.assertEqual((moved["bytes_before"], moved["bytes_after"]), (len(self.raw), len(short)))
        self.assertLessEqual(len(short), CAP)
        self.assertGreater(moved["moved_lines"], 0)
        self.assertEqual(moved["full"], str(self.full))
        self.assertTrue(short.startswith(BOM))
        self.assertIn(memory_index.POINTER, short)
        # 每一行非空白原文都還在：完整索引照次數收下整份原文，短索引留下的是其中一部分。
        self.assertFalse(_nonblank(self.raw) - _nonblank(full))
        kept = _nonblank(memory_index._without_pointer(short))
        self.assertFalse(kept - _nonblank(self.raw))
        self.assertEqual(sum((_nonblank(self.raw) - kept).values()), moved["moved_lines"])
        # 這一次的備份在 <庫>/.epitype/memory_index_backups/<時間>/。
        [backup] = self._backups()
        self.assertEqual(Path(moved["backup"]), backup)
        self.assertEqual((backup / self.index.name).read_bytes(), self.raw)
        # 第 11 節報出搬了什麼，且不再列給人。
        self.assertIn(f"{len(self.raw)} → {len(short)}", _rendered(section))
        self.assertIn(f"{moved['moved_lines']} 行", _rendered(section))
        self.assertNotIn(str(self.index), _review_paths(section))
        # 再跑一次沒超過：什麼都不動，也不多一批備份。
        again = _section(self.vault, self.config)
        self.assertEqual((again["counts"]["over_cap"], again["counts"]["slimmed"]), (0, 0))
        self.assertEqual((self.index.read_bytes(), self.full.read_bytes()), (short, full))
        self.assertEqual(len(self._backups()), 1)

    def test_concurrent_change_after_backup_aborts_the_write(self):
        real_backup = memory_index._backup

        def racing_backup(*args, **kwargs):
            target = real_backup(*args, **kwargs)
            with self.index.open("ab") as stream:
                stream.write(APPENDED)
            return target

        with patch.object(memory_index, "_backup", side_effect=racing_backup):
            section = _section(self.vault, self.config)

        self.assertEqual((section["counts"]["over_cap"], section["counts"]["slimmed"]), (1, 0))
        self.assertEqual(section["examples"][0]["auto_slim"]["status"], "abandoned")
        self.assertEqual(self.index.read_bytes(), self.raw + APPENDED)
        self.assertFalse(self.full.exists())
        self.assertIn(str(self.index), _review_paths(section))

    def test_full_index_changed_since_plan_aborts_the_write(self):
        memory_index.apply_plan(memory_index.plan(self.vault, self.config))
        self.index.write_bytes(self.index.read_bytes() + APPENDED * 20)
        proposed = memory_index.plan(self.vault, self.config)
        full_before = self.full.read_bytes() + b"- written by another tool\n"
        self.full.write_bytes(full_before)
        index_before = self.index.read_bytes()

        with self.assertRaises(card_io.CardConflict):
            memory_index.apply_plan(proposed)

        self.assertEqual((self.index.read_bytes(), self.full.read_bytes()), (index_before, full_before))

    def test_line_loss_is_refused_before_any_write(self):
        def lossy(previous, source):
            return memory_index.FULL_HEADER + source.replace("第 9 條".encode("utf-8"), b"")

        with patch.object(memory_index, "_full_payload", side_effect=lossy):
            section = _section(self.vault, self.config)

        auto = section["examples"][0]["auto_slim"]
        self.assertEqual(auto["status"], "refused")
        self.assertIn("多重集合", auto["reason"])
        self.assertEqual(self.index.read_bytes(), self.raw)
        self.assertFalse(self.full.exists())
        self.assertEqual(self._backups(), [])

    def test_codex_generated_index_is_untouched(self):
        codex = self.root / ".codex" / "memories"
        codex.mkdir(parents=True)
        generated = codex / memspec.MEMORY_INDEX_FILENAME
        generated.write_bytes(self.raw)

        section = _section(codex, self.config)

        self.assertEqual(generated.read_bytes(), self.raw)
        self.assertEqual(section["counts"]["slimmed"], 0)
        self.assertEqual(sorted(path.name for path in codex.iterdir()), [memspec.MEMORY_INDEX_FILENAME])

    def test_index_that_cannot_fit_stays_listed(self):
        header = ("# 入口\r\n" + "這一段是檔首的手寫說明，夢不能搬。" * 30 + "\r\n\r\n").encode("utf-8")
        self.raw = BOM + header + self.raw[len(BOM):]
        self.index.write_bytes(self.raw)

        section = _section(self.vault, self.config)

        self.assertEqual(self.index.read_bytes(), self.raw)
        self.assertFalse(self.full.exists())
        self.assertEqual((section["counts"]["over_cap"], section["counts"]["slimmed"]), (1, 0))
        self.assertEqual(section["examples"][0]["auto_slim"]["status"], "cannot-fit")
        self.assertIn(str(self.index), _review_paths(section))
        self.assertIn("cannot-fit", _rendered(section))

    def test_dry_run_only_reports(self):
        section = _section(self.vault, self.config, apply=False)

        self.assertEqual(self.index.read_bytes(), self.raw)
        self.assertFalse(self.full.exists())
        self.assertEqual(section["examples"][0]["auto_slim"]["status"], "would-rewrite")
        self.assertFalse((self.vault / memspec.DREAM_DIRECTORY).exists())

    def test_source_bound_index_is_not_rewritten(self):
        self.raw = b"---\nname: index\nrule_provenance: {}\n---\n" + self.raw[len(BOM):]
        self.index.write_bytes(self.raw)

        section = _section(self.vault, self.config)

        self.assertEqual(self.index.read_bytes(), self.raw)
        self.assertFalse(self.full.exists())
        auto = section["examples"][0]["auto_slim"]
        self.assertEqual(auto["status"], "refused")
        self.assertIn("rule_provenance", auto["reason"])

    def test_rewrite_that_would_be_invalid_is_not_written(self):
        verdict = SimpleNamespace(status="INVALID", issues=["frozen meaning changed"])
        with patch.object(rule_pipeline, "inspect_card", return_value=verdict):
            section = _section(self.vault, self.config)

        self.assertEqual(self.index.read_bytes(), self.raw)
        self.assertFalse(self.full.exists())
        auto = section["examples"][0]["auto_slim"]
        self.assertEqual(auto["status"], "refused")
        self.assertIn("INVALID", auto["reason"])


if __name__ == "__main__":
    # run_all passes --selftest; drop it from sys.argv itself, since hook mains called by the tests read it too.
    while "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
