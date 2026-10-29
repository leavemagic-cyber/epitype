# Failure Modes

Product behavior and coverage limits. Personal incident records and source quotations are kept outside the public repository.

## 1. Recall is treated as optional search

Prompt-time recall makes relevant cards available without depending on an optional manual search. A missing index or unreadable vault remains an explicit coverage gap.

## 2. The write side knows time; the read side ignores it

Recall evaluates card dates, expiry and supersession rather than assuming that stored text remains current. Historical context is not current authority.

## 3. Stored rules cannot stop actions

Only explicitly declared literal action conditions can be checked mechanically. Native host permissions, content checks and semantic review have different coverage.

## 4. Evaluation measures recall, not conduct

Finding a card does not prove that an agent followed it. Evaluate retrieval, delivery and observed tool behavior separately.

## 5. Automatic extraction creates unowned dirty memory

Automatic capture retains unpublished drafts until source and authority requirements are met. A generated draft does not become an owner decision by being stored.

## 6. Pipeline fragility destroys trust

Hook failures must preserve the documented host contract and report incomplete coverage. Encoding errors, parser failures and timeouts cannot count as verified success.

## 7. The host accepts the registration but never runs the hook

Registration alone does not establish execution. Doctor uses isolated hook probes and shim status to check the installed route.

## 8. The card exists, but recall cannot reach it

Indexes, aliases and generated views must include eligible cards. Deep lint checks reachability without requiring a handwritten index entry for every card.

## 9. The hook is present, healthy, and too slow to answer

Each startup segment needs a bounded deadline and a bounded result. Checking time only between segments cannot bound a blocking operation within a segment.

## 10. The ruling is injected, and the turn re-opens it anyway

A recalled ruling is context, not proof of compliance. Content checks examine applicable forbidden wording while preserving quotations and documented exemptions.

## 11. The gate reads commands, and the violation is written to a file

Write checks inspect the proposed file content as well as tool arguments. An opaque program that creates content outside the visible tool payload remains a coverage limit.

## 12. The assistant's own promise is the one thing nobody tracks

Promises can be recorded and compared with later evidence. A promise, notification or exit code alone does not prove completion.

## 13. The tidy-up pass exists and never runs

Dream supports piggyback, scheduled and disabled modes. Inspect its saved run state and report errors rather than inferring that installation means a nightly run occurred.

## 14. 事件捕捉精準度：量測方法與已知盲點

Capture precision must be measured with labeled inputs and explicit misses. Transport probes, quoted assistant text and unsupported attribution must not become standing owner rulings.

## 15. Token 洞：泛詞查詢、開場清單、承諾誤抓

Generic queries, repeated startup lists and noisy promise extraction can waste context. Qualification and deduplication reduce repetition; they do not establish billing savings.

## 16. U64：引用禁詞當證據，被當成又提議一次

A quotation of forbidden wording may be evidence rather than a new proposal. Exemptions must be scoped to the quoted claim and must not exempt a later adoption of it.

## 17. 捕捉落點：專案的進專案庫

Capture uses the conversation's project scope to choose a vault. Shared delivery storage does not change a card's originating vault identity.

## 18. Governance boundaries: authority, stale hits, delivery and unavailable vaults

Missing sources, stale hits and unavailable vaults remain explicit. Stored text is not current verification, and delivery is not authorization.

## 19. Recall slots consumed before dedupe, or monopolized by the first vault

Deduplicate before consuming recall slots and interleave vault ranks. Independent BM25 scores from different corpora are not directly comparable.

## 20. Questions can assert unsupported premises

Questions can contain unsupported factual premises. Evaluate the asserted premise even when the sentence asks for a decision; a genuine request is not itself an owner ruling.

## 21. A follow-up can silently replace the active task

A follow-up normally steers the active task. Replacement requires an explicit cancellation or incompatible new objective, not merely a newer message.

## 22. Regression markers can leak across test runs

Synthetic runs isolate homes, configuration, session identifiers and delivery markers. A previous run's deduplication state must not alter the next run's result.

## 23. Recovery can lose or misattribute original messages

Recovery preserves message roles, source locators and original text. Tool output, summaries and selected AI options cannot be relabeled as typed owner words.

## 24. Git option suffixes and diagnostic patterns mistaken for operations

Diagnostic text and Git option suffixes are not necessarily operations. Literal conditions must be checked within their declared scope instead of guessing intent from a substring.

## 25. Captured conditions silently shortened into apparent standing authority

Captured conditions and exceptions must remain attached to the quotation. Shortening conditional wording into an unconditional instruction changes its authority.

## 26. Control cleanup rules recalled in discussion but absent at tool use

A cleanup rule must reach the relevant tool boundary to affect behavior. Track only attributable resources and preserve unknown creation or closure outcomes.

## 27. Installation selftest copied a sibling test's transient directory

Installation fixtures must not copy a sibling check's transient state. Use isolated inputs and retain only the files needed for each probe.

## 28. The pre-generation procedure was re-sent on every prompt

Unchanged preparation guidance should not be resent on every prompt. Deduplication must preserve eligibility after compaction or a relevant content change.

## 29. SessionStart echoed an index the host had already loaded

Hosts already load their native instruction files. SessionStart should not echo the same index merely because it can read it.

## 30. Behavior-layer features removed by owner ruling (2026-09-09)

Historical behavior layers were simplified to remove redundant semantic interception. Current explicit literal guards remain distinct from advisory rules and host-native restrictions.

## 31. Hand-written indexes drifted; reachability lint forced them

Generated views provide a browsable catalog without forcing every card into a handwritten index. Preserve user-owned index sections during shaping.

## 32. Auto-capture wrote unverified sentences as rulings

Captured wording needs an original source, author and scope before promotion. Unverified evidence stays available as a lookup hint or unpublished draft.

## 33. MEMORY.md grows back by itself

Nightly shaping moves carried links outside allowed index sections into retained drafts. It preserves the original lines and leaves handwritten sections unchanged; dry-run writes nothing.

## 34. The card-driven action gate was a layer that should not exist (2026-09-09)

The historical regex trigger layer was removed, then replaced in part by explicitly declared literal action conditions. This does not provide semantic command understanding or replace host permissions.

## 35. Session start became a fixed 8 KB prologue

Startup context should contain actionable findings rather than a fixed catalog. Missing or truncated segments remain distinguishable from a clean result.

## 36. Unconfirmed material stays where it landed

Unconfirmed captures remain drafts. Dream lists candidates for review and does not silently turn an unreviewed item into a permanent rule.

## 37. "Nobody carries this quote" was measured by the one route almost nobody uses

A quote can be carried by an explicit source link, normalized quotation or declared carrier. Filename-only matching misses valid carriers and does not establish full source verification.

## 38. Nothing measures whether the feedback ever worked

Feedback review compares observed deliveries, blocks and available transcript evidence. A wording change or shorter response is not proof that the feedback improved behavior.

## 39. The contract was prose nobody could generate, check, or cost

Core generation assembles approved rule text with scope, provenance and supersession information. Size checks and host synchronization do not themselves verify an unavailable original conversation.

## 40. The short index was echoed to hosts that already load it

Short indexes belong in host-loaded instruction files. Removing duplicate hook delivery depends on the actual loading route, not an assumed native capability.

## 41. Recall read the bottom layer out loud (2026-09-09)

Recall distinguishes source quotations, AI wording and lookup hints. Current source-bound behavior may display the original quotation while sharing repeated labels and retaining each rule's scope exceptions.

## 42. The split-candidate list could not be finished (2026-09-10)

Nightly split candidates honor reviewed markers and recent split metadata. A finished review should not return solely because copied descriptive text still matches a mechanical heuristic.

## 43. Recall was the largest token cost, and most of it went unused (2026-09-17)

Recall usage reports measure observed delivery and mention coverage, not comprehension or universal future usefulness. Rules are not silenced merely because their filenames were not mentioned.

## 44. Every card missed what was said before a tool call (2026-09-17)

Tool-time advice can deliver a bounded excerpt of an applicable text rule. Advice is deduplicated per card and session; its delivery does not prove that the model obeyed it.

## 45. 規則再犯時: the card grew and still could not block (2026-09-25)

Appending another incident to a behavior card does not strengthen its executable condition. Recurrence review distinguishes operative field changes from narrative growth and preserves legitimate quotations.

## 46. The host rewrote the card and the card stopped blocking (2026-09-30)

Native declarations and card bindings must stay synchronized. A generated file is not the authority for changing a card, and preserving native settings is separate from proving runtime enforcement.

## 47. Replacement guidance revives an intermediate rule or disappears at the host

Replacement traversal must reach a live endpoint without reviving an intermediate superseded rule. Source checks, expiry and hook delivery still apply to successor guidance.

## 48. A cleanup reminder did not stop a tab kept "for later" (2026-10-03)

Browser lifecycle checks distinguish confirmed open resources, unknown candidates, retained resources and confirmed closure. A reset or missing group alone does not prove a tab was closed.

## 49. A missing original conversation stopped rule synchronization (2026-10-04)

An unavailable original conversation leaves the card UNVERIFIED without blocking unrelated rule synchronization. Invalid bindings retain a recoverable prior host entry or omit that card with a notice.

## 50. Bash ran Markdown text as commands and froze the machine (2026-10-12)

Shell examples and embedded Markdown require literal-safe quoting. Validate the actual shell path and proposed artifact; a successful parser invocation alone does not prove the command's intended effect.
