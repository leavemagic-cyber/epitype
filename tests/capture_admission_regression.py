import sys; sys.dont_write_bytecode = True
"""自動捕捉的入庫政策（owner 2026-09-09 裁定 Q5「C」）的回歸。

三件事釘在這裡：形狀明確的句子才自動入庫、其餘只落 `_drafts/captured_pending/`、
以及「機器抓的、還沒人核過」的卡不得成為任何閘門的依據。第三件是最容易在改別的
東西時悄悄失守的一件——決策閘、寫檔閘、PreToolUse 授權判定與開場裁定清單各自讀
卡，任何一處改成「事件卡也算」都會讓未核的句子變成授權。
"""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import capture, card_lint, harvest, memsearch, memspec  # noqa: E402
import _hook_common as common  # noqa: E402
import pretooluse_gate  # noqa: E402
import recall_hook as recall  # noqa: E402
import sessionstart_hook as sessionstart  # noqa: E402
import stop_gate  # noqa: E402

# 白名單三型各一句，句子本身是本檔作者編的（privacy_lint 會擋真人原話）。
ADMITTED = (
    ("arrow-answer", "要走甲案還是乙案？<-甲，以後都照這個順序", "請你確認要用哪個方案？"),
    ("leading-correction", "不是！那個欄位只放小分類，不要放品名", None),
    ("explicit-grant", "那個資料夾的整理你可以直接動，以後不用再問我", None),
)
# 現行規則照樣捕捉得到，但形狀不明確：只寫提案。
PROPOSED = (
    ("standing-ruling", "以後都用第一種寫法，一律不要混用", None),
    ("mid-sentence-correction", "那個路徑我說過只能放第二層，不要亂放", None),
)
NEEDLE = "admissionneedle"


class CaptureAdmissionRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-capture-admission-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {
            "EPITYPE_CONFIG": str(self.config), "EPITYPE_DREAM_MODE": "off",
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            "CODEX_HOME": str(self.root / "home" / ".codex"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def capture(self, prompt, question=None, vault=None):
        return capture.capture_event(
            prompt, vault or self.vault,
            {"cwd": str(self.root), "session_id": "admission-session"}, None,
            question=question, replay=capture.Replay(stamp="2026-09-09T01:02:03Z"),
        )

    @property
    def pending_root(self):
        return self.vault.joinpath(*memspec.CAPTURE_PENDING_SUBPATH)

    # ------------------------------------------------------------------ routing
    def test_whitelisted_shapes_are_filed_and_marked_unverified(self):
        for template, prompt, question in ADMITTED:
            with self.subTest(template=template):
                self.assertEqual(capture.auto_admitted(prompt), (True, template))
                path = self.capture(prompt, question)
                self.assertIsNotNone(path, prompt)
                self.assertEqual(path.parent.parent, self.vault)
                self.assertIn(
                    path.parent.name,
                    [name for name, _type in memspec.EVENT_CARD_DIRECTORIES],
                )
                text = path.read_text(encoding="utf-8")
                self.assertIn(
                    f"{memspec.PROVENANCE_FIELD}: {memspec.PROVENANCE_AUTO_CAPTURED}", text)
                self.assertIn(f"{memspec.VERIFIED_FIELD}: {memspec.VERIFIED_FALSE}", text)

    def test_other_captured_shapes_only_propose(self):
        for name, prompt, question in PROPOSED:
            with self.subTest(name=name):
                self.assertIsNotNone(capture.classify(prompt, question), prompt)
                self.assertFalse(capture.auto_admitted(prompt)[0])
                path = self.capture(prompt, question)
                self.assertIsNotNone(path)
                # 檔名與正式卡完全一樣（同 kind、同日期、同 digest），轉正＝原地改欄位再搬。
                self.assertEqual(path.parent.name, "20260909")
                self.assertEqual(path.parent.parent, self.pending_root)
                kind = capture.classify(prompt, question)[0]
                self.assertTrue(path.name.startswith(f"{kind}-20260909-"))
                self.assertIn(f"{memspec.VERIFIED_FIELD}: {memspec.VERIFIED_FALSE}",
                              path.read_text(encoding="utf-8"))

    def test_a_proposal_is_written_once_across_dates(self):
        first = self.capture(PROPOSED[0][1], PROPOSED[0][2])
        replay = capture.Replay(stamp="2026-09-10T01:02:03Z")
        again = capture.capture_event(
            PROPOSED[0][1], self.vault,
            {"cwd": str(self.root), "session_id": "later"}, None,
            question=PROPOSED[0][2], replay=replay,
        )
        self.assertIsNone(again)
        self.assertEqual(replay.status, capture.STATUS_DUPLICATE)
        self.assertEqual(sorted(self.pending_root.rglob("*.md")), [first])

    def test_proposals_are_not_indexed_or_recalled(self):
        prompt = f"以後都用第一種寫法處理 {NEEDLE}，一律不要混用"
        path = self.capture(prompt)
        self.assertEqual(path.parent.parent, self.pending_root)
        memsearch.build_index(self.vault)
        self.assertEqual(memsearch.recall_index(self.vault, NEEDLE)["count"], 0)
        self.assertNotIn(
            path.stem, json.dumps(memsearch.query_index(self.vault, NEEDLE), ensure_ascii=False))
        self.assertNotIn(
            path.resolve(), [Path(item) for item in memsearch.indexed_card_paths(self.vault)])
        value = recall._handle(
            {"prompt": NEEDLE, "session_id": "recall-session", "cwd": str(self.root)},
            time.monotonic(), [],
        )
        self.assertNotIn(path.stem, json.dumps(value, ensure_ascii=False))

    def test_replay_files_and_proposes_exactly_as_the_live_hook_does(self):
        replayed = self.root / "replayed"
        replayed.mkdir()
        for _template, prompt, question in ADMITTED + PROPOSED:
            found = harvest.candidates(prompt, question)
            self.assertEqual(len(found), 1, prompt)
            kind, directory, digest, label, body, summary = found[0]
            capture.write_capture(
                replayed, directory, kind, digest, label, body,
                {"cwd": str(self.root), "session_id": "replay"}, None,
                summary=summary, replay=capture.Replay(stamp="2026-09-09T01:02:03Z"),
            )
            live = self.capture(prompt, question)
            twin = next(replayed.rglob(live.name))
            self.assertEqual(
                twin.relative_to(replayed).parent, live.relative_to(self.vault).parent, prompt)

    def test_a_replay_never_promotes_an_unreviewed_proposal(self):
        proposal = self.capture(PROPOSED[0][1], PROPOSED[0][2])
        counts, lines = harvest.reevaluate(self.pending_root, self.vault, apply=True)
        self.assertEqual(counts["held"], 1)
        self.assertEqual(counts["moved"], 0)
        self.assertTrue(proposal.is_file())
        self.assertTrue(any(text.startswith("HOLD") for text in lines))
        self.assertEqual(list((self.vault / memspec.RULING_DIRECTORY).glob("*.md")), [])
        # 人核過（verified: true）之後同一條命令才把它搬進庫。
        proposal.write_text(
            proposal.read_text(encoding="utf-8").replace(
                f"{memspec.VERIFIED_FIELD}: {memspec.VERIFIED_FALSE}",
                f"{memspec.VERIFIED_FIELD}: {memspec.VERIFIED_TRUE}\n"
                f"{memspec.VERIFIED_BY_FIELD}: owner\n"
                f"{memspec.VERIFIED_AT_FIELD}: 2026-09-09",
            ),
            encoding="utf-8",
        )
        counts, _lines = harvest.reevaluate(self.pending_root, self.vault, apply=True)
        self.assertEqual(counts["held"], 0)
        self.assertEqual(counts["moved"], 1)
        self.assertEqual(len(list((self.vault / memspec.RULING_DIRECTORY).glob("*.md"))), 1)

    def test_the_promoted_card_lints_clean(self):
        promoted = self.vault / memspec.RULING_DIRECTORY / "ruling-20260909-abcdef123456.md"
        promoted.parent.mkdir(parents=True, exist_ok=True)
        promoted.write_text(
            "---\nname: ruling-20260909-abcdef123456\n"
            "description: owner ruling auto-captured 2026-09-09: 以後都用第一種寫法\n"
            "captured_at: 2026-09-09T01:02:03Z\nsession_id: admission-session\n"
            f"{memspec.PROVENANCE_FIELD}: {memspec.PROVENANCE_AUTO_CAPTURED}\n"
            f"{memspec.VERIFIED_FIELD}: {memspec.VERIFIED_TRUE}\n"
            f"{memspec.VERIFIED_BY_FIELD}: owner\n{memspec.VERIFIED_AT_FIELD}: 2026-09-09\n"
            "---\n以後都用第一種寫法，一律不要混用\n",
            encoding="utf-8",
        )
        card_type, findings = card_lint.check_card(
            promoted, promoted.relative_to(self.vault).as_posix())
        self.assertEqual(card_type, memspec.CARD_TYPE_RULING)
        self.assertEqual(
            [reason for level, _rule, reason in findings
             if level in (card_lint.FAIL, card_lint.WARN)], [])

    # ------------------------------------------------------------------ consumers
    def test_an_unverified_card_is_authority_for_no_gate(self):
        """一張帶著決策／trigger 字樣的捕捉卡，四道閘一個都不能認。"""
        forbidden_phrase = "admissionforbidden"
        card = self.capture(
            f"不是！那個一律不要用 {forbidden_phrase}，以後都改用第二種")
        self.assertIsNotNone(card)
        self.assertEqual(card.parent.parent, self.vault)  # 這張是入庫的，不是提案
        started = time.monotonic()
        event = {"cwd": str(self.root), "session_id": "gate-session"}
        config = common.load_config(started)

        # 1. Stop 決策閘：只認 decision_key + status: active 的卡。
        self.assertEqual(list(stop_gate._decisions(self.vault, started)), [])
        # 2. 寫檔閘規則 A 讀的是同一批決策卡，所以它也擋不到。
        target = self.root / "note.md"
        write_value, _notices = pretooluse_gate._write_review(
            {**event, "tool_name": "Write"}, "Write",
            {"file_path": str(target), "content": f"重新提案使用 {forbidden_phrase}"},
            config, started,
        )
        self.assertIsNone(write_value)
        # 3. PreToolUse 授權判定只讀宣告 trigger 的卡。
        self.assertEqual(
            [path.name for path in pretooluse_gate._trigger_card_paths(self.vault)], [])
        # 4. 開場的現行裁定清單同樣要求 decision_key + active。
        self.assertEqual(sessionstart._active_decisions(self.vault, started), [])
        # 5. 喚回端得出來，但只能掛「歷史捕捉」前綴，不能佔決策席。
        memsearch.build_index(self.vault)
        value = recall._handle(
            {"prompt": forbidden_phrase, "session_id": "recall-gate", "cwd": str(self.root)},
            time.monotonic(), [],
        )
        context = (value or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        shown = [line for line in context.splitlines() if card.stem in line]
        self.assertEqual(len(shown), 1, context)
        self.assertTrue(shown[0].startswith("- " + recall._CAPTURE_HISTORY), shown[0])
        self.assertNotIn(memspec.DECISION_PREFIX, shown[0])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
