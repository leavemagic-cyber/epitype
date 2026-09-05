# Failure Modes

This document describes recurring system failures without naming external products. Each section includes a repository-local synthetic check. A passing command proves only the behavior named in that section; it is not evidence for every host, upgrade, or production environment.

## 1. Recall is treated as optional search

### Symptom

Memory is loaded once at session start or exposed as a tool the agent may choose to call. The conversation later compacts, the decision context changes, or the agent is already on the wrong path; no fresh rule is selected at the action surface.

### Why it happens

Storage and ranking receive most of the engineering attention. Retrieval timing is delegated to the same model whose lapse is supposed to trigger retrieval.

### Epitype countermeasure

`UserPromptSubmit` performs contextual card recall, while `PreToolUse` evaluates trigger-bearing scar cards against the current tool name and input before each covered action. This is narrower than claiming that every rule is injected before every possible action: prompt recall and scar interception are distinct paths. At the CLI boundary, read commands never create a missing index or disguise that state as zero hits; they return an explicit no-index error and leave first-write ownership to `build`.

### Self-verification

```powershell
python adapters/claude/recall_hook.py --selftest
python adapters/claude/pretooluse_gate.py --selftest
```

The checks use synthetic vaults and events. They do not prove installation in a real host; `graft.py doctor` performs the local installed-health probe.

## 2. The write side knows time; the read side ignores it

### Symptom

An old decision is marked obsolete, yet retrieval can still return it beside the replacement. The agent receives storage history without a reliable current view.

### Why it happens

Systems often model creation or invalidation metadata without making current-state filtering the default read behavior. Recording supersession and enforcing it are different jobs.

### Epitype countermeasure

Decision cards use `decision_key`, `status`, `current_decision_at`, and `decided_by`. The lint gate requires exactly one `active` card for each key and requires `superseded_by` to point to an active card with the same key. The search index retains `status` and `superseded_by`; `query` and `recall` exclude superseded cards by default, emit a replacement guidance line when needed, and expose retained history only through `--include-superseded`. Claude prompt recall consumes the filtered default.

### Self-verification

```powershell
python epitype/memsearch.py --selftest
python adapters/claude/recall_hook.py --selftest
python epitype/decision_lint.py --selftest
```

This proves schema and lint behavior, not read-time exclusion.

## 3. Stored rules cannot stop actions

### Symptom

The agent can quote a rule and still execute the action the rule was meant to prevent.

### Why it happens

Memory is usually delivered as advisory context. Enforcement hooks may exist, but their conditions are disconnected from accumulated operational lessons.

### Epitype countermeasure

A scar card may carry `trigger.tool`, `trigger.input`, and `advice`. Shell tools match executable and unquoted argument positions by default, so trigger text in an argument is blocked too; only quoted string literals, heredoc bodies, and comments are excluded. `trigger.match: fulltext` explicitly restores whole-input matching. A match drives a bounded deny response, offers a safer alternative, and appends an audit row. The distinct claim is memory-derived interception criteria; the hook and deny mechanism themselves are not claimed as novel.

### Self-verification

```powershell
python adapters/claude/pretooluse_gate.py --selftest
```

The selftest covers a matching denial, advice text, an audit row, a non-match, and fail-open handling of a malformed card.

## 4. Evaluation measures recall, not conduct

### Symptom

A system scores well when asked what it remembers, while regressions in rule-following, failure handling, or action gating remain invisible.

### Why it happens

Question-answering benchmarks are easy to compare. Behavioral compliance requires executable scenarios, bounded claims, and release consequences when one scenario fails.

### Epitype countermeasure

Every current tool and adapter exposes a synthetic `--selftest`, and `tests/run_all.py` requires every listed component to pass. The exam engine ships in `exam/`, and the publication gate also runs the full corpus with strict failure semantics. These checks are useful behavioral regression evidence, but they are not a mature field benchmark.

### Self-verification

```powershell
python tests/run_all.py
```

The final `TOTAL PASS N/N` count covers the registered component selftests in this checkout.

## 5. Automatic extraction creates unowned dirty memory

### Symptom

Questions, examples, tentative statements, or one-off corrections are promoted into durable fact. More extraction produces more material that nobody is responsible for reviewing or retiring.

### Why it happens

Write throughput is optimized without a promotion gate, explicit decision ownership, or a cleanup lifecycle.

### Epitype countermeasure

The transcript scanner emits proposals only and does not write cards into a vault. A human or authorized workflow must review a proposal. Grant capture must first establish that the source is an owner's direct utterance; system-injected text and quotations never qualify as owner speech. Structured decisions then pass the decision lint contract before being treated as current.

### Self-verification

```powershell
python install/scar_scan.py --selftest
python epitype/decision_lint.py --selftest
```

These checks prove proposal-only scanning and decision-card validation with synthetic data. They do not prove that every human review will be correct.

## 6. Pipeline fragility destroys trust

### Symptom

Installation overwrites host configuration, a hook fails silently, native memory is disabled, or uninstall removes user data. Even strong retrieval becomes irrelevant once the operator cannot trust the pipeline.

### Why it happens

Install and removal are treated as packaging details instead of governed behavior with ownership markers, backups, health checks, and reversible tests.

### Epitype countermeasure

`graft.py` previews changes, merges marked entries, backs up changed host files, rejects native-memory disable diffs, runs synthetic hook health checks, and removes only owned registrations. Reinstallation preserves an existing curated vault list; adopting current detection requires the explicit `graft.py vaults --resync` command. Hosts register stable launchers under `~/.epitype/hooks/`; after moving the repository, `graft.py relocate --to <new-root>` updates only `config.json`, resolves all four adapters, and runs `doctor` without changing host hook configuration. A shim that must fail open leaves a bounded reason breadcrumb, synthetic health passes only with positive adapter trace evidence, and `doctor` reveals any recorded silent outage until it is explicitly cleared. Its synthetic untouched-host round trip restores the original registration bytes and preserves vault cards; later host edits are preserved by marked-entry removal rather than overwritten with an old backup.

### Self-verification

```powershell
python install/graft.py --selftest
```

For an installed local host, also run:

```powershell
python install/graft.py doctor
```

After moving the repository, run `python install/graft.py relocate --to <new-root>` once; it rewrites `config.json` and then runs `doctor` itself.

The selftest proves a synthetic filesystem round trip. `doctor` proves current local registration and synthetic hook execution; neither proves every future host version.

## 7. The host accepts the registration but never runs the hook

### Symptom

The installer writes the hook entries, doctor reports registration PASS, and the host still never injects a single line of memory. Nothing errors; the user concludes the memory system has no memory.

### Why it happens

Some hosts execute a hook only after the user has explicitly trusted it, and keep that trust in a separate state store. Registration and trust are different facts; a registration-only health check cannot see the second one.

### Epitype countermeasure

`adapters/codex/hook_trust.py check` reads the registrations and the host's trust store, classifies every Epitype hook as `TRUSTED`, `UNTRUSTED`, `DISABLED`, or `MODIFIED` (definition changed after trust was granted), and exits non-zero with the exact review step. Trust itself stays a user action in the host UI; Epitype never forges it.

### Self-verification

```powershell
python adapters/codex/hook_trust.py --selftest
python adapters/codex/hook_trust.py check
```

The selftest uses synthetic hook and config files. The `check` run inspects the real host state and is the only way to prove the hooks can run there.

## 8. The card exists, but recall cannot reach it

### Symptom

A rule or permission was written down, and the next session still asks for it again. Nobody deleted anything; retrieval simply never looked where the card was.

### Why it happens

Three independent gaps produce the same symptom: the index refresh rule compared card age against index age, so a card written shortly after a rebuild stayed unindexed until an unrelated later write; the host auto-creates a memory directory per working directory that a fixed vault list never named; and consent given in conversation was only carded when the agent remembered to do so.

### Epitype countermeasure

Index staleness is rate-limited by index age, then checked with the indexed path/mtime/size manifest so deletions, renames, backdated additions, and size changes cannot remain permanently hidden. `resolve_vaults` joins the working directory's native memory directory and its ancestors when they already hold cards, ahead of configured vaults. Grant-shaped prompts are captured verbatim into the configured vault that holds `_WORK_LEDGER.md` (falling back to the first vault), deduplicated by digest, lock-guarded, and indexed immediately so the very next prompt can recall them; interpretation is left to whoever reads the card.

Two more gaps showed up once the first three were closed: a to-do line written months ago ("pending, owner to handle") kept resurfacing after the owner had ruled the item out of scope, and the owner's correction of that resurfacing survived only as long as the agent remembered to card it. Correction-shaped owner sentences now take the same governance-vault capture path into `corrections/`, and recall pins any correction hit first with a visible marker, ahead of whatever plan card matched better lexically. When the previous assistant turn explicitly asked the owner to rule ("要你裁決", "由你決定", "please decide"), the owner's reply is captured verbatim into the governance vault's `rulings/` directory together with the question it answers, and pinned the same way; the trigger set is widened by the correction shapes observed on 2026-09-02 (sentence-initial "不是!", "我怎麼不知道", "不是指…"). `epitype/pending_lint.py` names to-do lines that have an entry but no exit (no `verify:`, not closed, older than the configured window), and session start announces the count in a single first line so the budget cannot drop it.

Injection itself is a cost. Measured on 40 real prompts, each recall injection averaged 3.6 KB over 11 lines: absolute vault paths were 32% of it, descriptions 54%, and every configured vault contributed its full top-k whether or not anything matched well. Recall now prints one `vaults:` legend line and `V1/relative` paths, truncates descriptions, admits at most two body-only hits per vault, and caps the total at eight lines; captured grant/correction/ruling cards carry the owner's words in their description so the injected line is self-contained, and a ruling stores only the window around the request phrase instead of the tail of the assistant turn. The working ledger is held to its byte budget by moving history into cards, since session start injects it whole.

Narration between tool calls ("that failure was my path typo, rerunning with C:/…") is paid for twice: once as output and again on every later turn as context, and it tells the owner nothing. The PreToolUse gate reads the transcript tail and, when the block just before the tool call is a mid-turn text segment, returns one `additionalContext` line naming its size and the rule; the permission decision is never touched, the opening line of a turn is exempt, and one narration is flagged once per session. `epitype/narration_meter.py` gives the per-transcript and per-day totals for release review.

### Self-verification

```powershell
python epitype/memsearch.py --selftest
python epitype/pending_lint.py --selftest
python adapters/claude/recall_hook.py --selftest
python adapters/claude/sessionstart_hook.py --selftest
python epitype/pending_lint.py <vault> --strict
```

## 9. The hook is present, healthy, and too slow to answer

### Symptom

Doctor passes, every selftest is green, and every prompt and tool call still injects nothing; a command a scar card should have denied goes through. Nothing errors: the hook returned exit 0 with empty output.

### Why it happens

A hook must answer within the host's timeout and within its own deadline (three seconds each until 2026-09-05; now ten at the host and nine inside the hook). Work that grows with the vault — resolving every directory entry to detect junctions, reading every card on every tool call, the interpreter's own start-up on a saturated CPU — can cross that line without any single step failing, and the deadline then does exactly what it was built to do: fail open, silently. A 2026-09-04 change that resolved every vault entry cost four seconds per call on a machine at 100% CPU; selftests with vaults of one to five cards could not see it.

### Epitype countermeasure

The vault scan lists directories with `os.scandir` and recognises symlinks and junctions from the entry's own attributes; nothing is resolved, and `_`- and `.`-prefixed parts are never entered. The stale check reads nothing inside its grace window. The action gate keeps a manifest cache of which cards declare a trigger (`<vault>/.epitype/gate_triggers.json`) and re-reads only cards that changed; a card whose trigger cannot be compiled is named to the model once per session rather than dropped. The memsearch selftest lists 300 cards and asserts that no path is resolved.

### Self-verification

```powershell
python epitype/memsearch.py --selftest
python adapters/claude/pretooluse_gate.py --selftest
python install/graft.py doctor
```

Doctor's HEALTH step runs each hook once against a synthetic event; on a loaded machine, compare its wall time with the three-second timeout in the host registration — a hook that answers in two seconds there has little margin left.
