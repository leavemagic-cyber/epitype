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

## Four retrieval routes

Epitype uses four routes because no single retrieval mode is correct for every piece of memory.

1. **Resident.** A small native index or stable rule block remains visible. Size limits keep residency selective instead of turning it into an unbounded prompt prefix.
2. **Point-in-time injection.** A hook injects selected context at a lifecycle event. Prompt recall and tool-trigger evaluation belong here; PreCompact recovery maps are another time-specific artifact.
3. **Agent-directed retrieval.** The resident index points to a fuller card, and the agent opens that card through the host's normal read path. This route is useful for detail that should not be permanently resident, but it is not sufficient for a rule that must intercept an action.
4. **Search.** `memsearch.py` builds a local trigram FTS index at `<vault>/.epitype/memory_fts.sqlite3` and supports explicit query or prompt-oriented recall. Only `build` creates or migrates a missing index; `query` and `recall` report no-index distinctly from a valid zero-hit result. Existing stale indexes retain the bounded incremental refresh path, with the refresh disclosed in the response. Search is a retrieval aid, not an authority source and not permission to act.

All injected memory is advisory data. It cannot override higher-priority instructions or grant tool authority. Hook output is capped at 10 KiB and hook execution is designed to fail open within three seconds.

## Decision cards

A decision card has four required fields:

| Field | Meaning |
|---|---|
| `decision_key` | Stable identity shared by every version of the same decision |
| `status` | `active` or `superseded` |
| `current_decision_at` | ISO date or timestamp at which this card's decision became current |
| `decided_by` | One of `owner-explicit`, `owner-implicit`, `ai-autonomous`, or `three-way` |

For every `decision_key`, exactly one card should be `active`. A superseded card is retained for provenance and carries `superseded_by` pointing to its replacement. `owner-explicit` cards also carry the required source quote.

The normative read rule is simple: only the active card is eligible to govern the next decision. `decision_lint.py` validates the fields and supersession links, rejects multiple active cards for one key, and warns when a key has no active card. `memsearch.py` indexes `status` and `superseded_by`, excludes superseded cards from `query` and `recall` by default, and preserves an explicit `--include-superseded` archaeology path. Its result payload also identifies the current replacement when a filtered predecessor matched but its successor did not.

Run the implemented contract checks with:

```powershell
python epitype/decision_lint.py --selftest
```

## Scar lifecycle

1. **Incident.** Record an observable failure, its boundary, and enough evidence to reproduce or audit it. A transcript scanner may propose a candidate, but it does not write a scar automatically.
2. **Reflex.** Turn the narrow lesson into a scar card. If the failure can precede a tool action, add a `trigger.tool` regex, a `trigger.input` regex, and actionable `advice` that provides a safer route.
3. **Mechanization.** Move stable prevention into code, a hook, a deterministic check, or another machine-enforced guard. A scar does not count as mechanized merely because its prose is prominent.
4. **Retirement.** Remove the reflex from the resident scar block after the mechanism covers the original hazard. Preserve the incident and test evidence outside the resident path so the reason for the guard remains auditable.

The interceptor tests the scar-card form, bounded deny response, alternative advice, and audit row:

```powershell
python adapters/claude/pretooluse_gate.py --selftest
```

## Source-of-authority rules

1. **Higher law outranks lower law.** Host safety and instruction-priority rules cannot be amended by memory.
2. **Special law outranks ordinary law within its scope.** A narrow project or domain rule beats a generic convention for that project or domain.
3. **Later law wins only among rules of the same authority and scope.** Recency does not borrow authority from a lower source.
4. **A later ordinary rule does not silently amend an earlier special rule.** The special rule remains controlling until an equally specific authorized decision replaces it.
5. **Conflicts are reported.** If authority, scope, or supersession cannot be resolved, surface the conflicting sources and stop treating either as an unqualified current instruction.

These rules govern selection; they do not let memory override the host's system, developer, permission, or safety boundaries.

## Shared contract across hosts

Supported Claude Code and Codex registrations use the same four event names: `SessionStart`, `UserPromptSubmit`, `PreCompact`, and `PreToolUse`. They also share the vault list and limits from `epitype/memspec.py`. Host adapters translate the event envelope; they do not fork the memory format.

This is a two-host implementation boundary. Future hosts and future host upgrades require their own integration tests before they can be described as supported.

## Failure feedback components

| Component | Responsibility | Boundaries |
|---|---|---|
| `epitype/telemetry.py` | Appends one privacy-bounded execution row for SessionStart, UserPromptSubmit, and PreCompact; records only exceptional PreToolUse outcomes and rate-limits its allow heartbeat | Fixed numeric/code schema, one `O_APPEND` write, no prompt/card/tool input, 512 KiB rotation at SessionStart |
| `epitype/findings.py` | Converts recent execution evidence and host-state checks into a deduplicated stateful finding view, a bounded SessionStart notice, doctor output, and ack/close transitions | 150 ms detector wall-clock budget; only stale-index rebuild is automatic; host settings and source code are never changed |
