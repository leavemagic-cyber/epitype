import sys; sys.dont_write_bytecode = True
"""The literal action guard: a scar card's fragments deny a call, nothing else does.

Owner 2026-09-09 (FAILURE_MODES §34) removed card-driven action interception on the
premise that the host's native rules would carry those hazards. 2026-09-16 that
premise was tested and failed for five of nine hazard classes -- Claude's Bash
patterns match positionally and have no AND operator -- and the owner lifted the
"cards may not carry an action condition" half of the ruling.

These tests pin the narrow form that came back, and equally pin what did NOT: no
regex, no shell parsing, no guess about what a command means. A call is denied only
when every literal fragment a card names is present in the call's own text.
"""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import memspec
import _hook_common as common
import pretooluse_gate as pretool

HEREDOC_WITH_BACKSLASH = "python - <<'PY'\n" + r"path = 'C:\Users\x'" + "\nPY\n"
PLAIN_HEREDOC = "python - <<'PY'\nprint(sum(range(10)))\nPY\n"
BACKSLASH_ONLY = r"findstr /C:'x' C:\Users\x\notes.txt"


def card(**fields):
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines += ["---", "body", ""]
    return "\n".join(lines)


class ActionGuardRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-guard-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {
            memspec.EPITYPE_CONFIG_ENV: str(self.config),
            memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def write_card(self, filename, **fields):
        (self.vault / filename).write_text(card(**fields), encoding="utf-8")
        # Discovery is cached against the manifest; a fresh card must be re-read.
        cache = pretool._guard_cache(self.vault)
        if cache.exists():
            cache.unlink()

    def heredoc_card(self):
        self.write_card(
            "scar-heredoc.md",
            name="heredoc 吃掉反斜線",
            description="shell 的 heredoc 會吃掉一層反斜線",
            guard_tool="Bash",
            guard_all_of=['"<<"', '"\\\\"'],
            guard_advice="改用寫檔工具建檔，不要用 heredoc 生內容",
        )

    def call(self, tool_name, tool_input, defects=None):
        return pretool._handle(
            {"tool_name": tool_name, "tool_input": tool_input, "session_id": "guard-test"},
            time.monotonic(),
            [] if defects is None else defects,
        )

    def denial(self, value):
        return (value or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason")

    def test_all_fragments_present_denies_and_names_the_card(self):
        self.heredoc_card()
        reason = self.denial(self.call("Bash", {"command": HEREDOC_WITH_BACKSLASH}))
        self.assertIsNotNone(reason)
        self.assertIn("heredoc 吃掉反斜線", reason)
        self.assertIn("改用寫檔工具建檔", reason)

    def test_a_missing_fragment_is_not_a_hit(self):
        # The conjunction is the whole safety story: either fragment alone is ordinary.
        self.heredoc_card()
        for command in (PLAIN_HEREDOC, BACKSLASH_ONLY, "git status"):
            self.assertIsNone(self.denial(self.call("Bash", {"command": command})), command)

    def test_the_guard_only_applies_to_its_own_tool(self):
        self.heredoc_card()
        self.assertIsNone(self.denial(self.call("Read", {"command": HEREDOC_WITH_BACKSLASH})))

    def test_the_guard_fires_every_time_not_once_per_session(self):
        # A guard that stops guarding after one hit would pass the second attempt,
        # which is precisely the repeat it exists to prevent.
        self.heredoc_card()
        for _ in range(3):
            self.assertIsNotNone(self.denial(self.call("Bash", {"command": HEREDOC_WITH_BACKSLASH})))

    def test_fragments_are_sought_in_every_string_the_call_carries(self):
        self.heredoc_card()
        value = self.call("Bash", {"description": "寫檔", "command": HEREDOC_WITH_BACKSLASH})
        self.assertIsNotNone(self.denial(value))

    def test_a_lone_short_fragment_is_refused_and_named_rather_than_enforced(self):
        # ["\\"] alone would deny almost every Windows command; disabling a whole
        # tool is the host's native rules' job, so the card is reported, not obeyed.
        self.write_card(
            "scar-too-wide.md",
            name="太寬的守衛",
            description="只有一個短片段",
            guard_tool="Bash",
            guard_all_of=['"\\\\"'],
        )
        defects = []
        self.assertIsNone(self.denial(self.call("Bash", {"command": BACKSLASH_ONLY}, defects)))
        self.assertTrue(any("太寬的守衛" in line for line in defects), defects)

    def test_fields_nested_under_a_parent_block_enforce_nothing(self):
        # 2026-09-16: three freshly written decision cards were silently disarmed
        # this way. The gate reads top-level fields only, so a nested card must not
        # look armed -- card_lint is what tells the author, not a surprise denial.
        (self.vault / "scar-nested.md").write_text(
            "---\nname: 巢狀守衛\ndescription: 欄位被包進 metadata\nmetadata:\n"
            '  guard_tool: Bash\n  guard_all_of:\n    - "<<"\n    - "\\\\"\n---\nbody\n',
            encoding="utf-8",
        )
        cache = pretool._guard_cache(self.vault)
        if cache.exists():
            cache.unlink()
        self.assertIsNone(self.denial(self.call("Bash", {"command": HEREDOC_WITH_BACKSLASH})))

    def test_warming_discovers_a_guard_past_one_call_s_slice(self):
        # A tool call reads a bounded slice, so on a cold cache a guard sitting past
        # it is not enforced yet and says nothing about it. SessionStart warms once.
        for index in range(memspec.ACTION_GUARD_MAX_CARDS_PER_VAULT + 5):
            (self.vault / f"aaa{index:03d}.md").write_text(
                f"---\nname: filler{index}\ndescription: 2026-09-16 佔位卡\n---\nbody\n",
                encoding="utf-8",
            )
        self.heredoc_card()  # sorts after the fillers, so one call cannot reach it
        self.assertIsNone(self.denial(self.call("Bash", {"command": HEREDOC_WITH_BACKSLASH})))
        pretool.warm_guard_cache([self.vault], time.monotonic())
        self.assertIsNotNone(self.denial(self.call("Bash", {"command": HEREDOC_WITH_BACKSLASH})))

    def test_no_card_means_no_opinion_about_any_command(self):
        # The four hazards §34 retired stay retired unless a card names them.
        for command in (
            HEREDOC_WITH_BACKSLASH,
            "taskkill /IM claude.exe /F",
            "git reset --hard",
            "Get-Content .env",
        ):
            self.assertIsNone(self.denial(self.call("Bash", {"command": command})), command)

    def test_a_broken_config_denies_nothing(self):
        # main() swallows the raise; what matters here is that no denial is produced.
        self.heredoc_card()
        self.config.write_text("{broken", encoding="utf-8")
        result = common.run_synthetic(
            Path(pretool.__file__),
            {"tool_name": "Bash", "tool_input": {"command": HEREDOC_WITH_BACKSLASH}},
            self.config,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_an_expired_deadline_denies_nothing(self):
        self.heredoc_card()
        value = pretool._handle(
            {"tool_name": "Bash", "tool_input": {"command": HEREDOC_WITH_BACKSLASH}}, 0, []
        )
        self.assertIsNone(self.denial(value))


class RequireWhenThen(unittest.TestCase):
    """The conversion for rules whose compliance is invisible from outside.

    A rule like "verify before claiming done" cannot be checked: whether I verified
    happens in my head. Restated as "if you claim done, say what you checked", the
    omission becomes visible, and a fabricated check stops being a skipped step and
    becomes a false statement, which the honesty floor already covers."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-require-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        (self.vault / "decision-evidence.md").write_text(
            "---\nname: 完成宣稱要帶證據\n"
            "description: 說做完了就要同時寫出查了什麼\n"
            "decision_key: completion-carries-evidence\nstatus: active\n"
            "current_decision_at: 2026-09-16\ndecided_by: owner-explicit\n"
            "owner_quote: 完成要寫明證據範圍\n"
            "aliases: [完成宣稱, 證據範圍]\n"
            "require_when: (已完成|做完了|全部通過)\n"
            "require_text: (實測|讀了|跑了|PASS)\n---\nbody\n",
            encoding="utf-8",
        )
        environment = patch.dict(os.environ, {
            memspec.EPITYPE_CONFIG_ENV: str(self.config),
            memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def block(self, message):
        import stop_gate

        markers = patch.object(stop_gate, "recall_marker_directory",
                               lambda session: self.root / "markers" / str(session))
        markers.start()
        self.addCleanup(markers.stop)
        return stop_gate._handle(
            {"session_id": "req-" + str(id(message)), "stop_hook_active": False,
             "last_assistant_message": message},
            time.monotonic(), [],
        )

    def test_a_behaviour_card_arms_without_carrying_a_ruling_of_its_own(self):
        # card_lint tells a behaviour card's author to add `forbidden` or
        # `require_when`. If only decision-keyed cards were read, obeying that
        # instruction would enforce nothing -- silent disarming delivered by the
        # lint's own advice. 2026-09-16: six armed cards were inert exactly so.
        (self.vault / "feedback-plain.md").write_text(
            "---\nname: 行為卡沒有裁定鍵\ndescription: 說明文字\n"
            "forbidden:\n  - 這句話不准說\n---\nbody\n",
            encoding="utf-8",
        )
        value = self.block("我還是要講這句話不准說。")
        self.assertIsNotNone(value)
        self.assertIn("行為卡沒有裁定鍵", value.get("reason", ""))

    def test_a_retired_behaviour_card_stops_speaking(self):
        (self.vault / "feedback-retired.md").write_text(
            "---\nname: 已退役\ndescription: 說明\nstatus: superseded\n"
            "forbidden:\n  - 退役禁語\n---\nbody\n",
            encoding="utf-8",
        )
        self.assertIsNone(self.block("這裡出現退役禁語。"))

    def test_a_completion_claim_without_evidence_is_blocked(self):
        value = self.block("這批已完成，可以進下一步。")
        self.assertIsNotNone(value)
        self.assertIn("completion-carries-evidence", value.get("reason", ""))

    def test_the_same_claim_with_evidence_passes(self):
        self.assertIsNone(self.block("這批已完成：實測 8/8，卡片體檢 FAIL 0。"))

    def test_a_turn_that_never_triggers_is_untouched(self):
        self.assertIsNone(self.block("我先讀了設定檔，還沒有動任何東西。"))

    def test_a_claim_inside_a_quotation_is_a_citation_not_a_claim(self):
        self.assertIsNone(self.block("規則擋的是「這批已完成」這種沒有證據的講法。"))


class GuardCardLint(unittest.TestCase):
    """§34's third objection to `trigger:` was that a mis-written card fails
    silently. These pin the answer: the lint says so, at write time, as a FAIL."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-guardlint-")
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name).resolve()

    def findings(self, filename, text):
        from epitype import card_lint

        (self.vault / filename).write_text(text, encoding="utf-8")
        _type, findings = card_lint.check_card(self.vault / filename, filename)
        return {(level, rule) for level, rule, _reason in findings}, findings

    def test_a_field_nested_one_level_deep_is_a_fail_not_a_pass(self):
        rules, findings = self.findings(
            "scar-nested.md",
            "---\nname: 巢狀守衛\ndescription: 2026-09-16 欄位被包進 metadata\nmetadata:\n"
            '  guard_tool: Bash\n  guard_all_of:\n    - "<<"\n---\nbody\n',
        )
        self.assertIn(("FAIL", "disarmed-field"), rules, findings)
        reason = next(r for lvl, rule, r in findings if rule == "disarmed-field")
        self.assertIn("頂層", reason)

    def test_a_nested_decision_key_is_caught_the_same_way(self):
        rules, _ = self.findings(
            "decision-nested.md",
            "---\nname: 巢狀裁定\ndescription: 2026-09-16 欄位被包進 metadata\nmetadata:\n"
            "  decision_key: x\n  status: active\n---\nbody\n",
        )
        self.assertIn(("FAIL", "disarmed-field"), rules)

    def test_a_lone_short_fragment_fails_the_lint(self):
        rules, _ = self.findings(
            "scar-wide.md",
            "---\nname: 太寬\ndescription: 2026-09-16 只有一個短片段\n"
            'guard_tool: Bash\nguard_all_of:\n  - "\\\\"\n---\nbody\n',
        )
        self.assertIn(("FAIL", "guard"), rules)

    def behaviour_card(self, filename, extra="", stamp="2026-09-16"):
        return self.findings(
            filename,
            "---\nname: 行為卡\ndescription: 2026-09-16 owner 糾正了一個行為\n"
            f"aliases:\n  - 別名甲\nlast_verified_at: {stamp}\n" + extra
            + "metadata:\n  type: feedback\n---\nbody\n",
        )

    def test_a_behaviour_card_written_now_must_arm_or_say_it_cannot(self):
        # The choice of card shape used to be the writer's alone, and 170 times the
        # writer picked the shape that binds nobody. It is no longer a silent choice.
        rules, _ = self.behaviour_card("feedback-unarmed.md")
        self.assertIn(("FAIL", "unarmed"), rules)

    def test_each_arming_field_satisfies_the_rule(self):
        for extra in (
            'forbidden:\n  - 再提議把這件事推回 owner\n',
            'guard_tool: Bash\nguard_all_of:\n  - "<<"\n  - "\\\\"\n',
            "require_when: (已完成)\nrequire_text: (實測)\n",
            "unenforceable: 判斷型，訊息裡沒有可比對的字面訊號\n",
        ):
            rules, _ = self.behaviour_card("feedback-armed.md", extra)
            self.assertNotIn(("FAIL", "unarmed"), rules, extra)
            self.assertNotIn(("WARN", "unarmed"), rules, extra)

    def test_an_older_behaviour_card_is_a_warning_not_a_failure(self):
        # A rule change does not retroactively fail a year of cards; it makes the
        # backlog countable. 2026-09-16 that count was 194.
        rules, _ = self.behaviour_card("feedback-old.md", stamp="2026-08-01")
        self.assertIn(("WARN", "unarmed"), rules)
        self.assertNotIn(("FAIL", "unarmed"), rules)

    def test_the_retired_trigger_field_does_not_count_as_armed(self):
        rules, _ = self.behaviour_card(
            "feedback-trigger.md", 'trigger: {tool: "^(Bash)$", input: "rm -rf"}\n'
        )
        self.assertIn(("FAIL", "unarmed"), rules)

    def test_a_requirement_with_only_one_half_fails_the_lint(self):
        for text, name in (
            ("require_when: (已完成)\n", "decision-half-a.md"),
            ("require_text: (實測)\n", "decision-half-b.md"),
        ):
            rules, _ = self.findings(
                name,
                "---\nname: 半條要求\ndescription: 2026-09-16 只寫了一半\n"
                "decision_key: half\nstatus: active\ncurrent_decision_at: 2026-09-16\n"
                "decided_by: owner-explicit\nowner_quote: x\naliases: [甲, 乙]\n"
                + text + "---\nbody\n",
            )
            self.assertIn(("FAIL", "require-pair"), rules, name)

    def test_a_guard_with_no_fragments_fails_the_lint(self):
        rules, _ = self.findings(
            "scar-empty.md",
            "---\nname: 沒片段\ndescription: 2026-09-16 缺 guard_all_of\nguard_tool: Bash\n---\nbody\n",
        )
        self.assertIn(("FAIL", "guard"), rules)

    def test_a_well_formed_guard_card_raises_no_guard_finding(self):
        _rules, findings = self.findings(
            "scar-ok.md",
            "---\nname: heredoc 吃掉一層反斜線\ndescription: 2026-09-16 heredoc 會吃掉一層反斜線\n"
            'guard_tool: Bash\nguard_all_of:\n  - "<<"\n  - "\\\\"\n'
            "guard_advice: 改用寫檔工具\nlast_verified_at: 2026-09-16\n---\nbody\n",
        )
        self.assertEqual([f for f in findings if f[1] in ("guard", "disarmed-field")], [])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
