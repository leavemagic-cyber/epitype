"""Just-in-time control advice; tool intent is not evidence of resource state."""

CONTROL_GUIDE = (
    "[Epitype: control lifecycle]\n"
    "If this tool is used for UI control, use it only when needed. Prefer suitable non-UI tools. "
    "Record the window/tab/group IDs you create; preserve pre-existing user resources. "
    "For mixed groups, close only your tabs. "
    "Immediately when the UI step is finished, close only your own windows/tabs/groups and end control; "
    "do not wait for the whole task. Closing a window does not end control; "
    "REPL reset does not close browser tabs/groups. Verify cleanup from actual tool results, "
    "not requests or self-report; disclose failures or unknowns. "
    "Do not restart control just to check reset. Honor user interruption and host safety; "
    "cleanup is not permission to resume stopped input."
)

# REPLs can run non-UI code too: deliver conditional advice, never a deny or
# an assertion that control started. Exact tool identities avoid quoted code hits.
_TOOLS = frozenset(
    f"mcp__{server}{separator}{action}"
    for server in ("node_repl", "cua_repl")
    for separator in ("__", ".")
    for action in ("js", "js_reset")
)


def guidance(tool_name):
    if not isinstance(tool_name, str):
        return None
    prefixes = ("mcp__claude-in-chrome__", "mcp__Claude_Browser__", "mcp__computer-use__")
    host_control = any(tool_name.startswith(prefix) and len(tool_name) > len(prefix) for prefix in prefixes)
    return CONTROL_GUIDE if tool_name in _TOOLS or host_control else None
