---
name: scar-destructive-git
description: Require explicit scope approval for destructive Git operations.
trigger:
  tool: '^(Bash|PowerShell|Shell|shell_command|exec_command)$'
  input: '(?i)(?:(?<![\w-])|(?<=\\n))git(?:\.exe)?(?:\\?[\x22\x27])?[ \t]+(?:[^\n]*(?:[ \t]|\\n))?(?:reset[ \t]+--hard\b|clean[ \t]+-fd\w*\b|checkout[ \t]+--(?:[ \t]|$))'
  match: command
advice: Preserve the working tree; obtain explicit approval for the exact destructive target and scope.
incident: Synthetic gate regression, 2026-09-08: a clone option and a diagnostic regex were mistaken for operations.
metadata:
  type: feedback
---

Match complete command and operation names, not option suffixes such as
`--no-checkout`. This remains a narrow lexical guard, not a complete Git or shell
interpreter. The adapter's conservative full-text fallback remains active for
unsupported syntax; literal examples in such scripts can still be rejected.
