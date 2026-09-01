# Failure Modes

This document describes recurring system failures without naming external products. Each section includes a repository-local synthetic check. A passing command proves only the behavior named in that section; it is not evidence for every host, upgrade, or production environment.

## 1. Recall is treated as optional search

### Symptom

Memory is loaded once at session start or exposed as a tool the agent may choose to call. The conversation later compacts, the decision context changes, or the agent is already on the wrong path; no fresh rule is selected at the action surface.

### Why it happens

Storage and ranking receive most of the engineering attention. Retrieval timing is delegated to the same model whose lapse is supposed to trigger retrieval.

### Epitype countermeasure

`UserPromptSubmit` performs contextual card recall, while `PreToolUse` evaluates trigger-bearing scar cards against the current tool name and input before each covered action. This is narrower than claiming that every rule is injected before every possible action: prompt recall and scar interception are distinct paths.

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

Decision cards use `decision_key`, `status`, `current_decision_at`, and `decided_by`. The lint gate rejects multiple `active` cards for one key, warns when a key has none, and validates `superseded_by` links. This is only a partial countermeasure today: the pre-1.0 search index does not yet filter out `superseded` cards. The missing read filter must be implemented and behavior-tested before Epitype can claim that an agent sees only the current decision.

### Self-verification

```powershell
python epitype/decision_lint.py --selftest
```

This proves schema and lint behavior, not read-time exclusion.

## 3. Stored rules cannot stop actions

### Symptom

The agent can quote a rule and still execute the action the rule was meant to prevent.

### Why it happens

Memory is usually delivered as advisory context. Enforcement hooks may exist, but their conditions are disconnected from accumulated operational lessons.

### Epitype countermeasure

A scar card may carry `trigger.tool`, `trigger.input`, and `advice`. A match drives a bounded deny response, offers a safer alternative, and appends an audit row. The distinct claim is memory-derived interception criteria; the hook and deny mechanism themselves are not claimed as novel.

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

Every current tool and adapter exposes a synthetic `--selftest`, and `tests/run_all.py` requires every listed component to pass. v1.0 is reserved for the later exam-gated release. Current component selftests are useful engineering evidence, but they are not yet a mature field benchmark.

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

The transcript scanner emits proposals only and does not write cards into a vault. A human or authorized workflow must review a proposal. Structured decisions then pass the decision lint contract before being treated as current.

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

`graft.py` previews changes, merges marked entries, backs up changed host files, rejects native-memory disable diffs, runs synthetic hook health checks, and removes only owned registrations. Its synthetic untouched-host round trip restores the original registration bytes and preserves vault cards; later host edits are preserved by marked-entry removal rather than overwritten with an old backup.

### Self-verification

```powershell
python install/graft.py --selftest
```

For an installed local host, also run:

```powershell
python install/graft.py doctor
```

The selftest proves a synthetic filesystem round trip. `doctor` proves current local registration and synthetic hook execution; neither proves every future host version.
