import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]
"""Package-import and unified-CLI smoke tests."""

import os
from pathlib import Path
import subprocess
import tempfile

_REPO_ROOT = Path(__file__).resolve().parents[1]
if os.fspath(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(_REPO_ROOT))


def main():
    checks = []
    try:
        from epitype import decision_lint, ledger_gate, scar_census

        checks.append((
            "package modules import without a top-level memspec module",
            all((decision_lint, ledger_gate, scar_census)),
        ))

        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.fspath(_REPO_ROOT)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory(prefix="epitype-package-smoke-") as temp_dir:
            version = subprocess.run(
                [sys.executable, "-m", "epitype", "--version"],
                cwd=temp_dir,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            search_help = subprocess.run(
                [sys.executable, "-m", "epitype", "search", "--help"],
                cwd=temp_dir,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        checks.append((
            "python -m epitype exposes version and delegated command help",
            version.returncode == 0
            and version.stdout.strip().startswith("epitype ")
            and search_help.returncode == 0
            and "build" in search_help.stdout
            and "query" in search_help.stdout,
        ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 2
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
