---
name: Stage explicit paths, not the whole tree (PowerShell)
description: the same git add -A hazard arrives through the PowerShell tool under another name
aliases:
  - git add -A powershell
  - stage everything
  - 全部暫存
starter: epitype
last_verified_at: 2026-09-20
guard_tool: PowerShell
guard_all_of:
  - "git add -A"
guard_advice: Run git status first, then name each path you actually changed
example_blocks:
  - git add -A; git commit -m wip
example_allows:
  - git add epitype/cli.py tests/run_all.py
  - git status --short
---

The twin of the Bash card. A guard names one tool, and only the shell-family names are
treated as aliases of each other — PowerShell deliberately is not, because the two tools
take different syntax and a card written for one is not automatically right for the other.

So the hazard needs a card per tool. If your agent never calls PowerShell, delete this
file; if it calls some other shell, copy this card and change `guard_tool`.
