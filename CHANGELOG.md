# Changelog

## Unreleased

The hot-path release. Measured on a machine at 100% CPU with two real vaults
(158 + 294 cards): a recall or gate call that took 4.5–5.5 s — past both the
host's and its own 3 s deadline, so it silently injected nothing and let a
destructive command through — now takes 1.2–1.7 s end to end, and the work
inside the interpreter fell from 4.0–4.6 s to about 0.4 s.

- The vault scan reads directory entries and never resolves a path: symlinks and
  Windows junctions are recognised from the entry's own attributes, and `_`- or
  `.`-prefixed parts are never entered. Inside its grace window the stale check
  reads nothing; after it, the stored path/mtime/size manifest is compared, so
  deletions, renames, backdated additions, and size changes cannot stay hidden.
  Results carry `card_path`, so no caller resolves the vault again. The selftest
  lists 300 cards and asserts that no path is resolved.
- The action gate finds trigger cards through a manifest cache
  (`<vault>/.epitype/gate_triggers.json`) and re-reads only cards that changed.
  Its regex validator rejects only shapes that can backtrack exponentially
  (nested unbounded repetition, alternation under repetition, backreferences);
  adjacent repetitions and bounded groups are accepted and the length limit is
  1024. A card the gate cannot use is named to the model once per session rather
  than skipped silently. A matched rule stays a deny when audit logging is
  contended or the advice exceeds the output ceiling.
- Recall lines say each fact once: a name the path already spells and the
  "owner … auto-captured" label are dropped (about 40% fewer bytes per pinned
  line). The advisory and vault legend are sent once per session per distinct
  legend; compaction clears the session's recall markers, so they — and any
  correction or ruling injected before it — return afterwards. A capture that
  cannot take the index lock ages the index so the next prompt rebuilds it.
  Every injected block that a budget cuts ends with the count of pieces left
  out instead of silently skipping middle pieces.
- One frontmatter reading for the index, the lints, and the gate
  (`memspec.split_frontmatter`, `parse_scalar`, `join_block_scalar`): the BOM,
  CRLF, `...` document ends, duplicated keys (first wins), `|-`/`>-` block
  scalars, and a quoted `#` now read the same everywhere;
  `tests/frontmatter_consistency.py` proves it card by card.
- Decision lint: a key whose every card is retired warns rather than fails,
  keeping the exam contract; supersession chains are valid unless they loop; a
  replacement must share the decision key.
- Installer: doctor accepts a `python` on PATH or any existing interpreter in a
  registration instead of the exact path of the interpreter running doctor;
  host-file backups keep only the newest three; `hook_trust` prints
  `UNVERIFIED` (exit 1) instead of a `SKIP` that read as green; `python -m
  epitype` gains a `__main__` guard; a tag publish requires `package.json` to
  carry the tag version; `tests/run_all.py --jobs N` runs selftests in parallel
  on CI.
- From the 2026-09-04 batch, kept as found: incremental index builds use mtime
  and size; owner captures and compact maps share the governance vault; compact
  maps are per session and bounded; the installed package provides a unified
  `epitype` command and CI exercises the installed wheel; Codex trust requires
  exactly the four supported registrations.

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
