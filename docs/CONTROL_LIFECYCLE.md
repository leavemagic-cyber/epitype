# Browser resource leak detection

Owner decision, 2026-09-27: Epitype records the browser resources an agent
creates, records verified closures, and reminds the agent when it switches to a
non-control tool or at the next user message while a resource remains open.

Owner decision, 2026-10-03: the reminder did not stop an agent from keeping an
idle Colab tab "for later", so a confirmed agent-created browser tab that is
still open now blocks the agent's next non-browser call (see
[Blocking](#blocking)).

`PostToolUse` feeds tool results into `control_lifecycle.sqlite3` beside
Epitype's config. The ledger stores session IDs, the creating subagent's ID,
tool-call IDs, resource IDs, event times, and open/closed/expired state. It does
not store URLs, titles, page content, or a browser-wide inventory, with one
exception: for an open tab the agent created whose listed URL is a Colab Drive
notebook, it keeps the `authuser` number and notebook ID, which a running-VM
declaration is checked against. A repeated hook result is counted once.

Only results that can change the ledger are read. Everything else (screenshots,
clicks, page reads) returns before the hook loads config or opens SQLite:

- Claude Chrome `tabs_context_mcp`, `tabs_create_mcp`, `tabs_close_mcp` and
  `navigate` (its result carries the tab list with URLs).
- Built-in browser (`mcp__Claude_Browser__*`) `tabs_create`, `preview_start`,
  `tabs_close`, `tabs_context` and `navigate`.
- Codex CUA `js` and `js_reset`: `createBrowserTab`, `listTabs`, `getState`,
  and controller shutdown. A reset closes only the controller record; a tab
  still needs its own confirmation.
- Other `mcp__computer-use__*` tools whose names contain
  create/open/start/launch are recorded as unverified, because their results
  are not parsed. Close/stop/reset names record nothing.

## What counts as closed

A resource is `closed` only on confirmation:

- Chrome: the tab close result says `Closed tab X` for the requested tab.
  `availableTabs` lists only this session's tab group, so a tab missing from it
  may have been dragged out of the group or left behind when the group was
  dissolved; absence never closes a Chrome tab. `No tab group exists` or
  `Group is now empty (auto-removed)` closes only the group record.
- Built-in browser: `tabs_close` says `Closed tab X` for the requested tab, or
  says the Browser pane is now closed (which closes every recorded pane tab).
  `tabs_context` lists every pane tab, so a recorded tab missing from it, or
  `browserOpen: false`, closes the record. A pane tab is recorded only from
  `tabs_create` / `preview_start` results with `reused: false`, or from a
  `navigate` that ran while the pane was known to be closed.
- CUA: a complete, unfiltered inventory no longer contains the ID. `listTabs`
  (scoped by a literal `iab`/`chrome`/`edge`) or `getState()` counts only when
  it is the code's final statement by itself, and the output holds exactly one
  candidate inventory. A wrapped or filtered result
  (`JSON.stringify(t.filter(...))`), something printed after it, or a browser
  chosen by variable or numeric ID proves nothing.

An inventory can close only IDs previously recorded as created by this session.
Tool errors, missing result hooks, hosts without installed/trusted hooks, and
operations outside these interfaces cannot be claimed as observed. Epitype never
closes a tab or a controller itself and never targets tabs that were already
open.

## Unverified records

When a result cannot attribute a creation, the ledger records `unverified-*`:

- The standard first Chrome step, `tabs_context_mcp({createIfEmpty: true})`
  with no earlier snapshot, cannot tell a new group from an existing one. Each
  returned tab is recorded as an `unverified-chrome` candidate by tab ID, and a
  later `Closed tab X` clears it.
- A CUA creation whose tab ID cannot be read clears when a complete inventory
  for that browser is empty.
- A built-in browser `tabs_create` / `preview_start` whose result names no tab
  is recorded as `unverified-control` under its call ID.
- Every unverified record expires one hour after it was opened. Expiry is
  recorded as `expired`, never as `closed`.

## Reminders

Reminders go only to the agent that opened the resource: a call whose hook
input carries `agent_id` belongs to that subagent, and neither the main thread
nor another subagent is reminded about it.

The next non-control `PreToolUse` call can carry a model-directed reminder;
each resource is mentioned there at most once. When that call is blocked (see
[Blocking](#blocking)), the denial takes the reminder's place. The next `UserPromptSubmit`
reads the current ledger and sends any outstanding reminder as model context;
each resource is mentioned there at most three times. A reminder is due only
while some pending resource is still under its limit, so closing part of a set
does not repeat the rest. Both hooks claim a reminder only when it fits the
hook's budget, so a reminder dropped for budget is retried later. A later
reopening of the same ID is a new episode with its own count. If the owner has
already ordered the agent to stop, the reminder directs it to report the open
resource instead of making another UI call.

`Stop` emits no lifecycle continuation: Codex renders a blocking `Stop` reason
as a user-facing Hook feedback card. A resource left open at the end of a turn
remains visible in the ledger until a verified closure arrives.

## Blocking

`PreToolUse` denies a call when the calling agent still has an open
`chrome-tab` or `pane-tab` record, i.e. a tab whose creation a tool result
attributed to this agent. The denial reason, in Traditional Chinese, names the
tabs and the close tool, and says to reopen a tab when it is needed again.

- Browser and computer-control tools are never blocked, nor are `ToolSearch`,
  `Skill`, `AskUserQuestion`, `TodoWrite`, `TaskCreate/Update/List/Get`,
  `TaskStop` and the plan-mode tools, which cleanup itself may need. Every
  other tool, including `Read`, is blocked.
- Only the creating agent is blocked; a subagent's tabs pass to the main thread
  when it finishes, exactly like reminders.
- Owner tabs, unattributable `unverified-*` candidates, tab groups (a group
  mixed with someone else's tab must not be closed whole), Codex CUA tabs and
  the controller session keep the reminder only.
- A Chrome tab absent from the latest group listing (`tabs_context_mcp`,
  `tabs_create_mcp`, `tabs_close_mcp`, `navigate`), or any Chrome tab after
  `No tab group exists` or `Claude in Chrome is not connected`, is out of the
  agent's reach and stops blocking. It is not closed: it stays pending and
  reminded, and blocks again if a later listing shows it.
- A failure inside this check fails open with the usual degraded notice.
- Each denial is counted per tab in `alerts` (stage `block`); closing the tab
  keeps the count until retention removes it.

### Running Colab VM tabs

Closing the notebook tab of a Colab runtime that has background work lets
Colab reclaim the VM. Such a tab is exempt only after this standalone shell
command (Bash or PowerShell, nothing chained to it):

```powershell
python -m epitype lifecycle keep TAB_ID --authuser N --notebook NOTEBOOK_ID
```

`PreToolUse` checks the declaration before the command runs and denies it
unless:

- the tab is the calling agent's own open `chrome-tab` / `pane-tab`;
- a tab listing seen in the last five minutes showed that tab on
  `https://colab.research.google.com/drive/NOTEBOOK_ID` with the same
  `authuser` (absent means `0`); in Chrome call `tabs_context_mcp` first, in
  the built-in browser the `navigate` result carries the URL;
- no other kept tab of this session already holds the same account and
  notebook (one tab per VM).

An accepted declaration is recorded as a `keep` event. The command itself then
reads the record back and exits 1 when the hook did not record it, so a
missing or untrusted hook is visible. The exemption ends after 24 hours, when a
later listing shows the tab on another URL, or when the tab closes; a reopened
ID starts without it. Kept tabs are left out of tool-stage reminders and named
as kept in prompt-stage reminders. Whether the VM is actually running is not
observable from tool results; the check proves the tab is the declared notebook
on the declared account, not that a job is running.

## Retention

Call rows exist only to drop repeated results and are kept for one day. Events,
resources, reminder counts, and per-session state are deleted seven days after
their last change. Cleanup runs at most once an hour, and the first cleanup
waits one hour after the ledger is created or upgraded.

## Health check

`epitype doctor` sends its `PostToolUse` probe as a real `tabs_context_mcp`
result against a temporary config and ledger, and fails with
`ledger-not-written` unless the probe's call reaches that temporary ledger.

Inspect one session with:

```powershell
python -m epitype lifecycle --session SESSION_ID --json
```

`pending` is the set that still needs verification; a row owned by a subagent
also carries `agent`. The `events` array shows recorded opens, confirmed closes,
and expiries. This command opens the ledger read-only: it never creates,
upgrades, or prunes it.

## Upgrading a version 1 ledger

The first hook that opens a version 1 ledger upgrades it in place, once. Its
open `unverified-*` records were keyed by call ID or came from close/stop/reset
tools, so no rule in this version could ever clear them, and they had already
been reminded; the upgrade marks them `expired` (event call ID `schema-v2`).
Confirmed open records are kept. Version 1 reminder fingerprints no longer
match and are discarded.
