import sys; sys.dont_write_bytecode = True
r"""TURN_CITED_PATH_PATTERN must stay linear and must find exactly what it found before.

2026-10-12: a Bash command with one long unbroken run of characters (`echo aaa...`) took
8 s at 3,000 characters and was killed past 5,000. The time was in opened.basenames:
`[path chars]*[name chars]+` overlap, so each start position tried O(n^2) splits and
finditer retried every position, O(n^3). basenames runs in the PreToolUse gate after the
gate's own budget check, so the stall hit Claude Code's 10 s hook deadline.

The old pattern is kept here verbatim as the oracle: the rewrite must return the same
matches, not merely similar basenames.
"""
import random
import re
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
from epitype import memspec, opened

OLD_PATTERN = (
    r"[A-Za-z0-9_.\\/~-]*[A-Za-z0-9_-]+"
    r"\.(?:py|md|json|jsonl|txt|ps1|sh|sqlite3|toml|yaml|yml|cfg|ini|csv)\b"
)
OLD_REGEX = re.compile(OLD_PATTERN)
NEW_REGEX = re.compile(memspec.TURN_CITED_PATH_PATTERN)

# Far below the 1 s requirement, far above what a linear scan of 256 KB needs; the old
# pattern needed 0.65 s for 1,000 characters on the same machine.
BUDGET_SECONDS = 0.25
LONG = 256 * 1024

SAMPLES = (
    ("cat C:\\Epitype\\repo\\epitype\\opened.py", ["opened.py"]),
    ("sed -n '1,9p' /c/Epitype/repo/tests/run_all.py && head notes.md",
     ["run_all.py", "notes.md"]),
    ("Read ~/.claude/settings.json and state/x/data.jsonl", ["settings.json", "data.jsonl"]),
    ("python -m epitype sync --file ./a-b_c.toml", ["a-b_c.toml"]),
    ("a.py.bak a.py/b.yaml x.jsonlz y.json, z.CSV", ["a.py", "b.yaml", "y.json"]),
    ("ghost.py ghost.py GHOST.PY", ["ghost.py"]),
    ("..\\..\\deploy.ps1; ./run.sh; mem.sqlite3; c.cfg c.ini c.yml c.txt",
     ["deploy.ps1", "run.sh", "mem.sqlite3", "c.cfg", "c.ini", "c.yml", "c.txt"]),
    (".py -.md /.json a..py a.-py", ["-.md"]),
    ("中文路徑\\資料.md 與 x中.py 資料a.md", ["a.md"]),
    ("", []),
)


def _matches(regex, text):
    return [(match.start(), match.end(), match.group(0)) for match in regex.finditer(text)]


class LinearTime(unittest.TestCase):
    def assert_fast(self, function, text):
        started = time.perf_counter()
        result = function(text)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, BUDGET_SECONDS, f"{elapsed:.3f}s for {len(text)} chars")
        return result

    def test_basenames_on_256kb_unbroken_run(self):
        for text in ("echo " + "a" * LONG, "a" * LONG, "a/" * (LONG // 2), "a." * (LONG // 2),
                     "a" * LONG + ".py"):
            with self.subTest(head=text[:6], size=len(text)):
                self.assert_fast(opened.basenames, text)
        self.assertEqual(opened.basenames("echo " + "a" * LONG), [])

    def test_shared_regex_on_256kb_without_truncation(self):
        # stop_gate runs the same regex on whole sentences, which basenames' 8,000-char cut
        # does not protect.
        for text in ("a" * LONG, "a" * LONG + ".py", "/" * LONG + "a.md", "a-" * (LONG // 2)):
            with self.subTest(head=text[:6], size=len(text)):
                found = self.assert_fast(lambda value: _matches(NEW_REGEX, value), text)
                self.assertEqual(len(found), int(text.endswith((".py", ".md"))))
        whole = "a" * LONG + ".py"
        self.assertEqual(_matches(NEW_REGEX, whole), [(0, len(whole), whole)])


class SameMatchesAsBefore(unittest.TestCase):
    def test_sampled_paths(self):
        for text, expected in SAMPLES:
            with self.subTest(text=text):
                self.assertEqual(opened.basenames(text), expected)
                self.assertEqual(_matches(NEW_REGEX, text), _matches(OLD_REGEX, text))

    def test_randomised_against_old_pattern(self):
        # Short strings only: the oracle is the cubic pattern. The alphabet is the path
        # characters, the extension letters, and the separators that end a run.
        alphabet = list("ab_-.\\/~ ,") + ["py", "md", "json", "jsonl", "sh", "yml", "中"]
        generator = random.Random(20261013)
        for _ in range(20000):
            text = "".join(generator.choice(alphabet) for _ in range(generator.randint(0, 14)))
            self.assertEqual(_matches(NEW_REGEX, text), _matches(OLD_REGEX, text), text)

    def test_payload_cap_and_path_limit_unchanged(self):
        names = " ".join(f"f{index}.py" for index in range(memspec.TURN_CITED_MAX_PATHS + 5))
        self.assertEqual(len(opened.basenames(names)), memspec.TURN_CITED_MAX_PATHS)
        beyond = "x" * memspec.OPENED_PAYLOAD_MAX_CHARS + " late.py"
        self.assertEqual(opened.basenames(beyond), [])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
