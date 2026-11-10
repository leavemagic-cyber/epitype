"""Read bounded native context estimates without exporting messages or schemas."""
import hashlib
import json
import os
from pathlib import Path
import re

TAIL_BYTES = 4 * 1024 * 1024
NAMES = {"Messages", "System tools", "MCP tools", "Memory files", "Skills",
         "System prompt", "MCP server instructions", "Custom agents"}
USAGE_TOOL = "mcp__ccd_session_mgmt__get_usage"


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _summary(value, *, structured):
    total = value.get("total_tokens" if structured else "tokensUsed")
    window = value.get("raw_max_tokens" if structured else "contextWindow")
    categories = value.get("categories")
    if not _integer(total) or not _integer(window) or not window or not isinstance(categories, list):
        return None
    used, deferred, unknown_used = {}, 0, 0
    for item in categories:
        if not isinstance(item, dict) or not _integer(item.get("tokens")):
            return None
        name, tokens = item.get("name"), item["tokens"]
        if not isinstance(name, str):
            return None
        kind = item.get("kind") if structured else "used"
        if kind == "deferred":
            deferred += tokens
        elif kind == "used":
            if name in NAMES:
                if name in used:
                    return None
                used[name] = tokens
            else:
                unknown_used += tokens
    # Unknown or omitted categories never silently become zero.
    required = {"Messages", "System tools", "MCP tools", "Memory files", "Skills", "System prompt"}
    if not required <= used.keys():
        return None
    category_total = sum(used.values()) + unknown_used
    return {"reported_used_tokens": total, "context_window": window,
            "used_categories": used, "other_used_category_tokens": unknown_used,
            "category_used_tokens": category_total,
            "unattributed_tokens": total - category_total,
            "category_accounting_status": ("RECONCILED" if total == category_total else
                                           "UNATTRIBUTED" if total > category_total else "INCONSISTENT"),
            "non_message_category_tokens": category_total - used["Messages"],
            "deferred_tokens": deferred if structured else None,
            "tool_deferral_status": "separate_categories" if structured else "UNREPORTED",
            "per_tool_loaded_state": "UNREPORTED", "billing_status": "NOT_A_BILL"}


def observe(path, cwd, *, allowed):
    """Explicit diagnostic only; no startup scan and no changes to tool availability."""
    path = Path(path)
    if not allowed(path):
        raise ValueError("excluded source")
    target = os.path.normcase(str(Path(cwd).resolve()))
    calls, latest = set(), None
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        start = max(0, size - TAIL_BYTES)
        stream.seek(start)
        read_bytes = 0
        if start:
            read_bytes += len(stream.readline(size - start))  # Discard an incomplete first record.
        while stream.tell() < size:
            offset = stream.tell()
            raw = stream.readline(size - offset)
            read_bytes += len(raw)
            if not raw:
                break  # A live log may have been truncated after the size snapshot.
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or not isinstance(row.get("cwd"), str):
                continue
            if os.path.normcase(str(Path(row["cwd"]).resolve())) != target or row.get("isSidechain"):
                continue
            summary = None
            if row.get("type") == "system" and row.get("subtype") == "local_command":
                usage = row.get("contextUsage")
                if isinstance(usage, dict):
                    summary = _summary(usage, structured=True)
            message = row.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            if isinstance(content, list):
                for piece in content:
                    if not isinstance(piece, dict):
                        continue
                    if (row.get("type") == "assistant" and piece.get("type") == "tool_use"
                            and piece.get("name") == USAGE_TOOL and isinstance(piece.get("id"), str)):
                        calls.add(piece["id"])
                    if (row.get("type") != "user" or piece.get("type") != "tool_result"
                            or piece.get("tool_use_id") not in calls or piece.get("is_error")):
                        continue
                    blocks = piece.get("content", [])
                    if not isinstance(blocks, list):
                        continue
                    for block in blocks:
                        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                            continue
                        try:
                            value = json.loads(block["text"])
                        except ValueError:
                            continue
                        usage = value.get("context") if isinstance(value, dict) else None
                        if (isinstance(usage, dict) and usage.get("status") == "ok"
                                and usage.get("session", "self") == "self"):
                            summary = _summary(usage, structured=False)
            if summary:
                # Only validated observation markers accompany counts; other metadata stays out.
                latest = {**summary, "record_offset": offset,
                          "record_sha256": hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest()}
                for key, pattern in (("timestamp", r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"),
                                     ("version", r"\d+\.\d+\.\d+")):
                    text = row.get(key)
                    if isinstance(text, str) and re.fullmatch(pattern, text):
                        latest[key] = text
    return {"status": "NATIVE_ESTIMATE" if latest else "NO_OBSERVATION_IN_WINDOW",
            "source": str(path.resolve()), "source_size": size, "read_bytes": read_bytes,
            "bounded_window_bytes": TAIL_BYTES, "observation": latest,
            "comparison_status": "INCOMPARABLE_WITHOUT_HOST_MODEL_AND_RETENTION",
            "limitation": "A native estimate is not billing or proof of removable tool cost; catalogs are excluded."}
