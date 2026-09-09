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
                kind, directory, digest, _label, body, summary = replay[0]
                self.assertEqual(kind, expected)
                # owner 2026-09-09 Q5「C」：白名單過關的進 <kind>/，其餘只寫提案；線上
                # 與回放判的是同一句（owner 自己那半），所以落點也必須一樣。
                admitted, _template = capture.auto_admitted(capture.owner_side(body, summary))
                if admitted:
                    self.assertEqual(cards[0].parent.name, directory)
                else:
                    self.assertEqual(
                        cards[0].parent.parent.name, memspec.CAPTURE_PENDING_SUBPATH[-1]
                    )
                    self.assertEqual(cards[0].name[: len(kind)], kind)
                self.assertIn(digest, cards[0].name)
                text = cards[0].read_text(encoding="utf-8")
                self.assertTrue(text.endswith(body + "\n"))
                self.assertIn(f"cwd: {self.root}", text)
                self.assertIn(f"session_id: {name}", text)
                self.assertIn(
                    f"{memspec.PROVENANCE_FIELD}: {memspec.PROVENANCE_AUTO_CAPTURED}", text
                )
                self.assertIn(f"{memspec.VERIFIED_FIELD}: {memspec.VERIFIED_FALSE}", text)
                recall._handle(event, time.monotonic(), [])
                self.assertEqual(list(vault.rglob("*.md")), cards)

    def test_legacy_entrypoints_share_context_and_write_one_card(self):
        prompt = "不要再問這件事，以後一律用第二案。"
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
            digest = capture.grant_digest(prompt)
            event_id, origin, _session = capture.event_identity(event, digest)
            # U-P：檔名尾巴是事件識別，不再只有文句雜湊——同一句話在兩場對話裡各講一
            # 次，同一天會撞到同一個舊檔名，第二件事故就靜靜消失了。
            self.assertEqual(path.name, f"ruling-20260102-{digest}-{event_id}.md")
            self.assertIn(f"{memspec.EVENT_ID_FIELD}: {event_id}", text)
            self.assertIn(f"{memspec.ORIGIN_FIELD}: {origin}", text)
            capture.capture_event(prompt, vault, event, None, replay=replay)
            self.assertEqual(replay.status, capture.STATUS_DUPLICATE)
            self.assertEqual(len(list(vault.rglob("*.md"))), 1)

    def test_the_same_sentence_in_two_sessions_keeps_two_events(self):
        """卡數＝事故數的前提（U-P）：同句不同場各留一張，同一則事件重送只留一張。

        U-P 之前這裡是「同文句一律不重寫」，所以 owner 在三場對話各講一次同一句話只
        會留下一張卡——回饋檢討要算「同一件事被糾正第二次」時，次數已經不在庫裡了。
        """
        prompt = "不是！那個欄位只放小分類，不要放品名"
        vault = self.root / "two-sessions"
        vault.mkdir()
        first = capture.capture_event(
            prompt, vault, self.event("sess-a", prompt, None), None,
            replay=capture.Replay(stamp="2026-01-02T03:04:05Z"),
        )
        second = capture.capture_event(
            prompt, vault, self.event("sess-b", prompt, None), None,
            replay=capture.Replay(stamp="2026-01-02T05:06:07Z"),
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(vault.rglob("*.md"))), 2)

        # 同一則事件重送（同宿主、同場、同位置、同句）：不重寫。
        resent = capture.Replay(stamp="2026-01-02T03:04:05Z")
        self.assertIsNone(capture.capture_event(
            prompt, vault, self.event("sess-a", prompt, None), None, replay=resent))
        self.assertEqual(resent.status, capture.STATUS_DUPLICATE)
        # 同一場對話裡再講一次同一句話仍然只是同一件事。
        later = capture.Replay(stamp="2026-01-03T03:04:05Z")
        event = self.event("sess-a", prompt, None)
        event[capture.MESSAGE_INDEX_KEY] = 99
        self.assertIsNone(capture.capture_event(prompt, vault, event, None, replay=later))
        self.assertEqual(later.status, capture.STATUS_DUPLICATE)
        self.assertEqual(len(list(vault.rglob("*.md"))), 2)

    def test_both_host_shapes_carry_an_event_id_and_origin(self):
        """UserPromptSubmit 的兩種事件形狀都要說得出自己是哪一則事件（U-P 第 2 行）。

        Codex 形狀沒有 `session_id`（鏡像欄是 `sessionId`），連那個都缺時只剩 rollout
        檔名——那本身就是那場對話唯一的名字，所以來源仍然推得出來，不必依賴 SessionEnd。
        """
        digest = capture.grant_digest("x")
        claude = {
            "session_id": "claude-session",
            "transcript_path": str(self.root / "home" / ".claude" / "projects" / "p" / "s.jsonl"),
        }
        codex_mirrored = {
            "sessionId": "codex-session",
            "transcript_path": str(
                self.root / "home" / ".codex" / "sessions" / "2026" / "01" / "02"
                / "rollout-2026-01-02T03-04-05-sessone.jsonl"
            ),
        }
        codex_headless = {"transcript_path": codex_mirrored["transcript_path"]}
        seen = []
        for name, event, host, session in (
            ("claude", claude, capture.HOST_CLAUDE, "claude-session"),
            ("codex-mirrored", codex_mirrored, capture.HOST_CODEX, "codex-session"),
            ("codex-headless", codex_headless, capture.HOST_CODEX,
             "rollout-2026-01-02T03-04-05-sessone"),
        ):
            with self.subTest(shape=name):
                event_id, origin, resolved = capture.event_identity(event, digest)
                self.assertEqual(capture.event_host(event), host)
                self.assertEqual(resolved, session)
                self.assertTrue(origin.startswith(f"{host}/{session}/"))
                self.assertEqual(len(event_id), 12)
                seen.append(event_id)
        self.assertEqual(len(set(seen)), len(seen))
        # 位置判不出來（沒有轉錄檔、沒有行號）時寫 `-`，不冒充位置 0。
        self.assertEqual(capture.event_position({}), capture.ORIGIN_UNKNOWN)
        self.assertEqual(capture.event_host({}), capture.HOST_UNKNOWN)

    def test_non_record_json_does_not_interrupt_capture(self):
        for index, scalar in enumerate((None, [], 5, "text")):
            vault = self.root / f"scalar-{index}"
            vault.mkdir()
            common.write_config(self.config, [vault])
            event = self.event(f"scalar-{index}", "不要再問這件事，以後一律用第二案。", "請你確認要用哪個方案？")
            transcript = Path(event["transcript_path"])
            transcript.write_text(json.dumps(scalar) + "\n" + transcript.read_text(encoding="utf-8"), encoding="utf-8")
            recall._handle(event, time.monotonic(), [])
            cards = list(vault.rglob("*.md"))
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0].parent.name, memspec.RULING_DIRECTORY)

    def test_no_request_or_unrelated_request_preserves_correction_priority(self):
        prompt = "不要再問這件事，以後一律用第二案。"
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
