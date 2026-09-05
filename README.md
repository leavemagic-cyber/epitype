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

Epitype connects the same native vaults to four host events:

| Event | What Epitype does |
|---|---|
| `SessionStart` | Injects a bounded memory index and work ledger when those files exist. |
| `UserPromptSubmit` | Recalls up to five relevant cards from each resolved vault within the shared output budget. Short owner-grant statements are stored verbatim, deduplicated, and indexed; their meaning is not inferred during capture. |
| `PreToolUse` | Matches scar-card triggers against the tool and its input. A match returns a bounded denial, safer advice, and an audit row. |
| `PreCompact` | Builds a small recovery map from the transcript tail before context compaction. |

Injected memory remains advisory. It cannot override system or developer instructions, bypass host permissions, or grant a tool authority by itself. Hook output is capped at 10 KiB and each hook has a ten-second fail-open deadline.

## Governance beyond recall

### Current decisions

Decision cards have a stable `decision_key`, an `active` or `superseded` status, an effective time, and a named decision source. Exactly one card should be active for each key. `query`, `recall`, and the prompt hook exclude superseded cards by default while retaining them for provenance. Use `--include-superseded` only when you want the history.

### Scars that can stop an action

A scar is an incident-born rule. Adding `trigger.tool`, `trigger.input`, and actionable `advice` turns a suitable scar into a narrow action gate. Command matching inspects executable and unquoted argument positions by default, so a trigger word inside a quoted string, comment, or heredoc body does not block the command. Cards that need literal full-text matching can opt in explicitly.

### Native-first installation

The installer merges only entries marked as Epitype, keeps detected native vaults, and writes backups before changing an existing host file. Stable shims let the repository move without rewriting every host registration. Uninstall removes Epitype-owned registrations and configuration while preserving native memory and vault cards.

### Failure evidence

Missing indexes, stale indexes, shim failures, malformed cards, and lock contention have distinct outcomes. The hooks fail open when they cannot safely finish, and the installer doctor reports recorded shim outages instead of treating silence as health.

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
- In the Desktop app, open **hooks need review** or the **Hooks** panel and approve the Epitype entries for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, and `PreCompact`.

Run the check again. Codex is ready only when it prints `CODEX TRUST: PASS 4/4`. `doctor` verifies registration and synthetic execution; it does not replace this trust check.

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

## Verify this checkout

Run the public checks from the repository root:

```powershell
python tests/run_all.py
python tests/privacy_lint.py
python exam/exam_runner.py --strict
```

`tests/run_all.py` currently runs 20 component selftests covering the core tools, hook adapters, package surface, installer, exam engine, and privacy gate. The included exam corpus is a small synthetic sample. For this release, the publication gate also passed a strict 300-case behavior corpus and a 15-seed review; those release materials are not part of this repository.

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
- Action gates are only as precise as their scar triggers and advice. Malformed cards fail open rather than taking control of the host.
- Claude Code and Codex are the tested host boundary. A host upgrade still needs integration testing.
- The bundled tests are synthetic. They exercise behavior and failure handling, not long-term field performance.

## Documentation

- [Architecture](docs/ARCHITECTURE.md): memory blocks, retrieval routes, decision cards, scar lifecycle, and authority rules.
- [Failure modes](docs/FAILURE_MODES.md): symptoms, countermeasures, and verification boundaries.
- [Uninstall](docs/UNINSTALL.md): ownership-aware removal and backup guidance.
