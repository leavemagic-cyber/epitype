# Fixed startup load (advisory)

`python -m epitype fixed-load --cwd <launch-directory> --vault <memory-vault>`
reports estimated catalog cost, source budgets and placement. It never removes
capabilities or blocks work, including on incomplete inputs. `--json` gives the
sanitized ledger; `--output <path>` explicitly publishes it and `--refresh`
updates the cached startup report for this launch directory. No background AI or
new service is launched. Sources are read only. The only writes are explicitly
requested policy/report publication, hook wiring and delivery receipts.

## Policy and default placement

Install the packaged starting policy explicitly:

```powershell
python -m epitype fixed-load --install-policy "$HOME/.epitype/fixed_load_budget.json"
```

An existing policy is preserved. `--policy` selects a reviewed policy; otherwise
the user policy is used when present, then the packaged default. This policy is
version 1, advisory. Reasons and reconsideration conditions live in that file.

New tools, skills, agents, rules and connectors default to project scope.
User scope needs at least two distinct named projects. Worktrees sharing a Git
common directory, and scratch aliases, count as one project. A placement review
is an invitation to record evidence, never an instruction to disable a tool.
Project settings remain attached to actual launch directories; shared Git
auto-memory resolution does not make project settings global.

`sources` entries may declare `path`, `kind`, `declared_scope`, `projects` (actual
project directories), `purpose`, `author`, `evidence`, `version`, `rollback`, and
`move_to`. Discovered physical scope overrides a declaration. Unrecorded global
extensions receive a placement review. No files are moved. A move proposal keeps
redirect, reverse-link, shared-quota, router, view, search and consumer/rollback
obligations in the report. Never populate this policy with secrets.

Provisional disjoint targets: built-in tools (including agents) 30,000; MCP 4,000;
memory/instructions 9,000; skills/retained content 2,200; system 4,799 tokens.
Total 49,999 versus the lead's supplied 82,559. These are engineering targets,
not measured savings or new owner restrictions. A full 18,743 MCP browser profile
instead gives a conditional 64,742 target. Preserve required capabilities and
approved core even when over budget. Source targets: agent description 35,
skill description 40, index 1,000 estimated tokens; existing raw index 1,526
bytes and generated core 14,523 bytes remain independent. Agent catalog target
315 assumes the current nine roles, not a limit on the number of agents.

## Measurement and privacy

The static estimate is ceil(ASCII characters / 4 + non-ASCII characters). Agent
and skill bodies are conditional invoked cost. A hidden automatic skill
description has zero startup-description estimate and its body remains available.
Canonical paths deduplicate aliases. Child descriptions are never added to host
category totals. Static candidates are **partial**, not proof of actual loading.
Raw index byte budgets use the file's original size, including CRLF and BOM,
matching `memory-index`; text normalization is only for token estimation.
Imports, host/synced catalogs, launch definitions, exact settings precedence and
retained context need a sanitized host export. A missing/partial scan is explicit.
ToolSearch denial reports a possible upfront MCP cost, never a saving.

Native user and private project MCP registrations in `~/.claude.json` are
reported as counts. Project candidates are exact normalized absolute keys for
the launch directory and the Git common-root directory, with duplicates counted
once per scope. Other projects and their paths are neither resolved nor opened.
Counts do not establish native precedence, enabled/connected tools or schema
tokens. A configured candidate without a valid host export is explicitly
`MCP_DEFINITIONS_UNMEASURED`; unreadable/invalid native metadata has unknown
counts and `MCP_SETTINGS_UNREADABLE_OR_INVALID`, rather than a false zero.
Bundled, managed, plugin and SDK/launch catalogs still require a host export.
See the [native MCP scope documentation](https://code.claude.com/docs/en/mcp).

The scanner reads only known prompt sources and known settings locations. It
does not scan transcripts or credentials, open `.credentials.json`, `.codex`,
`.gemini`, `.grok`, or `*_private` paths. Additional folder names can be listed
in `~/.epitype/fixed_load_exclude.txt`, one per line; names are casefolded and
matched against whole path parts. A missing or unreadable file adds no exclusions.
The scanner does not launch Claude or export config
values, env, MCP payloads/URLs/headers, tool inputs, schema bodies or source text.
Parse errors emit diagnostic codes without exception values. Reports include
paths, named versions, modification times, counts and totals. The native profile
also has an opaque SHA-256 fingerprint of relevant MCP definitions and selection
state. Server payloads and account values are never exported. The fingerprint is
for change detection, not a cost estimate or a hash of the whole account file.

`--runtime <sanitized-export.json>` accepts this contract:

```json
{
  "context": {
    "cwd": "/absolute/launch/path", "project": "/absolute/git/common-dir",
    "host_version": "observed-version", "model_context": "observed-context",
    "retention_state": "same-retained-skill-state", "observation_version": "capture-name"
  },
  "totals": {"tools": 30000, "mcp": 4000, "memory": 9000, "skills": 2200, "system": 4799},
  "tools": [{"name": "PowerShell", "category": "tools", "state": "loaded", "tokens": 200}],
  "retained_skill_tokens": 0
}
```

Numbers here illustrate the schema; they are not measurements. Host-produced
totals are required for all five categories. Per-tool entries have canonical
names, `tools`/`mcp`, loaded/deferred state and token counts; extra fields are
discarded, conflicting duplicates rejected. Retained skill tokens are nested in
the skills total. `--compare <previous-report.json>` compares only identical host
version, cwd, Git project, model context and retention state; otherwise it reports
INCOMPARABLE/RUNTIME_UNMEASURED. A deny entry, zero calls or a file's existence
cannot prove a rendered schema was removed.

Native `/context` output is useful separate evidence, but a rounded label such
as `39.1k` is not an exact integer export for this contract. Preserve the displayed
precision, host version, launch directory, model/window and retained state.
Compare whole-context usage as well as every category: a smaller skill listing
can coincide with a larger tools category. Cache files also do not establish
the active account catalog. A configuration change alone does not demonstrate
a whole-context saving.

Desktop can also save structured `contextUsage` on its native local-command
record. Preserve those host estimates separately from rounded UI labels. A
native observation must check that used categories sum to the displayed total and exclude deferred
definitions and the compaction buffer. Its per-tool catalog lacks individual
loaded/deferred states, so it is not a complete export for the contract above.
A catalog entry or its full definition size does not establish its current
resident cost or the saving from disabling it. A resumed diagnostic also
includes earlier messages and does not measure a fresh-session floor.

`--native-context <native-session.jsonl>` now reads a bounded last 4 MiB on an
explicit diagnostic call. It accepts structured native `/context` records, or
native `get_usage` results linked to their actual tool call. It preserves only
allowlisted token counts, a record hash/offset and validated timestamp/version.
Messages, schemas, tool arguments and account/plan details are not exported.
The record's cwd must match `--cwd`; a session folder's name alone is not proof
of the runtime working directory. An older observation outside the window is
reported as unavailable, never zero usage. No transcript scanning is added to
SessionStart or ordinary tool calls.

The `native_context` ledger remains separate from the complete `--runtime`
contract. Structured deferred categories and the compaction buffer are excluded
from used cost; a tool catalog never establishes individual loaded state.
Legacy `get_usage` has no deferral breakdown, which remains `UNREPORTED`.
Differences between the reported total and category sum stay visible as
`unattributed_tokens`. These estimates neither establish billing savings nor
upgrade a host/model/retention mismatch into a valid before/after comparison.

Current Claude Code [MCP documentation](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search)
defaults to native deferral. `ENABLE_TOOL_SEARCH=auto` is a separate threshold
mode; denying `ToolSearch` can make MCP definitions load upfront. `alwaysLoad`
is a per-server exception, not a general optimization. Do not assume Desktop's
first-party browser exposes the same settings as a configurable MCP server.
Keep observed reductions separate from configuration changes whose effect
has not been demonstrated.

## Hooks and activation

SessionStart reads the launch directory's cached report below
`~/.epitype/fixed_load_reports/by-cwd/` and records its observed state in
`notified.json`. Neither Claude nor Codex receives fixed-load status lines in
working context, including normal, stale-source and unreadable-report states.
For explicit fixed-load/token maintenance, run `epitype fixed-load` in the
original project to inspect current accounting, or add `--refresh` to update
its cached `current.json`. Existing lint accounting remains in `_LINT_STATUS.md`.
Directory keys are reversible encodings of the resolved cwd, split into short
components; different Chinese paths cannot collapse into the same slug. Reports
and observation receipts are separate for each cwd, including Git worktrees with
different launch settings. Prompt source contents are not rescanned. Ordinary
source/directory/settings changes are recorded in the receipt's
`changed_since_report`, including newly added agents and skills. Receipts update
even when the hook has no context to emit or the output budget omits it;
unchanged state is not rewritten. Wrong-cwd reports are
not attributed to the current project. An existing singleton `current.json` is a
read-only migration fallback only when the cwd has no scoped report and the saved
cwd matches. New lint/refresh writes never overwrite another launch's report.
Corrupt reports record `REPORT_UNREADABLE` without copying their contents.
Use `epitype fixed-load --refresh` from the original project to replace a stale
or corrupt cache; the default preview command remains read-only.

The mixed-project native profile is checked using its scoped MCP fingerprint,
including creation/deletion and valid/invalid transitions. Account/session state,
trust flags and other projects do not change that fingerprint or the refreshed
report receipt. Relevant server/selection changes still mark the report stale.
This prevents unrelated Claude sessions from changing observation receipts.

The existing standalone `~/.claude/hooks/memory_lint.py` is outside this repository.
Explicit wiring preserves all its existing checks and strict exit decisions:

```powershell
python -m epitype fixed-load --wire-memory-lint "$HOME/.claude/hooks/memory_lint.py"
```

The installer requires exact known insertion/caller anchors and is idempotent.
It adds shared `memory_lint_lines` accounting to `_LINT_STATUS.md` and publishes
the sanitized current report; failures append INCOMPLETE without changing lint's
exit status. Its existing `_epitype_repo()` selects code from the activated
`repo_root`; merging alone does not change that binding. The normal lint path
needs an existing project vault; a new project without a vault uses the explicit
CLI. To initialize or refresh without running other memory maintenance:

```powershell
python -m epitype fixed-load --cwd <launch-directory> --vault <memory-vault> --refresh
```

No settings registration is added: the existing SessionStart and memory-lint
registrations are reused. Check both their actual runtime `repo_root` and report
output after activation. This change does not apply agent-description patches.
