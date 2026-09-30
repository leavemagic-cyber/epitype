"""Read-only replay measurements. Counterfactual compaction is an estimate, not live savings."""
import sys; sys.dont_write_bytecode = True
import argparse
import json
from pathlib import Path
from statistics import median


def claude_trace(path):
    samples, outputs, seen, tools = [], [], set(), {}
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message", {})
            if not isinstance(message, dict):
                continue
            content = message.get("content", [])
            for part in content if isinstance(content, list) else ():
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    tools[part.get("id")] = part.get("name")
                if part.get("type") == "tool_result" and tools.get(part.get("tool_use_id")) in ("Bash", "PowerShell"):
                    value = part.get("content", "")
                    if isinstance(value, list):
                        value = "\n".join(p.get("text", "") for p in value if isinstance(p, dict))
                    if isinstance(value, str):
                        outputs.append(len(value))
            usage = message.get("usage")
            ident = message.get("id")
            if row.get("type") != "assistant" or not isinstance(usage, dict) or ident in seen:
                continue
            seen.add(ident)
            counts = [usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
            if not all(isinstance(n, int) and n >= 0 for n in counts):
                continue
            samples.append({"context": sum(counts), "output": usage.get("output_tokens", 0)})
    return samples, outputs


def replay_compaction(samples, threshold, carry, summary_tokens=3000):
    """Keep observed positive growth; reset at observed or hypothetical compactions.

    Extra compactions each cost the full current input plus an empirical summary.
    Actual future cache hits and retained information cannot be reconstructed.
    """
    if not samples or threshold <= carry or carry < 0:
        raise ValueError("threshold must exceed carry, with nonempty samples")
    current = samples[0]["context"]
    total, compactions, previous = 0, 0, current
    for i, sample in enumerate(samples):
        original = sample["context"]
        if i:
            current = original if original < previous * 0.6 else current + max(0, original - previous)
        if current >= threshold:
            total += current + summary_tokens
            compactions += 1
            current = carry
        total += current + sample["output"]
        previous = original
    return {"estimated_tokens": total, "extra_compactions": compactions}


def report(paths, window, carry):
    traces, outputs = [], []
    for path in paths:
        trace, lengths = claude_trace(path)
        if trace:
            traces.append(trace)
        outputs.extend(lengths)
    baseline = sum(s["context"] + s["output"] for trace in traces for s in trace)
    candidates = {}
    for pct in (30, 40, 50, 60, 92):
        results = [replay_compaction(t, window * pct // 100, carry) for t in traces]
        total = sum(r["estimated_tokens"] for r in results)
        candidates[str(pct)] = {"estimated_tokens": total,
            "extra_compactions": sum(r["extra_compactions"] for r in results),
            "estimated_reduction_pct": round(100 * (baseline - total) / baseline, 2) if baseline else None}
    return {"sessions": len(traces), "calls": sum(map(len, traces)), "observed_tokens": baseline,
        "window_assumption": window, "carry_assumption": carry, "compaction_replay": candidates,
        "shell_outputs": len(outputs), "shell_chars": sum(outputs),
        "shell_median_chars": median(outputs) if outputs else 0,
        "output_limit_replay": {str(cap): sum(min(n, cap) for n in outputs) for cap in (30000, 15000, 10000)},
        "limits": "Counterfactual replay, not measured live savings; fixed carry/summary, cache and information loss unverified."}


def codex_output_report(paths):
    sizes = []
    for path in paths:
        with Path(path).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                payload = row.get("payload", {})
                if row.get("type") != "response_item" or not isinstance(payload, dict):
                    continue
                if payload.get("type") not in ("function_call_output", "custom_tool_call_output"):
                    continue
                value = payload.get("output")
                if isinstance(value, list):
                    value = "\n".join(p.get("text", "") for p in value
                        if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"))
                if isinstance(value, str):
                    sizes.append((len(value.encode("utf-8")) + 3) // 4)
    return {"host": "codex", "outputs": len(sizes), "observed_estimated_tokens": sum(sizes),
        "output_limit_replay": {str(cap): sum(min(n, cap) for n in sizes) for cap in (10000, 5000)},
        "limits": "Byte/4 estimates from stored output, same-record cap replay; live host truncation and later rereads unverified."}


def recall_history_report(vault, log):
    from epitype import memsearch, memspec
    from collections import Counter
    total, omitted, superseded, unknown = 0, Counter(), Counter(), 0
    for line in Path(log).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        total += 1
        source_vault = Path(row.get("vault") or vault).resolve()
        path = (source_vault / str(row.get("card", ""))).resolve()
        if not path.is_relative_to(source_vault):
            unknown += 1
            continue
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            unknown += 1
            continue
        front, _ = memspec.split_frontmatter(raw)
        if front is None:
            continue
        fields, _ = memspec.frontmatter_fields(path)
        description = fields.get("description", "")
        scope = fields.get("scope", "")
        status = fields.get("status", "")
        if status == "superseded":
            superseded[str(row.get("card"))] += 1
        elif memsearch.historical_only(description, scope, status):
            omitted[str(row.get("card"))] += 1
    return {"delivery_rows_before": total, "same_rows_after_filter": total - sum(omitted.values()),
        "historical_rows_removed": sum(omitted.values()), "omitted_cards": dict(omitted),
        "currently_superseded_rows_already_filtered": sum(superseded.values()),
        "unreadable_rows": unknown, "limits": "Same delivery ledger reclassified using current cards, not live savings; historical card revisions unavailable. Superseded rows are not credited to the new filter."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--host", choices=("claude", "codex", "recall"), default="claude")
    parser.add_argument("--window", type=int)
    parser.add_argument("--carry", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--delivery-log", type=Path)
    args = parser.parse_args()
    if args.host == "claude" and (args.window is None or args.carry is None):
        parser.error("Claude replay requires --window and --carry assumptions")
    if args.host == "recall":
        if not args.delivery_log or len(args.paths) != 1:
            parser.error("recall replay requires one vault and --delivery-log")
        data = recall_history_report(args.paths[0], args.delivery_log)
    elif args.host == "codex":
        data = codex_output_report(args.paths)
    else:
        data = report(args.paths, args.window, args.carry)
    payload = json.dumps(data, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
