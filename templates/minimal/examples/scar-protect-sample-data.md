---
name: Protect synthetic sample data
description: Intercept removal of a named synthetic data set until a recoverable route is available.
metadata:
  type: scar
aliases: [sample data guard, recoverable removal]
scope: data
trigger:
  tool: ^(Bash|Shell|shell_command)$
  input: (?i)\b(delete|remove)\b.*\bsample-data\b
advice: Resolve the exact sample-data target, inspect it read-only, and create a recoverable backup before retrying.
---

# Protect synthetic sample data

This neutral example demonstrates a narrow tool-and-input trigger. Replace both regular expressions and the advice before using it outside a sandbox.
