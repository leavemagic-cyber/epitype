import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 consoles must not break the privacy gate.
"""Scan Git-tracked files for portable privacy patterns and local blocklist terms."""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile


_REPO_ROOT = Path(__file__).resolve().parents[1]
_LINUX_HOME_PREFIX = "/" + "home" + "/"


@dataclass(frozen=True)
class Rule:
    name: str
    regex: re.Pattern


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    pattern: str


_BUILTIN_RULES = (
    Rule(
        "windows-home",
        re.compile(
            r"\b[A-Za-z]:[\\/]Users[\\/]([^\\/\s<>:\"'{}]+)(?:[\\/]|$)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "linux-home",
        re.compile(
            re.escape(_LINUX_HOME_PREFIX)
            + r"([^/\s<>:\"'{}]+)(?:/|$)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "session-uuid",
        re.compile(
            r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "email",
        re.compile(
            r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
        ),
    ),
)

# Only the named built-in patterns are exempted. Local blocklist terms are never
# allowlisted, so a publisher's private deny-list remains authoritative.
_ALLOWLIST = {
    "LICENSE": frozenset({"email"}),
}


def _tracked_files(repo):
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files failed: {detail}")
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def _blocklist_rules(path):
    if path is None:
        return ()
    terms = []
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            term = raw_line.strip()
            if term:
                terms.append(
                    Rule(
                        f"blocklist:{line_number}",
                        re.compile(re.escape(term), re.IGNORECASE),
                    )
                )
    return tuple(terms)


def scan_repo(repo, blocklist=None):
    repo = Path(repo).resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"not a Git repository: {repo}")
    rules = _BUILTIN_RULES + _blocklist_rules(blocklist)
    findings = []
    tracked = _tracked_files(repo)
    for relative in tracked:
        path = repo / relative
        try:
            if path.is_symlink():
                text = os.readlink(path)
            else:
                text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            raise OSError(f"cannot read tracked file {relative}: {exc}") from exc
        allowed = _ALLOWLIST.get(Path(relative).as_posix(), frozenset())
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule in rules:
                if rule.name in allowed:
                    continue
                if rule.regex.search(line):
                    findings.append(
                        Finding(Path(relative).as_posix(), line_number, rule.name)
                    )
    return tracked, findings


def _git_init_and_add(root):
    subprocess.run(
        ["git", "init", "-q", str(root)],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "add", "--", "."],
        capture_output=True,
        check=True,
    )


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-privacy-") as temp_dir:
            root = Path(temp_dir)
            poison = root / "poison"
            poison.mkdir()
            toxic_text = "\n".join(
                (
                    "C:" + "\\Users\\" + "SampleName\\notes.txt",
                    "/" + "home" + "/" + "sample-name/notes.txt",
                    "12345678" + "-1234-4234-9234-123456789abc",
                    "sample" + "@" + "example.test",
                    "orchard-private-marker",
                )
            )
            (poison / "toxic.txt").write_text(toxic_text + "\n", encoding="utf-8")
            blocklist = root / "local-blocklist.txt"
            blocklist.write_text("orchard-private-marker\n", encoding="utf-8")
            _git_init_and_add(poison)
            _, poison_findings = scan_repo(poison, blocklist)
            patterns = {finding.pattern for finding in poison_findings}
            checks.append(
                (
                    "poisoned tracked fixture is blocked",
                    patterns
                    == {
                        "windows-home",
                        "linux-home",
                        "session-uuid",
                        "email",
                        "blocklist:1",
                    },
                )
            )

            clean = root / "clean"
            clean.mkdir()
            (clean / "notes.txt").write_text(
                "Portable synthetic notes without private identifiers.\n",
                encoding="utf-8",
            )
            (clean / "LICENSE").write_text(
                "Contact " + "license" + "@" + "example.test for this synthetic fixture.\n",
                encoding="utf-8",
            )
            _git_init_and_add(clean)
            _, clean_findings = scan_repo(clean)
            checks.append(("clean tracked tree passes", clean_findings == []))
            checks.append(
                (
                    "specific LICENSE allowlist is effective",
                    not any(finding.path == "LICENSE" for finding in clean_findings),
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 3
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(_REPO_ROOT), help="Git repository to scan")
    parser.add_argument("--blocklist", help="private one-term-per-line blocklist")
    parser.add_argument("--selftest", action="store_true", help="run synthetic gate checks")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    try:
        tracked, findings = scan_repo(args.repo, args.blocklist)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"PRIVACY ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(f"FAIL {finding.path}:{finding.line} [{finding.pattern}]")
    if findings:
        print(f"PRIVACY FAIL {len(findings)} finding(s) in {len(tracked)} tracked file(s)")
        return 1
    print(f"PRIVACY PASS {len(tracked)} tracked file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
