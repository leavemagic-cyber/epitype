# Cross-vault near-duplicate detection

`epitype dupes [vaults...] [--json] [--threshold N]` only writes a report.
It never calls a model or edits, merges, or retires a card. Review candidates
against their original sources and scopes before making any changes.

The default threshold is **0.35**. This is a lexical candidate detector with
low recall on the human semantic-merge benchmark, not a complete duplicate audit.

## Method

Normalize the body with Unicode NFKC and case folding, remove whitespace and
punctuation, and compare character trigrams, including unsegmented Chinese.
Remove HTML audit comments, wiki-links and URLs before comparing
wording. An inverted index computes exact Jaccard and corpus IDF cosine; no
random candidate sampling occurs. The score is their maximum, augmented by:

- Shared alias or mutual wiki-link: `0.3 + 0.7 * cosine`.
- One-way wiki-link: `0.15 + 0.85 * cosine`.
- Name ratio at least 0.85: `0.2 * name_ratio + 0.8 * cosine`.
- Same nonempty decision key: 1.

Connected components of accepted pairs form clusters. Reported Jaccard is
separate from the candidate score. A shared alias or link alone cannot pass
the default threshold. Suggestions prefer a machine-check carrier, then the
newest dated card (mtime is the fallback for an undated card). Suggestions do
not override owner wording. Quote dates come from explicitly attributed quote
lines/sections or the quote field and its decision date; maintenance dates do
not by themselves date an owner quote. Undated quotes are marked as such.

Skip superseded cards, historical scope, level-4 grants/rulings/corrections,
private/generated directories and linked files using the shared card scanner.
Malformed/unreadable cards are reported as incomplete coverage. JSON schema 1
retains card paths, machine-field names, newest recorded owner quotes, dates,
pair scores/signals and the suggested keep path. Markdown and JSON are written
to the governance vault's `_drafts/duplicates/YYYYMMDD.*`, including empty scans.
The ledger holder is governance, falling back to the first input vault.

## Remembering judgments

`epitype dupes --judge <report.json> <verdicts.md>` imports the lead's verdicts
into `_drafts/duplicates/judged.json` in the governance vault. `--governance
<vault-copy>` reads judgments and writes reports there while keeping the scan
vaults unchanged. It applies to both imports and scans. `--date YYYY-MM-DD`
overrides the judgment date (otherwise the report date) or scan date.

The verdict file has exactly one nonempty line per numbered report cluster:

```text
OVERLAP 1 | Separate rules with some shared wording.
OTHER-VAULT 2 | UNRELATED | Shared template only.
SKIPPED-BINDING 3 | owner_quote, verify | OVERLAP：Different scopes.
DUPLICATE 4 | Same operative rule; settlement still needed.
```

Cluster verdicts apply to every reported pair in that cluster, without
inventing judgments for unreported combinations. Unknown lines, duplicate
numbers, missing clusters and invalid records fail without replacing the
existing record. Reimports update matching pairs and preserve other judgments.
The record is replaced atomically after validation.

Each pair stores its two absolute card paths, verdict, reason, date and SHA-256
body fingerprints. New reports retain these fingerprints from scan time, so
delayed import cannot silently approve a later body edit. Fingerprints include
all body text (including comments and links), with text-mode newline
normalization; frontmatter is excluded. Legacy reports without fingerprints
read the current bodies at import and explicitly warn that judging-time content
cannot be reconstructed. Such records carry `fingerprint_source: import`.

Both the CLI and nightly dream omit an overlap/unrelated pair only while both
body fingerprints match. Filtering precedes clustering: disconnected groups
split, isolated cards disappear, and groups with no remaining pairs disappear.
New pairs and changed bodies return for judgment; a `duplicate` verdict stays
visible until settlement. JSON, Markdown and the dream summary report the
number of suppressed candidate pairs. Missing records suppress nothing;
unreadable or malformed records surface an error rather than hide candidates.

## Calibration method and aggregate results

A historical calibration compared lexical candidates with manually judged
settlement groups. Reconstruct pre-settlement snapshots in disposable copies,
exclude generated and historical cards with the normal scanner, then sweep
thresholds. Never publish the source cards, quotations, local paths, snapshot
identifiers or judgment plans. These aggregate figures are one calibration,
not a guarantee for another vault or a claim of current runtime performance.

The sample contained 894 eligible cards with 0 scan errors. Of 139 judged
groups, 88 in-scope positive groups formed the primary denominator (46 merge,
13 supersede, 29 conflict). Groups outside the sampled vaults were excluded;
10 positive groups with unavailable members remained misses. Eligible-only
recall used 78 groups. A hit requires every named member in one cluster.
An extra cluster contains no complete positive group; because judgments are
not exhaustive, this does not establish a false positive.

| Threshold | Whole groups / 88 | Recall | Clusters | Extra | Largest | Keep groups co-clustered |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.25 | 15 | 17.05% | 101 | 86 | 44 | 21 |
| **0.35** | **8** | **9.09%** | **55** | **47** | **20** | **19** |
| 0.45 | 2 | 2.27% | 17 | 15 | 8 | 12 |
| 0.55 | 0 | 0% | 8 | 8 | 8 | 6 |

At 0.35, merge/supersede recall was 5/59 (8.47%); eligible-only recall was
8/78 (10.26%). Fourteen of 88 groups shared at least one detected pair,
which is weaker than complete recovery. Thirty of 47 extra clusters had no
positive ground-truth card. The default reduces the workload relative to
0.25, but lexical detection still misses semantic paraphrases and needs review.

A separate remembered-verdict measurement imported 48 judged clusters
(45 overlap, 3 unrelated) into 160 pair records over 138 cards. A scan of
938 cards reported 0 errors, suppressed those 160 unchanged pairs and left
0 clusters. A copied-body change restored 1 pair in 1 cluster with no
suppression. Legacy reports without fingerprints used import-time bodies;
this does not prove the bodies matched their earlier report-time versions.

For local reproduction, keep source snapshots and judgments outside the
repository. Compare complete-group recall, eligible-only recall, largest
cluster size and co-clustered keep groups together. Public regression cases
use invented cards in temporary vaults; private measurements are not package
fixtures and do not establish universal recall or precision.
