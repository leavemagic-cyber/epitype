# Product scope and related work

Reviewed against the v1.9.1 source on 2026-10-21, with the v1.9.2 corrections below.

Epitype manages the lifecycle of instructions used by long-running coding agents.
Its useful unit is a workflow: retain the original instruction and scope, identify
the current decision, deliver it where relevant, synchronize supported host
settings, and recover the source after compaction. Native Codex and Claude memory
remain the underlying stores; Epitype adds no model-based memory service.

## Components and boundaries

| Stage | Existing implementation | Responsibility |
|---|---|---|
| Capture and publication | `capture`, `capture_route`, `card_io`, `harvest` | Retain source material, separate drafts from published cards, avoid clobbering other writers. |
| Instruction validity | `rule_provenance`, `rule_pipeline`, `rule_context`, `decision_lint` | Check source role/quotation, complete payload binding, declared scope, expiry and replacement links. Semantic interpretation still needs review. |
| Retrieval and delivery | `memsearch`, `recall_hook`, `recall_quiet` | Search local indexes, select current cards, preserve attribution, avoid unnecessary repeated context and measure usage within supported coverage. |
| Shared/native configuration | `core_gen`, `host_sync`, `host_native` | Generate managed instruction blocks and supported host settings, track ownership, preserve unrelated user settings and withdraw obsolete managed entries. |
| Continuity | `compact_map`, `handoff`, `precompact_hook`, `sessionstart_hook` | Preserve original transcript locations and handoffs around host compaction. |
| Checks and maintenance | `pretooluse_gate`, `stop_gate`, `compliance`, `dream`, installer/doctor | Separate advisory findings from explicit guards, retain observable results, maintain indexes and verify installed wiring. |

Source binding is not semantic authorization. Hook delivery is not proof that a
model followed the instruction. Native permissions remain the security boundary.

## Related projects

These are documented capability comparisons, not measured quality or speed rankings.

- [Codex memory](https://learn.chatgpt.com/docs/customization/memories) and
  [Claude Code memory](https://code.claude.com/docs/en/memory) supply persistent
  memory and native loading. Epitype builds on these rather than replacing their
  storage or generation.
- [claude-mem](https://github.com/thedotmack/claude-mem/blob/bbb337902271db56849b7645fee0f3b77b9aabf6/README.md)
  provides capture, compression and retrieval across supported tools. Its
  [progressive disclosure](https://github.com/thedotmack/claude-mem/blob/main/docs/public/usage/search-tools.mdx)
  and [project identity options](https://github.com/thedotmack/claude-mem/blob/main/docs/public/configuration.mdx)
  are useful design references: keep summaries small and preserve source identity.
  Epitype already has layered retrieval; its usage statistics must also preserve
  the originating vault.
- [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service/blob/d112e788dd642a4115a0d2bd462474a96bd2a852/README.md)
  includes retrieval, hooks, contradictions and supersession. Those features alone
  are not unique to Epitype.
- [ClawMem](https://github.com/yoloshii/clawmem/blob/ea92fa07fc7cdb75d5df23d30b0d53697a6d45d9/README.md)
  includes context retrieval, source metadata and compaction continuity. Compare
  the complete instruction workflow, not the presence of a similarly named feature.
- [Graphiti](https://github.com/getzep/graphiti/blob/8e320f880e7d169a907dbf2a7ac4188586c03b55/README.md)
  represents entities and changing facts in a temporal graph with episode
  provenance. It is a possible retrieval component, but adopting its graph/model
  dependencies requires a demonstrated benefit for Epitype's file-based workflow.

Epitype's emphasis is the combination of source-bound instruction governance,
owned native-setting synchronization and transcript recovery for Codex and Claude,
using the Python standard library. There is no claim of exclusive invention or
overall superiority over these projects.

## Corrections in v1.9.2

1. A previously quieted information card is reconsidered when it becomes a behavior
   card. The current card is checked only when it is a matched quieting candidate.
2. Changed tool advice is delivered again for the same target; unchanged advice
   still deduplicates, and failed output remains eligible for retry.
3. Nightly recall usage separates identical filenames in different vaults. Legacy
   unscoped statistics are discarded as derived cache data, without deleting cards.
4. Truncated, unreadable or unattributed usage evidence cannot update the quiet
   list. Its coverage is reported without stalling unrelated compliance replay.

The quieting usage metric currently parses Claude transcripts. The separate
delivery/compliance ledger supports both hosts. Extending usage measurement should
reuse that ledger and the shared transcript parser, with explicit coverage, rather
than silently treating unsupported Codex records as unused memory.

Further candidates are measuring repeated advisory cost, improving retrieval
selection and reducing cold-start work. They need representative evidence before
changing behavior. This release does not add a model service, replace native memory
or claim a token-saving percentage. It uses offline synthetic regressions; it does
not establish a model's end-to-end compliance rate.
