"""Core-rule size ratchet: compare the generated core block with the last accepted size.

owner 2026-11-17（decision-memory-size-host-limits-core-ratchet-20261117）：核心規則不再用
自訂位元組上限（舊的 core_cap_bytes），改成跟「上次確認的大小」比。任何一個宿主的載入量
變大就列出哪張卡、多少、理由，寫一份待處理檔，開場看得到，直到有人處理或寫理由接受；
等於或變小自動更新基準。沒有容許值：多一個位元組也算。規則照寫不擋——擋住會讓舊規則
繼續被讀（CORE-25）。絕對的保底是宿主事實（memspec.CLAUDE_MD_SKIP_BYTES 等）。

usage: epitype core-baseline [vault] [--accept --reason TEXT [--by NAME]] [--json]
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

try:
    from . import memspec
except ImportError:
    import memspec

BASELINE_FILENAME = "core_size_baseline.json"
PENDING_FILENAME = "core_growth_pending.json"
INITIAL_REASON = "initial baseline per owner decision 2026-11-17"
AUTO_BY = "epitype (equal or smaller)"
LEGACY_CORE_CAP_NOTICE = ("core_cap_bytes 已不再使用；核心規則改成跟上次確認的大小比"
                          "（epitype core-baseline），變大就列出哪張卡")
GROWTH_NOTICE = ("核心規則比上次確認多 {over} 位元組（{items}）。處理：改成用到才送、合併舊條目，"
                 "或寫理由接受：epitype core-baseline --accept --reason \"…\"")
REASON_REQUIRED = "接受新基準要寫理由：--reason \"…\"（不能空白）"
STATUS_INITIALIZED = "initialized"
STATUS_GREW = "grew"
STATUS_SAME_OR_SMALLER = "same-or-smaller"
STATUS_BROKEN = "baseline-broken"
BROKEN_NOTICE = ("核心規則基準檔{why}，不自動重建（重建會把變大的部分當成新基準）。確認目前大小後寫理由接受："
                 "epitype core-baseline --accept --reason \"…\"")
BROKEN_UNREADABLE = "讀不懂"
BROKEN_MISSING = "不見了但還有待處理的變大紀錄"
GROWTH_ITEMS_SHOWN = 5


def _directory(vault):
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY


def baseline_path(vault):
    return _directory(vault) / BASELINE_FILENAME


def pending_path(vault):
    return _directory(vault) / PENDING_FILENAME


def governance_vault(options=None):
    """The one vault that holds the baseline: `global_vault`, else the first configured vault.

    Every writer (core-gen, sync, dream, accept) and the SessionStart reader resolve it here,
    and measure only this vault's rules: two measurements of different views would make one
    writer lower the baseline and the next report growth, forever (flap)."""
    options = memspec.config_options() if options is None else options
    configured = options.get(memspec.CONFIG_VAULTS_FIELD)
    selected = options.get("global_vault") or (
        configured[0] if isinstance(configured, list) and configured else None)
    if isinstance(selected, Path):
        selected = str(selected)
    if not isinstance(selected, str) or not selected.strip():
        return None
    return Path(selected).expanduser().resolve()


def legacy_notice(config):
    return LEGACY_CORE_CAP_NOTICE if memspec.CONFIG_CORE_CAP_BYTES_FIELD in (config or {}) else None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _card_key(rule, vault):
    same = Path(rule["vault"]).resolve() == Path(vault).resolve()
    return rule["path"] if same else f"{rule['vault']}|{rule['path']}"


def _rationale(rule):
    provenance = rule.get("provenance") or {}
    for holder in (provenance.get("payload_binding"), provenance.get("scope_basis"), provenance):
        if isinstance(holder, dict) and isinstance(holder.get("rationale"), str) and holder["rationale"].strip():
            return holder["rationale"].strip()
    return None


def snapshot(text, rules, vault):
    """Same measurement as core_gen.host_loads and the host_sync rules region."""
    import hashlib
    try:
        from . import core_gen
    except ImportError:
        import core_gen
    generated = [rule for rule in rules if rule["layer"] in memspec.RULE_GENERATED_LAYERS]
    return {
        "host_loads": core_gen.host_loads(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "cards": {_card_key(rule, vault): len(rule["text"].encode("utf-8")) for rule in generated},
        "_meta": {_card_key(rule, vault): {"approved_by": rule.get("approved_by") or None,
                                           "rationale": _rationale(rule)} for rule in generated},
    }


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _read_baseline(path):
    value = _read(path)
    if value is None or not isinstance(value.get("host_loads"), dict) or not isinstance(value.get("cards"), dict):
        return None
    return value


def _write(path, value):
    """Temp file + fsync + replace: a half-written baseline would read as "broken"."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staging = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=1) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        Path(staging).unlink(missing_ok=True)


def _baseline_record(current, by, reason):
    return {"host_loads": current["host_loads"], "sha256": current["sha256"],
            "cards": current["cards"], "accepted_at": _now(), "accepted_by": by, "reason": reason}


def growth(baseline, current):
    """Cards added or grown since the baseline, largest first, with who approved them and why."""
    items = []
    old = baseline.get("cards") or {}
    for key, size in current["cards"].items():
        delta = size - old.get(key, 0)
        if delta > 0:
            meta = current.get("_meta", {}).get(key, {})
            items.append({"card": key, "delta": delta, "bytes": size, "added": key not in old,
                          "approved_by": meta.get("approved_by"), "rationale": meta.get("rationale")})
    items.sort(key=lambda item: (-item["delta"], item["card"]))
    return items


def _over(baseline, current):
    before = baseline.get("host_loads") or {}
    return {host: size - before.get(host, 0) for host, size in current["host_loads"].items()
            if size > before.get(host, 0)}


def notice_line(over, items):
    shown = [f"{Path(item['card'].split('|')[-1]).stem} +{item['delta']}" for item in items[:GROWTH_ITEMS_SHOWN]]
    if len(items) > GROWTH_ITEMS_SHOWN:
        shown.append(f"另 {len(items) - GROWTH_ITEMS_SHOWN} 張")
    if not shown:
        shown = ["卡片字數沒變，是區塊排版或標題"]
    return GROWTH_NOTICE.format(over=max(over.values()), items="、".join(shown))


def evaluate(vault, text, rules, *, write=True):
    """Compare with the baseline. Never blocks: returns a report; writes only when `write`."""
    current = snapshot(text, rules, vault)
    baseline = _read_baseline(baseline_path(vault))
    report = {"vault": str(vault), "host_loads": current["host_loads"], "notice": None,
              "growth": [], "over": {}, "write": write}
    if baseline is None and (baseline_path(vault).exists() or pending_path(vault).exists()):
        # 只有「從沒有過基準、也沒有待處理」才算第一次。壞掉或被刪掉時自己重建，會把
        # 還沒處理的變大當成新基準、清掉待處理，等於沒有理由就接受了。
        why = BROKEN_UNREADABLE if baseline_path(vault).exists() else BROKEN_MISSING
        report.update(status=STATUS_BROKEN, notice=BROKEN_NOTICE.format(why=why))
        if write and not pending_path(vault).exists():
            _write(pending_path(vault), {"detected_at": _now(), "broken": True, "notice": report["notice"],
                                         "host_loads": current["host_loads"], "sha256": current["sha256"]})
        return report
    if baseline is None:
        report["status"] = STATUS_INITIALIZED
        report["baseline"] = _baseline_record(current, "epitype", INITIAL_REASON)
        if write:
            _write(baseline_path(vault), report["baseline"])
            pending_path(vault).unlink(missing_ok=True)
        return report
    over = _over(baseline, current)
    if over:
        items = growth(baseline, current)
        report.update(status=STATUS_GREW, over=over, growth=items, notice=notice_line(over, items))
        report["pending"] = {"detected_at": _now(), "baseline_host_loads": baseline.get("host_loads"),
                             "host_loads": current["host_loads"], "over": over,
                             "baseline_accepted_at": baseline.get("accepted_at"),
                             "sha256": current["sha256"], "growth": items, "notice": report["notice"]}
        if write:
            _write(pending_path(vault), report["pending"])
        return report
    report["status"] = STATUS_SAME_OR_SMALLER
    if write:
        if baseline.get("sha256") != current["sha256"] or baseline.get("cards") != current["cards"]:
            _write(baseline_path(vault), _baseline_record(current, AUTO_BY, "equal or smaller than the last baseline"))
        pending_path(vault).unlink(missing_ok=True)
    return report


def accept(vault, text, rules, reason, by=None):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(REASON_REQUIRED)
    record = _baseline_record(snapshot(text, rules, vault), by or "owner", reason.strip())
    _write(baseline_path(vault), record)
    pending_path(vault).unlink(missing_ok=True)
    return record


def surface_line(vault):
    """The one SessionStart line while growth is pending; None otherwise."""
    pending = _read(pending_path(vault))
    notice = (pending or {}).get("notice")
    return notice if isinstance(notice, str) and notice.strip() else None


def current_core(vaults, config=None):
    """(text, rules, errors) for the governance core block, assembled the core-gen way."""
    try:
        from . import core_gen, core_qualification
    except ImportError:
        import core_gen
        import core_qualification
    options = config if config is not None else memspec.config_options()
    rules, errors = core_gen.collect_rules(vaults, minimum_projects=core_qualification.minimum(options))
    return core_gen.assemble(rules), rules, errors


def measure(vaults, config=None):
    """(governance vault, text, rules, errors) for the one view every writer compares, or None.

    None when no governance vault is configured or this run's vaults do not include it:
    a project vault never gets a baseline of its own, and other vaults' rules never count."""
    options = config if config is not None else memspec.config_options()
    governance = governance_vault(options)
    if governance is None or governance not in {Path(v).expanduser().resolve() for v in vaults}:
        return None
    text, rules, errors = current_core([governance], options)
    return governance, text, rules, errors


def check_vaults(vaults, *, write=True, config=None):
    """core-gen and sync: evaluate the governance view; None when not measurable."""
    measured = measure(vaults, config)
    if measured is None:
        return None
    governance, text, rules, errors = measured
    if errors or not any(rule["layer"] in memspec.RULE_GENERATED_LAYERS for rule in rules):
        return None
    return evaluate(governance, text, rules, write=write)


def main(argv=None, output=sys.stdout):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vault", nargs="?", type=Path, help="must be the governance vault (global_vault, else the first configured vault); the default")
    parser.add_argument("--accept", action="store_true", help="set the baseline to the current core block")
    parser.add_argument("--reason", help="why the larger core block is accepted (required with --accept)")
    parser.add_argument("--by", help="who accepts (default: owner)")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(argv)
    config = memspec.config_options()
    vault = governance_vault(config)
    if vault is None or not vault.is_dir():
        print("ERROR 找不到治理庫：在設定檔設 global_vault 或 vaults", file=output)
        return 2
    if parsed.vault is not None and Path(parsed.vault).expanduser().resolve() != vault:
        print(f"ERROR 基準只記在治理庫 {vault}；專案庫不另立基準", file=output)
        return 2
    if parsed.accept:
        if not parsed.reason or not parsed.reason.strip():
            print(f"ERROR {REASON_REQUIRED}", file=output)
            return 2
        text, rules, errors = current_core([vault], config)
        if errors:
            print("ERROR 規則卡讀取未通過，不改基準：" + "；".join(map(str, errors[:3])), file=output)
            return 2
        record = accept(vault, text, rules, parsed.reason, parsed.by)
        result = {"status": "accepted", "baseline": record}
    else:
        baseline = _read_baseline(baseline_path(vault))
        pending = _read(pending_path(vault))
        broken = baseline is None and baseline_path(vault).exists()
        result = {"status": "pending" if pending else ("broken" if broken else ("ok" if baseline else "no-baseline")),
                  "baseline": {k: v for k, v in (baseline or {}).items() if k != "cards"} or None,
                  "pending": pending}
    if parsed.json:
        print(json.dumps(result, ensure_ascii=False, indent=1), file=output)
    else:
        if result["status"] == "accepted":
            print(f"ACCEPTED host_loads={result['baseline']['host_loads']} reason={result['baseline']['reason']}",
                  file=output)
        elif result["status"] == "pending":
            print(f"PENDING {result['pending'].get('notice')}", file=output)
        elif result["status"] == "ok":
            base = result["baseline"]
            print(f"OK host_loads={base.get('host_loads')} accepted_at={base.get('accepted_at')} "
                  f"by={base.get('accepted_by')} reason={base.get('reason')}", file=output)
        elif result["status"] == "broken":
            print("BROKEN " + BROKEN_NOTICE.format(why=BROKEN_UNREADABLE), file=output)
        else:
            print("NO-BASELINE 下一次 core-gen 或 sync 會以目前大小建立基準", file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
