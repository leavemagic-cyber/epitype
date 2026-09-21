# Failure Modes

This document describes recurring system failures without naming external products. Each section includes a repository-local synthetic check. A passing command proves only the behavior named in that section; it is not evidence for every host, upgrade, or production environment.

## 1. Recall is treated as optional search

### Symptom

Memory is loaded once at session start or exposed as a tool the agent may choose to call. The conversation later compacts, the decision context changes, or the agent is already on the wrong path; no fresh rule is selected at the action surface.

### Why it happens

Storage and ranking receive most of the engineering attention. Retrieval timing is delegated to the same model whose lapse is supposed to trigger retrieval.

### Epitype countermeasure

`UserPromptSubmit` performs contextual card recall, while `PreToolUse` checks the content a file write is about to commit against the settled rulings and the card contract. This is narrower than claiming that every rule is injected before every possible action: recall is a retrieval path and the write gate is a content gate, and neither one refuses a shell command — that is the host's own native rules (§34). At the CLI boundary, read commands never create a missing index or disguise that state as zero hits; they return an explicit no-index error and leave first-write ownership to `build`.

CJK punctuation must delimit Latin query terms: `請查：identifier。` cannot search for the literal `：identifier。`. Recall splits those prose delimiters while preserving ASCII punctuation inside technical identifiers and paths. This does not provide semantic translation or fix missing bilingual aliases.

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

2026-09-09 (§34) settled where the enforcement belongs, and 2026-09-16 corrected one half of that settlement after testing its premise.

What Epitype refuses on content, unchanged since §34: the write gate checks the text a file write would land against the owner's settled `forbidden` patterns and against `card_lint`'s contract for the card's own type, and the Stop gate checks everything the assistant said during a turn (see §44) against the same rulings through the same validator. Both append an audit row naming the rule, the ruling and the filename — never the content.

**The premise that failed.** §34 removed card-driven action interception on the understanding that irreversible actions would move to the host's own native rules (Claude `permissions.deny`, Codex `execpolicy`). Four of nine hazard classes moved across. The remaining five could not: Claude's Bash permission patterns match the command text positionally with no AND operator, and the documentation states they "aren't a security boundary". A rule written for the heredoc hazard was installed and verified not to block anything. Those five classes were therefore homeless for a week, and on 2026-09-16 one of them — a heredoc eating one level of backslashes — was hit four times in a single session with the lesson already carded three times over.

**What came back, and how narrow it is.** The owner lifted the "cards may not carry an action condition" half of §34 that day. A card may declare `guard_tool` plus `guard_all_of`, a list of literal fragments; the call is denied only when *every* fragment appears in the call's own text. §34's three objections were each answered rather than waived:

- *A regex cannot be a shell parser.* There is no regex. `fragment in text` does not claim to parse a shell, so it neither over-matches greedily nor misses on quoting rules. It over-approximates toward denial, which is the safe direction.
- *Every tool call pays for it.* `PreToolUse` already runs for the write gate, and guard discovery shares the same manifest-cache design as the Stop gate's rulings, so the marginal cost is a few substring checks.
- *A mis-written card fails silently.* `card_lint` FAILs a guard with no fragments, too many fragments, or a single fragment short enough to disable a whole tool — and FAILs any card whose gate fields were written one level deep, which is a real disarming seen the same day. A card the adapter cannot use is named on stderr instead of being dropped.

**The third shape, for rules whose compliance is invisible.** A large class of rule is "do this first" — verify before claiming done, read the canonical before answering — and whether it was done happens out of sight. Restating the rule as *if you did it, say so* moves the omission into the message, where a string check reaches it: a card names `require_when` (the condition) and `require_text` (what must also appear), and the turn is blocked when the first matches and the second does not. The step is not merely skippable-but-noticed; skipping it while claiming otherwise stops being an omission and becomes a false statement, which the honesty floor already governs.

**Arming is independent of a ruling.** The Stop gate reads any card that declares `forbidden` or `require_when`, not only cards carrying a `decision_key`. `card_lint` puts the question — arm it, or say why it cannot be armed — to the card types that record how to behave next time (`feedback`, `correction`, `scar`, `habit`); the types that record a fact rather than a habit are not asked, and rule cards reach the agent through the generated core instead. The two halves have to match: a lint that tells an author to add `forbidden` while the gate reads only decision cards would hand out silent disarming as advice — which is exactly what happened on 2026-09-16 before the gate was widened, leaving six freshly armed cards inert.

What did **not** come back: semantic judgement, intent, and any opinion about a command a card has not named. Genuinely irreversible actions — destructive git, killing the host process, reading credential material — remain the host's native rules, which refuse the call before it runs and cannot be turned off by a malformed card.

### Self-verification

```powershell
python adapters/claude/pretooluse_gate.py --selftest
python adapters/claude/stop_gate.py --selftest
python tests/action_guard_regression.py
```

The write gate's selftest covers a forbidden-content denial, the ruling text in the reason, an audit row that carries no content, the card contract's FAIL and WARN levels, the same-session dedupe, and fail-open handling of an unusable pattern — plus the surviving negative half of §34: a card that still declares the retired `trigger:` field denies nothing and audits nothing.

The action-guard regression pins both halves of the 2026-09-16 correction. A guard denies when all of its fragments are present, and does not when any one is missing, when the tool is not its own, or when no card names the command at all — the four hazards §34 retired stay retired unless a card names them. It also fires every time rather than once per session, since a guard that stopped guarding after one hit would pass exactly the repeat it exists to prevent. On the lint side it pins that a nested gate field and an over-wide lone fragment are both FAIL.

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

`graft.py` previews changes, merges marked entries, backs up changed host files, rejects native-memory disable diffs, runs synthetic hook health checks, and removes only owned registrations. Reinstallation preserves an existing curated vault list; adopting current detection requires the explicit `graft.py vaults --resync` command. Hosts register stable launchers under `~/.epitype/hooks/`; after moving the repository, `graft.py relocate --to <new-root>` updates `config.json` and the nightly task command when enabled, resolves the adapters, and runs `doctor` without changing host hook configuration. A shim that must fail open leaves a bounded reason breadcrumb, synthetic health passes only with positive adapter trace evidence, and `doctor` reveals any recorded silent outage until it is explicitly cleared. Its synthetic untouched-host round trip restores the original registration bytes and preserves vault cards; later host edits are preserved by marked-entry removal rather than overwritten with an old backup.

### Self-verification

```powershell
python install/graft.py --selftest
```

For an installed local host, also run:

```powershell
python install/graft.py doctor
```

After moving the repository, run `python install/graft.py relocate --to <new-root>` once; it updates `config.json` and the enabled nightly task command, then runs `doctor` against both.

The selftest proves a synthetic filesystem round trip. `doctor` proves current local registration and synthetic hook execution; neither proves every future host version.

## 7. The host accepts the registration but never runs the hook

### Symptom

The installer writes the hook entries, doctor reports registration PASS, and the host still never injects a single line of memory. Nothing errors; the user concludes the memory system has no memory.

### Why it happens

Some hosts execute a hook only after the user has explicitly trusted it, and keep that trust in a separate state store. Registration and trust are different facts; a registration-only health check cannot see the second one.

### Epitype countermeasure

`adapters/codex/hook_trust.py check` reads the registrations and trust store, then verifies candidate trusted entries against Codex's native `hooks/list` inventory and current hashes. A cached digest or an old `trusted_hash` alone cannot prove runtime trust. Results are `TRUSTED`, `UNTRUSTED`, `DISABLED`, `MODIFIED`, or `UNVERIFIED`; missing or unavailable native evidence fails closed. The bounded inventory query starts no model turn. Trust itself stays an explicit action in the host UI; Epitype never forges it.

A trusted registration can still never run: on Windows, Codex launches command hooks through `cmd.exe /C`, whose quoting rule drops the first and last quote of a line that begins with one. A command written as `"…/python.exe" "…/recall.py"` therefore fails at the shell before Python starts (2026-09-05, a day after a quoted form was introduced), while Claude Code, which runs the same line through a POSIX shell, keeps working. The installer now leaves tokens unquoted whenever the path allows, and when a token must be quoted it also registers a `commandWindows` form wrapped in one outer pair of quotes, which is what `cmd.exe /C` preserves.

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

The vault scan lists directories with `os.scandir` and recognises symlinks and junctions from the entry's own attributes; nothing is resolved, and `_`- and `.`-prefixed parts are never entered. The stale check reads nothing inside its grace window. The Stop gate keeps a manifest cache of which cards declare a decision key and re-reads only cards that changed; a ruling whose `forbidden` pattern cannot be compiled is named to the model once per session rather than dropped. The memsearch selftest lists 300 cards and asserts that no path is resolved. (Before §34 the write gate kept a second such cache, `<vault>/.epitype/gate_triggers.json`, for trigger-bearing cards; nothing reads or writes it any more, and existing files are left in place.)

### Self-verification

```powershell
python epitype/memsearch.py --selftest
python adapters/claude/pretooluse_gate.py --selftest
python install/graft.py doctor
```

Doctor's HEALTH step runs each hook once against a synthetic event and prints its wall time beside the verdict, warning when a hook used more than half of the time the host registration allows. It also warns when `repo_root` has uncommitted changes: the live hooks run whatever is in that tree, and the 2026-09-04 batch ran live for a day precisely because an unfinished change needs no release to take effect.

### The deadline only guards the gaps between segments

Checking `expired(started_at)` between segments is not the same as bounding each segment. A single unbounded segment — the 2026-09-06 incident was `SessionStart`'s pending-lint summary doing a full vault scan, 2.8 s warm across two vaults and unbounded cold — can by itself burn through the host's 10 s kill window before the next check ever runs. The host then reports the whole hook `Failed` and every piece of context that segment's siblings had already assembled is lost with it, not just the slow line.

The fix is a soft per-segment budget, not a single top-level deadline: `SESSIONSTART_BUDGET_SECONDS` bounds the whole session-start handler, and each disk-scanning segment gets its own `*_HOOK_BUDGET_SECONDS` slice of whatever remains. A segment that cannot finish inside its slice is omitted — its one line is dropped, nothing else is — rather than being allowed to run unbounded and take the entire injection down with it. Any state file such a segment writes is written to a temp path and moved into place with `os.replace`; a kill mid-write then leaves the previous good file in place instead of a truncated or 0-byte one.

## 10. The ruling is injected, and the turn re-opens it anyway

### Symptom

The owner settled a question weeks ago. The session opens with that ruling listed in the owner's own words, recall pins it again mid-session, and the assistant still ends a turn by offering the ruled-out option as a live choice, or by putting the settled question back to the owner. Nothing failed: every injection worked, and the model simply wrote past it.

### Why it happens

Injection is advice, not enforcement. `SessionStart` and `UserPromptSubmit` both write into the context window and then hand control back to the model; nothing reads what the model actually produced. A rule enforced only by the attention of the party it constrains is not enforced. The 2026-09-05 incident is the shape of it: a decision the owner had ruled on 08-13 came back as an option in the same session that opened with that decision on screen, and the owner asked why it keeps happening.

### Epitype countermeasure

The `Stop` hook compares what the assistant said during the turn (every text block since the last user prompt, §44) against the active decision cards of the cwd vault and the governance vault before the turn is allowed to end. A card's `forbidden` sequence — regular expressions or literals, validated by the same rejection rules the action gate uses, so a card cannot hang the turn it guards — blocks the turn and quotes the owner back. A question sentence naming one card by two of its `aliases` blocks it as well: putting a settled matter back to the owner is the same failure as proposing it. A block emits `{"decision": "block", "reason": ...}` and is audited to `_GATE_LOG.jsonl` as `stop_block`. The host re-runs `Stop` after a block, so `stop_hook_active` is never blocked twice, and one `(decision, message)` pair blocks once per session — the marker lives in the recall marker directory, so compaction clears it with the rest.

Every other path fails open: a missing or unreadable config, an unusable pattern (named on stderr, never silently dropped), a vault with no decision cards, or the hook's own deadline all let the turn end.

### How to write a `forbidden` pattern

Write the shape of the sentence that re-opens the ruling, not the name of the thing that was ruled out. A bare noun blocks every mention of it — including the one sentence the owner most needs to read: "here is why we are *not* adopting X". That is the 2026-09-06 incident: explaining a rejected option back to the owner was blocked as if it were a fresh proposal.

- Write this: `(建議|要不要|是否|應該).{0,12}(納入|採用|改成)X` — the proposing verb is inside the pattern, so only a fresh proposal matches.
- Not this: `X` — every explanation, every retrospective, and every edit of the card that defines the rule matches too.

`card_lint` warns `forbidden-bare-term` on an item that carries no proposing verb, is at most `memspec.FORBIDDEN_BARE_TERM_MAX_CHARS` (8) characters long, and contains no regex metacharacter — the three signals a machine can read off a bare noun. WARN, not FAIL: a short pattern that does carry a verb is legitimate, and a literal may be exactly what the owner wants.

The rule has to stay editable, so `PreToolUse` rule A (§11) exempts two shapes from blocking: a write whose post-write text carries the same `decision_key` as the pattern that fired, and one whose matched fragment sits inside that text's own frontmatter `forbidden:` block. Changing a rule is always allowed; re-stating it anywhere else is not.

### Self-verification

```powershell
python adapters/claude/stop_gate.py --selftest
python install/graft.py doctor
python adapters/codex/hook_trust.py check
```

Codex will not run a newly registered hook until the owner trusts it again in the Codex app; `hook_trust check` is the only step that reports that state as a failure rather than as green.

## 11. The gate reads commands, and the violation is written to a file

### Symptom

Two shapes, one hole. The owner has settled a question and the assistant does not say the ruled-out thing out loud — it writes it into a plan, a report, or a card, where the Stop gate never looks because the turn ends with prose about the file rather than the file's text. And a memory card lands in the vault missing the fields its own type requires: `card_lint` names it afterwards, on the next scan, once the card is already the vault's answer to a query.

### Why it happens

The action gate matched on the Bash command string, so a file written through `Write`/`Edit` was never inspected at all; the Stop gate matched on the last assistant message, which is a summary of the write, not the write. `card_lint` is a scan, not a gate: it reports a malformed card, it does not stop one from being created. Between them, the content of a write was the one thing nothing read.

### Epitype countermeasure

`PreToolUse` inspects the text a file-writing call is about to put on disk — `Write`'s `content`, `Edit`'s `new_string`, each `new_string` of a `MultiEdit`, and the equivalents of the Codex-shaped tool names. Since 2026-09-22 it also reads the `*** Begin Patch` envelopes carried in the text of any other call, per target file and added lines only (see "What it still does not catch" below for why the tool-name list stayed as it was).

Rule A blocks new content matching any `forbidden` pattern of an active decision card in the cwd vault or the governance vault, quoting the owner and the matched fragment. The decision cards, the pattern validator, and the manifest cache are the Stop gate's own, so a ruling cannot be enforced at the end of a turn and ignored mid-turn. Editing the rule itself is exempt: a hit is ignored when the post-write text carries the same `decision_key` as the card that fired, or when the matched fragment sits inside that text's own frontmatter `forbidden:` block — otherwise the card defining a pattern is the one file that pattern makes unwritable (2026-09-06 incident; §10 covers how to write the pattern so this comes up less).

Rule B applies when the target is a card of a registered vault — `.md`, no `_`/`.` prefixed path part, not the memory index, by the same filter `memsearch` uses. The prospective post-write text is checked by `card_lint.check_card`, the same single-card check the CLI scan runs: FAIL (a missing required field for the card's type, broken frontmatter) blocks and names the missing fields with a line to copy; WARN only advises through `additionalContext`. For an `Edit`, the post-write text is the current file with one `old_string`→`new_string` substitution applied; when `old_string` is not in the file, nothing is judged and the call proceeds — a guessed result would block a card nobody wrote. A generic card whose frontmatter carries no date is not blocked when the date can be read out of its body or its filename, since that is the same derivation the CLI scan applies; the git-history source is not available here, because the gate judges text that is not on disk yet.

A block is audited to `_GATE_LOG.jsonl` as `write_block` with the rule and either the `decision` key or the `card_path`, never the content itself. One `(rule, file, content digest)` blocks once per session, so an assistant that cannot satisfy a ruling is not denied the same write forever.

### What it still does not catch

- **A shell redirection or heredoc** (`echo … > card.md`, `python - <<PY`) writes a file without any file-writing tool, so this gate never sees it. That path is covered by the `no-bare-redirect` scar card on the action gate's command matching, not here.
- **Diff-shaped tools** remain out of `WRITE_GATE_TOOL_NAMES`, for the reason they were excluded on 2026-09-06: a diff's context and removed lines would match `forbidden` patterns the write never adds, and blocking those is a false block. What changed on 2026-09-22 is *what is read*, not the tool-name list. Any call whose input text carries a `*** Begin Patch` … `*** End Patch` envelope is parsed as text by `epitype/patch_envelope.py` — no execution, no language parsing — and each target file inside it is judged separately on **its added (`+`) lines only**, which is exactly the objection above answered: context and removed lines are never handed to Rule A. Rule B judges only an `Add File`, whose added lines are the whole file; an `Update File` cannot be reconstructed from a patch, so its post-write text stays unknown and the card contract does not judge it, the same way an `Edit` whose `old_string` is absent is not judged. This is what closed the Codex-shaped gap: this host's Codex has no `apply_patch` tool at all, it puts the envelope inside an `exec` call whose `tool_name` has already become `Bash` by the time `PreToolUse` sees it, which is why every `write_block` row in the real ledger came from Claude and none from Codex.
- **A file inside an envelope that cannot be judged** — an unresolvable path, added lines past `WRITE_GATE_MAX_CONTENT_BYTES`, or a malformed envelope with no `*** End Patch` — is left unknown while the other files in the same envelope are still judged. One undecidable file neither passes nor blocks the rest.
- **Content past `WRITE_GATE_MAX_CONTENT_BYTES` (256 KiB)**, an unreadable target file, and an oversized target all fail open rather than spend the hook's deadline.
- **Rule B judges the text, not the intent**: content that already FAILs stays writable if the write does not change that (an `Edit` whose post-write text cannot be determined is not judged), and a card that was already malformed is not repaired by the gate.

### Self-verification

```powershell
python adapters/claude/pretooluse_gate.py --selftest
python epitype/patch_envelope.py --selftest
python tests/patch_envelope_gate_regression.py --selftest
python epitype/card_lint.py "<vault>"
```

The second command is the population this rule will act on: every card it reports as FAIL today is a card whose next `Edit` is blocked unless the edit leaves it passing.

## 12. The assistant's own promise is the one thing nobody tracks

### Symptom

Mid-turn the assistant says「我等一下會把測試補上」or「等 verifier 回報後我會改」. The turn ends, compaction runs or the session is replaced, and the promise is gone from both sides: the model has no memory of making it, and the owner is the only party still holding the thread — so the owner has to chase it. Everything the owner said is captured (grants, corrections, rulings); the sentence the assistant volunteered is not.

### Why it happens

Every capture path in Epitype watches the owner's words, because that is where authority lives. But a to-do can also be opened by the assistant, and that one has no author to defend it: the model that made the promise is the same model whose context window is about to be discarded. Injection cannot help either — there is nothing on disk to inject.

### Epitype countermeasure

The `Stop` hook, after it has finished deciding whether to block the turn, reads the same last assistant message twice more: `commitments.settle` closes any open promise the message reports as finished, and `commitments.extract` + `record` writes the new ones. The ledger is `<governance vault>/.epitype/commitments.jsonl` (`ts`, `session`, `digest`, `text`, `status`) — deliberately not a card: this is not an owner to-do, it must not be recalled as memory, and `pending_lint` must never name it as a zombie owner item. `SessionStart` prints one line (`⏳ AI 未兌現承諾 N 條（最近：…）`) under the owner's pending line, including `source: compact`, which is exactly the moment the promise would otherwise evaporate; `PreCompact` appends the newest five open rows to the recovery map. The trigger table, the exclusions, and the digest dedupe all live in memspec's `COMMITMENT_*` block, so the hook and the CLI judge a sentence the same way.

Detection is a sentence-pattern table, not a model, so its two error directions are known and bounded:

- **False positives.** The assistant restating an owner instruction («owner 說我會…»), a conditional or hypothetical that happens to contain a trigger word, and a promise nested inside a longer clause are the shapes most likely to be mis-recorded. Quoted triggers, attribution phrases, questions, and completed forms are excluded, but a novel restatement shape will still land in the ledger. The cost is one line the owner can close with `--close`; the ledger is never treated as authority over what was actually agreed.
- **False negatives.** A promise with no trigger word at all ("補完測試再回報") is not recorded, and no pattern table will catch it. The ledger is therefore a floor, not a guarantee: it catches the phrasings that recur, and `CORE-0.3A` still binds the turn.
- **Settlement is heuristic.** A row closes when the message names its digest, or when the promise's first 20 characters appear in the part of the message that is *not* itself a commitment — restating a promise never closes it. A completion report that paraphrases instead of restating leaves the row open, which is the safe direction: a stale open row is visible, a wrongly closed one is not.

### Self-verification

```powershell
python epitype/commitments.py --selftest
python adapters/claude/stop_gate.py --selftest
python epitype/commitments.py "<governance vault>" --list
```

## 13. The tidy-up pass exists and never runs

### Symptom

The vault has an offline inventory command that names everything needing attention — cards failing their type contract, cards with no aliases, zombie pending lines, unsettled promises, unreviewed drafts, aging event cards. Nobody runs it. Months later the numbers are large enough that nobody wants to start, and the memory system is quietly degrading while every hook still reports green.

### Why it happens

The command is correct and the schedule is a person. A tool that must be remembered competes with the work it was supposed to protect, and it loses. The usual fix — "run it nightly" — assumes the operator's machine, timezone, and habits, which a shared repository cannot assume: a cron line installed on someone else's laptop is an unrequested background job.

### Epitype countermeasure

`dream.mode` decides who remembers. The default, `piggyback`, needs no scheduler and no habit: `SessionStart` compares `dream_state.json`'s last completion against `dream.interval_hours`, and when the gap is wide enough it starts `epitype/dream.py --scheduled` as a detached, low-priority process and returns immediately — the session never waits, and a token/PID lease protected by an OS guard (only an expired dead holder can be replaced after 30 minutes) keeps concurrent sessions from starting a second one. Operators who prefer a real schedule use `graft install --dream nightly [--at HH:MM]`, which registers one daily system task and stops the piggyback trigger so the same day is not swept twice; `graft doctor` prints the mode and last attempt completion and verifies the actual nightly command and time, and `graft uninstall` unregisters the task. `off` disables both.

The failure directions are bounded on purpose:

- **The dream must not become a second failure surface.** The background run reads vaults and writes only `<governance vault>/.epitype/` (pack, JSON pack, state, log). It gives itself ten minutes and marks the sections it did not reach; any exception lands in `dream.log`. A hook that cannot find a Python executable, or whose spawn fails, logs one line and skips — `SessionStart` injection is never affected.
- **It reports, it does not apply.** A pack is a review packet; every suggestion names the existing CLI command that would act on it, and no model is called. An installed Epitype never spends model budget on its own.
- **Announcing it must not become noise.** The next session prints exactly one line — the four headline numbers and the pack path — and marks it announced; a session resumed by compaction prints nothing, because it is not a new day. A dream that found nothing still prints a short line, since silence cannot be distinguished from a dream that never ran.

### Self-verification

```powershell
python epitype/dream.py --selftest
python adapters/claude/sessionstart_hook.py --selftest
python install/graft.py --selftest
python install/graft.py install --home <disposable home> --dream nightly --dry-run
```

## 14. 事件捕捉精準度：量測方法與已知盲點

### Symptom

The vault fills with grants, corrections and rulings the owner never meant as rules.
A ruling card is injected at the top of every prompt, so a wrong one is not dead
weight — it is contamination: a measured recall regression (48 → 47) traced back to
mis-captured ruling cards competing for the pinned seats.

### Why it happens

Capture used to ask one question per kind: does a trigger phrase appear anywhere in
the prompt? Hand-labelling 254 real event cards (176 replayed offline, 78 written
live) showed that question is wrong 57% of the time — only 110 cards carried the
right kind and only 100 were worth keeping. Eight shapes account for the misses:

1. a bare 附和 phrase (「依照你的建議處理」) read as an authorization — 18 cards;
2. a ruling created purely because the *assistant's* previous turn asked for a
   decision, whatever the owner then said — 27 of 94 ruling cards are pure questions;
3. a permission question (「你可以…嗎？」) read as permission granted;
4. the owner admitting their own mistake (「我說錯了」) read as a correction of the agent;
5. a request about communication style (「白話跟我說」) read as a ruling;
6. assistant prose the owner pasted back into the prompt read as the owner's words;
7. a scopeless one-word acknowledgement (「我同意」) pinned on every later recall;
8. an urging question (「怎麼還…？」) read as a correction.

### Epitype countermeasure

`capture.classify` is now the single judgment for the live hook, the offline replay
and the harness, and it works at clause level:

- `owner_reply` keeps only what follows the owner's last quote marker (`<-`/`<=`/`《`),
  so pasted assistant text cannot supply the trigger;
- `_segments` splits on 。！？；and newlines, drops interrogative clauses, and — because
  a real ruling often embeds its rhetorical question mid-sentence — re-splits a
  question clause on commas and drops only the interrogative half;
- `CAPTURE_VETO_REGEX` deletes hollow acknowledgement, self-blame, urging and
  style-request phrases from what remains, so they can never be the evidence;
- what survives must match `CAPTURE_DECISIVE_PATTERN` (or a standing-scope or trigger
  phrase) before any card is written;
- `looks_generated` rejects a candidate over 200 characters or with 3+ digits per
  20 characters — the measured shape of an assistant's own analysis;
- a ruling no longer stands on the assistant's question alone: the owner's sentence
  must itself be decisive, *and* either the assistant did ask for a decision or the
  sentence carries standing scope (以後／一律／不用問我) or several decisive clauses.

`tests/capture_precision.py` is the measuring台: `--selftest` runs 33 synthetic checks
(two counter-examples per failure shape, four multi-clause true rulings, three positives
per kind), `--local <json>` scores a labelled real-sentence set that lives outside the
repo. Measured on the 254-card set: precision (right kind among captured) 0.436 → 0.807,
retention of the 100 keep-worthy cards 0.91 → 0.85.

### 已知盲點

- **Retention, not true recall.** Every row in the labelled set is a card the *old*
  rules captured, so the set contains no example of a sentence the old rules missed.
  The reported recall is retention of previously-captured keepers; it cannot detect a
  shape neither judgment ever saw.
- **The assistant's question is only stored on ruling cards.** A grant or correction
  card keeps the owner sentence alone, so on replay those rows can only reach the
  standing-scope route. Their measured recall is a lower bound.
- **correction ⇄ ruling is a genuinely soft boundary.** 8 of the remaining errors are
  sentences that carry an explicit correction trigger (我說過／不要亂) but were labelled
  rulings because they read as standing decisions. No lexical rule fully separates them;
  correction now yields to ruling only when the assistant's prior turn actually asked
  for a decision (`ruling_question` hits) and `ruling_body` accepts the owner's reply —
  with no question pending, correction still wins so a proactive「我說過」is never
  demoted (2026-09-06 measurement: fixes 1 of the 8, trades it for 1 new miss on an
  unrelated correction that happened to sit beside an unrelated question — precision
  and retention held at 0.807/0.760, did not improve).
- **A one-off order that looks like a rule still gets in.** 「不要再讀檔、不要呼叫工具」
  is scoped to one turn but is lexically indistinguishable from a standing rule.
- **Value is not kind.** 0.71 of captured cards are worth keeping long-term; a
  correctly-kinded one-off correction is still noise on recall. Card triage, not
  capture, is the place to fix that.

### Self-verification

```powershell
python tests/capture_precision.py --selftest
python tests/capture_precision.py --local <labelled set outside the repo>
python epitype/harvest.py --reevaluate <quarantine directory>
```

## 15. Token 洞：泛詞查詢、開場清單、承諾誤抓

### Symptom

三個地方各自把 context 花在沒有資訊的位元組上，量到才看得見：

- 一句與記憶完全無關的「今天天氣如何」注入 2007 bytes／7 張卡，命中的全是「今天」
  「天天」這類泛詞碰到卡片正文；24 句無關日常問話裡有 15 句都有注入。
- 每一場開場列出 12 條現行裁定，每條帶完整 owner 原話＝1977 bytes。同一句原話在喚回
  命中那張卡時本來就會再送一次。
- 承諾帳本累到 23 條 open，其中 6 條是過程旁白（「Private list: (1) the verifier's
  background pytest…」「這兩個跑完我會確認…」），開場那行的數字因此變成背景噪音。

### Why it happens

中文切詞在沒有詞典的情況下只能滑動取二元組，一半的二元組跨詞界（「馬拉松|前一天」
切出「松前」「前一」）；再加上「命中」被定義成子字串包含，於是任何一句話都碰得到
幾張卡，而檢索器沒有辦法區分「碰到」與「問的是這件事」。開場清單與承諾帳本則是同一
個形狀的另一面：兩者都只進不出，沒有人為「這條還值不值得每場都送」定過條件。

### Epitype countermeasure

- 查詢端把泛詞排除在「命中」之外（`memspec.RECALL_GENERIC_TERMS`：時間詞、量詞、
  填充詞、英文虛詞；裸數字不入切詞），一句話若沒有任何實詞命中就整份不注入；單一
  中文二元組只認卡的身分欄（name／aliases），碰到描述或本文要第二個實詞背書。
  單一英文詞比照辦理，但只收在≤3字母（原因見下方 Known limits）：碰到 body 不算，
  也要身分欄或第二個實詞背書。
- 英文詞 ≤3 字母只認詞尾邊界（`memsearch._short_latin_pattern`：`term + \b`），
  不再任何位置的子字串都算；≥4 字母維持子字串比對，規格未變。
- 開場一條裁定只列 `decision_key｜日期`，並只列近 `SESSIONSTART_DECISION_RECENT_DAYS`
  天的、或帶 `forbidden`（會擋人）的那些，其餘用一行說還有幾條與看全部的命令。
- 承諾只從訊息結尾那一段抽（過程段不算）、執行旁白詞（`COMMITMENT_NOISE_PATTERN`）
  一律不算承諾、一回合最多 `COMMITMENT_MAX_PER_TURN` 條、open 超過
  `COMMITMENT_STALE_DAYS` 天自動標 expired 且不再計數。

### Known limits

- **詞面碰撞不是切詞錯誤。** 「熱帶魚缸的水草照明週期」碰到「週期」、「台北到高雄」
  碰到「台北」、「what is the capital of Portugal」碰到 capital——這些詞在該庫的別名裡
  是本業詞彙。無關問句題庫 20 題到 16 題棄答，剩下 4 題全是這一類；修法在語意層
  （向量或模型），不在查詢端。
- **短拉丁詞（≤3 字母）2026-09-06 改認詞尾邊界，字首巧合已擋。** `bug\b` 吃得到
  debug 的字尾（真字根，回歸題庫 titan-log-over-screenshot 要的那個命中）；
  `tie\b` 吃不到 tier／tiered／service_tier 的字首（純巧合）——「how do I tie a bow
  tie」注入從 2073 bytes／27 張候選卡歸零。同一道身分欄門檻（單一實詞只碰 body 不
  算）也收進≤3 字母這批，理由：試過推廣到所有英文實詞（不分長短），
  `memsearch --selftest` 的「body-only 真命中」案例（`ordinarynostatusneedle`）直接
  斷言失敗——那是既有正確行為（獨特詞只出現在 body 也該找得到），跟 capital 撞見
  高頻多義詞是兩回事，光靠字數或身分欄分不出來。**capital 因此留在原地**：診斷視窗
  （200 候選）仍有 ~24 張，但真機 `recall_hook.py` 的 `FTS_TOP_K=5` 頂帽（跟這次修的
  洞無關、原本就有）本來就把實際注入壓到 5 張。
- **開場是預算飽和的。** 開場注入本來就頂到 `budget_bytes`，所以裁定清單省下的
  1.6 KB 不會讓總位元組變小，而是把原本被丟掉的帳本／索引段落換進來（實測掉段
  42→35、行數 61→103）。要讓總量下降只能調 `budget_bytes`。
- **既有帳本不會自己重評。** 規則改了之後舊列還在，`--requalify --dry-run` 印每條
  keep/drop 供人決定；結尾段那條規則無法回溯（原始訊息已經不在）。

### Self-verification

```powershell
python epitype/memsearch.py --selftest
python adapters/claude/sessionstart_hook.py --selftest
python epitype/commitments.py --selftest
python tests/recall_regression.py --selftest
python epitype/commitments.py "<vault>" --requalify --dry-run
```

## 16. U64：引用禁詞當證據，被當成又提議一次

### Symptom

owner 要 Stop 閘的「閘門實測表」報告，表格裡引用一句已被判 `forbidden` 的話當測試
案例（例：一格寫著「要不要我修復這個錯誤」→ 擋下）。這段報告本身沒有再提議任何事，
卻被 Stop 閘判定成模型又把已裁定的事端回去，整份報告被擋下。同一天，為同一條
`forbidden` 寫驗證腳本時，腳本裡定義該禁詞的字串常值也被寫檔閘規則 A 擋下——只能
用字串拼接繞過，寫不出一份直接了當的腳本。

### Why it happens

`stop_gate._forbidden_fragment`（§10）與寫檔閘規則 A（§11）都只是對整段文字跑
`forbidden` 正則，命中就擋；正則不知道命中的字落在引號裡還是裸露在外，「引用一句
被否決的話當證據」與「把被否決的話當提議再講一次」在字面上是同一件事。兩道閘共用
同一個 `_forbidden_fragment`，這個盲點兩邊都有。

### Epitype countermeasure

`_forbidden_fragment` 在跑 `forbidden` 正則之前，先算出訊息裡「引用區段」的字元範圍
（`stop_gate._quoted_spans`）：owner 既有的 `memspec.RULING_QUOTED_TEXT_PATTERN`
（中文引號「」『』、直角＋彎雙引號、彎單引號、單行反引號與```圍籬```）,另加 Markdown
引用行（開頭 `>`，整行算引用）。命中的字若整段落在某個引用區段內，不算再提議；只要
有一部分落在區段外，仍然照擋——同一則訊息裡引用一次、另一處又裸提一次，裸的那次
一樣擋。寫檔閘規則 A 經同一個函式，兩邊同一處修好；規則 A「只豁免定義該裁定的卡
本身」（§10 末段、U63）完全沒動，引用豁免與那條豁免各自成立、互不放寬。

ASCII 直引號 `'…'` 沿用 `RULING_QUOTED_TEXT_PATTERN` 既有的排除，不在此重新收錄：
英文縮寫 don't 的單一撇號會配對出假引號區間（2026-09-03 對抗審查 #5 的教訓），把它
納入等於在寫檔閘（腳本語言常見單引號字串）重新踩一次同一個地雷。啟用旗標
`memspec.STOP_GATE_QUOTE_MASK_ENABLED` 留給 owner 一鍵關閉。

**2026-09-17 收窄與已知殘留。** 這道豁免的開關握在被管制的那一方手上：把整段主張包
進「」、直引號、反引號或 `>` 引用行，四種寫法實測 4/4 全部靜音。加了兩道上限——遮罩
佔整則超過 `STOP_GATE_QUOTE_MASK_MAX_SHARE`（0.5）就整則不遮，單一引用區段超過
`STOP_GATE_QUOTE_SPAN_MAX_CHARS`（120 字）也不遮，因為引用是一句話、不是一段主張。

**殘留仍在，而且修不掉**：一句短的違規話加上引號，與一次正當的引用，在字面上完全
一樣；比例那一道還可以靠多寫二十個字壓下去（實測）。所以第三道不是再收窄，是**留
痕**：遮罩放過的豁免寫一列 `memspec.STOP_GATE_MASKED_LOG_KIND`（卡名、命中片段、訊息
指紋，內容本身不記），夜間第 13 節把它數成 `quoted_exemptions`。**粒度是一則訊息對一
張卡一列**，不是每個命中片段各一列：同一張卡有三條樣式在同一則訊息裡各命中一次，被繞
過的仍然只是那一則訊息，記三列會把數字灌大。所以這個數字讀作「有幾次某張卡本來會擋、
結果沒擋」，不是「有幾個片段被遮掉」。夜間重放
看不到這些——它跟閘用同一套遮罩，在它眼裡那根本不算命中——所以這個數字只能從閘自己
的稽核列來。以前繞過去之後，閘、稽核、夜報三個地方同時看不到；現在分不出來的東西
至少數得出來。

### Self-verification

```powershell
python adapters/claude/stop_gate.py --selftest
python adapters/claude/pretooluse_gate.py --selftest
python epitype/memspec.py --selftest
```

## 17. 捕捉落點：專案的進專案庫

### Symptom

owner 在某個專案的對話裡下裁定、做糾正——話裡就明講了那個專案的名字——自動捕捉卻把卡
寫進通用（治理）庫。結果是：在那個專案裡開新場時，屬於它的裁定不在它的庫裡；而通用庫
被別的專案的細節塞滿，喚回時每一則都要先判「這條是不是在講我現在這個專案」。
2026-09-06 實測：治理庫 132 張自動捕捉的事件卡裡，74 張的 `cwd` 指向另一個已登記的專案庫。

### Why it happens

落點與「哪個庫負責治理」被當成同一個問題。捕捉端只知道一個答案——治理庫（帳本持有者）
——因為那是 hook 唯一算過的庫；`cwd` 雖然一直被寫進卡的 frontmatter，卻沒有任何一段
程式讀它來決定要寫哪裡。喚回端相反：它早就會把 cwd 對應的原生專案庫排在最前面，所以
「讀得到專案庫、卻永遠不往專案庫寫」這個不對稱可以長期存在而不報錯。

### Epitype countermeasure

落點規則集中在 `epitype/capture_route.py`，線上 hook（`_hook_common.capture_vault`）、
離線回放（`epitype/harvest.py`）與稽核工具共用同一份：`cwd` 自己或它的任一層祖先若對應
到**已登記**的原生記憶庫（已有索引或至少一張卡，`holds_cards`），卡就落最相關（最深）的
那一個；都沒有才落治理庫。宿主替每個 cwd 開的空目錄不算庫——往空殼寫第一張卡等於替
owner 決定在那裡開庫，所以未登記一律退回治理庫，並在卡上留下 `cwd`（`memspec.CWD_FIELD`）
供事後歸戶。Codex 的 `cwd` 只在開場的 `session_meta` 出現一次，回放時補到每一筆紀錄上，
否則那批卡沒有來源專案可判（實測 40 張如此）。

跨專案通用的長效規則仍該進治理庫，但那是人立卡時的判斷，自動捕捉判不了；所以自動捕捉
一律照 cwd 落點，通用化留給人。既有的誤置卡不由捕捉端搬：
`python epitype/capture_route.py <vault> --audit` 唯讀列出 `MISROUTED <卡> -> <庫>` 與統計，
`--apply` 先產生完整歸戶註記，再發布到未占用的檔名；同名加 `-2`，發布時仍拒絕競態覆蓋。
失敗保留原件；原名已被其他寫者使用而無法復原時，錯誤訊息指出 recovery 檔位置。
成功後兩邊的索引都標舊讓下一個讀者重建。

卡上的 `scope: governance-core` 沒有跟著改：那是索引裡的搜尋欄位，改它等於新增一套
scope 詞彙，超出本次修正的範圍——落點的證據看 `cwd`，不看 `scope`。

### Self-verification

```powershell
python epitype/capture_route.py --selftest
python adapters/claude/recall_hook.py --selftest
python epitype/harvest.py --selftest
```

## 18. Governance boundaries: authority, stale hits, delivery and unavailable vaults

Synthetic regressions cover four gaps in the shared hook paths:

- **Authority:** Stop previously discarded `decided_by` and could describe an AI
  decision as the owner's settled ruling. Cached decisions now retain the source;
  only `owner-explicit` with a nonempty source quote blocks re-asking. Other
  decision sources retain their existing forbidden-pattern checks. The cache
  version changes so old, source-less records are not reused.
- **Freshness:** an indexed-active card that is retired or unreadable on disk
  must be dropped, not downgraded into ordinary recall or a captured-event pin.
  This checks the candidate card without forcing a whole-vault index rebuild.
- **Delivery:** recall prepares output without consuming dedupe markers. Only
  card lines actually included in successfully flushed output are marked;
  budget-omitted lines, deadline drops and output errors remain retryable.
  A crash between output and marking can repeat context. A successful flush
  means output was written, not that the host or model acknowledged it.
- **Availability:** an unavailable configured vault is retained in configuration
  identity but excluded from recall's readable set. The hook reports degradation.
  Since that vault may have held the governance ledger, capture, commitment/audit
  writes, recovery-map writes and piggyback-dream writes must not select a new
  destination implicitly. Those writes pause until the configured set is available.
  Compaction clears recall dedupe even when its recovery-map write is unavailable.

`python tests/governance_regression.py --selftest` covers these synthetic cases;
`python tests/run_all.py --jobs 2` includes the existing adapter and installer
regressions. These checks do not establish live model compliance in either host.

## 19. Recall slots consumed before dedupe, or monopolized by the first vault

Applying the global eight-card cap before checking delivery markers stranded
unseen candidates. Concatenating each vault's top five also gave earlier vaults
the ordinary slots regardless of query evidence. A long ordinary line could
then stop a prefix-only byte pack even when a later whole card would fit.

Recall now deduplicates before assigning output slots. Ordinary candidates are
merged at each vault's frontier by matched non-generic query-term count, then
local rank and vault order; independent BM25 values are never compared across
databases. Each vault's original order, five-candidate cap, body-only cap and
decision/correction priority remain intact. This is lexical evidence, not a
semantic relevance guarantee, and it does not widen the search window.

Only ordinary lines may be skipped for size. An unfit authority-prefix line
stops selection; output never bypasses it with a cheaper ordinary card. Cards
omitted by size or slot caps remain unmarked and eligible on a later turn.
Eight cards, the configured byte budget, JSON-envelope ceiling and hook
deadline still apply. This bounds each injection, not total session tokens:
previously stranded cards can now be delivered on later turns.

`python tests/recall_selection_regression.py --selftest` checks the real
shared recall path with synthetic homes and vaults, plus packing boundaries.
These offline checks do not establish improvements in unrestricted dialogue.

## 20. Questions can assert unsupported premises

> Superseded by §30 (owner ruling 2026-09-09): the pre-generation question procedure was removed from the product.

An assistant can ask the user to choose details of a combined workflow before
checking that its components actually connect. Earlier searches, individual
API documentation, citations, and an assistant's own `verified` flag do not
establish that integration. Conversely, unimplemented does not mean infeasible:
a clearly hypothetical feature and its development investment can be discussed.

Lexical recall searches the user's prompt, not every question the assistant
will later invent. Relevant feedback cards can therefore be absent even when
they exist. The existing Stop and PreToolUse rules check particular prohibited
actions or settled decisions; they do not judge arbitrary evidence entailment.

The shared procedure is `memspec.QUESTION_PREFLIGHT`. It is reserved before
recalled cards and delivered **once per session** (see §28, owner 2026-09-09):
every SessionStart restores it before generation, including startup, resume and
compact, and claims the session marker; a UserPromptSubmit sends it only while
no marker exists (first prompt, or after PreCompact cleared the markers), and
otherwise omits it. Without a session id it remains per prompt. App-originated
continuation may arrive through SessionStart without an observed
UserPromptSubmit, so compact-only coverage is insufficient.
It requires
the answering model to retrieve accessible facts, check the exact premise and
integration path, distinguish tested behavior / concrete development route /
unknown or test-needed behavior, and disclose gaps before dependent choices.
Preferences, tradeoffs, necessary authorization, owner-only information and
explicit hypothetical designs remain legitimate questions. Quoted failures
and discussion of rules are not outgoing questions.

The procedure also covers option labels and descriptions: a caveated stem does
not justify omitting a missing integration step or asserting lower cost, latency
or implementation state without evidence. Missing tests do not establish missing
implementation. Recommendations must leave a requested user choice intact.

This is **pre-generation guidance, not a semantic interception gate**. No new
question-word classifier, evidence certificate or model-per-question call is
added. Raw unsupported questions still pass the existing gates. Stop happens
after a response and is not proof of preventing display. Host tool-event support
and actual App behavior must be verified separately from adapter subprocesses.
Sending a message into an already loaded App conversation may produce neither
entry event; this change does not force host events or retroactively inject
into such a continuation. Existing trusted hook registration is required.

The question procedure alone costs 1,372 UTF-8 bytes on each delivered prompt
hook and session-entry hook (the continuity procedure in §21 adds to this);
no-hit turns were previously empty. The same raw-context and JSON-envelope caps
apply to the combined output, leaving less room for recall cards; omitted cards
remain eligible. An undersized budget warns and omits the complete procedure
rather than truncating it. Existing timeout/configuration fail-open behavior
and host-side truncation can still prevent delivery. Token and task-quality
improvement are not implied by byte counts or successful delivery.

`python tests/question_premise_regression.py --selftest` checks twelve shared
delivery, budget, session entry and non-denial contracts. It deliberately does not label
evidence support as mechanically verified. The synthetic evaluation protocol in
`docs/QUESTION_PREMISE_VALIDATION.md` separates actual retrieval, model judgment,
normal questions and native App coverage. Private incident transcripts stay
outside the repository.

A bounded native App check observed the full procedure before actual reads,
ordinary text and a question-tool call. An option still omitted a required
receiver and claimed lowest cost. Refining the procedure corrected those parts
and restored a skipped investment choice, but other answers still equated
untested with unimplemented and called existing components tested. Behavioral
acceptance therefore remained **FAIL**, despite passing delivery regressions.
No semantic error was mechanically blocked before display. Preserve such
failures instead of reporting hook delivery or a better example as sign-off.

A further bounded native run used separate implementation and validation axes.
It handled new contrasts but repeated failures on existing cases: existence
became tested behavior, and absent tests became absent implementation. That
increment was withdrawn; the prior procedure remains, with no claimed semantic
acceptance. The native run also preserved normal preferences, hypothetical
investment, owner-only information, authorization and actual configuration reads.
Those successes do not cancel the failures or establish unrestricted coverage.

Evidence misuse also precedes questions: a truncated directory listing is not
proof of global absence; an old provisional choice is not a current final
decision; relative improvement is not an absolute positive outcome. A new card
can perpetuate those errors. Check the named source, later corrections and the
exact metric/comparison before treating a recalled conclusion as established.
These distinctions remain model judgments, not new lexical denial rules.

## 21. A follow-up can silently replace the active task

> Superseded by §30 (owner ruling 2026-09-09): the task-continuity procedure was removed from the product.

An assistant may save one design decision and stop, or answer a diagnostic
aside and discard an already-authorized repair. It can also explicitly admit
that implementation is unfinished while ending the turn. Neither a completed
turn nor a saved checkpoint proves that the requested scope is complete.

Existing Stop rules only enforce selected settled decisions. The commitments
ledger records promises; it is not an active-task completion judge. A global
pending list cannot identify which work belongs to this conversation. Raising
a host's stop cap or parsing private native-goal transcript formats would not
repair that missing semantic distinction.

`memspec.TURN_CONTINUITY` now runs as a procedure for the answering model,
delivered by the existing UserPromptSubmit and SessionStart paths alongside
the question procedure. It requires recovering the active scope,
remaining deliverables and still-valid authorization **before** interpreting
a follow-up. A confirmation, status question or related bug report does not
erase that scope. Answer an aside and take the next safe authorized step in
the same turn; before ending, compare the actual results to the whole task.

Genuine owner choices, owner-only information, necessary authorization and
verified external gates with no independent work remain valid waiting points.
Explicit pauses or scope changes win; standalone analysis stays analysis.
Unrelated ledger items and quoted failure examples do not authorize new work.

This is **not an automatic unfinished-work detector or a new Stop block**.
The model can still misjudge scope, ignore the procedure or overclaim evidence.
No self-certified completion field, question-word gate, native-goal parser or
per-turn model call is added. Stop's existing loop brake is unchanged; ordinary
questions, explanations and valid final answers acquire no new mechanical denial.

The shared packing helper reserves whole procedures within the existing raw
and JSON budgets. The new part costs 1,055 additional UTF-8 bytes including
its separator, 2,427 bytes total with the current question procedure. If only the
question procedure fits it remains intact, and omitted continuity is reported
on stderr; neither procedure is cut mid-sentence. This reduces room for cards,
not the eight-card or time limits. A warm App continuation without an entry
event receives no fresh procedure. CLI, installed-shim and native App results
must be reported separately; see [continuity validation](TASK_CONTINUITY_VALIDATION.md).

## 22. Regression markers can leak across test runs

Isolating HOME does not isolate `tempfile.gettempdir()`. Fixed synthetic session
ids therefore reused persistent Stop dedupe markers, suppressing the expected
first block and producing false test failures. `stop_freshness_regression`
now binds its marker-directory function to each test's temporary root. It
retains the real claiming logic, creates no new runtime behavior and does not
delete real host markers. Two consecutive focused runs and the full suite
passed; that result is test isolation, not evidence of semantic enforcement.

## 23. Recovery can lose or misattribute original messages

The old compact-map decoder ignored human queued-command attachments, accepted
host-generated compact summaries as user messages, and joined assistant records
under the last physical line number. Clipped user text was not marked. An agent
following that map could miss a later instruction or quote the wrong source.
These are demonstrated parser defects, not proof that compaction caused every
historical wrong answer.

`transcript.source_record` now distinguishes host user records (U), human queue
records (Q) and assistant records (A), excludes meta/compact summaries and agent
queues, and supports Codex response-item messages without duplicating event
streams. Compact maps retain one pointer per physical record and mark clipped
message text. The proposal-only scar scanner uses the same source decoder.
Source identity does not make quoted instructions authoritative.

The map routes back to the read-only `source` command. Each lookup uses one
explicit JSONL file, at most 64 MiB and a three-second decoding budget, retaining
up to eight latest matches in source order. It exposes the snapshot, physical
line/byte position, row hash, unreadable rows, omitted matches, changed-file and
incomplete-scan status. Preview text is capped at 800 characters and output at
16 KiB; truncation requires reading the original line. Offset-based line numbers
are explicitly relative. There is no global search, semantic matcher, automatic
link following, model call, new daemon or memory write.

The core recovery map remains capped at 2 KiB from a 4 MiB tail; its larger header
leaves less room for excerpts. The existing host adapter can append a separate
open-commitments snapshot, so the final on-disk file can exceed that core cap.
Neither a full byte scan nor a matching hash
proves complete historical coverage, current authority or semantic entailment.
Tool results are deliberately excluded from message lookup: inspect the named
artifact or original tool record when that is the evidence. Filesystem I/O is
not covered by the decoding deadline. Existing host permission gates still apply.

Synthetic provenance regressions initially failed seven of eight checks;
after repair, provenance and lookup regressions each passed nine checks, and
the complete runner passed 47/47. Existing recall@8 remains 45/51. These numbers
do not cancel the native semantic failures recorded in section 20.

A subsequent native Claude App Code batch actually read the map, original
messages, both reports and configuration; it preserved normal questions and
continued after the operator's answers. It still inferred absent implementation
from absent sync tests in an explanatory answer: **behavioral FAIL**. The new
lookup also ran successfully from the native App in a separate instrumentation
check. Neither that command nor the successful source/metric answers corrected
the unsupported prose before display. No prompt retuning or semantic judge was
added after those outputs.

Native test operators must also mark non-owner messages structurally. A short
untagged synthetic probe was captured as an owner ruling; a natural-language
disclaimer did not stop lexical capture. The existing leading-tag exclusion
avoids capture of explicit operator blocks while retaining ordinary guidance and
read-only tools. This is harness isolation, not proof that all impersonated or
hypothetical authority can be recognized. Supersede the mistaken capture rather
than deleting its evidence or treating it as a real owner decision.

## 24. Git option suffixes and diagnostic patterns mistaken for operations

**Historical.** The mechanism this section repairs was removed on 2026-09-09
(§34); the incident is kept because it is the evidence for that removal, and
because the same trap waits for anyone who tries to classify a command with a
regular expression. `tests/git_gate_regression.py` retired with the mechanism.

A scar matching arbitrary text between `git` and `checkout` also matched the
clone flag `--no-checkout`. When unsupported PowerShell syntax triggered the
adapter's conservative full-text fallback, a quoted diagnostic regex matched
itself too. The portable destructive-Git example now requires complete command
and operation boundaries while retaining case, quoted-executable and serialized
newline handling. The same rule can update an existing local scar; adding the
example to the repository does not automatically overwrite an installed card.

Regression inputs go through the real adapter with a temporary vault; dangerous
commands are never executed. Preserve direct, chained, wrapper and unsupported
dynamic-execution denials, and keep full-text fallback/auditing. This is a narrow
lexical repair, not a complete shell interpreter: unsupported scripts quoting
actual dangerous commands may still be conservatively blocked. Source recovery,
rollback proof and native question-premise acceptance remain separate results.

## 25. Captured conditions silently shortened into apparent standing authority

The capture writer shortened descriptions to 80 characters; recall shortened them
again and labeled captured rulings as owner decisions. A stored passage's trailing
restriction could disappear although it remained in the card body. Historical
utterances also lost their original project/session context at delivery.

New descriptions retain the captured passage. Auto-captured hits now read the
original card in one bounded 16KiB snapshot and show historical-capture status,
the recorded timestamp/project/session and a resolvable card path. Body previews
over 800 characters explicitly require reading the rest. Missing, malformed or
oversized sources produce an explicit lookup limitation, not an index summary
presented as checked original text. The entire line remains indivisible under
the existing context/JSON budget and delivery-marker rules.

Curated active decisions still precede captures; classification, expiry and
supersession rules are unchanged. Captures may contain one sentence or a labeled
question/answer pair, not the complete conversation. A project/session identifies
the origin, not permanent scope or fresh authorization. Reading the source and
determining its current meaning remain the agent's responsibility.

Seven synthetic regressions cover legacy and new captures, a multiline grant,
missing provenance, read failure, preview clipping and budget omission; the full
runner passes 49/49. The installed shim also delivered the repaired content from
an isolated synthetic home. These are pipeline checks, not proof of arbitrary
model inference or native App acceptance. A separate native Claude App check
verified delivery through a normal-text UserPromptSubmit and a response that
distinguished historical origin from standing authorization, without fallback
tool reads. This bounded check does not resolve other recorded semantic failures.
No extra runtime model, database migration, vault-wide scan or historical-card
rewrite was added.

## 26. Control cleanup rules recalled in discussion but absent at tool use

> Superseded by §30 (owner ruling 2026-09-09): the control-lifecycle guidance was removed from the product.

A short memory-card description can omit the operational distinction between
closing a target window, closing browser tabs/groups and ending the controller.
Prompt-based recall also need not fire when the agent independently chooses a
control tool. `PreToolUse` now supplies an indivisible 696-byte lifecycle guide
for the node/CUA REPL tool identities and Claude's Chrome, built-in browser and
computer-use tool families. Ordinary file/shell calls and quoted tool names do
not select it. Generic REPL advice is conditional: not all JavaScript uses UI.

The guide requires necessary use, recording created resource IDs, immediate
cleanup, preserving pre-existing resources and mixed-group tabs, separate
controller shutdown, actual-result verification and respect for interruptions.
Existing scar/write denials take precedence. There is no new permission decision,
event registration, resource mutation, persistent tracker or runtime model call.
Each matching call adds the guide; it is omitted whole with a diagnostic when the
configured budget cannot hold it. It is not deduplicated across control calls.

This is tool-stage instruction delivery, not automatic closure or a semantic
necessity/ownership gate. An already-selected tool call is not cancelled by this
advice. The model must apply the procedure and inspect actual results. The
[Codex hook interface](https://learn.chatgpt.com/docs/hooks) and
[Claude hook interface](https://code.claude.com/docs/en/hooks) distinguish
tool input from post-execution output; this repair does not register a result
observer or treat an issued close/reset request as proof of success. Unknown
tool families, UI actions outside hooks, CLI browser commands and host security
interruptions remain outside this delivery check.

Seven regressions cover complete/repeated delivery, conditional non-UI use,
nonmatches, existing-deny priority, budget omission and fail-open boundaries.
An installed-shim check used an isolated home. Native Codex delivered the guide
on actual REPL calls. A separate native Claude App probe used Chrome tools:
create a session-owned new tab/group, close the returned tab ID, then confirm
the group no longer exists. Hook and tool-result records substantiate that
bounded cleanup path. The fixture opened the browser's new-tab page rather than
the requested `about:blank`; an initial create-before-group error is retained.
Neither this instructed example nor fixture tests measure autonomous compliance
rates, mixed-group cleanup or every supported tool family.

## 27. Installation selftest copied a sibling test's transient directory

Parallel hook-trust tests create and remove repo-local `.hook-trust-*` directories.
The installation selftest copied the repo without excluding that prefix, so a
directory could disappear during `copytree`. Its fixture now excludes this owned
transient prefix; a new assertion verifies that real source names remain included.
Runtime installation behavior is unchanged.

The initial full run passed 50/50. A later run failed 49/50 on the copy race.
After the fixture repair, all 41 installation assertions passed but the old
hard-coded denominator of 40 still failed that suite. The denominator was fixed;
the final isolated installation run passed 41/41, while the other 49 suites had
already passed on the final source. This is composite passing coverage of all
50 suites, not a newly observed single all-green invocation. Both failed full
runs are retained; no repeated full run was added for the count-only correction.

## 28. The pre-generation procedure was re-sent on every prompt

> Superseded by §30 (owner ruling 2026-09-09): the procedure and its once-per-session marker was removed from the product.

`QUESTION_PREFLIGHT` + `TURN_CONTINUITY` total 2,427 characters. Until
2026-09-09 every valid UserPromptSubmit re-sent both, in addition to
SessionStart, so a 50-prompt session paid roughly 35,000 tokens for text that
never changed. The owner measured it in the universal-vault tidy of 2026-09-09
and chose option B: **once per session, again after compaction.**

Mechanism: SessionStart still emits the procedure first and, after a successful
emission, claims a per-session marker `guide-<sha256[:24]>` in the recall marker
directory. UserPromptSubmit omits the procedure while that marker exists and
otherwise sends it and claims the marker after emission (a failed output never
consumes the send). PreCompact already clears the session's markers, so the
procedure returns on the first prompt after compaction. Events without a
session id keep the old per-prompt behavior because no marker can exist.

Not changed: the procedure text, its budget priority (never evicted by cards),
the SessionStart coverage of startup/resume/clear/compact, and card dedupe.
Regressions: `question_premise_regression` (repeat turn omits until markers are
cleared; session entry claims for following prompts), `governance_regression`
and `recall_selection_regression` (a fully delivered session emits nothing),
recall selftest "same-session deduplication". Gates after the change:
run_all 50/50, privacy PASS, corpus 330/330, seeds 15/15 and 5/5.

## 29. SessionStart echoed an index the host had already loaded

Claude Code loads the cwd slug's `MEMORY.md` into context on its own. SessionStart
also injected a slimmed copy of that same index (up to 3 KB), so every Claude
session carried it twice; the copy also crowded the ledger out of the 10 KB
budget (`…超出預算，餘 N 段未注入`). Codex has no native index load, so for it the
echo is the only index.

Owner 2026-09-09: skip the echo on Claude. The host is recognised by what Claude
Code alone sends — a `transcript_path` under a `.claude` directory. Only the
vault of the exact cwd slug is skipped, because that is the one Claude loads;
ancestor-directory vaults and the governance vault are still echoed, and the
ledger is unchanged. Events without a `.claude` transcript (Codex, synthetic)
keep the old behaviour. Selftest: "Claude host skips the natively loaded cwd
index, keeps governance index and ledger" (sessionstart 29/29).

## 30. Behavior-layer features removed by owner ruling (2026-09-09)

Four features told the model how to behave. None of them was memory, and the
owner removed all four on 2026-09-09, answering each question in turn:

| Q | Feature | Owner's word |
|---|---|---|
| Q1 | The pre-generation procedures (`QUESTION_PREFLIGHT` + `TURN_CONTINUITY`) leave the product; the local rules stay in the contract and the behaviour layer runs on cards plus exam questions | 「A」 |
| Q2 | The control-lifecycle cleanup rules leave the product; locally the card `feedback_close_programs_when_done` carries them | 「A」 |
| Q3 | The commitment ledger (Stop extraction plus the SessionStart / PreCompact reminders) leaves the product; unfinished work goes on pending cards and in the handover file | 「A」 |
| Q6 | The narration meter leaves the product | 「A」, with the follow-up question 「這不是超級浪費 token 行為，為什麼要這樣做」 |

**Root cause**, conceded by the AI when the owner pressed on Q6: every owner
correction was reflexively turned into a product mechanism; the token cost of
the mechanism itself was never counted, and the 2026-09-02 ruling that the
behaviour layer runs on cards plus exam questions was never checked against.
The standing rule that follows: **Epitype does memory only** — cards, fields,
views, recall, and gates that rule on what a card says — and ships no built-in
behavioural guidance text of its own.

### Removed

| Kind | Item |
|---|---|
| module | `epitype/control_lifecycle.py` |
| module | `epitype/commitments.py` |
| module | `epitype/narration_meter.py` |
| constants | `memspec.QUESTION_PREFLIGHT`, `memspec.TURN_CONTINUITY`, the whole `COMMITMENT_*` block, `NARRATION_*` (the marker constants survive as `NOTICE_MARKER_*`) |
| helper | `_hook_common.pre_generation_guide`, `_hook_common.guide_marker_digest`, `sessionstart_hook._claim_guide`, the recall hook's guide gate and `_bounded_recall(prefix=)` |
| hook wiring | `pretooluse_gate._narration_context` and its `control_lifecycle.guidance` call; `stop_gate._commitments`; the SessionStart commitment line; the PreCompact commitment snapshot; dream section 4 (sections 5-8 renumber to 4-7) |
| CLI | `epitype commitments`, `epitype narration` |
| test | `tests/control_lifecycle_regression.py` (retired: the feature it validated is gone) |
| test | `tests/commitment_persistence_regression.py` (retired: same) |
| test | `tests/question_premise_regression.py` (retired; its two negative guards — no denial at a question tool, no Stop block on completion wording — continue as `tests/no_semantic_gate_regression.py`) |
| doc | `docs/QUESTION_PREMISE_VALIDATION.md`, `docs/TASK_CONTINUITY_VALIDATION.md` |

**Exam questions retired: none.** Every graded corpus was scanned for questions
depending on the removed features (`corpus_300.json`, both 2026-09-02 seed sets,
`recall_regression_local_20260906.json`, `recall_irrelevant_local_20260906.json`,
`capture_precision_local.json`), and no question exercised narration, commitments,
control lifecycle, preflight or continuity. The six `capture_precision_local.json`
rows whose owner text happens to contain those words test capture labelling, not
the removed features, so they stay. Denominators are therefore unchanged: corpus
330/330, seeds 15/15 and 5/5.

### Kept

The Stop decision gate (`decisions` / `forbidden`), the write-content gate, scar
triggers, recall, the SessionStart index / ledger / rulings / pending lines,
compaction recovery, dream, harvest, aliases, card lint and the exam runner. The
per-session dedupe markers the PreToolUse gate needs for trigger-card defects and
write-gate denials keep working under `NOTICE_MARKER_*`.

**Superseded later the same day:** scar triggers did not survive — §34 removed them
outright. Everything else in this list still holds.

Existing `.epitype/commitments.jsonl` files are **not deleted** — the data stays
on disk, and nothing reads it any more. Gates after the change: run_all 48/48
(52 before), privacy PASS, corpus 330/330, seeds 15/15 and 5/5, doctor HEALTH
PASS 5/5. Selftest denominators: pretooluse 73, stop 22, sessionstart 31,
precompact 8, recall 45, dream 31.
## 31. Hand-written indexes drifted; reachability lint forced them

Two vaults kept their catalogue by hand in `MEMORY.md`. They drifted the way any
hand-maintained list drifts: the two vaults grouped cards differently, the same
fact appeared in the index and again in the SessionStart injection, and closing a
project meant remembering to move a line. The local SessionEnd lint made it worse
rather than better — its "orphan" rule flagged every card not reachable by
breadth-first search from `MEMORY.md`, so the only way to a clean lint was to keep
adding lines to the file the host loads whole into every session. Reachability was
never how recall worked: recall is content-based (bm25 over the FTS index), and a
card is found by what it says, not by who links to it.

Owner 2026-09-09 (Q8, option 丙), after a Claude↔Codex round that converged:

- `MEMORY.md` becomes a hand-written short entry point. The generator never writes
  it — block markers cannot stop "A reads, B appends, A rewrites from its stale
  snapshot", and the host's own auto-memory is one of the writers.
- `epitype views` generates `_views/current.md` and `_views/history/closed.md` from
  card fields, sharing `memsearch`'s scan range and `card_lint`'s type inference, so
  a card cannot be one type to the catalogue and another to the lint. Unchanged input
  fingerprint rewrites nothing; concurrent generators share a lock.
- Levels change by editing a field, not by moving a file. `closed` moves a project
  card to level 3 and changes nothing else — it stays searchable and recallable;
  only `superseded` redirects recall to the successor.
- A card that needs a status and has none is listed under "needs review". Not being
  closed is not evidence of being current.
- The local lint drops the orphan rule and delegates: `epitype views` plus
  `epitype cards --deep`, which folds in `decision_lint`'s uniqueness and
  supersession-chain rules rather than growing a second copy of them, and adds the
  two checks that catch a card actually disappearing — missing from the views,
  missing from the search index. Its index-size warning is now MEMORY.md > 3 KB
  (target ≤ 2 KB), because the complete list lives in the views.

Boundary: none of this improves recall coverage. Moving standing rules out of the
resident index trades per-session tokens for dependence on keyword recall; a tidy
catalogue does not close that gap, and the exam corpus is what measures it.
Regressions: `tests/views_regression.py` (11 cases), `epitype/views.py --selftest`
(12), plus "Closed status moves the view only; the card stays searchable" in
memsearch and the `--deep` case in card_lint.

## 32. Auto-capture wrote unverified sentences as rulings

The 2026-09-01 blueprint had `scar_scan.py` produce proposals only. U26 turned
capture into direct filing (measured kind-precision 43% → 81%), and the filed card
is a real card: indexed, recalled, pinned above ordinary hits. What a trigger match
actually proves is that a sentence *looks like* a ruling — not that anyone checked
it. So system notices and probe sentences kept landing as authority: the 3.6 KB
fake grant of 2026-09-02, and "Transport check only…" opening a session on
2026-09-09.

Owner 2026-09-09 (Q5, option 「C」), verbatim: 「C」 — 「自動捕捉 owner 原話：只有
形狀明確的句子自動入庫，其餘進草稿；自動入庫卡永遠標「捕捉、未核」，不得單獨當授權
依據」.

**The whitelist (`memspec.CAPTURE_ADMIT_*`), judged on the owner's own half.**
`capture.owner_side()` picks the text about to be stored — a grant or correction
stores the owner's sentence, a ruling stores the assistant's question too and the
owner's answer separately — so the assistant's words can never stamp the owner's
chop. Three templates, each one a shape a reader can name without context:

| Template | Rule | Admits | Holds back |
|---|---|---|---|
| `arrow-answer` | a reply marker (`<-` `<=` `《`) is present **and** the owner's half opens with an answer token (同意／可／不／好／甲乙丙／A–E／yes／no) | `…?<-甲，以後都照這個順序` | `6S 維持擋單<我怎麼不知道有這個設定` — a long continuation, not a short answer |
| `leading-correction` | the owner's half **opens** with a correction (不是！／不對，／不要／別再／錯了／stop) | `不是！那個欄位只放小分類，不要放品名` | `那個路徑我說過只能放第二層，不要亂放` — the same correction, mid-sentence |
| `explicit-grant` | the owner's half names a first-person authorization (我同意／同意過／我授權／准你／批准你／允許你／你可以＋動詞／I agree／you may) | `那個資料夾的整理你可以直接動` | `go ahead`／`don't ask me` — tone, not a named authorization; still captured, only proposed |

Anything the rules still capture but no template admits is written to
`<vault>/_drafts/captured_pending/YYYYMMDD/<the same filename>.md`. `_`-prefixed
path parts are already outside `memsearch._scan_vault`, so a proposal is not
indexed, not recalled, and not seen by the write gate's card contract — no second
exclusion rule to keep in sync. Promotion is a person's edit (`verified: true` plus
`verified_by`/`verified_at`, then move), never a replay: every proposal is one
today's rules still capture, which is exactly why it was held, so
`harvest --reevaluate --apply` **HOLD**s it and the dream packet asks for review
instead of offering that command.

**Every auto-written card says so on its face**: `provenance: auto-captured` and
`verified: false`, on filed cards and proposals alike, so promotion is a field edit
rather than a rewrite. `card_lint` lists the four fields as optional for the event
types, so `epitype cards` does not WARN on them.

**`verified: false` is authority for nothing** — verified per consumer, not assumed:

| Consumer | What it reads | Why a captured card cannot get in |
|---|---|---|
| Stop decision gate | `stop_gate._decision_frontmatter` requires `decision_key`; `_read_decision` requires `status: active` | a captured card declares neither |
| Write gate rule A | `pretooluse_gate._forbidden_write` iterates `stop_gate._decisions` | same cards, same bar |
| Write gate rule B | `_vault_card_path` skips any `_`/`.` path part | a proposal carries no card contract |
| SessionStart ruling list | `_active_decisions` requires `decision_key` + `status: active` | same bar |
| Recall | since §41 a captured card is not injected at all — `_event_card` drops every hit under `grants/` `corrections/` `rulings/` | a quote nobody curated is the bottom reading layer, reached by `memsearch` |

**Measured, 254 hand-labelled real sentences** (`tests/capture_precision.py --local`;
`exam/exam_runner.py` cannot run this corpus — it wants a `questions` list). The
policy did not raise the ratio, it cut the volume: kind-precision of the filed pile
0.807 → 0.750 and value(keep) 0.714 → 0.600, while auto-filed cards fall 119 → 40,
wrong-kind cards 23 → 10, not-worth-keeping cards 34 → 16, and 79 sentences become
proposals. Recall of the filed lane drops 0.850 → 0.240, which the owner accepted.
Boundary, stated because it argues against the mechanism: on this corpus the held
pile scores *better* than the admitted one (kind 0.835 vs 0.750, keep 0.772 vs
0.600) — "clearly shaped" and "worth keeping long-term" are different axes, and a
one-off 「我同意診斷」 is shape-perfect. What the change buys is that two thirds of
unreviewed material stops entering recall unasked, not a cleaner ratio.

Regressions: `tests/capture_admission_regression.py` (8 cases, including the
consumer-gate sweep), the admission split in `epitype/harvest.py --selftest` and
`adapters/claude/recall_hook.py --selftest`, and the landing assertion for every
fixture in `tests/capture_integration_regression.py`.

## 33. MEMORY.md grows back by itself

§31 made `MEMORY.md` a hand-written short entry point and moved the catalogue into
`_views/`. That fixed the file once. It does not keep it that way, because the file
has writers other than the person who trimmed it, and every one of them writes the
same shape — one more index line:

1. **The host's own default.** After a card is saved, "append a pointer line to
   `MEMORY.md`" is the assistant's default closing move. It happened on 2026-09-09
   while the trim was in progress: another session appended a `## 使用者既有資源`
   section.
2. **Another session editing directly.** Sessions run concurrently against one
   vault; the second one to read has no idea a trim happened.
3. **Expiry archiving.** Only ever removes lines, so it cannot cause growth — listed
   because it is the third writer and it must keep working while shaping runs.

A write gate cannot be the answer here. The gate would have to reject exactly the
line shape the short entry point is *made of* — `- [name](card.md)` is what the
「索引卡」 and 「習慣與偏好」 sections legitimately contain — so blocking it before
the fact blocks the hand-written entry point as much as the drift.

So the correction happens afterwards, in the dream (owner 2026-09-09:
「我們不是有類似夢的機制，不就是剛好處理這個?」). The dream already runs offline at
03:30, calls no model, and walks every vault; Codex's (c) in the same day's
convergence allows exactly this much contact with the file: `MEMORY.md` may be
edited only in low-frequency, controlled, small-scope passes.

**The rule** (`memspec.INDEX_ALLOWED_SECTIONS`, `epitype/dream.py:shape_index`):

- Sections named in `INDEX_ALLOWED_SECTIONS` (習慣與偏好 / 找不到就搜 / 索引卡 /
  專案規則, each with an English spelling) are the hand-written area. **Nothing
  inside them is ever touched**, links included. Adding a hand-written section means
  adding it to that tuple.
- Outside those sections, a line carrying a card link (`](….md)`) is moved out **only
  if every card it links to is already listed in `_views/current.md` or
  `_views/history/closed.md`**. A link the views do not carry stays where it is and
  is reported instead: it may be a card written minutes ago whose view has not been
  generated, and moving it would be the one case where a line really disappears.
- **The preamble — everything before the first `##` — is never touched either**,
  because a short entry point's title line and its opening sentences can legitimately
  carry links and there is no section to judge them against.
- **A leading BOM is skipped while the sections are parsed**: with it, the first
  heading's opening character is not `#`, so no section is ever recognised and the
  whole file reads as "outside every section" — including the hand-written areas. It
  is skipped for the judgement only; the bytes written back are the original lines,
  BOM included.
- Moved lines are appended verbatim to `<vault>/_drafts/index_pruned/YYYYMMDD.md`
  with the timestamp, the section they came from, and the reason. Verbatim is
  byte-for-byte, line ending included, and identical lines are each recorded — the
  same line under two sections is two facts. Nothing is deleted; `_`-prefixed paths
  are outside the scan range, so the record is not itself indexed.
- The dream regenerates `_views/` before shaping, because "the catalogue already
  carries this card" is the entire test and it must not be answered from a stale
  catalogue.

**When it gives up.** The failure this guards against is the one block markers
cannot stop: A reads, B appends, A writes back its stale snapshot and B's line is
gone. So the pass is read → record mtime and size → compute → **re-check mtime and
size** → rename-into-place with the original bytes compared under a lock
(`card_io.replace_if_unchanged`) → read back and compare. Any mismatch abandons the
whole pass, writes one line into the packet, and changes nothing; the next dream
retries. The record file is appended before the swap, so a rejected swap can never
lose a line; the next pass records it again, each entry carrying its own timestamp —
a record file is the one place where a duplicate costs nothing and a gap costs
everything.
`--dry-run` reports what it would move and writes nothing at all.

Boundary: this is cleanup after the fact, not prevention. Between two dreams the
file can be any size, and shaping cannot rescue a line pasted *inside* an allowed
section — that is the price of never fighting the hand-written area. The size signal
is the local SessionEnd lint, where `MEMORY.md` over 3 KB counts as an ISSUE and
surfaces at the next SessionStart.

Regressions: nine cases in `epitype/dream.py --selftest` — `--dry-run` moves nothing,
a real pass moves only the carried lines and leaves the three short-entry sections
byte-identical, the record file carries the verbatim line with its source section, a
second pass is a no-op, and a file changed between read and write abandons with the
line still in place and nothing appended; plus the three the first review caught — a
BOM'd file's allowed section stays byte-identical, a preamble link line stays while
the same file's out-of-section line still moves, and a CRLF line reaches the record
with its `\r\n` intact and twice when it sat under two sections.

## 34. The card-driven action gate was a layer that should not exist (2026-09-09)

### Symptom

A memory card carried a `trigger.tool` regex, a `trigger.input` regex and `advice`;
`PreToolUse` compiled every such card on every tool call, matched the pair against
the tool name and its input, and returned a bounded denial on a hit. Nine cards in
the owner's own vault ran that way — destructive git, `git add -A`, killing the host
process, reading credential material, and five environment traps.

### Owner's ruling, verbatim

> 「我認為沒有所謂攔截層，應該都是變成類似規則或記憶卡，沒必要多設計攔截層出來」

> 「機械阻斷<-這個就是多餘設計，我認為這種就是核心記憶」

The Q4 answer kept the Stop decision gate and the write-content gate; only the
card-trigger interception was struck.

### Root cause

The gate was built because a rule written in prose does not stop a hand. That
observation is correct, and the conclusion drawn from it was not: it produced a
*third* enforcement layer in front of two that already covered the same calls —
the host's native rules, which refuse an irreversible action before it runs, and
the contract the model reads. Three consequences followed.

1. **It could not do the job it claimed.** A regex over a command string is not a
   shell parser. §24 is the receipt: a pattern narrow enough to catch
   `reset --hard` also caught the clone option `--no-checkout` and a diagnostic
   regular expression that executed nothing, while an unparsed wrapper fell back to
   whole-input matching and denied a quoted mention. Both directions were wrong at
   once, and the fix each time was a longer pattern.
2. **It was paid for on every tool call.** Reading the trigger cards, compiling two
   regexes each and classifying command positions ran before every covered call,
   inside a nine-second fail-open deadline — for a decision the host had already
   made, or would make, in its own configuration.
3. **A card that failed open was silent.** A malformed trigger disabled the guard
   and left a notice; a hung one was killed by the host and the call proceeded. A
   guard that can be switched off by a typo in a memory card is not a guard.

The prerequisite that made removal safe was in place first: Claude's
`permissions.deny` already covers credential material, destructive git, `git add -A`
and killing the host process, and Codex's native `execpolicy` rule file
(`~/.codex/rules/epitype_guard.rules`) was installed and verified on the real
machine. The standing rule that followed: **Epitype gates content, never actions.**
Irreversible actions belong to the host's own native rules; what stayed here was the
pair of gates over what the model is about to write or say, which no host rule can
express.

**Superseded in part, 2026-09-16.** The prerequisite above turned out to cover four
of nine hazard classes, not nine: Claude's Bash patterns match the command text
positionally with no AND operator, so the remaining five could not be written at all,
and one of them — a heredoc eating a level of backslashes — was then hit four times
in a single session with the lesson already carded three times over. The owner lifted
the half of the ruling that forbade a card from carrying an action condition, and
what returned is only the literal form (§3). The rest of the ruling stands: no
semantic judgement, no intent, and no opinion about a command no card has named.

### Removed

| Kind | Item |
|---|---|
| gate path | `pretooluse_gate`: `_trigger_card_paths`, `_parse_trigger_card`, `_declares_trigger`, `_write_trigger_cache`, `_inline_mapping`, `_bounded_deny`, `_append_audit`, `_append_parse_defect`, `_parse_defect_seen_today` |
| command parsing | `_command_candidates`, `_shell_segments`, `_without_heredoc_bodies`, `_heredoc_markers`, `_python_embedded_commands`, `_command_match_position`, `_uses_command_matching`, `SHELL_TOOL_NAMES` |
| constants | `memspec.TRIGGER_TOOL_FIELD`, `TRIGGER_INPUT_FIELD`, `TRIGGER_MATCH_FIELD`, `TRIGGER_COMMAND_MATCH`, `TRIGGER_FULLTEXT_MATCH`, `TRIGGER_TOOL_PATH`, `TRIGGER_INPUT_PATH`, `GATE_DEFECT_NOTICE`; `TRIGGER_REGEX_MAX_CHARS` renamed `FORBIDDEN_REGEX_MAX_CHARS` |
| state files | `<vault>/.epitype/gate_triggers.json` and `gate_parse_defect_seen.json` are neither read nor written; existing files are left on disk |
| card contract | `scar` no longer requires `trigger.tool`/`trigger.input`; `trigger` is not a type signal any more, and a card that still declares one gets one `card_lint` WARN (`deprecated-field`) rather than a FAIL |
| test | `tests/git_gate_regression.py` (retired: it graded the command matcher against 31 git command shapes) |
| test | `tests/capture_admission_regression.py` consumer 3 (「PreToolUse 授權判定只讀宣告 trigger 的卡」); the other four consumers stay |
| template | the four example cards keep their incident and advice and lose their trigger; `templates/power/examples/scar-destructive-git.md` now records why a lexical pattern was the wrong shape |

Kept and moved rather than removed: the bounded-regex validator that rejects
catastrophic backtracking, renamed `_compile_bounded_regex` — it is the shared
reading of a decision card's `forbidden` patterns for both gates — and the YAML flow
splitter, now `memspec.split_flow_items`, which the Stop gate and `dream.py` had
been borrowing from the trigger parser.

### Exam questions retired

**89 graded gate questions changed verdict, none deleted.** Every `gate` question in
the graded corpora installed a trigger card and asserted a denial, so each one now
asserts the opposite and is a regression test for this removal: `corpus_300.json`
59 of 80 flipped from `deny` to `allow` (the other 21 already expected `allow`), and
`seeds_20260902_morning_review.json` 5 of 9. Denominators are unchanged — corpus
330/330, seeds 15/15 and 5/5 — and the categories keep their counts. The packaged
`exam/sample_corpus.json` kept its three gate questions at 16 total but rebuilt them
on the write gate: two denials (English and Chinese) against an active ruling's
`forbidden` pattern, and one allow for content that follows the ruling.

One repeatability defect surfaced while rebuilding them: the write gate blocks a
given `(session, rule, file, content)` exactly once, so a corpus replayed twice saw
the second run allowed. `_run_gate` now gives each question its own session id and
clears the markers afterwards, the same treatment `_run_stop` already had
(`_hook_common.clear_notice_markers`).

### Gates after the change

run_all 48/48 (49 before, minus the retired git-gate regression), privacy PASS,
corpus 330/330, seeds 15/15 and 5/5. Selftest denominators: pretooluse 30 (73
before), card_lint 42 (41 before), exam runner 7, stop 22 unchanged.

### Boundary

This removes Epitype's claim to stop an action; it does not remove the hazard. The
guarantee now rests entirely on the host's native rules, which are configuration
outside this repository and are not tested by these gates. A host installed without
them has no action-level protection from Epitype — and, per §24, never really had
the protection the trigger cards appeared to offer.
## 35. Session start became a fixed 8 KB prologue

Every SessionStart injected the same prologue before anything specific to the
session: the whole work ledger, the vault's standing rulings, an overdue-pending
line, a "correct the card yourself" reminder, a card-type lint line that also spoke
for WARN, and a dream line that reported even when the dream had nothing to report.
Measured on the owner's two real vaults on 2026-09-09 (copies, four event shapes),
every shape hit the 8 KB budget cap and ended in `…（超出預算，餘 N 段未注入）`:
8015 / 8060 / 8153 / 7986 bytes. In two of the four shapes the short entry point —
the one piece a host without a native index load cannot get anywhere else — was
among the pieces cut.

### Why it happened

Continuity was implemented as re-sending. Each piece was added for a real incident
(§25's ruling that came back as an option, a ledger nobody read, a pending item with
no exit), and each time the fix was "make the next session see it at the top". Nobody
priced the recurring cost: a piece added once is paid at every session start, forever,
and it is paid first, so it displaces whatever the session actually needed.

Owner 2026-09-09, on the ledger and the ruling block:「不應該塞，這是多餘設計」
「原生功能就會你就會去讀claude.md;CODEX就會去讀agents.md」「現行裁定清單（12 行）我認為
應該是回歸進入正確地方」「沒必要就拿掉阿，反正浪費token的行為都不應該」.

### The rule

SessionStart carries the short-index echo and nothing that does not need an action:

- **Work ledger:** not injected. The file stays where it is — it is still the marker
  that identifies the governance vault — and is read when the work needs it.
- **Standing rulings:** not injected. Recall already brings a decision card, with the
  owner's own words, when the prompt touches it (§25); the Stop and write gates read
  the same cards directly. `epitype decisions` lists them on demand.
- **Overdue pending lines:** not injected. `epitype pending` and the nightly dream's
  section 3 name them.
- **Fixed explanatory text:** none. The self-correction reminder (`🔁`) is gone;
  behaviour belongs in cards and exams, not in a per-session string (§30).
- **Card-type lint:** one line only when something FAILs. WARN alone is the dream's
  list, not this session's job; the WARN count still rides along on the FAIL line.
- **Dream:** one line only when someone must act — the dream errored, the dream left
  review candidates, or the dream is past due and is not running. A clean finished
  dream says nothing; "it ran and found nothing" is not news. Overdue is judged by the
  same `interval_hours` that decides whether to start one, and a lock younger than
  `DREAM_LOCK_STALE_SECONDS` means it is running, not missing.

Same measurement after the change: 0 / 2033 / 1821 / 3510 bytes — and the entry point
is no longer among the cut pieces. The Claude shape whose cwd vault the host already
loads now injects nothing at all, which is the correct amount.

Retired with the behaviour: `pending_lint.summary_line` and its two selftest checks
(7/7 → 5/5), the `_active_decisions` / `_decision_block` / `_vault_labels` /
`_frontmatter_fields` helpers, and the memspec strings they used
(`SESSIONSTART_DECISIONS_HEADER`, `SESSIONSTART_DECISIONS_MAX_LINES`,
`SESSIONSTART_DECISION_RECENT_DAYS`, `SESSIONSTART_DECISION_REST_LINE`,
`CARD_SELF_CORRECT_NOTICE`, `PENDING_LINT_HOOK_BUDGET_SECONDS`,
`DREAM_NOTICE_CLEAN_LINE`). Four sessionstart selftest cases were rewritten from
"this appears" into "this never appears", and the removed behaviour's own cases went
with it (31/31 → 30/30); `card_lint` gained the WARN-only case (41/41 → 42/42).
No exam question depended on any of it: the exam drives UserPromptSubmit, PreToolUse
and Stop, never SessionStart — corpus 330/330 and seeds 5/5 unchanged, 0 questions
retired.

Boundary: this removes a fixed cost, it does not add a retrieval route. A ruling the
prompt never touches is not recalled, and that is the trade the owner took — the
decision cards, the ledger and the pending list are all one command away, and the
hosts load `CLAUDE.md` / `AGENTS.md` on their own.
## 36. Unconfirmed material stays where it landed

Epitype has, up to §33, a write side that files things and a read side that serves
them. Nothing in it asks the opposite question: **what got produced and never
confirmed, and is still sitting wherever it landed?** Owner 2026-09-09 put that job
on the dream — it is the pass that returns unconfirmed material to the layer or the
card it belongs in — and named four shapes of it. Measured on this machine the same
day with the sections built for it (`epitype dream --dry-run`, both
registered vaults):

1. **Pocket vaults.** 「我不知道會有多少地方在產生非整理的記憶」. The registered vault
   list cannot answer that: it enumerates only the places already known. Counting from
   disk instead found **8 unregistered `<home>/.claude/projects/*/memory` directories
   holding 134 cards**, none of them reachable by any lint, view, or recall path.
2. **Draft backlog.** **370 files under the governance vault's `_drafts/**`** (226 of
   them under `decisions/`, 84 under `harvest/`, 51 under `captured_dropped/`). §4
   already counted them; a count nobody reads is not a queue, and the count alone does
   not say whether they are yesterday's batch or half a year old.
3. **Mixed cards.** 「一張卡就是一個記憶或規則，不要混雜」. **71 cards in the governance
   vault and 88 in the project vault** carry more than one thing — bodies with up to 11
   `## ` headings, bodies over 14 KB, descriptions that string several rules together
   with `＋`/`；`. A mixed card cannot be superseded, expired, or retired as one fact.
4. **Quotes with nobody behind them.** Recall serves `rulings`/`corrections`/`grants`
   under `⚖ owner 裁決：` and `⚠ owner 曾糾正：`, and only a card that declares
   `verified: false` is demoted to a historical capture
   (`adapters/claude/recall_hook.py`). So an event card that is still trusted but that
   no decision card mentions reads exactly like a standing ruling. There are **91 of
   them in the governance vault and 53 in the project vault** — among them a transport
   probe (`Return only {"probe":"ok"}`) and a one-off instruction, both currently
   served with the ruling prefix. This is the item the 2026-09-09 Claude↔Codex
   convergence added: before splitting the automatic-recall quotes, find the quotes
   that were recalled *as rulings* with no decision card carrying them.

A fifth shape has no measurement yet because it has no threshold: owner 2026-09-09 sets
a cap as the reviewed value plus twenty percent, and the product must not guess one. The
cap keys were unset on this machine, so the section says so and judges nothing rather
than substituting a number of its own.

**The countermeasure** (`epitype/dream.py` §§8–12) is deliberately the weakest one
available: **every section only lists candidates**. Nothing is moved, split, promoted,
registered, trimmed, or deleted. That is not timidity — each of the five disposals is a
judgement the machine cannot make. Which pocket vault belongs to which household, which
card is two rules and which is one long one, whether a quote is a standing ruling or a
one-off, and what a cap should be, are all owner decisions; a dream that guessed any of
them would be manufacturing confirmation, which is the exact failure this section is
about. So each section ends in a human call, and `_next_steps` says 「人工判斷」 rather
than naming a command that would apply anything.

Three traps the implementation had to avoid:

- **A hardcoded home.** The projects root is recognised from a registered vault's own
  path (`.claude/projects` walking up) and only falls back to `HOME`. Taking the looser
  rule — "the registered vault's grandparent is the root" — would make a vault at
  `C:\a\b` scan every directory under `C:\`.
- **A guessed cap.** A cap the product invented, reported as "over cap", would read as
  the owner's own threshold. Missing keys write one "unset" line and judge nothing.
- **A stem match.** `carried` is a substring of `uncarried`, so matching a quote's
  filename without its extension would report "a decision card carries this" for a quote
  nobody carries — an under-report, in the one direction that hides the problem. Only
  the full filename and the `decision_key` count.

Regressions: eight cases in `epitype/dream.py --selftest` — the pocket-vault scan lists
the unregistered directory and skips both the empty one and the registered vault; drafts
are aged, grouped by first-level subdirectory, and reported oldest first; the three mixed
shapes are flagged while a fenced markdown example is not; the cap section says "unset"
and judges nothing without the keys, then lists both over-cap files with their overage
and leaves both byte-identical with them; only the trusted quote no decision card carries
is listed; the next steps carry all five counts; and the pack renders §§8–12 with shaping
moved to §13 and the next steps to §14. The selftest points `HOME`, `USERPROFILE` and
`EPITYPE_CONFIG` at its own temporary directory and restores them in a `finally` — §8
reads the home directory and §11 reads the config, so without that the test would scan
the real one.

## 37. "Nobody carries this quote" was measured by the one route almost nobody uses

§36's fourth section asks whether any decision card carries an event card's quote, and
it answered by looking for the event card's **filename or `decision_key`** in a decision
card's body, `source`, `superseded_by` or `aliases`. That is how a machine would link two
cards. It is not how these cards were written: a decision card carries a quote by
**copying the sentence into `owner_quote`**, and an event card that has been dealt with
records its home in its own **`carried_by`** field. Neither leaves a filename anywhere.

So the section reported every trusted event card in both vaults — 91 and 53 on the day
§36 was written, every single one of them. A list that flags everything says nothing, and
its next-steps line ("N quotes read as standing rulings with nobody behind them") reads as
an alarm rather than a queue. The defect is the same shape as the one §36 itself warns
about in the other direction: a match rule narrower than the corpus it judges produces a
verdict about the rule, not about the corpus.

The fix is three carry routes, any one of which clears a card:

1. **Named** — the original route, unchanged (full filename or `decision_key`).
2. **Quoted verbatim** — a decision card's `owner_quote` overlaps the event card's body.
   Both sides are normalised the same way: NFKC first, then everything that is not a
   letter, digit or combining mark is dropped, then casefold. That is one rule for
   whitespace, full-width/half-width forms, quotation marks and punctuation, and it
   hardcodes no language's punctuation — the real pair that exposed the defect differs
   only by a stray `<` inside the sentence. `owner_quote` is also split at Unicode quote
   and bracket characters before matching, because one field routinely packs several
   quotes from several sources (`「B」（Q7）；「…」`), and the whole field is a substring of
   nothing. The overlap must run at least `UNCARRIED_QUOTE_MIN_CHARS` (12) characters in
   whichever direction is shorter: a four-character overlap (「先對過帳」) happens between
   any two Chinese sentences, and accepting it would silently switch the section off.
3. **Self-declared** — the event card's own `carried_by` names its carrier. Whether that
   card exists is `epitype cards`' question; this section only asks whether anybody has
   taken the quote somewhere.

Each row also gains a **suspected-noise** column: auto-capture files the payload of a
cross-CLI transport probe as a ruling (`Return only {"probe":"ok"}. Do not use tools.`),
and those read like owner rulings while being nobody's sentence. A body matching one of
`memspec.EVENT_NOISE_MARKERS` is marked, and nothing else happens to it — deleting or
demoting stays a human action, like every other row in §§8–12.

Measured on this machine on 2026-09-09 (`epitype dream --dry-run`, both registered
vaults, section 12 counts): **56 + 40 = 96 before, 29 + 23 = 52 after**. The before
numbers are lower than §36's 91/53 because 41 of those cards had since been demoted with
`verified: false`, which the section already skipped. Of the 44 cards the fix cleared, 33
+ 18 declared a `carried_by` (a field the vaults were already using, mostly naming a
`feedback` card), and the verbatim-quote route matched 3 cards in the governance vault —
all three already cleared by another route, so its measured contribution today is zero.
It is kept because it is the route the decision cards actually use: all 19 decision cards
in the governance vault copy their ruling into `owner_quote`, while naming the event file
clears only 6 of them, and the next card written that way will have no `carried_by` to
fall back on.

Regressions: six cases in `epitype/dream.py --selftest` (48 → 53) — the section lists
only what no route clears; each of the three routes clears one card (named, quoted in
part, self-declared); an overlap shorter than the twelve-character floor is not a carry
and its card stays listed; and the transport-probe template is the only row marked as
noise.

## 38. Nothing measures whether the feedback ever worked

Epitype captures owner corrections, blocks turns and writes, and grades itself against
a corpus. Nothing reads any of that back. Owner 2026-09-09: 「應該有回饋檢討機制，你跟
CODEX設計一下」. The Claude↔Codex convergence that followed settled the shape —
feedback keeps its provenance, the dream assembles candidates, a review sitting happens
only once enough candidates accumulate, and the owner decides a whole batch at once; no
session gains a single mandatory action — and Codex named two wiring gaps that had to
be in the first unit. Both were real, and both are measured here.

**Gap one: card count could never mean incident count.** Capture deduplicated on the
sentence digest alone, so the same sentence said in three different conversations left
exactly one card. Every downstream question the review loop needs to ask — 「同一件事被
糾正第二次了嗎」 — was unanswerable from the vault, because the second and third
incidents were dropped at write time as "already have this". Worse, the filename was
`{kind}-{date}-{digest}.md`: two conversations on the same day would have collided on
the filename even if the digest check had let them through.

The fix is the smallest one that restores the count: each event gets an identity of its
own (`event_id` = host + conversation + message position + sentence; `origin` = the same
identity in readable form), the identity goes into both the frontmatter and the
filename, and deduplication asks "same event" instead of "same sentence". Same
conversation, same sentence stays one card — saying something twice in one turn is one
incident. Position is the transcript's byte length at capture time for a live hook and
the record's line number for a replay; when neither can be read it is `-`, never `0`.

**Gap two: an exam failure could not name the rule it failed.** The runner printed
`PASS`/`FAIL` and an id. `exam_runner.py` now carries whatever mapping the question
declares (`cards`/`card`/`decision_key`) out with each result and prints
`UNMAPPED n/total`. On this machine, that number is currently **350/350** — no shared
corpus question declares a rule card yet, and the honest report of that is the point:
the review pack can only connect failures that have a mapping, and says so instead of
reporting zero. The fixtures in `setup.vault_cards` are deliberately *not* used as the
mapping; they are the question's synthetic vault, and reading them as the rule would
make "unmapped" permanently zero — a metric that always says "fine" is not a metric.

**The countermeasure is report-only, like §36.** Dream §15 lines the four sources up
against the cards they point at and counts; it judges no type (A/B/C is the review
sitting's call), changes no card, moves no layer, and never injects anything into a
session. Two numbers are deliberately shown but not counted toward the trigger: the
§8–§12 candidates, which already have their own next-step lines, and events with no
`matched_card`, which are exactly what §12 measures. Counting either would double-count
and, at the real vaults' scale, keep the threshold permanently satisfied — a trigger
that is always on is the same failure as a metric that always says "fine".

Measured the same day on both registered vaults (`epitype dream --dry-run`, read-only):
governance **3 rows / 3 items** against a trigger of 5, from 91 events (29 of them
`verified: false`, **91 with no `matched_card`**) and 15 blocks; project vault **1 row /
1 item**, 53 events (12 unverified, 53 unmapped), 1 block. Both said 「沒有
`exam_results_latest.json`」, since no exam run had yet written one to a real vault. The
review pack therefore reports what it can actually attribute — the blocked decisions —
and states the rest as a gap: the `matched_card` field exists in the contract and no
path writes it yet, which is the convergence's own choice (a correction only names a
rule when it names one; nothing is forced to search every session).

**The trigger value is not evidence.** Five is Codex's operating starting point for
amortising the cost of two readers going through a pack, adopted so the loop has a
number to run with; it is not measured, and `memspec.REVIEW_PACK_TRIGGER` is the one
place to change it once per-pack tokens, effective dispositions, and waiting time say
something.
## 39. The contract was prose nobody could generate, check, or cost

Owner 2026-09-09, on the rule contract the agents load every session:
「契約應該是簡單扼要規則」and「契約等應該跟整個epitype做整合」. Both sentences point at
the same defect. The contract had grown into an 18 KB document of paragraphs: a rule and
its explanation, its incident history and its examples all lived in one block of prose,
so nothing could be counted, superseded, or retired as one item. And it lived outside
the memory system that governs every other durable statement — cards have required
fields, a lint, a supersession chain, a catalogue and a size check; the contract had a
file and a habit of editing it.

The consequences were all the ones the card contract already exists to prevent:

- **No unit.** A rule and its rationale were the same paragraph, so shortening the
  resident cost meant rewriting prose by hand and hoping nothing normative was lost.
- **No provenance per rule.** The document as a whole had a version; an individual
  sentence had no record of who approved that exact wording or when.
- **No mechanical check.** Whether the file on disk still matched what had been approved
  was a question only a careful human reading could answer.
- **No layer.** Every sentence was resident by construction. A rule that only matters
  when a prompt touches it was paid for on every turn of every session.

**The countermeasure** is the `rule` card plus `epitype/core_gen.py` (see
`docs/ARCHITECTURE.md`, "Core generation"). One card is one rule; the card carries the
approved sentence in `text`, who decided and who approved it, the incidents behind it,
and `layer` — which decides whether the sentence is paid for every turn (`floor`,
`resident`) or only when recall reaches it (`situational`, `recall`). The generator
selects, orders and copies; it never rewrites a single character, because a rewritten
sentence is a rule nobody approved. It refuses to write anything at all when a generated
card lacks its approval fields or when the assembly is over its cap, and it records
what it built from in an approval pack keyed by the SHA-256 of each `text`. `--check`
re-assembles and compares byte for byte, so "does the file still match the cards" became
one command — run by hand, and nightly as a report-only candidate in the dream's §11.

What this unit deliberately does **not** do: it writes no contract file and no host file.
The product gained the ability to generate a core block from approved cards; deciding
which sentences become cards, and pointing the generator at a real file, stays outside
the repository (owner ruling 2026-09-09: the wording of those files is approved before
it is written, not after).

Regressions: eighteen cases in `epitype/core_gen.py --selftest` — the two generated
layers assemble in the right order while `situational` and `superseded` cards do not
appear at all; every generated `text` survives byte for byte, including non-ASCII; the
approval pack carries every field including per-card `text` hashes; `--check` matches a
fresh file, detects a single hand-edited line, and stays silent when there is nothing to
compare; `--dry-run`, over-cap and missing-approval runs all leave the target file
untouched; and the CLI returns non-zero for a refusal without repairing the file. Two
more in `epitype/dream.py --selftest` hold the §11 side: a hand-edited block is listed as
a drift candidate and a vault with no rule cards never is. The core-gen selftest points
`HOME`, `USERPROFILE` and `EPITYPE_CONFIG` at its own temporary directory and restores
them in a `finally`, with one case pinning that: its CLI cases go through `main()`, which
reads the real config when nothing redirects it, so the day an owner sets `core_cap_bytes`
an unisolated selftest would start failing against a cap that has nothing to do with it.

## 40. The short index was echoed to hosts that already load it

§35 emptied the session prologue of everything standing except one piece: an echo of
each vault's hand-written `MEMORY.md` short entry point, kept "for hosts that do not
load it natively". The premise was already false for one host and became false for the
other. Claude Code loads the cwd project's `MEMORY.md` on its own, which is why §35's
Claude-shaped measurement was already 0 bytes; Codex loads `AGENTS.md`. What was
missing was never a delivery mechanism — it was that neither host file carried the
index. Once the index section was written into `~/.claude/CLAUDE.md`,
`~/.codex/AGENTS.md` and each project's `AGENTS.md`, the hook's echo was a second copy
of text the host had already loaded, sent at every session start whether or not
anything in it was wanted.

### Owner's ruling, verbatim

owner 2026-09-09:「原生功能就會你就會去讀claude.md;CODEX就會去讀agents.md」
「不應該塞，這是多餘設計」「沒必要就拿掉阿，反正浪費token的行為都不應該」.

### Why it happened

The echo was built when the host files held no index, so "the host does not load it"
was true and the hook was the only route. The condition was then encoded as a host
test — `transcript_path` under `.claude` means Claude, so skip; anything else means
Codex, so echo — instead of as a question about the destination. A host test cannot
notice that the destination changed. The order the owner insisted on ("先接通替代路徑
再拆舊功能") is what makes the removal safe: the index section reached all three host
files, and was confirmed there, before the echo was taken out.

### The rule

SessionStart injects nothing that is not an action. The whole index path is gone —
the Claude/Codex host test, the ancestor-vault echoes, the byte-budget sampling and
its "sent N of M bytes" footer. `additionalContext` may now be empty, and when it is,
the hook emits nothing at all; the stdout JSON shape is otherwise unchanged
(`hookSpecificOutput` → `hookEventName` + `additionalContext`). What remains: the
card-type FAIL line, the by-the-way alias task, and the dream line. The vault
selection (cwd vault plus the governance vault) survives — it now bounds which vaults
the card scan reads rather than which indexes are echoed.

Measured on copies of the owner's two real vaults, four event shapes in the order
Claude×`C:\`, Codex×`C:\`, Claude×titan, Codex×titan, 2026-09-09: before
**0 / 996 / 996 / 2624** bytes, after **0 / 0 / 0 / 0**. (§35 records 0 / 2033 / 1821 /
3510 for the same four shapes earlier the same day; the copies were re-taken for this
unit and the vaults had changed in between, so the before column is re-measured against
`master` rather than carried over.) The same four shapes against the real config and real home
also injected 0 bytes with `rc=0`, empty stderr, and no write to either vault
(`dream_state.json` and `no_chinese_cursor.json` unchanged by sha256). Zero is the
honest number here rather than a floor: on that day neither vault had a card-type FAIL
or a card missing a Chinese alias, and the dream was inside its interval, so nothing
named an action. A vault with a FAIL still gets its one line.

Retired with the behaviour: `sessionstart_hook._index_echo`,
`_claude_native_index_vaults`, `_joined`, `_fits`, `_SUFFIX_RESERVE`, the
`payload_fits` and `epitype.capture_route` imports, `memspec.slim_index` and
`memspec.SESSIONSTART_INDEX_TRUNCATED_LINE`. The sessionstart selftest went 30/30 →
25/25: the two `slim_index` cases, the two over-budget sampling cases and the two
host-shape echo cases retired, replaced by one case pinning that no index reaches the
context and one pinning that the Claude and Codex shapes now receive a byte-identical
payload — a difference there would mean a host-specific branch is still alive. Six
further cases that used "this index line is present" as their liveness anchor were
re-anchored on a card that fails its type contract, because on an empty context every
"…is not present" assertion is vacuously true. `tests/governance_regression.py`'s
degraded-vault case moved to the same anchor. No exam question depended on any of it:
`exam_runner` drives UserPromptSubmit, PreToolUse and Stop, never SessionStart —
corpus 330/330 and seeds 15/15, 5/5 unchanged, **0 questions retired**.

Boundary: the index is not lost, it moved to the layer that was already paying for it.
Keeping it there is the sync tool's job, not the hook's — if a host file ever stops
carrying its index section, SessionStart will not notice and will not compensate.

## 41. Recall read the bottom layer out loud (2026-09-09)

Owner 2026-09-09, verbatim: 「其他全部按需讀：索引指到卡片，卡片只在喚回時出現；卡片
下面才是解說，再下面才是原話和對話紀錄，要用到才翻」.

Auto-capture files the owner's sentence verbatim under `grants/` `corrections/`
`rulings/`. Those files are the bottom of the four reading layers — the raw record a
card points back to when somebody needs the exact wording. Recall injected them as if
they were the card layer: `_captured_context` read the whole quote file, prefixed the
line with 「歷史捕捉（非完整對話／現行裁定）」, and gave it a pinned seat that the byte
budget could not drop. So the layer meant to be opened on demand arrived first, in
full, at every prompt whose words happened to match — and 「歷史捕捉」 is a label, not
a filter: a sentence that was never reviewed still reads like a standing instruction
once it is sitting in the turn ahead of the cards.

The root cause is a layering one, not a labelling one: **the raw record was not kept
at the bottom.** A card is a thing somebody curated — a decision, a rule, a scar, a
feedback note. A quote file is evidence for one. Injecting evidence beside curated
cards makes the two indistinguishable at the point of reading, and the pinned seat
made the uncurated one louder.

**The rule.** `adapters/claude/recall_hook._event_card` decides from the
vault-relative path: a hit whose first path segment is one of
`memspec.EVENT_CARD_DIRECTORIES` is skipped before any cap, prefix or seat is
computed. One exception, and it is not an exception to the layering — a file that is
itself an active decision card (`decision_key` + `status: active`, read from the card
by `_active_decision`, not from the index) is a curated card that happens to live in
a capture directory, and keeps its pinned decision seat. The removed machinery goes
with it: `_captured_context`, the 「歷史捕捉」 prefix, the correction/ruling pinned
seats, and `memspec.CORRECTION_PREFIX` / `RULING_PREFIX` / `CAPTURE_LABEL_REGEX`.

**What did not change.** Capture still writes exactly what it wrote before (the U-B
whitelist, the U-P event identity, the proposal area). `memsearch` still indexes the
quote files and still returns them from `query` and `recall` — that is the whole
point: an AI that needs the owner's exact words searches for them, one file at a
time, because a card sent it there. The Stop gate and the write gate read decision
cards and were never in this path.

Prerequisite, honoured in order: the quotes were carried into decision/rule/feedback
cards first (U-N-1, U-N-2, titan; `carried_by`), so removing the injection removes a
duplicate route, not the only route. The dream's §12 keeps listing quotes no card
carries yet.

Regressions: `tests/capture_recall_regression.py` (never injected, still searchable,
a promoted decision card keeps its seat), `tests/capture_admission_regression.py`
(the consumer sweep), `tests/recall_selection_regression.py` (a quote consumes no
slot), and `adapters/claude/recall_hook.py --selftest`.

## 42. The split-candidate list could not be finished (2026-09-10)

§10 of the dream lists mixed cards from three shape signals: two or more `## `
headings, a body over `memspec.CARD_BODY_MIXED_BYTES`, or a long `description` that
strings several things together with `＋`/`；`. The signals were the whole judgement.
Nothing read the record of a human having already looked.

So the owner reviewed the project vault card by card on 2026-09-09 — 89 cards marked
`mixed_reviewed: 2026-09-09-keep` / `-index`, the ones actually split marked
`status: superseded` — and the next run listed **122**: the same 89, plus the new
cards the split had just produced, several of which inherited the parent's packed
`description`. Two ways to fail from there, and both are worse than the original
mess: work through a list that cannot go down, or stop reading a section that is now
mostly noise. A backlog that regenerates itself after the work is done teaches people
to ignore it.

The root cause is that **a mechanical signal was allowed to outrank a human
decision.** The shapes are cheap and language-free, which is why they are the right
way to *find* candidates; they are not a judgement about whether a card holds one
thing. A review is that judgement, and it leaves a mark — the product simply never
read the mark.

**The rule** (`dream._mixed_skip_reason`). A card is skipped and counted, not listed,
when any of these holds: `memspec.MIXED_REVIEWED_FIELD` (`mixed_reviewed`) is present
in the frontmatter — **the value is never read**, so a mark written in any language
works and no date or vocabulary is hardcoded; the card is `status: superseded` (a card
that has been replaced is not a split candidate); or the card carries
`memspec.SPLIT_FROM_FIELD` (`split_from`) *and* is now under the byte cap with fewer
than `CARD_MIXED_HEADING_MIN` headings. That last exemption forgives exactly one
thing, the inherited `description`: a card that came out of a split and then grew two
headings of its own, or ran over the cap, is listed again on its own account.

The skip is counted **only for a card the shapes would otherwise have listed**, and
the section reports it as 「已審過略過 N 張」 with a per-reason breakdown. Counting
every marked card instead would inflate N with cards that were never candidates, and
the number would no longer reconcile with the drop in the list. Measured on the two
real vaults (read-only `--dry-run`, 2026-09-10): project vault **122 → 22**, skipped
100 (89 reviewed, 11 fresh splits); governance vault **75 → 11**, skipped 64 (all
reviewed). Both reconcile exactly.

What this does not do: nothing is written, moved, or unmarked. Removing a
`mixed_reviewed` mark from a card puts it straight back on the list — the mark is the
only thing holding it off, and it lives in the card, where a human put it.

Regression: `epitype/dream.py --selftest` (a reviewed card with two headings is
skipped, a superseded card over the cap is skipped, a fresh split is not relisted
while an unmarked twin with the same description still is, a split card that grew two
headings is listed again, and the count reported is the count taken off the list).

## 43. Recall was the largest token cost, and most of it went unused (2026-09-17)

Measured over 7 days of real transcripts (96 sessions, about 13,000 model calls; token
counts estimated): recall injected roughly 570K tokens, and because injected context is
re-read on every later call until compaction, it carried roughly 78M cache-read tokens.
Every other Epitype mechanism was at least an order of magnitude smaller. Stop-gate
rewrites (7 in the week) and action-guard denials (13) were cheap.

Two separate wastes:

- **Re-sending cards already in context.** Aliases (`V1`, `V2`...) are numbered by which
  vaults a given prompt used, and the per-session dedup digest included them, so a card
  already delivered was sent again whenever that set changed: 1,404 identical lines
  re-sent within one compaction segment, median 3 prompts apart. Dedup now keys on the
  real vault path (`recall_hook._card_identity`), which still keeps an identical line
  from another vault distinct.
- **Cards that are shown and never used.** A loose proxy (the card's name appears in a
  later reply or tool call) put use at 4% for project cards and 7% for reference cards.
  `epitype/recall_quiet.py` counts, nightly in dream §13, how many compaction segments
  each card was shown in and whether it was used, over a rolling
  `RECALL_QUIET_WINDOW_DAYS` (14). A card shown in at least `RECALL_QUIET_MIN_SEGMENTS`
  (10) segments with zero use goes on the vault's quiet list and recall skips it.

What is never quieted: decision cards (pinned), any card carrying a gate field, and every
type outside `RECALL_QUIET_TYPES` (`project`, `reference`, `pending`). A behaviour card
that is never named may still be shaping behaviour; the proxy cannot tell, so those
types are out of scope. A quiet card stops accumulating showings, so it falls back out of
the window and is re-evaluated; memsearch still finds it. A missing or malformed health
file quiets nothing.

Simulated on the real vaults before shipping (14 days, read-only): 56 cards would go
quiet (37 project, 19 reference), 12% of all card showings.

Regression: `epitype/recall_quiet.py --selftest`,
`tests/recall_selection_regression.py` (alias renumbering does not re-send a card; a
quiet ordinary card is skipped while a decision on the same list is not).

## 44. Every card missed what was said before a tool call (2026-09-17)

The Stop gate read `last_assistant_message`: the turn's final text only. A turn that
talks, calls a tool, and talks again shows the owner all of it, but the gate saw only
the last part. On 2026-09-17 a false claim ("this was never verified on Cursor", when
it had been, the day before) sat before a tool call; the card armed that same day to
catch exactly that sentence could not have reached it. Every armed `forbidden` and
`require_when` card had the same blind spot.

The gate now reads the transcript tail (`STOP_GATE_TURN_TAIL_BYTES`) back to the last
real user prompt (tool results are logged as user rows and do not end a turn), joins
the turn's text blocks with `memspec.join_turn_text`, and checks that. Evidence given
anywhere in the turn satisfies a `require_text`. A transcript that cannot be read falls
back to the last message, the previous behaviour. The re-run after a block is still
never blocked (`stop_hook_active`), so a correction cannot loop.

The nightly replay uses the same join, so a turn has one digest on both sides and the
"outside the gate's view" count is now expected to be zero. The replay's guard matching
also uses `memspec.action_guard_tool_matches`, the same tool-name equivalence as the gate.

What this does not change: text already shown mid-turn cannot be unsaid. A block makes
the turn end with a correction instead of standing uncorrected. Hosts whose Stop hook
cannot block (Cursor) gain nothing here; Codex transcripts use a different format and
fall back to the last message.

Regression: `tests/action_guard_regression.py` (a mid-turn claim is blocked while the
last-message-only reading passes it; evidence earlier in the turn satisfies the
requirement; an earlier turn does not count; an unreadable transcript falls back; the
gate and the replay record the same digest), `epitype/compliance.py --selftest`.
