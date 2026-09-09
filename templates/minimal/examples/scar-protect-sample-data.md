---
name: Protect synthetic sample data
description: 2026-09-09 removal of a named synthetic data set needs a recoverable route first.
metadata:
  type: scar
aliases: [sample data guard, recoverable removal]
scope: data
advice: Resolve the exact sample-data target, inspect it read-only, and create a recoverable backup before retrying.
incident: Synthetic example, 2026-09-09: a sample data set was removed before a recoverable copy existed.
---

# Protect synthetic sample data

This neutral example shows the two fields a scar card carries: the incident it
came from, and the safer route to take instead. Replace both before using it
outside a sandbox.

A card is advisory context, not an enforcement mechanism. Rules that must hold
mechanically belong in the host's own configuration.
