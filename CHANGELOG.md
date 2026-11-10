# Changelog

## Unreleased

## 1.11.0 (2026-11-11)

- Turn automatic prompt matching off by default to reduce unrelated memory injection. Opt in with `proactive_recall: true`; explicit search, event capture, pending notices and compaction recovery remain available independently.
- When proactive recall is enabled, check project ownership before ranking, exclude resident rules, and prevent inactive or ineligible cards from bringing linked cards into context. Apply the same scope checks during compaction recovery.
- Improve lexical recall across writing systems and let explicit search find current cards by complete original phrases after summaries change. Ambiguous matches and missed paraphrases remain possible.
- Isolate delivery receipts by host and parent or child context, acknowledge only successfully emitted content, and retain newly relevant passages after a summary was already delivered.
- Restore short working state after compaction, with a must-read handoff pointer when the full state cannot fit. Keep child-agent recovery separate and detect overwritten handoffs.
- Review rules by their current meaning, scope and execution fields. Historical conversation audits remain explicit and optional; generated advice stays bound to reviewed content.
- Compact generated rule metadata and scope descriptions while preserving complete rule text and machine-readable recovery data. Advisory size targets no longer leave obsolete rules in place.
- Add `epitype memory-index` for configurable, lossless index budgeting, with preview, lint and explicit apply modes.
- Add `epitype fixed-load` for advisory startup-load reports, separating static estimates from observed runtime and MCP load without disabling tools or blocking work.
- Cache maintenance lint results and warm action guards incrementally. Keep card-cleanup, alias and fixed-load maintenance notices out of working conversations.
- Support token-based Claude Code compaction windows and keep learned thresholds tied to the window that produced them.
- Save unreviewed human events as searchable drafts without granting rule authority. Skip capture in non-interactive Codex exec sessions and exclude tool results and compaction summaries.
- Narrow completion-claim and deletion reminders to relevant actions, preserve localized Stop messages, and distinguish delegation from verification.
- Share one collaboration skill across Claude Code and Codex, update public guides, and register all regression suites in the test runner.
- Known limit: Codex sessions without a local transcript cannot deliver Stop reminders and may repeat tool reminders. Literal action matching remains a guardrail; host permissions govern irreversible actions.

## 1.10.1 (2026-10-29)

- Link the changelog and release notes from the PyPI project page, and ship `CHANGELOG.md`, homepage and issue links with the npm package. No code changes.

## 1.10.0 (2026-10-29)

- Isolate host sync failures per card so other cards keep syncing; bind generated rules to their rule text and scope while allowing descriptive metadata to change.
- Share repeated source notes in generated rules and recall, reducing representative output by about 30% while retaining per-rule exceptions.
- Add `epitype owner-index`, an incremental full-text index of the owner's own words with source locators and distinct roles for selected AI options.
- Add `epitype dupes`, a nightly cross-vault near-duplicate report with remembered verdicts that are reconsidered when card bodies change.
- Fuse recall ranking across BM25, aliases, one-hop links and dates, with source offsets prepared nightly.
- Fix three regressions found by the release suite before publication: recall again rechecks a quieted card that became a behavior rule, a self-closing image tag no longer hides the owner's following words from the owner index, and status or successor edits with unchanged rule text are validated again.

## v1.9.2 (2026-10-21)

- Recheck current card eligibility before applying a cached quiet decision, so information cards promoted to behavior rules are recalled again.
- Include delivered advisory content in tool-notice identity, so a changed rule reaches the same target while unchanged reminders remain deduplicated.
- Attribute nightly recall usage to the originating vault, reset ambiguous legacy statistics, and preserve quiet lists when observations are incomplete. Report the Claude-only usage coverage separately from cross-host compliance replay.
- Document product scope, related projects and evidence limits; correct stale architecture descriptions of budget notices, source quotations and host synchronization. Clarify that injected context still consumes host tokens.

## v1.9.1 (2026-10-21)

- Preserve recorded Windows and POSIX path identity during historical replay on either host; never resolve historical paths against the replay machine's filesystem.
- Resolve Windows temporary fixture paths before comparing adapter arguments and guard paths. Synchronize concurrent meter tests before their actual claims, and give the sample-integrity fixture an explicit lock budget while retaining the production fail-open budget.
- Correct both READMEs: starter examples are unpublished drafts, Stop findings are advisory, and explicit action guards remain distinct from workflow reminders.

## v1.9.0 (2026-10-21)

- Dispatch stable hook shims in the current Python process while preserving adapter results and input.
- Preserve valid earlier Codex compaction settings and make date-sensitive fixtures reproducible.
- Strengthen source-bound rule generation, native-setting ownership, browser lifecycle tracking and same-target tool-sequence evidence.
- Isolate synthetic checks from live configuration and preserve unavailable-source records as UNVERIFIED.

## v1.8.1 (2026-09-28)

- Refine behavior-card classification, expiry advice and supersession handling.

## v1.8.0 (2026-09-28)

- Add context usage and pre-compaction handoff support for both hosts.
- Restore saved context maps after compaction and validate shell examples using their declared parser.
- Detect ineffective recurrence edits without treating a longer incident narrative as stronger enforcement.

## v1.7.0 (2026-09-22)

- Align Claude and Codex guard delivery and distinguish direct file evidence from weaker read claims.

## v1.6.2 (2026-09-22)

- Bound SessionStart segments, improve project rule delivery and discover native project vaults.
- Keep configuration and home-directory inference explicit and isolate timing-sensitive fixtures.

## v1.5.0 (2026-09-21)

- Improve guard precision, remove redundant context delivery and provide portable starter examples.

## v1.4.0 (2026-09-17)

- Add explicit action-card checks, nightly feedback review and broader rule delivery.
- Keep semantic judgments distinct from literal conditions that tools can verify.

## v1.3.0 (2026-09-10)

- Generate resident rules from approved cards, reduce redundant startup context and honor reviewed split candidates.
- Expand nightly candidate inventories and preserve the distinction between captured evidence and authoritative rules.

## v1.2.0 (2026-09-06)

- Improve recall qualification, event capture, stale-state handling and portable installation.
- Preserve source roles and avoid treating quoted evidence as a new proposal.

## v1.1.0 — 2026-09-03

- Reduce repeated context delivery, improve owner-word capture and surface stale pending items.

## v1.0.0 — 2026-09-02

- Initial public release of card-based local memory governance, recall, hooks and nightly review.
