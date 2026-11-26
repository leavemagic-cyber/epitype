import sys; sys.dont_write_bytecode = True
"""Dream hands the lead one short judgment list, fixes rule-only defects itself,
and does not measure Codex's own generated memory against our hand-index cap."""
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import dream, memspec

TODAY = date(2026, 11, 13)
BODY_DATED = (
    "---\nname: body-dated\ndescription: 欄位無日期、正文有\naliases:\n  - 正文日期\n"
    "metadata:\n  type: reference\n---\n2026-08-15 那天的紀錄\n"
)


class DreamReviewRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-dream-review-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    # ------------------------------------------------------------- short list

    def report(self, fails=0, alias_vaults=None, over=False):
        sections = [
            {"id": 1, "title": "缺別名卡", "error": None,
             "counts": {"by_vault": alias_vaults or {}}},
            {"id": 2, "title": "卡片型別檢查", "error": None, "counts": {"fixed_dates": 3},
             "fail_list": [{"vault": "V", "path": f"card-{n}.md", "fail": 1, "rule": "status",
                            "reason": "status=retired 不在 active|superseded\n第二行"} for n in range(fails)]},
        ]
        if over:
            sections.append({"id": 11, "title": "上限檢查", "error": None, "examples": [{
                "path": "V/MEMORY.md", "bytes": 2000, "cap": 1526, "over": 474, "reduce_bytes": 474,
                "kind": "index-file", "items": [{"bytes": 30, "path": "V/MEMORY.md", "line": 3, "text": "長條目"}],
            }]})
        sections.append({"id": 4, "title": "草稿待審", "error": None,
                         "counts": {"total_drafts": 300, "over_7_days": 250}, "commands": []})
        return {"today": TODAY.isoformat(), "since": "2026-08-15", "sections": sections}

    def test_list_is_capped_per_group_and_in_total_and_keeps_true_totals(self):
        aliases = {f"vault-{n}": n + 1 for n in range(30)}
        report = self.report(fails=12, alias_vaults=aliases, over=True)
        review = dream.review_list(report)
        groups = {group["group"]: group for group in review["groups"]}
        group_max = memspec.DREAM_REVIEW_GROUP_MAX_ITEMS
        self.assertEqual(groups["卡片型別 FAIL"], {"group": "卡片型別 FAIL", "total": 12, "listed": group_max})
        self.assertEqual(len(review["items"]), group_max + 1 + group_max)
        self.assertEqual(review["total"], 12 + 1 + 30)
        # 總上限：組上限放寬時，後面的組只拿得到剩下的名額。
        wide = dream.review_list(report, cap=20, group_cap=10)
        self.assertEqual(len(wide["items"]), 20)
        self.assertEqual([group["listed"] for group in wide["groups"]], [10, 1, 9])
        # 每項一行：換行被攤平，路徑、理由、建議三欄都在。
        first = review["items"][0]
        self.assertEqual(first["path"], os.fspath(Path("V") / "card-0.md"))
        self.assertNotIn("\n", first["reason"])
        self.assertTrue(first["action"])
        # 缺別名依張數多的庫先列。
        alias_items = [item for item in review["items"] if item["group"].startswith("缺別名")]
        self.assertEqual(alias_items[0]["path"], "vault-29")

    def test_drafts_never_become_a_judgment_item(self):
        review = dream.review_list(self.report())
        self.assertEqual(review, {"items": [], "groups": [], "total": 0})
        rendered = dream._render_review(self.report(), review)
        self.assertIn(dream.REVIEW_EMPTY_LINE, rendered)
        self.assertIn(dream.REVIEW_FIXED_LINE.format(count=3), rendered)
        self.assertNotIn("草稿", rendered)

    def test_rendered_list_carries_cap_material_only_when_over(self):
        report = self.report(fails=1, over=True)
        rendered = dream._render_review(report, dream.review_list(report))
        self.assertIn(dream.REVIEW_CAP_HEADING, rendered)
        self.assertIn("索引條目依長度降冪排列", rendered)
        self.assertLess(len(rendered.encode("utf-8")), 4000)
        plain = self.report(fails=1)
        self.assertNotIn(dream.REVIEW_CAP_HEADING, dream._render_review(plain, dream.review_list(plain)))

    def test_section_errors_lead_the_list_without_asking_for_a_manual_full_run(self):
        report = self.report(fails=1)
        report["sections"].append({"id": 14, "title": "宿主檔同步", "error": None,
                                   "errors": ["REFUSE AGENTS.md: marker broken"]})
        review = dream.review_list(report)
        self.assertEqual(review["items"][0]["group"], "夢這一晚沒跑完或出錯")
        self.assertIn("epitype sync", review["items"][0]["action"])
        self.assertNotIn("dream.py", json.dumps(review, ensure_ascii=False))

    def test_group_totals_come_from_section_counts_not_truncated_examples(self):
        report = self.report()
        report["sections"].append({
            "id": 5, "title": "決策卡", "error": None,
            "counts": {"active": 20, "superseded": 0, "missing_owner_or_forbidden": 12},
            "examples": [{"vault": "V", "path": f"d{n}.md", "missing_forbidden": True}
                         for n in range(dream.EXAMPLE_LIMIT)]})
        report["sections"].append({
            "id": 3, "title": "待辦", "error": None,
            "counts": {"zombie_cards": 11, "zombie_lines": 30, "oldest_days": 90,
                       "orphan_cards": 2, "orphan_lines": 4},
            "examples": [{"vault": "V", "path": f"p{n}.md", "oldest_days": 90}
                         for n in range(dream.EXAMPLE_LIMIT)],
            "orphans": [{"vault": "V", "path": "o.md", "lines": 2}, {"vault": "V", "path": "q.md", "lines": 2}]})
        review = dream.review_list(report)
        totals = {group["group"]: group["total"] for group in review["groups"]}
        self.assertEqual(totals["決策卡缺 owner_quote／forbidden"], 12)
        self.assertEqual(totals["殭屍／孤兒待辦"], 13)
        self.assertEqual(review["total"], 25)

    def test_checks_outside_sections_that_failed_are_listed(self):
        # 近重複與原話索引出錯一樣讓這一晚不算查完，清單不能說沒有要判斷的。
        report = self.report()
        report["duplicates"] = {"error": "PermissionError: 合成"}
        report["owner_index"] = {"error": "DatabaseError: 合成", "errors": [],
                                 "unreadable_rows": [{"source": "s", "byte_offset": 0}], "skipped_zst": 0}
        self.assertEqual(set(dream._report_errors({**report, "sections": []})) & {"duplicates", "owner_index"},
                         {"duplicates", "owner_index"})
        review = dream.review_list(report)
        failed = [item for item in review["items"] if item["group"] == "夢這一晚沒跑完或出錯"]
        self.assertEqual([item["reason"] for item in failed],
                         ["PermissionError: 合成", "DatabaseError: 合成", "1 列讀不出來"])

    # ------------------------------------------------------- mechanical fixes

    def test_section_two_stamps_derived_dates_only_when_applying(self):
        vault = self.root / "vault"
        vault.mkdir()
        card = vault / "body-dated.md"
        card.write_bytes(BODY_DATED.encode("utf-8"))
        dry = dream._section_card_lint([vault], TODAY, None, {}, {"apply_fixes": False})
        self.assertEqual((dry["counts"]["fixable_dates"], dry["counts"]["fixed_dates"]), (1, 0))
        self.assertEqual(card.read_bytes(), BODY_DATED.encode("utf-8"))
        applied = dream._section_card_lint([vault], TODAY, None, {}, {"apply_fixes": True})
        self.assertEqual(applied["counts"]["fixed_dates"], 1)
        self.assertEqual(applied["counts"]["warn"], dry["counts"]["warn"] - 1)
        text = card.read_text(encoding="utf-8")
        self.assertIn(f"{memspec.LAST_VERIFIED_AT_FIELD}: 2026-08-15\n---\n", text)
        self.assertEqual(text.replace(f"{memspec.LAST_VERIFIED_AT_FIELD}: 2026-08-15\n", ""), BODY_DATED)
        again = dream._section_card_lint([vault], TODAY, None, {}, {"apply_fixes": True})
        self.assertEqual((again["counts"]["fixable_dates"], again["counts"]["fixed_dates"]), (0, 0))

    def test_section_two_leaves_source_bound_cards_alone_and_lists_them(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from rule_source_fixtures import owner_bound_card
        from epitype import rule_pipeline

        vault = self.root / "vault"
        vault.mkdir()
        card = vault / "feedback_guard.md"
        text = ("---\nname: 合成守衛\ndescription: 不准合成危險指令\naliases:\n  - 合成守衛\n"
                "guard_tool: Bash\nguard_all_of:\n  - 合成危險指令\n"
                "metadata:\n  type: feedback\n---\n2026-08-15 合成紀錄\n")
        card.write_text(owner_bound_card(text, card, source_root=self.root / "src", case_id="dream-date"),
                        encoding="utf-8", newline="\n")
        before = card.read_bytes()
        result = dream._section_card_lint([vault], TODAY, None, {}, {"apply_fixes": True})
        self.assertEqual(card.read_bytes(), before)
        self.assertEqual((result["counts"]["fixed_dates"], result["counts"]["bound_dates"]), (0, 1))
        current = card.read_text(encoding="utf-8")
        fields, _problem = memspec.frontmatter_text(current)
        self.assertTrue(rule_pipeline.inspect_card(fields, card, card_text=current).verified)
        review = dream.review_list({"today": TODAY.isoformat(), "sections": [{"id": 2, **result}]})
        self.assertEqual([(item["group"], item["path"]) for item in review["items"]],
                         [("來源綁定卡沒有自動補日期", os.fspath(vault / "feedback_guard.md"))])

    def test_section_two_leaves_legacy_pinned_cards_alone(self):
        # 2026-11-13 real run: a date line turned 16 legacy-pinned cards (no
        # rule_provenance, frozen-hash identity) from UNVERIFIED to INVALID.
        from types import SimpleNamespace
        from unittest import mock
        from epitype import rule_pipeline

        vault = self.root / "vault"
        vault.mkdir()
        card = vault / "body-dated.md"
        card.write_bytes(BODY_DATED.encode("utf-8"))
        before = card.read_bytes()

        def pinned(_fields, _path, *, card_text=None):
            changed = memspec.LAST_VERIFIED_AT_FIELD in (card_text or "")
            return SimpleNamespace(status="INVALID" if changed else "UNVERIFIED")

        with mock.patch.object(rule_pipeline, "legacy_integrity", pinned):
            result = dream._section_card_lint([vault], TODAY, None, {}, {"apply_fixes": True})
        self.assertEqual(card.read_bytes(), before)
        self.assertEqual((result["counts"]["fixed_dates"], result["counts"]["bound_dates"]), (0, 1))

        # Cards outside any legacy pin (status unchanged) still get their date.
        result = dream._section_card_lint([vault], TODAY, None, {}, {"apply_fixes": True})
        self.assertEqual(result["counts"]["fixed_dates"], 1)
        self.assertIn(memspec.LAST_VERIFIED_AT_FIELD, card.read_text(encoding="utf-8"))

    def test_retired_cards_skip_the_date_freshness_check(self):
        # 2026-11-13：bound_dates 只剩 4 張已取代的卡（3 張繼任卡在別的庫、1 張繼任卡的
        # supersedes 是空的），重綁一定失敗；退役的卡不再生效，日期新不新沒有意義。
        from types import SimpleNamespace
        from unittest import mock
        from epitype import card_lint, rule_pipeline

        vault = self.root / "vault"
        vault.mkdir()
        cards = {}
        for status in memspec.STOP_GATE_SILENT_STATUSES:
            name = f"feedback_{status}.md"
            cards[name] = BODY_DATED.replace(
                "metadata:", f"status: {status}\nsuperseded_by: ../other-vault/feedback_next.md\nmetadata:")
            (vault / name).write_bytes(cards[name].encode("utf-8"))
        undated = BODY_DATED.replace("2026-08-15 那天的紀錄", "沒有日期的紀錄").replace(
            "metadata:", "status: superseded\nmetadata:")
        (vault / "feedback_undated_retired.md").write_bytes(undated.encode("utf-8"))
        cards["feedback_undated_retired.md"] = undated
        (vault / "body-dated.md").write_bytes(BODY_DATED.encode("utf-8"))

        def pinned(_fields, _path, *, card_text=None):
            changed = memspec.LAST_VERIFIED_AT_FIELD in (card_text or "")
            return SimpleNamespace(status="INVALID" if changed else "UNVERIFIED")

        with mock.patch.object(rule_pipeline, "legacy_integrity", pinned):
            result = dream._section_card_lint([vault], TODAY, None, {}, {"apply_fixes": True})
        self.assertEqual(result["bound_dates"], [{"vault": str(vault), "path": "body-dated.md"}])
        for name, text in cards.items():
            self.assertEqual((vault / name).read_text(encoding="utf-8"), text)
        report = card_lint.scan_vault(vault, TODAY)
        rules = {card["path"]: {finding["rule"] for finding in card["findings"]} for card in report["cards"]}
        for name in cards:
            self.assertFalse(rules.get(name, set()) & {"date", "date-derived"}, name)
        # 現行的卡照舊要補日期。
        self.assertIn("date-derived", rules["body-dated.md"])

    def test_section_two_without_context_never_writes_cards(self):
        vault = self.root / "vault"
        vault.mkdir()
        card = vault / "body-dated.md"
        card.write_bytes(BODY_DATED.encode("utf-8"))
        self.assertIn(dream._section_card_lint, dream._CONTEXT_SECTIONS)
        result = dream._section_card_lint([vault], TODAY, None, {})
        self.assertEqual(result["counts"]["fixed_dates"], 0)
        self.assertEqual(card.read_bytes(), BODY_DATED.encode("utf-8"))

    # ------------------------------------------------------- Codex-native skip

    def test_codex_generated_memory_is_not_held_to_our_index_cap(self):
        codex = self.root / ".codex" / "memories"
        (codex / "rollout_summaries").mkdir(parents=True)
        (codex / "MEMORY.md").write_text("x" * 5000, encoding="utf-8")
        (codex / "rollout_summaries" / "r.md").write_text("y", encoding="utf-8")
        (codex / "hand-card.md").write_text("z", encoding="utf-8")
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "MEMORY.md").write_text("- 很長的索引條目\n" * 201, encoding="utf-8")  # 宿主只載前 200 行
        self.assertTrue(dream._codex_native_generated(codex, codex / "MEMORY.md"))
        self.assertTrue(dream._codex_native_generated(codex, codex / "rollout_summaries" / "r.md"))
        self.assertFalse(dream._codex_native_generated(codex, codex / "hand-card.md"))
        self.assertFalse(dream._codex_native_generated(plain, plain / "MEMORY.md"))
        section = dream._section_caps([codex, plain], TODAY, None, {"index_cap_bytes": 1526})
        paths = [item["path"] for item in section["examples"] if item.get("kind") == "index-file"]
        self.assertEqual(paths, [str(plain / "MEMORY.md")])

    def test_an_event_card_a_decision_carries_is_not_an_aging_candidate(self):
        # 被承接的事件卡是決策卡的原話出處，「要不要歸檔」的答案固定是保留；
        # 2026-11-13 真機每晚因此多列 23 張。沒寫承接者（或寫了空值）的照列。
        vault = self.root / "vault"
        grants = vault / "grants"
        grants.mkdir(parents=True)

        def card(name, extra=""):
            (grants / f"{name}.md").write_text(
                f"---\nname: {name}\ndescription: owner said\ncaptured_at: 2026-01-01\n{extra}---\n原話\n",
                encoding="utf-8")

        card("carried", f"{memspec.CARRIED_BY_FIELD}: some-decision\n")
        card("blank", f"{memspec.CARRIED_BY_FIELD}: \n")
        card("plain")
        section = dream._section_event_aging([vault], TODAY, date(2026, 10, 1), {})
        self.assertEqual(sorted(item["path"] for item in section["examples"]),
                         ["grants/blank.md", "grants/plain.md"])
        self.assertEqual(section["counts"]["aging_by_type"]["grant"], 2)
        self.assertEqual(section["counts"]["by_type"]["grant"], 3)

    def test_codex_generated_memory_is_not_read_as_our_cards_in_any_section(self):
        # 2026-11-13 真機：第 1 節把 ~/.codex/memories/skills/*/SKILL.md 列成缺別名的卡，
        # 第 3 節把 rollout_summaries 裡當時對話的「待辦」列成孤兒。
        skill_card = "---\nname: some-skill\ndescription: a skill definition\n---\n- ⏳ 還沒做\n"
        hand_card = ("---\nname: hand\ndescription: a hand-written card\ncaptured_at: 2026-09-05\n"
                     "---\n- ⏳ 還沒做\n")

        def lay_out(vault):
            (vault / "skills" / "some-skill").mkdir(parents=True)
            (vault / "rollout_summaries").mkdir()
            (vault / "skills" / "some-skill" / "SKILL.md").write_text(skill_card, encoding="utf-8")
            (vault / "rollout_summaries" / "r.md").write_text(skill_card, encoding="utf-8")
            (vault / "hand.md").write_text(hand_card, encoding="utf-8")

        codex = self.root / ".codex" / "memories"
        plain = self.root / "plain"
        lay_out(codex)
        lay_out(plain)
        everything = {"hand.md", "rollout_summaries/r.md", "skills/some-skill/SKILL.md"}

        def listed(rows, key):
            return {
                vault: {Path(row[key]).relative_to(vault).as_posix() if Path(row[key]).is_absolute()
                        else row[key] for row in rows if row["vault"] == str(vault)}
                for vault in (codex, plain)
            }

        aliases = dream._section_missing_aliases([codex, plain], TODAY, None, {})
        self.assertEqual(listed(aliases["examples"], "card_path"),
                         {codex: {"hand.md"}, plain: everything})
        self.assertEqual(aliases["counts"]["by_vault"], {str(codex): 1, str(plain): 3})

        pending = dream._section_pending([codex, plain], TODAY, None, {})
        self.assertEqual(listed(pending["orphans"], "path"), {codex: {"hand.md"}, plain: everything})
        self.assertEqual(pending["counts"]["orphan_cards"], 4)
        self.assertEqual(pending["counts"]["orphan_lines"], 4)

        self.assertTrue(dream._codex_native_generated(codex, codex / "skills" / "some-skill" / "SKILL.md"))
        self.assertEqual({path.relative_to(codex).as_posix() for path in dream._card_files(codex)},
                         {"hand.md"})
        self.assertEqual({path.relative_to(plain).as_posix() for path in dream._card_files(plain)},
                         everything)

    # ------------------------------------------------------------- drafts once

    def _draft_vault(self, name):
        vault = self.root / name
        auto = vault.joinpath(*memspec.CAPTURE_PENDING_SUBPATH, "20260901", "auto.md")
        auto.parent.mkdir(parents=True)
        auto.write_text(
            f"---\nname: auto\ndescription: 自動捕捉\n{memspec.PROVENANCE_FIELD}: "
            f"{memspec.DRAFT_AUTO_PROVENANCE[0]}\n{memspec.DRAFT_AGE_FIELDS[0]}: 2026-09-01\n---\n內文\n",
            encoding="utf-8")
        for relative, stamped in (("decisions/human.md", date(2026, 10, 1)), ("notes/fresh.md", TODAY)):
            path = vault / dream.DRAFT_DIRNAME / relative
            path.parent.mkdir(parents=True)
            path.write_text(f"---\nname: {path.stem}\ndescription: 人寫的草稿\n---\n內文\n", encoding="utf-8")
            moment = datetime(stamped.year, stamped.month, stamped.day, tzinfo=timezone.utc).timestamp()
            os.utime(path, (moment, moment))
        return vault, auto

    def test_drafts_are_reported_once_after_expiry_with_their_ages(self):
        from unittest import mock

        home = self.root / "home"
        home.mkdir()
        vault, auto = self._draft_vault("vault")
        config = self.root / "config.json"
        config.write_text(json.dumps({memspec.CONFIG_VAULTS_FIELD: [os.fspath(vault)]}), encoding="utf-8")
        environ = {"HOME": os.fspath(home), "USERPROFILE": os.fspath(home),
                   memspec.EPITYPE_CONFIG_ENV: os.fspath(config)}
        with mock.patch.dict(os.environ, environ):
            report = dream.build_report([vault], today=TODAY)
        carrying = [section["id"] for section in report["sections"]
                    if "total_drafts" in (section.get("counts") or {})]
        self.assertEqual(carrying, [4])
        drafts = next(section for section in report["sections"] if section["id"] == 4)
        self.assertIsNone(drafts["error"])
        counts = drafts["counts"]
        # 到期先跑：自動草稿被標過期，不再算待審；剩下兩份人寫的照年齡分桶。
        self.assertIn(memspec.DRAFT_TRIAGED_FIELD + ":", auto.read_text(encoding="utf-8"))
        self.assertEqual((counts["total_drafts"], counts["expired_tonight"],
                          counts["over_7_days"], counts["over_30_days"]), (2, 1, 1, 1))
        self.assertEqual(counts["by_subdir"], {"decisions": 1, "notes": 1})
        self.assertEqual((drafts["examples"][0]["path"], drafts["examples"][0]["age_days"]),
                         ("_drafts/decisions/human.md", 43))

        # --dry-run 不蓋章：會過期的那份照算待審，檔案一個位元組都不動。
        dry_vault, dry_auto = self._draft_vault("dry")
        before = dry_auto.read_bytes()
        dry = dream._section_drafts([dry_vault], TODAY, None, {}, {"home": None, "dry_run": True})
        self.assertEqual(dry_auto.read_bytes(), before)
        self.assertEqual((dry["counts"]["total_drafts"], dry["counts"]["expired_tonight"]), (3, 1))

    # ------------------------------------------------------------- notices

    def test_overdue_notice_never_tells_an_agent_to_run_the_long_job(self):
        line = memspec.DREAM_NOTICE_OVERDUE_LINE.format(last="2026-11-01")
        self.assertIn("2026-11-01", line)
        self.assertNotIn("dream.py", line)
        self.assertNotIn("--scheduled", line)


if __name__ == "__main__":
    unittest.main(argv=[argument for argument in sys.argv if argument != "--selftest"])
