import sys; sys.dont_write_bytecode = True
"""Owner 2026-11-13: with bind_only_core_rules, only core-rule cards carry triggers."""
import json
from pathlib import Path
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_guard_regression as fixtures
import pretooluse_gate as pre
import stop_gate as stop
from epitype import memspec

CORE_TEXT = "CORE-99. 派工限定範圍和回傳大小。"


class CoreOnlyBinding(unittest.TestCase):
    setUp = fixtures.ActionGuardRegression.setUp

    def write(self, filename, **fields):
        (self.vault / filename).write_text(fixtures.card(**fields), encoding="utf-8")

    def cards(self):
        self.write("rule-core-99.md", name="rule-core-99", description="core", layer="resident",
                   text=f'"{CORE_TEXT}"', advise_tool="Agent", advise_text="派工限定範圍和回傳大小。",
                   forbidden=["core-forbidden-text"])
        self.write("scar-old.md", name="scar-old", description="old scar", advise_tool="Agent",
                   advise_text="old reminder", forbidden=["old-forbidden-text"])
        # Looks like a core card by layer but is not numbered CORE-NN: not core.
        self.write("rule-floor.md", name="rule-floor", description="floor", layer="resident",
                   text='"Not a core rule."', advise_tool="Agent", advise_text="floor reminder")

    def set_flag(self, value):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config[memspec.CONFIG_CORE_ONLY_BINDING_FIELD] = value
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def event(self):
        return {"session_id": "core-only", "cwd": str(self.root), "hook_event_name": "PreToolUse"}

    def guard_cards(self):
        return sorted(guard.card for guard in pre._guards(self.vault, time.monotonic(), [],
                                                          event=self.event()))

    def decision_keys(self):
        return sorted(decision.key for decision in stop._decisions(
            self.vault, time.monotonic(), [], event=self.event()))

    def test_flag_on_keeps_only_core_rule_triggers(self):
        self.cards()
        self.set_flag(True)
        self.assertEqual(self.guard_cards(), ["rule-core-99"])
        self.assertEqual(self.decision_keys(), ["rule-core-99"])

    def test_flag_absent_or_not_true_keeps_every_card(self):
        self.cards()
        for value in (None, False, "true"):
            with self.subTest(value=value):
                if value is not None:
                    self.set_flag(value)
                self.assertEqual(self.guard_cards(), ["rule-core-99", "rule-floor", "scar-old"])
                self.assertEqual(self.decision_keys(), ["rule-core-99", "scar-old"])

    def set_extra(self, value):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config[memspec.CONFIG_BIND_ALSO_CARDS_FIELD] = value
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def test_named_noncore_card_still_binds_others_do_not(self):
        # Owner 2026-11-25: a few named non-core cards keep triggers; the rest stay memory.
        self.cards()
        self.set_flag(True)
        self.set_extra(["scar-old"])
        self.assertEqual(self.guard_cards(), ["rule-core-99", "scar-old"])
        self.assertEqual(self.decision_keys(), ["rule-core-99", "scar-old"])

    def test_malformed_extra_list_names_no_card(self):
        self.cards()
        self.set_flag(True)
        for value in ("scar-old", {"scar-old": True}, [None, "", "  "], ["scar-old.md"], ["SCAR-OLD"]):
            with self.subTest(value=value):
                self.set_extra(value)
                self.assertEqual(self.guard_cards(), ["rule-core-99"])
                self.assertEqual(self.decision_keys(), ["rule-core-99"])

    def test_extra_list_ignored_when_flag_off(self):
        self.cards()
        self.set_extra(["scar-old"])
        self.assertEqual(self.guard_cards(), ["rule-core-99", "rule-floor", "scar-old"])

    def test_drop_notice_for_named_noncore_card(self):
        import _hook_common as common
        self.cards()
        self.set_flag(True)
        os_env = __import__("os").environ
        old = os_env.get(memspec.EPITYPE_CONFIG_ENV)
        os_env[memspec.EPITYPE_CONFIG_ENV] = str(self.config)
        try:
            self.assertIsNone(common.rule_drop_notice(self.vault / "scar-old.md", "INVALID: x"))
            self.set_extra(["scar-old"])
            self.assertIsNotNone(common.rule_drop_notice(self.vault / "scar-old.md", "INVALID: x"))
        finally:
            if old is None:
                os_env.pop(memspec.EPITYPE_CONFIG_ENV, None)
            else:
                os_env[memspec.EPITYPE_CONFIG_ENV] = old

    # Strict matching above stays strict; a wrong entry must not stay quiet (findings
    # 2026-11-25): the session start names it and says what to write instead.
    def notices(self, vaults=None):
        from epitype import bind_also
        return bind_also.startup_notices(vaults or [self.vault])

    def test_misnamed_or_malformed_entries_are_reported(self):
        self.cards()
        self.set_flag(True)
        cases = (
            ("scar-old", [memspec.BIND_ALSO_NOT_LIST_NOTICE.format(kind="str")]),
            ({"scar-old": True}, [memspec.BIND_ALSO_NOT_LIST_NOTICE.format(kind="dict")]),
            (["scar-old.md"], [memspec.BIND_ALSO_NO_MATCH_HINT_NOTICE.format(
                name="scar-old.md", stem="scar-old")]),
            (["SCAR-OLD"], [memspec.BIND_ALSO_NO_MATCH_HINT_NOTICE.format(
                name="SCAR-OLD", stem="scar-old")]),
            (["no-such-card"], [memspec.BIND_ALSO_NO_MATCH_NOTICE.format(name="no-such-card")]),
            ([None, "scar-old"], [memspec.BIND_ALSO_BAD_ENTRY_NOTICE.format(entry="None")]),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.set_extra(value)
                self.assertEqual(self.notices(), expected)
        # The good entry next to a bad one still binds.
        self.assertEqual(self.guard_cards(), ["rule-core-99", "scar-old"])

    def test_notice_lines_are_capped(self):
        self.cards()
        self.set_flag(True)
        self.set_extra([f"missing-{index}" for index in range(6)])
        lines = self.notices()
        self.assertEqual(len(lines), memspec.BIND_ALSO_NOTICE_MAX_LINES)
        self.assertEqual(lines[-1], memspec.BIND_ALSO_MORE_NOTICE.format(
            count=6 - memspec.BIND_ALSO_NOTICE_MAX_LINES + 1))

    def test_correct_config_or_flag_off_is_silent(self):
        self.cards()
        self.set_extra(["scar-old.md", "SCAR-OLD"])
        self.assertEqual(self.notices(), [])  # flag off: the list is inert
        self.set_flag(True)
        self.set_extra(["scar-old"])
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.guard_cards(), ["rule-core-99", "scar-old"])

    def test_duplicate_stem_is_reported_and_every_copy_still_binds(self):
        self.cards()
        self.set_flag(True)
        self.set_extra(["scar-old"])
        (self.vault / "probe").mkdir()
        self.write("probe/scar-old.md", name="scar-old-probe", description="probe",
                   advise_tool="Agent", advise_text="probe reminder", forbidden=["probe-forbidden-text"])
        where = "、".join(sorted([str(self.vault / "probe" / "scar-old.md"), str(self.vault / "scar-old.md")]))
        self.assertEqual(self.notices(), [memspec.BIND_ALSO_AMBIGUOUS_NOTICE.format(
            name="scar-old", count=2, where=where)])
        # Warning only: binding is unchanged, both copies keep their triggers.
        self.assertEqual(self.guard_cards(), ["rule-core-99", "scar-old", "scar-old-probe"])
        import _hook_common as common
        self.assertIsNotNone(common.rule_drop_notice(self.vault / "scar-old.md", "INVALID: x"))
        (self.vault / "probe" / "scar-old.md").rename(self.vault / "probe" / "scar-probe.md")
        self.assertEqual(self.notices(), [])

    def test_duplicates_in_vaults_both_named_memory_are_told_apart(self):
        # Every native vault is `<slug>/memory`; a vault-name label would print the same twice.
        self.cards()
        self.set_flag(True)
        self.set_extra(["scar-old"])
        first, second = self.root / "proj-a" / "memory", self.root / "proj-b" / "memory"
        for vault in (first, second):
            vault.mkdir(parents=True)
            (vault / "scar-old.md").write_text(fixtures.card(name="scar-old", description="x"),
                                               encoding="utf-8")
        lines = self.notices([first, second])
        self.assertEqual(len(lines), 1)
        self.assertIn(str(first / "scar-old.md"), lines[0])
        self.assertIn(str(second / "scar-old.md"), lines[0])

    def test_walk_is_bounded_inside_a_vault_and_says_so(self):
        from epitype import cardscan
        self.cards()
        self.set_flag(True)
        for index in range(3):
            (self.vault / f"dir{index}").mkdir()
            (self.vault / f"dir{index}" / f"card{index}.md").write_text("x", encoding="utf-8")
        calls = []

        def out_of_time():
            calls.append(1)
            return len(calls) > 2  # time runs out after two directories of the one vault

        with self.assertRaises(TimeoutError):
            cardscan.scan_vault(self.vault, out_of_time=out_of_time)
        self.assertEqual(len(cardscan.scan_vault(self.vault)), 6)  # no deadline: unchanged
        self.set_extra(["scar-old.md", None])
        calls.clear()
        self.assertEqual(self.notices_with(out_of_time), [
            memspec.BIND_ALSO_INCOMPLETE_NOTICE,
            memspec.BIND_ALSO_BAD_ENTRY_NOTICE.format(entry="None")])

    def notices_with(self, out_of_time):
        from epitype import bind_also
        return bind_also.startup_notices([self.vault], out_of_time=out_of_time)

    def test_gate_reads_no_state_file(self):
        import _hook_common as common
        self.cards()
        self.set_flag(True)
        self.set_extra(["scar-old"])
        self.assertEqual(sorted(path.name for path in self.config.parent.glob("*.json")), ["config.json"])
        self.notices()
        self.assertEqual(sorted(path.name for path in self.config.parent.glob("*.json")), ["config.json"])
        self.assertEqual(common.core_only_extra_cards(), frozenset({"scar-old"}))

    def test_gate_never_walks_the_vault_for_the_check(self):
        import _hook_common as common
        from epitype import cardscan
        from unittest.mock import patch
        self.cards()
        self.set_flag(True)
        self.set_extra(["scar-old"])
        with patch.object(cardscan, "scan_vault", side_effect=AssertionError("vault walk")):
            self.assertEqual(common.core_only_extra_cards(), frozenset({"scar-old"}))

    def test_session_start_carries_the_notice(self):
        import sessionstart_hook
        self.cards()
        self.set_flag(True)
        self.set_extra(["scar-old.md"])
        event = {"session_id": "core-only-start", "cwd": str(self.root),
                 "hook_event_name": "SessionStart", "source": "startup"}
        value = sessionstart_hook._handle(event, time.monotonic())
        context = (value or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn(memspec.BIND_ALSO_NO_MATCH_HINT_NOTICE.format(
            name="scar-old.md", stem="scar-old"), context)

    def test_lint_checks_examples_of_named_cards(self):
        from epitype import card_lint
        self.write("scar-old.md", name="scar-old", description="old scar",
                   forbidden=["old-forbidden-text"], example_blocks=["old-forbidden-text here"])

        def example_rules():
            _type, findings = card_lint.check_card(self.vault / "scar-old.md", "scar-old.md")
            return [rule for _level, rule, _reason in findings if rule == "example"]

        self.set_flag(True)
        self.assertEqual(example_rules(), [])  # not named: memory, examples not linted
        self.set_extra(["scar-old"])
        self.assertEqual(example_rules(), ["example"])  # named: one-sided examples flagged
        self.set_flag(False)
        self.assertEqual(example_rules(), [])

    def test_core_detection(self):
        self.assertTrue(memspec.is_core_rule({"layer": "resident", "text": CORE_TEXT}))
        self.assertFalse(memspec.is_core_rule({"layer": "resident", "text": "核心 CORE-1. x"}))
        self.assertFalse(memspec.is_core_rule({"layer": "floor", "text": CORE_TEXT}))
        self.assertFalse(memspec.is_core_rule({"text": CORE_TEXT}))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.argv.remove("--selftest")
    unittest.main()
