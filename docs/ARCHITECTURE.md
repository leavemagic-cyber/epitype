# Architecture

Epitype is a governance layer over native CLI memory. Native stores remain the storage authority; Epitype adds structure, retrieval timing, two content gates, auditability, and release checks. Irreversible actions are not its job: those belong to the host's own native rules (Claude `permissions.deny`, Codex `execpolicy`), which refuse the call before it runs.

## The three-block memory model

The three blocks have different change rates and different failure modes. Keeping them separate prevents a growing task list from becoming permanent policy and prevents a retired incident reflex from remaining resident forever.

| Block | Contains | Normal lifetime | Governance rule |
|---|---|---|---|
| Primary memory | Stable habits, preferences, and operating conventions | Long-lived | Keep only guidance that is broadly reusable and cheap enough to remain visible |
| Scars | Incident-born reflexes: the incident, and the safer route to take instead | Independent lifecycle | Admit from evidence, state the safer route narrowly, and retire after the hazard is mechanically prevented |
| Pending ledger | Open work, blockers, owners, and next checks | Short-lived | Update as work changes; never promote an unfinished item into durable policy by accident |

Decision cards are a cross-cutting record type. They may live beside the domain they govern, but their current/superseded state is checked independently from the three-block placement.

## Four reading levels

Browsing memory and retrieving from it are different jobs. A hand-written index that
also tries to list everything drifts, and "reachable from the index" then becomes a
lint rule that forces people to maintain the list by hand — while recall never used
reachability in the first place.

Owner 2026-09-09, verbatim: 「其他全部按需讀：索引指到卡片，卡片只在喚回時出現；卡片
下面才是解說，再下面才是原話和對話紀錄，要用到才翻」. Each level is opened by the one
above it, never sent ahead of it.

| Level | File | Written by | Read when |
|---|---|---|---|
| 1 | `MEMORY.md` | Hand-written only | Every session (hosts that load it natively; echoed to hosts that do not) |
| 2 | `_views/current.md` | `epitype views` | Browsing what is in use; the fixed entry point for the complete list of active decisions |
| 3 | `_views/history/closed.md` | `epitype views` | Looking up what was closed or replaced |
| 4 | `grants/` `corrections/` `rulings/` | Auto-capture, verbatim | Somebody needs the owner's exact words — reached by `memsearch`, one file at a time, because a card pointed there (failure mode 41) |

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
  where the decision section lists every active decision — since §35 that view and
  `epitype decisions` are the only places a full list exists, because session start
  no longer recites one.
- `feedback`, `reference`, `user`, `habit`, `scar` and event cards are never closed
  by project state; only `superseded` moves them.

Level 4 is the one level recall never sends. The quote files stay in the search
index — that is how an AI reaches them — but `recall_hook._event_card` drops every
hit under those three directories before it can take a seat, so a sentence nobody
curated cannot arrive beside the cards that were. The single exception is a file in
one of those directories that is itself an active decision card (`decision_key` +
`status: active`): that is level 2 material that happens to sit in a capture
directory, and it keeps its pinned seat.

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
minutes ago. The hand-written sections themselves — and the preamble above the first
one — are never touched, and any mismatch between the read and the write abandons the
whole pass rather than overwriting another writer.

## Four retrieval routes

Epitype uses four routes because no single retrieval mode is correct for every piece of memory.

1. **Resident.** A small native index or stable rule block remains visible. Size limits keep residency selective instead of turning it into an unbounded prompt prefix.
2. **Point-in-time injection.** Cards only: prompt recall pins active decision cards, fills the rest of the window with ordinary cards, and injects no level-4 quote file at all (failure mode 41). A hook injects selected context at a lifecycle event. Prompt recall and the two content gates belong here; PreCompact writes a distinct, bounded recovery map per session or transcript so concurrent sessions do not overwrite one another, and emits nothing at all: neither host delivers PreCompact output to the model, so the path is handed back afterwards by SessionStart, which on `source=compact` alone adds one line naming that map file when it exists (both hooks compute the destination with the same `epitype.compact_map.map_destination`, and a path that would push the line past 240 UTF-8 bytes drops the whole line rather than truncating a path nothing could read back). SessionStart is otherwise deliberately the thinnest of these: it emits only lines that name something to do — a card-type FAIL, the by-the-way alias task, a dream that errored, left review candidates, or is past due — and a session with none of those gets no injection at all. Nothing standing is re-sent at every session: not the work ledger, the vault's active rulings or the overdue-pending list (failure mode 35), and not the short index either, which the host loads for itself out of `CLAUDE.md` / `AGENTS.md` (failure mode 40). Those are read on demand, and a rule that must reach the model when a prompt touches it belongs to recall, not to the prologue.
3. **Agent-directed retrieval.** The resident index points to a fuller card, and the agent opens that card through the host's normal read path. This route is useful for detail that should not be permanently resident, but it is not sufficient for a rule that must intercept an action.
4. **Search.** `memsearch.py` builds a local trigram FTS index at `<vault>/.epitype/memory_fts.sqlite3` and supports explicit query or prompt-oriented recall. Only `build` creates an index; `query` and `recall` atomically move an existing legacy `.cairn` index into place before reading, fall back to that legacy snapshot with `index_migration_pending` if the move is blocked, and otherwise report no-index distinctly from a valid zero-hit result. Existing stale indexes retain the bounded incremental refresh path, with the refresh disclosed in the response. Search is a retrieval aid, not an authority source and not permission to act.

All injected memory is advisory data. It cannot override higher-priority instructions or grant tool authority. Hook output is capped at 10 KiB and hook execution is designed to fail open within the host's ten-second limit (the hook's own deadline is nine seconds, so a late answer is delivered rather than killed). Because failing open is silent, the work a call may do is bounded by design: one directory listing of each vault, one FTS query, and the decision cards a manifest cache names — never a card-by-card read, and never a resolved path (see failure mode 9).

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

## Core generation (rule cards → the resident block)

The resident core is the one memory that is read on every turn of every session, so
every byte of it is a fixed cost. Owner 2026-09-09 settled its shape: a hand-written
floor plus resident sentences that are **generated from cards**, one rule per card,
with a cap set from the reviewed size plus twenty percent. `epitype/core_gen.py` is
the generator; it is the only thing in the product that assembles a core block, and
it assembles nothing else.

A `rule` card carries the sentence and its provenance:

| Field | Meaning |
|---|---|
| `layer` | `floor`, `resident`, `situational`, or `recall` — where the sentence is read |
| `section` | Free-text grouping inside the resident layer (e.g. `evidence`) |
| `order` | Integer sort key inside a layer |
| `text` | The **approved sentence**, one line, any language, at most `RULE_TEXT_MAX_BYTES` |
| `decided_by` | Same domain as a decision card; `owner-explicit` also requires `owner_quote` |
| `approved_by` / `approved_at` | Who approved this exact wording, and when |
| `aliases` | At least one, so the card is reachable by search |
| `hosts` | Optional: the hosts this rule is only for (`claude`, `codex`); absent means shared |

Also optional: `source_anchor` (where the sentence came from), `incidents` (dated
one-liners), and `status`/`superseded_by` with the decision card's supersession meaning. Explanation,
examples, and the incident narrative belong in the card body and the explanation layer —
not in `text`, because `text` is what everyone pays for on every turn.

Only `floor` and `resident` are generated. `situational` and `recall` cards exist for
the same reason recall exists: a rule that must arrive when a prompt touches it is not
a rule that must arrive every time. The generator therefore does three things and
nothing else — **select** (the two generated layers, skipping `superseded`), **order**
(`floor` numbered by `order`; `resident` grouped into `section` subsections, section
order taken from each section's lowest `order`), and **copy** (`text` byte for byte).
Only the title, the note line, the headings and the list markers are the product's own
words, and all of them come from language-neutral templates in `memspec`: the product
carries no behavior-rule text of its own (failure mode 30).

Two refusals, both of which write nothing at all:

- **No approval, no generation.** A card in a generated layer without `approved_by`
  and `approved_at` stops the whole run and is listed. A core block is the wrong place
  to discover that one sentence was never approved.
- **Over the cap, no generation.** `--cap-bytes`, else `core_cap_bytes` from the
  config; unset means no cap, because a cap the product guessed is not the owner's
  threshold (failure mode 36). Over it, the ten longest cards are listed instead.

A successful run also writes an approval pack to `<vault>/.epitype/core_gen_latest.json`:
per card the vault, path, layer, section, order, approver, and the SHA-256 of its
`text`; for the output the SHA-256, the byte count, the cap in force, and the time.
That is what lets a later reader prove which approved wordings a given core block was
built from.

### Host zones (a rule only one host is missing)

Some rules exist only to supply what one host does not provide natively. Owner
2026-09-10 settled where those live: what both hosts need goes in the shared core, and
a gap on one side only goes into that side's zone, so the other host does not pay for
it on every turn. A card says so with `hosts` — a sequence over `claude` and `codex`,
absent meaning shared. A `floor` card may not carry it (a floor sentence one host never
reads is a floor with a hole in it), and `card_lint` fails one that does.

Host-only `resident` cards assemble into a third section, `## C. host zones`, one
comment-delimited zone per host; a card naming both hosts appears once in each zone:

```markdown
## C. host zones
<!-- HOST claude BEGIN -->
### section
- the approved sentence
<!-- HOST claude END -->
```

`core_gen.host_view(text, host)` is a pure function returning what that host actually
loads — the shared sections plus its own zone, with the other zones and every marker
removed. Writing that into a host file is still not the product's job; the point is
that the generator and a downstream sync script share one definition of "what this host
loads" instead of keeping two. That same number is what `--cap-bytes` measures — the
largest single host load, not the size of the file, because no session ever pays for
both zones — and the run prints every host's load. The approval pack records each
card's `hosts`, the bytes of each zone, and each host's load.

A vault where no card carries `hosts` assembles exactly the bytes it did before this
existed: no `host-only` count on the note line, no `## C.` section, and `host_view`
handing back the text unchanged.

`--check` runs the same assembly and compares it with `--out` byte for byte, exiting
non-zero on a difference and writing nothing. The dream's §11 calls the same
predicate for every configured `core_files` entry and lists a drifted block as a
report-only candidate — with one deliberate silence: a vault holding no rule cards
yields no comparison at all, so a machine that has not adopted rule cards yet is not
told nightly that its core block "drifted" from an empty assembly.

Writing the assembled block into a host file (`CLAUDE.md`, `AGENTS.md`) is not the
product's job and is not in this repository: the generator writes the file it is
pointed at, and nothing else.

```powershell
python epitype/core_gen.py --selftest
```

## Card types and required fields

One table, read off `CARD_REQUIRED_FIELDS` in `epitype/memspec.py`. `card_lint.py` and the write gate share it, so this is exactly what a blocked write is asking for.

| `type` | Required frontmatter fields |
|---|---|
| `decision` | `decision_key`, `status`, `current_decision_at`, `decided_by`, `aliases` |
| `rule` | `layer`, `section`, `order`, `text`, `decided_by`, `approved_by`, `approved_at`, `aliases` |
| `scar` | `advice`, `incident` |
| `grant`, `correction`, `ruling` | `name`, `description`, `captured_at`, `session_id` |
| `pending` | `owner`, `verify`, `exit` |
| `feedback`, `project`, `reference`, `user`, `habit` | `name`, `description` |

A card's type is inferred, not declared: a structural signal first (`decision_key`), then the event directory it sits in, then a self-reported `metadata.type`. Optional fields per type live beside the table in `CARD_OPTIONAL_FIELDS`. `trigger` is a retired field: a card that still carries one is linted with a single WARN and read by nothing (`docs/FAILURE_MODES.md` §31).

The three event types also carry `provenance: auto-captured` and `verified: false` when a hook or a replay wrote them, plus `verified_by`/`verified_at` once a person has promoted one. Those fields are documentation of origin, not a gate input: both gates read `decision_key`, which is why a captured sentence can never become authority on its own (`docs/FAILURE_MODES.md` §32).

## Capture: admission and the proposal area

`epitype/capture.py` is the single judgment for the live hook and the offline replay. `classify()` decides *which* card a sentence would earn; `auto_admitted()` then decides whether that card is filed or only proposed, judging `owner_side(body, summary)` — the owner's own half of what is about to be stored, so a ruling's embedded assistant question cannot vouch for the owner. Three shape templates admit (`memspec.CAPTURE_ADMIT_*`): an arrow reply whose owner half opens with a short answer, a correction that opens the sentence, and a named first-person authorization.

Everything else lands under `<vault>/_drafts/captured_pending/YYYYMMDD/` with the filename it would have had. That path is already outside `memsearch._scan_vault` (any `_`- or `.`-prefixed part), so a proposal is invisible to the index, to recall, and to the write gate's card contract without a second exclusion rule. `existing_capture()` deduplicates across both locations, so a repeated sentence does not grow a fresh proposal every day. Promotion is a person's edit followed by a move; `harvest --reevaluate --apply` reports **HOLD** for an unverified proposal rather than promoting it, because every proposal is by construction one today's rules still capture.

## Scar lifecycle

1. **Incident.** Record an observable failure, its boundary, and enough evidence to reproduce or audit it. A transcript scanner may propose a candidate, but it does not write a scar automatically.
2. **Reflex.** Turn the narrow lesson into a scar card: the `incident` it came from and actionable `advice` that provides a safer route. Most cards are context read back into a turn and refuse nothing; a card that declares `guard_tool` with `guard_all_of` also denies a tool call whose text carries every literal fragment it names (2026-09-16, FAILURE_MODES §3), with no regex and no reading of intent.
3. **Mechanization.** Move stable prevention into code, a deterministic check, or the host's own native rules (Claude `permissions.deny`, Codex `execpolicy`), which refuse the call before it runs. A scar does not count as mechanized merely because its prose is prominent.
4. **Retirement.** Remove the reflex from the resident scar block after the mechanism covers the original hazard. Preserve the incident and test evidence outside the resident path so the reason for the guard remains auditable.

## The two gates

Epitype refuses exactly two things, both about content the model is about to commit, and neither expressible as a host rule:

- **Write gate** (`PreToolUse`, `adapters/claude/pretooluse_gate.py`). Before a file write lands, the new text is checked against the settled rulings — rule A blocks content that re-states what the owner already ruled out — and a card written into a registered vault must satisfy `card_lint`'s contract for its own type (rule B: FAIL blocks, WARN advises). Every block appends one audit row to `<vault>/_GATE_LOG.jsonl` naming the rule, the ruling, and the filename — never the content. Anything else proceeds untouched.
- **Stop gate** (`adapters/claude/stop_gate.py`). At the end of a turn, the last assistant message is checked against the same decision cards and the same `forbidden` patterns, through one shared validator, so a sentence that cannot be written into a file cannot be said at the end of a turn either.

Both gates fail open, and both are exercised by their own synthetic events:

```powershell
python adapters/claude/pretooluse_gate.py --selftest
python adapters/claude/stop_gate.py --selftest
```

## Waking and dreaming

Epitype splits memory governance into two regimes. Waking runs inside hooks: deterministic, latency-bounded, no model calls. Dreaming is an offline consolidation pass — it may use a model, but every dream produces a review packet that a human or another model applies; nothing a dream proposes lands in the vault by itself.

`epitype/dream.py` is the deterministic half of dreaming: a read-only inventory across one or more vaults (missing aliases, card-lint FAIL/WARN, zombie pending lines, unreviewed drafts under `_drafts/`, the decision-card supersession chain, aging event cards, and cards added in the last 7 days), rendered as a numbered review packet with example rows and the existing CLI command that acts on each finding. It calls no model and writes nothing to the vaults it scans; a failure in one section is reported inline and does not stop the rest of the packet. Unreviewed drafts and the live event directories (`grants`/`corrections`/`rulings`) both re-judge against today's capture rules through `epitype/harvest.py --reevaluate` — forward, promoting a passing draft back under the vault, or reversed with `--quarantine-drops`, sweeping a vault's own event cards and moving the ones that no longer pass out to a quarantine directory — always apply-gated and never deleting a card. Capture proposals under `_drafts/captured_pending/` are the exception in the forward direction: they are HELD, not promoted, and the packet asks for review instead of offering that command.

Dreaming is scheduled, not asked for. `dream.mode` in `~/.epitype/config.json` selects one of three regimes. `piggyback` (the default) makes `SessionStart` check `<governance vault>/.epitype/dream_state.json`; once the gap since the last finished dream exceeds `dream.interval_hours` (24 by default), the hook starts one detached, low-priority background process and returns without waiting for it — a lock file holding the pid and the start time keeps a second dream from starting, and a lock older than 30 minutes is treated as a dead one and taken over. `nightly` leaves the run to the operating system: `graft install --dream nightly [--at HH:MM]` registers a daily task (`schtasks` on Windows, one marked `crontab` line elsewhere), `graft uninstall` removes it, and `graft doctor` reports the mode and the last completion. `off` does nothing at all. Every mode runs the same check — `epitype dream --scheduled` — which resolves the registered vaults, the governance vault, and its own output paths from the config, so a changed vault list never requires re-registering the schedule. (The `nightly` schedule entry itself still names the literal script path, `epitype/dream.py --scheduled`, since `schtasks`/`crontab` invoke an interpreter and a file, not the installed console command.)

Five later sections answer a different question from the first seven: not "what is wrong with a card" but "what never got confirmed and is still sitting there" (owner 2026-09-09 — the dream is what puts unconfirmed material back into the layer or the card it belongs in). Every one of them is **report-only**: nothing is moved, split, promoted, or deleted, because each disposal is a human judgement.

- **§8 pocket vaults.** Registered vaults cannot answer "how many places are producing unfiled memory", so this counts from disk instead: every `<home>/.claude/projects/*/memory/` directory that is not registered and holds at least one `*.md` card is listed as a filing candidate with its path, card count, and newest mtime. The home directory is never hardcoded — it is recognised from a registered vault's own path (`.claude/projects` walking up) and only falls back to `HOME`. Only that shape counts: taking "the registered vault's grandparent is the root" would make a vault at `C:\a\b` scan every directory under `C:\`.
- **§9 draft aging.** `_drafts/**` per registered vault: total, older than 7 days, older than 30, grouped by first-level subdirectory, and the five oldest with their age in days. §4 counts how many drafts are waiting; this counts how long they have waited (the backlog measured 370 files on 2026-09-09).
- **§10 mixed cards.** One card is one memory or one rule. Three shape signals, any one of which lists a split candidate: two or more `## ` headings in the body (fenced code is skipped, since cards quote markdown), a body over `memspec.CARD_BODY_MIXED_BYTES`, or a `description` over 160 characters that strings several things together with `＋`/`；`. Fifty rows per vault; past that only the total. Human review outranks the shapes: a card carrying `memspec.MIXED_REVIEWED_FIELD` (any value — only the key is read, so no language is hardcoded), a `status: superseded` card, and a card with `split_from` that is now one heading and under the byte cap are skipped and counted instead, reported as 「已審過略過 N 張」; the skip is counted only for a card the shapes would otherwise have listed, so the number is exactly what review took off the list, and a split-out card that grew two headings or ran over the cap is listed again on its own account.
- **§11 caps.** Three optional config keys — `index_cap_bytes`, `core_files` (absolute paths), `core_cap_bytes`. A missing key writes one "unset" line and judges nothing: a cap the product guessed, reported as "over cap", would read as the owner's own threshold. What is over is listed with its size, cap, and overage; no file is touched. The same `core_files` list is also compared against a fresh assembly from the vault's rule cards, and a block that no longer matches is listed as a drift candidate — report-only, and skipped entirely when there are no rule cards to assemble from (see [Core generation](#core-generation-rule-cards--the-resident-block)).
- **§12 quotes no decision card carries.** Recall serves `rulings`/`corrections`/`grants` cards under the `⚖ owner 裁決：`/`⚠ owner 曾糾正：` prefixes, and only a card that declares `verified: false` is demoted to a historical capture. So an event card that is still trusted, but that no `type: decision` card mentions — in its body or its `source`/`superseded_by`/`aliases` — reads like a standing ruling with nobody behind it. Those are listed newest first (path, `captured_at`, a suspected-noise column, first 80 characters), thirty per vault. The match is on the full filename and the `decision_key`, never a bare stem: `carried` is a substring of `uncarried`, and a stem match would report "carried" for a quote nobody carries. Naming is only one of **three carry routes**, because most cards carry a quote without ever writing its filename: a decision card that quotes the sentence verbatim in `owner_quote` carries it (both texts are normalised to letters and digits — NFKC first, then every separator, quote and punctuation dropped, so no language's punctuation is hardcoded — and the overlap must run at least twelve characters, since a four-character overlap happens in any two Chinese sentences), and so does an event card that names its carrier in its own `carried_by` field (whether that card exists is `epitype cards`' question, not this section's). The noise column marks a row whose body matches one of `memspec.EVENT_NOISE_MARKERS` — the fixed cross-CLI transport-probe templates that auto-capture files as rulings; marking only, nothing is changed or deleted.

Section 4 carries one more piggyback task, ahead of its own count: the run replays `epitype/harvest.py` over the host transcripts in drafts-only mode (`harvest(..., drafts_only=True)`, or `--drafts-only` on the CLI), so a sentence the online capture missed while a session was running still reaches the vault the same night. Drafts-only closes the admission gate outright — every captured sentence lands in `_drafts/captured_pending/`, no card reaches `grants`/`corrections`/`rulings`, and no index is rebuilt or aged — because nobody is watching that run. Its cursor is harvest's own per-file fingerprint manifest, not a date: a file is recorded only after it has been read end to end, so a run that stops early leaves the rest for the next one. It stops on a `memspec.DREAM_HARVEST_BUDGET_SECONDS` (30 s) deadline, or the packet's own deadline if that comes first, and it stops between files, never inside one. A failure costs the section one error line and nothing else; `--dry-run` is passed straight through, so a dry run counts what it would propose and writes nothing. The packet reports the number actually added (`harvest_new_drafts`, with `harvest_files` scanned) as 「本次 harvest 新增 N 張草稿」.

The inventory itself is read-only, but the run carries two piggyback tasks that write into each vault: it regenerates `_views/` (nothing is rewritten when the input fingerprint is unchanged), and then it shapes `MEMORY.md` back into a short entry point, reporting both in the packet. Shaping runs second on purpose — "the catalogue already carries this card" is its only test, and a stale catalogue would answer it wrongly.

Apart from those two tasks, the background run's own output stays inside `<governance vault>/.epitype/`: `dream_pack_latest.md`, `dream_pack_latest.json`, `dream_state.json` (completion time, per-section counts, elapsed seconds) and `dream.log`. It gives itself a ten-minute budget and marks any section it did not reach rather than dropping it silently. The next session — but not one resuming after a compaction — opens with one line, once, and only when someone has to act: the run left headline numbers to review, or it did not finish. A dream that finished clean says nothing; what distinguishes "clean" from "never ran" is the other line, which appears when the dream is past its `interval_hours` and no unexpired lock says one is running (failure mode 35). None of this calls a model: the model half of dreaming stays manual, so an installed Epitype never spends model budget on its own.

```powershell
python epitype/dream.py --selftest
```

## 回饋檢討 / Feedback review

Owner ruling, 2026-09-09: 「應該有回饋檢討機制」. The loop is deliberately cheap — nothing new happens during a session, no session pays for it, and the dream only assembles candidates. Judging them is a separate, occasional sitting.

**Every event carries its own identity.** A captured card used to be identified by *which sentence* it held (the digest in its filename), so the same sentence said in three different conversations left one card: card count could never stand in for incident count. Each captured card now also carries `event_id` — a stable hash of host, conversation, message position, and sentence — and `origin`, the readable `host/session/position` form of that same identity (`epitype/capture.py`; both the live hook and the offline replay compute them in one place). Deduplication asks "is this the same event": a resent event is never rewritten, the same sentence inside one conversation is still one card, and the same sentence in a different conversation keeps its own. The host is read from the transcript's location (`.codex/…/rollout-*` versus `.claude/projects/…`), and a Codex event with neither `session_id` nor `sessionId` falls back to the rollout filename, which is that conversation's only name — so nothing here depends on `SessionEnd`.

**The exam says which rule each question is about.** `exam/exam_runner.py` carries any `cards` / `card` / `decision_key` the question itself declares out with the result and prints `UNMAPPED n/total`, because a failure that cannot be traced back to a rule is a gap, not a zero. A question's `setup.vault_cards` are its synthetic fixtures, never that mapping. When `EPITYPE_CONFIG` points at a configuration — and only then, and not under `--dry-run` — the run also writes `<governance vault>/.epitype/exam_results_latest.json`: question id, pass/fail, cards, and `rules_version`, the first 12 characters of the corpus file's SHA-256.

**§15 of the dream packet is the review pack.** It lines four sources up against the cards they point at — event cards (`matched_card`, `event_id`, `verified`), the `stop_block` and `write_block` rows of `_GATE_LOG.jsonl` (read through `epitype/gates_report.py`, so the numbers match the block report), the failed questions in `exam_results_latest.json`, and the §8–§12 candidate counts as background. Each named card gets one row: events (deduplicated by `event_id`), blocks, exam failures, and the latest date. "Items to judge" is the row count, and when it reaches `memspec.REVIEW_PACK_TRIGGER` (5) the packet's next steps carry one line saying so; below it the section says how far off it is. Two things are shown but never counted, because both would count the same thing twice and keep the trigger permanently satisfied: the §8–§12 candidates, which already have their own next-step lines, and events with no `matched_card`, which are what §12 measures. A `verified: false` event is listed, never counted as a confirmed incident. The section changes no card, moves no layer, judges no type — those are the review sitting's job — and it never reaches `SessionStart`, which reads only the state file's headline fields.

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
