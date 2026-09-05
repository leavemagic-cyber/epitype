import sys; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 consoles.
"""One card, one reading: the index, the lints, and the action gate must agree
on where a card's frontmatter ends and what its fields say.

Five card shapes that used to split the readers — a BOM with CRLF, a `...`
document end, a duplicated key, a `|-` block scalar, and a quoted '#' — are
parsed by every reader and compared field by field."""

from datetime import date
from pathlib import Path
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "adapters" / "claude") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "adapters" / "claude"))

from epitype import decision_lint, memsearch, memspec, pending_lint, scar_census  # noqa: E402
import pretooluse_gate  # noqa: E402

CARDS = {
    "bom-crlf.md": "﻿---\r\nname: bom card\r\ndescription: 2026-06-01 待辦 written in frontmatter\r\nstatus: active\r\n---\r\nbody line\r\n",
    "document-end.md": "---\nname: dots card\ndescription: closes with dots\nstatus: active\n...\n2026-06-01 待辦 body zombie\n",
    "duplicate-key.md": "---\nname: dup card\ndescription: first wins\nstatus: active\nstatus: superseded\n---\nbody\n",
    "block-scalar.md": "---\nname: block card\ndescription: |-\n  first line\n  second line\nstatus: active\n---\nbody\n",
    "quoted-hash.md": '---\nname: hash card\ndescription: "keeps # inside quotes"\nstatus: active\n---\nbody\n',
}
EXPECTED = {
    "bom-crlf.md": ("bom card", "2026-06-01 待辦 written in frontmatter", "active", 3),
    "document-end.md": ("dots card", "closes with dots", "active", 3),
    "duplicate-key.md": ("dup card", "first wins", "active", 4),
    "block-scalar.md": ("block card", "first line\nsecond line", "active", 5),
    "quoted-hash.md": ("hash card", "keeps # inside quotes", "active", 3),
}

# U38: memspec.frontmatter_fields is the ported primitive behind
# decision_lint._parse_frontmatter's thin wrapper — every hook and lint that used
# to keep its own copy now calls one of these two. These fixtures add the shapes
# EXPECTED above does not exercise (tab indent, no frontmatter at all) for the
# parity check below; the oversized-file shape is generated at test time instead
# of stored here.
MEMSPEC_PARITY_EXTRA_CARDS = {
    "tab-indent.md": "---\nname: tab card\ndescription: ok\n\tstatus: active\n---\nbody\n",
    "no-frontmatter.md": "not a card\njust text\n",
}


def _readings(path):
    """(name, description, status, frontmatter line count) as each reader sees them."""
    indexed = memsearch._read_card(path)
    linted, problem = decision_lint._parse_frontmatter(path)
    gate_lines = pretooluse_gate._frontmatter(path)
    census, _body = scar_census._frontmatter_fields(path)
    front_lines, _closing = memspec.split_frontmatter(path.read_text(encoding="utf-8"))
    return {
        "index": (indexed["name"], indexed["description"], indexed[memspec.DECISION_STATUS_FIELD], len(front_lines)),
        "decision_lint": (
            linted.get("name", ""),
            linted.get("description", ""),
            linted.get(memspec.DECISION_STATUS_FIELD, ""),
            len(front_lines),
        ),
        "gate": (linted.get("name", ""), linted.get("description", ""), linted.get(memspec.DECISION_STATUS_FIELD, ""), len(gate_lines)),
        "scar_census": (census.get("name", ""), linted.get("description", ""), linted.get(memspec.DECISION_STATUS_FIELD, ""), len(front_lines)),
    }, problem


def _selftest():
    checks = []
    with tempfile.TemporaryDirectory(prefix="epitype-frontmatter-") as temp_dir:
        vault = Path(temp_dir).resolve()
        for name, text in CARDS.items():
            (vault / name).write_bytes(text.encode("utf-8"))
        for name, text in MEMSPEC_PARITY_EXTRA_CARDS.items():
            (vault / name).write_bytes(text.encode("utf-8"))
        oversized_value = "x" * (2 * 1024 * 1024)
        (vault / "oversized.md").write_bytes(
            f"---\nname: big card\ndescription: {oversized_value}\nstatus: active\n---\nbody\n".encode("utf-8")
        )
        parity_names = [*CARDS, *MEMSPEC_PARITY_EXTRA_CARDS, "oversized.md"]
        parity_mismatches = [
            name
            for name in parity_names
            if memspec.frontmatter_fields(vault / name) != decision_lint._parse_frontmatter(vault / name)
        ]
        checks.append((
            "memspec.frontmatter_fields matches decision_lint._parse_frontmatter's wrapper "
            "on every fixture (dup key, block scalar, CRLF, BOM, tab indent, no frontmatter, oversized)",
            not parity_mismatches,
        ))
        if parity_mismatches:
            print(f"    mismatched fixtures: {parity_mismatches!r}", file=sys.stderr)
        for name, expected in EXPECTED.items():
            readings, problem = _readings(vault / name)
            agreed = all(value == expected for value in readings.values())
            checks.append((f"{name}: every reader sees {expected[:3]!r} over {expected[3]} frontmatter lines", agreed))
            if not agreed:
                for reader, value in readings.items():
                    print(f"    {reader}: {value!r}", file=sys.stderr)
        checks.append((
            "the duplicated key is reported by the lint, not silently accepted",
            "重複欄位" in (decision_lint._parse_frontmatter(vault / "duplicate-key.md")[1] or ""),
        ))
        pending = pending_lint.scan_vault(vault, today=date(2026, 9, 5))
        zombies = {card["path"]: [item["line"] for item in card["lines"]] for card in pending["cards"]}
        checks.append((
            "pending lint never reads frontmatter as a to-do and always reads the body after `...`",
            zombies == {"document-end.md": [6]},
        ))
        memsearch.build_index(vault)
        checks.append((
            "the index searches the block scalar's text and calls the duplicated card active",
            memsearch.query_index(vault, "second line")["count"] == 1
            and memsearch.query_index(vault, "first wins")["count"] == 1,
        ))
    passed = sum(bool(ok) for _, ok in checks)
    total = len(EXPECTED) + 4
    for label, ok in checks:
        if not ok:
            print(f"FAILED: {label}")
    print(f"SELFTEST {'PASS' if passed == total and len(checks) == total else 'FAIL'} {passed}/{total}")
    return 0 if passed == total and len(checks) == total else 1


if __name__ == "__main__":
    if "--selftest" not in sys.argv[1:]:
        print("usage: frontmatter_consistency.py --selftest", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(_selftest())
