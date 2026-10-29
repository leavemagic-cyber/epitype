"""Time-limited owner wording without expiry is advisory, including at write time."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import io
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import card_lint, memspec


# Invented wording exercises both time markers without retaining a real quotation.
OWNER_QUOTE = "合成花園先試一禮拜，一禮拜後要通知我盆栽的澆水安排。"


class OwnerQuoteTimeLimit(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-owner-time-limit-")
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name).resolve()
        self.path = self.vault / "synthetic.md"

    def write_card(self, body="", quote=None, extra=""):
        text = ("---\nname: synthetic\ndescription: 2026-10-28 合成卡\n"
                "last_verified_at: 2026-10-28\naliases: [合成卡]\n"
                "unenforceable: 判斷型\nmetadata:\n  type: feedback\n")
        if quote is not None:
            text += "owner_quote: |\n" + "\n".join("  " + line for line in quote.splitlines()) + "\n"
        text += extra + "---\n" + body + "\n"
        self.path.write_text(text, encoding="utf-8")
        return text

    def findings(self, body="", quote=None, extra=""):
        self.write_card(body, quote, extra)
        _kind, findings = card_lint.check_card(self.path, "synthetic.md")
        return [(level, reason) for level, rule, reason in findings
                if rule == "owner-quote-time-limit"]

    def test_owner_week_trial_without_expiry_warns(self):
        findings = self.findings(quote=OWNER_QUOTE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], card_lint.WARN)
        for fragment in ("一禮拜", "後要通知", "移除條件", *memspec.CARD_EXPIRY_FIELDS,
                         memspec.TIME_LIMIT_REVIEWED_FIELD):
            self.assertIn(fragment, findings[0][1])

    def test_reviewed_marker_suppresses_warning_only_when_filled(self):
        field = memspec.TIME_LIMIT_REVIEWED_FIELD
        self.assertEqual(self.findings(
            quote=OWNER_QUOTE, extra=f'{field}: "2026-10-28 仍有效：解除條件是 owner 明確取消"\n'), [])
        self.assertEqual(len(self.findings(quote=OWNER_QUOTE, extra=f'{field}: ""\n')), 1)

    def test_each_honoured_expiry_suppresses_warning(self):
        self.assertEqual(set(memspec.CARD_EXPIRY_FIELDS), {"valid_until", "expires_at"})
        for field in memspec.CARD_EXPIRY_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(self.findings(quote=OWNER_QUOTE, extra=f"{field}: 2026-11-04\n"), [])

    def test_empty_expiry_and_unhonoured_fields_do_not_suppress_warning(self):
        for extra in ("valid_until:\nexpires_at: ''\n", "expiry: 2026-11-04\n"):
            with self.subTest(extra=extra):
                self.assertEqual(len(self.findings(quote=OWNER_QUOTE, extra=extra)), 1)

    def test_only_missing_or_active_status_warns(self):
        for status in (None, "active", "ACTIVE"):
            with self.subTest(status=status):
                extra = "" if status is None else f"status: {status}\n"
                self.assertEqual(len(self.findings(quote=OWNER_QUOTE, extra=extra)), 1)
        for status in (*memspec.STOP_GATE_SILENT_STATUSES, "pending"):
            with self.subTest(status=status):
                self.assertEqual(self.findings(quote=OWNER_QUOTE, extra=f"status: {status}\n"), [])

    def test_ordinary_owner_wording_does_not_warn(self):
        self.assertEqual(self.findings(quote="合成花園使用藍色花盆"), [])

    def test_all_markers_warn_in_attributed_quote_lines(self):
        for marker in card_lint.OWNER_QUOTE_TIME_LIMIT_MARKERS:
            for attribution in ("owner", "Owner", "使用者", "原話"):
                with self.subTest(marker=marker, attribution=attribution):
                    findings = self.findings(body=f"{attribution} 說：「{marker}。」")
                    self.assertEqual(len(findings), 1)
                    self.assertIn(marker, findings[0][1])

    def test_non_owner_and_unquoted_lines_do_not_warn(self):
        for body in ("其他人說：「先試一禮拜。」", "「先試一禮拜。」", "Owner 先試一禮拜。",
                     "Owner 說：「報告縮短。」之後再討論。", "Owner\n「先試一禮拜。」"):
            with self.subTest(body=body):
                self.assertEqual(self.findings(body=body), [])

    def test_markers_in_code_fences_are_ignored(self):
        for delimiter in ("```", "~~~~", "````"):
            for close in (delimiter, ""):
                fenced = f"  {delimiter}text\n  Owner 說：「先試一禮拜。」\n  {close}"
                with self.subTest(delimiter=delimiter, close=close):
                    self.assertEqual(self.findings(body=fenced), [])
                    self.assertEqual(self.findings(quote=fenced), [])

    def test_longer_fence_is_not_closed_by_shorter_fence(self):
        self.assertEqual(self.findings(body="````text\n```\nOwner：「暫時。」\n````"), [])

    def test_real_quote_after_fence_still_warns(self):
        self.assertEqual(len(self.findings(body="~~~text\n範例\n~~~\nOwner：「暫時。」")), 1)

    def test_owner_quote_scalar_and_multiple_quotes_warn_once(self):
        self.assertEqual(len(self.findings(body="Owner：「先試。」與「一禮拜。」",
                                          extra='owner_quote: "暫時這樣做"\n')), 1)

    def test_metadata_expiry_uses_shared_parser(self):
        text = self.write_card(quote=OWNER_QUOTE)
        text = text.replace("  type: feedback\n", "  type: feedback\n  valid_until: 2026-11-04\n")
        self.path.write_text(text, encoding="utf-8")
        _kind, findings = card_lint.check_card(self.path, "synthetic.md")
        self.assertFalse(any(rule == "owner-quote-time-limit" for _, rule, _ in findings))

    def test_warning_never_becomes_a_strict_cli_failure(self):
        self.write_card(quote=OWNER_QUOTE)
        output = io.StringIO()
        self.assertEqual(card_lint.main([str(self.vault), "--strict"], output), 0)
        self.assertIn("owner-quote-time-limit", output.getvalue())

    def test_before_write_uses_same_advisory_finding(self):
        from adapters.claude import pretooluse_gate

        text = self.write_card(quote=OWNER_QUOTE)
        _kind, findings = card_lint.check_card(
            self.path, "synthetic.md", before_text=text)
        self.assertIn((card_lint.WARN, "owner-quote-time-limit"),
                      [(level, rule) for level, rule, _ in findings])
        denial, advice = pretooluse_gate._card_review("synthetic.md", text, before_text=text)
        self.assertIsNone(denial)
        self.assertIn("一禮拜", advice)


if __name__ == "__main__":
    unittest.main(argv=[argument for argument in sys.argv if argument != "--selftest"])
