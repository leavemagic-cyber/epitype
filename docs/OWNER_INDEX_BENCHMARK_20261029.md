# Owner index measurement — 2026-10-29

Historical EOPT2 baseline. The extraction, duplication and prefix-checking
results below are superseded by [the EOPT2B real-data report](OWNER_INDEX_EOPT2B_20261029.md).

Measured code: `a5a58bac5e6cbeb6fe376e086f47cb9af3d82223`, on the isolated
`opt/owner-index-20261029` worktree. Python 3.13.15, SQLite 3.50.4;
optional `zstandard` was available. No transcript, live vault or live Epitype
configuration was changed by the build or tests.

## Real source scope and results

Discovery covered Claude project transcripts/backups, Codex sessions and archived
sessions, and three cold-storage zip files. It found 5,236 physical files totaling
25,832,315,908 bytes. Zip members become separate index sources. Decompression
means the bytes scanned below exceed the physical storage size. Sources remained
active during measurement; this is a sequence of per-file snapshots.

| Metric | Fresh build | Immediately following update |
| --- | ---: | ---: |
| Elapsed seconds | 1,839.988 | 15.116 |
| Indexed source entries after operation | 6,811 | 6,814 |
| Owner message rows after operation | 28,307 | 28,322 |
| New source entries | 6,811 | 3 |
| Appended source entries | 0 | 11 |
| Unchanged source entries | 0 | 6,800 |
| Whole-source rereads | 0 | 0 |
| Added message rows | 28,307 | 15 |
| Complete-row bytes scanned | 41,593,291,210 | 15,206,203 |
| Old prefix bytes checked | 0 | 1,998,964,057 |
| Database bytes after close | 372,084,736 | 372,084,736 |
| Unreadable files/archives | 0 | 0 |
| Malformed rows | 0 | 0 |
| Sources skipped for missing zstd | 0 | 0 |

The resulting database is 372.1 MB (decimal). It is retained only in the
worktree's ignored `.epitype/eopt2/owner-index.sqlite3`, alongside the detailed
`measurement.json` and reproduction script `measure.py`. The database was fresh
at the beginning of this measurement. Earlier provisional results were discarded
after correcting owner attribution; there is one retained database.

The supplied reference measured a 536-second build and a 15 MB JSONL output with
6,591 source entries. This SQLite build took longer. The source set and output
formats differ: this implementation retains full owner text and every physical
provenance occurrence, validates all JSON rows, and maintains the trigram index.
Their individual timing costs were not isolated. These measurements do not
establish a full-build speed improvement over the reference script.

## Incremental I/O boundary

Unchanged plain transcripts are skipped before opening them. An appended source
currently has its old complete prefix hashed, followed by parsing its new rows.
This detects an earlier rewrite combined with file growth. Size, mtime and an
offset alone cannot distinguish that operation from a pure append.

Consequently the update above parsed only 15.2 MB of new complete rows but also
read approximately 2.0 GB of old prefixes. It does **not** literally read only
new bytes. The owner was asked to choose between complete prefix checking and
append-only I/O that can miss simultaneous earlier rewrites; no response had
arrived when this report was prepared. The current implementation retains full
checking. This unresolved tradeoff must remain explicit at delivery.

## Validation

Latest final-code focused checks:

| Command | Result |
| --- | --- |
| `python tests/owner_index_regression.py` | 17/17 passed |
| `python tests/source_lookup_regression.py` | 10/10 passed |
| `python tests/transcript_provenance_regression.py` | 9/9 passed |
| `python tests/language_regression.py` | 27/27 passed |
| `python tests/package_smoke.py --selftest` | 2/2 passed |

`python tests/run_all.py` completed with `TOTAL PASS 146/146` component suites
and overall exit 1 from the final registration/privacy checks. This full run
began before the last focused corrections and included the then-current
16-test owner suite; the final 17-test owner suite and 10-test source suite were
rerun after those corrections. No existing test was removed or weakened.

The final full-run failures were reproduced by executing the original checks
on a pristine archive of both the base and current master,
`97ab94dbdc8b6f5ccf660ea0aa321ecfc6429305`:

- `tests/shim_dispatch_regression.py` is absent from the suite registration.
- Privacy scan reports three existing fixture findings:
  `tests/card_lint_body_noise_regression.py:32` (`session-uuid`),
  the same file at line 33 (`windows-home`), and
  `tests/dream_vault_snapshot_regression.py:29` (`email`).

The base's registration function returned the same unregistered file, and its
privacy CLI exited 1 with the same three findings across 279 tracked files.
The task run scanned 282 tracked files; none of the task's changed files was
flagged. The isolated base copy was removed after the comparison; the result
and reproducible script remain in ignored `master-check.json` and
`check_master.py`. Unrelated fixtures/registration were left unchanged.

After the final build/update, `python .epitype/eopt2/audit_index.py` checked
12 original row hashes at stored byte offsets: three each for ordinary U,
archived U, Q and S. All 12 matched. SQLite `integrity_check` and the FTS5
external-content `integrity-check` both returned success. The small result is
retained in ignored `audit.json`; this is a sample of original rows, not an
independent provenance validation of every message.

## Example and provenance

For a typed or human-queued message containing
`copied assistant text《只索引這一段`, only `只索引這一段` enters the owner-text
search field. A selected question option retains role S and separate question
context. `source --find ... --index ...` now locates these rows without a global
transcript scan; the original source-file command still rereads the original
bytes. Indexed hits remain `verified: false`, and finding a sentence never
establishes a current owner decision or grants authorization.
