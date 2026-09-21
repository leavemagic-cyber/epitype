# -*- coding: utf-8 -*-
"""封套寫檔、已讀證據強弱、Codex 呼叫解析——這一批補上的三個缺口各自的兩向實測。

為什麼這三件事要放在一起測：它們是同一個缺口的三個面。這台機器上的 Codex 沒有
`apply_patch` 工具，寫檔是把封套夾在 `exec` 的文字裡送出去，而呼叫進到動手閘時工具名
已經是 `Bash`——於是內容規則對 Codex 從未生效（真庫 40 列 `write_block` 全是 Claude）、
呼叫內容在紀錄那一側也是空的。

誤擋是這一類檢查最貴的失敗，所以每一條都測兩向：該擋的擋、不該擋的不擋、判不準的標
未知而不是亂判。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters" / "claude"))

import json
import tempfile
import time
import unittest
from unittest.mock import patch

from epitype import handoff, memspec, opened, patch_envelope, transcript
from adapters.codex import hook_trust
from install import graft

import pretooluse_gate as pre
import stop_gate


FORBIDDEN_PHRASE = "先改完再說"


def _decision(path):
    fields = {
        "key": "測試裁定",
        "decided_at": "2026-09-22",
        "quote": "不要先改完再說",
        "forbidden": (FORBIDDEN_PHRASE,),
        "aliases": (),
        "path": path,
        "decided_by": "owner-explicit",
        "require_when": "",
        "require_text": "",
        "advice": "",
        "applies_to": "",
        "turn_check": "",
        "turn_check_limit": "",
    }
    return stop_gate._Decision(**fields)


def _envelope(*body):
    return "\n".join((patch_envelope.BEGIN_MARKER,) + body + (patch_envelope.END_MARKER,))


class ThePatchEnvelopeParser(unittest.TestCase):
    def test_only_added_lines_are_handed_on(self):
        # §11 反對整份 diff 的理由就是這個：上下文行與刪除行不是這次寫入新增的東西。
        entry = patch_envelope.parse(_envelope(
            "*** Update File: a.md",
            "@@ hunk",
            " 上下文 %s" % FORBIDDEN_PHRASE,
            "-刪掉的 %s" % FORBIDDEN_PHRASE,
            "+留下來的",
        ))[0]
        self.assertEqual(entry.additions, ("留下來的",))

    def test_a_broken_envelope_yields_nothing(self):
        self.assertEqual(patch_envelope.parse("*** Begin Patch\n*** Add File: a.md\n+x"), ())

    def test_an_add_file_carries_its_whole_content(self):
        entry = patch_envelope.parse(_envelope("*** Add File: a.md", "+一", "+二"))[0]
        self.assertEqual(patch_envelope.full_content(entry), "一\n二")

    def test_an_update_file_is_never_reconstructed(self):
        entry = patch_envelope.parse(_envelope("*** Update File: a.md", "@@", "+一"))[0]
        self.assertFalse(entry.complete)
        self.assertIsNone(patch_envelope.full_content(entry))


class TheWriteGateOnEnvelopes(unittest.TestCase):
    """動手閘那一側：封套的內容真的被判了，而且只判新增行。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.card = self.root / "測試裁定.md"
        self.event = {"cwd": str(self.root), "session_id": "sess-envelope"}
        patches = [
            patch.object(stop_gate, "_vaults", return_value=[self.root]),
            patch.object(stop_gate, "_decisions", return_value=[_decision(self.card)]),
            patch.object(pre, "_write_marker", return_value=True),
            patch.object(pre, "_best_effort_audit"),
            patch.object(pre, "resolve_vaults", return_value=[]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def _review(self, text, tool="Bash"):
        return pre._write_review(self.event, tool, {"command": text}, {}, time.monotonic())

    def test_a_removed_line_carrying_the_phrase_is_not_a_restatement(self):
        # 誤擋比漏擋貴：刪掉那句話的那次寫入，不是又說了一次那句話。
        deny, _notices = self._review(_envelope(
            "*** Update File: note.md",
            "@@ hunk",
            " 上下文一行",
            "-%s" % FORBIDDEN_PHRASE,
            "+改成先想清楚",
        ))
        self.assertIsNone(deny)

    def test_a_context_line_carrying_the_phrase_is_not_a_restatement(self):
        deny, _notices = self._review(_envelope(
            "*** Update File: note.md",
            "@@ hunk",
            " %s" % FORBIDDEN_PHRASE,
            "+無關的一行",
        ))
        self.assertIsNone(deny)

    def test_an_added_line_carrying_the_phrase_is_blocked(self):
        deny, _notices = self._review(_envelope(
            "*** Update File: note.md",
            "@@ hunk",
            " 上下文一行",
            "+%s" % FORBIDDEN_PHRASE,
        ))
        self.assertIsNotNone(deny)
        self.assertIn(FORBIDDEN_PHRASE, json.dumps(deny, ensure_ascii=False))

    def test_the_tool_name_list_is_not_what_opened_this_path(self):
        # §11 把 diff 形狀的工具排除在 WRITE_GATE_TOOL_NAMES 之外的理由仍然成立，
        # 所以這條路徑不是靠加工具名走通的——`Bash` 本來就不在名單裡。
        self.assertNotIn("bash", memspec.WRITE_GATE_TOOL_NAMES)
        self.assertNotIn("apply_patch", memspec.WRITE_GATE_TOOL_NAMES)

    def test_a_clean_file_in_the_same_envelope_is_untouched(self):
        with patch.object(pre, "_review_one_target", wraps=pre._review_one_target) as reviewed:
            deny, _notices = self._review(_envelope(
                "*** Add File: clean.md",
                "+完全沒問題的一行",
                "*** Add File: dirty.md",
                "+%s" % FORBIDDEN_PHRASE,
            ))
        self.assertIsNotNone(deny)
        judged = [call.args[2].name for call in reviewed.call_args_list]
        self.assertEqual(judged, ["clean.md", "dirty.md"])
        self.assertIn("dirty.md", json.dumps(deny, ensure_ascii=False)
                      + str(reviewed.call_args_list[-1]))

    def test_one_undecidable_file_does_not_excuse_the_rest(self):
        # 路徑解不出來的那個檔標未知，後面那個違規的照樣要擋。
        deny, _notices = self._review(_envelope(
            "*** Add File: ",
            "+無處可放",
            "*** Add File: dirty.md",
            "+%s" % FORBIDDEN_PHRASE,
        ))
        self.assertIsNotNone(deny)

    def test_a_broken_envelope_is_neither_judged_nor_an_error(self):
        deny, notices = self._review("*** Begin Patch\n*** Add File: x.md\n+%s" % FORBIDDEN_PHRASE)
        self.assertIsNone(deny)
        self.assertEqual(notices, [])

    def test_a_text_without_an_envelope_costs_nothing(self):
        with patch.object(pre, "_forbidden_write") as forbidden:
            deny, _notices = self._review("git status --short")
        self.assertIsNone(deny)
        self.assertFalse(forbidden.called)

    def test_the_same_block_fires_once_per_session(self):
        with patch.object(pre, "_write_marker", return_value=False):
            deny, _notices = self._review(_envelope(
                "*** Add File: dirty.md", "+%s" % FORBIDDEN_PHRASE))
        self.assertIsNone(deny)

    def test_a_block_is_audited_as_a_write_block(self):
        with patch.object(pre, "_best_effort_audit") as audited:
            self._review(_envelope("*** Add File: dirty.md", "+%s" % FORBIDDEN_PHRASE))
        self.assertTrue(audited.called)
        self.assertEqual(audited.call_args.args[0], pre._append_write_block)
        self.assertEqual(audited.call_args.args[2], memspec.WRITE_GATE_FORBIDDEN_RULE)
        self.assertEqual(audited.call_args.args[3], {"decision": "測試裁定"})

    def test_oversized_additions_are_unknown_not_blocked(self):
        giant = "x" * (memspec.WRITE_GATE_MAX_CONTENT_BYTES + 1)
        deny, _notices = self._review(_envelope(
            "*** Add File: huge.md", "+%s%s" % (FORBIDDEN_PHRASE, giant)))
        self.assertIsNone(deny)


class TheCardContractOnEnvelopes(unittest.TestCase):
    """規則 B 只判 Add File：Update File 的寫入後內容，補丁裡根本看不到。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.event = {"cwd": str(self.root), "session_id": "sess-card"}
        patches = [
            patch.object(pre, "_forbidden_write", return_value=None),
            patch.object(pre, "resolve_vaults", return_value=[self.root]),
            patch.object(pre, "_write_marker", return_value=True),
            patch.object(pre, "_best_effort_audit"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def _review(self, text):
        return pre._write_review(self.event, "Bash", {"command": text}, {}, time.monotonic())

    def test_an_add_file_card_without_frontmatter_is_blocked(self):
        deny, _notices = self._review(_envelope("*** Add File: 新卡.md", "+只有一句話，沒有前置欄位"))
        self.assertIsNotNone(deny)

    def test_an_update_file_card_is_left_unknown(self):
        with patch.object(pre, "_card_review") as reviewed:
            deny, _notices = self._review(_envelope(
                "*** Update File: 舊卡.md", "@@", "+只有一句話，沒有前置欄位"))
        self.assertIsNone(deny)
        self.assertFalse(reviewed.called)

    def test_a_delete_file_judges_no_content(self):
        with patch.object(pre, "_card_review") as reviewed:
            deny, _notices = self._review(_envelope("*** Delete File: 舊卡.md"))
        self.assertIsNone(deny)
        self.assertFalse(reviewed.called)


class TheStrengthOfReadEvidence(unittest.TestCase):
    """自由文字裡提到一個檔名，不是打開過它。"""

    def test_a_structured_path_field_is_strong(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            opened.record(vault, "s", "", r"C:\Epitype\repo\epitype\dream.py")
            self.assertIn("dream.py", opened.strong_names(vault, "s"))
            self.assertIn("dream.py", opened.names(vault, "s"))

    def test_a_filename_mentioned_in_free_text_is_weak(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            opened.record(vault, "s", "Write-Output 'ghost.py'")
            self.assertEqual(opened.strong_names(vault, "s"), frozenset())
            self.assertIn("ghost.py", opened.names(vault, "s"))

    def test_weak_evidence_cannot_satisfy_a_cited_read(self):
        card = _decision(Path("測試裁定.md"))._replace(
            turn_check=memspec.TURN_CHECK_CITED_UNREAD)
        turn = stop_gate._turn([], "檢查檔案")
        claim = "我讀過 ghost.py。"
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            # 真的讀了 other.py（結構化欄位），只是在命令列的文字裡提到 ghost.py。
            opened.record(vault, "s", "Write-Output 'ghost.py'", r"C:\repo\other.py")
            weak = opened.names(vault, "s")
            strong = opened.strong_names(vault, "s")
        self.assertIn("ghost.py", weak)
        # 舊行為：全文掃出來的提及讓這句宣稱過關。新行為：強證據裡沒有它，照樣擋。
        self.assertIsNone(stop_gate._cited_unread_gap(card, claim, turn, weak))
        self.assertIsNotNone(stop_gate._cited_unread_gap(card, claim, turn, strong))
        self.assertIsNone(
            stop_gate._cited_unread_gap(card, claim, turn, strong | {"ghost.py"}))

    def test_the_turn_gate_is_wired_to_the_strong_half(self):
        source = (Path(__file__).resolve().parents[1]
                  / "adapters" / "claude" / "stop_gate.py").read_text(encoding="utf-8")
        self.assertIn("opened_module.strong_names(", source)
        self.assertNotIn("opened_module.names(", source)

    def test_the_gate_only_reads_the_strong_half(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            opened.record(vault, "s", "grep -n x ghost.py", r"C:\repo\dream.py")
            self.assertEqual(opened.strong_names(vault, "s"), frozenset({"dream.py"}))
            self.assertEqual(opened.names(vault, "s"), frozenset({"dream.py", "ghost.py"}))

    def test_the_old_format_reads_back_as_weak(self):
        # 2026-09-22 之前寫下的 147 個附記檔每行只有檔名。分不出強弱的，不准算成強。
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            legacy = opened.path_for(vault, "s")
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text("dream.py\nghost.py\n", encoding="utf-8")
            self.assertEqual(opened.names(vault, "s"), frozenset({"dream.py", "ghost.py"}))
            self.assertEqual(opened.strong_names(vault, "s"), frozenset())

    def test_strong_fields_are_target_fields_only(self):
        for field in memspec.OPENED_STRONG_FIELDS:
            if field != "filePath":     # Claude 以外的宿主拼法，不在 target 清單裡
                self.assertIn(field, memspec.OPENED_TARGET_FIELDS)
        for free_text in ("command", "glob", "pattern"):
            self.assertNotIn(free_text, memspec.OPENED_STRONG_FIELDS)


class TheCodexCallShape(unittest.TestCase):
    """Codex 的 `custom_tool_call` 把程式整段塞在 freeform 的 `input` 裡。"""

    def _row(self, shape, name, **fields):
        return {"type": "response_item", "payload": {"type": shape, "name": name, **fields}}

    def test_a_freeform_input_survives_verbatim(self):
        text = "await tools.exec_command({cmd: 'Get-Date'});"
        row = self._row("custom_tool_call", "exec", input=text)
        _kind, _texts, tools = transcript.turn_parts(row)
        self.assertEqual(tools, [("exec", {transcript.FREEFORM_INPUT_FIELD: text})])

    def test_no_javascript_is_parsed_out_of_it(self):
        # 原文保留就是原文保留：不去猜 `cmd` 的值，猜錯比看不到糟。
        row = self._row("custom_tool_call", "exec", input="await tools.exec_command({cmd: 'x'});")
        _kind, _texts, tools = transcript.turn_parts(row)
        self.assertNotIn("cmd", tools[0][1])

    def test_a_json_input_is_still_parsed_as_fields(self):
        row = self._row("custom_tool_call", "exec", input=json.dumps({"cmd": "Get-Date"}))
        _kind, _texts, tools = transcript.turn_parts(row)
        self.assertEqual(tools[0][1], {"cmd": "Get-Date"})

    def test_handoff_collects_the_cmd_field(self):
        row = self._row("function_call", "exec_command", arguments=json.dumps({"cmd": "Get-Date"}))
        self.assertEqual(handoff._harvest([row])[1], ["Get-Date"])

    def test_handoff_still_collects_the_command_field(self):
        row = self._row("function_call", "Bash", arguments=json.dumps({"command": "Get-Date"}))
        self.assertEqual(handoff._harvest([row])[1], ["Get-Date"])

    def test_handoff_falls_back_to_the_verbatim_input(self):
        text = "await tools.exec_command({cmd: 'Get-Date'});"
        row = self._row("custom_tool_call", "exec", input=text)
        self.assertEqual(handoff._harvest([row])[1], [text])

    def test_a_long_freeform_input_is_truncated_not_dropped(self):
        row = self._row("custom_tool_call", "exec", input="x" * (handoff.COMMAND_MAX_CHARS * 3))
        self.assertEqual(len(handoff._harvest([row])[1][0]), handoff.COMMAND_MAX_CHARS)


class TheHookEventList(unittest.TestCase):
    def test_the_checker_reads_the_installer_list(self):
        self.assertIs(hook_trust.REQUIRED_EVENTS, graft.EVENTS)

    def test_subagent_stop_is_required_not_unexpected(self):
        self.assertIn("SubagentStop", hook_trust.REQUIRED_EVENTS)
        registered = json.loads(
            (Path(__file__).resolve().parents[1]
             / "adapters" / "codex" / "hooks_template.json").read_text(encoding="utf-8-sig")
        ).get("hooks", {})
        unexpected = [event for event in registered if event not in hook_trust.REQUIRED_EVENTS]
        self.assertEqual(unexpected, [])


def _selftest():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromTestCase(case)
        for case in (ThePatchEnvelopeParser, TheWriteGateOnEnvelopes,
                     TheCardContractOnEnvelopes, TheStrengthOfReadEvidence,
                     TheCodexCallShape, TheHookEventList)
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    passed = total - len(result.failures) - len(result.errors)
    print("SELFTEST %s %d/%d" % ("PASS" if passed == total else "FAIL", passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(_selftest() if "--selftest" in sys.argv[1:] else 0)
