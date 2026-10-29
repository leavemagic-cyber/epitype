# Owner input index

`python -m epitype owner-index build` creates `owner_index.sqlite3` beside the
Epitype configuration (normally `~/.epitype/`). `update` resumes that database;
`find` opens it read-only and never creates a missing database. All three accept
`--index PATH`, so tests and measurements can stay outside live host storage.

```
python -m epitype owner-index build --root transcripts --root archives --index scratch/owner.sqlite3
python -m epitype owner-index update --root transcripts --root archives --index scratch/owner.sqlite3
python -m epitype owner-index find 中文 原話 --after 2026-10-01 --role U,Q --limit 10 --index scratch/owner.sqlite3
python -m epitype source --find 中文原話 --index scratch/owner.sqlite3
```

With no `--root`, discovery uses Claude project-level `.jsonl` and
`.jsonl.backup*` files, Codex sessions recursively, and Codex archived sessions.
`--root` is repeatable and replaces those defaults; a directory is searched
recursively for transcripts, backups, `.jsonl.zst` and `.zip` files. Register
external cold storage by adding its directory to the command, or set a complete
list in the configuration's `owner_index_roots` array. No machine-specific cold
storage path is assumed. Use the same roots on subsequent updates.

The index consumes `transcript.source_record` and `ask_tool_uses`. It preserves
typed messages (U), human queued messages (Q), and structurally matched
question-box selections (S). S text is an AI-written option the owner selected,
not a verbatim utterance. Typed question-box answers retain U. Script origins
`codex_exec` and `sdk-*`, Codex `session_meta.source=exec` (including older
Desktop sessions whose originator alone does not identify the script), meta
records, sidechains, compact summaries and duplicate
Codex event records are excluded. Leading quoted `《…》` or XML context is removed
from typed input; a command's `<command-args>` carries its typed arguments.
Host instruction and continuation boilerplate is excluded. Codex guardian
approval requests (both initial history and subsequent history deltas) are
machine replay envelopes, even though the transcript assigns them a user role.
They are rejected before their numbered `[N] user:` blocks are joined. Those
blocks are old turns, not new input. Browser/environment context, system
reminders, pasted context, internal context, file-mention lists and image tags
are removed; repeated `## My request:` delimiters carry no owner words.
Older `# Context from my IDE setup:` blocks (active file/open tabs) retain only
the text after `## My request:` or `## My request for Codex:`; an IDE-only block
has no new owner text.
Other long pasted text has no length limit and remains searchable, including
ordinary XML that is not a host context tag. For U and Q, copied
assistant text before inline `《`, `<`, `<-` or `<=` reply markers is removed using
the existing `capture.owner_reply` rule. Only the following human annotations
are indexed; S continues to retain the selected AI option's distinct role.

Known machine packets also include heartbeat/scheduled automation, hook and
command output, subagent notifications, relayed Codex instructions and ambient
app/plugin state. Their bodies are not newly authored owner messages. Unknown
XML remains a paste, rather than being discarded merely for starting with `<`.
Codex question-reply envelopes retain only their `answer` strings, with the
AI-written questions stored separately; realtime voice envelopes retain only
the new `input`, excluding `transcript_delta`. Text blocks are joined before
wrapper removal so a tag split around an image cannot leak its closing tag or
discard the new request between blocks.

Rows retain full owner text, role, timestamp, session ID, message UUID when the
host provides one, source path, physical byte offset and the original row's
SHA-256. Matched question text is stored separately, including for typed answers.
The preceding assistant text is limited to its last 500 characters. Assistant
context is never indexed as owner text. Schema v2 stores each exact extracted
text once in `messages`, with physical occurrences in `locators`. Backup and
archive copies share the same text and FTS entry. `find` returns the newest
matching locator for each text plus its total `locator_count`; role/date and
question matching apply to individual locators before deduplication. A source
reread replaces only its locators; text still referenced elsewhere survives.
Use `build` once to replace a schema-v1 index; `update` rejects incompatible
versions rather than silently mixing old extraction and new source offsets.

Search uses SQLite FTS5 with its trigram tokenizer. NFKC, case folding and
punctuation/whitespace removal follow the reference finder; every search word
must occur in the owner text or its question. One- and two-character searches
use literal normalized substring checks because trigrams cannot index them.
Results are newest first, with optional inclusive ISO date and role filters.

Each source stores size, nanosecond mtime, last complete byte offset, boundary
SHA-256 and resumable parser state (session origin, pending questions, previous
assistant context). Unchanged files require metadata checks only. For a changed,
growing file, update hashes at most 4 KiB immediately before the old offset and
parses only new complete JSONL rows when it matches. It saves another small
window at the new offset. A shrink, same-size modification or changed boundary
replaces that source's locators by rereading only that file. This follows the
append-only transcript contract: a simultaneous edit outside that boundary
window and append can go unnoticed. Use `build` after editing historical bytes.
An unfinished final row waits for its newline. Metadata-preserving edits cannot
be detected by metadata-only unchanged checks; use `build` after such edits.
`bytes_read` counts transcript bytes returned to the parser plus boundary reads,
including an unfinished row; archive values are decompressed bytes, not OS disk
traffic or zip directory metadata. `messages`/`text_characters` count unique
extracted text; `locators` counts its original physical occurrences.

The nightly dream calls `owner_index.update()` after regenerating views, using
the default index beside the selected configuration and its configured source
roots. Its result is included in the pack; failures mark the run incomplete.
`--dry-run` does not update the owner index.

Zip members and standalone zstd archives are treated as immutable and indexed
once per source path. Replacing an immutable archive requires `build`. Archive
offsets refer to decompressed bytes, and zip locators use `archive.zip!member`.
`zstandard` is optional; missing support increments `skipped_zst`. There are no
network or model calls and no required third-party packages.

Each source is committed independently. Unreadable archives/files are listed in
`errors`, malformed rows (including non-message records) in `unreadable_rows`;
other sources continue. Full builds validate every JSON row before role filtering
so a corrupt tool/event row cannot silently make a broken source look clean.
A failed source rolls back its new rows and retains its previous snapshot for
retry. Existing rows for sources absent from the supplied roots remain until a
full build, so an index can retain historical locators to moved files.

Indexed hits always carry `verified: false`: neither the current source nor its
unchanged state has been checked by `find`. The original `source SOURCE.jsonl`
command still reads the raw bytes and computes its own digest. Use a hit's
`byte_offset` and compare its `record_sha256` there; selection prompts and
authority still require the existing provenance validation. For archives,
inspect the named member's original decompressed row. A search miss never
proves global absence, and a hit never proves a current decision or grants
authorization.
