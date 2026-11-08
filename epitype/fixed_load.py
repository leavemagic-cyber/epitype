"""Sanitized, advisory startup-cost accounting. Never read credential stores.

Static catalogs and host-rendered totals are separate ledgers: a description is
already inside a host category and must never be added to that category again.
Only explicit report/policy publication and notice receipts write files.
"""
import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

CATEGORIES = ("tools", "mcp", "memory", "skills", "system")
KINDS = {"agent": "tools", "skill": "skills", "index": "memory",
         "instruction": "memory", "rule": "memory", "tool": "tools", "connector": "mcp"}
DEFAULT_POLICY = Path(__file__).with_name("fixed_load_budget.json")


def estimate(text):
    """Planning estimate: ASCII/4 + one per non-ASCII character, rounded up."""
    return math.ceil(sum(0.25 if ord(c) < 128 else 1 for c in text))


def allowed(path):
    parts = [p.casefold() for p in Path(path).resolve().parts]
    return not any(p in {".credentials.json", ".codex", ".gemini", ".grok", "出國用"}
                   or p.endswith("_private") for p in parts)


def _read(path):
    if not allowed(path):
        raise ValueError("excluded source")
    return Path(path).read_text(encoding="utf-8-sig")


def _json(path):
    value = json.loads(_read(path))
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _number(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _modified(path):
    try:
        return Path(path).stat().st_mtime_ns if allowed(path) else None
    except OSError:
        return None


def _catalog_directories(home, cwd):
    roots = [Path(home) / ".claude", *(p / ".claude" for p in (Path(cwd), *Path(cwd).parents))]
    paths = []
    for root in roots:
        paths += [root, root / "agents", root / "skills"]
        if allowed(root / "skills") and (root / "skills").is_dir():
            paths += [p for p in (root / "skills").iterdir() if p.is_dir() and allowed(p)]
    return [{"path": str(p), "mtime_ns": _modified(p)} for p in dict.fromkeys(paths)]


def load_policy(path=None):
    policy = _json(path or DEFAULT_POLICY)
    if (policy.get("version") != 1 or policy.get("mode") != "advisory"
            or any(not _number(policy.get("targets", {}).get(k)) for k in CATEGORIES)
            or not isinstance(policy.get("sources"), list)):
        raise ValueError("invalid budget policy")
    return policy


def _write(path, value):
    path = Path(path)
    if not allowed(path):
        raise ValueError("excluded destination")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".fixed-load-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        if Path(temp).exists():
            Path(temp).unlink()


def identity(cwd):
    """Git's own common-dir resolves worktrees without guessing a path slug."""
    cwd = Path(cwd).resolve()
    if not allowed(cwd):
        return str(cwd)
    done = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute",
                           "--git-common-dir"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode == 0:
        return str(Path(done.stdout.strip()).resolve())
    return str(cwd)


def _front(text):
    """Descriptions may be YAML block scalars; bodies are conditional cost."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("unterminated frontmatter")
    fields = {}
    i = 1
    while i < end:
        match = re.match(r"^([\w-]+):\s*(.*?)\s*$", lines[i])
        if match:
            name, value = match.groups()
            if value in {"|", ">", "|-", ">-", "|+", ">+"}:
                chunks = []
                while i + 1 < end and (not lines[i+1].strip() or lines[i+1][0].isspace()):
                    i += 1
                    chunks.append(lines[i].strip())
                value = "\n".join(chunks)
            fields[name] = value.strip("\"'")
        i += 1
    return fields, "".join(lines[end+1:])


def discover(home, cwd, vaults, unresolved):
    """Only known prompt files; no recursive home scan, MCP values or transcripts."""
    home, cwd = Path(home), Path(cwd).resolve()
    rows = []
    roots = [(home / ".claude", "user")]
    roots += [(p / ".claude", "project") for p in reversed((cwd, *cwd.parents))]
    for root, scope in roots:
        for kind, pattern in (("agent", "agents/*.md"), ("skill", "skills/*/SKILL.md")):
            try:
                for path in sorted(root.glob(pattern)):
                    if allowed(path):
                        rows.append({"path": str(path), "kind": kind, "physical_scope": scope})
            except OSError:
                unresolved.append({"code": "CATALOG_SCAN_INCOMPLETE", "path": str(root)})
        instruction = root / "CLAUDE.md"
        if instruction.is_file() and allowed(instruction):
            rows.append({"path": str(instruction), "kind": "instruction", "physical_scope": scope})
    for parent in reversed((cwd, *cwd.parents)):
        for name in ("CLAUDE.md", "CLAUDE.local.md"):
            path = parent / name
            if path.is_file() and allowed(path):
                rows.append({"path": str(path), "kind": "instruction", "physical_scope": "project"})
    for vault in vaults:
        path = Path(vault) / "MEMORY.md"
        if path.is_file() and allowed(path):
            rows.append({"path": str(path), "kind": "index", "physical_scope": "project"})
    return rows


def _source(row, policy):
    path = Path(row["path"]).resolve()
    kind = row["kind"]
    if kind not in KINDS:
        raise ValueError("invalid source kind")
    text = _read(path)
    stat = path.stat()
    description, body, hidden = text, "", False
    if kind in {"agent", "skill"}:
        front, body = _front(text)
        if "description" not in front:
            raise ValueError("missing description")
        description = front["description"]
        hidden = front.get("disable-model-invocation", "").lower() == "true"
    tokens = 0 if hidden else estimate(description)
    budget = policy.get("source_targets", {}).get(kind)
    projects = row.get("projects", [])
    physical = row.get("physical_scope", "unknown")
    declared = row.get("declared_scope", "project")
    out = {"path": str(path), "kind": kind, "category": KINDS[kind],
           "physical_scope": physical, "declared_scope": declared,
           "projects": projects, "placement_author": row.get("author", "unrecorded"),
           "purpose": row.get("purpose", "unrecorded"), "evidence": row.get("evidence", []),
           "rollback": row.get("rollback", "unrecorded"),
           "version": row.get("version", "mtime"), "mtime_ns": stat.st_mtime_ns,
           "estimated_tokens": tokens, "conditional_body_tokens": estimate(body),
           "automatic_description_hidden": hidden, "target": budget,
           "visibility_paths": [str(Path(row["path"]).absolute())]}
    findings = []
    if _number(budget) and tokens > budget:
        findings.append({"code": "SOURCE_OVER_BUDGET", "path": str(path),
                         "actual": tokens, "target": budget, "unit": "estimated_tokens"})
    # Aliases/worktrees with a common Git dir count as one project.
    distinct = {identity(p) for p in projects}
    if physical == "user" and (declared != "user" or len(distinct) < 2):
        findings.append({"code": "PLACEMENT_REVIEW", "path": str(path),
                         "actual": len(distinct), "target": 2, "unit": "distinct_projects"})
    if kind == "index":
        count = len(text.encode("utf-8"))
        if count > policy["index_cap_bytes"]:
            findings.append({"code": "INDEX_OVER_BUDGET", "path": str(path),
                             "actual": count, "target": policy["index_cap_bytes"], "unit": "bytes"})
    if kind == "instruction":
        match = re.search(r"<!-- EPITYPE RULES BEGIN[^\n]*\n(.*?)<!-- EPITYPE RULES END -->", text, re.S)
        if match and len(match[1].encode("utf-8")) > policy["core_cap_bytes"]:
            findings.append({"code": "CORE_OVER_BUDGET_PRESERVE_REQUIRED_RULES", "path": str(path),
                             "actual": len(match[1].encode("utf-8")), "target": policy["core_cap_bytes"], "unit": "bytes"})
    if row.get("move_to"):
        out["move_obligations"] = ["preserve redirect", "update reverse links and shared quota references",
                                   "update generated views, recall router and search index",
                                   "verify consumers and rollback destination"]
    return out, findings


def _runtime(path, cwd, project):
    """Allowlisted host export. Never copy schema/config/input payloads."""
    raw = _json(path)
    context = raw.get("context", {})
    if (not isinstance(context, dict) or context.get("cwd") != str(Path(cwd).resolve())
            or context.get("project") != project
            or not all(isinstance(context.get(k), str) and context[k] for k in
                       ("host_version", "model_context", "retention_state", "observation_version"))):
        raise ValueError("incomparable runtime context")
    totals = raw.get("totals", {})
    if not isinstance(totals, dict) or any(not _number(totals.get(k)) for k in CATEGORIES):
        raise ValueError("incomplete runtime totals")
    tools, seen = [], set()
    for item in raw.get("tools", []):
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]+", item["name"])
                or item.get("category") not in {"tools", "mcp"}
                or item.get("state") not in {"loaded", "deferred"}
                or not _number(item.get("tokens"))):
            raise ValueError("invalid tool inventory")
        key = item["name"]
        safe = {k: item[k] for k in ("name", "category", "state", "tokens")}
        if key in seen:
            if safe not in tools:
                raise ValueError("conflicting duplicate tool")
            continue
        seen.add(key)
        tools.append(safe)
    for category in ("tools", "mcp"):
        if sum(t["tokens"] for t in tools if t["category"] == category
               and t["state"] == "loaded") > totals[category]:
            raise ValueError("tool costs exceed parent total")
    retained = raw.get("retained_skill_tokens")
    if not _number(retained) or retained > totals["skills"]:
        raise ValueError("invalid retained skill cost")
    return {"context": {k: context[k] for k in ("cwd", "project", "host_version", "model_context",
                                               "retention_state", "observation_version")},
            "totals": {k: totals[k] for k in CATEGORIES}, "tools": tools,
            "retained_skill_tokens": retained}


def report(*, home=None, cwd=None, vaults=(), policy=None, runtime_path=None):
    home, cwd = Path(home or Path.home()), Path(cwd or Path.cwd()).resolve()
    policy = deepcopy(policy or load_policy())
    unresolved, findings = [], []
    project = identity(cwd)
    from .capture_route import native_cwd_vaults
    common = Path(project)
    memory_root = common.parent if common.name == ".git" else cwd
    vaults = [*vaults, *native_cwd_vaults(str(memory_root), home)]
    rows = discover(home, cwd, vaults, unresolved)
    settings = []
    settings_paths = [home / ".claude/settings.json"]
    for parent in reversed((cwd, *cwd.parents)):
        settings_paths += [parent / ".claude/settings.json", parent / ".claude/settings.local.json",
                           parent / ".mcp.json"]
    for path in dict.fromkeys(settings_paths):
        if not allowed(path) or not path.is_file():
            continue
        try:
            data = _json(path)
            permissions = data.get("permissions", {})
            denies = permissions.get("deny", []) if isinstance(permissions, dict) else []
            # Never export permission arguments, server identifiers, env, URLs or headers.
            settings.append({"path": str(path), "mtime_ns": path.stat().st_mtime_ns,
                             "deny_count": len(denies) if isinstance(denies, list) else 0,
                             "server_count": len(data.get("mcpServers", {})) if isinstance(data.get("mcpServers", {}), dict) else 0})
            if isinstance(denies, list) and "ToolSearch" in denies:
                findings.append({"code": "TOOLSEARCH_DENIED_UPFRONT_MCP_RISK", "path": str(path),
                                 "actual": 1, "target": 0, "unit": "deny_entries_not_savings"})
        except (OSError, ValueError, TypeError):
            unresolved.append({"code": "SETTINGS_UNREADABLE_OR_INVALID", "path": str(path)})
    # Declared metadata overrides discovered defaults; physical scope remains observed.
    declared = {str(Path(r["path"]).resolve()): r for r in policy["sources"]}
    rows = [{**r, **declared.get(str(Path(r["path"]).resolve()), {}),
             "physical_scope": r["physical_scope"]} for r in rows]
    rows += [r for p, r in declared.items() if p not in {str(Path(x["path"]).resolve()) for x in rows}]
    sources = {}
    for row in rows:
        try:
            key = str(Path(row["path"]).resolve())
            if key in sources:
                visibility = str(Path(row["path"]).absolute())
                if visibility not in sources[key]["visibility_paths"]:
                    sources[key]["visibility_paths"].append(visibility)
                continue
            source, issues = _source(row, policy)
            sources[key] = source
            findings.extend(issues)
        except (OSError, ValueError, KeyError, TypeError):
            unresolved.append({"code": "SOURCE_UNREADABLE_OR_INVALID", "path": str(row.get("path", ""))})
    # Slug collisions are evidence of ambiguity, never evidence of ownership.
    slugs = {}
    for row in rows:
        for p in row.get("projects", []):
            slugs.setdefault(re.sub(r"[^A-Za-z0-9]", "-", str(p)), set()).add(str(p))
    for paths in slugs.values():
        if len(paths) > 1:
            unresolved.append({"code": "AMBIGUOUS_PROJECT_SLUG", "paths": sorted(paths)})
    static = {k: sum(s["estimated_tokens"] for s in sources.values() if s["category"] == k)
              for k in CATEGORIES}
    runtime = None
    if runtime_path:
        try:
            runtime = _runtime(runtime_path, cwd, project)
        except (OSError, ValueError, TypeError, KeyError):
            unresolved.append({"code": "RUNTIME_INVALID_OR_INCOMPARABLE", "path": str(runtime_path)})
    if runtime is None:
        unresolved.append({"code": "RUNTIME_UNMEASURED"})
    else:
        for k in CATEGORIES:
            if runtime["totals"][k] > policy["targets"][k]:
                findings.append({"code": "CATEGORY_OVER_BUDGET", "path": str(runtime_path),
                                 "category": k, "actual": runtime["totals"][k],
                                 "target": policy["targets"][k], "unit": "measured_tokens"})
    agent_total = sum(s["estimated_tokens"] for s in sources.values() if s["kind"] == "agent")
    if agent_total > policy["agent_catalog_target"]:
        findings.append({"code": "AGENT_CATALOG_OVER_BUDGET", "path": str(home / ".claude/agents"),
                         "actual": agent_total, "target": policy["agent_catalog_target"],
                         "unit": "estimated_tokens"})
    return {"version": 1, "mode": "advisory", "cwd": str(cwd), "project": project,
            "runtime_status": "MEASURED" if runtime else "RUNTIME_UNMEASURED",
            "static_status": "INCOMPLETE" if any(x["code"] != "RUNTIME_UNMEASURED" for x in unresolved) else "PARTIAL",
            "static_estimates": static, "static_total": sum(static.values()),
            "targets": policy["targets"], "target_total": sum(policy["targets"].values()),
            "baseline_total": sum(policy["baseline"].values()),
            "sources": list(sources.values()), "runtime": runtime, "findings": findings,
            "settings": settings, "memory_root": str(memory_root),
            "catalog_directories": _catalog_directories(home, cwd),
            "unresolved": unresolved,
            "limitations": ["Static candidates are not proof of effective host loading; settings inheritance, imports, bundled/synced and launch sources require host export.",
                            "Static children and retained skills are nested inside runtime totals; do not add them.",
                            "Estimates are ASCII/4 plus non-ASCII characters, not tokenizer measurements."]}


def compare(before, after):
    old, new = before.get("runtime"), after.get("runtime")
    if not old or not new:
        return {"status": "RUNTIME_UNMEASURED"}
    keys = ("cwd", "project", "host_version", "model_context", "retention_state")
    if any(old["context"][k] != new["context"][k] for k in keys):
        return {"status": "INCOMPARABLE"}
    return {"status": "COMPARABLE", "delta": {k: new["totals"][k] - old["totals"][k] for k in CATEGORIES}}


def format_report(value):
    lines = [f"fixed-load advisory: {value['runtime_status']}; static {value['static_total']} estimated tokens (partial); target {value['target_total']}"]
    for f in value["findings"]:
        lines.append(f"{f['code']}: {f['actual']} / {f['target']} {f['unit']} — {f['path']}")
    lines += [f"{x['code']} — {x.get('path', '')}" for x in value["unresolved"]]
    return "\n".join(lines)


def publish(value, path):
    _write(path, value)


def notice(report_path, receipt_path, cwd):
    """Read cached sanitized report only. Delivery receipt is committed by caller."""
    report_path, receipt_path = Path(report_path), Path(receipt_path)
    if not report_path.exists():
        return None, None
    try:
        value = _json(report_path)
        if value["cwd"] != str(Path(cwd).resolve()):
            return None, None
        # Comparing named values + modification state, never content hashes.
        state = {k: value[k] for k in ("cwd", "runtime_status", "static_total", "targets", "findings", "unresolved")}
        state["sources"] = [{k: source[k] for k in ("path", "mtime_ns", "version", "estimated_tokens")}
                            for source in value["sources"]]
        state["settings"] = value.get("settings", [])
        state["runtime"] = value["runtime"]
        stale_paths = []
        for row in [*value["sources"], *value.get("settings", []), *value.get("catalog_directories", [])]:
            modified = _modified(row["path"])
            if modified != row["mtime_ns"]:
                stale_paths.append({"path": row["path"], "mtime_ns": modified})
        state["changed_since_report"] = stale_paths
        previous = _json(receipt_path) if receipt_path.exists() else None
        if previous == state:
            return None, None
        count = len(value["findings"])
        if stale_paths:
            line = f"固定負載：來源已變更，報告待更新；請跑 epitype fixed-load，來源紀錄見 {report_path}（不阻擋工作）。"
        else:
            line = f"固定負載：{value['runtime_status']}；靜態估計 {value['static_total']} tokens，{count} 項提醒；來源與數字見 {report_path}（不阻擋工作）。"
        return line, state
    except (OSError, ValueError, KeyError, TypeError):
        # No exception text: a malformed JSON line can contain secrets.
        state = {"error": "REPORT_UNREADABLE", "mtime_ns": report_path.stat().st_mtime_ns}
        try:
            if _json(receipt_path) == state:
                return None, None
        except (OSError, ValueError):
            pass
        return f"固定負載：報告無法讀取，請重跑 epitype fixed-load；{report_path}（不阻擋工作）。", state


def memory_lint_lines(vault, cwd=None):
    """Shared accounting for the existing lint status; no contribution to strict exit."""
    home = Path.home()
    policy_path = home / ".epitype/fixed_load_budget.json"
    policy = load_policy(policy_path) if policy_path.exists() else load_policy()
    value = report(home=home, cwd=cwd or Path.cwd(), vaults=[vault], policy=policy)
    publish(value, home / ".epitype/fixed_load_reports/current.json")
    return ["", "## Fixed-load accounting (advisory)", format_report(value)]


def wire_memory_lint(path):
    """Explicit installation into an existing hook; preserve all other code."""
    path = Path(path)
    text = _read(path)
    marker = "    # Epitype fixed-load accounting (advisory).\n"
    if marker in text:
        return False
    anchor = "    body = _status_body(r, mem_dir)\n"
    if text.count(anchor) != 1:
        raise ValueError("memory lint integration anchor differs; review required")
    call = "        return run_lint(mem_dir, strict, budget, background_full=full)\n"
    if text.count(call) != 1:
        raise ValueError("memory lint caller differs; review required")
    text = text.replace(call, "        global _FIXED_LOAD_CWD\n"
                        "        _FIXED_LOAD_CWD = next(iter(_start_candidates(stdin_raw, argv)), os.getcwd())\n" + call)
    addition = '''    # Epitype fixed-load accounting (advisory).
    try:
        repo = _epitype_repo()
        if repo is not None:
            sys.path.insert(0, str(repo))
        from epitype.fixed_load import memory_lint_lines
        body += "\\n" + "\\n".join(memory_lint_lines(mem_dir, globals().get("_FIXED_LOAD_CWD"))) + "\\n"
    except Exception:
        body += "\\nFixed-load accounting: INCOMPLETE (advisory; no task stop).\\n"
'''
    path.write_text(text.replace(anchor, anchor + addition), encoding="utf-8", newline="\n")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--vault", type=Path, action="append", default=[])
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--install-policy", type=Path)
    parser.add_argument("--wire-memory-lint", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.install_policy:
            if args.install_policy.exists():
                raise ValueError("policy exists; preserve local decisions")
            _write(args.install_policy, load_policy())
            return 0
        if args.wire_memory_lint:
            wire_memory_lint(args.wire_memory_lint)
            return 0
        policy_path = args.policy or args.home / ".epitype/fixed_load_budget.json"
        policy = load_policy(policy_path if policy_path.exists() else None)
        value = report(home=args.home, cwd=args.cwd, vaults=args.vault, policy=policy,
                       runtime_path=args.runtime)
        if args.compare:
            value["comparison"] = compare(_json(args.compare), value)
        if args.output:
            publish(value, args.output)
        print(json.dumps(value, ensure_ascii=False, indent=2) if args.json else format_report(value))
    except Exception:
        print('{"mode":"advisory","status":"INCOMPLETE","reason":"fixed-load input or output unavailable; no task stop"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
