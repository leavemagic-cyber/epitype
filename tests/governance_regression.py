import sys; sys.dont_write_bytecode = True
"""Synthetic regressions for decision authority, freshness and hook delivery."""

import contextlib
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

from epitype import memsearch, memspec
import _hook_common as common
import recall_hook as recall
import stop_gate as stop
import precompact_hook as compact
import sessionstart_hook as start


class GovernanceRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-governance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {
            memspec.EPITYPE_CONFIG_ENV: str(self.config),
            memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
            "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        markers = patch.object(recall, "recall_marker_directory",
                               lambda session: self.root / "markers" / session)
        markers.start()
        self.addCleanup(markers.stop)

    def card(self, source="owner-explicit", quote="Keep the synthetic setting"):
        path = self.vault / "decision.md"
        path.write_text(
            "---\nname: fixturedecision\ndescription: fixturedecision current rule\n"
            "decision_key: fixturedecision\nstatus: active\n"
            "current_decision_at: 2026-09-07\n"
            f"decided_by: {source}\nowner_quote: {quote}\n"
            "aliases: [設定甲, 設定乙]\nforbidden: [forbiddenfixture]\n---\n",
            encoding="utf-8",
        )
        return path

    def test_only_explicit_owner_decisions_block_reasking(self):
        for source in memspec.DECIDED_BY_VALUES:
            with self.subTest(source=source):
                self.card(source)
                decisions = stop._decisions(self.vault, time.monotonic())
                self.assertEqual(len(decisions), 1)
                decision = decisions[0]
                self.assertEqual(
                    stop._asks_again(decision, "設定甲和設定乙要不要調整？"),
                    source == memspec.OWNER_EXPLICIT_DECIDER,
                )
                self.assertEqual(stop._forbidden_fragment(decision, "forbiddenfixture", []),
                                 "forbiddenfixture")
        self.card(quote="")
        decision = stop._decisions(self.vault, time.monotonic())[0]
        self.assertFalse(stop._asks_again(decision, "設定甲和設定乙要不要調整？"))

    def test_retired_or_unreadable_decision_cannot_return_as_ordinary_context(self):
        for directory, removed in (("", False), ("rulings", False), ("corrections", True)):
            with self.subTest(directory=directory, removed=removed):
                path = self.card()
                if directory:
                    destination = self.vault / directory / path.name
                    destination.parent.mkdir()
                    path = path.rename(destination)
                memsearch.build_index(self.vault)
                if removed:
                    path.unlink()
                else:
                    path.write_text(path.read_text(encoding="utf-8").replace(
                        "status: active", "status: superseded"), encoding="utf-8")
                self.assertFalse(memsearch._is_stale(self.vault, memsearch._db_path(self.vault)))
                value = recall._handle({"prompt": "fixturedecision"}, time.monotonic())
                context = (value or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
                self.assertNotIn("fixturedecision", context)

    def recall_once(self, session="delivery"):
        output = io.StringIO()
        event = {"prompt": "fixturedecision", "session_id": session}
        with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
             patch.object(sys, "argv", ["recall"]), \
             patch.object(recall, "_STARTED_AT", time.monotonic()), \
             contextlib.redirect_stdout(output):
            self.assertEqual(recall.main(), 0)
        return output.getvalue()

    def test_deadline_before_output_leaves_recall_retryable(self):
        self.card()
        memsearch.build_index(self.vault)
        real_handle = recall._handle
        late = False

        def delayed(*args, **kwargs):
            nonlocal late
            value = real_handle(*args, **kwargs)
            late = True
            return value

        with patch.object(recall, "_handle", delayed), \
             patch.object(recall, "expired", lambda _started: late):
            self.assertEqual(self.recall_once(), "")
        self.assertIn("fixturedecision", self.recall_once())
        self.assertEqual(self.recall_once(), "")

    def test_output_failure_leaves_recall_retryable(self):
        self.card()
        memsearch.build_index(self.vault)
        with patch.object(recall, "emit", side_effect=OSError("synthetic output failure")):
            self.assertEqual(self.recall_once(), "")
        self.assertIn("fixturedecision", self.recall_once())
        self.assertEqual(self.recall_once(), "")

    def test_missing_vault_keeps_reads_but_never_redirects_capture(self):
        self.card()
        memsearch.build_index(self.vault)
        missing = self.root / "missing"
        for vaults in ([missing, self.vault], [self.vault, missing]):
            with self.subTest(vaults=vaults):
                common.write_config(self.config, vaults)
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors), \
                     patch.object(recall, "_capture_owner_sentence") as capture:
                    value = recall._handle({"prompt": "fixturedecision"}, time.monotonic())
                    config = common.load_config(time.monotonic())
                context = (value or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
                self.assertIn("fixturedecision", context)
                self.assertIn("unavailable", errors.getvalue())
                capture.assert_not_called()
                self.assertEqual(config[memspec.CONFIG_VAULTS_FIELD], vaults)
                self.assertEqual(common.resolve_vaults(config, {}), [self.vault])
                with self.assertRaises(OSError):
                    common.governance_vault(config, for_write=True)
                self.assertFalse(missing.exists())

    def test_budget_omitted_cards_remain_eligible(self):
        path = self.card(quote="x" * 120)
        second = self.vault / "second.md"
        second.write_text(path.read_text(encoding="utf-8").replace(
            "decision_key: fixturedecision", "decision_key: secondfixture"), encoding="utf-8")
        memsearch.build_index(self.vault)
        value = recall._handle({"prompt": "fixturedecision"}, time.monotonic())
        context = value["hookSpecificOutput"]["additionalContext"]
        head, first, _second = context.rsplit("\n", 2)
        suffix = memspec.CONTEXT_TRUNCATED_SUFFIX.format(dropped=1)
        budget = len((head + "\n" + first + "\n" + suffix).encode("utf-8"))
        common.write_config(self.config, [self.vault], budget=budget)
        first_output = json.loads(self.recall_once())["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(sum(line.startswith("- ") for line in first_output.splitlines()), 1)
        second_output = json.loads(self.recall_once())["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(sum(line.startswith("- ") for line in second_output.splitlines()), 1)
        self.assertNotIn(first, second_output)
        self.assertEqual(self.recall_once(), "")

    def test_degraded_governance_writers_are_paused(self):
        self.card()
        common.write_config(self.config, [self.root / "missing", self.vault])
        with contextlib.redirect_stderr(io.StringIO()):
            config = common.load_config(time.monotonic())
            with self.assertRaises(OSError):
                stop._commitments({}, "I will finish fixturedecision", config, time.monotonic())
            stop._audit(config, "fixturedecision", "question", time.monotonic())
            transcript = self.root / "transcript.jsonl"
            transcript.write_text('{"type":"user","message":{"content":"fixture"}}\n', encoding="utf-8")
            with patch.object(compact, "clear_recall_markers") as cleared:
                with self.assertRaises(OSError):
                    compact._handle({"transcript_path": str(transcript), "session_id": "fixture"},
                                    time.monotonic())
                cleared.assert_called_once_with("fixture")
            with patch.object(start, "_dream_spawn") as spawn, \
                 patch.object(start, "_dream_notice") as notice:
                self.assertIsNotNone(start._handle({}, time.monotonic()))
                spawn.assert_not_called()
                notice.assert_not_called()
        self.assertFalse((self.vault / memspec.GATE_LOG_FILENAME).exists())
        self.assertFalse((self.vault / memspec.FTS_INDEX_DIRECTORY / memspec.COMMITMENT_LEDGER_FILENAME).exists())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] if "--selftest" in sys.argv else None)
