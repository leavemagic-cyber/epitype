---
name: No hedged completion
description: should work now is a guess wearing the clothes of a result
aliases:
  - should work now
  - hedged claim
  - 模稜兩可的完成宣稱
starter: epitype
last_verified_at: 2026-09-20
forbidden:
  - "(?i)(that|this) should fix it"
  - "(?i)should work now"
  - "(?i)should be fine now"
  - "(?i)probably works now"
example_blocks:
  - "I changed the timeout; that should fix it."
  - "Rebuilt the image, so it should work now."
example_allows:
  - "I changed the timeout and reran the suite: 12/12 green."
  - "You should fix it before the release; I have not touched it."
  - "I have not rerun the suite, so I do not know whether it works."
---

Stops a turn that hands over a guess phrased as an outcome. Either the change was
verified, in which case say what was run, or it was not, in which case say that — the
hedge costs the reader a round trip to find out which one it was.

Each entry is an ordinary Python regular expression, one per line. `(that|this) should
fix it` is deliberately narrower than `should fix it`, so ordinary advice such as
`you should fix it` still gets through. Delete this file to switch the rule off.
