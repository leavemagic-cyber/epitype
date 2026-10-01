import sys; sys.dont_write_bytecode = True
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import memsearch


class HistorySelection(unittest.TestCase):
    def test_history_is_searchable_but_not_automatically_delivered(self):
        with tempfile.TemporaryDirectory(prefix="history-") as directory:
            vault = Path(directory).resolve()
            for name, fields in (
                ("audit", "description: Historical audit copy of rules needlecheck"),
                ("archive", "description: needlecheck\nscope: historical-audit"),
                ("retired", "description: needlecheck\nstatus: superseded\nsuperseded_by: current.md"),
                ("current", "description: needlecheck current policy")):
                (vault / (name + ".md")).write_text("---\nname: " + name + "\n" + fields + "\n---\nneedlecheck\n", encoding="utf-8")
            memsearch.build_index(vault)
            automatic = memsearch.recall_index(vault, "needlecheck")["results"]
            self.assertEqual([r["name"] for r in automatic], ["current"])
            manual = memsearch.query_index(vault, "needlecheck", include_superseded=True)["results"]
            self.assertEqual({r["name"] for r in manual}, {"audit", "archive", "retired", "current"})


if __name__ == "__main__":
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
