---
name: scar-destructive-git
description: 2026-09-09 destructive Git operations need explicit scope approval for the exact target.
metadata:
  type: scar
  source_agent: codex
aliases: [destructive git, scope approval before reset]
advice: Preserve the working tree; obtain explicit approval for the exact destructive target and scope, or use a non-destructive alternative such as a stash or a new branch.
incident: Synthetic example, 2026-09-08: a clone option and a diagnostic regular expression were mistaken for destructive operations by a lexical pattern.
---

# Destructive Git operations

A card of this shape records what went wrong and the safer route. It is
advisory context read back into a turn, not an enforcement mechanism: a rule
that must hold even when nobody reads it belongs in the host's own
configuration, where the tool call is refused before it runs.

The 2026-09-08 incident above is why: a lexical pattern narrow enough to catch
`reset --hard` also caught `--no-checkout` and a diagnostic regular expression
that executed nothing. A card cannot be a shell parser, and pretending
otherwise produced both false denials and a false sense of coverage.
