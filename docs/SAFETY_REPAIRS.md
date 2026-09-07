# Safety repair contracts

These repairs preserve existing cards and the hooks' fail-open behavior. They do
not migrate a vault or change installed settings on import.

- Harvest reevaluation of an already correctly placed card is a no-op. Quarantine,
  duplicate and rehome destinations cannot overwrite another card, including a
  writer that arrives after the filename check. A failed move restores the source
  or reports the retained `.recovery` path if a new writer already owns that name.
  Publication uses a hard link on the destination filesystem; unsupported filesystems
  fail without an unsafe replacement fallback.
- Alias suggestions containing control characters or line separators are rejected.
  Quotes, commas and comment markers round-trip through both list formats. Alias
  and derived-date writers serialize with the existing card lock and reject a stale
  source snapshot. Arbitrary editors must honor that lock for full serialization;
  the content check is not an operating-system compare-and-swap. Search index format
  6 rebuilds parser-derived data from the retained cards.
- Nightly relocation updates the registered command as well as `repo_root`.
  Doctor checks the actual executable, script, arguments and daily time. Rollback
  preserves later owner edits and reports conflicts. Crontab read failures never
  authorize replacing an unknown table; only a recognized missing-table result
  counts as empty. The system crontab CLI has no atomic compare-and-swap with
  unrelated external writers.
- Live capture resolves the preceding assistant question before choosing exactly
  one event kind, using the replay classifier. Non-record JSON transcript lines
  cannot interrupt that selection.
- Dream state retains per-section and per-vault errors. `completed_at` means the
  attempt finished; `complete` means all checks finished. Partial and legacy unknown
  results cannot produce a clean notice. Partial runs retain interval throttling.
  A persistent OS-locked guard serializes lease updates; only an expired dead owner
  can be replaced. A token and PID identify the holder, and parent-to-child handoff
  is single-use. The guard file must not be deleted or replaced while in use.
- Commitment mutation results contain only persisted changes. Hooks retain their
  fail-open API; CLI read/write/lock failures return a nonzero status. Empty existing
  vaults remain valid no-ops. Writers refuse incomplete or unrecognized ledgers
  rather than silently deleting rows that readers can skip.
- Stop's cache supplies discovery hints only. Returned decision fields come from
  one bounded snapshot read during that invocation, even if size and timestamps
  are unchanged. Each call reads at most 30 discovery heads and 30 known decision
  heads, subject to the existing deadline. Negative entries rotate: a metadata-
  preserving promotion may take `ceil(negative cards / 30)` calls in a stable vault
  with sufficient time; changed-card backlogs or exhausted deadlines can extend
  that delay. A new cache is discovered incrementally. Existing v2 hints survive
  upgrade, but their authority values are never trusted.

Run `python tests/run_all.py` for the existing suite and all eleven new regression
groups. Tests use synthetic vaults and fake scheduler mutations. Native scheduler
execution and operating systems not actually tested require their own evidence.
