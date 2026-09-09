# Epitype

[繁體中文](README.zh-TW.md)

Epitype is a memory governance layer for CLI agents.

It keeps the host's native memory as the storage authority, then adds the structure, timing, action gates, and evidence needed for remembered rules to affect later behavior.

An agent can retrieve the right fact and still break the rule attached to it. Epitype focuses on that gap:

- a replaced decision should not return as current;
- an incident lesson should reach the tool action where it matters;
- a permission should remain attributable to the person who gave it;
- a memory failure should be visible and testable.

Epitype currently supports Claude Code and Codex. It uses only the Python standard library and does not require a hosted memory service.

## How it works

Epitype connects the same native vaults to five host events:

| Event | What Epitype does |
|---|---|
| `SessionStart` | Echoes the bounded memory index to hosts that do not load it natively, plus any line that names something to do (a card-type FAIL, a dream that errored, left candidates, or is past due). Nothing standing is re-sent. |
| `UserPromptSubmit` | Recalls up to five relevant cards from each resolved vault within the shared output budget. Short owner statements are stored verbatim, deduplicated, and indexed; their meaning is not inferred during capture. |
| `PreToolUse` | Write gate: checks the content a file write is about to commit against the settled rulings and the card contract. A block returns the ruling and an audit row. |
| `PreCompact` | Builds a small recovery map from the transcript tail before context compaction. |
| `Stop` | Round-end decision gate: blocks a reply that re-proposes a rejected option or re-asks a ruled question. |

Injected memory remains advisory. It cannot override system or developer instructions, bypass host permissions, or grant a tool authority by itself. Hook output is capped at 10 KiB and each hook has a ten-second fail-open deadline.

### Capture: filed, or proposed

A trigger match proves a sentence *looks like* a ruling, not that anyone checked it,
so capture files only what one of three shape templates admits — an arrow reply
whose owner half opens with a short answer, a correction that opens the sentence, or
a named first-person authorization. Everything else the rules still capture is
written to `<vault>/_drafts/captured_pending/YYYYMMDD/` instead: not indexed, not
recalled, waiting for a person. Both kinds carry `provenance: auto-captured` and
`verified: false`, and promotion means editing those fields (`verified: true` plus
`verified_by`/`verified_at`) and moving the file — a replay refuses to do it.

No `verified: false` card is authority for anything: the Stop decision gate and the
write gate both read cards that declare `decision_key`, which a captured card never
does.
Recall still surfaces it, labelled as history rather than as a standing decision.
Details and the measured trade in `docs/FAILURE_MODES.md` §32.

## Governance beyond recall

### Current decisions

Decision cards have a stable `decision_key`, an `active` or `superseded` status, an effective time, and a named decision source. Exactly one card should be active for each key. `query`, `recall`, and the prompt hook exclude superseded cards by default while retaining them for provenance. Use `--include-superseded` only when you want the history.

### Scars, and what actually stops an action

A scar is an incident-born rule: the `incident` it came from and actionable `advice` that names the safer route. A card is context read back into a turn, not a refusal — it cannot stop a tool call, and a lexical pattern pretending otherwise produces both false denials and false confidence. Irreversible actions belong to the host's own native rules (Claude `permissions.deny`, Codex `execpolicy`), which refuse the call before it runs. Epitype refuses only content: the write gate below, and the Stop gate at the end of a turn.

### Native-first installation

The installer merges only entries marked as Epitype, keeps detected native vaults, and writes backups before changing an existing host file. Stable shims let the repository move without rewriting every host registration. Uninstall removes Epitype-owned registrations and configuration while preserving native memory and vault cards.

### Tidy-up that runs itself

The offline inventory pass ("the dream") does not wait to be remembered. By default (`dream.mode: piggyback`) a session start whose last dream is older than `dream.interval_hours` starts one detached, low-priority background process and returns without waiting; a pid-bearing lock, stale after 30 minutes, keeps a second one from starting. `graft install --dream nightly [--at HH:MM]` registers a daily system task instead (`graft doctor` shows the mode and the last completion; `graft uninstall` removes the task), and `--dream off` disables both. The run reads vaults and writes only `<governance vault>/.epitype/` — review pack, state, log — inside a ten-minute budget, and the next session announces it in one line. Its last section is the feedback review pack: one row per card that owner events, gate blocks or exam failures point at, flagged once enough rows accumulate (`memspec.REVIEW_PACK_TRIGGER`, 5) that a review sitting is worth holding. It judges nothing and changes no card. No model is called: the model half of tidying stays manual, so an installed Epitype never spends model budget on its own.

### Failure evidence

Missing indexes, stale indexes, shim failures, malformed cards, and lock contention have distinct outcomes. The hooks fail open when they cannot safely finish, and the installer doctor reports recorded shim outages instead of treating silence as health.

`epitype gates <vault> [--since Nd|YYYY-MM-DD] [--json] [--by kind|decision|session|day]` turns a vault's `_GATE_LOG.jsonl` into a read-only report of what the gates actually blocked, by kind, decision or scar card, day, and session, plus a same-session-same-card ≥3 hint for suspected false positives.

## Quickstart

Requirements: Python 3.11 or newer and a Claude Code or Codex installation with hook support.

Install: `pip install epitype`. The `epitype` command exposes installation, search, lint, exam, and diagnostic tools; `epitype-graft` remains as a compatibility alias for the installer.

The `@hungyu/epitype` package on npm is only a signpost back to this Python project (npm rejects the bare name as too similar to an existing package).

Preview the planned changes:

```powershell
epitype install --dry-run
```

If the preview contains only the hosts and paths you expect, install and run the synthetic health check:

```powershell
epitype install
epitype doctor
```

The installer detects existing native vaults. If it finds none, it creates an empty fallback vault. Reinstall preserves a curated vault list; use `epitype vaults --resync --dry-run` and then rerun without `--dry-run` when you intentionally want to adopt the latest detection result.

### Approve Codex hooks

Codex registration and Codex trust are separate. Check the real trust state after installation:

```powershell
epitype trust
```

If any Epitype entry is `UNTRUSTED`, `DISABLED`, or `MODIFIED`:

- In the terminal UI, enter `/hooks`, press `t` to trust all entries in the panel, then press `esc`.
- In the Desktop app, open **hooks need review** or the **Hooks** panel and approve the Epitype entries for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PreCompact`, and `Stop`.

Run the check again. Codex is ready only when it prints `CODEX TRUST: PASS 5/5`. `doctor` verifies registration and synthetic execution; it does not replace this trust check.

### Choose a vault layout

Start from one of the tracked templates:

| Template | Intended use |
|---|---|
| [`minimal`](templates/minimal/) | One person on one machine. |
| [`team`](templates/team/) | A shared vault using the common write-lock contract. |
| [`power`](templates/power/) | The full layout, including census and exam-ready directories. |

## Search the local vault

Build a vault's local FTS index, then query it directly or recall against a prompt:

```powershell
epitype search build C:\path\to\vault
epitype search query term --vault C:\path\to\vault
epitype search recall "natural-language prompt" --vault C:\path\to\vault
```

The generated database lives at `<vault>/.epitype/memory_fts.sqlite3` and is ignored by Git. Only `build` creates a missing index. Existing indexes refresh incrementally when stale; a missing index is reported separately from a valid zero-result query.

## Command reference

`epitype <command> --help` prints the full option list for any command below.

### Everyday

| Command | What it does |
|---|---|
| `epitype doctor [--home HOME] [--dry-run] [--clear-shim-status]` | Synthetic health check for hook registration and shim execution; run after install or whenever something looks broken. |
| `epitype dream [vaults...] [--since SINCE] [--dry-run] [--scheduled] [--json]` | Read-only offline tidy inventory (missing aliases, card-lint findings, zombie pending lines, unreviewed drafts, aging event cards, unregistered pocket vaults, draft aging, mixed cards to split, files over their configured cap, owner quotes no decision card carries, and a review pack lining owner events, gate blocks and exam failures up against the cards they point at) rendered as a numbered review packet; applies nothing itself. Scheduling is `dream.mode` in config — `piggyback` (default: a detached background run at session start), `nightly` (an OS-scheduled task), or `off` — switched with `epitype install --dream {piggyback,nightly,off} [--at HH:MM]` (nightly default `03:30`). |
| `epitype gates <vault> [--since Nd\|YYYY-MM-DD] [--json] [--by kind\|decision\|session\|day]` | Turns `_GATE_LOG.jsonl` into a report of what the action gates actually blocked, e.g. `epitype gates C:\path\to\vault --since 2d`. |
| `epitype cards <vault> [--strict] [--verbose] [--deep] [--json] [--fix-dates [--dry-run]]` | Type-checks memory cards against their required fields. `--deep` adds the vault-level checks: one active card per `decision_key`, valid supersession chains, and every managed card present in both the generated views and the search index. `--fix-dates` is the only flag that writes: it backfills a derived `last_verified_at:` line; preview the exact writes first with `--fix-dates --dry-run`. |
| `epitype views <vaults...> [--force] [--json]` | Regenerates the browsable catalogue from card fields: `_views/current.md` (cards in use, with the complete list of active decisions) and `_views/history/closed.md` (closed projects and superseded decisions). Never writes `MEMORY.md`, rewrites nothing when the input fingerprint is unchanged, and takes a lock so two generators cannot overlap. See [Three reading levels](docs/ARCHITECTURE.md#three-reading-levels). |
| `epitype aliases {export,apply}` | `export` lists cards missing aliases as a JSON worklist; `apply` writes reviewed `suggested` aliases back, additive only. |
| `epitype search {build,query,recall}` | Builds the local FTS index and queries it by keyword or natural-language prompt; see [Search the local vault](#search-the-local-vault) above. |

### Maintenance and batch

| Command | What it does |
|---|---|
| `epitype decisions [vault] [--audit] [--selftest]` | Read-only lint of decision cards: uniqueness per key, supersession chain, decider field; `--audit` lists current decisions that are not `owner-explicit`. |
| `epitype ledger append --ledger PATH --entry TEXT --evidence PATH::SUBSTRING [--check-only]` | Verifies each evidence claim actually appears in the named file's bytes before appending the ledger entry; `--check-only` validates without writing. |
| `epitype capture-route <vault> [--audit] [--apply] [--home HOME] [--json]` | Audits a vault's auto-captured event cards against the routing rule: the card belongs to the project vault its `cwd` names, so anything in the governance vault that belongs elsewhere is listed as `MISROUTED <card> -> <vault>`. `--audit` is read-only; `--apply` moves the cards (`os.replace`, `-2` suffix on a name collision, never a delete) and appends a one-line rehome note. |
| `epitype harvest [--inventory] [--docs DOCS] [--since SINCE] [--reevaluate DIR [--apply]] [--quarantine-drops [DIR]]` | Zero-model replay of the capture rules over historical transcripts and documents for first-time backfill; also re-judges drafts or a vault's own event cards against today's rules. |
| `epitype token-meter [rollout] [--selftest]` | Reads a Codex rollout JSONL and prints its last current and cumulative token usage against the context window. |
| `epitype scar-census build` | Builds the machine-generated view of the four-layer scar census. |
| `epitype compact-map build` | Builds a bounded compact-recovery map, the same kind `PreCompact` writes per session. |
| `epitype source SOURCE.jsonl [--find TEXT] [--role user\|assistant\|all] [--line N] [--offset BYTES] [--limit 1..8]` | Reads original messages with source roles, physical lines, hashes and explicit truncation/coverage. Line numbers are relative when an offset is supplied. Literal retrieval is not proof of a current decision or of evidence supporting a claim. |
| `epitype pending <vault> [--max-age-days N] [--strict] [--json]` | Lints for zombie pending lines: a todo marker with no closing text, no runnable `verify:`, and past the age threshold. |
| `epitype exam [corpus] [--strict] [--selftest]` | Runs the exam engine against a behavior-question corpus. |
| `epitype trust [--home HOME]` | Checks Codex's real hook trust state; see [Approve Codex hooks](#approve-codex-hooks) above. |
| `epitype install \| uninstall \| vaults \| relocate` | Installer, removal, vault resync, and repository relocation; see [Quickstart](#quickstart) and [Moving or removing Epitype](#moving-or-removing-epitype) below. |

## Verify this checkout

Run the public checks from the repository root:

```powershell
python tests/run_all.py
python tests/privacy_lint.py
python exam/exam_runner.py --strict
```

`tests/run_all.py` currently runs 34 component selftests covering the core tools, hook adapters, package surface, installer, exam engine, and privacy gate. The included exam corpus is a small synthetic sample. For this release, the publication gate also passed a strict 300-case behavior corpus and a 15-seed review; those release materials are not part of this repository.

These checks are regression evidence, not proof that every future host version or every memory failure is covered.

## Moving or removing Epitype

After moving the repository, update the stable shim target and rerun the doctor:

```powershell
epitype relocate --to C:\path\to\new\repo
```

Preview uninstall before removing Epitype-owned files:

```powershell
epitype uninstall --dry-run
epitype uninstall
```

Read [Uninstall Epitype](docs/UNINSTALL.md) before restoring a backup manually.

## Limits

- Hooks can govern only events and tools the host exposes. Direct file reads remain outside Epitype's current-decision filter.
- The time and output ceilings require selection; Epitype never injects the entire vault into every prompt.
- Epitype gates content, not actions: it never refuses a shell command or a read. Irreversible actions are the host's own native rules to refuse. Malformed cards fail open rather than taking control of the host.
- Claude Code and Codex are the tested host boundary. A host upgrade still needs integration testing.
- The bundled tests are synthetic. They exercise behavior and failure handling, not long-term field performance.

## Documentation

- [Architecture](docs/ARCHITECTURE.md): memory blocks, retrieval routes, decision cards, scar lifecycle, and authority rules.
- [Failure modes](docs/FAILURE_MODES.md): symptoms, countermeasures, and verification boundaries.
- [Uninstall](docs/UNINSTALL.md): ownership-aware removal and backup guidance.
