---
name: Stage explicit paths, not the whole tree
description: git add -A sweeps in whatever else is lying around the worktree
aliases:
  - git add -A
  - stage everything
  - 全部暫存
starter: epitype
last_verified_at: 2026-09-20
guard_tool: Bash
guard_all_of:
  - "git add -A"
guard_advice: Run git status first, then name each path you actually changed
example_blocks:
  - git add -A && git commit -m wip
example_allows:
  - git add epitype/cli.py tests/run_all.py
  - git status --short
---

Stops one shell call: `git add -A` through Bash. It does not stop `git add .`, and
it does not know what is in your worktree — the fragment is matched literally, so a
build artifact, a stray credential file or another agent's half-finished edit is the
thing it is guarding against, not the flag itself.

To widen it, add a second card whose `guard_all_of` holds `git add .`; one card is one
conjunction, so a second form needs a second card. To switch it off, delete this file —
nothing else refers to it. `epitype starter --remove` deletes it only while it is untouched.
