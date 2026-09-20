import sys; sys.dont_write_bytecode = True
"""Bounded current-content authority checks and eventual negative-cache discovery."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import cardscan, memsearch, memspec
import _hook_common as common
import stop_gate as stop

CARD = ("---\nname: fixture\ndescription: fixture rule\ndecision_key: fixture\nstatus: active\n"
        "current_decision_at: 2026-01-02\ndecided_by: owner-explicit\nowner_quote: Old owner quote\n"
        "aliases: [aliasold, otherold]\nforbidden: [badold]\n---\nbody\n")
# Filler the gate must ignore entirely. Since 2026-09-16 an arming field makes a plain
# behaviour card speak even without a decision key, so both have to go.
NON_CARD = CARD.replace("decision_key:", "decision_kex:").replace("forbidden: [badold]\n", "")


class StopFreshnessRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-stop-freshness-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            "CODEX_HOME": str(self.root / "home" / ".codex"),
            "EPITYPE_CONFIG": str(self.config), "EPITYPE_DREAM_MODE": "off",
        })
        environment.start()
        self.addCleanup(environment.stop)
        marker = patch.object(stop, "recall_marker_directory",
                              lambda session: self.root / "markers" / common.session_component(session))
        marker.start()
        self.addCleanup(marker.stop)

    def card(self, name="decision.md", text=CARD):
        path = self.vault / name
        path.write_text(text, encoding="utf-8")
        return path

    def decisions(self):
        return stop._decisions(self.vault, time.monotonic())

    def replace_preserving_metadata(self, path, before, after):
        info = path.stat()
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(before, after), encoding="utf-8")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(path.stat().st_size, info.st_size)
        return info

    def test_actual_hook_stops_using_implicit_authority_with_identical_scan_signature(self):
        card = self.card()
        event = {"session_id": "before", "last_assistant_message": "aliasold 和 otherold 要不要調整？"}
        self.assertEqual(stop._handle(event, time.monotonic(), [])["decision"], "block")
        scan = [(key, stamp, size) for key, _, stamp, size in memsearch.scan_cards(self.vault)]
        self.replace_preserving_metadata(card, "owner-explicit", "owner-implicit")
        self.assertEqual(scan, [(key, stamp, size) for key, _, stamp, size in memsearch.scan_cards(self.vault)])
        event["session_id"] = "after"
        self.assertIsNone(stop._handle(event, time.monotonic(), []))
        self.assertEqual(self.decisions()[0].decided_by, "owner-implicit")

    def test_current_status_quote_date_aliases_and_forbidden_are_read(self):
        for old, new, field in (("Old owner quote", "New owner quote", "quote"),
                                ("2026-01-02", "2026-01-03", "decided_at"),
                                ("aliasold", "aliasnew", "aliases"),
                                ("badold", "badnew", "forbidden"), ("active", "paused", None)):
            with self.subTest(field=field):
                card = self.card()
                self.assertEqual(len(self.decisions()), 1)
                self.replace_preserving_metadata(card, old, new)
                found = self.decisions()
                if field is None:
                    self.assertEqual(found, [])
                else:
                    self.assertIn(new, getattr(found[0], field))
                    self.assertNotIn(old, getattr(found[0], field))

    def test_shared_parser_reads_all_fields_from_one_bounded_head(self):
        for ending in ("---", "..."):
            text = CARD.replace("owner_quote: Old owner quote", "owner_quote: >\n  First line\n  Second line")
            text = text.replace("---\nbody", ending + "\nbody")
            card = self.card(text="\ufeff" + text)
            self.assertEqual(memspec.frontmatter_fields(card), memspec.frontmatter_text("\ufeff" + text))
            with patch.object(memspec, "frontmatter_fields", side_effect=AssertionError("second file read")):
                self.assertEqual(stop._read_decision(card)["quote"], "First line Second line")
        card = self.card(text="---\n" + "x" * memspec.STOP_GATE_FRONTMATTER_MAX_BYTES + "\n" + CARD)
        self.assertIsNone(stop._read_decision(card))
        card.write_bytes(CARD.encode("utf-8").replace(b"Old owner quote", b"\xff"))
        self.assertIsNone(stop._read_decision(card))

    def test_negative_promotion_with_all_identity_metadata_unchanged_is_eventually_discovered(self):
        for index in range(memspec.STOP_GATE_MAX_CARDS_PER_VAULT + 1):
            self.card(f"n{index:03d}.md", NON_CARD)
        self.decisions()
        self.decisions()
        cache = self.vault / memspec.FTS_INDEX_DIRECTORY / stop._DECISION_CACHE_FILENAME
        _manifest, _rulings, cursor = stop._read_cache(cache)
        card = self.vault / cursor
        before = dict((row[0], row) for row in cardscan.scan_vault(self.vault))[cursor]
        self.replace_preserving_metadata(card, "decision_kex:", "decision_key:")
        # 掃描讀的是 os.scandir 的 stat，不是 Path.stat；而清單多比一欄建立時間，Linux
        # 一寫入就會動。要問的是「三個數字都沒變的時候」，所以直接把那一列釘回原值。
        original_scan = cardscan.scan_vault

        def unchanged_signature(vault):
            return [before if row[0] == cursor else row for row in original_scan(vault)]

        with patch.object(cardscan, "scan_vault", unchanged_signature):
            self.assertEqual(self.decisions(), [])
            self.assertEqual([item.path for item in self.decisions()], [card])

    def test_every_known_decision_is_enforced_while_discovery_stays_capped(self):
        """The cap bounds discovery, never enforcement.

        Capping enforcement meant a card past the limit silently stopped applying,
        and the ordering could not rescue it: a card earns priority by having been
        caught, and it can only be caught if it is read. 2026-09-17 review measured
        500 armed cards in a 5000-card vault coming out as 12 live per turn.
        """
        cap = memspec.STOP_GATE_MAX_CARDS_PER_VAULT
        for index in range(cap + 5):
            self.card(f"d{index:03d}.md")
        for index in range(320):
            self.card(f"n{index:03d}.md", NON_CARD)
        for _ in range(13):
            self.decisions()
        with patch.object(stop, "_read_decision", wraps=stop._read_decision) as reader:
            self.assertEqual(len(self.decisions()), cap + 5)
            self.assertLessEqual(reader.call_count, 3 * cap)
        for index in range(cap):
            self.replace_preserving_metadata(self.vault / f"d{index:03d}.md", "active", "paused")
        found = self.decisions()
        self.assertTrue(all(item.path.name >= f"d{cap:03d}.md" for item in found))

    def test_deadline_never_returns_unread_cached_authority_and_keeps_discovery_progress(self):
        self.card("a.md")
        self.card("b.md")
        reads = 0
        original = stop._read_decision

        def read(path):
            nonlocal reads
            reads += 1
            return original(path)

        with patch.object(stop, "_read_decision", read), patch.object(stop, "expired", lambda _: reads > 0):
            self.assertEqual(self.decisions(), [])
        cache = self.vault / memspec.FTS_INDEX_DIRECTORY / stop._DECISION_CACHE_FILENAME
        self.assertEqual(len(stop._read_cache(cache)[0]), 1)
        self.assertEqual(len(self.decisions()), 2)

    def test_v2_upgrade_reuses_only_discovery_and_rechecks_existing_authority(self):
        for index in range(40):
            self.card(f"a{index:03d}.md", NON_CARD)
        card = self.card("z-decision.md")
        stale = stop._read_decision(card)
        self.replace_preserving_metadata(card, "owner-explicit", "owner-implicit")
        cache = self.vault / memspec.FTS_INDEX_DIRECTORY / stop._DECISION_CACHE_FILENAME
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"version": 2, "manifest": {}, "decisions": {card.name: stale}}), encoding="utf-8")
        self.assertEqual(self.decisions()[0].decided_by, "owner-implicit")
        self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["version"], stop._DECISION_CACHE_VERSION)

    def test_bounded_read_can_split_utf8_body_after_complete_frontmatter(self):
        card = self.card()
        head = CARD.encode("utf-8")
        body = head + b"a" * (memspec.STOP_GATE_FRONTMATTER_MAX_BYTES - len(head) - 1)
        card.write_bytes(body + ("中文" * 100).encode("utf-8"))
        card.read_text(encoding="utf-8")
        self.assertEqual(stop._read_decision(card)["key"], "fixture")
        event = {"session_id": "split-body", "last_assistant_message": "aliasold 和 otherold 要不要調整？"}
        self.assertEqual(stop._handle(event, time.monotonic(), [])["decision"], "block")

    def test_timeout_in_the_scan_loop_is_never_silent(self):
        """A timeout before a card is recognised is the least visible one.

        Those cards never enter the candidate list, so they are missing from the
        "checked N of M" denominator too: the turn reports as fully checked. New and
        just-edited cards sort exactly here — the ones that most need to apply.
        """
        for index in range(5):
            self.card(f"d{index:03d}.md")
        calls = []

        def expired(_started_at):
            calls.append(1)
            return len(calls) > 5  # 盤點那 5 次先放過，逾時落在辨認迴圈裡

        defects = []
        with patch.object(stop, "expired", expired):
            self.assertEqual(stop._decisions(self.vault, time.monotonic(), defects), [])
        self.assertTrue(any("連認都還沒認" in line for line in defects), defects)
        self.assertTrue(any("5" in line for line in defects), defects)

    def test_timeout_on_the_last_candidate_still_reports(self):
        """`continue` relies on the next iteration to report. There isn't one here.

        On the final candidate the loop simply ends: zero decisions, zero defects —
        identical to "checked everything, nothing to block".
        """
        self.card("only.md")
        self.decisions()  # 先讓快取認得這張卡，才會走到候選迴圈
        calls = []

        def expired(_started_at):
            # 逾時必須剛好落在「重讀完這張卡之後」那一次檢查：1=盤點、2=候選迴圈開頭
            # （辨認迴圈此時已空）、3=重讀之後。落在第 2 次的話迴圈開頭就報了缺陷，測到
            # 的是別條路徑——這個測試第一版就是那樣寫的，拿掉修正照樣綠。
            calls.append(1)
            return len(calls) > 2

        defects = []
        with patch.object(stop, "expired", expired):
            self.assertEqual(stop._decisions(self.vault, time.monotonic(), defects), [])
        self.assertEqual(len(calls), 3, "呼叫次序變了，這個測試已經測不到原本那條路徑")
        self.assertTrue(any("0/1" in line for line in defects), defects)

    def test_malformed_cached_value_remains_eligible_for_discovery(self):
        self.card()
        self.decisions()
        cache = self.vault / memspec.FTS_INDEX_DIRECTORY / stop._DECISION_CACHE_FILENAME
        for value in ({}, {"key": ""}, [], 5):
            saved = json.loads(cache.read_text(encoding="utf-8"))
            saved["decisions"]["decision.md"] = value
            cache.write_text(json.dumps(saved), encoding="utf-8")
            self.assertEqual([item.key for item in self.decisions()], ["fixture"])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
