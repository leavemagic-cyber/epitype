import sys; sys.dont_write_bytecode = True
"""Offline selection regressions, with isolated homes, vaults and markers."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get("EPITYPE_SELECTION_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import memsearch, memspec
import _hook_common as common
import recall_hook as recall


class RecallSelectionRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-selection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vaults = [self.root / "first", self.root / "second"]
        for vault in self.vaults:
            vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, self.vaults)
        for instrument in (
            patch.dict(os.environ, {
                memspec.EPITYPE_CONFIG_ENV: str(self.config),
                memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
                "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            }),
            patch.object(recall, "recall_marker_directory", lambda session: self.root / "markers" / session),
        ):
            instrument.start()
            self.addCleanup(instrument.stop)

    def card(self, vault, name, description="refillneedle", decision=False):
        authority = ("decision_key: " + name + "\nstatus: active\n"
                     "current_decision_at: 2026-09-07\ndecided_by: owner-explicit\n"
                     "owner_quote: Keep the synthetic policy\n") if decision else ""
        (vault / (name + ".md")).write_text(
            f"---\nname: {name}\ndescription: {description}\n{authority}---\n",
            encoding="utf-8",
        )

    def populate(self, count=5):
        for index, vault in enumerate(self.vaults):
            for number in range(count):
                self.card(vault, f"fixture-{index}-{number}")
            memsearch.build_index(vault)

    def invoke(self, session="refill", prompt="refillneedle"):
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "session_id": session}))), \
             patch.object(sys, "argv", ["recall"]), \
             patch.object(recall, "_STARTED_AT", time.monotonic()), \
             contextlib.redirect_stdout(output):
            self.assertEqual(recall.main(), 0)
        if not output.getvalue():
            return ""
        return json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]

    @staticmethod
    def lines(context):
        return [line for line in context.splitlines() if line.startswith("- ")]

    def test_seen_cards_do_not_consume_new_slots(self):
        self.populate()
        first, second = self.lines(self.invoke()), self.lines(self.invoke())
        self.assertEqual((len(first), len(second)), (8, 2))
        self.assertTrue(set(first).isdisjoint(second))
        self.assertEqual(self.invoke(), memspec.QUESTION_PREFLIGHT + "\n" + memspec.TURN_CONTINUITY)

    def test_existing_per_vault_candidate_cap_does_not_expand(self):
        self.populate(count=7)
        delivered = self.lines(self.invoke()) + self.lines(self.invoke())
        self.assertEqual(len(set(delivered)), 10)
        self.assertEqual(self.invoke(), memspec.QUESTION_PREFLIGHT + "\n" + memspec.TURN_CONTINUITY)

    def test_delivered_authority_releases_slots_without_losing_priority(self):
        self.populate()
        for number in range(4):
            self.card(self.vaults[1], f"decision-{number}", decision=True)
        memsearch.build_index(self.vaults[1])
        first, second = self.lines(self.invoke()), self.lines(self.invoke())
        self.assertEqual((len(first), len(second)), (8, 6))
        self.assertTrue(all(line.startswith("- " + memspec.DECISION_PREFIX) for line in first[:4]))
        self.assertFalse(any(line.startswith("- " + memspec.DECISION_PREFIX) for line in second))
        self.assertEqual(self.invoke(), memspec.QUESTION_PREFLIGHT + "\n" + memspec.TURN_CONTINUITY)

    def test_absent_session_has_no_persistent_dedupe(self):
        self.populate()
        self.assertEqual(self.invoke(session=""), self.invoke(session=""))
        self.assertEqual(len(self.lines(self.invoke(session=""))), 8)

    def test_stronger_query_evidence_in_later_vault_gets_first_slot(self):
        for vault in self.vaults:
            for number in range(5):
                self.card(vault, f"weak-{number}", "selectneedle")
        third = self.root / "third"
        third.mkdir()
        self.vaults.append(third)
        common.write_config(self.config, self.vaults)
        self.card(third, "answer", "selectneedle detailneedle")
        for vault in self.vaults:
            memsearch.build_index(vault)
        lines = self.lines(self.invoke(prompt="selectneedle detailneedle"))
        self.assertEqual(len(lines), 8)
        self.assertIn("V3/answer.md", lines[0])

    def test_equal_evidence_interleaves_local_ranks_before_vault_ties(self):
        self.populate()
        lines = self.lines(self.invoke())
        self.assertEqual(["V1/" in line for line in lines[:4]], [True, False, True, False])

    def test_single_vault_keeps_existing_search_order(self):
        self.populate()
        common.write_config(self.config, [self.vaults[0]])
        hits = memsearch.recall_index(self.vaults[0], "refillneedle")["results"]
        lines = self.lines(self.invoke())
        self.assertEqual([line.rsplit("/", 1)[-1] for line in lines],
                         [hit["card_path"] for hit in hits])

    def test_authority_precedes_stronger_ordinary_query_evidence(self):
        self.card(self.vaults[0], "policy", "selectneedle", decision=True)
        self.card(self.vaults[1], "answer", "selectneedle detailneedle")
        for vault in self.vaults:
            memsearch.build_index(vault)
        lines = self.lines(self.invoke(prompt="selectneedle detailneedle"))
        self.assertTrue(lines[0].startswith("- " + memspec.DECISION_PREFIX))

    def test_whole_short_card_fills_space_after_oversized_ordinary_hit(self):
        self.card(self.vaults[0], "long", "packneedle " + "長" * 100)
        self.card(self.vaults[0], "short", "packneedle")
        memsearch.build_index(self.vaults[0])
        hits = memsearch.recall_index(self.vaults[0], "packneedle")["results"]
        hits.sort(key=lambda hit: hit["card_path"] != "long.md")
        with patch.object(recall.memsearch, "recall_index", return_value={"results": hits}), \
             patch.object(recall, "resolve_vaults", return_value=[self.vaults[0]]):
            # Measure only the fixed legend/header; keep a whole small card, not the long one.
            full = self.invoke(session="")
            header = full.split("\n- ", 1)[0]
            budget = len(header.encode("utf-8")) + 145
            common.write_config(self.config, [self.vaults[0]], budget=budget)
            packed = self.invoke()
            self.assertIn("V1/short.md", packed)
            self.assertNotIn("V1/long.md", packed)
            self.assertLessEqual(len(packed.encode("utf-8")), budget)
            common.write_config(self.config, [self.vaults[0]])
            retried = self.invoke()
            self.assertIn("V1/long.md", retried)
            self.assertNotIn("V1/short.md", retried)

    def test_oversized_pinned_policy_is_not_bypassed_by_small_ordinary_card(self):
        self.card(self.vaults[0], "policy", "packneedle", decision=True)
        policy = self.vaults[0] / "policy.md"
        policy.write_text(policy.read_text(encoding="utf-8").replace(
            "Keep the synthetic policy", "長" * 500), encoding="utf-8")
        self.card(self.vaults[0], "short", "packneedle")
        memsearch.build_index(self.vaults[0])
        common.write_config(self.config, [self.vaults[0]], budget=700)
        self.assertEqual(self.invoke(prompt="packneedle"), "")
        common.write_config(self.config, [self.vaults[0]])
        self.assertIn("V1/policy.md", self.invoke(prompt="packneedle"))

    def test_merge_keeps_each_vaults_order_even_when_coverage_varies(self):
        groups = [[(1, "a"), (3, "b")], [], [(2, "c"), (0, "d")]]
        self.assertEqual(list(recall._merge_ordinary(groups)), ["c", "a", "b", "d"])

    def test_budget_sweep_keeps_whole_cards_header_and_authority_prefix(self):
        header = memspec.UNTRUSTED_ADVISORY + "\nvaults: V1=synthetic"
        pinned = ["- POLICY-" + "裁定" * 15, "- POLICY-" + "規則" * 30]
        ordinary = ["- CARD-" + ('"\\條文' * size) for size in (3, 150, 5, 7, 9, 11, 13, 15)]
        for budget in range(80, 2200, 37):
            with self.subTest(budget=budget):
                context = recall._bounded_recall([header, *pinned, *ordinary], budget, 3, 1)
                if not context:
                    continue
                self.assertTrue(context.startswith(header))
                self.assertTrue(common.payload_fits("UserPromptSubmit", context, budget))
                selected = self.lines(context)
                self.assertLessEqual(len(selected), memspec.RECALL_TOTAL_MAX_LINES)
                self.assertTrue(all(line in pinned + ordinary for line in selected))
                if any(line in ordinary for line in selected):
                    self.assertEqual(selected[:2], pinned)
                if pinned[1] in selected:
                    self.assertIn(pinned[0], selected)

    def test_recall_metadata_excludes_generic_words_and_query_output_is_unchanged(self):
        self.card(self.vaults[0], "meta", "selectneedle detailneedle the")
        memsearch.build_index(self.vaults[0])
        hit = memsearch.recall_index(self.vaults[0], "the selectneedle detailneedle")["results"][0]
        self.assertEqual(hit["matched_term_count"], 2)
        self.assertNotIn("matched_term_count", memsearch.query_index(self.vaults[0], "selectneedle")["results"][0])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] if "--selftest" in sys.argv else None)
