# Browser resource leak detection

Owner decision, 2026-09-27: Epitype records the browser resources an agent
creates, records verified closures, and reminds the agent if it moves to another
tool or tries to finish while a resource remains open.

`PostToolUse` feeds successful tool results into `control_lifecycle.sqlite3`
beside Epitype's config. The ledger stores session IDs, tool-call IDs, resource
IDs, event times, and open/closed state. It does not store URLs, titles, page
content, or a browser-wide inventory. A repeated hook result is counted once.

Recognized result shapes:

- Claude Chrome `tabs_context_mcp`, `tabs_create_mcp`, and `tabs_close_mcp`:
  tab/group IDs from successful context and close responses.
- Codex CUA `createBrowserTab`, `listTabs`, `getState`, and `js_reset`:
  created tab IDs, successful inventories, and controller shutdown. A reset
  closes only the controller record; a tab still needs its own confirmation.
- Other named browser/computer-control tools: an operation whose name suggests
  opening or closing is marked unverified when Epitype cannot parse its result.

An inventory can close only IDs previously recorded as created by this
session. Unknown ownership or ambiguous results remain unverified. Tool errors,
missing result hooks, hosts without installed/trusted hooks, and operations
outside these interfaces cannot be claimed as observed. Epitype never closes a
tab or a controller itself and never targets tabs that were already open.

The next non-control `PreToolUse` call can receive a one-time reminder for the
current outstanding set. `Stop` can block once with the same reminder. A later
reopening is a new episode and gets a new reminder. If the owner has already
ordered the agent to stop, the reminder directs it to report the open resource
instead of making another UI call.

Inspect one session with:

```powershell
python -m epitype lifecycle --session SESSION_ID --json
```

`pending` is the set that still needs verification. The `events` array shows
recorded opens and confirmed closes. This command only reads the ledger.
