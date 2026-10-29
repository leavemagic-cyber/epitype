import sys; sys.dont_write_bytecode = True
"""Captured quotes stay out of recall, stay reachable by memsearch (U-H)."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude"), str(ROOT / 'tests')]
from epitype import capture, memsearch, memspec, rule_pipeline, rule_provenance
from rule_writer_sources_regression import fixture_capture_event, fixture_card
import _hook_common as common
import recall_hook as recall

TAIL = "但是只能在隔離副本操作，禁止修改正式資料"
QUOTE = "fixturehistory 這次先整理" + "相關資料與規則的對照，" * 9 + TAIL


class CaptureRecallRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-capture-recall-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {
            "EPITYPE_CONFIG": str(self.config), "EPITYPE_DREAM_MODE": "off",
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def card(self, quote=QUOTE, kind="ruling", legacy=False):
        event = {"cwd": "synthetic-project", "session_id": "synthetic-source-session"}
        replay = capture.Replay(stamp="2026-01-02T03:04:05Z")
        event = fixture_capture_event((kind, quote, None, quote), self.vault, event, replay,
            source_root=self.root / 'synthetic-originals')
        path = capture.write_capture(
            self.vault, kind + "s", kind, capture.grant_digest(quote),
            f"owner {kind} auto-captured", quote,
            event, None, replay=replay,
            source_text="不要再這樣做",
        )
        if legacy:
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            lines = [line[:line.index(": ", 13) + 2] + capture.one_line(quote)[:80]
                     if line.startswith("description: ") else line for line in lines]
            plain = '\n'.join(line for line in lines if not line.startswith(rule_provenance.FIELD + ':')) + '\n'
            path.write_text(fixture_card(plain, path, source_root=self.root / 'synthetic-originals',
                case_id='legacy-description'), encoding='utf-8')
        self.assertEqual(replay.status, capture.STATUS_WRITTEN)
        self.assertEqual(rule_pipeline.admission('', path.read_text(encoding='utf-8'), 'decision', path), [])
        memsearch.build_index(self.vault)
        return path

    def plain_card(self, name="plain", needle="fixturehistory"):
        path = self.vault / f"{name}.md"
        path.write_text(f"---\nname: {name}\ndescription: {needle} 的整理卡\n---\n{needle} body\n",
                        encoding="utf-8")
        memsearch.build_index(self.vault)
        return path

    def invoke(self, session="", budget=10240):
        config = common.load_config(time.monotonic())
        config[memspec.CONFIG_BUDGET_BYTES_FIELD] = budget
        markers = []
        with patch.object(recall, "load_config", return_value=config):
            value = recall._handle({"prompt": "fixturehistory", "session_id": session,
                                    "cwd": "unrelated-current-project"}, time.monotonic(), markers)
        if value:
            self.assertTrue(common.payload_fits("UserPromptSubmit",
                            value["hookSpecificOutput"]["additionalContext"], budget))
        return value, markers

    def context(self):
        value = self.invoke()[0]
        return value["hookSpecificOutput"]["additionalContext"] if value else ""

    def searched(self, prompt="fixturehistory"):
        return [item["card_path"] for item in
                memsearch.recall_index(self.vault, prompt).get("results", ())]

    def test_new_description_retains_trailing_condition(self):
        path = self.card()
        fields, _ = memspec.frontmatter_fields(path)
        self.assertIn(TAIL, fields["description"])

    def test_captured_ruling_is_never_injected(self):
        path = self.card()
        self.plain_card()
        context = self.context()
        self.assertIn("plain", context)
        for absent in (path.name, path.stem, TAIL, "rulings/"):
            self.assertNotIn(absent, context)

    def test_captured_grant_is_never_injected(self):
        quote = "fixturehistory 你可以整理測試素材\n" + "保留對照資料；" * 16 + "本次授權到測試結束為止"
        path = self.card(quote, kind="grant", legacy=True)
        self.plain_card()
        context = self.context()
        self.assertNotIn(path.stem, context)
        self.assertNotIn("本次授權到測試結束為止", context)

    def test_a_vault_of_quotes_alone_injects_nothing(self):
        self.card()
        self.card("fixturehistory 另一句原話", kind="correction")
        self.assertEqual(self.invoke()[0], None)

    def test_quotes_recall_skips_are_still_reachable_by_memsearch(self):
        path = self.card()
        self.assertIn(f"{memspec.RULING_DIRECTORY}/{path.name}", self.searched())

    def test_a_promoted_decision_card_keeps_its_pinned_seat(self):
        path = self.card()
        text = path.read_text(encoding="utf-8")
        head, _, rest = text.partition("\n---\n")
        head = '\n'.join(line for line in head.splitlines() if not line.startswith(rule_provenance.FIELD + ':'))
        final_text = (
            head
            + f"\n{memspec.DECISION_KEY_FIELD}: fixturehistory-key"
            + f"\n{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}"
            + f"\n{memspec.CURRENT_DECISION_AT_FIELD}: 2026-01-02"
            + f"\n{memspec.OWNER_QUOTE_FIELD}: fixturehistory 一律照這條走\n---\n"
            + rest
        )
        path.write_text(fixture_card(final_text, path, source_root=self.root / 'synthetic-originals',
            case_id='promoted-decision-final'), encoding='utf-8')
        self.assertEqual(rule_pipeline.admission('', path.read_text(encoding='utf-8'), 'decision', path), [])
        memsearch.build_index(self.vault)
        context = self.context()
        self.assertIn(memspec.DECISION_PREFIX + "fixturehistory-key（2026-01-02）", context)
        # The current source contract requires the original, AI author and full
        # scope together; the former quote-hiding expectation is superseded.
        self.assertIn('原始 Owner 引句：fixturehistory 一律照這條走', context)
        self.assertIn(memspec.RECALL_SOURCE_AI.format(author='synthetic-test-ai'), context)
        document = json.loads(memspec.frontmatter_fields(path)[0][rule_provenance.FIELD])
        self.assertIn('適用範圍＝' + rule_provenance.serialize(document['scope']), context)
        self.assertEqual(context.count(rule_provenance.serialize(document['scope'])), 1)

    def test_budget_preserves_pointer_then_drops_whole_card_without_marker(self):
        path = self.plain_card()
        full, _ = self.invoke()
        size = len(full["hookSpecificOutput"]["additionalContext"].encode("utf-8"))
        value, markers = self.invoke("pointer", size - 1)
        context = value["hookSpecificOutput"]["additionalContext"] if value else ""
        self.assertIn(path.name, context)
        self.assertNotIn("原文節錄", context)
        self.assertTrue(any(digest.startswith("card-") for _session, digest in markers))
        value, markers = self.invoke("tiny", 100)
        context = value["hookSpecificOutput"]["additionalContext"] if value else ""
        self.assertNotIn(path.name, context)
        self.assertFalse(any(digest.startswith("card-") for _session, digest in markers))
        self.assertIn(path.stem, self.context())

    def test_unreviewed_capture_is_pending_and_not_searchable(self):
        replay = capture.Replay(stamp='2026-01-02T03:04:05Z')
        path = capture.write_capture(self.vault, memspec.RULING_DIRECTORY, 'ruling',
            capture.grant_digest(QUOTE), 'owner ruling auto-captured', QUOTE,
            {'cwd': 'synthetic-project'}, None, replay=replay, source_text='不要再這樣做')
        self.assertEqual(replay.status, capture.STATUS_PENDING)
        self.assertIn('captured_pending', path.parts)
        memsearch.build_index(self.vault)
        self.assertEqual(self.searched(), [])
        self.assertEqual(self.invoke()[0], None)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
