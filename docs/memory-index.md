# Short memory indexes

`epitype memory-index` keeps each vault's `MEMORY.md` within the host's real
load limit, not a number Epitype chose. Claude Code loads only **the first 200
lines or the first 25 KB** of `MEMORY.md` at session start, whichever comes first;
the rest is never read ([Claude Code memory](https://code.claude.com/docs/en/memory)).
Epitype counts lines as `str.splitlines()` does and bytes as the raw file, including
its header, BOM and CRLF line endings. The limit is 200 lines or 25,000 bytes and
applies equally to project vaults and the shared governance vault (owner decisions
2026-10-10 and 2026-11-17). It is defined once, as `HOST_INDEX_MAX_LINES` /
`HOST_INDEX_MAX_BYTES` in `epitype/memory_index.py`; change it only if Claude Code
changes its load limit.

No configuration key can lower it. The old `index_cap_bytes` and
`shared_index_cap_bytes` keys (1,526 bytes by default before 2026-11-17) are still
parsed so old configurations keep working, but they are ignored: when either is
set, the command, the nightly cap report and `epitype sync` print one line saying
it is no longer used. `core_cap_bytes` is ignored the same way; core rules are compared with their last
accepted size instead (`epitype core-baseline`).

## Preview, lint, apply

```sh
epitype memory-index /path/to/vault
epitype memory-index /path/to/vault --check
epitype memory-index /path/to/vault --apply
epitype memory-index --projects /path/to/.claude/projects --json
epitype memory-index --projects /path/to/.claude/projects --apply
```

Without `--apply`, the command writes nothing. `--check` exits 1 for an index
over the host limit, 2 for an input/configuration error, and 0 otherwise. Missing indexes
are reported as `no-index`; they are not created. Without paths, the command uses
the configured `vaults`. `--projects` includes every direct `*/memory` directory,
including ones with no index, and does not enter linked project/memory directories.
`epitype cards <vault> --deep`, the nightly cap report and `epitype fixed-load`
use the same limit function (`memory_index.limit_of`). Host sync no longer limits
the shared index region by a configured number; it judges the whole host file by
the host's own behaviour (see [Architecture](ARCHITECTURE.md)). File shortening
and syncing resident host regions are separate operations.

## Selection and preservation

Only indexes over the host limit are shortened; below it nothing moves, and there
is no slimming to a smaller target. The handwritten introduction (before the first
list entry or next heading) and reference-link definitions stay verbatim. Units
then leave by validity, not position: first those linking to a card that is
superseded, closed, retired, archived, deprecated or historical, has
`superseded_by`, or whose `expires_at`/`valid_until` has passed (bottom-up), then
the remaining units from the bottom, until the short index plus the full-index
link fits. Kept units retain their original order and heading hierarchy. List
continuations, prose paragraphs and fenced blocks stay together. If the required
handwritten introduction/reference definitions and link cannot fit, the command
reports it and leaves that vault unchanged; other vaults still run. It does not
truncate handwriting to claim success.

Before the first rewrite, `_MEMORY_BACKUP.md` receives the exact original bytes
and is never overwritten. `_MEMORY_FULL.md` contains the entire original index,
not just the moved subset, so its original headings and reference definitions
also accompany all moved lines. Both are siblings of `MEMORY.md`; relative links
retain their base, and their underscore names keep them out of card/recall scans.
Cards and `_views/` are never modified by this command.
An unrelated existing first-backup file is reported instead of silently accepting
it as a backup of the index being rewritten. Files beside project directories
are skipped during project discovery.

On later runs the full index preserves its existing text and appends new text
units with their complete source heading ancestry. An item moved from Active to
Closed is retained under each historical heading; an added Active item is never
appended beneath the archive's last Closed heading. Sources without a heading
use an explicitly labelled unsectioned-additions heading. Duplicate occurrences
are counted within their heading context. Newline normalization is used only for
matching; original saved bytes are not rewritten. Managed pointer
text is excluded from that archive. Native writers may append before or after
the pointer, or change it to Windows CRLF. An unchanged rerun does not rewrite
the index, backup or archive. Discovery excludes Windows junctions on Python
3.11 as well as newer runtimes.
This archive is deliberately additive: obsolete or repeated historic entries
are retained rather than silently removed or reconciled as current decisions.

The optional `test` extra supplies Hypothesis and markdown-it-py for generated
edit/retry sequences and independent heading/list checks. They are not runtime
dependencies. Run `python tests/reliability_sequence_regression.py --selftest`
after `python -m pip install ".[test]"`. Test caches live in disposable directories.

Archive publication precedes replacement of the short index. Existing Epitype
compare-and-replace writes reject a stale index or changed generated archive.
If publication fails, the original index remains; if the index changes after
archiving, the retained archive/backup can contain extra text but nothing is
discarded. No cross-file atomic transaction is claimed. An unrelated existing
`_MEMORY_FULL.md` or linked output is refused rather than overwritten.

The index command is separate from `sync`, so operators can review its preview
before explicitly applying it. Updating host rule/index regions remains the job
of the existing sync command. The index command never edits a resident core block.
