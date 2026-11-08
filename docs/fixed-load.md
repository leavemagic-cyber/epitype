# Fixed startup load (advisory)

`python -m epitype fixed-load --cwd <launch-directory> --vault <memory-vault>`
reports estimated catalog cost, source budgets and placement. It never removes
capabilities or blocks work, including on incomplete inputs. `--json` gives the
sanitized ledger; `--output <path>` explicitly publishes it. No background AI or
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
Imports, host/synced catalogs, launch definitions, exact settings precedence and
retained context need a sanitized host export. A missing/partial scan is explicit.
ToolSearch denial reports a possible upfront MCP cost, never a saving.

The scanner reads only known prompt sources and known settings locations. It
does not scan transcripts or credentials, open `.credentials.json`, `.codex`,
`.gemini`, `.grok`, `*_private`, or `出國用` paths, launch Claude, or export config
values, env, MCP payloads/URLs/headers, tool inputs, schema bodies or source text.
Parse errors emit diagnostic codes without exception values. Reports include
paths, named versions, modification times, counts and totals; no hashes.

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

## Hooks and activation

SessionStart reads `~/.epitype/fixed_load_reports/current.json` and emits one short
changed-state notice. It never rescans source contents. Source/directory/settings
modification state makes a stale report visible, including newly added agents
and skills. A receipt is written only after the existing context budget actually
delivers the line; repeated unchanged inputs stay quiet. Wrong-cwd reports are
not attributed to the current project. Corrupt reports yield a short diagnostic.

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
python -m epitype fixed-load --cwd <launch-directory> --vault <memory-vault> --output "$HOME/.epitype/fixed_load_reports/current.json"
```

No settings registration is added: the existing SessionStart and memory-lint
registrations are reused. Check both their actual runtime `repo_root` and report
output after activation. This change does not apply agent-description patches.
