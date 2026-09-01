---
name: Gate synthetic export
description: Intercept publication of a sample export until its census and exam evidence are available.
metadata:
  type: scar
aliases: [sample export gate, exam before publish]
scope: governance-core
trigger:
  tool: ^(Bash|Shell|shell_command)$
  input: (?i)\b(publish|upload)\b.*\bsynthetic-export\b
advice: Run the synthetic exam and inspect the generated census before publishing the sample export.
---

# Gate synthetic export

This example connects an incident-style reflex to a release gate without naming a real product, account, or destination.
