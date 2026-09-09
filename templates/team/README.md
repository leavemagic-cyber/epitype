# Team template

Use this template when several trusted writers share one vault.

## Layout

- `vault/MEMORY.md`, `vault/SCARS.md`, and `vault/_WORK_LEDGER.md` keep stable habits, incident reflexes, and pending work separate.
- `vault/habits/`, `vault/scars/`, and `vault/decisions/` hold fuller cards.
- `examples/` contains synthetic team conventions, one decision card, and one scar card. Examples remain outside the live vault until reviewed.

## Shared-write contract

All supported Epitype writers use the common `memspec.file_lock` contract. A lock is created beside the exact file being changed and is released by its owner; stale-lock handling is bounded by the shared constants. Direct edits made by unrelated software do not automatically acquire that lock, so a team must route automated writes through the governed tools or coordinate manual edits separately.

One shared vault does not mean every participant has equal authority. Use `scope`, `decided_by`, and the source-of-authority rules in `docs/ARCHITECTURE.md`.

## Start

1. Put the empty `vault/` skeleton in the team's approved shared location.
2. Configure each supported CLI to use that same vault through Epitype's vault list.
3. Keep generated search databases, audit logs, and transient lock files out of ordinary card review.
4. Review and adapt example cards one at a time before moving them into the vault.

## Verify

From the repository root:

```powershell
python epitype/memspec.py --selftest
python epitype/decision_lint.py templates/team/examples --audit
python epitype/card_lint.py templates/team/examples
```

The lock selftest uses concurrent synthetic writers. It does not prove that an external editor participates in the lock contract.
