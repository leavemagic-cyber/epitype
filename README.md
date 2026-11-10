# Epitype

[![PyPI](https://img.shields.io/pypi/v/epitype.svg)](https://pypi.org/project/epitype/)
[![Python](https://img.shields.io/pypi/pyversions/epitype.svg)](https://pypi.org/project/epitype/)
[![License: MIT](https://img.shields.io/github/license/leavemagic-cyber/epitype.svg)](LICENSE)

[繁體中文](README.zh-TW.md)

You wrote the rule in `CLAUDE.md`. The agent still did something else.

You settled a decision three days ago. Today the agent answers from the version you replaced. One compaction later, a rule you put in the instruction file might as well not be there.

Epitype is the small layer I built for that. It runs on the hooks Claude Code and Codex already have, leaves your existing memory files where they are, and adds the part those files cannot do on their own: getting the right note in front of the agent at the moment it matters, and keeping the wrong one out.

It uses the Python standard library at runtime, with no required service or model calls of its own. Injected context still uses the host's context budget.

Keep every vault's always-loaded index short with [`epitype memory-index`](docs/memory-index.md):
preview or apply lossless overflow into a full index, using one configurable byte budget.

Original quotations are optional reference material. Current meaning, scope and actual effects are primary; normal rule and memory maintenance does not rescan conversations. See [current-content review](docs/RULE_PROVENANCE.md).

## Start with templates for review

```powershell
pip install epitype
epitype install
epitype starter
```

The five shipped templates are AI-authored examples without verified original sources.
`epitype starter` puts them under `_drafts/starter_templates/`; installing them does
not activate rules. Review any rule you adopt and check its current meaning and scope
before publishing it in your vault. See [rule provenance](docs/RULE_PROVENANCE.md).

Stop findings are reminders for the next prompt in the same session. They do not
withhold a reply, request a rewrite, or start another model turn.

## Where you actually notice it

You rejected an approach on Monday. On Thursday the agent proposes it again. The Stop observer records the match and brings the current ruling into that session's next prompt. It does not retract the delivered reply or force a rewrite.

Before a tool runs, workflow checks provide advice about writing, attribution and cleanup. Explicit action guards and retained safety checks can refuse a matching call.

The context gets compacted in the middle of a task. The next turn recovers the current requirements, exceptions, decisions and unfinished work from a short working state. Original messages stay available for a specific dispute; they are not a routine reload or a prerequisite for editing memory.

None of this is a new place to keep things. All three run on notes already sitting in the vault your host reads.


## Install

You need Python 3.11 or newer and a Claude Code or Codex install with hook support.

```powershell
pip install epitype
```

The `epitype` command covers installation, search, lint, exam and diagnostics. `epitype-graft` still works as an alias for the installer. The `@hungyu/epitype` package on npm is only a signpost back here; npm rejects the bare name as too close to an existing package.

Preview what the installer would change:

```powershell
epitype install --dry-run
```

If the preview lists only the hosts and paths you expected, install and run the synthetic health check:

```powershell
epitype install
epitype doctor
```

The installer looks for native vaults you already have. If there are none it creates an empty one. Reinstalling keeps a vault list you curated; run `epitype vaults --resync --dry-run` and then without `--dry-run` when you do want the latest detection result.


## Who it is for

Worth a try if you already run Claude Code or Codex against a `CLAUDE.md` or `AGENTS.md` you maintain, you have watched an agent forget or reopen something you settled, and you want a record of what got stopped.

Probably not yet if you are new to CLI agents and have no accumulated rules to govern, or if what you actually want is a security boundary around dangerous commands. That one is the host's job: `permissions.deny` in Claude Code, `execpolicy` in Codex. Epitype can deny a tool call whose text carries the literal fragments a scar card names, but an equivalent rewrite gets through — it is a guardrail against repeating a carded mistake, not a control that holds against someone trying to get past it.

## How it works

Epitype hangs off six host events and reads the same vaults your host already uses.

| Event | What Epitype does |
|---|---|
| `SessionStart` | Reads cached maintenance results and incrementally warms action guards without assigning card cleanup or alias work in the task conversation. After compaction, restores short working state; child agents receive their own state instead of a parent handoff. |
| `UserPromptSubmit` | General prompt matching is off by default. Opt in with `proactive_recall: true`; project scope is checked before ranking and resident rules are excluded. Pending notices, event capture and compaction recovery operate independently. |
| `PreToolUse` | Advisory write-format, attribution and workflow checks; explicit action guards and retained safety checks can refuse calls. The context meter reminds once per compaction cycle: Claude Code uses a configured token window or compatible observed compactions; Codex uses its own limit with 44k tokens of headroom. Unknown thresholds produce no reminder. |
| `PreCompact` | Writes a small recovery map from the tail of the transcript before the context is compacted. On an automatic compaction it also records the usage at that moment, which is how the context meter learns the threshold. |
| `PostToolUse` | Records browser tabs and control sessions created by the agent, plus closures confirmed by successful tool results. A later prompt or non-browser tool call reminds the agent about anything still open. It never closes the resource itself. |
| `Stop` | Records matches and queues reminders for the next prompt in the same session, without retracting replies. `SubagentStop` uses the same advisory path. Self-completion claims are distinguished from quoted evidence. |

Injected memory is advisory. It cannot override system or developer instructions, bypass host permissions, or hand a tool any authority by itself. Hook output is capped at 10 KiB, and every hook has a ten-second deadline that fails open.

Browser tracking stores resource IDs, not URLs, titles or page content. It only treats resources recorded as created by this agent session as its own; unclear ownership stays unverified. See [Browser resource leak detection](docs/CONTROL_LIFECYCLE.md).

### Capture: filed, or waiting for you

A trigger match identifies a candidate, not a reviewed rule. Unreviewed human statements are saved under `<vault>/_drafts/captured_pending/YYYYMMDD/` with their original wording, source identity, time and scope. Explicit memory search can find them, labelled `capture_review: UNREVIEWED` and `authority: none`; proactive recall and action reminders exclude them.

Publication requires the existing content, authorship, scope and payload review. A shape match, a `verified: true` flag or moving a file cannot supply that review. Both hosts capture human transcript messages through the same incremental entry point, including human queued messages; tool outputs and compaction summaries are excluded. Non-interactive Codex exec sessions skip user-event capture. See [rule provenance](docs/RULE_PROVENANCE.md).

A `verified: false` card is authority for nothing. The Stop gate and the write gate both read cards that declare a `decision_key`, which a captured card never has. Recall leaves it alone too: a captured quote sits at the bottom reading level, so `memsearch` finds it when someone goes looking for the exact words, while the prompt hook injects cards only. The measured trade-off is in `docs/FAILURE_MODES.md` §32 and §41.

## Beyond recall

### Decisions that stay replaced

Decision cards carry a stable `decision_key`, a status of `active` or `superseded`, an effective time, and a named source. One card is active per key. `query`, `recall` and the prompt hook drop superseded cards by default and keep them for provenance. Ask for `--include-superseded` when you actually want the history.

### Scars, and what really stops an action

A scar is a rule born from an incident: the `incident` it came from, plus `advice` that names the safer route. Most cards are context read back into a turn and refuse nothing.

A card can also refuse, in three narrow forms. `forbidden` patterns stop a turn from ending when the model has said something the owner ruled out. `require_when` with `require_text` stops a turn whose claim arrives without the evidence the rule asks for. `guard_tool` with `guard_all_of` denies a tool call whose own text contains every literal fragment the card names — no regex, no shell parsing, no guess about what a command means.

A fourth form is structural rather than literal. `fence_shell` (`powershell`, `bash` or `sh`) hands every fenced block tagged as a shell language (`fence_langs`, default bash, sh, shell, zsh, console, powershell, pwsh, ps1, ps, cmd, bat) to that shell's own parser — syntax only, never executed — and stops the turn when a block would fail the moment someone presses Run. `powershell` means Windows PowerShell 5.1 (`powershell.exe`), not `pwsh`. Untagged blocks are not checked; if the parser is missing, slow or broken the turn passes and the gate says so on stderr.

That last one was removed in 2026-09-09 and restored on 2026-09-16, after the premise behind removing it was tested and failed: Claude's Bash permission patterns match positionally and have no AND operator, so four of nine hazard classes moved to the host and five could not be expressed at all. A literal conjunction over-approximates toward denial, which is the safe direction, and it is a guardrail against a mistake someone has already carded — not a security boundary. An equivalent rewrite of the same command gets through, by design.

Semantic judgement and genuinely irreversible actions stay with the host, which refuses the call before it runs: `permissions.deny` in Claude Code, `execpolicy` in Codex.

### An installer that leaves your setup alone

The installer merges only the entries it marks as its own, keeps the native vaults it detects, and backs up any host file before changing it. Stable shims mean you can move the repository without rewriting every registration. Uninstall takes back Epitype's own registrations and config and leaves native memory and vault cards untouched.

### Tidying that happens on its own

The offline inventory pass, the dream, runs on its own. You never have to remember to start it.

By default (`dream.mode: piggyback`) a session start whose last dream is older than `dream.interval_hours` kicks off one detached, low-priority background run and returns without waiting for it. A lock carrying its pid, stale after 30 minutes, keeps a second one from starting. `graft install --dream nightly [--at HH:MM]` registers a daily system task instead, `graft doctor` shows the mode and the last completion, `graft uninstall` removes the task, and `--dream off` turns both off.

A run reads vaults and writes, within a ten-minute budget, in three places: `<governance vault>/.epitype/` (review pack, state, log and the gates' health file), the governance vault's `MEMORY.md` when its index has drifted, and — this one is worth knowing, because it happens unattended — the marked blocks inside `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, so a rule you changed in a card is in front of the agent by morning without you remembering a command. Only the text between those markers is touched, the file is backed up first, and `epitype sync --remove` (or `graft uninstall`) takes the blocks back out. On the way it harvests new material into drafts, and it never touches a card you already filed. The next session mentions it in one line.

The last section is the feedback review pack: one row per card that owner events, gate blocks or exam failures keep pointing at, raised once enough rows pile up (`memspec.REVIEW_PACK_TRIGGER`, 5) that a review sitting is worth holding. It judges nothing and edits nothing. No model is called, so an installed Epitype never spends your model budget while you are not looking.

### What a healthy install looks like

`epitype doctor` feeds a synthetic event to every hook and reports what answered:

```text
HOSTS: claude, codex
SHIM RESOLUTION: PASS 5/5 repo_root=C:\Epitype\repo
REGISTRATION claude: PASS 6/6
REGISTRATION codex: PASS 6/6
HOOK SessionStart: PASS (515 ms)
HOOK UserPromptSubmit: PASS (369 ms)
HOOK PreCompact: PASS (297 ms)
HOOK PreToolUse: PASS (301 ms)
HOOK Stop: PASS (287 ms)
HOOK SubagentStop: PASS (268 ms)
HEALTH PASS 6/6
```

### When something breaks, you can see it

Missing indexes, stale indexes, shim failures, malformed cards and lock contention all end differently. Hooks fail open when they cannot finish safely, and the installer doctor reports recorded shim outages instead of reading silence as health.

`epitype gates <vault> [--since Nd|YYYY-MM-DD] [--json] [--by kind|decision|session|day]` turns a vault's `_GATE_LOG.jsonl` into a read-only report of what the gates actually blocked, by kind, card, day and session, and flags the same card blocking three times in one session as a likely false positive.


## After installing

### Approve Codex hooks

Registering a hook in Codex and trusting it are two different things. Check the real state after installing:

```powershell
epitype trust
```

If an Epitype entry comes back `UNTRUSTED`, `DISABLED` or `MODIFIED`:

- In the terminal UI, type `/hooks`, press `t` to trust everything in the panel, then `esc`.
- In the Desktop app, open **hooks need review** or the **Hooks** panel and approve the Epitype entries for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PreCompact` and `Stop`.

Run the check again. Codex is ready when it prints `CODEX TRUST: PASS 5/5`. `doctor` checks registration and synthetic execution, and does not replace this.

### Language

Every message a gate shows you comes in English or Traditional Chinese. A fresh install
writes `"language": "en"` into `~/.epitype/config.json`, unless your OS locale is Chinese,
in which case it writes `"language": "zh-TW"`. Edit that field to switch, or override it
for one run with the `EPITYPE_LANG` environment variable (`en` or `zh-TW`), which wins over
the config file. A config without the field behaves as `zh-TW`. `epitype doctor` prints the
active setting on its `LANGUAGE:` line. The switch changes displayed text only — patterns,
field names and what the gates match on are the same in both languages. Your cards stay in
whatever language you wrote them in.

### Choose a vault layout

Start from one of the tracked templates:

| Template | Intended use |
|---|---|
| [`minimal`](templates/minimal/) | One person, one machine. |
| [`team`](templates/team/) | A shared vault under the common write-lock contract. |
| [`power`](templates/power/) | The full layout, census and exam directories included. |

## Search the local vault

Build the index once, then query it or recall against a prompt:

```powershell
epitype search build C:\path\to\vault
epitype search query term --vault C:\path\to\vault
epitype search recall "natural-language prompt" --vault C:\path\to\vault
```

The database lives at `<vault>/.epitype/memory_fts.sqlite3` and Git ignores it. Only `build` creates a missing index; an existing one refreshes incrementally when it goes stale. A missing index is reported as missing, not as a query that found nothing.

## Command reference

`epitype <command> --help` prints the full option list for anything below.

### Everyday

| Command | What it does |
|---|---|
| `epitype doctor [--home HOME] [--dry-run] [--clear-shim-status]` | Synthetic health check for hook registration and shim execution. Run it after installing, or whenever something feels off. |
| `epitype dream [vaults...] [--since SINCE] [--dry-run] [--scheduled] [--json]` | Read-only tidy inventory rendered as a numbered review packet, applying nothing itself: missing aliases, card-lint findings, zombie pending lines, unreviewed drafts, aging event cards, unregistered pocket vaults, draft aging, mixed cards to split, files over their cap, owner quotes no decision card carries, generated core blocks that have drifted from their rule cards, and the review pack. Scheduling is `dream.mode` in config — `piggyback` (default), `nightly` (an OS task) or `off` — switched with `epitype install --dream {piggyback,nightly,off} [--at HH:MM]` (nightly defaults to `03:30`). |
| `epitype gates <vault> [--since Nd\|YYYY-MM-DD] [--json] [--by kind\|decision\|session\|day]` | Turns `_GATE_LOG.jsonl` into a report of what the gates actually blocked, e.g. `epitype gates C:\path\to\vault --since 2d`. |
| `epitype cards <vault> [--strict] [--verbose] [--deep] [--json] [--refresh-cache] [--fix-dates [--dry-run]]` | Type-checks cards against their required fields. `--deep` adds vault-level checks: one active card per `decision_key`, valid supersession chains, every managed card present in both the generated views and the search index. `--refresh-cache` runs the complete per-card and example checks and writes `<vault>/.epitype/card-lint-result.json`; it cannot be combined with `--deep` or `--fix-dates`. Startup only reads this accounting, invalidating it on changed inputs, validator versions or UTC dates (at most 24 hours), and injects no lint or alias maintenance tasks. Rule consumers still validate current cards. `--fix-dates` backfills a derived `last_verified_at:`; preview it with `--fix-dates --dry-run`. |
| `epitype dupes [vaults...] [--json] [--threshold N]` | Cross-vault near-duplicate candidates using character shingles and metadata. Defaults to the shared configured vault list and threshold 0.35. Always writes Markdown and JSON to the governance vault's `_drafts/duplicates/YYYYMMDD.*`; `--json` selects JSON on stdout. Dream writes the same report after regenerating views (except in `--dry-run`). Lists machine-check fields, owner quotes, pair similarities and suggested keep cards. The lead AI settles candidates under the owner's rule; Epitype never edits or retires cards. Lexical detection misses semantic paraphrases; see `docs/dupes-calibration.md`. |
| `epitype views <vaults...> [--force] [--json]` | Regenerates the browsable catalogue from card fields: `_views/current.md` (cards in use, with every active decision) and `_views/history/closed.md` (closed projects and superseded decisions). Never writes `MEMORY.md`, rewrites nothing when the input fingerprint is unchanged, and takes a lock so two generators cannot overlap. See [Four reading levels](docs/ARCHITECTURE.md#four-reading-levels). |
| `epitype core-gen <vaults...> --out FILE [--cap-bytes N] [--dry-run] [--check] [--json]` | Assembles the resident core block from `type: rule` cards — `floor` numbered by `order`, `resident` grouped by `section`, host-only cards in their own zone — copying each approved `text` byte for byte and writing an approval pack beside it. Exceeding the size target produces a note while retaining complete rules; a generated-layer card without `approved_by` / `approved_at` still prevents writing. `--check` compares instead of writing. See [Core generation](docs/ARCHITECTURE.md#core-generation-rule-cards--the-resident-block). |
| `epitype aliases {export,apply}` | `export` lists cards missing aliases as a JSON worklist; `apply` writes reviewed `suggested` aliases back, additive only. |
| `epitype search {build,query,recall}` | Builds the local index and queries it by keyword or prompt; see [Search the local vault](#search-the-local-vault). |

### Maintenance and batch

| Command | What it does |
|---|---|
| `epitype decisions [vault] [--audit] [--selftest]` | Read-only lint of decision cards: uniqueness per key, supersession chain, decider field. `--audit` lists current decisions that are not `owner-explicit`. |
| `epitype ledger append --ledger PATH --entry TEXT --evidence PATH::SUBSTRING [--check-only]` | Checks that each evidence claim really appears in the named file's bytes before appending the entry. `--check-only` validates without writing. |
| `epitype capture-route <vault> [--audit] [--apply] [--home HOME] [--json]` | Audits auto-captured event cards against the routing rule: a card belongs to the project vault its `cwd` names, so anything sitting in the governance vault that belongs elsewhere is listed as `MISROUTED <card> -> <vault>`. `--audit` is read-only; `--apply` moves the cards (`os.replace`, `-2` suffix on a name collision, never a delete) and appends a one-line note. |
| `epitype harvest [--inventory] [--docs DOCS] [--since SINCE] [--drafts-only] [--reevaluate DIR [--apply]] [--quarantine-drops [DIR]]` | Zero-model replay of the capture rules over old transcripts and documents, for a first backfill. Also re-judges drafts, or a vault's own event cards, against today's rules. `--drafts-only` keeps everything it finds in the pending drafts area. |
| `epitype token-meter [rollout] [--selftest]` | Reads a Codex rollout JSONL and prints its last current and cumulative token usage against the context window. |
| `epitype context-meter calibrate [--root DIR]` / `status [--transcript PATH]` | Claude Code context meter. `calibrate` is a read-only report of the last 5 automatic compactions in the past 30 days (median, sample count, time range); it writes nothing, because without a configured token window the threshold is learned from this machine's own automatic compactions; a configured window can provide a threshold immediately, adjusted by compatible observations. `status` prints the threshold, its source and, given a transcript, where the current usage sits (for a Codex rollout: Codex's count, limit and reminder point). `codex-calibrate --limit N --hard-cap N` replays Codex rollouts read-only and reports, per candidate headroom, how many automatic compactions would have been warned at least one model step ahead. |
| `epitype lifecycle --session SESSION_ID --json` | Reads the browser-resource ledger for one session. `pending` lists resources whose closure is still unverified. |
| `epitype starter [--list\|--remove\|--dry-run]` | Creates five AI starter templates for review, lists them, or removes only untouched cards installed by this command. |
| `epitype scar-census build` | Builds the machine-generated view of the four-layer scar census. |
| `epitype compact-map build` | Builds a bounded compact-recovery map, the same kind `PreCompact` writes per session. |
| `epitype source SOURCE.jsonl [--find TEXT] [--role user\|assistant\|all] [--line N] [--offset BYTES] [--limit 1..8]` | Reads original messages back with roles, physical lines, hashes and explicit truncation and coverage. Line numbers are relative when you supply an offset. Finding a sentence is not proof that it is the current decision. |
| `epitype owner-index build\|update\|find [WORDS ...] [--index PATH] [--root PATH] [--after DATE] [--role U,Q,S] [--limit N]` | Incremental SQLite trigram search of owner input, human queues and question-box answers, including their question context. See [owner index](docs/OWNER_INDEX.md) for archive sources and integrity checks. |
| `epitype source --find TEXT [--index PATH]` | Searches the owner index for unverified locators; supply a source file and byte offset to read original evidence. Does not build or update the index. |
| `epitype pending <vault> [--max-age-days N] [--strict] [--json]` | Lints for zombie pending lines: a todo marker with no closing text, no runnable `verify:`, and past the age threshold. |
| `epitype exam [corpus] [--strict] [--selftest]` | Runs the exam engine against a behavior-question corpus. |
| `epitype trust [--home HOME]` | Checks Codex's real hook trust state; see [Approve Codex hooks](#approve-codex-hooks). |
| `epitype install \| uninstall \| vaults \| relocate` | Installer, removal, vault resync, repository relocation. See [Install](#install) and [Moving or removing Epitype](#moving-or-removing-epitype). |

## Verify this checkout

From the repository root:

```powershell
python tests/run_all.py
python tests/privacy_lint.py
python exam/exam_runner.py --strict
```

`tests/run_all.py` runs component selftests across the core tools, hook adapters, package surface, installer, exam engine and privacy gate. The exam corpus in this repository is a small synthetic sample. Public tests use synthetic data; local field measurements and private corpora are not included.

These checks are regression evidence. They are not proof that every future host version, or every way memory can fail you, is covered.

## Moving or removing Epitype

After moving the repository, point the shims at the new path and check:

```powershell
epitype relocate --to C:\path\to\new\repo
epitype doctor
```

Preview an uninstall before it removes anything:

```powershell
epitype uninstall --dry-run
epitype uninstall
```

Read [Uninstall Epitype](docs/UNINSTALL.md) before restoring a backup by hand.

## Where your data goes

Nowhere. Epitype makes no network calls: there is no import of `urllib`, `http`, `socket` or
any HTTP client anywhere in the package, and no telemetry, account or key. Your cards are
Markdown files in your own vault directory; the search index is a local SQLite file that can be
deleted and rebuilt. The gate log records rule names and timestamps — never message content.

## How it compares

- **[claude-mem](https://github.com/thedotmack/claude-mem)** summarises sessions so the agent
  remembers more. Epitype keeps current rulings, checks writes against them and records
  matching reply violations for a later reminder.
- **[claudekit](https://github.com/carlrannaberg/claudekit)** and similar hook toolkits give you
  building blocks to wire up. Epitype connects cards to retrieval, write checks and reminders.
- A **`CLAUDE.md` / `AGENTS.md` kit** gives the agent more text to read. Epitype generates that
  resident block from your rule cards, then checks at the end of the turn whether the rule held.

They compose: nothing here stops you running a memory summariser alongside it.

## Limits

- Hooks reach only the events and tools the host exposes. A direct file read stays outside the current-decision filter.
- Time and output ceilings force selection. Epitype never pushes a whole vault into a prompt.
- Workflow checks are advisory; explicit action guards and retained safety checks can refuse matching tool calls. Stop observations provide later reminders and never veto delivered replies. Equivalent command rewrites can evade literal matching; irreversible actions remain the host's responsibility. A malformed card fails open instead of taking the host with it.
- Claude Code and Codex are the tested boundary. A host upgrade still needs its own integration test.
- The bundled tests are synthetic. They exercise behaviour and failure handling, not months in the field.

## Documentation

- [Documentation index and record status](docs/INDEX.md): current guides, dated evidence and superseded reports.
- [Repositories evaluated for reuse](docs/REPOSITORY_REFERENCES.md): selected approaches, costs and adoption status.
- [Architecture](docs/ARCHITECTURE.md): memory blocks, retrieval routes, decision cards, scar lifecycle, authority rules.
- [Browser resource tracking](docs/CONTROL_LIFECYCLE.md): what Epitype records, reminds about and leaves unverified.
- [Failure modes](docs/FAILURE_MODES.md): symptoms, countermeasures, verification boundaries.
- [Uninstall](docs/UNINSTALL.md): ownership-aware removal and backup guidance.

## Status

Published versions are listed in [GitHub Releases](https://github.com/leavemagic-cyber/epitype/releases).
Unreleased local changes are recorded in [CHANGELOG](CHANGELOG.md) and the dated results above. Local integration and package publication are reported separately.
