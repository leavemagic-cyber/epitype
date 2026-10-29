# EOPT3 progress

## Request and boundaries

Resume ticket EOPT3 in this worktree only. Detect cross-vault near duplicates,
write Markdown/JSON reports, integrate after nightly view generation, calibrate
against MERGE1/MERGE2 using pre-merge Git snapshots, run touched tests and
`python tests/run_all.py`, commit explicit paths with the requested coauthor.
Never modify the live repository, other ticket worktrees, or live vaults.
Epitype makes no model/network calls and never settles or retires cards.

## Verified starting point

- Branch opt/dupe-detect-20261029, HEAD 97ab94d; status and diff clean.
- No *_PROGRESS.md in this worktree or ticket scratchpad; prior attempt logs empty.
- Applicable parent rules read: C:/Epitype/AGENTS.md.
- MERGE plans are in the user-provided scratchpad/quotes directory.
- First matching merge commits: G 6a4d794, V 4506492, T d1072de.
- Native memory registry searched; no relevant precedent used.

## Completed implementation and verification

- epitype/dupes.py: shared scanner/discovery, normalized character trigrams,
  exact Jaccard plus IDF cosine and metadata, connected clusters, quote/field
  inventory, keep suggestions, Markdown/JSON, report-only CLI.
- epitype/cli.py and memspec.py: command and Chinese/English messages.
- dream.py: after views, write duplicate reports; propagate failures to pack
  and incomplete state. Exclude generated reports from draft/aging counts.
- tests/dupes_regression.py registered in run_all: final focused run 26/26.
- python epitype/dream.py --selftest: 70/70 after fixing generated reports
  inflating draft counts. Initial observed 69/70 was caused by our integration;
  the existing assertion was preserved, production counting was corrected.
- python epitype/memspec.py --selftest: 10/10.
- Real scheduled dream executed in a synthetic home/config/vault fixture.
  No live hook activation or source-card writes. A same-task compaction
  checkpoint was separately saved at the developer's explicitly requested path.
- Calibration reconstructed G/V/T via git show of the first merge's parent;
  1,124 Markdown copies, 894 eligible cards, 0 scan errors. Cached only in
  this worktree's ignored .epitype/premerge while iterating.
- Corrected both MERGE2 amendments; final plans have 139 groups. Eight groups
  involve another vault; 88 in-scope MERGE/SUPERSEDE/CONFLICT positives remain.
  At 0.35: 8/88 strict whole groups (9.09%), 55 clusters, 47 extra, largest20;
  eligible-only 8/78, duplicate-only MERGE/SUPERSEDE 5/59. This low semantic
  recall is explicitly documented, not called a complete audit.
- Lower 0.25: 15/88, 101 clusters, 86 extras, largest44. Higher 0.45: 2/88.
  Rejected unconditional links (800+ component) and weighted containment
  (largest190 at 0.25). Selected 0.35 balances workload and limited recall.
- Final calibration re-run on final scoring code matched all selected metrics.
  Distilled docs/dupes-calibration.json plus docs/dupes-calibration.md retain
  hashes, versions, group IDs and aggregates, not live card bodies/paths.
- git diff --check clean; Git warns LF will become CRLF on touched text files.

## Full-suite result and baseline reproduction

python tests/run_all.py completed: TOTAL PASS 146/146 registered suites.
Overall exit 1, because regression registration finds the existing
tests/shim_dispatch_regression.py absent from SELFTESTS, and privacy scan has
3 existing fixture findings: tests/card_lint_body_noise_regression.py:32
(session-uuid), :33 (windows-home), tests/dream_vault_snapshot_regression.py:29
(email). No registered suite failed. No test or privacy rule was weakened.

Reproduced both failing gates on a fresh isolated archive of current master
97ab94dbdc8b6f5ccf660ea0aa321ecfc6429305: same unregistered shim test and same
3 privacy file/line/pattern findings, privacy exit1 over279 tracked base files.
The feature tree has285 tracked files. Temporary baseline was removed.
Bounded reproduction script/result and full-suite log retained at:
.epitype/check_master.py, .epitype/master-gates-result.json,
.epitype/eopt3-run-all.log. Source versions/reproduction/results are also in
docs/dupes-calibration.md; no raw private fixture values were copied there.

## Cleanup and closeout

Resolved own .epitype/premerge absolute path, compared git worktree inventory,
inspected running processes and scheduled jobs: zero consumers. Removed the
1,124 pre-merge Markdown copies, snapshot manifest, old trial JSONs and distill
script; verified cache_removed=true. Keep only distilled committed evidence
and bounded validation log/reproduction files. No browser or agent resources.

Implementation/tests committed as 3f74d81 with requested coauthor. This
progress, README entries, calibration script and distilled results form the
separate calibration/documentation commit. Product work and cleanup complete;
only final Git readback follows that commit. No merge, push, tag or live
activation. The two unchanged master validation gates and low semantic recall
remain explicitly disclosed.
