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

The vault scan lists directories with `os.scandir` and recognises symlinks and junctions from the entry's own attributes; nothing is resolved, and `_`- and `.`-prefixed parts are never entered. The stale check reads nothing inside its grace window. The action gate keeps a manifest cache of which cards declare a trigger (`<vault>/.epitype/gate_triggers.json`) and re-reads only cards that changed; a card whose trigger cannot be compiled is named to the model once per session rather than dropped. The memsearch selftest lists 300 cards and asserts that no path is resolved.

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

The `Stop` hook compares the turn's last assistant message against the active decision cards of the cwd vault and the governance vault before the turn is allowed to end. A card's `forbidden` sequence — regular expressions or literals, validated by the same rejection rules the action gate uses, so a card cannot hang the turn it guards — blocks the turn and quotes the owner back. A question sentence naming one card by two of its `aliases` blocks it as well: putting a settled matter back to the owner is the same failure as proposing it. A block emits `{"decision": "block", "reason": ...}` and is audited to `_GATE_LOG.jsonl` as `stop_block`. The host re-runs `Stop` after a block, so `stop_hook_active` is never blocked twice, and one `(decision, message)` pair blocks once per session — the marker lives in the recall marker directory, so compaction clears it with the rest.

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

`PreToolUse` inspects the text a file-writing call is about to put on disk — `Write`'s `content`, `Edit`'s `new_string`, each `new_string` of a `MultiEdit`, and the equivalents of the Codex-shaped tool names.

Rule A blocks new content matching any `forbidden` pattern of an active decision card in the cwd vault or the governance vault, quoting the owner and the matched fragment. The decision cards, the pattern validator, and the manifest cache are the Stop gate's own, so a ruling cannot be enforced at the end of a turn and ignored mid-turn. Editing the rule itself is exempt: a hit is ignored when the post-write text carries the same `decision_key` as the card that fired, or when the matched fragment sits inside that text's own frontmatter `forbidden:` block — otherwise the card defining a pattern is the one file that pattern makes unwritable (2026-09-06 incident; §10 covers how to write the pattern so this comes up less).

Rule B applies when the target is a card of a registered vault — `.md`, no `_`/`.` prefixed path part, not the memory index, by the same filter `memsearch` uses. The prospective post-write text is checked by `card_lint.check_card`, the same single-card check the CLI scan runs: FAIL (a missing required field for the card's type, broken frontmatter) blocks and names the missing fields with a line to copy; WARN only advises through `additionalContext`. For an `Edit`, the post-write text is the current file with one `old_string`→`new_string` substitution applied; when `old_string` is not in the file, nothing is judged and the call proceeds — a guessed result would block a card nobody wrote. A generic card whose frontmatter carries no date is not blocked when the date can be read out of its body or its filename, since that is the same derivation the CLI scan applies; the git-history source is not available here, because the gate judges text that is not on disk yet.

A block is audited to `_GATE_LOG.jsonl` as `write_block` with the rule and either the `decision` key or the `card_path`, never the content itself. One `(rule, file, content digest)` blocks once per session, so an assistant that cannot satisfy a ruling is not denied the same write forever.

### What it still does not catch

- **A shell redirection or heredoc** (`echo … > card.md`, `python - <<PY`) writes a file without any file-writing tool, so this gate never sees it. That path is covered by the `no-bare-redirect` scar card on the action gate's command matching, not here.
- **Diff-shaped tools.** A tool that takes only a patch body (`apply_patch`) is not in `WRITE_GATE_TOOL_NAMES`: a diff's context and removed lines would match `forbidden` patterns the write never adds.
- **Content past `WRITE_GATE_MAX_CONTENT_BYTES` (256 KiB)**, an unreadable target file, and an oversized target all fail open rather than spend the hook's deadline.
- **Rule B judges the text, not the intent**: content that already FAILs stays writable if the write does not change that (an `Edit` whose post-write text cannot be determined is not judged), and a card that was already malformed is not repaired by the gate.

### Self-verification

```powershell
python adapters/claude/pretooluse_gate.py --selftest
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

`dream.mode` decides who remembers. The default, `piggyback`, needs no scheduler and no habit: `SessionStart` compares `dream_state.json`'s last completion against `dream.interval_hours`, and when the gap is wide enough it starts `epitype/dream.py --scheduled` as a detached, low-priority process and returns immediately — the session never waits, and a pid-bearing lock (stale after 30 minutes) keeps concurrent sessions from starting a second one. Operators who prefer a real schedule use `graft install --dream nightly [--at HH:MM]`, which registers one daily system task and stops the piggyback trigger so the same day is not swept twice; `graft doctor` prints the mode and the last completion, and `graft uninstall` unregisters the task. `off` disables both.

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

### Self-verification

```powershell
python adapters/claude/stop_gate.py --selftest
python adapters/claude/pretooluse_gate.py --selftest
python epitype/memspec.py --selftest
```
