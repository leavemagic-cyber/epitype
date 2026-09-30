# Batch 2 card classification and native restriction migration

302 previously unclassified active cards were individually reviewed in four configured vaults. Each judgment and the original content hash is in `BATCH2_CARD_CLASSIFICATION.json`; the JSON stores decisions, not a full copy of the vaults. Candidate cards preserve their bodies and record the reviewing agent and date. Staged classification has unclassified=0 and FAIL=0 in all four vaults. Product gaps remain visible instead of pretending that an observed tool call proves target identity, authorization, or semantic correctness.

The resident state-preservation and credential cards, process-cleanup card, heredoc scar and protected-runtime-files card own the existing native restrictions. Claude's 43 literal deny entries and Codex's 10 forbidden prefix patterns are preserved exactly. The migration fixture verifies set equality, and the installed `codex execpolicy check` accepts both legacy and rendered rule files. No new concurrency setting is introduced.

Official syntax and loading behavior: [Claude permissions](https://code.claude.com/docs/en/permissions), [Codex rules](https://developers.openai.com/codex/rules). Claude's current documentation says Write/Glob/legacy MultiEdit path entries are accepted but not consulted; the seven such legacy entries in this snapshot are retained for lossless migration, and their corresponding Read/Edit entries already exist. They are not credited as effective protection. Shell wrappers and host permission bypass modes have their own official limits; native restriction equality does not prove interception of every possible tool path.

The generic adoption operation runs after ordinary sync renders the cards:

```powershell
python -m epitype sync <configured-vaults> --apply
python -m epitype.native_migration <configured-vaults> --legacy-rules <legacy.rules> --apply
```

Adoption refuses native drift, unsupported/nonliteral legacy statements, non-forbidden rules, different prefix sets or an existing archive. It claims only the card-declared deny entries and archives the exact legacy rules file as `.rules.migrated`. Other user settings and rules remain outside its ownership. Codex loads rules at startup, so new-session loading remains separate from a successful file render. Local production deployment is deferred until final integration; the staged lint counts do not assert current live vault counts.

Validation: migration regression 2 errors before repair to 4/4 pass, including equal sets, foreign entry preservation and rejection without ownership changes. The original 43/10 fixtures remain unchanged.
