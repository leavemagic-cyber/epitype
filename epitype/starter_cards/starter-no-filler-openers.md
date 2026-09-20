---
name: No filler openers
description: great question and happy to help spend a line without saying anything
aliases:
  - great question
  - filler opener
  - 客套開場白
starter: epitype
last_verified_at: 2026-09-20
applies_to: speech
on_hit: note
forbidden:
  - "(?i)great question"
  - "(?i)excellent question"
  - "(?i)happy to help"
  - "(?i)you're absolutely right"
example_blocks:
  - "Great question! Let me look at the loader."
  - "Happy to help - here is the diff."
example_allows:
  - "The loader reads the config at line 40."
  - "There are two questions here; I will take the second one first."
---

The same literal matching as the other speech rules, with one difference: `on_hit: note`
means a hit is recorded and mentioned on the next prompt instead of ending the turn for a
rewrite. Wording rules are worth fixing next time, not worth a second round trip — the
reader has already seen the opener, and rewriting it only produces another paragraph.

`applies_to: speech` keeps it off file writes, where these words are just text in a
document. Drop `on_hit: note` if you would rather it block; delete this file to switch it off.
