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

The scar example is deliberately narrow. A scar card names the incident it came from and the safer next path. It is advisory context, not an enforcement mechanism: it does not grant authority, cannot override host instructions, and cannot refuse a tool call. Rules that must hold mechanically belong in the host's own configuration.

## Verify

From the repository root:

```powershell
python epitype/decision_lint.py templates/minimal/examples
python epitype/card_lint.py templates/minimal/examples
```

The first command validates any decision cards in the example set. The second checks every example against the required fields for its own card type.
