"""Read-only consolidation materials shared by dream, sync and SessionStart."""
from collections import defaultdict
from pathlib import Path
import re

try:
    from . import core_gen, memspec
except ImportError:
    import core_gen
    import memspec

STATE_FIELD = "cap_consolidation"
NEXT_STEP = "需要擬精簡稿，經 Claude 與 Codex 核對意義不變，再給 owner 前後對照表核准；核准後走規則管線重新綁定"


def material_path(config):
    """Use the existing dream destination: ledger holder, otherwise first vault."""
    configured = config.get(memspec.CONFIG_VAULTS_FIELD)
    vaults = [Path(raw).expanduser() for raw in (configured if isinstance(configured, (list, tuple)) else ())
              if isinstance(raw, (str, Path)) and str(raw).strip()]
    if not vaults:
        return "<治理庫>/" + memspec.DREAM_DIRECTORY + "/" + memspec.DREAM_PACK_FILENAME + " 第 11 節"
    governance = next((v for v in vaults if (v / memspec.WORK_LEDGER_FILENAME).is_file()), vaults[0])
    return str(governance.resolve() / memspec.DREAM_DIRECTORY / memspec.DREAM_PACK_FILENAME) + " 第 11 節"


def ranked_rules(text, host, rules):
    """Rank the loaded clauses, matching exact rendered text back to source cards.

    A drifted clause stays visible with an unknown card; current card text must
    never be passed off as text already loaded by the host.
    """
    sources = defaultdict(list)
    floor = [r for r in rules if r["layer"] == memspec.RULE_LAYER_FLOOR]
    for number, rule in enumerate(floor, 1):
        line = memspec.CORE_GEN_FLOOR_LINE.format(number=number, text=core_gen._display_rule(rule))
        sources[line].append(rule)
    for rule in rules:
        if rule["layer"] == memspec.RULE_LAYER_RESIDENT and (not rule["hosts"] or host in rule["hosts"]):
            line = memspec.CORE_GEN_RESIDENT_LINE.format(text=core_gen._display_rule(rule))
            sources[line].append(rule)
    view = core_gen.host_view(text, host)
    clauses, current = [], []
    for line in view.splitlines(keepends=True):
        if re.match(r"^(?:- |\d+\. )", line):
            if current:
                clauses.append("".join(current))
            current = [line]
        elif current and line.strip() and not line.startswith(("#", "<!--")):
            current.append(line)
        elif current:
            clauses.append("".join(current))
            current = []
    if current:
        clauses.append("".join(current))
    ranked = []
    for raw_clause in clauses:
        clause = raw_clause.rstrip("\n")
        matches = sources.get(clause)
        rule = matches.pop(0) if matches else None
        ranked.append({"text": clause, "bytes": len(raw_clause.encode("utf-8")),
                       "card_path": str(Path(rule["vault"]) / rule["path"]) if rule else None})
    ranked.sort(key=lambda item: (-item["bytes"], item["card_path"] or "", item["text"]))
    return ranked


def ranked_index(text, path, source_text=None):
    """Each index line retains its source location, including wrapped entries."""
    source_lines = (source_text if source_text is not None else text).splitlines()
    cursor = 0
    ranked = []
    for line in text.splitlines(keepends=True):
        if not line.strip() or line.startswith("#"):
            continue
        value = line.rstrip("\r\n")
        while cursor < len(source_lines) and source_lines[cursor] != value:
            cursor += 1
        ranked.append({"text": value, "bytes": len(line.encode("utf-8")),
                       "path": str(path), "line": cursor + 1})
        cursor += 1
    ranked.sort(key=lambda item: (-item["bytes"], item["line"]))
    return ranked


def summary(entries):
    """Small numeric summary in the existing dream state, not a second channel."""
    result = []
    for entry in entries:
        if entry.get("kind") == "core-rules":
            result.extend({"kind": "core-rules", "host": host, "over": size - entry["cap"]}
                          for host, size in entry["host_loads"].items() if size > entry["cap"])
        elif entry.get("over", 0) > 0:
            result.append({"kind": "index", "path": entry["path"], "over": entry["over"]})
    return result


def notice(entries, pack):
    parts = []
    for entry in entries:
        if entry["kind"] == "core-rules":
            parts.append(f"{entry['host']} 常駐規則超過上限 {entry['over']} bytes")
        else:
            parts.append(f"短索引 {entry['path']} 超過上限 {entry['over']} bytes")
    return "🌙 " + "；".join(parts) + f"；整理材料：{pack} 第 11 節；" + NEXT_STEP
