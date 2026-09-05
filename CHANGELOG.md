# Changelog

## Unreleased

- A matched PreToolUse rule now remains a deny when audit logging is contended or
  advice exceeds the output ceiling. Advice is truncated to a valid bounded payload,
  superseded cards are ignored, risky nested-repeat regexes are rejected before use,
  and documented shell aliases share command-aware matching.
- FTS freshness now compares the stored file manifest after the grace window, and
  incremental builds use both mtime and size. Vault scans do not follow symlinks or
  Windows junctions outside the vault.
- Decision lint requires one active card per key and validates that replacements are
  active cards with the same key. YAML `...` frontmatter endings are handled
  consistently by lint, search, and action gates.
- Owner captures and compact maps use the same ledger-selected governance vault.
  Compact recovery maps are session-specific and bounded by age and count.
- The installed package now provides a unified `epitype` command. CI and publishing
  build and exercise the installed wheel, and tag publication requires the package
  version and full source selftests to pass.
- Codex trust checks now require exactly the four supported registrations; missing or
  skipped configuration is no longer reported as runnable success.

## v1.1.0 — 2026-09-03

The token-economy release. Every line Epitype injects is paid for once as output and
again on every later turn as re-read context, so this release measures that cost on real
prompts and cuts it, and it closes the gaps where an owner's own words were being lost.

### Injection cost (measured on 40 real prompts)

| | v1.0.0 | v1.1.0 |
|---|---|---|
| Recall injection per prompt | 3.6 KB / 11 lines | **2.3 KB / 8.0 lines** |
| Absolute paths as a share of it | 32% | 17% |
| Session start (non-governance project) | 10 KB, hitting the budget cap | 7.6 KB |

- Recall prints one `vaults:` legend line and `V1/relative` paths instead of repeating an
  absolute vault path on every hit; descriptions are truncated; at most two body-only hits
  per vault; the whole block is capped, with pinned corrections and rulings first (U29).
- Session start injects the working directory's own vault plus the governance vault only.
  Another project's index and ledger no longer crowd the budget (U29). **Behaviour change
  for multi-vault installs**: the governance vault is the configured vault that holds the
  working ledger, and a vault reached from the working directory is always kept. If no
  configured vault holds a ledger, every configured vault is injected as before.
- Narration between tool calls ("that failure was my path typo, rerunning with C:/...")
  costs output tokens and then re-read context on every later turn while telling the owner
  nothing. The PreToolUse gate now names any such segment in one line without touching the
  permission decision; `epitype/narration_meter.py` reports per-session and per-day totals
  for release review. A 36-hour local baseline held 9,273 such segments (U28).

### Owner's words stop getting lost

- Corrections: a sentence where the owner corrects the agent is captured verbatim into
  `<vault>/corrections/`, indexed immediately, and pinned first at recall with a visible
  marker, ahead of whatever plan card matched better lexically (U25).
- Rulings: when the previous assistant turn explicitly asks the owner to decide, the reply
  is captured with the question it answers into `<vault>/rulings/` and pinned the same way
  (U26). A bare mention of the word in a report, or a request phrase inside quotes, no
  longer counts as a request (U30, U31).
- Captured cards carry the owner's words in their description, so the injected line is
  self-contained (U29).

### Stale to-do items

- `epitype/pending_lint.py` names to-do lines that have an entry but no exit: no runnable
  `verify:`, not closed, older than the configured window. Session start announces the
  count in a single first line so the budget cannot drop it (U25).

### Fixes

- Windows CI: four legacy-index selftests failed only on GitHub's Windows runners, which
  hand `tempfile` an 8.3 short path under the runner profile that never equals the
  product's resolved output. Every selftest fixture root is resolved, and `tests/run_all.py`
  fails on any unresolved one (U27).
- `.github/workflows/ci.yml` runs on master pushes, `v*` tags, and pull requests instead of
  every push.

### Found by pre-release adversarial review

An independent review of the changes above found five defects, each reproduced before it
was fixed and now covered by a selftest:

- A correction or ruling that matched only in its body was discarded by the weak-hit cap,
  and pinned lines were exempt from the total cap, so recall could exceed its own limit.
  Card kind is now decided before any cap, the cap covers every line, and pinned cards are
  searched in a window three times the ordinary one so a correction ranked below the
  top-k still surfaces.
- The `vaults:` legend could be dropped by a tight budget while `V1/...` hits survived,
  leaving aliases nothing could resolve. The legend now shares the required first block,
  and a path that cannot be made relative is printed in full rather than given an alias.
- A working-directory vault that also appeared in the configured list was dropped from
  session start.
- A request phrase inside Markdown backticks still counted as a request for a ruling.
- Captured cards are persistent, indexed, and re-injected later, so credential-shaped
  text (`token=`, `sk_...`, private-key headers, JWTs, `user:pass@host`) is now refused at
  capture time instead of being copied into the vault.
- Narration markers in the temp directory are swept after a day instead of accumulating.

## v1.0.0 — 2026-09-02

First public release: local recall over Markdown memory cards (SQLite FTS5), a card-driven
PreToolUse safety gate, a mechanical compaction map, decision-card lint, working-ledger
budget gate, owner-grant capture, and the `epitype-graft` installer for Claude Code and
Codex CLI.
