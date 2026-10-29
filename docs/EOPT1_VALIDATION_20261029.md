# EOPT1 validation — 2026-10-29

Scope: the isolated `opt/rule-sync-dedup-20261029` branch, based on `97ab94d`.
The live checkout, rule cards, baseline, and host settings were not activated or rewritten.
The host's separately mandated compaction handoff is outside this product change.

## Behavior

`rule_pipeline._payload_meaning` binds generated rule text, source scope, host routing,
and execution scope. Descriptions, retrieval aliases, wikilinks, and history below
that generated text do not alter the fingerprint. Field-driven guards without a
separate generated sentence keep their complete interpretation checks.

Old complete-card hashes must match before `rule_binding` records a narrow digest
in the configuration directory's `rule_binding_migrations` state. This works for
the pinned legacy baseline and inline/external provenance. Original cards, baseline
bytes, pins, and `approval.text_sha256` are not rewritten. A bad baseline pin, new
legacy membership, changed rule text, or changed scope cannot migrate itself.

`host_sync` reads each destination's current generated block. A failed card is
named with its reason and either retains its identifiable previous entry or is
explicitly omitted when no identifiable previous entry exists. Retained entries
are unverified, and the successful remainder is reported as `PARTIAL`. No text is
borrowed from the other host. Older blocks can be identified by their source path,
exact prior text (including the source document), or stable CORE number; recovery
does not guess ordinal identities.

`core_gen.assemble` states unavailable-source facts, default author, global scope,
and standard source directory/pattern once. Author/scope/path exceptions and
verified-source distinctions remain explicit. Relative card paths use the declared
directory. A compact comment records card identity, exact entry line count, order,
and inherited-default status, so subsequent failed-card recovery preserves multiline
text, routing, and ordering. The same renderer serves Claude, Codex, and project rules.

## Real-vault copies

Inputs were copied from these native vault slugs under `%USERPROFILE%/.claude/projects`:
`C--`, `C--dev-daipai`, `C--Users-User-Desktop-titan--`, and `C--projects-------`.
Host instruction files were copied too. The copied configuration retained the real
Codex document budget; no cap was increased. Copies used short temporary paths
inside this worktree, and all host writes were confined to them.

Absolute reciprocal supersession locators were translated only inside the fixture.
Original text/scope and source evidence remained intact; bindings or frozen hashes
were translated only after matching the original bytes. Real evidence locators were
read-only. Source-origin cache state and migration writes were confined to the copied
configuration. Temporary payload copies are removed; only the small reports remain.

The old renderer was loaded from `git show 97ab94d:epitype/core_gen.py`. Both old
and new generation used the copied G vault. Temporary directory prefixes were
normalized to the original directory for an equal-path byte comparison.

| G / Claude generated rules region | UTF-8 bytes |
| --- | ---: |
| Current real host block, including final newline | 19,103 |
| Base renderer, copied inputs | 19,103 |
| New renderer, copied inputs, dry run | 13,159 |
| Saved | 5,944 (31.1%) |

These figures use LF-normalized UTF-8 text, matching the generator and sync
comparison; they do not measure an entire host file or its filesystem encoding.
The current copy contains 44 collected rules across generated and other layers;
the byte figures measure only the Claude view, excluding the outer region markers.

The originally reported bad bounty-controller card now verifies successfully in
the read-only snapshot. Its raw SHA-256 is
`b9c43960cb286f07d7a4538a3e7d42d53bebdf96b2b1587d1eb3963ead0d53f2`.
Therefore the one-bad-card check changes only that copied card's generated `text`
after the valid snapshot has been applied. The unchanged source document and binding
then reject that edit; this does not describe a currently bad live card.

The local fixture runs actual subprocess commands, with `HOME`, `USERPROFILE`, and
`EPITYPE_CONFIG` redirected to copies:

```
python -m epitype sync <copied-vault>
python -m epitype sync <copied-vault> --apply --actor "EOPT1 isolated copy check"
python -m epitype sync <copied-vault>
```

All four copied vaults initially reported format drift (exit 1), applied successfully
(exit 0), and subsequently reported `OK` (exit 0). For the injected fourth-vault
failure, both apply and the subsequent dry run exited 0; each host named the failed
card and reason, the old text remained, the mutation was absent, and the aggregate
report was `PARTIAL`, explicitly not verified. The original host budget was retained.
The machine-local runner and small report are
`.epitype/eopt1_copy_check.py` and `.epitype/eopt1_copy_results.json`.

## Tests

Observed focused results:

| Command | Passed / run |
| --- | ---: |
| `python tests/rule_binding_regression.py --selftest` | 7 / 7 |
| `python tests/rule_sync_dedup_regression.py --selftest` | 10 / 10 |
| `python tests/rule_pipeline_regression.py --selftest` | 21 / 21 |
| `python epitype/host_sync.py --selftest` | 29 / 29 |
| `python epitype/core_gen.py --selftest` | 24 / 24 |
| `python tests/rule_native_sources_regression.py` | 48 / 48 |
| `python tests/card_lint_provenance_fields_regression.py --selftest` | 7 / 7 |
| `python tests/source_framework_closure_regression.py --selftest` | 10 / 10 |
| `python tests/shared_index_scope_regression.py --selftest` | 3 / 3 |
| `python tests/cap_consolidation_regression.py --selftest` | 13 / 13 |
| `python tests/shim_dispatch_regression.py` (unregistered base suite, run explicitly) | 6 / 6 |

The initial `python tests/run_all.py` exposed the new suite's missing `--selftest`
entrypoint support, two fixtures treating historical body edits as operative-text
edits, and two suites using the existing zero-argument `_plan` callback. The CLI and
callback compatibility were repaired. The two fixtures now mutate generated rule
text and retain their rejection assertions; the predecessor test also proves dated
history remains valid, matching the Owner's changed requirement.

Final command: `python -u tests/run_all.py --jobs 4`.
Observed result: **147/147 registered suites passed; overall exit 1** because the
existing registry audit and privacy scan below fail. No registered suite failed.
The final result is retained in `.epitype/eopt1_full_final.log`.
The initial `python tests/run_all.py` result was 141/146; its five intermediate
failures were repaired and rechecked as described above. That first attempt is
retained separately in `.epitype/eopt1_full_suite.log`.

The separate `python tests/privacy_lint.py` scan exits 1 with these three existing
fixture-string findings:

| Unchanged base file | Line | Pattern |
| --- | ---: | --- |
| `tests/card_lint_body_noise_regression.py` | 32 | session-uuid |
| `tests/card_lint_body_noise_regression.py` | 33 | windows-home |
| `tests/dream_vault_snapshot_regression.py` | 29 | email |

To check the baseline rather than infer it, `git archive 97ab94d` was extracted
into an isolated temporary repository, its explicit tracked path list was staged,
and the base `python tests/privacy_lint.py` was executed there. It also exited 1,
reporting exactly the same three findings across 279 tracked files. The scanner
and both affected source files have no diff from that base. The temporary baseline
copy was removed after the check. Its small result is
`.epitype/eopt1_master_privacy.json`. No privacy assertion or allowlist was weakened.

The final runner also reports `tests/shim_dispatch_regression.py` as an unregistered
regression file. Executing the base runner's `_unregistered_regressions` audit in
the same pristine `97ab94d` snapshot returns that exact file too. This file was not
changed by EOPT1. Its six tests were explicitly run and passed using the command
in the table; its existing entrypoint does not accept `--selftest`. Registration
of this unrelated base suite and the three existing privacy findings are left
unchanged and are the only remaining full-run failures.

`git diff --check` passed. Git reports the repository's normal LF-to-CRLF conversion
warnings; no whitespace error was reported.
