import sys; sys.dont_write_bytecode = True
"""夜間重放量不量得到「規則有沒有被照做」：兩個宿主都算、接續點不卡死、送達後違反看得見。

2026-09-28 實查：重放只從記憶庫路徑回推 `~/.claude/projects`，Codex 的 rollout 一份都沒
被重放過；接續點碰到上限或任何錯誤就不前進，而掃描永遠取最新的 250 份，積壓後舊的永遠
讀不到；喚回把卡送進對話之後違反了，沒有任何報表看得到；稽核帳用日期取整又只按
(場次, 卡) 計數，同一天較早的一次擋下可以抵掉後來真正的一次漏擋。

夾具全部自造：暫存家目錄、暫存設定，兩種宿主的紀錄格式照真機實測的列形狀寫。
"""
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import compliance, dream, memspec

class LargeTranscriptRegression(unittest.TestCase):
    def test_replays_turn_after_old_64mb_cutoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.jsonl"
            with path.open("w", encoding="utf-8") as stream:
                skipped = json.dumps({"type": "irrelevant", "padding": "x" * (1024 * 1024)}) + "\n"
                for _ in range(65):
                    stream.write(skipped)
                for row in claude_turn("large", CARD_EPOCH, "問", BANNED):
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            turns = list(compliance._turns(path))
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0][2], BANNED)


CARD_EPOCH = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
BANNED = "這句話不准講"


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def digest(text):
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def write_jsonl(path, rows, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Codex 的列序列化沒有空白（Rust serde_json），行首判讀靠的就是這個形狀。
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                            for row in rows), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def claude_turn(session, at, prompt, answer):
    return [
        {"type": "user", "sessionId": session, "timestamp": iso(at),
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "sessionId": session, "timestamp": iso(at + 1),
         "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}},
    ]


def codex_meta(session, at, thread=None):
    return {"timestamp": iso(at), "type": "session_meta",
            "payload": {"id": thread or session, "session_id": session, "timestamp": iso(at)}}


def codex_turn(at, prompt, answer):
    return [
        {"timestamp": iso(at), "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": prompt}]}},
        {"timestamp": iso(at), "type": "response_item",
         "payload": {"type": "reasoning", "summary": [], "encrypted_content": "x" * 512}},
        {"timestamp": iso(at + 1), "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": answer}]}},
        # 事件流是同一句話的第二份；重放只認 response_item，不能算兩次。
        {"timestamp": iso(at + 1), "type": "event_msg",
         "payload": {"type": "agent_message", "message": answer}},
    ]


def gate_row(session, card, at, message=None, kind=memspec.STOP_GATE_LOG_KIND):
    row = {"timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds"),
           "kind": kind, "decision": card, "rule": "forbidden", "session_id": session}
    if message is not None:
        row["digest"] = digest(message)
    return row


class Fixture(unittest.TestCase):
    """一個暫存家目錄：庫在 `.claude/projects/p/memory`，卡片早就寫好（比所有事件舊）。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-coverage-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        for name, value in (("HOME", str(self.home)), ("USERPROFILE", str(self.home)),
                            (memspec.EPITYPE_CONFIG_ENV, str(config))):
            patched = patch.dict(os.environ, {name: value})
            patched.start()
            self.addCleanup(patched.stop)

    def make_vault(self, vault):
        vault.mkdir(parents=True, exist_ok=True)
        card = vault / "decision-probe.md"
        card.write_text(
            "---\nname: 測試裁定\ndescription: 說明\ndecision_key: probe\nstatus: active\n"
            "current_decision_at: 2026-09-01\ndecided_by: owner-explicit\nowner_quote: 不准講\n"
            f"aliases: [甲名, 乙名]\nforbidden:\n  - {BANNED}\n---\nbody\n",
            encoding="utf-8",
        )
        os.utime(card, (CARD_EPOCH, CARD_EPOCH))
        return vault

    def claude_vault(self):
        return self.make_vault(self.home / ".claude" / "projects" / "p" / "memory")

    def write_gate(self, vault, rows):
        (vault / memspec.GATE_LOG_FILENAME).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def section(self, vault, since, until=None, **extra):
        context = {"compliance_since_stamp": since, **extra}
        if until is not None:
            context["compliance_until_stamp"] = until
        return dream._section_compliance([vault], date(2026, 9, 28), date(2026, 6, 30), {}, context)


class QuietUsageScope(Fixture):
    def test_nightly_counts_do_not_leak_between_same_named_cards(self):
        from epitype import recall_quiet

        first = self.claude_vault()
        second = self.make_vault(self.home / ".claude" / "projects" / "q" / "memory")
        for vault in (first, second):
            (vault / "shared.md").write_text(
                "---\nname: shared\ndescription: example\nmetadata:\n  type: reference\n---\nbody\n",
                encoding="utf-8")
        rows = []
        for _ in range(memspec.RECALL_QUIET_MIN_SEGMENTS):
            rows.extend([
                {"type": "attachment", "timestamp": iso(CARD_EPOCH + 60), "attachment": {
                    "type": "hook_additional_context",
                    "content": [f"vaults: V1={first}\n- example | V1/shared.md"]}},
                {"type": "system", "subtype": "compact_boundary"},
            ])
        write_jsonl(first.parent / "quiet.jsonl", rows)
        result = dream._section_compliance(
            [first, second], date(2026, 9, 28), date(2026, 6, 30), {},
            {"compliance_since_stamp": CARD_EPOCH, "write_health": True})
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["recall_usage"]["complete"])
        self.assertEqual(recall_quiet.quiet_cards(first), frozenset({"shared.md"}))
        self.assertEqual(recall_quiet.quiet_cards(second), frozenset())

    def test_incomplete_usage_preserves_health_without_stalling_other_replay(self):
        from epitype import recall_quiet

        vault = self.claude_vault()
        recall_quiet.update(vault, {}, date(2026, 9, 27))
        previous = recall_quiet.health_path(vault).read_bytes()
        write_jsonl(vault.parent / "unresolved.jsonl", [
            {"type": "attachment", "timestamp": iso(CARD_EPOCH + 60), "attachment": {
                "type": "hook_additional_context", "content": ["- example | V1/unknown.md"]}}])
        result = self.section(vault, CARD_EPOCH, write_health=True)
        self.assertEqual(result["errors"], [])
        self.assertFalse(result["recall_usage"]["complete"])
        self.assertTrue(result["recall_usage"]["diagnostics"])
        self.assertIn("quiet lists unchanged", result["note"])
        self.assertEqual(recall_quiet.health_path(vault).read_bytes(), previous)


class CodexIsReplayed(Fixture):
    """C1：家目錄有 .codex 時，Codex 的 rollout（進行中與封存）一起重放、一起對帳。"""

    def test_codex_rollouts_are_replayed_and_reconciled_with_gate_log(self):
        vault = self.claude_vault()
        since = datetime(2026, 9, 27, 3, 30, tzinfo=timezone.utc).timestamp()
        at = since + 3600
        claude_text = f"Claude 這邊{BANNED}。"
        write_jsonl(self.home / ".claude" / "projects" / "p" / "c1.jsonl",
                    claude_turn("claude-1", at, "問", claude_text))
        live = "codex-live-root"
        live_text = f"Codex 進行中{BANNED}。"
        write_jsonl(self.home / ".codex" / "sessions" / "2026" / "09" / "27"
                    / f"rollout-2026-09-27T05-00-00-{live}.jsonl",
                    [codex_meta(live, at), *codex_turn(at + 10, "問", live_text)])
        # 子代理的 rollout：自己的 thread id，但閘記帳用的是根場次的 session_id。
        sub_text = f"子代理{BANNED}。"
        write_jsonl(self.home / ".codex" / "sessions" / "2026" / "09" / "27"
                    / "rollout-2026-09-27T05-10-00-codex-live-subagent-thread.jsonl",
                    [codex_meta(live, at + 20, thread="codex-live-subagent-thread"),
                     *codex_turn(at + 30, "派工", sub_text)])
        archived = "codex-archived"
        archived_text = f"封存那場{BANNED}。"
        write_jsonl(self.home / ".codex" / "archived_sessions"
                    / f"rollout-2026-09-27T06-00-00-{archived}.jsonl",
                    [codex_meta(archived, at + 40), *codex_turn(at + 50, "問", archived_text)])
        self.write_gate(vault, [gate_row(live, "probe", at + 12, live_text),
                                gate_row(live, "probe", at + 33, sub_text)])

        result = self.section(vault, since)
        counts = result["counts"]
        self.assertEqual(result["errors"], [])
        self.assertEqual(counts["transcripts"], 4)
        self.assertEqual((counts["transcripts_claude"], counts["transcripts_codex"]), (1, 3))
        self.assertEqual(counts["hits"], 4)
        self.assertEqual(counts["blocked"], 2)
        self.assertEqual(sum(item["observed_blocks"] for item in result["rewrite_cost"]), 2)
        self.assertEqual(counts["missed"], 2)
        self.assertEqual(sorted(item["session"] for item in result["examples"]),
                         sorted([archived, "claude-1"]))

    def test_codex_only_home_is_replayed(self):
        # 陌生人只裝 Codex：沒有 ~/.claude，庫在家目錄底下的退路位置。
        vault = self.make_vault(self.home / "epitype-vault")
        since = datetime(2026, 9, 27, 3, 30, tzinfo=timezone.utc).timestamp()
        session = "codex-only"
        write_jsonl(self.home / ".codex" / "sessions" / "2026" / "09" / "27"
                    / f"rollout-2026-09-27T07-00-00-{session}.jsonl",
                    [codex_meta(session, since + 60), *codex_turn(since + 70, "問", f"{BANNED}。")])
        result = self.section(vault, since)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["counts"]["transcripts_codex"], 1)
        self.assertEqual(result["counts"]["missed"], 1)

    def test_claude_only_home_is_unchanged(self):
        vault = self.claude_vault()
        since = datetime(2026, 9, 27, 3, 30, tzinfo=timezone.utc).timestamp()
        write_jsonl(self.home / ".claude" / "projects" / "p" / "c1.jsonl",
                    claude_turn("claude-1", since + 60, "問", f"{BANNED}。"))
        result = self.section(vault, since)
        self.assertEqual(result["errors"], [])
        self.assertEqual((result["counts"]["transcripts_claude"],
                          result["counts"]["transcripts_codex"]), (1, 0))
        self.assertEqual(result["counts"]["missed"], 1)

    def test_codex_turns_use_the_same_turn_text_as_the_gate(self):
        # 閘把一回合的所有助手文字併成一段再算指紋；Codex 的一回合跨好幾列也一樣。
        session = "codex-turn"
        at = datetime(2026, 9, 27, 8, tzinfo=timezone.utc).timestamp()
        rows = [codex_meta(session, at), *codex_turn(at + 1, "問", "第一段。")]
        rows.append({"timestamp": iso(at + 5), "type": "response_item",
                     "payload": {"type": "function_call", "name": "exec_command",
                                 "arguments": json.dumps({"cmd": "ls"})}})
        rows.append({"timestamp": iso(at + 6), "type": "response_item",
                     "payload": {"type": "function_call_output", "output": "ok"}})
        rows.append({"timestamp": iso(at + 7), "type": "response_item",
                     "payload": {"type": "message", "role": "assistant",
                                 "content": [{"type": "output_text", "text": "第二段。"}]}})
        path = write_jsonl(self.root / "rollout-x.jsonl", rows)
        turns = list(compliance._turns(path))
        self.assertEqual(len(turns), 1)
        found_session, stamp, final_text, _texts, calls = turns[0]
        self.assertEqual(found_session, session)
        self.assertEqual(final_text, memspec.join_turn_text(["第一段。", "第二段。"]))
        self.assertEqual(stamp, iso(at + 7))
        self.assertEqual([call[0] for call in calls], ["exec_command"])


def report_with(section13):
    return {"today": "2026-09-28", "sections": [
        {"id": number, "title": str(number), "error": None, "errors": [],
         "counts": {}} if number != 13 else {"id": 13, "title": "13", "error": None, **section13}
        for number in range(1, 16)]}


class CheckpointCatchesUp(Fixture):
    """C2：由舊到新讀；碰到上限就推進到已完整讀完的那一點，下一晚接著追。"""

    T0 = datetime(2026, 9, 27, 3, 30, tzinfo=timezone.utc).timestamp()
    HOUR = 3600

    def backlog(self):
        """五份紀錄：一份跨過窗口起點的長場次（兩次違規），四份窗口內出生的（各一次）。"""
        vault = self.claude_vault()
        projects = self.home / ".claude" / "projects" / "p"
        codex = self.home / ".codex" / "sessions" / "2026" / "09" / "27"
        t0, hour = self.T0, self.HOUR
        # 長場次：窗口開始前出生，窗口內違規兩次（一次早、一次晚）。
        write_jsonl(projects / "long.jsonl",
                    [*claude_turn("long", t0 - hour, "舊的", "窗口外的話。"),
                     *claude_turn("long", t0 + 0.5 * hour, "問", f"早{BANNED}。"),
                     *claude_turn("long", t0 + 3.5 * hour, "問", f"晚{BANNED}。")],
                    mtime=t0 + 3.5 * hour + 10)
        write_jsonl(projects / "a.jsonl", claude_turn("a", t0 + 1 * hour, "問", f"甲{BANNED}。"),
                    mtime=t0 + 1 * hour + 10)
        write_jsonl(codex / "rollout-2026-09-27T05-30-00-01a0b.jsonl",
                    [codex_meta("01a0b", t0 + 2 * hour),
                     *codex_turn(t0 + 2 * hour, "問", f"乙{BANNED}。")],
                    mtime=t0 + 2 * hour + 10)
        write_jsonl(projects / "c.jsonl", claude_turn("c", t0 + 3 * hour, "問", f"丙{BANNED}。"),
                    mtime=t0 + 3 * hour + 10)
        write_jsonl(codex / "rollout-2026-09-27T07-45-00-01a0d.jsonl",
                    [codex_meta("01a0d", t0 + 3.75 * hour),
                     *codex_turn(t0 + 3.75 * hour, "問", f"丁{BANNED}。")],
                    mtime=t0 + 3.75 * hour + 10)
        return vault

    def test_capped_scan_catches_up_over_two_nights_without_loss_or_double_count(self):
        vault = self.backlog()
        state = vault / ".epitype" / memspec.DREAM_STATE_FILENAME
        pack = vault / ".epitype" / "pack.md"
        night1 = self.T0 + 4 * self.HOUR
        with patch.object(compliance, "MAX_TRANSCRIPTS", 3):
            first = self.section(vault, self.T0, until=night1)
        counts = first["counts"]
        self.assertEqual(first["errors"], [])
        # 最舊的三份先讀：長場次、甲、乙；丙與丁留到下一晚。
        self.assertEqual((counts["transcripts"], counts["transcripts_skipped"]), (3, 2))
        self.assertEqual((counts["transcripts_claude"], counts["available_claude"]), (2, 3))
        self.assertEqual((counts["transcripts_codex"], counts["available_codex"]), (1, 2))
        self.assertEqual(counts["coverage_pct"], 60)
        self.assertEqual(counts["coverage_status"], "不完整")
        self.assertIn("不完整", first["note"])
        # 只算到接續點（丙出生那一刻）為止：長場次晚的那一次留給下一晚，不重複算。
        self.assertEqual(counts["compliance_frontier"], self.T0 + 3 * self.HOUR)
        self.assertEqual(sorted(item["session"] for item in first["examples"]), ["01a0b", "a", "long"])
        report = report_with(first)
        self.assertIn("13", dream._report_errors(report))
        self.assertTrue(any("不完整" in step for step in dream._next_steps(report["sections"])))
        dream._write_run_state(state, report, pack, 1.0, night1)
        saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(saved[dream.COMPLIANCE_CHECKPOINT_FIELD], self.T0 + 3 * self.HOUR)

        night2 = self.T0 + 28 * self.HOUR
        since2 = dream._previous_compliance_start(state, night2)
        self.assertEqual(since2, self.T0 + 3 * self.HOUR)
        with patch.object(compliance, "MAX_TRANSCRIPTS", 3):
            second = self.section(vault, since2, until=night2)
        counts = second["counts"]
        self.assertEqual((counts["transcripts"], counts["transcripts_skipped"]), (3, 0))
        self.assertEqual(counts["coverage_status"], "完整")
        self.assertEqual(sorted(item["session"] for item in second["examples"]), ["01a0d", "c", "long"])
        self.assertEqual(first["counts"]["missed"] + counts["missed"], 6)
        report = report_with(second)
        self.assertNotIn("13", dream._report_errors(report))
        dream._write_run_state(state, report, pack, 1.0, night2)
        saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(saved[dream.COMPLIANCE_CHECKPOINT_FIELD], night2)

    def test_eighty_percent_is_partial_not_incomplete(self):
        vault = self.backlog()
        with patch.object(compliance, "MAX_TRANSCRIPTS", 4):
            result = self.section(vault, self.T0, until=self.T0 + 4 * self.HOUR)
        self.assertEqual(result["counts"]["coverage_pct"], 80)
        self.assertEqual(result["counts"]["coverage_status"], "部分")
        self.assertNotIn("不完整", result["note"])

    def test_scan_order_is_oldest_birth_first(self):
        self.backlog()
        roots = dream._transcript_roots([self.home / ".claude" / "projects" / "p" / "memory"])
        scan = compliance.transcripts_for(roots, since_stamp=self.T0, limit=5)
        paths, available = scan
        self.assertEqual(available, 5)
        self.assertEqual([path.name for path in paths],
                         ["long.jsonl", "a.jsonl", "rollout-2026-09-27T05-30-00-01a0b.jsonl",
                          "c.jsonl", "rollout-2026-09-27T07-45-00-01a0d.jsonl"])
        self.assertIsNone(scan.frontier)

    def test_frontier_that_cannot_move_is_an_error_and_keeps_the_checkpoint(self):
        # 窗口起點之前出生、至今還在寫的紀錄比單晚上限還多：接續點推不動，要講出來。
        vault = self.claude_vault()
        projects = self.home / ".claude" / "projects" / "p"
        for name in ("x", "y"):
            write_jsonl(projects / f"{name}.jsonl",
                        [*claude_turn(name, self.T0 - self.HOUR, "舊", "舊話。"),
                         *claude_turn(name, self.T0 + self.HOUR, "問", f"{BANNED}。")],
                        mtime=self.T0 + self.HOUR + 10)
        with patch.object(compliance, "MAX_TRANSCRIPTS", 1):
            result = self.section(vault, self.T0, until=self.T0 + 4 * self.HOUR)
        self.assertTrue(any("接續點無法前進" in error for error in result["errors"]))
        state = vault / ".epitype" / memspec.DREAM_STATE_FILENAME
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({dream.COMPLIANCE_CHECKPOINT_FIELD: self.T0}), encoding="utf-8")
        dream._write_run_state(state, report_with(result), vault / "pack.md", 1.0,
                               self.T0 + 4 * self.HOUR)
        saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(saved[dream.COMPLIANCE_CHECKPOINT_FIELD], self.T0)

    def test_vault_error_does_not_freeze_the_checkpoint(self):
        # 一個壞掉的庫每晚都會出錯；錯誤照報，但接續點不能因此永遠停在原地。
        state = self.root / memspec.DREAM_STATE_FILENAME
        state.write_text(json.dumps({dream.COMPLIANCE_CHECKPOINT_FIELD: 1000.0}), encoding="utf-8")
        section = {"counts": {"transcripts": 1, "transcripts_skipped": 0}, "errors": ["v: boom"]}
        dream._write_run_state(state, report_with(section), self.root / "pack.md", 1.0, 2000.0)
        saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(saved[dream.COMPLIANCE_CHECKPOINT_FIELD], 2000.0)
        self.assertIn("13", saved[memspec.DREAM_STATE_ERRORS_FIELD])


class WindowResetIsVisible(Fixture):
    """C2：狀態檔遺失或壞掉時退回只看 24 小時——報告要寫明「窗口重設」，不能像完整。"""

    def run_dream(self, vault, state):
        import io

        pack = self.root / "pack.json"
        code = dream.main(["--json", "--out", str(pack), "--state", str(state), str(vault)],
                          output=io.StringIO())
        self.assertEqual(code, 0)
        report = json.loads(pack.read_text(encoding="utf-8"))
        return next(section for section in report["sections"] if section["id"] == 13), report

    def test_corrupt_state_is_reported_as_window_reset(self):
        vault = self.claude_vault()
        state = self.root / "state" / memspec.DREAM_STATE_FILENAME
        state.parent.mkdir()
        state.write_text("{not json", encoding="utf-8")
        reasons = []
        dream._previous_compliance_start(state, 5000.0, reasons)
        self.assertEqual(reasons, ["狀態檔損壞"])
        section, report = self.run_dream(vault, state)
        self.assertIn("損壞", section["counts"]["window_reset"])
        self.assertIn("窗口重設", section["note"])
        self.assertTrue(any("窗口重設" in step for step in report["next_steps"]))

    def test_missing_state_is_reported_as_window_reset(self):
        vault = self.claude_vault()
        section, _report = self.run_dream(vault, self.root / "absent" / memspec.DREAM_STATE_FILENAME)
        self.assertIn("不存在", section["counts"]["window_reset"])

    def test_valid_checkpoint_is_not_a_reset(self):
        vault = self.claude_vault()
        state = self.root / "state" / memspec.DREAM_STATE_FILENAME
        state.parent.mkdir()
        import time as clock

        state.write_text(json.dumps({dream.COMPLIANCE_CHECKPOINT_FIELD: clock.time() - 600}),
                         encoding="utf-8")
        reasons = []
        dream._previous_compliance_start(state, clock.time(), reasons)
        self.assertEqual(reasons, [])
        section, _report = self.run_dream(vault, state)
        self.assertEqual(section["counts"]["window_reset"], "")
        self.assertNotIn("窗口重設", section.get("note") or "")


class ExactWindowsAndMatching(Fixture):
    """C4：稽核帳、引用豁免、喚回用量都照精確時間切；同場較早的擋下不能抵掉後來的漏擋。"""

    DAY = datetime(2026, 9, 27, tzinfo=timezone.utc).timestamp()

    def test_block_before_the_window_on_the_same_day_does_not_absorb_a_real_miss(self):
        vault = self.claude_vault()
        early, late = f"早上{BANNED}。", f"下午{BANNED}。"
        write_jsonl(self.home / ".claude" / "projects" / "p" / "s.jsonl",
                    [*claude_turn("s", self.DAY + 9 * 3600, "問", early),
                     *claude_turn("s", self.DAY + 15 * 3600, "問", late)])
        # 早上那次當場擋下（已在上一晚算過）；下午那次閘沒擋到。
        self.write_gate(vault, [gate_row("s", "probe", self.DAY + 9 * 3600 + 5, early)])
        result = self.section(vault, self.DAY + 12 * 3600, until=self.DAY + 20 * 3600)
        self.assertEqual((result["counts"]["blocked"], result["counts"]["missed"]), (0, 1))

    def test_later_block_does_not_absorb_an_earlier_miss_in_the_same_session(self):
        vault = self.claude_vault()
        first, second = f"一點{BANNED}。", f"三點{BANNED}。"
        write_jsonl(self.home / ".claude" / "projects" / "p" / "s.jsonl",
                    [*claude_turn("s", self.DAY + 13 * 3600, "問", first),
                     *claude_turn("s", self.DAY + 15 * 3600, "問", second)])
        self.write_gate(vault, [gate_row("s", "probe", self.DAY + 15 * 3600 + 3, second)])
        result = self.section(vault, self.DAY + 12 * 3600, until=self.DAY + 20 * 3600)
        self.assertEqual((result["counts"]["blocked"], result["counts"]["missed"]), (1, 1))
        self.assertEqual(result["examples"][0]["at"], iso(self.DAY + 13 * 3600 + 1))

    def test_block_without_digest_matches_only_a_nearby_hit(self):
        # Codex 的閘列算指紋的文字跟 rollout 重組出來的不一定一樣：沒有指紋可比時，
        # 只認時間相近的那一次，不讓它去抵同場幾小時前的漏擋。
        vault = self.claude_vault()
        session = "codex-nodigest"
        path = self.home / ".codex" / "sessions" / "2026" / "09" / "27" / f"rollout-x-{session}.jsonl"
        write_jsonl(path, [codex_meta(session, self.DAY + 12.5 * 3600),
                           *codex_turn(self.DAY + 13 * 3600, "問", f"先{BANNED}。"),
                           *codex_turn(self.DAY + 16 * 3600, "問", f"後{BANNED}。")])
        self.write_gate(vault, [gate_row(session, "probe", self.DAY + 16 * 3600 + 4)])
        result = self.section(vault, self.DAY + 12 * 3600, until=self.DAY + 20 * 3600)
        self.assertEqual((result["counts"]["blocked"], result["counts"]["missed"]), (1, 1))
        self.assertEqual(result["examples"][0]["at"], iso(self.DAY + 13 * 3600 + 1))

    def test_quoted_exemptions_use_exact_time(self):
        vault = self.claude_vault()
        masked = {"kind": memspec.STOP_GATE_MASKED_LOG_KIND, "decision": "probe",
                  "rule": "quoted", "session_id": "s"}
        self.write_gate(vault, [
            {**masked, "timestamp": datetime.fromtimestamp(self.DAY + 9 * 3600, timezone.utc)
             .isoformat(timespec="seconds")},
            {**masked, "timestamp": datetime.fromtimestamp(self.DAY + 14 * 3600, timezone.utc)
             .isoformat(timespec="seconds")},
        ])
        write_jsonl(self.home / ".claude" / "projects" / "p" / "s.jsonl",
                    claude_turn("s", self.DAY + 14 * 3600, "問", "沒事。"))
        result = self.section(vault, self.DAY + 12 * 3600, until=self.DAY + 20 * 3600)
        self.assertEqual(result["counts"]["quoted_exemptions"], 1)

    def test_recall_usage_uses_exact_time(self):
        from epitype import recall_quiet

        rows = []
        for hour in (9, 14):
            rows.append({"type": "attachment", "timestamp": iso(self.DAY + hour * 3600),
                         "attachment": {"type": "hook_additional_context",
                                        "content": [f"- 說明 | V1/card-{hour}.md"]}})
        path = write_jsonl(self.root / "recall.jsonl", rows)
        shown = recall_quiet.measure([path], since=self.DAY + 12 * 3600, until=self.DAY + 20 * 3600)
        self.assertEqual(shown, {"card-14.md": [1, 0]})
        # 舊的日期寫法照舊可用。
        self.assertEqual(set(recall_quiet.measure([path], since="2026-09-27")),
                         {"card-9.md", "card-14.md"})

    def test_guard_replay_reads_what_the_gate_reads(self):
        # 閘不看 description（那是寫給 owner 看的說明，拿它湊字面會假命中）；重放也不能看，
        # 否則指紋對不上閘的那一列，同一次擋下會被算成漏擋。
        vault = self.claude_vault()
        (vault / "scar-y.md").write_text(
            '---\nname: 測試守衛\ndescription: 說明\nguard_tool: Bash\n'
            'guard_all_of:\n  - "rm -rf"\n---\nbody\n', encoding="utf-8")
        os.utime(vault / "scar-y.md", (CARD_EPOCH, CARD_EPOCH))
        at = self.DAY + 14 * 3600
        blocked_input = {"command": "rm -rf build", "description": "清掉"}
        described_only = {"command": "ls", "description": "不會 rm -rf 任何東西"}
        rows = [{"type": "user", "sessionId": "g", "timestamp": iso(at),
                 "message": {"role": "user", "content": "清一下"}}]
        for offset, payload in ((10, blocked_input), (20, described_only)):
            rows.append({"type": "assistant", "sessionId": "g", "timestamp": iso(at + offset),
                         "message": {"role": "assistant", "content": [
                             {"type": "tool_use", "id": f"t{offset}", "name": "Bash", "input": payload}]}})
            rows.append({"type": "user", "sessionId": "g", "timestamp": iso(at + offset + 1),
                         "message": {"role": "user", "content": [
                             {"type": "tool_result", "tool_use_id": f"t{offset}", "content": "ok"}]}})
        # 回合最後一段話在半小時後：擋下的時間要對的是那一次呼叫，不是回合結尾。
        rows.append({"type": "assistant", "sessionId": "g", "timestamp": iso(at + 1800),
                     "message": {"role": "assistant", "content": [{"type": "text", "text": "好了。"}]}})
        write_jsonl(self.home / ".claude" / "projects" / "p" / "g.jsonl", rows)
        self.write_gate(vault, [{
            "timestamp": datetime.fromtimestamp(at + 11, timezone.utc).isoformat(timespec="seconds"),
            "kind": memspec.ACTION_GUARD_LOG_KIND, "rule": "action_guard", "card": "測試守衛",
            "tool": "Bash", "digest": digest("rm -rf build"), "session_id": "g"}])
        result = self.section(vault, self.DAY + 12 * 3600, until=self.DAY + 20 * 3600)
        self.assertEqual((result["counts"]["blocked"], result["counts"]["missed"]), (1, 0))


DECISION_CARD = (
    "---\nname: 飛輪袖套裁定\ndescription: 飛輪袖套的裁定：不准講那句\ndecision_key: sleeve\n"
    "status: active\ncurrent_decision_at: 2026-09-01\ndecided_by: owner-explicit\n"
    "owner_quote: 飛輪袖套不准講那句\naliases: [飛輪袖套, 袖套裁定]\n"
    f"forbidden:\n  - {BANNED}\n---\n飛輪袖套 body\n"
)
PLAIN_CARD = (
    "---\nname: 飛輪袖套筆記\ndescription: 飛輪袖套的使用筆記\nmetadata:\n  type: feedback\n"
    "aliases:\n  - 飛輪袖套\n---\n飛輪袖套 筆記內容。\n"
)


class DeliveryLedger(Fixture):
    """C3：喚回每送出一張卡就記一筆（時間、場次、卡、送達方式）；寫不進去也照樣送。"""

    def setUp(self):
        super().setUp()
        import _hook_common as common

        self.common = common
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / "decision-sleeve.md").write_text(DECISION_CARD, encoding="utf-8", newline="\n")
        (self.vault / "feedback-sleeve.md").write_text(PLAIN_CARD, encoding="utf-8", newline="\n")
        self.config = self.root / "hook-config.json"
        common.write_config(self.config, [self.vault])
        from epitype import memsearch

        memsearch.build_index(self.vault)
        self.ledger = self.vault / memspec.FTS_INDEX_DIRECTORY / memspec.RECALL_DELIVERY_LOG_FILENAME

    def prompt(self, session, prompt="飛輪袖套現在怎麼樣"):
        script = ROOT / "adapters" / "claude" / "recall_hook.py"
        event = {"session_id": session, "cwd": str(self.root), "prompt": prompt,
                 "transcript_path": str(self.home / ".claude" / "projects" / "p" / f"{session}.jsonl")}
        result = self.common.run_synthetic(script, event, self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def rows(self):
        if not self.ledger.is_file():
            return []
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]

    def test_each_delivered_card_is_recorded_once(self):
        output = self.prompt("ledger-1")
        self.assertIn("decision-sleeve.md", output)
        self.assertIn("feedback-sleeve.md", output)
        rows = self.rows()
        self.assertEqual(sorted((row["card"], row["mode"]) for row in rows),
                         [("decision-sleeve.md", memspec.RECALL_DELIVERY_MODE_DECISION),
                          ("feedback-sleeve.md", memspec.RECALL_DELIVERY_MODE_CARD)])
        self.assertTrue(all(row["session_id"] == "ledger-1" and row["host"] == "claude"
                            and Path(row["vault"]) == self.vault and row["timestamp"]
                            for row in rows))
        # 同一場再問一次：卡已經在對話裡，沒有再送，也就沒有再記。
        self.prompt("ledger-1")
        self.assertEqual(len(self.rows()), 2)
        self.prompt("ledger-2")
        self.assertEqual(len(self.rows()), 4)

    def test_ledger_failure_does_not_block_delivery(self):
        self.ledger.mkdir(parents=True)  # 同名目錄：開檔一定失敗
        output = self.prompt("ledger-broken")
        self.assertIn("decision-sleeve.md", output)

    def test_ledger_respects_the_hook_deadline(self):
        import time as clock

        with self.assertRaises(TimeoutError):
            self.common.append_recall_deliveries(
                self.vault, [{"session_id": "s", "card": "x.md"}],
                clock.monotonic() - memspec.HOOK_TIMEOUT_SECONDS - 1)
        self.assertFalse(self.ledger.exists())

    def test_ledger_rotates_at_its_cap(self):
        import time as clock

        with patch.object(memspec, "RECALL_DELIVERY_LOG_MAX_BYTES", 200):
            for index in range(6):
                self.common.append_recall_deliveries(
                    self.vault, [{"session_id": f"s{index}", "card": "x.md", "mode": "card"}],
                    clock.monotonic())
        rotated = self.ledger.with_name(self.ledger.name + ".1")
        self.assertTrue(rotated.is_file())
        self.assertLessEqual(self.ledger.stat().st_size, 400)

    def test_concurrent_writers_never_interleave(self):
        import subprocess

        script = (
            "import sys, time; from pathlib import Path\n"
            f"sys.path[:0] = [{str(ROOT)!r}, {str(ROOT / 'adapters' / 'claude')!r}]\n"
            "import _hook_common as c\n"
            "written = 0\n"
            "for i in range(40):\n"
            "    try:\n"
            "        c.append_recall_deliveries(Path(sys.argv[1]), "
            "[{'session_id': sys.argv[2] + str(i), 'card': 'x.md', 'mode': 'card'}] * 3, "
            "time.monotonic())\n"
            "        written += 1\n"
            # 鎖搶不到會丟 OSError（掛鉤那一側吞掉、照樣送達）；這裡只數成功寫進去的。
            "    except OSError as exc:\n"
            "        assert 'lock unavailable' in str(exc), exc\n"
            "print(written)\n"
        )
        workers = [subprocess.Popen([sys.executable, "-c", script, str(self.vault), f"w{n}-"],
                                    stdout=subprocess.PIPE, text=True) for n in range(4)]
        outputs = [worker.communicate(timeout=120)[0] for worker in workers]
        self.assertEqual([worker.returncode for worker in workers], [0, 0, 0, 0])
        written = [int(output.strip()) for output in outputs]
        self.assertTrue(all(count > 0 for count in written), written)
        rows = self.rows()  # 每一列都讀得回來：沒有兩個寫入者交錯成半列
        self.assertEqual(len(rows), sum(written) * 3)
        # 同一次寫入的三列必定相鄰：一次提問的送達不會被別的提問切開。
        sessions = [row["session_id"] for row in rows]
        for start in range(0, len(sessions), 3):
            self.assertEqual(len(set(sessions[start:start + 3])), 1)


class DeliveredThenViolated(Fixture):
    """C3：夜報對每條武裝規則列出「送達後同一場仍被擋或漏擋」的場次數；純文字規則只列送達。"""

    DAY = datetime(2026, 9, 27, tzinfo=timezone.utc).timestamp()

    def write_ledger(self, vault, rows):
        path = vault / memspec.FTS_INDEX_DIRECTORY / memspec.RECALL_DELIVERY_LOG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps({
            "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds"),
            "session_id": session, "vault": str(vault), "card": card, "mode": mode, "host": "claude",
        }, ensure_ascii=False) + "\n" for at, session, card, mode in rows), encoding="utf-8")

    def test_sessions_violated_after_delivery_are_counted_per_rule(self):
        vault = self.claude_vault()
        (vault / "note.md").write_text("---\nname: 筆記\ndescription: 純文字\n---\nbody\n",
                                       encoding="utf-8")
        hour = 3600
        base = self.DAY + 10 * hour
        projects = self.home / ".claude" / "projects" / "p"
        # s2：違規在送達之前（不算）；s3：送達後漏擋（算）。
        write_jsonl(projects / "s2.jsonl", claude_turn("s2", base - hour, "問", f"{BANNED}。"))
        write_jsonl(projects / "s3.jsonl", claude_turn("s3", base + 4 * hour, "問", f"{BANNED}。"))
        write_jsonl(projects / "s4.jsonl", claude_turn("s4", base + 4 * hour, "問", "沒事。"))
        # s1：送達兩小時後照樣被閘擋下（算）。
        self.write_gate(vault, [gate_row("s1", "probe", base + 2 * hour)])
        self.write_ledger(vault, [
            (base, "s1", "decision-probe.md", "decision"),
            (base + 60, "s1", "decision-probe.md", "decision"),
            (base, "s2", "decision-probe.md", "decision"),
            (base + 3 * hour, "s3", "decision-probe.md", "decision"),
            (base, "s4", "decision-probe.md", "decision"),
            (base, "s1", "note.md", "card"),
            (base, "s4", "note.md", "card"),
            # 窗口之前的送達：不算進今晚的送達次數。
            (self.DAY - 10 * hour, "s9", "note.md", "card"),
        ])
        result = self.section(vault, self.DAY, until=self.DAY + 20 * hour)
        self.assertEqual(result["errors"], [])
        delivery = result["delivery"]
        self.assertEqual(delivery["armed"], [{
            "card": "decision-probe.md", "rule": "probe", "deliveries": 5, "sessions": 4,
            "violated_sessions": 2}])
        self.assertEqual(delivery["plain"], [{
            "card": "note.md", "deliveries": 2, "sessions": 2,
            "verdict": compliance.DELIVERY_UNJUDGEABLE}])
        self.assertEqual(result["counts"]["deliveries"], 7)
        self.assertEqual(result["counts"]["delivered_violated_sessions"], 2)
        rendered = dream._render_markdown(report_with(result) | {
            "vaults": [str(vault)], "since": "2026-06-30", "index_shaping": [], "next_steps": []})
        self.assertIn(compliance.DELIVERY_UNJUDGEABLE, rendered)
        self.assertIn("decision-probe.md", rendered)

    def test_report_lists_only_top_n(self):
        vault = self.claude_vault()
        for index in range(3):
            (vault / f"note-{index}.md").write_text("---\nname: n\ndescription: d\n---\nb\n",
                                                    encoding="utf-8")
        self.write_ledger(vault, [(self.DAY + 3600 + index, f"s{index}", f"note-{index % 3}.md", "card")
                                  for index in range(6)])
        with patch.object(compliance, "DELIVERY_REPORT_LIMIT", 2):
            result = self.section(vault, self.DAY, until=self.DAY + 20 * 3600)
        self.assertEqual(len(result["delivery"]["plain"]), 2)
        self.assertEqual(result["counts"]["delivered_cards"], 3)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
