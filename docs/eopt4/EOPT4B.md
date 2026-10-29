# EOPT4B: injection recovery and source preparation

Measured implementation: `5806612d629882aee3d0e87d88182a4b72a13ad9`.
All four ticket cards reached all three fresh-session injections: 12/12.
The final 132 core calls and nine actual hook processes emitted context, with
maximum elapsed times of 3,874.49 ms and 3,451.27 ms respectively.
No test suites, live activation, merge, push, or tag were run.

The original snapshot and original per-repeat outputs are unavailable. This
prevents an exact historical-snapshot rerun and a definitive attribution of the
historical v2-03 empty repeat. The current paired results and those limitations
are kept separate below; this report does not claim every historical question
has been resolved.

## Changes

`adapters/claude/recall_hook.py`:

- `_recall_candidates` preserves the original relative order of active decisions
  with live decision keys. Ordinary candidates then enter in fused search order,
  followed by unranked successor hints. An active project record without a
  decision key stays ordinary. Source admission and successor labels still run.
- `_merge_ordinary` interleaves local ranks across vaults. Coverage breaks ties;
  a later governance result cannot continuously block another vault's first card.
- `_recall_cards` uses the index's prepared authorship offsets during projection.
  Pinned priority, the byte budget, line cap, source labels and ranking fusion
  are retained. No timeout or card-dropping cutoff was added.

`epitype/rule_provenance.py`: `prepare_authorship` groups exact owner quotations
by their original source and scans that source once during offline preparation.
`authorship_cache` selects the vault-local index cache. The existing
`first_authorship` parser and proof rules remain unchanged: reuse rereads the
cited row and hashes the examined original prefix, recomputes its role and
authorship, and rescans on a mismatch. The cache contains offsets and hashes,
not copied transcript bodies or a cached authority verdict. Unavailable evidence
still fails source admission. Malformed optional preparation entries are skipped.

`epitype/memsearch.py`: the explicit `build` CLI prepares source offsets after
indexing. `build_index(..., prepare_sources=True)` exposes the same operation.
The API's default and automatic refreshes remain cheap, so capture's immediate
reindex cannot accidentally perform the offline source scans inside a hook.
Before activation, the prepared indexes must be built with the explicit CLI;
an absent or invalidated cache uses the existing verification fallback.

`tests/recall_benchmark.py` retains each repeat's actual payload, stderr,
duration, bytes and exact card-path match. Every repeat gets a fresh session ID.
Actual process markers stay inside the disposable copy. Index/source preparation
is outside the timed calls, as index construction was in the earlier benchmark.
The existing selection suite gained assertions for rank interleaving, successor
ordering and active project records; no assertion or threshold was weakened,
and those suites were not run.

## Four-case diagnosis

`b-before.json` contains every newly reproduced pre-fix output; `b-final.json`
contains every final output. `b-admission-before.json` records actual candidate
and admitted-group inputs during diagnosis. The earlier four-case reproduction
is also retained in `b-before-cases.json`.

| Case | Observed cause and current behavior |
| --- | --- |
| v3-07 | Five ordinary successor hints exhausted the per-vault cap before the rank-1 Codex card could enter its group. Search results now enter first; all three repeats inject the card. |
| v3-09 | Three unranked hints preceded the rank-2 storage card, making it the fifth local ordinary candidate. The combined line limit omitted it. It now keeps its fused local rank and is injected in all three repeats. |
| v3-11 | The rank-1 resource card was also a successor hint and entered fourth locally; earlier hints and cross-vault selection filled the output first. It now enters first locally and is injected in all three repeats. |
| v2-03 | The historical report retained no individual outputs or individual durations. Its 4,282 ms field is the median of three repeats, not the duration of the empty repeat. All newly reproduced pre-fix repeats and all final repeats deliver the pinned card. The historical empty repeat's exact cause remains unproven. |

The old core benchmark supplied no session ID and did not write delivery
markers. Its omissions cannot be attributed to the session delivery ledger.
The new process runs use separate session IDs and preserve each emitted payload.
Source deadline exhaustion can lead to an empty outer recall result, but the
missing historical repeat cannot establish that this was its actual path.

One intermediate correction exposed v2-01: active project records without
decision keys had been mistaken for pinned candidates during ordering. Reading
the live key fixed this without removing any standing decision. The final audit
checks all 132 paired repeats: no previously delivered pinned path was lost,
no pre-fix complete-card case was lost, and every original master complete-card
case ID is delivered. `b-review.json` records these checks.
The artifact audit is reproducible with `python docs/eopt4/review.py`; after
disposable process-fixture cleanup it explicitly reports that fixture as absent.

## Source-cost evidence

A real v3-11 reproduction spent 5.534 seconds in six source projections;
5.428 seconds were in repeated `first_authorship` scans, with 33,934 JSON loads.
Referenced records lie around 75 MB and 157 MB into an approximately 1 GB
original transcript. Repeated bounded scans could stop before those records.

Offline preparation produced 15 reusable origins for V1 and three for V3.
The first preparation measured approximately 29.35 seconds across all four
indexes, including unavailable-source handling. This cost is paid offline.
A subsequent profile spent 0.300 seconds in projection, including 0.277 seconds
for the first prepared source. Source rows and prefixes were still read.
That diagnostic also rebuilt an index after fixture mtimes changed; its total
runtime is not presented as a clean hook measurement.

Selective JSON-row skipping was considered and rejected because it could miss
malformed earlier records. The final parser does not use that shortcut.

## Paired measurements

All cases use the unchanged 44-question corpus, project scope, and three repeats.
Core alias preprocessing is cold for each repeat. Complete-card case counts are
also the counts that succeed in all three repeats; pointers are separate.
Mean bytes use each case's last actual context, matching the historical metric.
Median/max durations cover every repeat. No zero-byte final outputs occurred.
For these paired reports, averaging bytes over all repeats gives the same means.

| Metric | e89ef1a on the current frozen copy | 5806612 on that same copy |
| --- | ---: | ---: |
| Search hits@8 | 39/44 | 39/44 |
| Complete-card cases, all repeats | 26/44 | 37/44 |
| Complete-card runs | 78/132 | 111/132 |
| Mean injected UTF-8 bytes | 3362.89 | 3524.34 |
| Core median ms | 1211.30 | 1044.17 |
| Core maximum ms | 6011.04 | 3874.49 |
| Process search hits@8, three cases | 3/3 | 3/3 |
| Process complete-card cases, all repeats | 1/3 | 2/3 |
| Process complete-card runs | 3/9 | 6/9 |
| Process mean injected UTF-8 bytes | 2849.33 | 3115.33 |
| Process median ms | 2198.76 | 1964.22 |
| Process maximum ms | 6611.06 | 3451.27 |

The process cases are v3-11, today-codex and today-graphify. Codex usage keeps
rank 6 and remains outside the ordinary per-vault candidate window; graphify
keeps rank 1 and is injected. The improved search ordering was not removed.
Context bytes increased as more complete cards reached the output; this change
does not claim a formatting-only byte reduction or a universal latency bound.

Historical `final.json` still records 39/44 search hits, 25/44 final-repeat card
hits, 3270.25 mean bytes, 830.58 ms median and 12098.54 ms maximum. Those numbers
are not substituted for the fresh paired baseline above.

## Snapshot and historical evidence limits

The prior EOPT4 closeout explicitly deleted `.eopt4-snapshot`. The episodic
registry had no matching entry; a focused original-transcript fallback
(`01a18696-c3bd-76a1-9a9e-7ff26a773023`) confirmed that cleanup. Its old benchmark
also did not save the individual outputs, so none are represented as recovered.

V1, V2 and V4 reconstruct exactly to their historical manifest hashes. V3 now
contains 264 cards instead of 263, with changed cards. Attempts using historical
Git versions and line-ending variants did not recover its old hash. The new V3
hash is `0579f4ad8c2b2a332ddf3dddbf156d3de576e5f15d6214ecccdd198922c1f63e`.
All current before/final reports use identical manifests. The review recomputed
the actual core and process manifests after execution and found no card drift.
Original-master case coverage comparisons therefore concern the same question
IDs, not byte-identical historical V3 source contents.

## Real commands and reproduction

The final wrapper executed these commands once at revision 5806612:

    python tests/recall_benchmark.py --snapshot .eopt4b/sample --scope project --cold --output docs/eopt4/b-final.json
    python tests/recall_benchmark.py --snapshot .eopt4b/process-final --scope project --process --case v3-11 --case today-codex --case today-graphify --output docs/eopt4/b-process-final.json

The pre-fix commands used the same benchmark harness with
`EPITYPE_BENCHMARK_ROOT` pointing to a local archive of e89ef1a and
`EPITYPE_BENCHMARK_REF` set to its full revision. Archive source-byte hashes
reflect Git's LF files; the checkout's historical hashes reflect CRLF files.
Two incomplete candidate corrections were measured before the final fix; their
failed outcomes drove the candidate-order and live-key corrections above.
They are not reported as final acceptance measurements.

The actual CLI command `python epitype/memsearch.py build .eopt4b/sample/V3`
returned `built`, 264 cards and `prepared_authorship: 3`. An initial invocation
with `--vault` was rejected as usage; the build command takes a positional vault.

The developer-requested `python epitype/dream.py --scheduled` ran with an isolated
synthetic home/config/vault. Its pack was read and state reported `complete: true`
with no section errors. The synthetic note had expected lint findings; this was
not a clean-data test or an update to the live nightly state. See `b-dream.json`.

One frozen fixture remains at `.eopt4b/sample` for lead review and reproduction.
Its config can create a new disposable process copy with the benchmark's
`--create --config .eopt4b/sample/config.json` options. Source archives, duplicate
process fixtures and unused owned diagnostics are released after review.
Five owned duplicate/source/synthetic fixture directories have been released;
cleanup checked registered worktrees, active process consumers and 260 scheduled
tasks. Only the single frozen sample remains under the ignored scratch root.
The frozen fixture's retention ends after lead review/integration; live original
transcripts remain external dependencies of source verification.

Git reported LF-to-CRLF normalization warnings. Focused whitespace checks passed.
