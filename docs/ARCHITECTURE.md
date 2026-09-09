# Architecture

Epitype is a governance layer over native CLI memory. Native stores remain the storage authority; Epitype adds structure, retrieval timing, action interception, auditability, and release checks.

## The three-block memory model

The three blocks have different change rates and different failure modes. Keeping them separate prevents a growing task list from becoming permanent policy and prevents a retired incident reflex from remaining resident forever.

| Block | Contains | Normal lifetime | Governance rule |
|---|---|---|---|
| Primary memory | Stable habits, preferences, and operating conventions | Long-lived | Keep only guidance that is broadly reusable and cheap enough to remain visible |
| Scars | Incident-born reflexes, including optional tool and input triggers | Independent lifecycle | Admit from evidence, intercept narrowly, and retire after the hazard is mechanically prevented |
| Pending ledger | Open work, blockers, owners, and next checks | Short-lived | Update as work changes; never promote an unfinished item into durable policy by accident |

Decision cards are a cross-cutting record type. They may live beside the domain they govern, but their current/superseded state is checked independently from the three-block placement.

## Three reading levels

Browsing memory and retrieving from it are different jobs. A hand-written index that
also tries to list everything drifts, and "reachable from the index" then becomes a
lint rule that forces people to maintain the list by hand — while recall never used
reachability in the first place.

| Level | File | Written by | Read when |
|---|---|---|---|
| 1 | `MEMORY.md` | Hand-written only | Every session (hosts that load it natively; echoed to hosts that do not) |
| 2 | `_views/current.md` | `epitype views` | Browsing what is in use; the fixed entry point for the complete list of active decisions |
| 3 | `_views/history/closed.md` | `epitype views` | Looking up what was closed or replaced |

The generator never writes `MEMORY.md`. That file has several concurrent writers —
sessions and the host's own auto-memory append to it — and a generator that rewrites
a region from a snapshot it read earlier silently drops whatever was appended in
between. It writes only the `_views/` tree it owns, under a lock shared by every
generating process, and rewrites nothing when the input fingerprint (each card's
path, mtime, and size) is unchanged.

Levels change by editing a field, not by moving a file, so links stay stable:

- `project` → `status: closed` (with optional `closed_at`, `closed_by`,
  `closed_evidence`); a project card with no status is listed under "needs review"
  rather than passed off as confirmed-current.
- `decision` → `status: superseded` with `superseded_by`; `active` stays at level 2,
  where the decision section lists every active decision, because the SessionStart
  ruling block is bounded (`forbidden` or recent, capped per vault) and is not a
  complete substitute.
- `feedback`, `reference`, `user`, `habit`, `scar` and event cards are never closed
  by project state; only `superseded` moves them.

`closed` changes the reading level and nothing else — the card stays in the search
index and is still recalled. Only `superseded` changes recall, by redirecting to the
successor. Two lint checks hold the pair together, because being listed in a view is
not the same as being findable: `card_lint --deep` reports managed cards missing from
the views and managed cards missing from the search index, each with the exact
command that rebuilds it.

Level 1 drifts back up on its own — the host's own default after saving a card is to
append a pointer line to `MEMORY.md`, and concurrent sessions edit it directly — so
putting things back down a level is the dream's job, not a gate's (§33). At 03:30 it
regenerates the views, then moves any card-link line that sits **outside** the
hand-written sections (`memspec.INDEX_ALLOWED_SECTIONS`) and whose cards level 2 or 3
already carries, verbatim, into `<vault>/_drafts/index_pruned/YYYYMMDD.md`. Links the
views do not carry stay put and are reported: an uncarried link may be a card written
minutes ago. The hand-written sections themselves are never touched, and any mismatch
between the read and the write abandons the whole pass rather than overwriting
another writer.

## Four retrieval routes

Epitype uses four routes because no single retrieval mode is correct for every piece of memory.

1. **Resident.** A small native index or stable rule block remains visible. Size limits keep residency selective instead of turning it into an unbounded prompt prefix.
2. **Point-in-time injection.** A hook injects selected context at a lifecycle event. Prompt recall and tool-trigger evaluation belong here; PreCompact writes a distinct, bounded recovery map per session or transcript so concurrent sessions do not overwrite one another.
3. **Agent-directed retrieval.** The resident index points to a fuller card, and the agent opens that card through the host's normal read path. This route is useful for detail that should not be permanently resident, but it is not sufficient for a rule that must intercept an action.
4. **Search.** `memsearch.py` builds a local trigram FTS index at `<vault>/.epitype/memory_fts.sqlite3` and supports explicit query or prompt-oriented recall. Only `build` creates an index; `query` and `recall` atomically move an existing legacy `.cairn` index into place before reading, fall back to that legacy snapshot with `index_migration_pending` if the move is blocked, and otherwise report no-index distinctly from a valid zero-hit result. Existing stale indexes retain the bounded incremental refresh path, with the refresh disclosed in the response. Search is a retrieval aid, not an authority source and not permission to act.

All injected memory is advisory data. It cannot override higher-priority instructions or grant tool authority. Hook output is capped at 10 KiB and hook execution is designed to fail open within the host's ten-second limit (the hook's own deadline is nine seconds, so a late answer is delivered rather than killed). Because failing open is silent, the work a call may do is bounded by design: one directory listing of each vault, one FTS query, and the handful of trigger cards a manifest cache names — never a card-by-card read, and never a resolved path (see failure mode 9).

## Decision cards

A decision card has five required fields (`aliases` is listed with the other types below):

| Field | Meaning |
|---|---|
| `decision_key` | Stable identity shared by every version of the same decision |
| `status` | `active` or `superseded` (`closed` is a project-card value and is rejected here) |
| `current_decision_at` | ISO date or timestamp at which this card's decision became current |
| `decided_by` | One of `owner-explicit`, `owner-implicit`, `ai-autonomous`, or `three-way` |

For every `decision_key`, exactly one card should be `active`. A superseded card is retained for provenance and carries `superseded_by` pointing to its replacement. `owner-explicit` cards also carry the required source quote.

The normative read rule is simple: only the active card is eligible to govern the next decision. `decision_lint.py` validates the fields and supersession links, requires exactly one active card for each key, and requires every `superseded_by` target to be an active card with the same key. `memsearch.py` indexes `status` and `superseded_by`, excludes superseded cards from `query` and `recall` by default, and preserves an explicit `--include-superseded` archaeology path. Its result payload also identifies the current replacement when a filtered predecessor matched but its successor did not.

Run the implemented contract checks with:

```powershell
python epitype/decision_lint.py --selftest
```

## Card types and required fields

One table, read off `CARD_REQUIRED_FIELDS` in `epitype/memspec.py`. `card_lint.py` and the write gate share it, so this is exactly what a blocked write is asking for.

| `type` | Required frontmatter fields |
|---|---|
| `decision` | `decision_key`, `status`, `current_decision_at`, `decided_by`, `aliases` |
| `scar` | `trigger.tool`, `trigger.input`, `advice`, `incident` |
| `grant`, `correction`, `ruling` | `name`, `description`, `captured_at`, `session_id` |
| `pending` | `owner`, `verify`, `exit` |
| `feedback`, `project`, `reference`, `user`, `habit` | `name`, `description` |

A card's type is inferred, not declared: a structural signal first (`decision_key`, a `trigger.*` field), then the event directory it sits in, then a self-reported `metadata.type`. Optional fields per type live beside the table in `CARD_OPTIONAL_FIELDS`.

The three event types also carry `provenance: auto-captured` and `verified: false` when a hook or a replay wrote them, plus `verified_by`/`verified_at` once a person has promoted one. Those fields are documentation of origin, not a gate input: the gates read `decision_key` and `trigger`, which is why a captured sentence can never become authority on its own (`docs/FAILURE_MODES.md` §32).

## Capture: admission and the proposal area

`epitype/capture.py` is the single judgment for the live hook and the offline replay. `classify()` decides *which* card a sentence would earn; `auto_admitted()` then decides whether that card is filed or only proposed, judging `owner_side(body, summary)` — the owner's own half of what is about to be stored, so a ruling's embedded assistant question cannot vouch for the owner. Three shape templates admit (`memspec.CAPTURE_ADMIT_*`): an arrow reply whose owner half opens with a short answer, a correction that opens the sentence, and a named first-person authorization.

Everything else lands under `<vault>/_drafts/captured_pending/YYYYMMDD/` with the filename it would have had. That path is already outside `memsearch._scan_vault` (any `_`- or `.`-prefixed part), so a proposal is invisible to the index, to recall, and to the write gate's card contract without a second exclusion rule. `existing_capture()` deduplicates across both locations, so a repeated sentence does not grow a fresh proposal every day. Promotion is a person's edit followed by a move; `harvest --reevaluate --apply` reports **HOLD** for an unverified proposal rather than promoting it, because every proposal is by construction one today's rules still capture.

## Scar lifecycle

1. **Incident.** Record an observable failure, its boundary, and enough evidence to reproduce or audit it. A transcript scanner may propose a candidate, but it does not write a scar automatically.
2. **Reflex.** Turn the narrow lesson into a scar card. If the failure can precede a tool action, add a `trigger.tool` regex, a `trigger.input` regex, and actionable `advice` that provides a safer route.
3. **Mechanization.** Move stable prevention into code, a hook, a deterministic check, or another machine-enforced guard. A scar does not count as mechanized merely because its prose is prominent.
4. **Retirement.** Remove the reflex from the resident scar block after the mechanism covers the original hazard. Preserve the incident and test evidence outside the resident path so the reason for the guard remains auditable.

The interceptor tests the scar-card form, bounded deny response, alternative advice, and audit row:

```powershell
python adapters/claude/pretooluse_gate.py --selftest
```

## Waking and dreaming

Epitype splits memory governance into two regimes. Waking runs inside hooks: deterministic, latency-bounded, no model calls. Dreaming is an offline consolidation pass — it may use a model, but every dream produces a review packet that a human or another model applies; nothing a dream proposes lands in the vault by itself.

`epitype/dream.py` is the deterministic half of dreaming: a read-only inventory across one or more vaults (missing aliases, card-lint FAIL/WARN, zombie pending lines, unreviewed drafts under `_drafts/`, the decision-card supersession chain, aging event cards, and cards added in the last 7 days), rendered as a numbered review packet with example rows and the existing CLI command that acts on each finding. It calls no model and writes nothing to the vaults it scans; a failure in one section is reported inline and does not stop the rest of the packet. Unreviewed drafts and the live event directories (`grants`/`corrections`/`rulings`) both re-judge against today's capture rules through `epitype/harvest.py --reevaluate` — forward, promoting a passing draft back under the vault, or reversed with `--quarantine-drops`, sweeping a vault's own event cards and moving the ones that no longer pass out to a quarantine directory — always apply-gated and never deleting a card. Capture proposals under `_drafts/captured_pending/` are the exception in the forward direction: they are HELD, not promoted, and the packet asks for review instead of offering that command.

Dreaming is scheduled, not asked for. `dream.mode` in `~/.epitype/config.json` selects one of three regimes. `piggyback` (the default) makes `SessionStart` check `<governance vault>/.epitype/dream_state.json`; once the gap since the last finished dream exceeds `dream.interval_hours` (24 by default), the hook starts one detached, low-priority background process and returns without waiting for it — a lock file holding the pid and the start time keeps a second dream from starting, and a lock older than 30 minutes is treated as a dead one and taken over. `nightly` leaves the run to the operating system: `graft install --dream nightly [--at HH:MM]` registers a daily task (`schtasks` on Windows, one marked `crontab` line elsewhere), `graft uninstall` removes it, and `graft doctor` reports the mode and the last completion. `off` does nothing at all. Every mode runs the same check — `epitype dream --scheduled` — which resolves the registered vaults, the governance vault, and its own output paths from the config, so a changed vault list never requires re-registering the schedule. (The `nightly` schedule entry itself still names the literal script path, `epitype/dream.py --scheduled`, since `schtasks`/`crontab` invoke an interpreter and a file, not the installed console command.)

The inventory itself is read-only, but the run carries two piggyback tasks that write into each vault: it regenerates `_views/` (nothing is rewritten when the input fingerprint is unchanged), and then it shapes `MEMORY.md` back into a short entry point, reporting both in the packet. Shaping runs second on purpose — "the catalogue already carries this card" is its only test, and a stale catalogue would answer it wrongly.

Apart from those two tasks, the background run's own output stays inside `<governance vault>/.epitype/`: `dream_pack_latest.md`, `dream_pack_latest.json`, `dream_state.json` (completion time, per-section counts, elapsed seconds) and `dream.log`. It gives itself a ten-minute budget and marks any section it did not reach rather than dropping it silently. The next session — but not one resuming after a compaction — opens with one line naming the four headline numbers and the pack path, once; a dream that found nothing still says it ran, because silence cannot be told apart from a dream that never happened. None of this calls a model: the model half of dreaming stays manual, so an installed Epitype never spends model budget on its own.

```powershell
python epitype/dream.py --selftest
```

## Source-of-authority rules

1. **Higher law outranks lower law.** Host safety and instruction-priority rules cannot be amended by memory.
2. **Special law outranks ordinary law within its scope.** A narrow project or domain rule beats a generic convention for that project or domain.
3. **Later law wins only among rules of the same authority and scope.** Recency does not borrow authority from a lower source.
4. **A later ordinary rule does not silently amend an earlier special rule.** The special rule remains controlling until an equally specific authorized decision replaces it.
5. **Conflicts are reported.** If authority, scope, or supersession cannot be resolved, surface the conflicting sources and stop treating either as an unqualified current instruction.

These rules govern selection; they do not let memory override the host's system, developer, permission, or safety boundaries.

## Shared contract across hosts

Supported Claude Code and Codex registrations use the same five event names: `SessionStart`, `UserPromptSubmit`, `PreCompact`, `PreToolUse`, and `Stop`. They also share the vault list and limits from `epitype/memspec.py`. Host adapters translate the event envelope; they do not fork the memory format.

This is a two-host implementation boundary. Future hosts and future host upgrades require their own integration tests before they can be described as supported.
