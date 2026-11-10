"""Generated edit/retry sequences, checked against an independent Markdown parser.

Install the optional test extra: python -m pip install ".[test]".
The parser is an oracle only; production always publishes original bytes.
"""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collections import Counter
from copy import deepcopy
import atexit
import json
import os
import tempfile
import unittest

# Hypothesis also caches parser constants; keep those out of the checkout.
_STORAGE = tempfile.TemporaryDirectory(prefix="epitype-hypothesis-")
atexit.register(_STORAGE.cleanup)
os.environ["HYPOTHESIS_STORAGE_DIRECTORY"] = _STORAGE.name

from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule
from markdown_it import MarkdownIt

from epitype import card_io, fixed_load, memory_index
from _fixed_load_fixtures import isolate_catalogs


PARSER = MarkdownIt("commonmark")


def entries(raw):
    """Return item text, heading names and locations using the external parser."""
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    headings, result = {}, []
    for token in PARSER.parse(text):
        if token.type == "heading_open" and token.level == 0:
            line = lines[token.map[0]].rstrip("\r\n")
            if line == memory_index.ROOT_ADDITIONS.decode().rstrip("\n"):
                headings = {}
                continue
            level = int(token.tag[1:])
            headings = {key: value for key, value in headings.items() if key < level}
            headings[level] = line.lstrip("#").strip()
        elif token.type == "list_item_open" and token.level == 1:
            begin, end = token.map
            body = "".join(lines[begin:end]).replace("\r\n", "\n").rstrip("\n")
            result.append(((tuple(headings.values()), body), begin))
    return result


def records(raw):
    return Counter(key for key, _line in entries(raw))


class IndexEdits(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.temp = tempfile.TemporaryDirectory(prefix="epitype-sequence-index-")
        self.vault = Path(self.temp.name).resolve()
        self.memory = self.vault / "MEMORY.md"
        self.card = self.vault / "card.md"
        self.card.write_bytes(b"unchanged card\n")
        self.config = {"index_cap_bytes": 320}
        self.expected = Counter()
        self.serial = 0

    @initialize(crlf=st.booleans())
    def create(self, crlf):
        original = (b"# Index\n## Active\n- [first](card.md) 2026-12-01\n## Closed\n"
                    + b"".join(b"- [old](card.md) " + str(i).encode() + b"x" * 180 + b"\n"
                               for i in range(8)))
        self.original = original.replace(b"\n", b"\r\n") if crlf else original
        self.memory.write_bytes(self.original)

    def source(self):
        return memory_index._without_pointer(self.memory.read_bytes()).replace(b"\r\n", b"\n")

    def insert(self, source, section, item):
        heading = b"## " + section.encode() + b"\n"
        if heading not in source:
            return source + b"\n" + heading + item
        return source.replace(heading, heading + item, 1)

    @rule(section=st.sampled_from(["Active", "Closed", "進行中", "Queue"]))
    def add(self, section):
        self.serial += 1
        item = f"- [item {self.serial}](card.md) 2026-12-01\n".encode()
        self.memory.write_bytes(self.insert(self.source(), section, item))

    @rule(section=st.sampled_from(["Active", "Closed", "已結案"]))
    def move_first_item(self, section):
        source = self.source()
        items = entries(source)
        if not items:
            return
        lines = source.splitlines(keepends=True)
        item = lines.pop(items[0][1])
        self.memory.write_bytes(self.insert(b"".join(lines), section, item))

    @rule(name=st.sampled_from(["Paused", "Closed", "進行中"]))
    def rename_section(self, name):
        source = self.source()
        lines = source.splitlines(keepends=True)
        for pos, line in enumerate(lines):
            if line.startswith(b"## "):
                lines[pos] = b"## " + name.encode() + b"\n"
                break
        self.memory.write_bytes(b"".join(lines))

    @rule()
    def duplicate_first_item(self):
        source = self.source()
        items = entries(source)
        if items:
            lines = source.splitlines(keepends=True)
            pos = items[0][1]
            lines.insert(pos, lines[pos])
            self.memory.write_bytes(b"".join(lines))

    @rule(crlf=st.booleans())
    def native_line_endings(self, crlf):
        raw = self.memory.read_bytes().replace(b"\r\n", b"\n")
        self.memory.write_bytes(raw.replace(b"\n", b"\r\n") if crlf else raw)

    @rule()
    def append_after_pointer(self):
        self.serial += 1
        self.memory.write_bytes(self.memory.read_bytes() + f"- [native {self.serial}](card.md)\n".encode())

    @rule()
    def add_fenced_heading_example(self):
        self.memory.write_bytes(self.source() +
            b"\n## Examples\n````text\n## fake heading\n```\n- fake item\n````\n")

    @rule()
    def reject_stale_plan_then_retry(self):
        proposed = memory_index.plan(self.vault, self.config)
        self.serial += 1
        self.memory.write_bytes(self.memory.read_bytes() + f"- [concurrent {self.serial}](card.md)\n".encode())
        try:
            memory_index.apply_plan(proposed)
        except card_io.CardConflict:
            pass
        else:
            raise AssertionError("stale plan overwrote the editor")

    @invariant()
    def preserve_history_context_and_noop(self):
        before_source = self.source()
        current = records(before_source)
        self.expected |= current
        memory_index.apply_plan(memory_index.plan(self.vault, self.config))
        short = self.memory.read_bytes()
        full = self.vault / memory_index.FULL_FILENAME
        backup = self.vault / memory_index.BACKUP_FILENAME
        archive = full.read_bytes()
        assert archive.startswith(memory_index.FULL_HEADER + self.original)
        assert backup.read_bytes() == self.original
        assert len(short) <= self.config["index_cap_bytes"]
        assert records(archive) >= self.expected, self.expected - records(archive)
        assert records(memory_index._without_pointer(short)) <= current
        assert self.card.read_bytes() == b"unchanged card\n"
        paths = (self.memory, full, backup)
        saved = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        memory_index.apply_plan(memory_index.plan(self.vault, self.config))
        assert saved == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}

    def teardown(self):
        self.temp.cleanup()


class ProjectReports(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.temp = tempfile.TemporaryDirectory(prefix="epitype-sequence-reports-")
        self.home = Path(self.temp.name).resolve()
        self.catalogs = isolate_catalogs(self.home)
        self.catalogs.start()
        self.projects, self.values, self.saved = {}, {}, {}
        for name in ("中文", "測試"):
            cwd = self.home / name
            cwd.mkdir()
            self.projects[name] = cwd
            self.values[name] = fixed_load.report(home=self.home, cwd=cwd)

    @rule(name=st.sampled_from(["中文", "測試"]), total=st.integers(min_value=0, max_value=5000))
    def refresh(self, name, total):
        value = deepcopy(self.values[name])
        value["static_total"] = total
        value["static_estimates"]["memory"] = total
        path, _receipt = fixed_load.cache_paths(self.home, self.projects[name])
        fixed_load.publish(value, path)
        self.saved[name] = path.read_bytes()

    @rule(name=st.sampled_from(["中文", "測試"]))
    def deliver(self, name):
        cwd = self.projects[name]
        path, receipt = fixed_load.cache_paths(self.home, cwd)
        line, state = fixed_load.notice(path, receipt, cwd)
        if line:
            fixed_load.publish(state, receipt)
        assert fixed_load.notice(path, receipt, cwd) == (None, None)

    @invariant()
    def other_projects_keep_their_reports(self):
        for name, raw in self.saved.items():
            path, _receipt = fixed_load.cache_paths(self.home, self.projects[name])
            assert path.read_bytes() == raw
            assert json.loads(raw)["cwd"] == str(self.projects[name])

    def teardown(self):
        self.catalogs.stop()
        self.temp.cleanup()


# Finite edit batches keep CI work bounded; filesystem timing is not an acceptance
# threshold. Reproducible generation and no shared example DB isolate every run.
IndexSequences = IndexEdits.TestCase
IndexSequences.settings = settings(max_examples=40, stateful_step_count=15,
                                   deadline=None, derandomize=True, database=None)
ReportSequences = ProjectReports.TestCase
ReportSequences.settings = settings(max_examples=30, stateful_step_count=12,
                                    deadline=None, derandomize=True, database=None)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
