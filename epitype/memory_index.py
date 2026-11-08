"""Bound MEMORY.md without rewriting cards or losing index text.

The full index and first backup are siblings of MEMORY.md: relative links keep
their base, and the leading underscore excludes both from card/recall scans.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

try:
    from . import card_io, memspec
except ImportError:
    import card_io
    import memspec

DEFAULT_CAP_BYTES = 1526
FULL_FILENAME = "_MEMORY_FULL.md"
BACKUP_FILENAME = "_MEMORY_BACKUP.md"
POINTER = b"\n<!-- epitype:index -->\n[Full index](_MEMORY_FULL.md)\n"
FULL_HEADER = b"<!-- epitype:full-index; source lines preserved verbatim -->\n"
_ENTRY = re.compile(rb"^(?:[-*+] |\d+[.)] |\[\[|\[[^]]+\]:)")
_HEADING = re.compile(rb"^#{1,6} ")
_DATE = re.compile(rb"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?!\d)")


def cap_of(config=None):
    """One raw-file budget; the old shared key is a migration alias for all vaults."""
    config = memspec.config_options() if config is None else config
    value = config.get(memspec.CONFIG_INDEX_CAP_BYTES_FIELD,
                       config.get(memspec.CONFIG_SHARED_INDEX_CAP_BYTES_FIELD, DEFAULT_CAP_BYTES))
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("index_cap_bytes must be a positive integer")
    return value


def _without_pointer(raw):
    # Native memory writers commonly append after our pointer.
    return raw.replace(POINTER, b"")


def _units(lines):
    """Keep introductory handwriting, headings, continuations and fences intact.

    A list item includes its indented continuation. Prose paragraphs and fenced
    blocks are indivisible; reference definitions are mandatory so retained
    reference-style links still work. Headings are retained only with their unit.
    """
    header_end = 0
    for i, line in enumerate(lines):
        if _ENTRY.match(line) or (i and _HEADING.match(line)):
            break
        header_end = i + 1
    required = set(range(header_end))
    units = []
    start = header_end
    headings = {}
    fence = None
    for i in range(header_end, len(lines)):
        line = lines[i]
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith((b"```", b"~~~")):
            fence = stripped[:3]
            continue
        if _HEADING.match(line):
            if start < i:
                units.append((list(range(start, i)), tuple(headings.values())))
            level = len(line) - len(line.lstrip(b"#"))
            headings = {k: v for k, v in headings.items() if k < level}
            headings[level] = i
            start = i + 1
        elif i > start and (_ENTRY.match(line) or
                            (lines[i - 1].strip() == b"" and line.strip())):
            units.append((list(range(start, i)), tuple(headings.values())))
            start = i
    if start < len(lines):
        units.append((list(range(start, len(lines))), tuple(headings.values())))
    for ids, context in units:
        if any(re.match(rb"^\[[^]]+\]:", lines[i]) for i in ids):
            required.update(ids)
            required.update(context)
    return required, units


@dataclass
class Plan:
    vault: Path
    original: bytes
    short: bytes
    kept: list
    moved: list
    cap: int

    def summary(self):
        return dict(vault=str(self.vault), before_bytes=len(self.original),
                    after_bytes=len(self.short), kept_lines=len(self.kept),
                    moved_lines=len(self.moved), cap_bytes=self.cap,
                    status="would-rewrite" if self.short != self.original else "unchanged")


def plan(vault, config=None):
    vault = Path(vault).expanduser().resolve()
    raw = (vault / memspec.MEMORY_INDEX_FILENAME).read_bytes()
    raw.decode("utf-8")  # Reject invalid encoding before any file is written.
    cap = cap_of(config)
    source = _without_pointer(raw)
    lines = source.splitlines(keepends=True)
    if len(raw) <= cap:
        return Plan(vault, raw, raw, list(range(len(lines))), [], cap)
    required, units = _units(lines)
    used = sum(len(lines[i]) for i in required) + len(POINTER)
    if used > cap:
        raise ValueError(f"{vault}: handwritten header/reference definitions plus pointer need "
                         f"{used} bytes; index_cap_bytes is {cap}. Increase the cap or edit the header.")
    chosen = set(required)

    def priority(unit):
        ids, _context = unit
        text = b"".join(lines[i] for i in ids)
        dates = [b"".join(m.groups()) for m in _DATE.finditer(text)]
        return ("⚠".encode() in text, max(dates, default=b""), max(ids))

    for ids, context in sorted(units, key=priority, reverse=True):
        extra = (set(ids) | set(context)) - chosen
        cost = sum(len(lines[i]) for i in extra)
        if used + cost <= cap:
            chosen.update(extra)
            used += cost
    kept = sorted(chosen)
    moved = [i for i in range(len(lines)) if i not in chosen]
    short = b"".join(lines[i] for i in kept) + POINTER
    return Plan(vault, raw, short, kept, moved, cap)


def _full_payload(previous, source):
    """Retain the entire first index; append only newly added line occurrences.

    Keeping its header, link definitions and kept entries too makes the full
    index readable on its own. Counts (not a set) preserve duplicate lines.
    """
    if previous is None:
        return FULL_HEADER + source
    if not previous.startswith(FULL_HEADER):
        raise ValueError(f"{FULL_FILENAME} already exists and is not an Epitype full index")
    available = Counter(previous[len(FULL_HEADER):].splitlines(keepends=True))
    added = []
    for line in source.splitlines(keepends=True):
        if available[line]:
            available[line] -= 1
        else:
            added.append(line)
    if not added:
        return previous
    # A separator preserves an unterminated last line rather than concatenating
    # it with a newly appended entry. The original bytes remain untouched.
    separator = b"\n" if previous and not previous.endswith((b"\n", b"\r")) else b""
    return previous + separator + b"".join(added)


def apply_plan(proposed):
    """Archive first, then compare/replace the short index; never touch a card."""
    vault = proposed.vault
    index = vault / memspec.MEMORY_INDEX_FILENAME
    if index.read_bytes() != proposed.original:
        raise card_io.CardConflict(f"index changed: {index}")
    backup = vault / BACKUP_FILENAME
    full = vault / FULL_FILENAME
    if proposed.short == proposed.original and not full.exists():
        return
    for target in (index, backup, full):
        if target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction()):
            raise ValueError(f"index output must not be a link: {target}")
    previous = full.read_bytes() if full.exists() else None
    payload = _full_payload(previous, _without_pointer(proposed.original))
    # publish never overwrites even if another process wins the first-backup race.
    if not backup.exists():
        try:
            card_io.publish(backup, proposed.original)
        except FileExistsError:
            pass
    if previous is None:
        card_io.publish(full, payload)
    elif payload != previous:
        card_io.replace_if_unchanged(full, payload, previous)
    if proposed.short != proposed.original:
        card_io.replace_if_unchanged(index, proposed.short, proposed.original)
    if index.read_bytes() != proposed.short or full.read_bytes() != payload:
        raise OSError(f"index readback failed: {vault}; original retained at {backup}")


def discover(projects):
    """Only direct native project memory directories; do not traverse links."""
    root = Path(projects).expanduser()
    if not root.is_dir():
        raise NotADirectoryError(root)
    found = []
    for project in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if project.is_symlink() or (hasattr(project, "is_junction") and project.is_junction()):
            continue
        memory = project / "memory"
        if memory.is_symlink() or (hasattr(memory, "is_junction") and memory.is_junction()):
            continue
        if memory.is_dir():
            found.append(memory)
    return found


def lint(vault, config=None):
    index = Path(vault) / memspec.MEMORY_INDEX_FILENAME
    cap = cap_of(config)
    size = index.stat().st_size
    return dict(vault=str(Path(vault).resolve()), before_bytes=size, cap_bytes=cap,
                status="over-cap" if size > cap else "unchanged")


def main(argv=None, output=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vaults", nargs="*", type=Path)
    parser.add_argument("--projects", type=Path, help="include every */memory directory under this root")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write backup/full index, then shorten MEMORY.md")
    mode.add_argument("--check", action="store_true", help="lint only; exit 1 for any over-cap index")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(argv)
    results = []
    exit_code = 0
    try:
        config = memspec.config_options()
        vaults = list(parsed.vaults)
        if parsed.projects:
            vaults.extend(discover(parsed.projects))
        if not vaults:
            vaults = [Path(v) for v in config.get(memspec.CONFIG_VAULTS_FIELD, [])]
        if not vaults:
            raise ValueError("provide a vault, --projects, or config vaults")
        unique = dict.fromkeys(p.expanduser().resolve() for p in vaults)
        for vault in unique:
            if not vault.is_dir():
                results.append(dict(vault=str(vault), status="error", error="vault is not a directory"))
                exit_code = 2
                continue
            if not (vault / memspec.MEMORY_INDEX_FILENAME).exists():
                results.append(dict(vault=str(vault), status="no-index"))
                continue
            try:
                if parsed.check:
                    row = lint(vault, config)
                    if row["status"] == "over-cap":
                        exit_code = max(exit_code, 1)
                    results.append(row)
                    continue
                proposed = plan(vault, config)
                row = proposed.summary()
                if parsed.apply:
                    apply_plan(proposed)
                    if row["status"] == "would-rewrite":
                        row["status"] = "rewritten"
                results.append(row)
            except (OSError, ValueError) as exc:
                results.append(dict(vault=str(vault), status="error", error=str(exc)))
                exit_code = 2
    except (OSError, ValueError) as exc:
        results.append(dict(status="error", error=str(exc)))
        exit_code = 2
    if parsed.json:
        print(json.dumps(results, ensure_ascii=False, indent=2), file=output)
    else:
        for row in results:
            print(json.dumps(row, ensure_ascii=False), file=output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
