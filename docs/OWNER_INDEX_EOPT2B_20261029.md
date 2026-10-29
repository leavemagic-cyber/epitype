# Owner index EOPT2B — 2026-10-29

Measured code: `540983eef7c089edfad56ce6e65260e8191ea813`; Python 3.13.15, SQLite 3.50.4; optional zstandard available: True.

This supersedes the extraction, duplication and prefix-policy findings in `OWNER_INDEX_BENCHMARK_20261029.md`. The original 372,084,736-byte database was deleted before rebuilding. Only one full rebuilt database is retained, in this worktree at `.epitype/eopt2/owner-index.sqlite3`.

## What the original records established

Three original Codex response-item rows that had produced roughly 100K-character owner hits actually contained 866, 841 and 848 input-text blocks. They were guardian action-review envelopes, carrying retained instructions and numbered earlier user/assistant/tool turns. The initial envelope, later history-delta envelope and headerless later deltas all belong to a `guardian_review` session (also recorded as `source.subagent.other=guardian`). They contain no new owner utterance. The entire review session is excluded before joining its text.

Older Desktop-origin programmatic sessions were also identified by their `session_meta.source=exec`; a Desktop originator does not make their generated market/reflection packets typed owner input. This is provenance filtering, not a prose/length heuristic.

Known browser/environment/system/internal/pasted-context blocks, attachment lists, image opening and closing tags, and repeated request delimiters are removed from actual input. Other long pastes remain uncapped. Existing inline quote/reply attribution and the distinct U/Q/S roles are retained; S denotes an AI-written option selected by the owner, not verbatim owner prose.

The full inventory also found 5,405 heartbeat texts containing 16,752,743 characters and 409 subagent notifications containing 1,520,823 characters. These automation/report packets, hook/command output, app/plugin state and relayed Codex instructions are excluded. The 186 older IDE setup headers retain only their request. Human question-reply envelopes retain answers with questions stored separately; voice envelopes retain only the new input and exclude transcript_delta. Recognition happens before inline reply attribution, so an arrow inside an automated report cannot turn its tail into an owner utterance. Actual outer closings take precedence over escaped nested examples.

Concrete real before/after: at byte 61,502,459 in the inspected parent Codex transcript, 738 characters of joined input text contained an attachment list, request wrapper and split image tags. The copied raw record was 334,775 bytes including image input. Actual CLI build/find now returns exactly the 10 characters `這一堆都已經登入好?`. A separate 424-character browser wrapper retains only its 19-character new request. No ambient login URL or token is copied into this report. An actual scheduled dream on copied data retained a genuine 2,628-character owner handoff exactly.

## Final real-data measurement

| Metric | Completed rebuild | Final incremental update |
| --- | ---: | ---: |
| Unique text rows | 18,677 | 18,677 |
| Text characters | 6,377,281 | 6,377,281 |
| Original locators | 24,120 | 24,120 |
| Source entries | 6,841 | 6,841 |
| Database bytes after close | 84,901,888 | 84,901,888 |
| Wall seconds | 4,231.619 | 4.310 |
| Transcript bytes returned to parser | — | 5,365,611 |
| Small boundary bytes read | — | 57,344 |
| Total transcript-content bytes read | — | 5,422,955 |
| New / appended / reread sources | — | 0 / 7 / 0 |
| Errors / unreadable rows / missing zstd | — | 0 / 0 / 0 |

Unique extracted text occupies 9,520,293 UTF-8 bytes; the database also contains search structures, locators and separately labelled short AI context. The three largest retained archive pastes were checked against original zip records: each has one input-text block, source=vscode and zero numbered replay labels. They are retained, including the largest 40,612-character draft, as requested.

The rebuild time includes inspection, extraction repairs and resumptions; it is not a clean uninterrupted build benchmark. Per-source committed checkpoints survived. The initial replay-header-only check missed later delta shapes, so 710 early Codex checkpoints were redone while 462 Claude checkpoints survived; subsequent metadata-based repairs preserved all valid offsets. A concurrent large inspection caused one SQLite lock error; the failed source was resumed with readers deferred until the writer exited. The final metadata-only pass removed intermediate machine-origin locators and image-tag leftovers, then optimized FTS and vacuumed the same database. No second full transcript rebuild or full database evidence copy was made.

The last continuation command took 961.943 seconds; the final metadata/compaction pass took 66.730 seconds. That pass identified 4,650 machine source entries, removed 493 intermediate locators and cleaned 332 closing-image text rows. Completed source offsets cover 41,777,495,971 uncompressed bytes.

The subsequent observed-wrapper pass repaired 6,480 texts and removed 7,548 machine locators in 30.226s. Another 314 original locators were read back in source/offset order and SHA-256 checked before removing clipped old machine-report fragments. The final inventory has zero recognized host tags or IDE wrappers in owner text.

Schema v2 stores exact extracted text once, with separate original locators. The most reused text has 434 locators. Search returns its newest matching occurrence, with role/date/question filters applied before selecting that occurrence. Exact text equality is used; different genuine utterances are not merged by similarity.

Growing append-only transcripts require one hash of up to 4 KiB immediately before the old offset, parsing the append, and saving the new small boundary. A failed boundary check, shrink or same-size modification rereads only that source. An edit outside the window combined with growth can be missed; this is the selected policy. Unchanged sources use metadata only. Byte counts describe parser/decompressed content plus boundary reads, not OS disk traffic or archive directory metadata.

## Real commands and readback

The measurement scripts run actual `python -m epitype owner-index build` and `update` with the worktree index. Source roots are the default Claude transcripts/backups and Codex sessions/archives plus the same cold-storage directory as the preceding benchmark. A worktree EPITYPE_CONFIG carries the root list to avoid the Windows command-line-length limit.

```
python .epitype/eopt2/measure_eopt2b.py
python .epitype/eopt2/resume_after_lock_eopt2b.py
python .epitype/eopt2/finalize_eopt2b.py
python .epitype/eopt2/repair_wrappers_eopt2b.py
python .epitype/eopt2/repair_residual_originals_eopt2b.py
python .epitype/eopt2/measure_final_eopt2b.py
python .epitype/eopt2/audit_eopt2b.py
python -m epitype owner-index find 記得多用codex --limit 3 --index .epitype/eopt2/owner-index.sqlite3
python -m epitype owner-index find 測試都是騙自己 --limit 3 --index .epitype/eopt2/owner-index.sqlite3
```

These scripts record this run and its resumptions; a fresh reproduction using the final code needs only one build followed by update, not the historical repair sequence. Detailed metrics are in `measurement-eopt2b.json`, original digest checks in `audit-eopt2b.json`, and exact search records in `find-eopt2b.json`, all beside the retained index.

Readback: 8/8 sampled original row hashes match (U/Q/S and ordinary/archive occurrences where present); SQLite integrity=ok, FTS external-content integrity=ok. Orphan texts/locators=0/0; machine-source locators=0; replay-header texts=0; file-list wrapper texts=0; known host tags=0; IDE wrapper texts=0.

Actual `python epitype/dream.py --scheduled` ran once with an isolated home/config and copied real cards/owner row: generated views and the default owner index were present in the pack. Its index step took 0.160s and retained the complete 2,628-character paste. Copied-source CLI updates demonstrated identical text sharing (one text, two locators; 0.046s and 19,366 bytes), accepted an outside-window edit plus append as specified, and reread exactly one source after an inside-window whitespace change (0.054s and 52,889 bytes). The actual attachment-row build/find produced one ten-character text in 0.282s. These are actual copied-data commands, not test suites. No test suite was run.

A final actual CLI build on six copied raw records retained four new human inputs (91 characters) in 0.158s: the image question, IDE request, typed question-box answer and new voice input. Searches for the heartbeat payload and subagent report returned no hits. Appending an original nested escaped delegation record then added zero text/locators (0.042s, 10,460 bytes).

The nightly updater is a separate post-views block, calls the default index path with configured roots, reports failures in the pack/state, and does not run in dry-run mode. Product changes remain in this worktree; no merge, push, tag or live-hook deployment was performed.

## Requested top-three search readback

Both commands requested three hits and returned one unique direct owner hit each. There are no second or third hits in this index snapshot. Full text, source paths, offsets and hashes remain in the local JSON.

### 記得多用codex

1. 2026-10-29T07:33:25.055Z, Q, 9 characters, 1 locator(s), byte 33190782: 記得多用codex

### 測試都是騙自己

1. 2026-10-29T07:41:41.274Z, U, 18 characters, 1 locator(s), byte 33640710: 測試都是騙自己，直接上線看效果最實際
