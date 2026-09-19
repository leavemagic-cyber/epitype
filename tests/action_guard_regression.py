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
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import compliance, memspec
import _hook_common as common
import pretooluse_gate as pretool

HEREDOC_WITH_BACKSLASH = "python - <<'PY'\n" + r"path = 'C:\Users\x'" + "\nPY\n"
PLAIN_HEREDOC = "python - <<'PY'\nprint(sum(range(10)))\nPY\n"
BACKSLASH_ONLY = r"findstr /C:'x' D:\data\x\notes.txt"


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

    def test_the_guard_still_applies_during_a_stop_retry(self):
        # `stop_hook_active` marks the re-run after the Stop gate blocked a turn.
        # Honouring it here switched the guard off exactly when the model has been
        # told to change approach and is most likely to reach for something rash.
        self.heredoc_card()
        value = pretool._handle(
            {"tool_name": "Bash", "tool_input": {"command": HEREDOC_WITH_BACKSLASH},
             "session_id": "retry", "stop_hook_active": True},
            time.monotonic(), [],
        )
        self.assertIsNotNone(self.denial(value))

    def test_the_guard_only_applies_to_its_own_tool(self):
        self.heredoc_card()
        self.assertIsNone(self.denial(self.call("Read", {"command": HEREDOC_WITH_BACKSLASH})))

    def test_a_bash_card_guards_the_same_shell_tool_under_another_host_s_name(self):
        # Cursor sends its shell tool as "Shell" (probe log 2026-09-16); exact name
        # matching left every Bash guard silently off there.
        self.heredoc_card()
        for tool in ("Shell", "exec_command"):
            self.assertIsNotNone(self.denial(self.call(tool, {"command": HEREDOC_WITH_BACKSLASH})), tool)
        self.assertIsNone(self.denial(self.call("PowerShell", {"command": HEREDOC_WITH_BACKSLASH})))

    def test_the_guard_fires_every_time_not_once_per_session(self):
        # A guard that stops guarding after one hit would pass the second attempt,
        # which is precisely the repeat it exists to prevent.
        self.heredoc_card()
        for _ in range(3):
            self.assertIsNotNone(self.denial(self.call("Bash", {"command": HEREDOC_WITH_BACKSLASH})))

    def test_fragments_are_sought_across_the_fields_that_act(self):
        # One fragment in the path, one in the content: no acting field is privileged
        # and the call is read whole. The case this replaced put both fragments in
        # `command` alone, so it never actually exercised a second field.
        self.write_card(
            "scar-write.md",
            name="金鑰不要寫進設定檔",
            description="設定檔裡出現長期金鑰",
            guard_tool="Write",
            guard_all_of=['"settings.json"', '"sk-live-"'],
            guard_advice="金鑰放環境變數，不要寫進設定檔",
        )
        value = self.call(
            "Write", {"file_path": "/tmp/settings.json", "content": "key = sk-live-123"}
        )
        self.assertIsNotNone(self.denial(value))

    def test_the_owner_facing_description_cannot_fake_a_hit(self):
        # 2026-09-18: a read-only `ls ... .jsonl` was denied twice by the "diagnostic
        # jsonl must survive" scar because the call's description read "Confirm ..."
        # and "Confirm " carries "rm ". The description is prose for the owner's
        # permission card; it never executes, so it is not evidence of an action.
        self.write_card(
            "scar-jsonl.md",
            name="診斷觀測檔在問題結案前不得刪",
            description="觀測用的 jsonl 在問題結案前刪掉，證據就沒了",
            guard_tool="Bash",
            guard_all_of=['"rm "', '".jsonl"'],
            guard_advice="觀測紀錄在問題結案前不要刪",
        )
        innocent = self.call("Bash", {
            "description": "Confirm both transcripts are still on disk",
            "command": "ls ./projects/vault/observations.jsonl",
        })
        self.assertIsNone(self.denial(innocent))
        # The real deletion is still denied, whatever the description says.
        for description in ("clean up", "Confirm the sweep"):
            guilty = self.call("Bash", {
                "description": description,
                "command": "rm /tmp/shimtest_log.jsonl",
            })
            self.assertIsNotNone(self.denial(guilty), description)

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

    def test_a_test_file_may_contain_the_forbidden_form(self):
        # 規則的回歸測試必須寫得出那句被禁的話。2026-09-19 兩次：為新規則寫測試樣本時，
        # 被新規則自己擋下——這道閘擋掉的第一份東西就是證明它有效的那份測試。
        self.write_card(
            "decision-fixture.md",
            name="樣本測試",
            description="說明",
            forbidden=["內部代號DDD"],
        )
        blocked = self.call("Write", {"file_path": str(self.root / "note.md"),
                                      "content": "內部代號DDD"})
        self.assertIsNotNone(self.denial(blocked))
        fixture = self.call("Write", {"file_path": str(self.root / "tests" / "case.py"),
                                      "content": 'assert blocks("內部代號DDD")'})
        self.assertIsNone(self.denial(fixture))

    def test_a_speech_only_ruling_does_not_judge_file_content(self):
        # 2026-09-19：白話規則（管的是對 owner 丟機器名稱）擋下了一則純英文的提交訊息。
        # 同一批卡兩道閘共用，不分適用範圍的話，管說話的規則會連程式碼一起擋。
        self.write_card(
            "decision-speech-only.md",
            name="只管說話",
            description="說明",
            applies_to="speech",
            forbidden=["內部代號CCC"],
        )
        value = self.call("Write", {"file_path": str(self.root / "note.md"),
                                    "content": "commit message mentioning 內部代號CCC"})
        self.assertIsNone(self.denial(value))

    def test_an_expired_guard_card_stops_guarding(self):
        self.write_card(
            "scar-expired.md",
            name="過期的守衛",
            description="時限型的守衛要自己停下來",
            guard_tool="Bash",
            guard_all_of=['"<<"', '"\\\\"'],
            valid_until="2026-09-18",
        )
        self.assertIsNone(self.denial(self.call("Bash", {"command": HEREDOC_WITH_BACKSLASH})))

    def test_a_guard_inside_its_window_still_guards(self):
        self.write_card(
            "scar-live.md",
            name="期限內的守衛",
            description="還沒到期",
            guard_tool="Bash",
            guard_all_of=['"<<"', '"\\\\"'],
            valid_until="2099-01-01",
        )
        self.assertIsNotNone(self.denial(self.call("Bash", {"command": HEREDOC_WITH_BACKSLASH})))

    # ---- 必填欄位型的守衛：擋的是「這次呼叫少了什麼」，字面比對看不到不存在的欄位 ----

    def dispatch_card(self, **overrides):
        fields = {
            "name": "派工要指名模型",
            "description": "派工不指名模型就沿用主線的貴模型去做機械工作",
            "guard_tool": "Task",
            "guard_requires": ["model"],
            "guard_unless": ["subagent_type=fork"],
            "guard_advice": "機械工作指定便宜的、判斷工作指定強的，不要留空",
        }
        fields.update(overrides)
        self.write_card("scar-dispatch.md", **fields)

    def test_a_call_missing_the_required_field_is_denied_and_names_the_field(self):
        self.dispatch_card()
        reason = self.denial(self.call("Task", {"prompt": "去查一個檔", "subagent_type": "scout"}))
        self.assertIsNotNone(reason)
        self.assertIn("model", reason)
        self.assertIn("派工要指名模型", reason)
        self.assertIn("不要留空", reason)

    def test_the_same_call_carrying_the_field_passes(self):
        self.dispatch_card()
        self.assertIsNone(self.denial(
            self.call("Task", {"prompt": "去查一個檔", "subagent_type": "scout", "model": "haiku"})
        ))

    def test_a_blank_field_counts_as_missing(self):
        # 空字串跟沒有這個欄位是同一件事：宿主一樣會去繼承主線的模型。
        self.dispatch_card()
        self.assertIsNotNone(self.denial(
            self.call("Task", {"prompt": "x", "model": "   "})
        ))

    def test_the_declared_exemption_lets_the_host_s_own_exception_through(self):
        # fork 型子代理的 model 是宿主明文忽略的；沒有逃生口，這種呼叫會被永久擋住，
        # 而且照擋下來的訊息去改也過不了——改不過去的閘會被繞過，不會被遵守。
        self.dispatch_card()
        self.assertIsNone(self.denial(
            self.call("Task", {"prompt": "x", "subagent_type": "fork"})
        ))

    def test_the_field_requirement_only_applies_to_its_own_tool(self):
        self.dispatch_card()
        self.assertIsNone(self.denial(self.call("Bash", {"command": "git status"})))

    def test_fragments_and_required_fields_declared_together_must_both_hold(self):
        self.dispatch_card(guard_all_of=['"deploy"'])
        self.assertIsNone(self.denial(self.call("Task", {"prompt": "查個檔"})))
        self.assertIsNotNone(self.denial(self.call("Task", {"prompt": "deploy 這包"})))

    def test_a_malformed_exemption_disarms_the_whole_card(self):
        # 反方向（忽略壞掉的那一條）會讓守衛擋得比作者寫的更多，而擋過頭沒有人會來報案。
        defects = []
        self.dispatch_card(guard_unless=["subagent_type"])
        self.assertIsNone(self.denial(self.call("Task", {"prompt": "x"}, defects)))
        self.assertTrue(any(memspec.ACTION_GUARD_UNLESS_FIELD in line for line in defects), defects)

    def test_more_required_fields_than_the_cap_disarms_the_card(self):
        defects = []
        self.dispatch_card(guard_requires=["a", "b", "c", "d", "e"])
        self.assertIsNone(self.denial(self.call("Task", {"prompt": "x"}, defects)))
        self.assertTrue(any(memspec.ACTION_GUARD_REQUIRES_FIELD in line for line in defects), defects)

    def test_a_field_requirement_counts_as_armed_but_is_not_replayed_as_a_hit(self):
        # 夜間重放手上只有呼叫的字串，看不到「少了哪個欄位」。空片段序列的 all() 是真，
        # 所以少寫一個「有片段才比對」的條件，就會把每一次同類呼叫都算成命中。
        from epitype import compliance

        self.dispatch_card()
        rules = compliance.armed_rules(self.vault)
        self.assertTrue(any(rule.card == "派工要指名模型" for rule in rules), rules)
        transcript = self.root / "dispatch.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant", "sessionId": "replay", "timestamp": "2026-09-19T01:00:00+00:00",
            "message": {"content": [{"type": "tool_use", "name": "Task",
                                     "input": {"prompt": "去查一個檔", "subagent_type": "scout"}}]},
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        hits = compliance.replay([rule._replace(mtime=0) for rule in rules], [transcript])
        self.assertEqual([hit for hit in hits if hit.kind == "guard"], [])


class ReadWaste(unittest.TestCase):
    """省 token：重複讀同一份沒變的內容、整檔拉大檔，在動手那一刻就看得出來。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-waste-")
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
        self.target = self.root / "note.md"
        self.target.write_text("內容", encoding="utf-8")

    def read(self, **extra):
        payload = {"file_path": str(self.target)}
        payload.update(extra)
        return pretool._handle(
            {"tool_name": "Read", "tool_input": payload, "session_id": "waste-test"},
            time.monotonic(), [],
        )

    def denial(self, value):
        return (value or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason")

    def test_the_first_read_passes_and_the_second_only_gets_a_note(self):
        # 壓縮之後重讀一次是正當的——那時模型手上真的沒有那份內容了。
        self.assertIsNone(self.denial(self.read()))
        second = self.read()
        self.assertIsNone(self.denial(second))

    def test_the_third_identical_read_is_denied(self):
        for _ in range(2):
            self.read()
        reason = self.denial(self.read())
        self.assertIsNotNone(reason)
        self.assertIn("省 token", reason)

    def test_a_changed_file_is_a_different_read(self):
        for _ in range(3):
            self.read()
        self.target.write_text("改過了", encoding="utf-8")
        os.utime(self.target, (time.time() + 5, time.time() + 5))
        self.assertIsNone(self.denial(self.read()))

    def test_a_different_slice_is_a_different_read(self):
        for _ in range(3):
            self.read()
        self.assertIsNone(self.denial(self.read(offset=200, limit=50)))

    def test_a_large_file_read_whole_is_denied_but_a_slice_passes(self):
        big = self.root / "big.md"
        big.write_text("x" * (memspec.READ_WASTE_BIG_FILE_BYTES + 10), encoding="utf-8")
        whole = pretool._handle(
            {"tool_name": "Read", "tool_input": {"file_path": str(big)}, "session_id": "waste-big"},
            time.monotonic(), [])
        self.assertIsNotNone(self.denial(whole))
        sliced = pretool._handle(
            {"tool_name": "Read", "tool_input": {"file_path": str(big), "offset": 1, "limit": 40},
             "session_id": "waste-big"},
            time.monotonic(), [])
        self.assertIsNone(self.denial(sliced))

    def test_a_page_range_counts_as_a_bounded_read(self):
        # 2026-09-19：這道閘上線半小時就誤擋了一次帶頁碼範圍的 PDF 讀取。
        big = self.root / "big.pdf"
        big.write_text("x" * (memspec.READ_WASTE_BIG_FILE_BYTES + 10), encoding="utf-8")
        value = pretool._handle(
            {"tool_name": "Read", "tool_input": {"file_path": str(big), "pages": "1-6"},
             "session_id": "waste-pdf"},
            time.monotonic(), [])
        self.assertIsNone(self.denial(value))

    def test_other_tools_are_untouched(self):
        value = pretool._handle(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}, "session_id": "waste-test"},
            time.monotonic(), [])
        self.assertIsNone(self.denial(value))


class RepeatGuardNotice(unittest.TestCase):
    """擋得對但一直擋，代表這道守衛在我伸手之前沒有抵達。開場先說最近一直擋人的那幾張。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-repeat-")
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
        (self.vault / "scar-heredoc.md").write_text(
            card(name="重複的坑", description="說明", guard_tool="Bash",
                 guard_all_of=['"<<"', '"\\\\"'], guard_advice="改用寫檔工具"),
            encoding="utf-8")

    def log(self, rows):
        import json as _json

        (self.vault / memspec.GATE_LOG_FILENAME).write_text(
            "\n".join(_json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8")

    @staticmethod
    def entry(card_name, hours_ago):
        from datetime import datetime, timedelta, timezone

        when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return {"timestamp": when.isoformat(), "kind": memspec.ACTION_GUARD_LOG_KIND,
                "rule": memspec.ACTION_GUARD_RULE, "card": card_name, "tool": "Bash"}

    def notices(self):
        import sessionstart_hook

        return sessionstart_hook._repeat_guard_notices([self.vault], time.monotonic())

    def test_a_guard_blocking_all_day_is_announced_with_its_advice(self):
        self.log([self.entry("重複的坑", 1) for _ in range(memspec.GUARD_REPEAT_NOTICE_THRESHOLD)])
        lines = self.notices()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("重複的坑", lines[0])
        self.assertIn("改用寫檔工具", lines[0])

    def test_below_the_threshold_says_nothing(self):
        self.log([self.entry("重複的坑", 1)
                  for _ in range(memspec.GUARD_REPEAT_NOTICE_THRESHOLD - 1)])
        self.assertEqual(self.notices(), [])

    def test_yesterday_s_blocks_do_not_keep_the_line_alive(self):
        # 次數掉下來這一行要自己消失，不然它就是另一條沒人拔的常駐規則。
        self.log([self.entry("重複的坑", 40)
                  for _ in range(memspec.GUARD_REPEAT_NOTICE_THRESHOLD * 3)])
        self.assertEqual(self.notices(), [])

    def test_at_most_two_cards_are_announced(self):
        rows = []
        for name in ("坑一", "坑二", "坑三"):
            rows.extend(self.entry(name, 1) for _ in range(memspec.GUARD_REPEAT_NOTICE_THRESHOLD))
        self.log(rows)
        self.assertLessEqual(len(self.notices()), memspec.GUARD_REPEAT_NOTICE_MAX_CARDS)


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

    def block_turn(self, rows, final):
        import stop_gate

        markers = patch.object(stop_gate, "recall_marker_directory",
                               lambda session: self.root / "markers" / str(session))
        markers.start()
        self.addCleanup(markers.stop)
        transcript = self.root / f"turn-{len(rows)}-{abs(hash(final))}.jsonl"
        transcript.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
        return stop_gate._handle(
            {"session_id": "turn-" + transcript.stem, "stop_hook_active": False,
             "last_assistant_message": final, "transcript_path": str(transcript)},
            time.monotonic(), [],
        )

    @staticmethod
    def said(text, tool=False):
        content = [{"type": "text", "text": text}]
        if tool:
            content.append({"type": "tool_use", "name": "Bash", "input": {"command": "ls"}})
        return {"type": "assistant", "message": {"content": content}}

    PROMPT = {"type": "user", "message": {"content": "請處理"}}
    TOOL_RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}

    def test_a_claim_made_mid_turn_is_checked_not_only_the_last_message(self):
        # 2026-09-17: the false claim sat before a tool call; the gate read only the
        # turn's last message, so no card could ever reach it.
        rows = [self.PROMPT, self.said("全部通過了", tool=True), self.TOOL_RESULT, self.said("接著看下一步")]
        value = self.block_turn(rows, "接著看下一步")
        self.assertEqual((value or {}).get("decision"), "block")
        self.assertIsNone(self.block("接著看下一步"))  # 只看最後一則的話，這個回合永遠過

    def test_evidence_anywhere_in_the_turn_satisfies_the_requirement(self):
        rows = [self.PROMPT, self.said("跑了全套測試", tool=True), self.TOOL_RESULT, self.said("全部通過了")]
        self.assertIsNone(self.block_turn(rows, "全部通過了"))

    def test_an_earlier_turn_does_not_count(self):
        rows = [self.PROMPT, self.said("全部通過了"), {"type": "user", "message": {"content": "新問題"}},
                self.said("好")]
        self.assertIsNone(self.block_turn(rows, "好"))

    def test_the_gate_and_the_nightly_replay_record_the_same_turn_digest(self):
        from epitype import compliance

        rows = [self.PROMPT, self.said("全部通過了", tool=True), self.TOOL_RESULT, self.said("接著看下一步")]
        for row in rows:
            row["sessionId"], row["timestamp"] = "digest-session", "2026-09-17T01:00:00+00:00"
        self.assertEqual((self.block_turn(rows, "接著看下一步") or {}).get("decision"), "block")
        transcript = sorted(self.root.glob("turn-*.jsonl"))[-1]
        logged = [json.loads(line) for line in (self.vault / memspec.GATE_LOG_FILENAME).read_text(
            encoding="utf-8").splitlines() if line.strip()]
        gate_digests = {row.get("digest") for row in logged if row.get("digest")}
        rules = [rule._replace(mtime=0) for rule in compliance.armed_rules(self.vault)]
        replayed = {hit.digest for hit in compliance.replay(rules, [transcript]) if hit.kind == "require"}
        self.assertTrue(replayed and replayed <= gate_digests, (replayed, gate_digests))

    def test_an_unreadable_transcript_falls_back_to_the_last_message(self):
        import stop_gate

        markers = patch.object(stop_gate, "recall_marker_directory",
                               lambda session: self.root / "markers" / str(session))
        markers.start()
        self.addCleanup(markers.stop)
        value = stop_gate._handle(
            {"session_id": "turn-missing", "stop_hook_active": False, "last_assistant_message": "全部通過了",
             "transcript_path": str(self.root / "no-such.jsonl")},
            time.monotonic(), [],
        )
        self.assertEqual((value or {}).get("decision"), "block")

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

    def test_a_pattern_that_cannot_compile_still_matches_itself_literally(self):
        # Self-repair: the author wrote a phrase that happens to contain a bracket.
        # Matching it literally is what they meant, and can only match less.
        (self.vault / "feedback-literal.md").write_text(
            "---\nname: 逐字比對\ndescription: 說明\n"
            "forbidden:\n  - 階段一)\n---\nbody\n",
            encoding="utf-8",
        )
        value = self.block("這批是階段一)，先這樣。")
        self.assertIsNotNone(value)
        self.assertIn("逐字比對", value.get("reason", ""))

    def test_a_truncated_pattern_is_not_guessed_at(self):
        # Half a pattern is half an intention. Completing it would be choosing the
        # rule's content for the author, so the literal fallback simply matches the
        # text as written -- which here is nothing.
        (self.vault / "feedback-truncated.md").write_text(
            "---\nname: 截斷樣式\ndescription: 說明\n"
            "forbidden:\n  - (甲案|乙案\n---\nbody\n",
            encoding="utf-8",
        )
        self.assertIsNone(self.block("這次走甲案，不走乙案。"))

    def test_a_retired_behaviour_card_stops_speaking(self):
        (self.vault / "feedback-retired.md").write_text(
            "---\nname: 已退役\ndescription: 說明\nstatus: superseded\n"
            "forbidden:\n  - 退役禁語\n---\nbody\n",
            encoding="utf-8",
        )
        self.assertIsNone(self.block("這裡出現退役禁語。"))

    def test_an_expired_card_stops_speaking_although_the_file_never_changed(self):
        # 時限型的規則（試行一週、某日之前先不要做）必須自己停下來。體檢早就把過期的卡
        # 標成「讀取端應視為失效」，閘卻照樣擋——同一張卡兩種身分。到期也必須在用的時候
        # 判，因為卡片不動也會過期，而解析結果是進快取的。
        (self.vault / "feedback-trial.md").write_text(
            "---\nname: 一週試行\ndescription: 說明\nvalid_until: 2026-09-18\n"
            "forbidden:\n  - 試行期禁語\n---\nbody\n",
            encoding="utf-8",
        )
        self.assertIsNone(self.block("這裡出現試行期禁語。"))

    def test_a_card_still_inside_its_window_keeps_blocking(self):
        (self.vault / "feedback-live-trial.md").write_text(
            "---\nname: 還在期限內\ndescription: 說明\nvalid_until: 2099-01-01\n"
            "forbidden:\n  - 期限內禁語\n---\nbody\n",
            encoding="utf-8",
        )
        self.assertIsNotNone(self.block("這裡出現期限內禁語。"))

    def test_a_broken_expiry_date_does_not_quietly_switch_a_rule_off(self):
        # 寫錯的日期不該把規則關掉：那種錯由體檢喊，不是由閘默默放行。
        (self.vault / "feedback-bad-date.md").write_text(
            "---\nname: 日期寫壞\ndescription: 說明\nvalid_until: 明天\n"
            "forbidden:\n  - 壞日期禁語\n---\nbody\n",
            encoding="utf-8",
        )
        self.assertIsNotNone(self.block("這裡出現壞日期禁語。"))

    def test_evidence_inside_a_quotation_still_counts_as_evidence(self):
        # 2026-09-19：「owner 原話要附引號原文」的證據就是引號本身。證據也走引號外比對
        # 的話，一則有三處引號的正常回報會因為遮罩把每個「…」連括號一起遮掉而被擋，
        # 而同一句單獨送反而過（引用佔比超過上限、遮罩整則失效）。
        (self.vault / "decision-verbatim.md").write_text(
            "---\nname: 原話要附引文\ndescription: 說明\n"
            "require_when: (owner|你)(的)?(原話|逐字)\nrequire_text: 「\n---\nbody\n",
            encoding="utf-8",
        )
        long_report = (
            "設好了。偏好「新開的 session 自動接上遠端遙控」已改成 On。"
            "這條定規寫成記憶卡，附你的原話「設定規則，session預設可以遠端遙控」。"
            "要改回去在「設定」關掉即可。本回合讀了：偏好設定兩次。"
        )
        self.assertIsNone(self.block(long_report))
        self.assertIsNotNone(self.block("這是你的原話，我照做了。"))

    def test_a_speech_only_ruling_still_blocks_what_i_say(self):
        (self.vault / "decision-speech.md").write_text(
            "---\nname: 只管說話\ndescription: 說明\napplies_to: speech\n"
            "forbidden:\n  - 內部代號AAA\n---\nbody\n",
            encoding="utf-8",
        )
        self.assertIsNotNone(self.block("這裡出現內部代號AAA。"))

    def test_a_write_only_ruling_does_not_judge_what_i_say(self):
        # 管寫檔內容的裁定講的是檔案裡不該有什麼，不是我不該說什麼。
        (self.vault / "decision-write.md").write_text(
            "---\nname: 只管寫檔\ndescription: 說明\napplies_to: write\n"
            "forbidden:\n  - 內部代號BBB\n---\nbody\n",
            encoding="utf-8",
        )
        self.assertIsNone(self.block("這裡出現內部代號BBB。"))

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


class FieldCombination(unittest.TestCase):
    """欄位帶了、但帶的組合本身就是錯的——派工給唯讀偵察兵卻指名最貴的模型。

    2026-09-19 實測近三天 294 次派工，scout 配 opus 有 47 次（16%）。字面片段做不到這
    件事：型別名與模型名都可能只是出現在派工單正文裡，比對字串會擋到只是提到的呼叫。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-when-")
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
        self.mismatch_card()

    def mismatch_card(self, **overrides):
        fields = {
            "name": "偵察兵不要配貴模型",
            "description": "唯讀偵察派給最貴的模型是白花錢",
            "guard_tool": "Task",
            "guard_when": ["subagent_type=scout|Explore", "model=opus|claude-opus-5"],
            "guard_advice": "偵察改指 haiku 或 sonnet",
        }
        fields.update(overrides)
        (self.vault / "scar-mismatch.md").write_text(card(**fields), encoding="utf-8")
        cache = pretool._guard_cache(self.vault)
        if cache.exists():
            cache.unlink()

    def call(self, tool_input, defects=None):
        return pretool._handle(
            {"tool_name": "Task", "tool_input": tool_input, "session_id": "when-test"},
            time.monotonic(),
            [] if defects is None else defects,
        )

    def denial(self, value):
        return (value or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason")

    def test_the_bad_pair_is_denied_and_the_reason_names_both_fields(self):
        reason = self.denial(self.call(
            {"subagent_type": "scout", "model": "opus", "prompt": "去找那個檔在哪"}))
        self.assertIsNotNone(reason)
        self.assertIn("subagent_type=scout", reason)
        self.assertIn("model=opus", reason)
        self.assertIn("haiku", reason)

    def test_an_alternative_spelling_on_either_side_still_hits(self):
        for kind, model in (("Explore", "opus"), ("scout", "claude-opus-5"),
                            ("EXPLORE", "OPUS")):
            self.assertIsNotNone(
                self.denial(self.call({"subagent_type": kind, "model": model})),
                msg=f"{kind}+{model}")

    def test_the_right_pairing_passes(self):
        for kind, model in (("scout", "haiku"), ("scout", "sonnet"),
                            ("coder", "opus"), ("auditor", "opus"),
                            ("general-purpose", "opus")):
            self.assertIsNone(
                self.denial(self.call({"subagent_type": kind, "model": model})),
                msg=f"{kind}+{model}")

    def test_one_condition_alone_is_not_a_hit(self):
        # 條件要全部成立才算命中。半數成立就擋的話，這張卡等於禁掉整個 opus 或整個 scout。
        self.assertIsNone(self.denial(self.call({"subagent_type": "scout"})))
        self.assertIsNone(self.denial(self.call({"model": "opus"})))
        self.assertIsNone(self.denial(self.call({})))

    def test_the_model_name_inside_the_prompt_text_is_not_a_field_value(self):
        # 字面比對法在這裡會誤擋：派工單正文提到 opus 不等於這次派給 opus。
        self.assertIsNone(self.denial(self.call({
            "subagent_type": "scout", "model": "haiku",
            "prompt": "去查一下哪幾次派工用了 opus"})))

    def test_it_only_judges_its_own_tool(self):
        self.assertIsNone(self.denial(pretool._handle(
            {"tool_name": "Bash", "tool_input": {"subagent_type": "scout", "model": "opus"},
             "session_id": "when-test"}, time.monotonic(), [])))

    def test_a_malformed_condition_disarms_the_card_and_says_so(self):
        # 條件寫壞就整張卡不生效，而且要出聲。忽略壞掉的那一條會讓守衛擋得比作者寫的更多。
        self.mismatch_card(guard_when=["subagent_type", "model=opus"])
        defects = []
        self.assertIsNone(self.denial(self.call(
            {"subagent_type": "scout", "model": "opus"}, defects)))
        self.assertTrue(defects)
        self.assertIn("guard_when", defects[0])

    def test_too_many_conditions_disarms_the_card_and_says_so(self):
        self.mismatch_card(guard_when=[f"field{index}=value" for index in range(
            memspec.ACTION_GUARD_MAX_WHEN + 1)])
        defects = []
        self.assertIsNone(self.denial(self.call({"field0": "value"}, defects)))
        self.assertTrue(defects)

    def test_the_escape_hatch_is_to_match_the_type_to_the_work(self):
        # 沒有魔法字可以繞過：真的需要貴模型，就派一個名副其實的型別。一道改不過去的閘
        # 會被繞過，所以逃生口必須存在——這裡的逃生口是換型別，而不是加一句咒語。
        self.assertIsNone(self.denial(self.call(
            {"subagent_type": "general-purpose", "model": "opus",
             "prompt": "這件事需要判斷，不只是找檔"})))

    def test_an_expired_field_guard_stops_blocking(self):
        # 欄位型守衛的到期日以前被靜靜忽略：卡片寫了期限、閘永遠不會停（2026-09-19 修）。
        self.mismatch_card(valid_until="2020-01-01")
        self.assertIsNone(self.denial(self.call(
            {"subagent_type": "scout", "model": "opus"})))

    def test_a_when_only_card_counts_as_armed(self):
        # 夜間報表若不認這種武裝，會叫人去「補武裝」一張已經在擋的卡。
        rules = compliance.armed_rules(self.vault)
        self.assertTrue([rule for rule in rules if rule.kind == "guard"])


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

    def test_a_guard_with_required_fields_and_no_fragments_passes_the_lint(self):
        # 2026-09-19：只認字面片段的檢查會把照規範寫的必填欄位型守衛判成不合格——
        # 那就是同一天修掉的「兩套規範互斥」再來一次。
        from epitype import card_lint

        rules, findings = self.findings(
            "scar-dispatch.md",
            "---\nname: 派工要指名模型\ndescription: 2026-09-19 派工不指名模型就繼承貴模型\n"
            "guard_tool: Task\nguard_requires:\n  - model\n"
            "guard_unless:\n  - subagent_type=fork\n---\nbody\n",
        )
        self.assertNotIn((card_lint.FAIL, "guard"), rules, findings)

    def test_a_pattern_that_cannot_compile_fails_the_lint(self):
        # A card whose pattern will not compile looks perfect and enforces nothing:
        # the gate drops it silently. Until 2026-09-17 the lint passed it too, so
        # nothing anywhere said the rule was dead.
        rules, findings = self.findings(
            "decision-bad-regex.md",
            "---\nname: 壞樣式\ndescription: 2026-09-17 括號沒關\ndecision_key: bad\n"
            "status: active\ncurrent_decision_at: 2026-09-17\ndecided_by: owner-explicit\n"
            "owner_quote: x\naliases: [甲名, 乙名]\nforbidden:\n  - a(b\n---\nbody\n",
        )
        self.assertIn(("WARN", "pattern"), rules)
        reason = next(r for lvl, rule, r in findings if rule == "pattern")
        self.assertIn("逐字比對", reason)

    def test_an_over_long_pattern_fails_the_lint(self):
        rules, _ = self.findings(
            "decision-long.md",
            "---\nname: 過長\ndescription: 2026-09-17 樣式超長\ndecision_key: long\n"
            "status: active\ncurrent_decision_at: 2026-09-17\ndecided_by: owner-explicit\n"
            "owner_quote: x\naliases: [丙名, 丁名]\nforbidden:\n  - " + ("x" * 1100)
            + "\n---\nbody\n",
        )
        self.assertIn(("WARN", "pattern"), rules)

    def test_an_unknown_guard_tool_is_reported(self):
        rules, _ = self.findings(
            "scar-bad-tool.md",
            "---\nname: 壞工具名\ndescription: 2026-09-17 工具不存在\n"
            'guard_tool: Shellzz\nguard_all_of:\n  - "aa"\n  - "bb"\n'
            "aliases: [壞工具]\n---\nbody\n",
        )
        self.assertIn(("WARN", "guard-tool"), rules)

    def test_a_usable_pattern_raises_no_pattern_finding(self):
        _rules, findings = self.findings(
            "decision-fine.md",
            "---\nname: 好卡\ndescription: 2026-09-17 正常\ndecision_key: fine\n"
            "status: active\ncurrent_decision_at: 2026-09-17\ndecided_by: owner-explicit\n"
            "owner_quote: x\naliases: [戊名, 己名]\nforbidden:\n  - (先不動|等看看)\n---\nbody\n",
        )
        self.assertEqual([f for f in findings if f[1] in ("pattern", "guard-tool")], [])

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
