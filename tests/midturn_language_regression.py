# -*- coding: utf-8 -*-
"""回合中途的語言提醒：工具之間寫給 owner 的文字不是卡片指定的語言，PreToolUse 提醒一次。

回合結束閘只量最後一則，工具之間的進度短訊整則英文也照樣漏過去（2026-10-07、10-10 都
再犯）。這裡驗：英文提醒一次、繁中不提醒、同一段只提醒一次、子代理不提醒、對話檔讀不到
就安靜，以及只讀檔尾。

全部在臨時目錄裡跑：TEMP、HOME、EPITYPE_CONFIG 都改指到這裡，不碰真的記憶庫或標記目錄。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import _hook_common as common
import pretooluse_gate as gate
from epitype import memspec

ENGLISH = ("Now documenting the reason and removal condition in the launcher, and making its "
           "give-up message say what to do next instead of failing quietly.")
CHINESE = "正在把舊的等待腳本收進一個資料夾，再整包送回收筒；只留下三支通用的，`wait_notify.py` 不動。"
ADVICE = "對 owner 一律繁體中文"


class MidturnLanguage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-midturn-language-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / "lang.md").write_text(
            f"---\nname: owner-language\ndescription: {ADVICE}\n"
            f"{memspec.TURN_CHECK_FIELD}: {memspec.TURN_CHECK_OWNER_LANGUAGE}\n"
            f"{memspec.TURN_CHECK_TARGET_FIELD}: zh-Hant\n---\nBody\n", encoding="utf-8")
        config_file = self.root / "config.json"
        common.write_config(config_file, [self.vault])
        self.config = {"vaults": [self.vault], "budget_bytes": 12000}
        scratch = self.root / "tmp"
        scratch.mkdir()
        env = mock.patch.dict(os.environ, {
            "EPITYPE_CONFIG": str(config_file), "EPITYPE_DREAM_MODE": "off",
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            "TEMP": str(scratch), "TMP": str(scratch), "TMPDIR": str(scratch)})
        env.start()
        self.addCleanup(env.stop)
        self.transcript = self.root / "session.jsonl"
        self.rows = []
        self.serial = 0
        self.session = f"midturn-{time.monotonic_ns()}"
        self.prompt("進度？")

    def _row(self, row):
        self.serial += 1
        row.setdefault("uuid", f"row-{self.serial:04d}")
        self.rows.append(row)
        self.transcript.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in self.rows),
            encoding="utf-8")

    def prompt(self, text):
        self._row({"type": "user", "message": {"role": "user", "content": text}})

    def say(self, text):
        self._row({"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}]}})

    def call_tool(self):
        self._row({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"toolu_{self.serial}", "name": "Bash",
             "input": {"command": "git status"}}]}})
        self._row({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{self.serial}",
             "content": [{"type": "text", "text": "Everything is clean, nothing to commit here."}]}]}})

    def event(self, **overrides):
        event = {"session_id": self.session, "transcript_path": str(self.transcript),
                 "cwd": str(self.root), "hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "git status"}}
        event.update(overrides)
        return event

    def line(self, **overrides):
        return gate._midturn_language_line(
            self.event(**overrides), self.config, time.monotonic(), lambda _line: True)

    def state_file(self):
        return common.notice_marker_directory(self.session) / memspec.TURN_LANGUAGE_MIDTURN_STATE_FILE

    def test_english_midturn_text_gives_one_reminder(self):
        self.call_tool()
        self.say(ENGLISH)
        # 工具回傳記在 user 那一側，不結束回合：英文那段之後還有一次回傳也照樣抓得到。
        self.call_tool()
        line = self.line()
        self.assertIsNotNone(line)
        self.assertIn("繁體中文只佔 0%", line)
        self.assertIn(ADVICE, line)
        self.assertLessEqual(len(line), memspec.TURN_LANGUAGE_MIDTURN_MAX_CHARS)
        # 走正式的輸出路徑：放行時附在 additionalContext，不是 deny 也不是 ask。
        self.say(ENGLISH + " Then the second fix.")
        value = gate._allow_context(self.event(), (), self.config, time.monotonic(),
                                    meter_claims=[])
        output = (value or {}).get("hookSpecificOutput", {})
        self.assertNotIn("permissionDecision", output)
        self.assertEqual(output.get("additionalContext", "").count("📌 Epitype：剛才工具之間"), 1)

    def test_zh_hant_text_gives_none(self):
        self.say(CHINESE)
        self.call_tool()
        self.assertIsNone(self.line())
        self.say(CHINESE + " 接著跑 `tests/run_all.py --jobs 4`，看 C:\\Epitype\\repo 的結果。")
        self.assertIsNone(self.line())
        self.assertIsNone(gate._allow_context(self.event(), (), self.config, time.monotonic(),
                                              meter_claims=[]))

    def test_the_same_block_is_reminded_only_once(self):
        self.say(ENGLISH)
        self.assertIsNotNone(self.line())
        self.call_tool()
        self.assertIsNone(self.line())
        self.assertIsNone(self.line())
        # 狀態檔不見了，同一段仍然不會再提醒：提醒本身另有一個只建立一次的標記。
        self.state_file().unlink()
        self.assertIsNone(self.line())
        # 去重是按段，不是整場只說一次：新的一段英文照樣提醒。
        self.say("Next, updating the decision card so it points to the new resident rule.")
        self.assertIsNotNone(self.line())

    def test_short_english_lines_are_measured_together(self):
        # 一句不到門檻的英文短訊判不了；跟下一句合起來夠了就提醒，而且只提醒一次。
        self.say("Let me check the card first.")
        self.assertIsNone(self.line())
        self.say("Now running the narrow tests.")
        self.assertIsNotNone(self.line())
        self.assertIsNone(self.line())

    def test_text_before_the_latest_prompt_is_not_this_turn(self):
        # 上一回合的英文夠多：要是跨過提問一起量，這一回合的中文會被拖到門檻以下。
        self.say(" ".join([ENGLISH] * 3))
        self.prompt("繼續")
        self.say(CHINESE)
        self.assertIsNone(self.line())
        # 壓縮摘要與 isMeta 列不是真人提問，不切回合。
        self.say(ENGLISH)
        self._row({"type": "user", "isCompactSummary": True,
                   "message": {"role": "user", "content": "This session is being continued."}})
        self.assertIsNotNone(self.line())

    def test_a_subagent_event_gives_none(self):
        self.say(ENGLISH)
        self.assertIsNone(self.line(agent_id="agent-1"))
        self.assertIsNone(self.line(agentId="agent-1"))
        self.assertFalse(self.state_file().exists())

    def test_a_missing_or_unreadable_transcript_gives_none(self):
        self.say(ENGLISH)
        self.assertIsNone(self.line(transcript_path=str(self.root / "missing.jsonl")))
        self.assertIsNone(self.line(transcript_path=str(self.root)))
        self.assertIsNone(self.line(transcript_path=None))
        garbage = self.root / "garbage.jsonl"
        garbage.write_bytes(b"\x00\xff{not json\n" * 50)
        self.assertIsNone(self.line(transcript_path=str(garbage)))
        codex = self.root / "rollout.jsonl"
        codex.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": ENGLISH}]}}) + "\n", encoding="utf-8")
        self.assertIsNone(self.line(transcript_path=str(codex)))
        with mock.patch.object(gate, "_owner_language_decisions", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.line())
        self.assertIsNone(self.line(session_id=""))

    def test_no_owner_language_card_gives_none(self):
        (self.vault / "lang.md").unlink()
        self.say(ENGLISH)
        self.assertIsNone(self.line())

    def test_only_the_tail_is_read(self):
        # 回合開頭（真人提問）在讀取上限之外：上限之外的英文段看不到，上限之內的照抓。
        self.say(ENGLISH)
        for _ in range(60):
            self.call_tool()
        self.say("Then " + ENGLISH)
        reads = []
        real_open = Path.open

        class Counting:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *details):
                return self.stream.__exit__(*details)

            def seek(self, *arguments):
                return self.stream.seek(*arguments)

            def read(self, *arguments):
                data = self.stream.read(*arguments)
                reads.append(len(data))
                return data

        def counting_open(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            return Counting(stream) if Path(path) == self.transcript else stream

        with mock.patch.object(memspec, "CONTEXT_METER_TAIL_FIRST_BYTES", 1024), \
             mock.patch.object(memspec, "STOP_GATE_TURN_TAIL_BYTES", 4096), \
             mock.patch.object(Path, "open", counting_open):
            found = gate._midturn_texts(self.transcript, set(), time.monotonic())
        self.assertEqual([text for _id, text in found], ["Then " + ENGLISH])
        self.assertTrue(reads)
        self.assertLessEqual(max(reads), 4096)
        self.assertGreater(self.transcript.stat().st_size, 4 * 4096)


def _selftest():
    suite = unittest.TestLoader().loadTestsFromTestCase(MidturnLanguage)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if not bad else "FAIL", total - bad, total))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
