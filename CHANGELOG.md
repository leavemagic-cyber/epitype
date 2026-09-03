# Changelog

## v1.1.0 — 2026-09-03

The token-economy release. Every line Epitype injects is paid for once as output and
again on every later turn as re-read context, so this release measures that cost on real
prompts and cuts it, and it closes the gaps where an owner's own words were being lost.

### Injection cost (measured on 40 real prompts)

| | v1.0.0 | v1.1.0 |
|---|---|---|
| Recall injection per prompt | 3.6 KB / 11 lines | **2.3 KB / 7.7 lines** |
| Absolute paths as a share of it | 32% | 17% |
| Session start (non-governance project) | 10 KB, hitting the budget cap | 7.6 KB |

- Recall prints one `vaults:` legend line and `V1/relative` paths instead of repeating an
  absolute vault path on every hit; descriptions are truncated; at most two body-only hits
  per vault; the whole block is capped, with pinned corrections and rulings first (U29).
- Session start injects the working directory's own vault plus the governance vault only.
  Another project's index and ledger no longer crowd the budget (U29).
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

## v1.0.0 — 2026-09-02

First public release: local recall over Markdown memory cards (SQLite FTS5), a card-driven
PreToolUse safety gate, a mechanical compaction map, decision-card lint, working-ledger
budget gate, owner-grant capture, and the `epitype-graft` installer for Claude Code and
Codex CLI.
