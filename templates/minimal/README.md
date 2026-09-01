# Minimal template

Use this template for one person, one machine, and one native memory vault.

## Layout

- `vault/MEMORY.md` is the small primary-memory index for stable habits.
- `vault/SCARS.md` is the independent resident scar block.
- `vault/_WORK_LEDGER.md` is the pending-work ledger.
- `vault/habits/`, `vault/scars/`, and `vault/decisions/` hold fuller cards.
- `examples/` contains synthetic cards. It is outside the vault so examples cannot become live memory by accident.

## Start

1. Copy the contents of `vault/` into the native vault you intend to govern.
2. Keep the three blocks separate: stable habits in primary memory, incident reflexes in scars, and unfinished work in the ledger.
3. Review an example before copying it into a vault card directory. Rename it and replace every synthetic statement with your own reviewed content.
4. Point Epitype's configured vault list at that native vault through the normal installer or configuration flow.

The trigger example is deliberately narrow. A trigger card needs a tool regex, an input regex, and advice that gives a safer next path. It does not grant authority and cannot override host instructions.

## Verify

From the repository root:

```powershell
python epitype/decision_lint.py templates/minimal/examples
python adapters/claude/pretooluse_gate.py --selftest
```

The first command validates any decision cards in the example set. The second validates the trigger-card interception contract with the adapter's own synthetic event.
