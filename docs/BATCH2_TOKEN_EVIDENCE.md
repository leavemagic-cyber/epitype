# Batch 2 token measurements

## K1 — compaction

Official settings: https://code.claude.com/docs/en/env-vars and
https://developers.openai.com/codex/config-reference . Claude's percentage applies to
its auto-compact window and cannot raise the host default. Codex exposes an absolute
`model_auto_compact_token_limit`; these are separate host mechanisms.

Local read-only source: Claude 42cec705-eaf3-4537-9952-8d7e99a81bcc, inspected 2026-10-01.
252 distinct assistant message IDs; observed input plus output 89,824,135 tokens.
This source has continued since the handoff's 248-call snapshot.

Replay assumptions: 1M window, 68k carried context (the handoff session's approximate
initial context), 3k summary output per additional compaction. Each extra compaction
includes its full input cost; positive observed growth is retained. Cache rebuilds,
information retention, and actual post-compaction trajectories remain unverified.

| Percentage | Same-trace replay tokens | Additional compactions | Reduction estimate |
|---|---:|---:|---:|
| 92 | 89,824,135 | 0 | 0% |
| 60 | 89,824,135 | 0 | 0% |
| 50 | 66,916,084 | 1 | 25.50% |
| 40 | 53,994,963 | 1 | 39.89% |
| 30 | 46,227,275 | 2 | 48.54% |

Candidate: 30%, lowest replay total, two extra compactions across 252 calls.
Owner's 2026-09-25 92% ruling is historical; the current batch explicitly authorizes
re-evaluation. No global default is silently imposed on strangers. Local card deployment
occurs after integration and verification. Live savings must be measured later.

Codex's local configured threshold is 210,000. Official documentation confirms the key,
but the current Claude trace cannot establish a better Codex threshold; retain it.
Local `[memories] use_memories = true` agrees with the official default (existing memories
are injected). The nearby “false” comment is stale; this batch does not change the value.

## K3 — tool output limits

Claude's same 252-call trace contains 171 shell results, median 613 characters.
Observed 254,657 characters; replay at 30,000 preserves 254,657, at 15,000 preserves
249,752 (4,905 fewer, 1.93%). Select 15,000; full results can be saved to files when needed.
No `bashOutputMaxChars` override was found in local user settings.

Codex source: rollout 01a0f201-81fe-71d1-8908-118c1d61e9a9 (2026-09-30).
67 stored text outputs, estimated 98,008 tokens by UTF-8 bytes/4. Same-record cap replay:
10,000 → 97,891; 5,000 → 82,601 (15,290 fewer, 15.62%). Select 5,000.
Image payloads are excluded from text counts. These are measured record sizes and replay
counts, not actual savings in future calls. Live runtime read-in remains to be checked.

## K4 — historical recall

Same local delivery ledger at the 2026-10-01 refresh: 3,237 rows; 80 historical audit deliveries would be omitted,
leaving 3,157 (2.47%). Per audit card: v1 51, v2 21, v3 8. Another 19 rows now refer to
superseded cards, already covered by the existing filter; they are not credited as new
savings. All source cards readable. This reclassifies an observed ledger with current
card metadata; past card revisions and live future reread savings are unverified.
Historical sources remain available through manual search. Rules are not silenced
merely because the assistant never mentioned their names.

## K2 — bounded checkpoint handoff

Epitype policy `context_meter.handoff_tokens: 250000` opts in to a soft checkpoint
reminder. It takes the lower of that value and 60% of the host's verified compaction
limit. With K1's candidate 300000 Claude limit it starts at 180000; with Codex's
210000 limit, at 126000 rather than the existing 166000 preparation point. The reminder
requires saving the handoff and confirmed takeover; it never creates a task itself.

Same-record counterfactual with original positive growth, carry 68000 for Claude and
25737 (first observed input) for Codex, handoff overhead 5000 tokens:

| Host | Calls | Before policy replay | Earlier handoff replay | Extra resets before/after | Estimated reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| Claude, after K1 300k baseline | 252 | 46,227,275 | 31,031,774 | 2 / 4 | 32.87% |
| Codex, 166k / 126k | 68 | 6,567,377 | 5,146,757 | 3 / 4 | 21.63% |

Source traces are the same as K1/K3. This measures a replay, not live transfers,
fact retention, cache reconstruction or billing savings. K1 and K2 reductions are
not additive. New session setup cost and successful takeover remain UNVERIFIED.

## K5 — rewriting that only adds wording

Same session audit (42cec705) currently contains **9 Stop blocks**. Keep usable prompt
delivery, readable owner language and substantive no-evasion checks. Move the two
numbers-scope wording blocks to notes; move the two missing-cause-word blocks to
`require_on_hit: note`, preserving that card's forbidden suppression hazards.
Same-audit disposition replay: **9 → 5 blocks**, four fewer (44.44%). This does not
claim four actual avoided calls or any billed token saving. A previous snapshot's
six blocks is not the current audit count. Already fixed prompt false positives are
not credited to this policy change.

Nightly output now counts each rule's observed blocks, notes and current-policy
replay. It marks useful follow-up content UNVERIFIED when the audit provides no
content evidence; a change in wording or length is not proof of useful content.
