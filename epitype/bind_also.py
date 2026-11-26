"""bind_also_cards：bind_only_core_rules 開著時照樣綁觸發的非核心卡（owner 2026-11-25）。

比對嚴格：設定裡寫的必須是卡檔名去掉 .md 的完整 stem，大小寫一致。寫錯不放寬比對——
放寬會讓一張卡靠相似的檔名把自己綁進來——而是在開場說出哪一條沒對上、該改成什麼。

這裡只提醒、不改綁定：同一個 stem 有好幾張卡時每一張都照綁（多的只是提醒型觸發），
開場那一行把每一張的完整路徑列出來，由人決定要不要改名。檢查要走訪庫，只在開場做，
而且有時間上限；閘門的熱路徑不碰這裡的走訪。
"""

import json
from pathlib import Path

try:
    from . import cardscan, memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import cardscan
    import memspec


def read_config(path=None):
    """The raw config object, or None when unreadable or not an object."""
    try:
        value = json.loads(Path(path or memspec.config_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def parse(value):
    """(named stems, shape problems) from a raw config object.

    A non-list value names no card; a bad entry is skipped while the good ones still bind.
    Surrounding whitespace is dropped; nothing else is normalised."""
    if not isinstance(value, dict) or memspec.CONFIG_BIND_ALSO_CARDS_FIELD not in value:
        return frozenset(), []
    raw = value.get(memspec.CONFIG_BIND_ALSO_CARDS_FIELD)
    if not isinstance(raw, list):
        return frozenset(), [memspec.BIND_ALSO_NOT_LIST_NOTICE.format(kind=type(raw).__name__)]
    names, problems = set(), []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            names.add(entry.strip())
        else:
            problems.append(memspec.BIND_ALSO_BAD_ENTRY_NOTICE.format(entry=repr(entry)[:40]))
    return frozenset(names), problems


def bound_names(path=None):
    """Stems named for binding when core-only binding is on; empty otherwise."""
    value = read_config(path)
    if not value or value.get(memspec.CONFIG_CORE_ONLY_BINDING_FIELD) is not True:
        return frozenset()
    return parse(value)[0]


def _stem_index(vaults, out_of_time):
    """{stem: [full card path, ...]} over every card in the vaults; None when out of time.

    Full paths, not vault names: every native vault is a directory called `memory`."""
    index, seen = {}, set()
    for vault in vaults:
        try:
            scan = cardscan.scan_vault(Path(vault).resolve(), out_of_time=out_of_time)
        except TimeoutError:
            return None
        except Exception:
            continue
        for relative, path, *_ in scan:
            shown = str(path)
            if shown in seen:
                continue
            seen.add(shown)
            index.setdefault(Path(relative).stem, []).append(shown)
    return index


def _hint(name, index):
    """The exact stem a near-miss name was probably meant to be, or None."""
    tries = [name]
    if name.lower().endswith(".md"):
        tries.append(name[:-3])
    tries.append(Path(name.replace("\\", "/")).stem)
    for candidate in tries:
        if candidate != name and candidate in index:
            return candidate
    folded = {}
    for stem in index:
        folded.setdefault(stem.casefold(), []).append(stem)
    for candidate in tries:
        matches = folded.get(candidate.casefold(), [])
        if len(matches) == 1 and matches[0] != name:
            return matches[0]
    return None


def startup_notices(vaults, config=None, out_of_time=lambda: False):
    """Warning lines for the session start; changes no binding.

    `vaults` are the ones this session's gates load. Silent when core-only binding is off
    or the list is correct. When the walk runs out of time the name checks are skipped and
    one line says so: a partial walk would call present cards missing."""
    value = read_config(config)
    if not value or value.get(memspec.CONFIG_CORE_ONLY_BINDING_FIELD) is not True:
        return []
    names, lines = parse(value)
    if names:
        index = _stem_index(vaults, out_of_time)
        if index is None:
            lines.insert(0, memspec.BIND_ALSO_INCOMPLETE_NOTICE)
        else:
            for name in sorted(names):
                hits = index.get(name, [])
                if len(hits) > 1:
                    lines.append(memspec.BIND_ALSO_AMBIGUOUS_NOTICE.format(
                        name=name, count=len(hits), where="、".join(sorted(hits)[:3])))
                elif not hits:
                    stem = _hint(name, index)
                    lines.append(memspec.BIND_ALSO_NO_MATCH_HINT_NOTICE.format(name=name, stem=stem)
                                 if stem else memspec.BIND_ALSO_NO_MATCH_NOTICE.format(name=name))
    cap = memspec.BIND_ALSO_NOTICE_MAX_LINES
    if len(lines) > cap:
        lines = lines[:cap - 1] + [memspec.BIND_ALSO_MORE_NOTICE.format(count=len(lines) - cap + 1)]
    return lines
