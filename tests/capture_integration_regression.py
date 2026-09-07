import sys; sys.dont_write_bytecode = True
"""Synthetic live/replay capture parity, provenance and compatibility checks."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude"), str(ROOT / "tests")]
from epitype import capture, harvest, memspec
import _hook_common as common
import recall_hook as recall
from capture_precision import SYNTHETIC


class CaptureIntegrationRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-capture-integration-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "config.json"
        environment = patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            "CODEX_HOME": str(self.root / "home" / ".codex"),
            "EPITYPE_CONFIG": str(self.config), "EPITYPE_DREAM_MODE": "off",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def event(self, name, prompt, question, codex=False):
        transcript = self.root / f"{name}.jsonl"
        message = {"role": "assistant", "content": [{"type": "text", "text": question or ""}]}
        item = {"type": "response_item", "payload": message} if codex else {"type": "assistant", "message": message}
        transcript.write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"prompt": prompt, "session_id": name, "cwd": str(self.root), "transcript_path": str(transcript)}

    def test_actual_hook_matches_replay_for_all_precision_fixtures(self):
        for name, prompt, question, expected in SYNTHETIC:
            with self.subTest(name=name):
                vault = self.root / name
                vault.mkdir()
                common.write_config(self.config, [vault])
                event = self.event(name, prompt, question)
                replay = harvest.candidates(prompt, question)
                with patch.object(capture, "classify", wraps=capture.classify) as classifier:
                    recall._handle(event, time.monotonic(), [])
                cards = list(vault.rglob("*.md"))
                self.assertEqual(len(cards), len(replay))
                self.assertLessEqual(classifier.call_count, 1)
                if not replay:
                    continue
                kind, directory, digest, _label, body, _summary = replay[0]
                self.assertEqual(kind, expected)
                self.assertEqual(cards[0].parent.name, directory)
                self.assertIn(digest, cards[0].name)
                text = cards[0].read_text(encoding="utf-8")
                self.assertTrue(text.endswith(body + "\n"))
                self.assertIn(f"cwd: {self.root}", text)
                self.assertIn(f"session_id: {name}", text)
                recall._handle(event, time.monotonic(), [])
                self.assertEqual(list(vault.rglob("*.md")), cards)

    def test_legacy_entrypoints_share_context_and_write_one_card(self):
        prompt = "就用第二案，以後都不要再問這件事。"
        for codex in (False, True):
            vault = self.root / str(codex)
            event = self.event(str(codex), prompt, "請你確認要用哪個方案？", codex)
            for kind in capture.CAPTURE_KINDS:
                self.assertIsNone(capture.capture_owner_sentence(prompt, vault, event, None, kind))
            replay = capture.Replay(stamp="2026-01-02T03:04:05Z", fields=(("source", "synthetic"),))
            path = capture.capture_ruling(prompt, vault, event, None, replay=replay)
            self.assertEqual(replay.status, capture.STATUS_WRITTEN)
            text = path.read_text(encoding="utf-8")
            self.assertIn("問（助理）：請你確認要用哪個方案？", text)
            self.assertIn("答（owner 逐字）：" + prompt, text)
            self.assertIn("captured_at: 2026-01-02T03:04:05Z", text)
            self.assertIn("source: synthetic", text)
            self.assertEqual(path.name, "ruling-20260102-" + capture.grant_digest(prompt) + ".md")
            capture.capture_event(prompt, vault, event, None, replay=replay)
            self.assertEqual(replay.status, capture.STATUS_DUPLICATE)
            self.assertEqual(len(list(vault.rglob("*.md"))), 1)

    def test_non_record_json_does_not_interrupt_capture(self):
        for index, scalar in enumerate((None, [], 5, "text")):
            vault = self.root / f"scalar-{index}"
            vault.mkdir()
            common.write_config(self.config, [vault])
            event = self.event(f"scalar-{index}", "就用第二案，以後都不要再問這件事。", "請你確認要用哪個方案？")
            transcript = Path(event["transcript_path"])
            transcript.write_text(json.dumps(scalar) + "\n" + transcript.read_text(encoding="utf-8"), encoding="utf-8")
            recall._handle(event, time.monotonic(), [])
            cards = list(vault.rglob("*.md"))
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0].parent.name, memspec.RULING_DIRECTORY)

    def test_no_request_or_unrelated_request_preserves_correction_priority(self):
        prompt = "就用第二案，以後都不要再問這件事。"
        for index, question in enumerate((None, "今天天氣如何？", "報告引用「請你確認要用哪個方案？」")):
            path = capture.capture_event(prompt, self.root / f"v{index}", {}, None,
                                         question=question, replay=capture.Replay())
            self.assertEqual(path.parent.name, memspec.CORRECTION_DIRECTORY)
            self.assertNotIn("問（助理）", path.read_text(encoding="utf-8"))

    def test_noise_and_expired_events_skip_tail_and_writes(self):
        with patch.object(capture, "last_assistant_text", side_effect=AssertionError("unexpected tail read")):
            for prompt in ("一般閒聊", "我同意", "你可以讀取那個資料夾嗎？", "```不要再改```",
                           memspec.GRANT_REJECT_MARKERS[0] + " 不要再亂改設定"):
                with self.subTest(prompt=prompt):
                    self.assertIsNone(capture.capture_event(prompt, self.root, {}, None))
            self.assertIsNone(capture.capture_event("不要再亂改設定", self.root, {},
                              time.monotonic() - memspec.HOOK_TIMEOUT_SECONDS - 1))

    def test_credential_rejection_remains_in_writer(self):
        replay = capture.Replay()
        value = capture.capture_event("不要再用 api_key: abcdefghijklmnop", self.root, {}, None,
                                      question="請你確認要用哪個方案？", replay=replay)
        self.assertIsNone(value)
        self.assertEqual(replay.status, capture.STATUS_REJECTED)
        self.assertEqual(list(self.root.rglob("*.md")), [])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
