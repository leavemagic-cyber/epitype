---
name: Guard synthetic shared configuration
description: 2026-09-09 a shared sample configuration is read and merged under the common lock, never replaced.
metadata:
  type: scar
aliases: [shared config guard, merge before replace]
scope: infra
advice: Read the current shared configuration, acquire the common writer lock, and merge only the intended field.
incident: Synthetic example, 2026-09-09: a shared sample configuration was replaced wholesale and lost another writer's field.
---

# Guard synthetic shared configuration

This synthetic example models a team write hazard without naming a real system
or file.
