# EOPT4: recall ranking and compact injection

Current follow-up: [EOPT4B results and evidence limits](EOPT4B.md), measured at
`5806612`. The measurements and remaining gaps below describe the historical
`e89ef1a` delivery, before EOPT4B.

Implementation: `4dc1ee7` and `092cc4e`, on `opt/recall-rank-20261029`, based on `97ab94d`.
The installed/live repository and live memory cards were not edited or activated.

The fixed corpus has 44 cases across four vaults. It was committed with the
original pre-change baseline in `cf274e8`. Historical prompts are owner quotations
recorded on cards; their original sessions were not independently reverified.
The fixture explicitly labels that limitation. Three additional cases come from
the owner's ticket: Codex usage, Antigravity usage and graphify.

`epitype/memsearch.py` now fuses BM25, exact/near aliases, one-hop outgoing/reverse
wikilinks and dates using stdlib weighted RRF. Equal signal values share a rank.
Query coverage reduces a single Latin alias's influence on long queries; CJK
phrase evidence remains one signal. Indexed metadata makes dates and links
available without rereading every card. Format-8 snapshots remain searchable with
their previous ranking while an upgrade lock is busy. Local relative links and
ambiguous stems are resolved before neighbour expansion.

`adapters/claude/recall_hook.py` preserves local fused order and existing pinned
decision priority. It prints shared source facts/default scope once, retaining
the item text, owner-verbatim versus AI classification, author, original quote,
non-default scope and successor relationship. Unlabelled items inherit the
shared unverified/non-authority explanation. Default scope applies only to
source-labelled items. No IDs-only expansion mode was introduced.

`epitype/rule_provenance.py` gives nested source checks one caller deadline, with
three seconds of the existing hook budget reserved outside source checks. Exhaustion
remains UNVERIFIED. Proof admission and unbounded offline verification are
unchanged. Already-delivered pinned cards no longer spend the next call's byte
budget or trap another card permanently in pointer fallback.

`epitype/memspec.py` contains Chinese and English messages. Regression changes
reflect the owner's selected shared explanation format: author and complete scope
remain asserted, and the budget test checks complete bodies separately from short
pointers. The new suite is registered in `tests/run_all.py`.

## Measurements

Final exact-source comparison is in `project-baseline.json` and `final.json`;
actual hook subprocess timings are in `process-baseline.json` and
`process-final.json`. The original all-vault baseline is retained as `baseline.json`.
Each report identifies source versions, sample manifest hashes, per-case results,
UTF-8 context bytes and latency. Do not treat earlier intermediate trial files as
final evidence.

| Project-scoped, 44 cases, three repeats | Original | Final |
| --- | ---: | ---: |
| Search hit at 8 | 37/44 (84.1%) | 39/44 (88.6%) |
| Expected complete card actually injected | 28/44 | 25/44 |
| Mean injected UTF-8 context bytes | 3639.89 | 3270.25 |
| Recall-core median ms | 1687.18 | 830.58 |
| Recall-core maximum ms | 7958.59 | 12098.54 |
| Actual process median ms, three representative cases | 3030.56 | 3019.44 |
| Actual process maximum ms, three representative cases | 9710.30 | 8431.53 |

The maximum core outlier means the host's ten-second latency requirement is NOT
established. The three actual-process cases are v3-11, today-codex and
today-graphify, three repeats each; they are not a 44-case process measurement.
Their original delivery matcher was a substring heuristic, so only their
timings/bytes are compared. Final matching uses exact selected vault/card paths.
Source admission and filesystem work remain in the measurements.
Per-case hit/byte results use the last repeat's actual context; latency medians
and maxima use all repeats. Timeout-dependent output can vary between repeats.
The copied index/provenance caches persist across sequential measurements;
only the pure alias cache is explicitly cleared by `--cold`. These are local
comparisons rather than clean-machine startup benchmarks.

Bytes decrease 10.2% across the test set, but this is not a pure formatting
effect: selection changes too. Final v2-03 emitted no context on its last repeat.
On the 43 cases with nonempty context in both runs, mean bytes are
3621.28 to 3346.30 (7.6% lower). Complete-card injection loses v2-03, v3-07,
v3-09 and v3-11, and gains resources-1. These regressions remain open.

`process-prebudget.json` retains the failed initial final-code process run
(maximum 11362.86 ms). Profiling a reproduction found 5.7 seconds in source
projection/attribution, 1.6 seconds in search and further import/output work.
The original one-second reserve was inadequate. The three-second reserve
improves the representative process result, but does not fix every outlier.

The ranking result is 37/44 to 39/44. Codex usage moves from rank 9 to rank 6,
and the interface case from rank 9 to rank 2. All original search passes remain.
Source text still goes through source admission. Pinned decisions can crowd an
ordinary card out of the final injection even when its search rank improves.
The Antigravity case has no qualifying query/link evidence in the frozen sample;
the live vault was not changed to manufacture a passing result.
For "記得多用codex", rank 9 to 6 is a search improvement only: the expected
usage card still does not reach the final injection. The graphify plan remains
rank 1 and is injected. Pinned decisions were not displaced to force a pass.
This implementation therefore improves retrieval and reduces repeated text,
but has not passed end-to-end recall/latency acceptance.

## Reproduction

Create one disposable snapshot, reading the supplied vault configuration:

    python tests/recall_benchmark.py --snapshot <temporary-sample> --create --config <configuration> --scope project --cold --output <report.json>

For original-code comparison, extract `97ab94d` to an owned temporary directory,
set `EPITYPE_BENCHMARK_ROOT` to it and `EPITYPE_BENCHMARK_REF` to the full original
revision. Reuse the exact same snapshot sequentially for original and final code.
The benchmark records sample hashes so drift is visible. Project mode selects
the current vault followed by governance, matching the adapter's established
scope. All-vault mode exercises the no-cwd stress path.

The normal benchmark measures the recall core with capture disabled and source
verification retained. It excludes process startup, capture and delivery-ledger
work. `--cold` clears pure alias preprocessing before each sample. Three repeats
per case are used. Actual process mode includes startup/capture/output/ledger:

    python tests/recall_benchmark.py --snapshot <disposable-process-sample> --create --config <frozen-sample-config> --scope project --process --case v3-11 --case today-codex --case today-graphify --output <process-report.json>

Process mode may write captures into its disposable copy. It must not run on the
authoritative frozen sample or a live vault. Timings are local measurements, not
a guarantee of model reading/adoption or deployment.

## Verification

Focused commands and observed passes:

| Command | Pass count |
| --- | --- |
| `python epitype/memsearch.py --selftest` | 50/50 |
| `python adapters/claude/recall_hook.py --selftest` | 53/53 |
| `python tests/recall_rank_regression.py --selftest` | 22/22 |
| `python tests/recall_regression.py --selftest` | 9/9; synthetic search 45/51 unchanged |
| `python tests/recall_selection_regression.py --selftest` | 23/23 |
| `python tests/recall_successor_regression.py --selftest` | 8/8 |
| `python tests/governance_regression.py --selftest` | 7/7 |
| `python tests/capture_recall_regression.py --selftest` | 8/8 |
| `python tests/rule_final_caller_regression.py --selftest` | 6/6 |
| `python tests/rule_pipeline_regression.py --selftest` | 21/21 |
| `python tests/rule_provenance_regression.py --selftest` | 42/42 |
| `python tests/rule_source_consumers_regression.py --selftest` | 11/11 |
| `python tests/recall_history_regression.py --selftest` | 1/1 |
| `python tests/stale_state_recall_regression.py --selftest` | 3/3 |
| `python tests/recall_advisory_regression.py --selftest` | 6/6 |
| `python exam/exam_runner.py --selftest` | 14/14 |

Initial `python tests/run_all.py` reported 142/146 components. Four changed-format
expectations failed and were corrected with the assertions described above.
The first final `python tests/run_all.py --jobs 3` passed 145/146 component
suites, exit 1: owner_attribution's repeated-Markdown timing assertion measured
2.0784443 seconds against its unchanged 2-second threshold. The exact test
passed when rerun alone on current code (1 test, 1.705 seconds) and original
97ab94d (1 test, 1.587 seconds); this does not prove master fails that assertion.
The command was repeated after the budget correction: 146/146 component suites
passed, exit 1. Owner attribution ran 81 tests in 1089.611 seconds and passed
(one existing skip). The remaining overall failures are the unregistered shim
suite and three privacy findings in 291 tracked files. `master-checks.txt`
independently reproduces the original revision's three privacy findings and
unregistered `tests/shim_dispatch_regression.py`; those are outside EOPT4.

The scheduled dream command was exercised only in an isolated synthetic home and
vault, exit 0. The live nightly state was not updated. Git reported LF-to-CRLF
normalization warnings; focused whitespace checks passed.
