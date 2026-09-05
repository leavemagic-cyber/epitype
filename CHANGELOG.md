# Changelog

## Unreleased

The hot-path release. Measured on a machine at 100% CPU with two real vaults
(158 + 294 cards): a recall or gate call that took 4.5–5.5 s — past both the
host's and its own 3 s deadline, so it silently injected nothing and let a
destructive command through — now takes 1.2–1.7 s end to end, and the work
inside the interpreter fell from 4.0–4.6 s to about 0.4 s.

- The vault scan reads directory entries and never resolves a path: symlinks and
  Windows junctions are recognised from the entry's own attributes, and `_`- or
  `.`-prefixed parts are never entered. Inside its grace window the stale check
  reads nothing; after it, the stored path/mtime/size manifest is compared, so
  deletions, renames, backdated additions, and size changes cannot stay hidden.
  Results carry `card_path`, so no caller resolves the vault again. The selftest
  lists 300 cards and asserts that no path is resolved.
- The action gate finds trigger cards through a manifest cache
  (`<vault>/.epitype/gate_triggers.json`) and re-reads only cards that changed.
  Its regex validator rejects only shapes that can backtrack exponentially
  (nested unbounded repetition, alternation under repetition, backreferences);
  adjacent repetitions and bounded groups are accepted and the length limit is
  1024. A card the gate cannot use is named to the model once per session rather
  than skipped silently. A matched rule stays a deny when audit logging is
  contended or the advice exceeds the output ceiling.
- Recall lines say each fact once: a name the path already spells and the
  "owner … auto-captured" label are dropped (about 40% fewer bytes per pinned
  line). The advisory and vault legend are sent once per session per distinct
  legend; compaction clears the session's recall markers, so they — and any
  correction or ruling injected before it — return afterwards. A capture that
  cannot take the index lock ages the index so the next prompt rebuilds it.
  Every injected block that a budget cuts ends with the count of pieces left
  out instead of silently skipping middle pieces.
- One frontmatter reading for the index, the lints, and the gate
  (`memspec.split_frontmatter`, `parse_scalar`, `join_block_scalar`): the BOM,
  CRLF, `...` document ends, duplicated keys (first wins), `|-`/`>-` block
  scalars, and a quoted `#` now read the same everywhere;
  `tests/frontmatter_consistency.py` proves it card by card.
- Decision lint: a key whose every card is retired warns rather than fails,
  keeping the exam contract; supersession chains are valid unless they loop; a
  replacement must share the decision key.
- Installer: doctor accepts a `python` on PATH or any existing interpreter in a
  registration instead of the exact path of the interpreter running doctor;
  host-file backups keep only the newest three; `hook_trust` prints
  `UNVERIFIED` (exit 1) instead of a `SKIP` that read as green; `python -m
  epitype` gains a `__main__` guard; a tag publish requires `package.json` to
  carry the tag version; `tests/run_all.py --jobs N` runs selftests in parallel
  on CI.
- Recall terms drop particle bigrams and punctuation fragments (「我們」,「的虛」,
  「——」), which matched every card and pushed the rule card that answered the
  prompt below the window; a ruling matched only in its body (the assistant's
  question, not the owner's answer) is an ordinary hit rather than a pinned one.
- Hook commands are registered without quotes whenever the paths allow it. Codex
  on Windows runs hooks through `cmd.exe /C`, which strips the first and last
  quote of a line that starts with one, so the fully quoted form the
  2026-09-04 batch introduced failed every Codex hook before Python started. A
  path that needs quoting also gets a `commandWindows` form wrapped in one outer
  pair of quotes. Doctor prints each hook's wall time and warns when
  `repo_root` has uncommitted changes.
- Hook registrations allow ten seconds instead of three (owner ruling
  2026-09-05); the hook's own deadline is nine. Existing installs pick this up
  by re-running `install`, and Codex hosts then need the four hooks trusted
  again because their definition changed.
- From the 2026-09-04 batch, kept as found: incremental index builds use mtime
  and size; owner captures and compact maps share the governance vault; compact
  maps are per session and bounded; the installed package provides a unified
  `epitype` command and CI exercises the installed wheel; Codex trust requires
  exactly the four supported registrations.
- The owner-quote capture core moves into `epitype/capture.py`, and
  `epitype/harvest.py` adds an inventory-plus-replay harvest whose manifest
  skips files that have not changed. `epitype/card_lint.py` checks
  required/optional frontmatter fields per card type across 11 types and
  prints a one-line SessionStart summary.
- Recall now places a matching current decision card ahead of corrections and
  rulings, carrying the owner's own words, and SessionStart injects a
  "current rulings" block per vault (compaction-aware). Flat frontmatter-field
  reading converges into `memspec.frontmatter_fields`, shared by
  `decision_lint`, `card_lint`, and all three hooks; the hot hook path no
  longer loads `decision_lint` or `argparse`.
- A Stop decision gate (5th hook event, Claude and Codex) compares the turn
  against the owner's current decision cards before it ends: a forbidden-card
  hit, or a question repeating the same card's alias twice or more, blocks
  and asks for a rewrite per the ruling. `stop_hook_active` is never blocked,
  each card blocks at most once per session, and a block is logged to
  `_GATE_LOG` with `kind=stop_block`.
- A 72-card, 51-question synthetic recall regression exam
  (`tests/recall_regression.py`, threshold 0.882) runs alongside questions
  against the real local vault. An offline alias batch
  (`epitype/alias_batch.py`) exports cards missing aliases and applies
  reviewed aliases back — additions only, NFKC-deduplicated, preserving
  BOM/CRLF, and a collision with a decision card blocks only that alias.
- The exam runner gains `stop` (runs the real stop gate), `abstention`
  (`top_k_empty`), and recall's `pinned_contains` kinds; the sample corpus
  grows from 12 to 16 questions, and the local 330-question corpus (kept out
  of the repo) passes in full.
- PreToolUse 寫檔內容閘——Write/Edit/MultiEdit（含 Codex 對應工具）的寫入內容命中
  現行決策卡 `forbidden` 即 deny 並附裁定原話；寫進記憶庫的卡若缺該型別必填欄位即
  deny 並列出欄位＋範例；WARN 只提示；同 session 同內容只擋一次；>256 KiB 放行；
  `_GATE_LOG` kind=write_block；FAILURE_MODES §11。
- AI 承諾帳本——Stop hook 從回合結尾訊息抽「我會／稍後／下一步／I'll…」承諾句寫入
  `<治理 vault>/.epitype/commitments.jsonl`（排除轉述、疑問、已完成；digest 去重；
  每回合 ≤5）；SessionStart／PreCompact 顯示未兌現承諾；`epitype commitments <vault>
  --list|--close|--purge-closed`；FAILURE_MODES §12。
- `epitype dream <vault>…`——不呼叫模型的離線整理審核包：缺別名卡、卡片型別 FAIL/WARN、
  殭屍待辦、AI 未兌現承諾、草稿待審、裁定鏈（superseded／缺 owner_quote／缺 forbidden）、
  事件卡老化、近 7 天新增，末尾依規則排「下一步」與指令；寫到
  `<vault>/.epitype/dream_pack_<日期>.md`（`--dry-run`／`--json`／`--since`）。
- owner 事件捕捉精準度——判定改為子句層（先切子句、去掉反問子句，其餘子句須有決定性內容）、
  `classify` 為線上／回放共用的唯一判定、一句一張卡；移除「依照你建議」「怎麼還」「你可以…嗎」
  「我錯了」「跟我說／白話」、一詞式應答與助理長段分析的誤抓；`tests/capture_precision.py`（合成句
  selftest＋本機真句量測：254 句 precision 0.44→0.81、保留率 0.85）；`harvest --reevaluate <dir>
  [--apply]` 重評隔離卡（正向：草稿放回）；加 `--quarantine-drops` 反向模式，把線上已收的
  drop 隔離到 `_drafts/captured_dropped`；兩者皆 `--apply` 門控、不刪；FAILURE_MODES §13。

## v1.1.0 — 2026-09-03

The token-economy release. Every line Epitype injects is paid for once as output and
again on every later turn as re-read context, so this release measures that cost on real
prompts and cuts it, and it closes the gaps where an owner's own words were being lost.

### Injection cost (measured on 40 real prompts)

| | v1.0.0 | v1.1.0 |
|---|---|---|
| Recall injection per prompt | 3.6 KB / 11 lines | **2.3 KB / 8.0 lines** |
| Absolute paths as a share of it | 32% | 17% |
| Session start (non-governance project) | 10 KB, hitting the budget cap | 7.6 KB |

- Recall prints one `vaults:` legend line and `V1/relative` paths instead of repeating an
  absolute vault path on every hit; descriptions are truncated; at most two body-only hits
  per vault; the whole block is capped, with pinned corrections and rulings first (U29).
- Session start injects the working directory's own vault plus the governance vault only.
  Another project's index and ledger no longer crowd the budget (U29). **Behaviour change
  for multi-vault installs**: the governance vault is the configured vault that holds the
  working ledger, and a vault reached from the working directory is always kept. If no
  configured vault holds a ledger, every configured vault is injected as before.
- Narration between tool calls ("that failure was my path typo, rerunning with C:/...")
  costs output tokens and then re-read context on every later turn while telling the owner
  nothing. The PreToolUse gate now names any such segment in one line without touching the
  permission decision; `epitype/narration_meter.py` reports per-session and per-day totals
  for release review. A 36-hour local baseline held 9,273 such segments (U28).

### Owner's words stop getting lost

- Corrections: a sentence where the owner corrects the agent is captured verbatim into
  `<vault>/corrections/`, indexed immediately, and pinned first at recall with a visible
  marker, ahead of whatever plan card matched better lexically (U25).
- Rulings: when the previous assistant turn explicitly asks the owner to decide, the reply
  is captured with the question it answers into `<vault>/rulings/` and pinned the same way
  (U26). A bare mention of the word in a report, or a request phrase inside quotes, no
  longer counts as a request (U30, U31).
- Captured cards carry the owner's words in their description, so the injected line is
  self-contained (U29).

### Stale to-do items

- `epitype/pending_lint.py` names to-do lines that have an entry but no exit: no runnable
  `verify:`, not closed, older than the configured window. Session start announces the
  count in a single first line so the budget cannot drop it (U25).

### Fixes

- Windows CI: four legacy-index selftests failed only on GitHub's Windows runners, which
  hand `tempfile` an 8.3 short path under the runner profile that never equals the
  product's resolved output. Every selftest fixture root is resolved, and `tests/run_all.py`
  fails on any unresolved one (U27).
- `.github/workflows/ci.yml` runs on master pushes, `v*` tags, and pull requests instead of
  every push.

### Found by pre-release adversarial review

An independent review of the changes above found five defects, each reproduced before it
was fixed and now covered by a selftest:

- A correction or ruling that matched only in its body was discarded by the weak-hit cap,
  and pinned lines were exempt from the total cap, so recall could exceed its own limit.
  Card kind is now decided before any cap, the cap covers every line, and pinned cards are
  searched in a window three times the ordinary one so a correction ranked below the
  top-k still surfaces.
- The `vaults:` legend could be dropped by a tight budget while `V1/...` hits survived,
  leaving aliases nothing could resolve. The legend now shares the required first block,
  and a path that cannot be made relative is printed in full rather than given an alias.
- A working-directory vault that also appeared in the configured list was dropped from
  session start.
- A request phrase inside Markdown backticks still counted as a request for a ruling.
- Captured cards are persistent, indexed, and re-injected later, so credential-shaped
  text (`token=`, `sk_...`, private-key headers, JWTs, `user:pass@host`) is now refused at
  capture time instead of being copied into the vault.
- Narration markers in the temp directory are swept after a day instead of accumulating.

## v1.0.0 — 2026-09-02

First public release: local recall over Markdown memory cards (SQLite FTS5), a card-driven
PreToolUse safety gate, a mechanical compaction map, decision-card lint, working-ledger
budget gate, owner-grant capture, and the `epitype-graft` installer for Claude Code and
Codex CLI.
