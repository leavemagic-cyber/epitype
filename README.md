# Epitype

Epitype is a governance layer on top of your CLI agent's native memory: it does not store more; it makes what is stored govern behavior.

It builds on native memory. It does not replace it, disable it, or introduce a second memory service.

> Release status: pre-1.0. The repository is usable for synthetic evaluation, but the release exam and one read-time supersession control listed under Limitations are not complete.

## The problem

- An agent is least likely to remember to search memory at the moment it most needs the rule. Optional search and a one-time session preload are not behavioral discipline.
- A write path may record when a decision changed while the read path still returns the old and current decisions together. Storage truth does not guarantee decision-time truth.
- A stored rule is context, not enforcement. Unless memory is connected to an action gate, the agent may acknowledge the rule and still perform the prohibited action.
- The benchmarks surveyed in the landscape analysis measure question answering or recall. They do not measure whether an agent follows a remembered rule while acting.

## What Epitype does

1. **Per-action contextual forced recall.** Before each covered tool action, the hook evaluates scar triggers against the current tool and input; prompt-time recall separately selects relevant cards. This is contextual selection at the action surface, not a memory dump performed only at session start.
2. **Decision supersession contract.** Decision cards carry a stable key, status, effective time, and decision source. The current lint gate rejects multiple active cards for one key and warns when a key has none. The intended read contract is that an agent sees only the active decision; the current pre-1.0 search path does not yet exclude superseded cards, so that end-to-end claim is not made here.
3. **Scar-driven interception.** Denial conditions come from accumulated lesson cards with explicit triggers, rather than from the mere existence of a hook. A match produces a bounded deny response, a safer alternative, and an audit row. Epitype did not invent tool hooks or denial; it connects remembered lessons to that mechanism.
4. **Behavior-level measurability.** Current components ship with synthetic behavioral selftests. The v1.0 release line is exam-gated and will include the exam runner; recall scores alone are not release evidence.
5. **Native-first operation.** Installation merges marked hook entries and never intentionally disables native memory. In the synthetic untouched-host round trip, uninstall restores the original host registration bytes and preserves the vault. If host files changed after installation, marked-entry removal preserves those later changes instead of overwriting them.
6. **One memory and one hook contract across supported CLIs.** The Claude Code and Codex adapters use the same configured vault list, event names, budgets, trigger fields, and core tools. This is a concrete two-host contract, not a claim that every CLI or every future host upgrade is already covered.

## Failure modes of the current landscape

The table names failure modes, not products. Every proof command exercises this repository with synthetic data.

| Failure mode | Epitype countermeasure | Run it yourself |
|---|---|---|
| Recall is optional or happens only at session start | Contextual prompt recall plus trigger evaluation on covered tool actions | `python adapters/claude/recall_hook.py --selftest`<br>`python adapters/claude/pretooluse_gate.py --selftest` |
| The write side knows a decision is obsolete, but the read side can still return it | Structured decision status, multiple-active rejection, and a missing-active warning; read-time exclusion remains a disclosed pre-1.0 gap | `python epitype/decision_lint.py --selftest` |
| Stored rules cannot stop actions | Trigger-bearing scar cards drive bounded denial, an alternative path, and an audit row | `python adapters/claude/pretooluse_gate.py --selftest` |
| Evaluation measures recall rather than conduct | Component selftests assert outputs, bounds, failure modes, and round trips; v1.0 adds the release exam | `python tests/run_all.py` |
| Automatic extraction turns guesses and corrections into unowned memory | Transcript scanning produces proposals only; structured decisions must pass lint before they count as current | `python install/scar_scan.py --selftest`<br>`python epitype/decision_lint.py --selftest` |
| Fragile installation or removal destroys trust | Dry-run, marked merge, native-memory protection, synthetic health checks, preserved vaults, and a tested uninstall round trip | `python install/graft.py --selftest` |

See [Failure Modes](docs/FAILURE_MODES.md) for the symptom, cause, countermeasure, and verification boundary of each row.

## Quickstart

Requirements: Python 3 and a supported Claude Code or Codex configuration. Epitype uses only the Python standard library.

From the repository root, preview every planned change first:

```powershell
python install/graft.py install --dry-run
```

If the preview names only the hosts and locations you intend to change, install and run the synthetic health check:

```powershell
python install/graft.py install
python install/graft.py doctor
```

Installation merges entries marked as Epitype, writes backups before changing existing host files, and points the shared configuration at detected native vaults. When no native vault is present, it creates an empty fallback vault; it does not disable native memory.

Choose a starter under [`templates/`](templates/): `minimal` for one person on one machine, `team` for a shared vault with the common write-lock contract, or `power` for the full layout with census and exam-ready directories.

## Uninstall

Preview removal, then remove only Epitype-owned registrations and configuration:

```powershell
python install/graft.py uninstall --dry-run
python install/graft.py uninstall
```

Native memory settings and vault cards are preserved. The tested untouched-host round trip is byte-exact for the host registration files; when later host changes exist, the uninstaller removes marked Epitype entries without erasing those changes. Read [Uninstall Epitype](docs/UNINSTALL.md) before restoring a backup manually.

## Architecture

Epitype separates stable habits, incident-born scars, and pending work; exposes four retrieval routes; and gives decisions and scars explicit lifecycles. See [Architecture](docs/ARCHITECTURE.md).

## Versioning

- **v1.0** means an exam-gated release, not merely a tagged build. The packaged behavior exam must pass before release.
- After v1.0, Epitype is updated monthly from observed usage. A new rule or interceptor must be tied to evidence and a reproducible check, not novelty alone.

## Limitations

1. Installation cannot be easier than native memory that is already enabled by default. Epitype's value proposition is governance, not zero setup.
2. Hook injection is bounded by host output and timing limits. Epitype uses a 10 KiB output ceiling and a three-second fail-open deadline, so it must select context rather than inject everything.
3. Behavior-level measurement is early. Current selftests are synthetic, and the v1.0 exam runner is not yet present in this U5 build.
4. Decision cards, multiple-active rejection, and a missing-active warning exist, but `memsearch.py` does not yet filter out superseded cards. Until that is wired and tested, `the agent sees only the current decision` is a design target, not a shipped guarantee.
