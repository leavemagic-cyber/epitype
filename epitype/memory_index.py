"""Bound MEMORY.md without rewriting cards or losing index text.

The full index and first backup are siblings of MEMORY.md: relative links keep
their base, and the leading underscore excludes both from card/recall scans.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import sys

try:
    from . import card_io, memspec
except ImportError:
    import card_io
    import memspec

# MEMORY.md 的上限是宿主實際的載入上限，不是自訂數字：Claude Code 開場只載入
# 前 200 行或前 25KB（先到者為準），超過的部分不會被讀到。共用治理庫與專案庫一樣。
# 來源：https://code.claude.com/docs/en/memory （owner 2026-10-10、2026-11-17 裁定）。
# 只有 Claude Code 改了這個載入上限才改這兩個數字；任何設定鍵都不能把它調低。
HOST_INDEX_MAX_LINES = 200
HOST_INDEX_MAX_BYTES = 25_000
# 舊設定鍵照樣讀得進來（舊設定不會壞），但不再當上限用。
LEGACY_CAP_KEYS = (memspec.CONFIG_INDEX_CAP_BYTES_FIELD, memspec.CONFIG_SHARED_INDEX_CAP_BYTES_FIELD)
LEGACY_CAP_NOTICE = ("{keys} 已不再使用；MEMORY.md 上限是宿主載入上限 "
                     f"{HOST_INDEX_MAX_LINES} 行或 {HOST_INDEX_MAX_BYTES:,} B")
# 搬出時優先挑連到失效卡的行：這些狀態、有 superseded_by、或到期日已過。
_STALE_STATUSES = ("superseded", "closed", "retired", "archived", "deprecated", "historical")
# Markdown links: `](a.md)`, `](<a b.md>)`, `](a%20b.md#x "t")`; wikilinks: `[[a]]`, `[[a.md|alias]]`.
_LINK_TARGET = re.compile(
    r"\]\(\s*(?:<([^>\n]+?\.md)(?:#[^>\n]*)?>|([^)\s#<]+\.md))(?:#[^)\s]*)?\s*(?:\"[^\"]*\")?\s*\)"
    r"|\[\[([^\]|#\n]+)")
FULL_FILENAME = "_MEMORY_FULL.md"
BACKUP_FILENAME = "_MEMORY_BACKUP.md"
POINTER = b"\n<!-- epitype:index -->\n[Full index](_MEMORY_FULL.md)\n"
FULL_HEADER = b"<!-- epitype:full-index; source lines preserved verbatim -->\n"
ROOT_ADDITIONS = b"# Index additions without a source heading <!-- epitype:root -->\n"
_ENTRY = re.compile(rb"^(?:[-*+] |\d+[.)] |\[\[|\[[^]]+\]:)")
_HEADING = re.compile(rb"^#{1,6} ")
# 每次真的改寫前，兩份原檔再各存一份到庫內部產物的落點；只留最近幾批。
# 首份備份（_MEMORY_BACKUP.md）只寫一次、永不覆蓋，管不到之後每一晚的改寫。
BACKUP_DIRECTORY = "memory_index_backups"
BACKUPS_KEPT = 7
LOSS_REASON = "多重集合核對失敗：{count} 行搬出後在完整索引裡找不到；不寫"
BOUND_REASON = "{name} 帶 rule_provenance（來源綁定），改內容會讓綁定失效；要人改完再用 rule_pipeline.bind_payload 重綁"
INVALID_REASON = "改寫後 {name} 會被規則檢查判成 INVALID（{issues}）；不寫"
CHANGED_REASON = "{name} 在規劃之後被改過；這一次整份放棄"


class CannotFit(ValueError):
    """留下的手寫檔首加指標就超過上限：要人決定，不硬寫。"""


class Refused(ValueError):
    """來源綁定、改寫後會變 INVALID，或核對出會掉行：不寫，交給人。"""


def _fence_line(line, fence):
    if fence:
        match = re.fullmatch(rb" {0,3}([`~]+)[ \t]*(?:\r?\n)?", line)
        closed = match and set(match[1]) == {fence[0]} and len(match[1]) >= fence[1]
        return (None if closed else fence), True
    match = re.match(rb" {0,3}(`{3,}|~{3,})([^\r\n]*)", line)
    if match and (match[1][0] != ord("`") or b"`" not in match[2]):
        return (match[1][0], len(match[1])), True
    return None, False


def legacy_cap_notice(config=None):
    """One line when an old index cap key is still set; None otherwise."""
    config = memspec.config_options() if config is None else config
    keys = [key for key in LEGACY_CAP_KEYS if key in config]
    return LEGACY_CAP_NOTICE.format(keys="、".join(keys)) if keys else None


@dataclass(frozen=True)
class Limit:
    max_bytes: int = HOST_INDEX_MAX_BYTES
    max_lines: int = HOST_INDEX_MAX_LINES
    notice: object = None

    def exceeded(self, raw):
        return len(raw) > self.max_bytes or line_count(raw) > self.max_lines


def line_count(raw):
    """Count lines the way memory_lint does (str.splitlines)."""
    return len(raw.decode("utf-8", errors="replace").splitlines())


def limit_of(vault=None, config=None):
    """The one limit every caller (memory-index, card_lint, dream, fixed_load) uses."""
    return Limit(HOST_INDEX_MAX_BYTES, HOST_INDEX_MAX_LINES, legacy_cap_notice(config))


def _without_pointer(raw):
    # Native memory writers commonly append after our pointer.
    return raw.replace(POINTER.replace(b"\n", b"\r\n"), b"").replace(POINTER, b"")


def _is_link(path):
    """lstat also sees Windows junctions on Python 3.11 (no Path.is_junction)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


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
        fence, fenced = _fence_line(line, fence)
        if fenced:
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
    # 規劃當下完整索引的位元組（不存在＝None；...＝沒記）；apply_plan 用它判斷中途有沒有人改過。
    full_original: object = ...
    cap_lines: int = HOST_INDEX_MAX_LINES
    notice: object = None

    def summary(self):
        row = dict(vault=str(self.vault), before_bytes=len(self.original),
                   before_lines=line_count(self.original), after_bytes=len(self.short),
                   after_lines=line_count(self.short), kept_lines=len(self.kept),
                   moved_lines=len(self.moved), cap_bytes=self.cap, cap_lines=self.cap_lines,
                   status="would-rewrite" if self.short != self.original else "unchanged")
        if self.notice:
            row["notice"] = self.notice
        return row


def plan(vault, config=None):
    vault = Path(vault).expanduser().resolve()
    index = vault / memspec.MEMORY_INDEX_FILENAME
    if _is_link(index):
        raise ValueError(f"index input must not be a link: {index}")
    raw = index.read_bytes()
    full = vault / FULL_FILENAME
    full_original = full.read_bytes() if full.exists() and not _is_link(full) else None
    raw.decode("utf-8")  # Reject invalid encoding before any file is written.
    limit = limit_of(vault, config)
    cap = limit.max_bytes
    source = _without_pointer(raw)
    lines = source.splitlines(keepends=True)
    extra = dict(cap_lines=limit.max_lines, notice=limit.notice)
    if not limit.exceeded(raw):
        return Plan(vault, raw, raw, list(range(len(lines))), [], cap, full_original, **extra)
    required, units = _units(lines)
    kept = _keep(vault, lines, required, units, limit)
    moved = [i for i in range(len(lines)) if i not in kept]
    short = b"".join(lines[i] for i in kept) + POINTER
    return Plan(vault, raw, short, kept, moved, cap, full_original, **extra)


def _link_paths(vault, text, names):
    """Card paths one unit links to. Markdown links are relative to the vault and may be
    percent-encoded; a wikilink names a card anywhere in the vault (unique name only)."""
    from urllib.parse import unquote
    for match in _LINK_TARGET.finditer(text.decode("utf-8", errors="replace")):
        if match[3] is None:
            yield vault / unquote(match[1] or match[2])
            continue
        name = match[3].strip()
        name = name if name.casefold().endswith(".md") else name + ".md"
        direct = vault / name
        if direct.is_file():
            yield direct
            continue
        if not names:
            try:
                from . import cardscan
            except ImportError:
                import cardscan
            names[""] = []  # built marker: an empty vault is walked once, not per link
            for card in cardscan.card_files(vault):
                names.setdefault(card.name.casefold(), []).append(card)
        found = names.get(Path(name).name.casefold()) or []
        if len(found) == 1:
            yield found[0]


def _stale_card(vault, text, names=None):
    """True when a card this unit links to is superseded, closed, archived or expired.

    `names` is a per-plan cache (pass {}) for resolving wikilinks to cards in subfolders."""
    names = {} if names is None else names
    for candidate in _link_paths(vault, text, names):
        try:
            path = candidate.resolve()
            path.relative_to(vault)
        except (OSError, ValueError):
            continue
        if not path.is_file() or _is_link(path):
            continue
        fields, _problem = memspec.frontmatter_fields(path)
        status = str(fields.get(memspec.DECISION_STATUS_FIELD, "")).strip().casefold()
        if (status in _STALE_STATUSES
                or str(fields.get(memspec.SUPERSEDED_BY_FIELD, "")).strip()
                or any(memspec.card_expired(fields.get(field)) for field in memspec.CARD_EXPIRY_FIELDS)):
            return True
    return False


def _keep(vault, lines, required, units, limit):
    """Move the fewest units until the host load limit holds; nothing moves below it.

    Units linking to invalid cards leave first (bottom-up), then the rest from the
    bottom. Headings stay while any unit under them stays.
    """
    in_context = {h for _ids, context in units for h in context}
    unit_lines = {i for ids, _context in units for i in ids}
    # Heading lines that own no unit (empty sections) are not ours to move.
    fixed = set(required) | {i for i in range(len(lines))
                             if i not in unit_lines and i not in in_context}

    def short_of(kept_units):
        keep = set(fixed)
        for ids, context in kept_units:
            keep.update(ids)
            keep.update(context)
        return sorted(keep)

    def fits(keep):
        return not limit.exceeded(b"".join(lines[i] for i in keep) + POINTER)

    base = short_of(())
    if not fits(base):
        raise CannotFit(f"{vault}: handwritten header/reference definitions plus pointer exceed the "
                         f"host load limit ({limit.max_lines} lines / {limit.max_bytes} bytes); edit the header.")
    order = list(reversed(range(len(units))))
    names = {}
    stale = [n for n in order if _stale_card(vault, b"".join(lines[i] for i in units[n][0]), names)]
    removal = stale + [n for n in order if n not in set(stale)]
    remaining = set(range(len(units)))
    for n in removal:
        keep = short_of(units[k] for k in sorted(remaining))
        if fits(keep):
            return keep
        remaining.discard(n)
    return short_of(units[k] for k in sorted(remaining))


def _archive_blocks(source, *, archived=False):
    """Pair complete text units with their heading ancestry, excluding code fences."""
    lines = source.splitlines(keepends=True)
    blocks, headings = [], {}
    start, context, fence = 0, (), None
    for i, line in enumerate(lines):
        fence, fenced = _fence_line(line, fence)
        if fenced:
            continue
        heading = _HEADING.match(line)
        boundary = heading or (i > start and (_ENTRY.match(line) or
                    (not lines[i - 1].strip() and line.strip())))
        if not boundary:
            continue
        if start < i:
            blocks.append((context, b"".join(lines[start:i])))
        if heading:
            if archived and line.rstrip(b"\r\n") == ROOT_ADDITIONS.rstrip(b"\n"):
                headings = {}
            else:
                level = len(line) - len(line.lstrip(b"#"))
                headings = {k: v for k, v in headings.items() if k < level}
                headings[level] = line
                blocks.append((tuple(headings.values()), b""))
            start = i + 1
        else:
            start = i
        context = tuple(headings.values())
    if start < len(lines):
        blocks.append((context, b"".join(lines[start:])))
    return blocks


def _archive_key(block):
    context, payload = block
    # A separator after an unterminated unit must not make every rerun append it.
    return (tuple(line.rstrip(b"\r\n") for line in context), bool(payload),
            payload.replace(b"\r\n", b"\n").rstrip(b"\n"))


def _full_payload(previous, source):
    """Retain the original archive; append new units in their source heading context.

    Context is part of identity: moving an unchanged item to another section is
    a new occurrence there. Counts preserve deliberate duplicate items.
    """
    if previous is None:
        return FULL_HEADER + source
    if not previous.startswith(FULL_HEADER):
        raise ValueError(f"{FULL_FILENAME} already exists and is not an Epitype full index")
    available = Counter(_archive_key(block) for block in
                        _archive_blocks(previous[len(FULL_HEADER):], archived=True))
    added = []
    context = None
    for block in _archive_blocks(source):
        key = _archive_key(block)
        if available[key]:
            available[key] -= 1
        else:
            ancestry, payload = block
            if ancestry != context or not payload:
                for heading in ancestry or (ROOT_ADDITIONS,):
                    added.append(heading if heading.endswith(b"\n") else heading + b"\n")
                context = ancestry
            if payload:
                added.append(payload)
                if not payload.endswith(b"\n"):
                    added.append(b"\n")
    if not added:
        return previous
    # A separator preserves an unterminated last line rather than concatenating
    # it with a newly appended entry. The original bytes remain untouched.
    separator = b"\n" if previous and not previous.endswith((b"\n", b"\r")) else b""
    return previous + separator + b"".join(added)


def _sha(raw):
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _unchanged_since_plan(proposed, index, full):
    """兩份檔與規劃當下相同（比雜湊）；不同就丟 CardConflict，什麼都不寫。"""
    if _sha(index.read_bytes()) != _sha(proposed.original):
        raise card_io.CardConflict(CHANGED_REASON.format(name=index.name))
    if proposed.full_original is not ...:
        current = full.read_bytes() if full.exists() else None
        if _sha(current) != _sha(proposed.full_original):
            raise card_io.CardConflict(CHANGED_REASON.format(name=full.name))


def _nonblank(raw):
    return Counter(line.rstrip(b"\r\n") for line in raw.splitlines() if line.strip())


def _verify_lossless(proposed, payload):
    """每一行非空白原文：要嘛還在短索引，要嘛在完整索引裡（逐字、照次數）。"""
    source = _without_pointer(proposed.original).splitlines(keepends=True)
    kept = b"".join(source[i] for i in proposed.kept)
    moved = b"".join(source[i] for i in proposed.moved)
    if (_nonblank(b"".join(source)) != _nonblank(kept) + _nonblank(moved)
            or _nonblank(_without_pointer(proposed.short)) != _nonblank(kept)):
        raise Refused(LOSS_REASON.format(count=len(proposed.moved)))
    missing = _nonblank(moved) - _nonblank(payload)
    if missing:
        raise Refused(LOSS_REASON.format(count=sum(missing.values())))


def _binding_issue(path, before, after):
    """有 rule_provenance 或改寫後會被 rule_pipeline 判成 INVALID 就回原因，否則 None。"""
    try:
        from . import rule_pipeline, rule_provenance
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from epitype import rule_pipeline, rule_provenance
    after_text = after.decode("utf-8-sig")
    fields, _problem = memspec.frontmatter_text(after_text)
    old_fields, _problem = memspec.frontmatter_text((before or b"").decode("utf-8-sig", errors="replace"))
    if rule_provenance.FIELD in (fields or {}) or rule_provenance.FIELD in (old_fields or {}):
        return BOUND_REASON.format(name=path.name)
    result = rule_pipeline.inspect_card(fields or {}, path, card_text=after_text)
    if result.status == rule_provenance.INVALID:
        return INVALID_REASON.format(name=path.name, issues="; ".join(result.issues[:3]))
    return None


def _backup(vault, files):
    root = Path(vault) / memspec.DREAM_DIRECTORY / BACKUP_DIRECTORY
    target = root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target.mkdir(parents=True, exist_ok=True)
    for path in files:
        if path.exists():
            shutil.copy2(path, target / path.name)
    for stale in sorted(item for item in root.iterdir() if item.is_dir())[:-BACKUPS_KEPT]:
        shutil.rmtree(stale, ignore_errors=True)
    return target


def apply_plan(proposed):
    """Archive first, then compare/replace the short index; never touch a card.

    寫入前依序：兩份檔與規劃當下比雜湊、多重集合核對、來源綁定／INVALID 檢查、備份，
    備份後再比一次雜湊；任一關不過就丟例外，什麼都不寫。回傳這一次的備份目錄（沒寫就 None）。
    """
    vault = proposed.vault
    index = vault / memspec.MEMORY_INDEX_FILENAME
    backup = vault / BACKUP_FILENAME
    full = vault / FULL_FILENAME
    for target in (index, backup, full):
        if _is_link(target):
            raise ValueError(f"index output must not be a link: {target}")
    _unchanged_since_plan(proposed, index, full)
    if proposed.short == proposed.original and not full.exists():
        return None
    previous = full.read_bytes() if full.exists() else None
    payload = _full_payload(previous, _without_pointer(proposed.original))
    _verify_lossless(proposed, payload)
    for path, before, after in ((index, proposed.original, proposed.short), (full, previous, payload)):
        if after != before:
            issue = _binding_issue(path, before, after)
            if issue:
                raise Refused(issue)
    snapshot = None
    if payload != previous or proposed.short != proposed.original:
        snapshot = _backup(vault, (index, full))
        _unchanged_since_plan(proposed, index, full)
    # publish never overwrites even if another process wins the first-backup race.
    if not backup.exists():
        try:
            card_io.publish(backup, proposed.original)
        except FileExistsError:
            pass
    if previous is None and backup.read_bytes() != proposed.original:
        raise ValueError(f"{BACKUP_FILENAME} already exists and is not this index's first backup")
    if previous is None:
        card_io.publish(full, payload)
    elif payload != previous:
        card_io.replace_if_unchanged(full, payload, previous)
    if proposed.short != proposed.original:
        card_io.replace_if_unchanged(index, proposed.short, proposed.original)
    if index.read_bytes() != proposed.short or full.read_bytes() != payload:
        raise OSError(f"index readback failed: {vault}; original retained at {backup}")
    return snapshot


def discover(projects):
    """Only direct native project memory directories; do not traverse links."""
    root = Path(projects).expanduser()
    if not root.is_dir():
        raise NotADirectoryError(root)
    found = []
    for project in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if _is_link(project) or not project.is_dir():
            continue
        memory = project / "memory"
        if _is_link(memory):
            continue
        if memory.is_dir():
            found.append(memory)
    return found


def describe(row):
    """One line for an over-limit row: actual size against the limit that applies."""
    return (f"{row['before_bytes']} bytes / {row['before_lines']} lines > host load limit "
            f"{row['cap_bytes']} bytes or {row['cap_lines']} lines")


def lint(vault, config=None):
    index = Path(vault) / memspec.MEMORY_INDEX_FILENAME
    limit = limit_of(vault, config)
    raw = index.read_bytes()
    row = dict(vault=str(Path(vault).resolve()), before_bytes=len(raw),
               before_lines=line_count(raw), cap_bytes=limit.max_bytes, cap_lines=limit.max_lines,
               status="over-cap" if limit.exceeded(raw) else "unchanged")
    if limit.notice:
        row["notice"] = limit.notice
    return row


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
