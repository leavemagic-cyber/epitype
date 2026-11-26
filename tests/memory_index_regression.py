"""Lossless short indexes, including Windows CRLF and no-clobber publication."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collections import Counter
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from epitype import card_io, card_lint, cardscan, dream, host_sync, memory_index as index, memspec
from datetime import date


class IndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="epitype-memory-index-")
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name)
        self.memory = self.vault / "MEMORY.md"
        self.config = {}
        # 小庫模擬宿主上限，才能用十幾行驗搬移；真實數字見 HostLoadLimit。
        limit = patch.object(index, "HOST_INDEX_MAX_BYTES", 240)
        limit.start()
        self.addCleanup(limit.stop)
        self.header = "# My handwritten index\r\nIntro kept verbatim.\r\n\r\n".encode()
        self.raw = self.header
        for n in range(12):
            (self.vault / f"card {n}.md").write_text("card body", encoding="utf-8")
            self.raw += f"- [{n} {'⚠' if n == 1 else ''}](card%20{n}.md) — 2026-11-{n+1:02d}\r\n".encode()
        self.raw += b"- duplicate\r\n- duplicate\r\n"
        self.memory.write_bytes(self.raw)

    def plan(self):
        return index.plan(self.vault, self.config)

    def test_cap_keeps_header_moves_from_the_bottom_and_partitions_original(self):
        proposed = self.plan()
        self.assertLessEqual(len(proposed.short), 240)
        self.assertTrue(proposed.short.startswith(self.header))
        self.assertIn(b"(card%200.md)", proposed.short)
        self.assertNotIn(b"- duplicate", proposed.short)
        self.assertNotIn(b"(card%2011.md)", proposed.short)
        lines = self.raw.splitlines(keepends=True)
        self.assertEqual(sorted(proposed.kept + proposed.moved), list(range(len(lines))))
        self.assertFalse(set(proposed.kept) & set(proposed.moved))
        self.assertEqual(Counter(lines[i] for i in proposed.kept + proposed.moved), Counter(lines))

    def test_full_backup_links_cards_and_idempotency(self):
        before_cards = {p: p.read_bytes() for p in self.vault.glob("card*.md")}
        index.apply_plan(self.plan())
        full = self.vault / index.FULL_FILENAME
        backup = self.vault / index.BACKUP_FILENAME
        self.assertEqual(backup.read_bytes(), self.raw)
        self.assertEqual(full.read_bytes(), index.FULL_HEADER + self.raw)
        for path in (self.memory, full):
            for link in re.findall(rb"\]\(([^)]+)\)", path.read_bytes()):
                self.assertTrue((path.parent / unquote(link.decode())).exists(), link)
        self.assertEqual(before_cards, {p: p.read_bytes() for p in before_cards})
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.memory, full, backup)}
        index.apply_plan(self.plan())
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        self.assertFalse(any(p.name.startswith("_MEMORY") for p in cardscan.card_files(self.vault)))

    def test_native_append_after_pointer_is_archived_without_pointer_duplication(self):
        index.apply_plan(self.plan())
        self.memory.write_bytes(self.memory.read_bytes() + b"- newest 2026-12-01\n" * 20)
        proposed = self.plan()
        index.apply_plan(proposed)
        self.assertEqual(self.memory.read_bytes().count(index.POINTER), 1)
        self.assertEqual((self.vault / index.FULL_FILENAME).read_bytes().count(b"- newest 2026-12-01\n"), 20)
        self.assertNotIn(index.POINTER, (self.vault / index.FULL_FILENAME).read_bytes())
        self.assertEqual((self.vault / index.BACKUP_FILENAME).read_bytes(), self.raw)

    def test_small_append_updates_existing_full_index(self):
        index.apply_plan(self.plan())
        index.HOST_INDEX_MAX_BYTES = 1000
        self.memory.write_bytes(self.memory.read_bytes() + b"- added\n")
        index.apply_plan(self.plan())
        self.assertIn(b"- added\n", (self.vault / index.FULL_FILENAME).read_bytes())

    def test_incremental_items_keep_their_heading_ancestry(self):
        original = b"# Index\n## Active\n### Queue\n- first\n## Closed\n- old\n"
        source = b"# Index\n## Active\n### Queue\n- first\n- new active\n## Closed\n- old\n"
        previous = index.FULL_HEADER + original
        full = index._full_payload(previous, source)
        self.assertTrue(full.startswith(previous))
        self.assertIn(b"# Index\n## Active\n### Queue\n- new active\n", full)
        self.assertEqual(index._full_payload(full, source), full)

    def test_same_text_moved_to_another_heading_is_archived_there(self):
        original = b"# Index\n## Active\n- unchanged words\n## Closed\n- old\n"
        source = b"# Index\n## Active\n## Closed\n- old\n- unchanged words\n"
        full = index._full_payload(index.FULL_HEADER + original, source)
        self.assertIn(b"# Index\n## Closed\n- unchanged words\n", full)
        self.assertEqual(full.count(b"- unchanged words\n"), 2)
        self.assertEqual(index._full_payload(full, source), full)

    def test_unsectioned_and_unterminated_additions_do_not_inherit_closed(self):
        previous = index.FULL_HEADER + b"## Closed\n- old"
        source = b"- unsectioned addition"
        full = index._full_payload(previous, source)
        self.assertIn(index.ROOT_ADDITIONS + source, full)
        self.assertTrue(full.startswith(previous))
        self.assertEqual(index._full_payload(full, source), full)

    def test_long_fence_is_not_closed_by_shorter_example_delimiter(self):
        raw = (b"# Index\n## Examples\n````text\n```\n## fake heading\n- fake item\n````\n"
               b"## Active\n- active 2026-12-01\n" + b"- old " + b"x" * 250 + b"\n")
        self.memory.write_bytes(raw)
        short = self.plan().short
        self.assertEqual(b"````text" in short, b"## fake heading" in short)
        self.assertEqual(b"````text" in short, b"\n````\n" in short)

    def test_missing_final_newline_preserved(self):
        self.memory.write_bytes(self.raw.rstrip(b"\r\n"))
        index.apply_plan(self.plan())
        self.assertEqual((self.vault / index.FULL_FILENAME).read_bytes(),
                         index.FULL_HEADER + self.raw.rstrip(b"\r\n"))

    def test_reference_definitions_fences_and_list_continuations(self):
        self.memory.write_bytes(b"# Header\n\n## Topic\n- [entry][card]\n  continued text\n"
                                b"- old entry " + b"x" * 200 + b"\n\n[card]: card%200.md\n"
                                b"\n```text\n- fake list\n```\n")
        proposed = self.plan()
        self.assertIn(b"[card]: card%200.md", proposed.short)
        self.assertEqual(b"- [entry][card]" in proposed.short, b"  continued text" in proposed.short)
        self.assertEqual(b"```text" in proposed.short, b"- fake list" in proposed.short)
        self.assertEqual(b"```text" in proposed.short, b"\n```\n" in proposed.short)

    def test_oversized_header_and_invalid_encoding_do_not_write(self):
        for raw in (b"# " + b"x" * 500 + b"\n", b"\xff"):
            self.memory.write_bytes(raw)
            with self.assertRaises((ValueError, UnicodeError)):
                self.plan()
            self.assertEqual(self.memory.read_bytes(), raw)
            self.assertFalse((self.vault / index.BACKUP_FILENAME).exists())

    def test_stale_plan_and_foreign_full_file_preserve_existing_work(self):
        proposed = self.plan()
        self.memory.write_bytes(self.raw + b"other writer\n")
        with self.assertRaises(card_io.CardConflict):
            index.apply_plan(proposed)
        self.assertFalse((self.vault / index.BACKUP_FILENAME).exists())
        self.memory.write_bytes(self.raw)
        full = self.vault / index.FULL_FILENAME
        full.write_bytes(b"handwritten existing file")
        with self.assertRaises(ValueError):
            index.apply_plan(self.plan())
        self.assertEqual(full.read_bytes(), b"handwritten existing file")
        self.assertEqual(self.memory.read_bytes(), self.raw)

    def test_foreign_first_backup_is_not_silently_accepted(self):
        backup = self.vault / index.BACKUP_FILENAME
        backup.write_bytes(b"unrelated existing backup")
        with self.assertRaises(ValueError):
            index.apply_plan(self.plan())
        self.assertEqual(backup.read_bytes(), b"unrelated existing backup")
        self.assertEqual(self.memory.read_bytes(), self.raw)
        self.assertFalse((self.vault / index.FULL_FILENAME).exists())

    def test_discover_ignores_files_and_includes_empty_vaults(self):
        projects = self.vault / "projects"
        projects.mkdir()
        (projects / "metadata.json").write_text("{}", encoding="utf-8")
        memory = projects / "project" / "memory"
        memory.mkdir(parents=True)
        self.assertEqual(index.discover(projects), [memory])

    def test_check_and_dry_run_do_not_write_and_missing_input_is_error(self):
        with patch.object(memspec, "config_options", return_value=self.config):
            out = io.StringIO()
            self.assertEqual(index.main([str(self.vault), "--check", "--json"], out), 1)
            self.assertEqual(json.loads(out.getvalue())[0]["status"], "over-cap")
            self.assertEqual(index.main([str(self.vault)], io.StringIO()), 0)
        self.assertEqual(self.memory.read_bytes(), self.raw)
        self.assertFalse((self.vault / index.BACKUP_FILENAME).exists())
        with patch.object(memspec, "config_options", return_value={}):
            self.assertEqual(index.main([], io.StringIO()), 2)

    def test_every_vault_has_same_raw_lint_in_cards_and_nightly(self):
        with patch.object(memspec, "config_options", return_value=self.config):
            findings = card_lint._vault_findings(self.vault, [])
            self.assertTrue(any(rule == "memory-index-cap" for _level, rule, _reason in findings))
            report = dream._section_caps([self.vault], date.today(), None, self.config)
            entries = [e for e in report["examples"] if e["kind"] == "index-file"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["bytes"], len(self.raw))
        self.memory.write_bytes(b"x" * 240)
        self.assertEqual(index.lint(self.vault, self.config)["status"], "unchanged")


    @unittest.skipUnless(os.name == "nt", "Windows filesystem behavior")
    def test_windows_crlf_backup_atomic_replace_and_readback(self):
        self.test_full_backup_links_cards_and_idempotency()
        self.assertIn(b"\r\n", self.memory.read_bytes())
        self.assertFalse(list(self.vault.glob("*.lock")))
        self.assertFalse(list(self.vault.glob("*.tmp")))

    @unittest.skipUnless(os.name == "nt", "Windows native editor line endings")
    def test_windows_native_crlf_pointer_rewrite(self):
        index.apply_plan(self.plan())
        normalised = self.memory.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        self.memory.write_bytes(normalised + b"- native appended 2026-12-01\r\n" * 12)
        index.apply_plan(self.plan())
        short = self.memory.read_bytes()
        full = (self.vault / index.FULL_FILENAME).read_bytes()
        self.assertEqual(short.count(b"<!-- epitype:index -->"), 1)
        self.assertNotIn(b"<!-- epitype:index -->", full)
        self.assertEqual(full.count(b"- native appended 2026-12-01\r\n"), 12)

    @unittest.skipUnless(os.name == "nt", "Windows junction privacy boundary")
    def test_windows_junction_discovery_is_not_followed(self):
        projects = self.vault / "projects"
        projects.mkdir()
        (projects / "metadata.json").write_text("{}", encoding="utf-8")
        real = projects / "real"
        memory = real / "memory"
        memory.mkdir(parents=True)
        alias = projects / "linked-project"
        linked_memory = projects / "linked-memory" / "memory"
        linked_memory.parent.mkdir()
        for link, target in ((alias, real), (linked_memory, memory)):
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(made.returncode, 0, made.stdout.decode(errors="replace"))
            self.addCleanup(os.rmdir, link)
        self.assertEqual(index.discover(projects), [memory])


class HostLoadLimit(unittest.TestCase):
    """MEMORY.md 上限＝Claude Code 載入上限（200 行或 25,000 B），不是自訂數字。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="epitype-host-limit-")
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name)
        self.memory = self.vault / "MEMORY.md"

    def card(self, name, extra=""):
        (self.vault / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: d\n{extra}---\nbody\n", encoding="utf-8")

    def test_limit_is_the_documented_host_limit(self):
        self.assertEqual((index.HOST_INDEX_MAX_LINES, index.HOST_INDEX_MAX_BYTES), (200, 25_000))
        limit = index.limit_of(self.vault, {"index_cap_bytes": 10, "shared_index_cap_bytes": 10})
        self.assertEqual((limit.max_lines, limit.max_bytes), (200, 25_000))
        self.assertIn("index_cap_bytes", limit.notice)
        self.assertIn("shared_index_cap_bytes", limit.notice)
        self.assertIsNone(index.limit_of(self.vault, {}).notice)
        # 舊鍵是壞值也照樣讀得進來，不讓舊設定把工具弄壞。
        self.assertEqual(index.limit_of(self.vault, {"index_cap_bytes": "x"}).max_bytes, 25_000)

    def test_1600_bytes_50_lines_is_not_slimmed_in_any_vault(self):
        raw = b"# Index\n" + b"".join(b"- [entry %02d](c.md) " % n + b"y" * 12 + b"\n" for n in range(48))
        raw += b"z" * (1600 - len(raw) - 1) + b"\n"
        self.assertEqual((len(raw), len(raw.splitlines())), (1600, 50))
        self.memory.write_bytes(raw)
        for config in ({}, {"index_cap_bytes": 1526},
                       {"vaults": [str(self.vault)], "shared_index_cap_bytes": 1526}):
            proposed = index.plan(self.vault, config)
            self.assertEqual(proposed.short, raw)
            self.assertEqual(proposed.moved, [])
            self.assertEqual(index.lint(self.vault, config)["status"], "unchanged")
            report = dream._section_caps([self.vault], date.today(), None, config)
            self.assertFalse([e for e in report["examples"] if e["kind"] == "index-file"])
            if config:
                self.assertIn("已不再使用", proposed.summary()["notice"])
                self.assertIn("已不再使用", report["note"])
        self.assertEqual(index.apply_plan(index.plan(self.vault, {})), None)
        self.assertEqual(self.memory.read_bytes(), raw)
        self.assertFalse((self.vault / index.FULL_FILENAME).exists())

    def assert_lossless(self, raw):
        index.apply_plan(index.plan(self.vault, {}))
        short = self.memory.read_bytes()
        full = (self.vault / index.FULL_FILENAME).read_bytes()
        self.assertEqual(full, index.FULL_HEADER + raw)
        self.assertLessEqual(len(short), 25_000)
        self.assertLessEqual(len(short.decode().splitlines()), 200)
        return short

    def test_over_200_lines_moves_superseded_card_lines_first(self):
        self.card("old", "status: superseded\nsuperseded_by: new.md\n")
        self.card("new")
        self.card("pointed", "superseded_by: new.md\n")
        lines = [b"# Index\n"]
        for n in range(206):
            target = b"old" if n in (10, 20) else b"pointed" if n == 30 else b"new"
            lines.append(b"- [item %03d](%s.md)\n" % (n, target))
        raw = b"".join(lines)
        self.memory.write_bytes(raw)
        proposed = index.plan(self.vault, {})
        moved = [raw.splitlines(keepends=True)[i] for i in proposed.moved]
        # 207 lines + 3 pointer lines → 10 must go: the 3 stale ones, then 7 from the bottom.
        self.assertEqual(len(moved), 10)
        for n in (10, 20, 30):
            self.assertIn(lines[n + 1], moved)
        self.assertEqual(set(moved) - {lines[11], lines[21], lines[31]}, set(lines[-7:]))
        short = self.assert_lossless(raw)
        self.assertEqual(len(short.decode().splitlines()), 200)

    def test_stale_link_forms_subfolder_wikilink_md_wikilink_angle_and_encoded(self):
        stale = "---" + chr(10) + "name: x" + chr(10) + "status: superseded" + chr(10) + "---" + chr(10) + "body" + chr(10)
        live = stale.replace("superseded", "active")
        (self.vault / "sub").mkdir()
        for name, text in (("old.md", stale), ("sub/deep.md", stale), ("old card.md", stale),
                           ("live.md", live), ("a/twin.md", stale), ("b/twin.md", stale)):
            (self.vault / name).parent.mkdir(parents=True, exist_ok=True)
            (self.vault / name).write_text(text, encoding="utf-8")
        cases = {
            "- [a](old.md)": True, "- [a](./old.md#x)": True, "- [[old]]": True, "- [[old|alias]]": True,
            "- [[old.md]]": True, "- [[deep]]": True, "- [[deep.md|d]]": True,
            "- [a](<old card.md>)": True, "- [a](old%20card.md)": True, "- [a](sub/deep.md \"t\")": True,
            "- [a](live.md)": False, "- [[live]]": False, "- [[missing]]": False,
            "- [[twin]]": False,  # two cards share the name: ambiguous, not guessed
            "- [a](../outside.md)": False,
        }
        names = {}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIs(index._stale_card(self.vault.resolve(), text.encode(), names), expected)
                self.assertIs(index._stale_card(self.vault.resolve(), text.encode()), expected)

    def test_over_25000_bytes_moves_closed_and_expired_card_lines_first(self):
        self.card("closed", "status: closed\n")
        self.card("expired", "valid_until: 2020-01-01\n")
        self.card("live")
        lines = [b"# Index\n"]
        for n in range(130):
            target = b"closed" if n == 5 else b"expired" if n == 6 else b"live"
            lines.append(b"- [item %03d](%s.md) " % (n, target) + b"w" * 180 + b"\n")
        raw = b"".join(lines)
        self.assertGreater(len(raw), 25_000)
        self.assertLess(len(raw.splitlines()), 200)
        self.memory.write_bytes(raw)
        proposed = index.plan(self.vault, {})
        moved = {raw.splitlines(keepends=True)[i] for i in proposed.moved}
        self.assertIn(lines[6], moved)
        self.assertIn(lines[7], moved)
        self.assertTrue(set(lines[1:6]) | set(lines[8:20]) <= set(raw.splitlines(keepends=True)[i]
                                                               for i in proposed.kept))
        self.assertIn(lines[-1], moved)
        self.assert_lossless(raw)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
