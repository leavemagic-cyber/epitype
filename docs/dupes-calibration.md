# Cross-vault duplicate calibration (2026-10-29)

`epitype dupes [vaults...] [--json] [--threshold N]` only writes a report.
It never calls a model or edits, merges, or retires a card. The lead AI applies
the owner's rule: latest owner wording wins; never prefer a limit the owner
did not set; ask only about money, deletion, or product direction.

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

## Ground truth and reconstruction

Inputs: the ticket's `merge_plan_MERGE1.md` and `merge_plan_MERGE2.md`.
Plan hashes and exact snapshot commits are in `dupes-calibration.json`.
The amendments in MERGE2 override its backup verdict and add the five contract
pointers' final keeper; they are not extra groups. Combined final plan counts:
52 MERGE, 13 SUPERSEDE, 30 CONFLICT, 43 KEEP, 1 MOVE (139 groups).

For each G/V/T repository, select the first chronological commit whose subject
starts `merge(cards): fold duplicate cards`, then reconstruct its parent using
`git show <commit>^:<path>`. Live vaults are read-only throughout.

| Vault | Merge | Parent | Markdown copies |
| --- | --- | --- | ---: |
| G | 6a4d794 | 91672d1 | 517 |
| V | 4506492 | ba67fcd | 220 |
| T | d1072de | 8aab35d | 387 |

The detector sees **894 eligible cards, 0 scan errors**. Eight plan groups
involve another vault, outside the requested G/V/T snapshots. Seven of those
are positive settlement groups, so the scoring denominator is **88**:
46 MERGE, 13 SUPERSEDE, 29 CONFLICT. KEEP/MOVE are not positive duplicates.
Ten positive groups have a missing or already-excluded member in these
pre-merge snapshots; the primary denominator retains them as misses.
Eligible-only recall uses 78 groups and is reported separately.

Strict group recall requires **all named members in one detected cluster**.
An extra cluster contains no complete positive ground-truth group; this does
not prove the extra is a false positive, because the plans are not exhaustive.
Larger clusters can absorb unrelated cards, so largest-cluster size and KEEP
groups with at least two members co-clustered are reported alongside recall.

| Threshold | Whole groups / 88 | Recall | Clusters | Extra | Largest | KEEP co-clustered |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.25 | 15 | 17.05% | 101 | 86 | 44 | 21 |
| **0.35** | **8** | **9.09%** | **55** | **47** | **20** | **19** |
| 0.45 | 2 | 2.27% | 17 | 15 | 8 | 12 |
| 0.55 | 0 | 0% | 8 | 8 | 8 | 6 |

At 0.35: MERGE 4/46, SUPERSEDE 1/13, CONFLICT 3/29. Thus strictly duplicate
MERGE/SUPERSEDE groups alone have recall **5/59 (8.47%)**. Eligible-only total
recall is **8/78 (10.26%)**. Fourteen of 88 groups have at least one pair
co-clustered; that weaker measure is not counted as complete recovery.
Thirty of the 47 extra clusters contain no positive ground-truth card.

0.35 retains some useful coverage while roughly halving the workload at 0.25;
0.45 loses six of the eight recovered groups. Exploratory unconditional-link
scoring produced an 800-card component, and weighted containment produced a
190-card component at 0.25; both were rejected rather than advertising their
inflated group recall. Semantic paraphrases and scope-related cards remain
difficult: the report requires original-card comparison by the lead AI.

## Reproduction

Run on read-only paths supplied locally; the script writes only its temporary
reconstruction and the explicitly named output. It deletes temporary copies on
exit unless `--snapshot-dir` is given for reuse during calibration.

```text
python tests/calibrate_dupes.py --plan <MERGE1.md> --plan <MERGE2.md> --vault G=<G> --vault V=<V> --vault T=<T> --out <summary.json>
```

Source cards, quotes, local absolute paths and transcripts are not copied into
the committed results. Only aggregate metrics, plan group IDs, hashes and
source versions are retained. This worktree does not activate the live hooks.

## Validation

- `python tests/dupes_regression.py --selftest`: 26/26 passed.
- `python epitype/dream.py --selftest`: 70/70 passed.
- `python epitype/memspec.py --selftest`: 10/10 passed.
- `python tests/run_all.py`: 146/146 registered selftest suites passed, but
  the overall command exited 1 on two existing gates: an unregistered
  `tests/shim_dispatch_regression.py`, and three privacy findings in existing
  fixtures (`tests/card_lint_body_noise_regression.py:32` session-uuid,
  `:33` windows-home, `tests/dream_vault_snapshot_regression.py:29` email).

Both failing gates were reproduced on an isolated archive of unchanged master
`97ab94dbdc8b6f5ccf660ea0aa321ecfc6429305`. Loading that revision's
`tests/run_all.py` and running `_unregistered_regressions` found the same shim
test. Running that revision's `python tests/privacy_lint.py --repo <archive>`
against its 279 staged source files exited 1 with the same three file/line/
pattern findings (the feature tree has 285 tracked files). The temporary
baseline was removed after the checks. These unrelated baseline gates were
not weakened, allowlisted, or silently reported as passing.
