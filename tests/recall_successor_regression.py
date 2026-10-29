import sys; sys.dont_write_bytecode = True
"""Current guidance and host delivery must reach the live end of a rule chain."""
from pathlib import Path
import contextlib
import io
import json
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from epitype import memsearch
import recall_selection_regression as selection
import recall_hook as recall


def card(vault, name, body="", status="", successor=""):
    path = vault / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {path.stem}\ndescription: {path.stem} policy\n"
        f"status: {status}\nsuperseded_by: {successor}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


class SuccessorSearch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-successors-")
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name).resolve()

    def check_guidance(self, expected):
        for lookup in (memsearch.query_index, memsearch.recall_index):
            with self.subTest(lookup=lookup.__name__):
                self.assertEqual(lookup(self.vault, "legacyneedle")["guidance"], expected)

    def test_chain_reaches_current_and_deduplicates(self):
        card(self.vault, "old.md", "legacyneedle", "superseded", "middle.md")
        card(self.vault, "other.md", "legacyneedle", "superseded", "middle.md")
        card(self.vault, "middle.md", "retired text", "superseded", "current.md")
        current = card(self.vault, "current.md", "current wording")
        memsearch.build_index(self.vault)
        self.check_guidance([memsearch._CURRENT_DECISION_GUIDANCE + str(current)])

    def test_stale_index_uses_live_successor_status(self):
        card(self.vault, "old.md", "legacyneedle", "superseded", "middle.md")
        card(self.vault, "middle.md", "earlier current wording")
        memsearch.build_index(self.vault)
        card(self.vault, "middle.md", "retired text", "superseded", "current.md")
        current = card(self.vault, "current.md", "new current wording")
        self.check_guidance([memsearch._CURRENT_DECISION_GUIDANCE + str(current)])

    def test_cycle_missing_history_and_noncard_never_claim_current(self):
        for target in ("cycle.md", "missing.md", "history.md", "plain.md", "malformed.md"):
            with self.subTest(target=target):
                card(self.vault, "old.md", "legacyneedle", "superseded", target)
                card(self.vault, "cycle.md", "", "superseded", "old.md")
                card(self.vault, "history.md", "", "historical")
                (self.vault / "plain.md").write_text("plain text", encoding="utf-8")
                (self.vault / "malformed.md").write_text(
                    "---\nname: malformed\ndescription: bad header\n", encoding="utf-8",
                )
                memsearch.build_index(self.vault)
                self.check_guidance([])

    def test_relative_name_and_vault_boundary(self):
        card(self.vault, "nested/old.md", "legacyneedle", "superseded", "middle")
        card(self.vault, "nested/middle.md", "", "superseded", "current")
        current = card(self.vault, "nested/current.md", "current")
        memsearch.build_index(self.vault)
        self.check_guidance([memsearch._CURRENT_DECISION_GUIDANCE + str(current)])
        card(self.vault, "nested/middle.md", "", "superseded", "../../outside.md")
        self.check_guidance([])

    def test_unindexed_private_target_and_deleted_index_entry_are_rejected(self):
        card(self.vault, "old.md", "legacyneedle", "superseded", "current.md")
        current = card(self.vault, "current.md", "current")
        memsearch.build_index(self.vault)
        current.unlink()
        self.check_guidance([])
        card(self.vault, "_private/current.md", "private")
        card(self.vault, "old.md", "legacyneedle", "superseded", "_private/current.md")
        memsearch.build_index(self.vault)
        self.check_guidance([])

    def test_indexed_link_cannot_resolve_outside_vault(self):
        # The resolver must distrust even an indexed pathname at read time.
        outside = self.vault.parent / (self.vault.name + "-outside.md")
        outside.write_text("outside", encoding="utf-8")
        self.addCleanup(outside.unlink)
        link = self.vault / "current.md"
        link.write_text("indexed file", encoding="utf-8")
        resolve = Path.resolve
        # Windows may forbid symlink creation; exercise its resolved destination
        # at the resolver boundary without weakening the containment assertion.
        with patch.object(Path, "resolve", lambda path: outside if path == link else resolve(path)):
            self.assertIsNone(memsearch._successor_path(
                self.vault, "old.md", "current.md", ["current.md"],
            ))


class SuccessorHost(unittest.TestCase):
    setUp = selection.RecallSelectionRegression.setUp

    def invoke(self, event_key):
        output = io.StringIO()
        event = {"prompt": "legacyneedle", event_key: event_key}
        with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
             patch.object(sys, "argv", ["recall"]), \
             patch.object(recall, "_STARTED_AT", time.monotonic()), \
             contextlib.redirect_stdout(output):
            self.assertEqual(recall.main(), 0)
        if not output.getvalue():
            return ""
        return json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]

    def test_successor_without_lexical_overlap_reaches_both_event_formats(self):
        vault = self.vaults[0]
        card(vault, "old.md", "legacyneedle", "superseded", "middle.md")
        card(vault, "middle.md", "retired", "superseded", "current.md")
        card(vault, "current.md", "Current instruction without the old search term")
        memsearch.build_index(vault)
        for event_format in ("session_id", "sessionId"):
            with self.subTest(event_format=event_format):
                context = self.invoke(event_format)
                self.assertIn("current.md", context)
                self.assertIn("繼任 | ", context)
                self.assertIn("Current instruction without the old search term", context)
                self.assertNotIn("old.md", context)
                self.assertNotIn("middle.md", context)

    def test_current_match_and_redirect_are_delivered_once(self):
        vault = self.vaults[0]
        card(vault, "old.md", "legacyneedle", "superseded", "current.md")
        card(vault, "current.md", "legacyneedle current instruction")
        memsearch.build_index(vault)
        context = self.invoke("session_id")
        self.assertEqual(context.count("V1/current.md"), 1)
        self.assertNotIn("old.md", context)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
