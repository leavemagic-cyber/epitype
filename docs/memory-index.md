# Short memory indexes

`epitype memory-index` keeps each vault's `MEMORY.md` within the same raw UTF-8
byte budget, including its header and CRLF line endings. The default is **1,526
bytes**. Set `index_cap_bytes` in `~/.epitype/config.json` to change it:

```json
{"index_cap_bytes": 1526}
```

The old `shared_index_cap_bytes` key is accepted as a migration alias, now for
every vault. `index_cap_bytes` takes precedence if both exist. A missing setting
uses the same default on a new installation; there is no machine-specific mode.
Core rule budgets and resident rule blocks are unaffected.

The default reuses the established shared-index target. A read-only measurement
of 15 existing indexes on 2026-11-08 left 9 unchanged and reduced the combined
index footprint from 36,552 to 15,666 bytes by moving 92 lines in 6 indexes. This
is a byte measurement, not an estimated token count. The budget is configurable
because a vault's handwritten introduction can itself be larger than the default.

## Preview, lint, apply

```sh
epitype memory-index /path/to/vault
epitype memory-index /path/to/vault --check
epitype memory-index /path/to/vault --apply
epitype memory-index --projects /path/to/.claude/projects --json
epitype memory-index --projects /path/to/.claude/projects --apply
```

Without `--apply`, the command writes nothing. `--check` exits 1 for an index
over the cap, 2 for an input/configuration error, and 0 otherwise. Missing indexes
are reported as `no-index`; they are not created. Without paths, the command uses
the configured `vaults`. `--projects` includes every direct `*/memory` directory,
including ones with no index, and does not enter linked project/memory directories.
`epitype cards <vault> --deep` and the nightly cap report use the same raw-file
budget. The pre-existing host sync still checks its loaded global index region
against this same setting; that region excludes the title, so a raw-file check
can report an overage even when the host region is within budget.

## Selection and preservation

Only over-cap indexes are shortened. The handwritten introduction (before the
first list entry or next heading) and reference-link definitions stay verbatim.
The remaining units are ranked deterministically: those containing `⚠` first,
then the newest explicit `YYYY-MM-DD`, `YYYY_MM_DD` or `YYYYMMDD` date in the unit,
then the later source position. Dates are textual index dates, not card mtimes.
Each whole unit that fits is selected, with its heading hierarchy, and selected
units retain their original order. List continuations, prose paragraphs and
fenced blocks stay together. The generated full-index link counts against the
budget. If the required handwritten introduction/reference definitions and link
cannot fit, the command reports the required size and leaves that vault unchanged;
other vaults still run. It does not truncate handwriting to claim success.

Before the first rewrite, `_MEMORY_BACKUP.md` receives the exact original bytes
and is never overwritten. `_MEMORY_FULL.md` contains the entire original index,
not just the moved subset, so its original headings and reference definitions
also accompany all moved lines. Both are siblings of `MEMORY.md`; relative links
retain their base, and their underscore names keep them out of card/recall scans.
Cards and `_views/` are never modified by this command.

On later runs the full index preserves its existing text and appends any new
line occurrences from the current index (including duplicates). Managed pointer
text is excluded from that archive. Native writers may append before or after
the pointer. An unchanged rerun does not rewrite the index, backup or archive.
This archive is deliberately additive: obsolete or repeated historic entries
are retained rather than silently removed or reconciled as current decisions.

Archive publication precedes replacement of the short index. Existing Epitype
compare-and-replace writes reject a stale index or changed generated archive.
If publication fails, the original index remains; if the index changes after
archiving, the retained archive/backup can contain extra text but nothing is
discarded. No cross-file atomic transaction is claimed. An unrelated existing
`_MEMORY_FULL.md` or linked output is refused rather than overwritten.

The index command is separate from `sync`, so operators can review its preview
before explicitly applying it. Updating host rule/index regions remains the job
of the existing sync command. The index command never edits a resident core block.
