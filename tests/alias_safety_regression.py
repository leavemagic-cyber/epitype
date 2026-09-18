"""Exercise harvest, rehoming, and batch writes through their real entrypoints."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from epitype import alias_batch, capture_route, card_io, card_lint, harvest, memsearch, memspec


CARD = "---\nname: original\ndescription: fixture\ndecision_key: fixture\nstatus: active\n---\nOwner body\n"


class MutationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.card = self.vault / "card.md"
        self.card.write_bytes(CARD.encode("utf-8"))


    def apply_aliases(self, aliases):
        review = self.root / "review.json"
        review.write_text(json.dumps([{"card_path": "card.md", "suggested": aliases}]), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return alias_batch.cmd_apply(argparse.Namespace(vault=str(self.vault), review=str(review), dry_run=False))


    def test_alias_line_breaks_cannot_close_frontmatter(self):
        self.assertEqual(self.apply_aliases(["safe\n---", "x\rstatus: draft", "x\u2028---", "x\x85---"]), 0)
        self.assertEqual(self.card.read_text(encoding="utf-8"), CARD)
        self.assertEqual(memspec.frontmatter_fields(self.card)[0]["status"], "active")


    def test_alias_punctuation_roundtrips_in_block_and_flow(self):
        wanted = ["a,b", "word # note", "inner[bracket]", 'inner"quote']
        for header in ("", "aliases: [old]\n"):
            with self.subTest(header=header):
                self.card.write_text(CARD.replace("status: active\n", "status: active\n" + header), encoding="utf-8")
                self.assertEqual(self.apply_aliases(wanted), 0)
                actual = memsearch._read_card(self.card)["aliases"].split("\n")
                for alias in wanted:
                    self.assertIn(alias, actual)

    def test_existing_plain_quotes_do_not_merge_aliases(self):
        for first in ("don't", 'plain"quote'):
            self.assertEqual(memsearch._alias_values(f"[{first}, untouched]"), [first, "untouched"])


    def test_alias_write_rejects_concurrent_owner_edit(self):
        collisions = alias_batch._collisions
        def edit_before_write(*args):
            result = collisions(*args)
            self.card.write_text(CARD + "OWNER CONCURRENT EDIT\n", encoding="utf-8")
            return result
        with patch.object(alias_batch, "_collisions", edit_before_write):
            self.assertEqual(self.apply_aliases(["new alias"]), 1)
        self.assertEqual(self.card.read_text(encoding="utf-8"), CARD + "OWNER CONCURRENT EDIT\n")


    def test_dates_use_the_same_stale_write_guard(self):
        report = {"vault": str(self.vault), "cards": [{"path": "card.md", "derived_date": "2026-09-07"}]}
        write = alias_batch._write_card
        def edit_then_write(*args, **kwargs):
            self.card.write_text(CARD + "OWNER DATE EDIT\n", encoding="utf-8")
            return write(*args, **kwargs)
        with patch.object(alias_batch, "_write_card", edit_then_write):
            with self.assertRaises(card_io.CardConflict):
                card_lint._fix_dates(report, False, io.StringIO())
        self.assertEqual(self.card.read_text(encoding="utf-8"), CARD + "OWNER DATE EDIT\n")


class IndentedAliases(unittest.TestCase):
    """宿主的記憶寫入器會把 aliases 重排到 metadata: 底下一層。同義詞清單放哪一層意思
    都一樣，而只認頂層的後果是那張卡在索引下次重建時安靜地不再回應任何別名。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-indented-alias-")
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name).resolve() / "vault"
        self.vault.mkdir()

    def write(self, name, text):
        (self.vault / name).write_text(text, encoding="utf-8")

    def query(self, term):
        memsearch.build_index(str(self.vault))
        result = memsearch.query_index(str(self.vault), term)
        return {(row["card_path"], tuple(row["hit_fields"])) for row in result["results"]}

    def test_an_indented_alias_list_is_still_indexed_and_found(self):
        self.write("project-nested.md",
                   "---\nname: project-nested\ndescription: fixture\n"
                   "last_verified_at: 2026-09-19\nmetadata:\n  type: project\n  aliases:\n"
                   "    - 縮排別名甲\n    - 縮排別名乙\n---\n內文\n")
        self.assertIn(("project-nested.md", ("aliases",)), self.query("縮排別名甲"))

    def test_items_under_an_unrelated_nested_key_are_not_aliases(self):
        # 別名清單在下一個鍵開始時就結束，不然隔壁清單的項目會被收成別名。
        self.write("project-neighbour.md",
                   "---\nname: project-neighbour\ndescription: fixture\n"
                   "last_verified_at: 2026-09-19\nmetadata:\n  aliases:\n    - 真的別名\n"
                   "  hosts:\n    - 隔壁清單的項目\n---\n內文\n")
        self.assertIn(("project-neighbour.md", ("aliases",)), self.query("真的別名"))
        self.assertEqual(self.query("隔壁清單的項目"), set())

    def test_a_malformed_indented_alias_line_is_skipped_not_raised(self):
        # 這一行以前整行被忽略；把它變成解析錯誤會比原本的缺陷更糟。
        self.write("project-broken.md",
                   "---\nname: project-broken\ndescription: 可搜尋的說明\n"
                   "last_verified_at: 2026-09-19\nmetadata:\n  aliases: [\"沒有收尾\n---\n內文\n")
        self.assertTrue(any(path == "project-broken.md" for path, _ in self.query("可搜尋的說明")))

    def test_the_checker_says_where_they_are_instead_of_calling_them_missing(self):
        self.write("project-nested.md",
                   "---\nname: project-nested\ndescription: fixture\n"
                   "last_verified_at: 2026-09-19\nmetadata:\n  type: project\n  aliases:\n"
                   "    - 縮排別名甲\n---\n內文\n")
        _type, findings = card_lint.check_card(self.vault / "project-nested.md", "project-nested.md")
        rules = {(level, rule) for level, rule, _reason in findings}
        self.assertNotIn((card_lint.WARN, "aliases"), rules)
        self.assertIn((card_lint.INFO, "aliases-indented"), rules)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
