import sys; sys.dont_write_bytecode = True
"""Replay gate inputs only; never execute the commands being classified."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]
from epitype import memspec
import _hook_common as common
import pretooluse_gate as gate

TEMPLATE = ROOT / "templates" / "power" / "examples" / "scar-destructive-git.md"
PREFIX = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
CLONE = "git clone --no-hardlinks --no-checkout -- SOURCE TARGET"
PROBE = PREFIX + "$probe = '" + CLONE + "'\n" + (
    "$pattern = 'git[^\\n]*(reset --hard|clean -fd|checkout -- )'\n"
    "$match = [regex]::Match($probe, $pattern)\n"
    "[pscustomobject]@{ executed = $false; matched = $match.Success }\n"
)
CASES = (
    ("clone flag suffix", CLONE, False),
    ("original grouped rollback batch", PREFIX + CLONE + "\n"
     "if ($LASTEXITCODE -ne 0) { throw 'clone failed' }\n"
     "git -C TARGET switch --detach HEAD\n"
     "git -C TARGET revert --no-commit HEAD\n", False),
    ("original non-executing diagnostic", PROBE, False),
    ("unparsed wrapper and alternative clone layout", PREFIX +
     "git -C SOURCE clone --no-checkout --no-hardlinks -- ORIGIN COPY", False),
    ("quoted mention in supported syntax", "Write-Output 'git reset --hard'", False),
    ("comment in supported syntax", "echo safe # git clean -fdx", False),
    ("read-only diff", "git -C TARGET diff --check", False),
    ("read-only historical content", "git show HEAD:notes.md", False),
    ("unrelated executable name", "not-git reset --hard", False),
    ("distinct reset option", "git reset --harder", False),
    ("reset", "git reset --hard", True),
    ("reset with path option", "git -C TARGET reset --hard", True),
    ("reset with multiple global options", "git -C TARGET -c color.ui=never reset --hard HEAD", True),
    ("forced clean", "git clean -fd", True),
    ("forced ignored clean", "git clean -fdx", True),
    ("checkout paths", "git checkout -- notes.md", True),
    ("checkout separator at end", "git checkout --", True),
    ("path and case normalization", r"C:\Tools\GIT.EXE -C TARGET RESET --HARD", True),
    ("chained reset", "echo safe && git reset --hard", True),
    ("cmd wrapper", 'cmd /c "git reset --hard"', True),
    ("PowerShell wrapper", 'powershell -Command "git -C TARGET clean -fdx"', True),
    ("bash wrapper", "bash -c 'git checkout -- notes.md'", True),
    ("Python execution wrapper", 'python -c "import os; os.system(\'git reset --hard\')"', True),
    ("unsupported grouped destructive command", PREFIX + "if ($true) { git reset --hard }", True),
    ("unsupported prefix then direct command retains denial", PREFIX + "git reset --hard", True),
    ("unsupported quoted executable retains denial", PREFIX +
     '& "C:\\Tools\\git.exe" reset --hard', True),
    ("unsupported command continuation retains denial", PREFIX +
     "git `\nreset --hard", True),
    ("unsupported dynamic execution stays conservative", PREFIX +
     "$code = 'git reset --hard'\nInvoke-Expression $code", True),
    ("unbalanced quote retains fallback", 'echo "git reset --hard', True),
    ("unsupported quoted example still conservatively denied", PREFIX +
     "$data = 'git reset --hard'\n[regex]::Match($data, 'example')", True),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card", type=Path, default=TEMPLATE)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source = args.card.read_text(encoding="utf-8")
    checks = []
    with tempfile.TemporaryDirectory(prefix="epitype-git-gate-") as temp_dir:
        root = Path(temp_dir).resolve()
        vault = root / "vault"
        vault.mkdir()
        (vault / "test-scar.md").write_text(source, encoding="utf-8")
        config = root / "config.json"
        common.write_config(config, [vault])
        with patch.dict(os.environ, {memspec.EPITYPE_CONFIG_ENV: str(config)}):
            for name, command, expected in CASES:
                value = gate._handle({"tool_name": "Bash", "tool_input": {"command": command}}, time.monotonic())
                denied = bool(value and value.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")
                checks.append({"case": name, "expected_deny": expected, "actual_deny": denied,
                               "pass": denied == expected})
        log = vault / memspec.GATE_LOG_FILENAME
        audit = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        checks.append({"case": "conservative fallback remains audited", "pass": any(
            row.get("fallback") == "fulltext" for row in audit)})
    for row in checks:
        if not row["pass"]:
            print(json.dumps(row, ensure_ascii=False))
    count = sum(row["pass"] for row in checks)
    if args.report:
        args.report.write_text(json.dumps({"passed": count, "total": len(checks), "checks": checks},
                                         ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"GIT GATE {'PASS' if count == len(checks) else 'FAIL'} {count}/{len(checks)}")
    return 0 if count == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
