import sys; sys.dont_write_bytecode = True
"""A card whose fields the host moved under `metadata:` is still the same card.

The Claude desktop app's memory writer keeps `name`, `description` and `metadata` and
nests every other top-level key under `metadata:`. 2026-09-30 two real cards were
rewritten that way and stopped blocking: every reader took top-level fields only.
These tests pin the shared reading (memspec.hoist_metadata_fields) at each reader
that rules on a card — both gates, the lint, the index, the recall mark — and pin
what did NOT change: a field under any other parent still enforces nothing.
"""
import json
import os
from datetime import date, timedelta
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import card_lint, memsearch, memspec
import _hook_common as common
import pretooluse_gate as pretool
import recall_hook
import stop_gate as stop

HEAD = "---\nname: nested-ruling\ndescription: 2026-09-30 宿主把欄位包進 metadata\n"
RULING_FIELDS = (
    "decision_key: nested-ruling\nstatus: active\ncurrent_decision_at: 2026-09-30\n"
    "decided_by: owner-explicit\nowner_quote: 合成的 owner 原話\n"
    "forbidden:\n  - 合成禁語甲\naliases:\n  - 合成別名\n"
    "example_blocks:\n  - 這裡出現合成禁語甲\nexample_allows:\n  - 這一句沒有問題\n"
    "last_verified_at: 2026-09-30\n"
)


def indented(block, spaces=2):
    return "".join(" " * spaces + line + "\n" for line in block.splitlines())


# What the host's writer leaves behind: everything but name/description one level down.
NESTED_RULING = HEAD + "metadata:\n  type: feedback\n" + indented(RULING_FIELDS) + "---\nbody\n"
TOP_LEVEL_RULING = HEAD + RULING_FIELDS + "metadata:\n  type: feedback\n---\nbody\n"
# Same indentation, another parent: no writer produces this, and no gate reads it.
OTHER_PARENT_RULING = (
    HEAD + "metadata:\n  type: feedback\nextra:\n" + indented(RULING_FIELDS) + "---\nbody\n")
BLOCKED_MESSAGE = "這一段回覆裡有合成禁語甲。"


class SharedReading(unittest.TestCase):
    """The hoist itself: same lines, reordered, and nothing invented."""

    def front(self, text):
        return text.split("---\n")[1].splitlines()

    def test_nested_and_top_level_cards_read_the_same(self):
        nested, _problem = memspec.frontmatter_text(NESTED_RULING)
        top, _problem = memspec.frontmatter_text(TOP_LEVEL_RULING)
        self.assertEqual(nested, top)
        for field in (memspec.FORBIDDEN_FIELD, memspec.ALIASES_FIELD,
                      memspec.EXAMPLE_BLOCKS_FIELD, memspec.EXAMPLE_ALLOWS_FIELD):
            self.assertEqual(
                memspec.sequence_items(memspec.split_frontmatter(NESTED_RULING)[0], field),
                memspec.sequence_items(memspec.split_frontmatter(TOP_LEVEL_RULING)[0], field),
                field)

    def test_the_lines_are_a_reordering_and_the_closing_index_does_not_move(self):
        raw = self.front(NESTED_RULING)
        lines, closing, origins, hoisted = memspec.frontmatter_layout(NESTED_RULING)
        self.assertEqual(closing, len(raw) + 1)
        self.assertEqual(sorted(origins), list(range(len(raw))))
        self.assertEqual([line.strip() for line in lines],
                         [raw[origin].strip() for origin in origins])
        self.assertIn(memspec.FORBIDDEN_FIELD, hoisted)
        self.assertNotIn("type", hoisted)
        self.assertIn("  type: feedback", lines)

    def test_a_top_level_field_wins_over_the_nested_one(self):
        text = (HEAD + "forbidden:\n  - 頂層禁語\nmetadata:\n  type: feedback\n"
                "  forbidden:\n    - 巢狀禁語\n  status: active\n---\nbody\n")
        lines, _closing, _origins, hoisted = memspec.frontmatter_layout(text)
        self.assertEqual(memspec.sequence_items(lines, memspec.FORBIDDEN_FIELD), ["頂層禁語"])
        self.assertEqual(hoisted, (memspec.DECISION_STATUS_FIELD,))

    def test_items_at_the_key_s_own_indent_stay_items(self):
        text = HEAD + "metadata:\n  type: feedback\n  forbidden:\n  - 同層項目甲\n  - 同層項目乙\n---\nbody\n"
        self.assertEqual(
            memspec.sequence_items(memspec.split_frontmatter(text)[0], memspec.FORBIDDEN_FIELD),
            ["同層項目甲", "同層項目乙"])

    def test_a_nested_block_scalar_keeps_its_text(self):
        text = (HEAD + "metadata:\n  type: feedback\n  owner_quote: >-\n    第一行\n    第二行\n"
                "  status: active\n---\nbody\n")
        fields, problem = memspec.frontmatter_text(text)
        self.assertIsNone(problem)
        self.assertEqual(fields[memspec.OWNER_QUOTE_FIELD], "第一行 第二行")
        self.assertEqual(fields[memspec.DECISION_STATUS_FIELD], "active")

    def test_a_problem_is_reported_at_the_line_it_is_written_on(self):
        text = HEAD + "metadata:\n  type: feedback\n  owner_quote: \"沒有收尾\n---\nbody\n"
        _fields, problem = memspec.frontmatter_text(text)
        self.assertIn("L6", problem)

    def test_layouts_that_are_not_a_plain_block_are_left_as_written(self):
        for label, body in (
            ("flow mapping", "metadata: {type: feedback, decision_key: x}\n"),
            ("tab indent", "metadata:\n\tdecision_key: x\n"),
            ("not a mapping", "metadata:\n  - decision_key: x\n"),
            ("dedent inside the block", "metadata:\n    type: feedback\n  decision_key: x\n"),
        ):
            raw = (HEAD + body).split("---\n")[1].splitlines()
            lines, origins, hoisted = memspec.hoist_metadata_fields(raw)
            self.assertEqual((lines, origins, hoisted), (raw, list(range(len(raw))), ()), label)

    def test_frontmatter_that_never_closes_is_not_reordered(self):
        text = NESTED_RULING.replace("---\nbody\n", "body\n")
        lines, closing, origins, hoisted = memspec.frontmatter_layout(text)
        self.assertIsNone(closing)
        self.assertEqual(lines, text.splitlines()[1:])
        self.assertEqual(hoisted, ())

    def test_bom_and_crlf_cards_are_hoisted_too(self):
        text = "\ufeff" + NESTED_RULING.replace("\n", "\r\n")
        fields, problem = memspec.frontmatter_text(text)
        self.assertIsNone(problem)
        self.assertEqual(fields[memspec.DECISION_KEY_FIELD], "nested-ruling")


class GateFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-metadata-nested-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            "CODEX_HOME": str(self.root / "home" / ".codex"),
            memspec.EPITYPE_CONFIG_ENV: str(self.config),
            memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
        })
        environment.start()
        self.addCleanup(environment.stop)
        marker = patch.object(stop, "recall_marker_directory",
                              lambda session: self.root / "markers" / common.session_component(session))
        marker.start()
        self.addCleanup(marker.stop)

    def card(self, text, name="nested-ruling.md"):
        path = self.vault / name
        path.write_text(text, encoding="utf-8")
        return path

    def stop_verdict(self, message=BLOCKED_MESSAGE, session="nested"):
        return stop._handle(
            {"session_id": session, "last_assistant_message": message}, time.monotonic(), [])


class StopGate(GateFixture):
    def test_a_forbidden_list_under_metadata_still_blocks(self):
        self.card(NESTED_RULING)
        value = self.stop_verdict()
        self.assertEqual((value or {}).get("decision"), "block", value)
        self.assertIn("合成禁語甲", value["reason"])

    def test_the_real_hook_process_blocks_the_same_card(self):
        self.card(NESTED_RULING)
        # 子行程的「同一段已擋過一次」標記落在暫存目錄；不隔離的話它會留在真的 %TEMP%，
        # 第二次跑這一題就被當成重送而放行。
        scratch = self.root / "tmp"
        scratch.mkdir()
        result = common.run_synthetic(
            Path(stop.__file__),
            {"session_id": "nested-process", "hook_event_name": "Stop",
             "stop_hook_active": False, "last_assistant_message": BLOCKED_MESSAGE},
            self.config, clock=memspec.HOOK_CLOCK_FROZEN,
            environment={name: str(scratch) for name in ("TMPDIR", "TEMP", "TMP")})
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout) if result.stdout.strip() else {}
        self.assertEqual(value.get("decision"), "block", (result.stdout, result.stderr))

    def test_nested_and_top_level_cards_yield_the_same_ruling(self):
        nested = stop._read_decision(self.card(NESTED_RULING, "nested.md"))
        top = stop._read_decision(self.card(TOP_LEVEL_RULING, "top.md"))
        self.assertIsNotNone(nested)
        self.assertEqual(nested, top)

    def test_the_example_lines_behave_as_written(self):
        self.card(NESTED_RULING)
        self.assertIsNone(self.stop_verdict("這一句沒有問題", session="allowed"))

    def test_a_field_under_another_parent_still_blocks_nothing(self):
        path = self.card(OTHER_PARENT_RULING)
        self.assertIsNone(stop._read_decision(path))
        self.assertIsNone(self.stop_verdict())

    def test_a_nested_status_retires_the_ruling(self):
        path = self.card(NESTED_RULING.replace("  status: active\n", "  status: superseded\n"))
        self.assertIsNone(stop._read_decision(path))
        self.assertIsNone(self.stop_verdict())

    def test_a_top_level_list_wins_over_the_nested_one(self):
        path = self.card(NESTED_RULING.replace(
            "metadata:\n", "forbidden:\n  - 頂層禁語\nmetadata:\n"))
        self.assertEqual(stop._read_decision(path)[memspec.FORBIDDEN_FIELD], ["頂層禁語"])
        self.assertIsNone(self.stop_verdict())

    def test_a_cache_that_called_the_card_inert_is_not_trusted(self):
        # 修正前的閘把這張卡認成「不是裁定」並存進快取；換版之後那個負例要重認，
        # 否則卡片不動就一直不擋，直到輪替剛好輪到它。
        path = self.card(NESTED_RULING)
        stop._decisions(self.vault, time.monotonic())
        cache = self.vault / memspec.FTS_INDEX_DIRECTORY / stop._DECISION_CACHE_FILENAME
        loaded = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(loaded["version"], stop._DECISION_CACHE_VERSION)
        self.assertTrue(loaded["decisions"][path.name])
        loaded["version"] = stop._DECISION_CACHE_VERSION - 1
        loaded["decisions"][path.name] = None
        cache.write_text(json.dumps(loaded, ensure_ascii=False), encoding="utf-8")
        self.assertEqual((self.stop_verdict() or {}).get("decision"), "block")


class ActionGuard(GateFixture):
    HEREDOC_WITH_BACKSLASH = "python - <<'PY'\n" + r"path = 'C:\Users\x'" + "\nPY\n"
    GUARD_FIELDS = (
        'guard_tool: Bash\nguard_all_of:\n  - "<<"\n  - "\\\\"\n'
        "guard_advice: 改用寫檔工具建檔\n"
    )

    def denial(self, tool_name, tool_input):
        value = pretool._handle(
            {"tool_name": tool_name, "tool_input": tool_input, "session_id": "nested-guard"},
            time.monotonic(), [])
        return (value or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason")

    def guard_card(self, parent):
        self.card(
            "---\nname: 巢狀守衛\ndescription: 2026-09-30 守衛欄位被包進下一層\n"
            "metadata:\n  type: scar\n"
            + ("" if parent == "metadata" else parent + ":\n")
            + indented(self.GUARD_FIELDS) + "---\nbody\n", "scar-nested.md")

    def test_a_guard_under_metadata_still_denies(self):
        self.guard_card("metadata")
        reason = self.denial("Bash", {"command": self.HEREDOC_WITH_BACKSLASH})
        self.assertIsNotNone(reason)
        self.assertIn("巢狀守衛", reason)
        self.assertIn("改用寫檔工具建檔", reason)
        self.assertIsNone(self.denial("Bash", {"command": "git status"}))

    def test_a_guard_under_another_parent_still_denies_nothing(self):
        self.guard_card("extra")
        self.assertIsNone(self.denial("Bash", {"command": self.HEREDOC_WITH_BACKSLASH}))

    def test_a_write_check_under_metadata_still_denies(self):
        self.card(
            "---\nname: ps51\ndescription: PowerShell 5.1 需要 UTF-8 BOM\n"
            "metadata:\n  type: feedback\n  last_verified_at: 2026-09-27\n"
            "  aliases: [PowerShell BOM]\n  write_check: ps51_utf8_bom\n---\nBody\n", "ps51.md")
        target = self.root / "new.ps1"
        self.assertIsNotNone(self.denial("Write", {"file_path": str(target), "content": "# 中文\n"}))
        self.assertIsNone(self.denial(
            "Write", {"file_path": str(target), "content": "\ufeff# 中文\n"}))

    def test_field_spans_point_at_the_card_s_own_lines(self):
        # 再犯閘靠這些行號把豁免欄位的行從差異裡拿掉；搬過的行要指回檔案裡原本那一行。
        lines = pretool._card_lines(NESTED_RULING)
        blocks, inside = pretool._field_spans(NESTED_RULING, (memspec.FORBIDDEN_FIELD,))
        self.assertEqual(blocks[memspec.FORBIDDEN_FIELD], ("forbidden:", "  - 合成禁語甲"))
        self.assertEqual([lines[index] for index in sorted(inside)],
                         ["  forbidden:", "    - 合成禁語甲"])
        top_blocks, _inside = pretool._field_spans(TOP_LEVEL_RULING, (memspec.FORBIDDEN_FIELD,))
        self.assertEqual(blocks, top_blocks)


class LintIndexAndRecall(GateFixture):
    def findings(self, text, name="nested-ruling.md"):
        path = self.card(text, name)
        _type, findings = card_lint.check_card(path, name)
        return {(level, rule) for level, rule, _reason in findings}, findings

    def test_the_lint_says_where_the_fields_are_and_does_not_call_them_disarmed(self):
        rules, findings = self.findings(NESTED_RULING)
        self.assertNotIn((card_lint.FAIL, "disarmed-field"), rules, findings)
        self.assertIn((card_lint.INFO, "metadata-nested"), rules, findings)
        reason = next(reason for _level, rule, reason in findings if rule == "metadata-nested")
        self.assertIn(memspec.FORBIDDEN_FIELD, reason)
        self.assertNotIn("type", reason.split("metadata")[0])

    def test_the_lint_rules_on_a_nested_card_exactly_as_on_the_top_level_one(self):
        nested, _ = self.findings(NESTED_RULING, "nested.md")
        top, _ = self.findings(TOP_LEVEL_RULING, "top.md")
        self.assertEqual(nested - {(card_lint.INFO, "metadata-nested")}, top)

    def test_another_parent_is_still_a_disarmed_fail(self):
        rules, findings = self.findings(OTHER_PARENT_RULING)
        self.assertIn((card_lint.FAIL, "disarmed-field"), rules, findings)
        self.assertNotIn((card_lint.INFO, "metadata-nested"), rules)

    def test_a_flow_mapping_is_still_a_disarmed_fail(self):
        rules, findings = self.findings(
            HEAD + "metadata: {type: feedback, decision_key: x}\n---\nbody\n")
        self.assertIn((card_lint.FAIL, "disarmed-field"), rules, findings)

    def test_the_index_carries_a_nested_status(self):
        # 索引讀不到巢狀的 status 時，被取代的卡會一直被當成現行卡喚回。
        self.card(NESTED_RULING)
        memsearch.build_index(str(self.vault))
        rows = memsearch.query_index(str(self.vault), "合成別名")["results"]
        self.assertEqual([(row["card_path"], row[memspec.DECISION_STATUS_FIELD]) for row in rows],
                         [("nested-ruling.md", "active")])
        self.card(NESTED_RULING.replace("  status: active\n", "  status: superseded\n"))
        memsearch.build_index(str(self.vault))
        self.assertEqual(memsearch.query_index(str(self.vault), "合成別名")["results"], [])

    def test_the_stale_mark_reads_a_nested_verification_date(self):
        stamp = (date.today() - timedelta(days=97)).isoformat()
        path = self.card(
            "---\nname: project-old\ndescription: 舊專案狀態\nmetadata:\n  type: project\n"
            f"  last_verified_at: {stamp}\n---\nbody\n", "project-old.md")
        self.assertIn("97", recall_hook._stale_state_mark(path))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
