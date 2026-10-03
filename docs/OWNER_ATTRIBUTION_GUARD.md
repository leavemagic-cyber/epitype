# Source admission for authority and operational restrictions

The existing `rule_provenance` verifier now admits prose claims as well as rule
cards. `Stop` checks attributed quotations against original messages before its
rewrite/deduplication exemptions. `PreToolUse` checks new Markdown in structured
Write/Edit calls, reconstructed patch envelopes, literal Python Markdown writes,
and Task/Agent/message dispatch
fields. A block is a denied write/dispatch or a blocked turn, never a permission
grant. Both adapters use the same verifier.

## Original authorship

The verifier scans original JSONL from byte zero, in physical order. Compaction
summaries, metadata, delegated messages and tool-result user envelopes cannot
establish human authorship. It reuses `capture.owner_reply_parts`: the prefix of
the existing `<`, `《`, `<-` and `<=` reply formats is cited material; the suffix
is the human reply. The first assistant occurrence, a cited prefix, an unreadable
prefix, malformed row, rewritten examined prefix or exhausted verification time
cannot pass as a human quotation. An assistant's normal quotation of genuine
human words passes without requiring an annotation, including file writes and
dispatches. Appending records after the examined complete prefix is harmless;
that prefix is reread and hash-checked. A partially written tail is not evidence.

The generated compact map emits `owner_reply=... | cited_text=...`, putting the
reply first before byte clipping. This preserves the physical source line. It
does not rewrite the host's model-generated compaction summary.

## Attached source locators

Attributed quotations are automatically resolved against the event's original
transcript. Optional single-line JSON annotations can supply another exact
original source. Supplied annotations are always validated. Surround JSON with
the literal opening comment text
`<!--` + ` EPITYPE CLAIM ` and closing text ` -->`. Examples below are schema
examples, with paths and words replaced by the actual original source.

```json
{"kind":"owner-directive","text":"EXACT_HUMAN_WORDS","quote":"EXACT_HUMAN_WORDS","source":{"path":"original.jsonl","line":42,"message_id":"original-message-id"}}
```

At least a physical `line` or `message_id` is mandatory. If both are supplied,
both must match. Optional `record_sha256`, `byte_offset` and `role` must also
match. The locator must identify the first original occurrence, and the text
must equal the quotation; interpretations need AI attribution. Paths are relative
to the target file's directory, or absolute. An unrelated true quotation cannot
authorize a second claim or an adjacent prohibition. Owner headings and prose
beside annotations still undergo admission checks. Actual code examples and
inline code literals are data; a generic fence around owner prose is not an
exemption. Technical descriptions and ordinary task constraints with no owner
attribution or newly asserted operating rule do not need claim metadata.

## AI-derived restrictions

A newly asserted operational rule needs explicit AI authorship, `challengeable:true`,
the same subject in its text and observations, and source-bound dated evidence.
The human-readable label should say it was proposed by the lead and can be
challenged. A label without evidence does not pass.

```json
{"kind":"ai-derived","author":"lead assistant","challengeable":true,"text":"SUBJECT threshold proposal","subject":"SUBJECT","basis":[{"quote":"EXACT_OBSERVATION","observed_at":"2026-10-03T00:00:00+00:00","valid_until":"2026-10-03T01:00:00+00:00","source":{"path":"original.jsonl","line":43}}],"rationale":"Why this observation supports this provisional restriction"}
```

Observation timestamps and their explicitly stated expiry must occur in the
cited original message, use timezones, and remain valid. There is no universal
freshness interval invented by this guard. A duration cancellation threshold is
compared in seconds against the upper observed range for the same subject; a
ten-minute timeout contradicting a normal fifteen-to-twenty-five-minute range is
rejected. An unexplained cancellation ratio is unverified.
Conditional cancellation directives are checked even without a leading phrase
such as "new rule"; an ordinary report of an earlier cancellation is not one.
Cancellation also requires `cancel_condition` in both the proposed text and
source evidence, plus source-bound `checkpoint`, `redo_cost` and `recovery`.
Elapsed time or silent logs alone cannot justify discarding valid work.
Non-duration restrictions need a rationale; its semantic soundness still requires review.
Existing AI-derived rule cards use the same claim schema in `restriction_evidence`
when their text introduces a restriction. Existing owner-directive cards also verify
earliest authorship through their original source locators.

## Boundaries and diagnostics

* Hook admission shares one two-second budget across its calls. Claim evaluation
  runs in an owned subprocess with a hard CPU/IO timeout; timeout kills and reaps
  that worker. Ordinary prose without authority or operating rules returns
  without source IO. Timeout does not invent an authority claim in code data;
  unresolved authority remains UNVERIFIED. Direct helper calls additionally
  check deadlines throughout scanning and classification.
* Transcript admission streams bounded rows through the initial snapshot without
  the former 64 MiB file-size ceiling. It checks the earliest physical occurrence
  and rereads the examined prefix. Multiple attached claims sharing one file scan
  its prefix once, then reread/hash-check cited rows. There is no persistent
  positive cache. An unreadable prefix, rewrite or exhausted deadline still blocks
  a claim. Existing explicit card-verification resource budgets remain supported.
* File edits use linear counts of lines and their owner-heading context instead
  of quadratic sequence matching. Unchanged prose is skipped; moving an owner
  heading onto old prose changes its authority context and is checked. Registered memory-card
  frontmatter continues through the existing card lint/provenance path. A fake
  frontmatter wrapper in an ordinary plan does not exempt its prose.
* Lines explicitly labelled as historical error reports or cited data are data,
  not new authority claims. A reported error must not be reused as an operative
  instruction. That distinction, implicit paraphrases, arbitrary semantic
  entailment and the truth of self-reported observations still require review.
* Python `-c` and here-documents with imported literal Path targets, `write_text`,
  aliases, bounded string addition, UTF-8 reads and replacement use the same gate.
  The submitted Python code is parsed, never executed by the guard. Queries and
  unimported lookalike methods are not treated as file writes. Dynamic Python,
  opaque shell writes and PowerShell redirections are outside the recognized
  write shapes. Host hook registration, delivery of event
  fields and external hook timeouts must be checked during deployment.
* The guard never modifies originals, removes files, changes the host config,
  authorizes external actions or overrides higher-priority instructions.
* Unexpected write-review exceptions preserve the existing fail-open contract
  and emit `write admission UNVERIFIED` with the exception type. They are not a
  successful verification.

## Verification and installation

Run `python -u tests/run_all.py --jobs 1` in an isolated checkout with TEMP/TMP
outside the source tree; its helper isolates
the home/config for hook selftests. Replay an explicitly named original with
`python tests/owner_attribution_regression.py --real-transcript <original.jsonl>`.
The regression packet also invokes the real Stop/PreToolUse subprocess entry
points with Claude and Codex arguments, not only helper functions.
The explicit incident replay freezes the full original prefix through its reply
in test-owned temporary storage and hash-compares it to the named original.
This preserves every earlier message and assertion while an active logger may
append. The temporary payload is removed with the test fixture. A separate test
asserts that stable-prefix appends pass while prefix rewrites remain blocked.
Repeat the incident command with `--admission-seconds 2` for the daily deadline.

Installation belongs to the lead after reviewing the commit and test artifacts:

1. Preserve the live checkout/config and inspect both hosts' current hook
   registrations. Prepare a separate, immutable checkout of the reviewed commit.
   Switching branches at a configured live `repo_root` activates unreviewed code.
2. Test that checkout with isolated home/config and verify its commit and files.
3. Use the existing `python -m epitype relocate --to <reviewed-checkout> --dry-run`,
   review it, then run the same command without `--dry-run`. Its transaction backs
   up the config, changes only `repo_root`, runs doctor and rolls back on failure;
   an existing nightly task also follows the reviewed repository. Existing dynamic
   shims then load its adapters. Repair registration gaps through the existing
   installer after review, not a parallel hook.
   This is a global root selection, not a per-session trial: other sessions load
   the selected adapters on their next hook call, and nightly follows that root.
4. Verify one false attribution is blocked and one genuine quotation passes in
   each host. Confirm write/dispatch denials and compact-map separation. Read back
   the configured root and loaded code. A passing isolated test is not deployment
   evidence. Restore the backed-up config to roll back the root selection.

This change is source preparation until those installation checks are performed.
