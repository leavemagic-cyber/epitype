import sys; sys.dont_write_bytecode = True
"""Synthetic persistence faults must never become successful commitment reports."""

from contextlib import nullcontext, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import commitments as ledger


class CommitmentPersistenceRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-commitment-persistence-")
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name).resolve() / "vault"
        self.target = ledger.ledger_path(self.vault)
        self.target.parent.mkdir(parents=True)
        self.old, self.fresh, self.done = "old", "fresh", "done"
        rows = [{"digest": key, "text": "我會補上" + key + "的測試。", "session": "fixture",
                 "status": "closed" if key == self.done else "open",
                 "ts": "2000-01-01T00:00:00Z" if key == self.old else ledger._stamp()}
                for key in (self.old, self.fresh, self.done)]
        self.before = "".join(ledger._encode(row) for row in rows).encode("utf-8")
        self.target.write_bytes(self.before)

    def operations(self):
        return (
            ("record", lambda **kw: ledger.record(self.vault, "new", ["我會補上新的測試。"], **kw), []),
            ("close", lambda **kw: ledger.close(self.vault, [self.fresh], **kw), []),
            ("expire", lambda **kw: ledger.expire_stale(self.vault, **kw), []),
            ("purge", lambda **kw: ledger.purge_closed(self.vault, **kw), None),
        )

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = ledger.main([str(self.vault), *args], output=out)
        return code, out.getvalue(), err.getvalue()

    def test_replace_failure_never_returns_a_successful_mutation(self):
        for name, operation, failure in self.operations():
            with self.subTest(operation=name), patch.object(ledger.os, "replace", side_effect=PermissionError("replace denied")):
                self.assertEqual(operation(), failure)
                with self.assertRaises(PermissionError):
                    operation(strict=True)
            self.assertEqual(self.target.read_bytes(), self.before)
            self.assertEqual(list(self.target.parent.glob("*.tmp-*")), [])

    def test_lock_failure_is_fail_open_for_hooks_and_an_error_for_cli(self):
        with patch.object(ledger.memspec, "file_lock", side_effect=lambda *_: nullcontext(False)):
            for name, operation, failure in self.operations():
                with self.subTest(operation=name):
                    self.assertEqual(operation(), failure)
                    with self.assertRaises(TimeoutError):
                        operation(strict=True)
            for args in (("--close", self.fresh), ("--expire-stale",), ("--purge-closed",)):
                code, out, err = self.cli(*args)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("lock unavailable", err)
        self.assertEqual(self.target.read_bytes(), self.before)

    def test_read_failure_cannot_overwrite_existing_ledger_or_report_empty(self):
        original = Path.read_text

        def denied(path, *args, **kwargs):
            if path == self.target:
                raise PermissionError("read denied")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", denied), patch.object(ledger, "_write_all") as writer:
            for _name, operation, failure in self.operations():
                self.assertEqual(operation(), failure)
            for args in (("--list",), ("--json",), ("--close", self.fresh), ("--expire-stale",),
                         ("--purge-closed",), ("--requalify", "--dry-run")):
                code, out, err = self.cli(*args)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("read denied", err)
            writer.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.before)

    def test_partial_temporary_write_preserves_original_and_reports_no_new_rows(self):
        original = Path.open

        class PartialWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.stream.close()

            def write(self, data):
                self.stream.write(data[:len(data) // 2])
                raise OSError("partial write")

        def partial(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            return PartialWriter(stream) if path.name.startswith(".commitments.jsonl.tmp-") else stream

        with patch.object(Path, "open", partial):
            self.assertEqual(self.operations()[0][1](), [])
        self.assertEqual(self.target.read_bytes(), self.before)
        self.assertEqual(list(self.target.parent.glob("*.tmp-*")), [])

    def test_malformed_rows_remain_readable_but_cannot_be_silently_dropped_by_writers(self):
        for bad in (b"{broken\n", b"[]\n", b"\xff\n"):
            self.target.write_bytes(self.before + bad)
            self.assertEqual(len(ledger._rows(self.vault)), 3)
            for _name, operation, failure in self.operations():
                self.assertEqual(operation(), failure)
                with self.assertRaises(ValueError):
                    operation(strict=True)
            self.assertEqual(self.target.read_bytes(), self.before + bad)

    def test_flush_failure_does_not_publish_uncommitted_rows(self):
        with patch.object(ledger.memspec, "file_lock", side_effect=lambda *_: nullcontext(True)), \
             patch.object(ledger.os, "fsync", side_effect=OSError("flush denied")):
            self.assertEqual(self.operations()[0][1](), [])
        self.assertEqual(self.target.read_bytes(), self.before)
        self.assertEqual(list(self.target.parent.glob("*.tmp-*")), [])

    def test_cli_partial_actions_report_only_committed_steps(self):
        original = ledger.os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("second write denied")
            return original(source, target)

        with patch.object(ledger.os, "replace", fail_second):
            code, out, err = self.cli("--expire-stale", "--close", self.fresh, "--purge-closed")
        self.assertEqual(code, 2)
        self.assertIn("EXPIRED 1 old", out)
        self.assertNotIn("CLOSED", out)
        self.assertNotIn("PURGED", out)
        self.assertIn("second write denied", err)
        rows = {row["digest"]: row["status"] for row in ledger._rows(self.vault)}
        self.assertEqual(rows, {self.old: "expired", self.fresh: "open", self.done: "closed"})

    def test_success_retry_and_noop_results_match_persisted_rows(self):
        for name, operation, _failure in self.operations():
            with self.subTest(operation=name):
                self.target.write_bytes(self.before)
                with patch.object(ledger.os, "replace", side_effect=PermissionError("retry later")):
                    operation()
                self.assertEqual(self.target.read_bytes(), self.before)
                changed = operation(strict=True)
                self.assertTrue(changed)
                self.assertNotEqual(self.target.read_bytes(), self.before)
                persisted = self.target.read_bytes()
                self.assertEqual(operation(strict=True), 0 if name == "purge" else [])
                self.assertEqual(self.target.read_bytes(), persisted)

    def test_empty_existing_vault_is_a_successful_noop(self):
        for index, args in enumerate((("--close", "missing"), ("--expire-stale",), ("--purge-closed",))):
            vault = self.vault.parent / f"empty-{index}"
            vault.mkdir()
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err):
                code = ledger.main([str(vault), *args], output=out)
            self.assertEqual(code, 0, err.getvalue())
            self.assertFalse(ledger.ledger_path(vault).exists())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
