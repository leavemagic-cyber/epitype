---
name: Guard synthetic shared configuration
description: Intercept replacement of a shared sample configuration until it is read and merged under the common lock.
metadata:
  type: scar
aliases: [shared config guard, merge before replace]
scope: infra
trigger:
  tool: ^(Bash|Shell|shell_command)$
  input: (?i)\b(overwrite|replace)\b.*\bshared-config\b
advice: Read the current shared configuration, acquire the common writer lock, and merge only the intended field.
---

# Guard synthetic shared configuration

This synthetic trigger models a team write hazard without naming a real system or file.
