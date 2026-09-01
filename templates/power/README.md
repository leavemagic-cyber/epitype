# Power template

Use this template for the full three-block layout plus scar census and exam-gated release preparation.

## Layout

- `vault/` is an empty primary-memory, scar, pending-ledger, habit-card, scar-card, and decision-card skeleton.
- `examples/` contains three synthetic cards, including a trigger-driven interception card.
- `census.json` is a runnable configuration for the empty scar skeleton.
- `exam/` reserves the release-exam boundary. The U5 repository does not yet contain the U6 engine or its final case schema, so this template does not invent one.

## Start

1. Copy the empty vault skeleton into the approved native vault.
2. Review any example before moving it into a live card directory.
3. Adjust the census layer list only when adding a real scar source. Census is a machine view, not a second source of truth.
4. Add exam cases only after the packaged runner defines and validates the schema. A v1.0 candidate must pass the release exam; a placeholder directory is not a pass.

## Verify

From the repository root, validate the synthetic examples and build the empty census view:

```powershell
python epitype/decision_lint.py templates/power/examples --audit
python epitype/scar_census.py build --config templates/power/census.json --out templates/power/census.generated.md
python adapters/claude/pretooluse_gate.py --selftest
```

`census.generated.md` is generated evidence and may be removed after inspection. When the exam runner ships, use its documented command; do not treat the reserved `exam/` directory as executable evidence.
