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
`codex_exec` and `sdk-*`, meta records, sidechains, compact summaries and duplicate
Codex event records are excluded. Leading quoted `《…》` or XML context is removed
from typed input; a command's `<command-args>` carries its typed arguments.
Host instruction and continuation boilerplate is excluded. Human queues retain
the host's queued prompt, following the existing queue attribution rule.

Rows retain full owner text, role, timestamp, session ID, message UUID when the
host provides one, source path, physical byte offset and the original row's
SHA-256. Matched question text is stored separately, including for typed answers.
The preceding assistant text is limited to its last 500 characters. Assistant
context is never indexed as owner text. Every physical occurrence is retained:
an archive and its original can produce separate hits with separate locators.

Search uses SQLite FTS5 with its trigram tokenizer. NFKC, case folding and
punctuation/whitespace removal follow the reference finder; every search word
must occur in the owner text or its question. One- and two-character searches
use literal normalized substring checks because trigrams cannot index them.
Results are newest first, with optional inclusive ISO date and role filters.

Each source stores size, nanosecond mtime, last complete byte offset, prefix
SHA-256 and resumable parser state (session origin, pending questions, previous
assistant context). Unchanged files require metadata checks only. For a changed,
growing file, update hashes the old prefix and parses only new complete JSONL
rows when it matches. The prefix read is necessary to detect an earlier edit
combined with growth; size and mtime alone cannot prove an append. A shrink,
same-size modification or changed prefix replaces that source's indexed rows.
An unfinished final row waits for its newline. Metadata-preserving edits cannot
be detected by metadata-only unchanged checks; use `build` after such edits.

Zip members and standalone zstd archives are treated as immutable and indexed
once per source path. Replacing an immutable archive requires `build`. Archive
offsets refer to decompressed bytes, and zip locators use `archive.zip!member`.
`zstandard` is optional; missing support increments `skipped_zst`. There are no
network or model calls and no required third-party packages.

Each source is committed independently. Unreadable archives/files are listed in
`errors`, malformed message rows in `unreadable_rows`; other sources continue.
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
