# Power template

Use this template for the full three-block layout plus scar census and exam-gated release preparation.

## Layout

- `vault/` is an empty primary-memory, scar, pending-ledger, habit-card, scar-card, and decision-card skeleton.
- `examples/` contains four synthetic cards: one decision, one habit, and two scars.
- `census.json` is a runnable configuration for the empty scar skeleton.
- `exam/` explains the boundary between this template and the packaged exam runner in `exam/exam_runner.py`.

## Start

1. Copy the empty vault skeleton into the approved native vault.
2. Review any example before moving it into a live card directory.
3. Adjust the census layer list only when adding a real scar source. Census is a machine view, not a second source of truth.
4. Keep release cases outside this empty template and run them through the packaged runner. A release candidate must pass the configured corpus; an empty template directory is not a pass.

## Verify

From the repository root, validate the synthetic examples and build the empty census view:

```powershell
python epitype/decision_lint.py templates/power/examples --audit
python epitype/scar_census.py build --config templates/power/census.json --out templates/power/census.generated.md
python epitype/card_lint.py templates/power/examples
```

`census.generated.md` is generated evidence and may be removed after inspection. Run `python exam/exam_runner.py --strict` for the bundled synthetic corpus; do not treat the reserved template directory itself as executable evidence.
