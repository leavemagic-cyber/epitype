import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude SessionStart adapter for slim index and work-ledger injection."""

import json
import os
from pathlib import Path
import re
import tempfile

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import card_lint, commitments, memsearch, memspec, pending_lint
from _hook_common import (
    bounded_context,
    emit,
    expired,
    governance_vault,
    load_config,
    native_cwd_vaults,
    payload,
    read_event,
    resolve_vaults,
    run_synthetic,
    write_config,
)


def _frontmatter_fields(path):
    """Top-level frontmatter scalars for one card, discarding the diagnostics.

    U38: the parse itself lives once, in memspec.frontmatter_fields — the same
    duplicate-key-first-wins and block-scalar rules decision_lint._parse_frontmatter
    uses. No decision_lint import on this hot path: its argparse/dataclasses
    cost is real time SessionStart must not pay; memspec alone is what the
    rest of this hook already imports.
    """
    fields, _problem = memspec.frontmatter_fields(path)
    return fields


def _active_decisions(vault, started_at):
    """(current_decision_at, decision_key, owner's words) per active decision card.

    Read from disk rather than from the index: the index carries `status` but
    neither the decision's key nor the owner's words, and a vault whose index was
    never built must still open the session with its standing rulings. None when
    the hook's deadline arrives mid-scan — half a vault's rulings would read as
    the whole list.
    """
    found = []
    try:
        paths = memsearch.card_files(vault)
    except Exception:
        return None
    for path in paths:
        if expired(started_at):
            return None
        try:
            fields = _frontmatter_fields(path)
        except Exception:
            continue
        key = " ".join(str(fields.get(memspec.DECISION_KEY_FIELD) or "").split())
        status = " ".join(str(fields.get(memspec.DECISION_STATUS_FIELD) or "").split())
        if not key or status != memspec.ACTIVE_DECISION_STATUS:
            continue
        said = " ".join(str(fields.get(memspec.OWNER_QUOTE_FIELD) or "").split())
        if not said:
            said = " ".join(str(fields.get(memspec.DESCRIPTION_FIELD) or "").split())
        found.append(
            (
                " ".join(str(fields.get(memspec.CURRENT_DECISION_AT_FIELD) or "").split()),
                key,
                said[: memspec.SESSIONSTART_DECISION_QUOTE_CHARS],
            )
        )
    found.sort(key=lambda row: row[1])
    found.sort(key=lambda row: row[0], reverse=True)  # newest first; undated last
    return found


def _decision_block(vault, label, started_at):
    """The vault's standing rulings as one piece: a header without its rulings,
    or rulings without the vault they bind, is worse than no block at all."""
    rows = _active_decisions(vault, started_at)
    if not rows:
        return None
    lines = [memspec.SESSIONSTART_DECISIONS_HEADER.format(vault=label)]
    for decided_at, key, said in rows[: memspec.SESSIONSTART_DECISIONS_MAX_LINES]:
        lines.append("｜".join(part for part in (key, decided_at, said) if part))
    dropped = len(rows) - memspec.SESSIONSTART_DECISIONS_MAX_LINES
    if dropped > 0:
        lines.append(f'…另 {dropped} 條：python epitype/decision_lint.py "{vault}"')
    return "\n".join(lines)


def _vault_labels(vaults):
    """Directory names, falling back to the full path where a name repeats: every
    native cwd vault is called `memory`, so the short name alone can be a lie."""
    names = [vault.name for vault in vaults]
    return [
        name if names.count(name) == 1 else str(vault)
        for vault, name in zip(vaults, names)
    ]


def _handle(event, started_at):
    config = load_config(started_at)
    if config is None:
        return None
    budget = config[memspec.CONFIG_BUDGET_BYTES_FIELD]
    pieces = []
    # Session start carries the cwd's own vault(s) plus the governance vault;
    # another project's index and ledger are noise here and were crowding the
    # budget. Recall still reaches that project's cards by content.
    # The governance vault is the one holding the working ledger, not whichever
    # path sorted first: the installer sorts vaults alphabetically, so position
    # carries no meaning, and a cwd vault that also appears in the configured
    # list must never be dropped (adversarial review 2026-09-03 #3, #7).
    resolved = resolve_vaults(config, event)
    native = native_cwd_vaults(event.get("cwd") if isinstance(event, dict) else None)
    governance = governance_vault(config)
    has_governance_ledger = (governance / memspec.WORK_LEDGER_FILENAME).is_file()
    if not has_governance_ledger:
        vaults = resolved  # no ledger anywhere: keep the pre-1.1.0 behaviour
    else:
        vaults = [vault for vault in resolved if vault in native or vault == governance]

    # One line, first, so the budget cannot drop it: pending items with an entry
    # and no exit are exactly what resurfaces as wrong memory later.
    if not expired(started_at):
        overdue = pending_lint.summary_line(vaults)
        if overdue:
            pieces.append(overdue)

    # U53：AI 自己開的承諾（「我等一下會…」）沒有任何人在追，而 compaction 正是它蒸發
    # 的時刻——所以 source: compact 也印。這不是 owner 的待辦，帳本另放，一行帶最近一條。
    if not expired(started_at):
        promised = commitments.summary_line(vaults, memspec.COMMITMENT_SESSIONSTART_MAX)
        if promised:
            pieces.append(promised)

    # A card missing its type's required fields is a card the recall side will
    # hand over half-true. One line, and only when the scan finished inside its
    # own budget: half a vault's numbers are worse than no numbers.
    if not expired(started_at):
        malformed = card_lint.summary_line(vaults)
        if malformed:
            pieces.append(malformed)

    # 2026-09-05 事故：owner 08-13 親裁的事被端回來當選項。A standing ruling the model
    # cannot see is a ruling it re-opens, so every session — including the one that
    # resumes after a compaction — opens with the vault's active decisions, in the
    # owner's own words, before any index.
    for vault, label in zip(vaults, _vault_labels(vaults)):
        if expired(started_at):
            break
        block = _decision_block(vault, label, started_at)
        if block:
            pieces.append(block)

    for vault in vaults:
        if expired(started_at):
            return None
        index_path = vault / memspec.MEMORY_INDEX_FILENAME
        if index_path.is_file():
            body = index_path.read_text(encoding="utf-8")
            slim = memspec.slim_index(
                body,
                min(memspec.SESSIONSTART_INDEX_BUDGET_BYTES, budget),
                index_path.resolve(),
            )
            pieces.append(f"## {memspec.MEMORY_INDEX_FILENAME}\n{slim}")

        ledger_path = vault / memspec.WORK_LEDGER_FILENAME
        if ledger_path.is_file():
            ledger = ledger_path.read_text(encoding="utf-8")
            pieces.append(f"## {memspec.WORK_LEDGER_FILENAME}")
            pieces.extend(ledger.splitlines())

    if expired(started_at):
        return None
    context = bounded_context("SessionStart", pieces, budget)
    return payload("SessionStart", context) if context else None


def _selftest():
    checks = []
    try:
        red_body = "# Heading\nordinary one\n🔴 urgent\n🔴🔴 critical\nordinary two\n"
        red_slim = memspec.slim_index(red_body, 256, "synthetic/MEMORY.md")
        checks.append(
            (
                "red priority retained",
                "🔴🔴 critical" in red_slim
                and "🔴 urgent" in red_slim
                and len(red_slim.encode("utf-8")) <= 256,
            )
        )

        plain_body = "# Plain\nalpha\nbeta\ngamma\n"
        plain_slim = memspec.slim_index(plain_body, 256, "synthetic/plain.md")
        checks.append(
            (
                "unmarked index retained",
                "alpha" in plain_slim and "beta" in plain_slim and "gamma" in plain_slim,
            )
        )

        with tempfile.TemporaryDirectory(prefix="epitype-sessionstart-") as temp_dir:
            root = Path(temp_dir).resolve()
            vault = root / "vault"
            vault.mkdir()
            (vault / memspec.MEMORY_INDEX_FILENAME).write_text(
                "# Synthetic Index\nindex detail\n",
                encoding="utf-8",
            )
            (vault / memspec.WORK_LEDGER_FILENAME).write_text(
                "ledger detail\n",
                encoding="utf-8",
            )
            config = root / "config.json"
            write_config(config, [vault])
            result = run_synthetic(Path(__file__), {"source": "startup"}, config)
            value = json.loads(result.stdout) if result.stdout.strip() else {}
            context = value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "index and ledger injection",
                    result.returncode == 0
                    and "index detail" in context
                    and "ledger detail" in context
                    and str((vault / memspec.MEMORY_INDEX_FILENAME).resolve()) in context,
                )
            )

            home = root / "home"
            project = root / "work" / "proj"
            project.mkdir(parents=True)
            slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
            native = home / ".claude" / "projects" / slug / "memory"
            native.mkdir(parents=True)
            (native / memspec.MEMORY_INDEX_FILENAME).write_text(
                "# Native Index\nnative index detail\n",
                encoding="utf-8",
            )
            native_result = run_synthetic(
                Path(__file__),
                {"source": "startup", "cwd": str(project)},
                config,
                environment={"HOME": os.fspath(home), "USERPROFILE": os.fspath(home)},
            )
            native_value = json.loads(native_result.stdout) if native_result.stdout.strip() else {}
            native_context = native_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "cwd-slug native index injected ahead of configured vaults",
                    native_result.returncode == 0
                    and native_context.index("native index detail") < native_context.index("index detail")
                    and "ledger detail" in native_context,
                )
            )

            (vault / "plan.md").write_text(
                "---\nname: plan\ndescription: synthetic plan\n---\n- 2026-07-22 未辦（owner 自行）：SWSetup\n",
                encoding="utf-8",
            )
            overdue_result = run_synthetic(Path(__file__), {"source": "startup"}, config)
            overdue_value = json.loads(overdue_result.stdout) if overdue_result.stdout.strip() else {}
            overdue_context = overdue_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "overdue pending line is announced first, in one line",
                    overdue_result.returncode == 0
                    and overdue_context.startswith("⏳ 殭屍待辦 1 行／1 卡")
                    and "index detail" in overdue_context,
                )
            )

            # A card that fails its type's required fields is named in one line;
            # a vault whose cards are all clean gets no line at all.
            card_vault = root / "card-vault"
            card_vault.mkdir()
            (card_vault / memspec.MEMORY_INDEX_FILENAME).write_text("# Cards\ncard index detail\n", encoding="utf-8")
            broken = card_vault / "broken-card.md"
            broken.write_text(
                "---\nname: broken-card\ndescription: english only and undated\n---\nbody\n",
                encoding="utf-8",
            )
            card_config = root / "card-config.json"
            write_config(card_config, [card_vault])
            broken_result = run_synthetic(Path(__file__), {"source": "startup"}, card_config)
            broken_value = json.loads(broken_result.stdout) if broken_result.stdout.strip() else {}
            broken_context = broken_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            broken.write_text(
                "---\nname: broken-card\ndescription: 2026-09-01 乾淨卡\naliases:\n  - 乾淨\nmetadata:\n  type: feedback\n---\nbody\n",
                encoding="utf-8",
            )
            clean_result = run_synthetic(Path(__file__), {"source": "startup"}, card_config)
            clean_value = json.loads(clean_result.stdout) if clean_result.stdout.strip() else {}
            clean_context = clean_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "card-type lint adds one line when a card FAILs and no line when every card is clean",
                    broken_result.returncode == 0
                    and clean_result.returncode == 0
                    and "🧾 卡片型別檢查：FAIL 1" in broken_context
                    and broken_context.count("🧾") == 1
                    and "card index detail" in broken_context
                    and "🧾" not in clean_context
                    and "card index detail" in clean_context,
                )
            )

            second = root / "second-vault"
            second.mkdir()
            (second / memspec.MEMORY_INDEX_FILENAME).write_text("# Second\nsecond index detail\n", encoding="utf-8")
            (second / memspec.WORK_LEDGER_FILENAME).write_text("second ledger detail\n", encoding="utf-8")
            two_config = root / "two-config.json"
            write_config(two_config, [vault, second])
            two_result = run_synthetic(Path(__file__), {"source": "startup"}, two_config)
            two_value = json.loads(two_result.stdout) if two_result.stdout.strip() else {}
            two_context = two_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "only the governance vault's index and ledger are injected, not another project's",
                    two_result.returncode == 0
                    and "index detail" in two_context
                    and "ledger detail" in two_context
                    and "second index detail" not in two_context
                    and "second ledger detail" not in two_context,
                )
            )

            # Adversarial review 2026-09-03 #3/#7: the cwd vault may also be a
            # configured one, and the governance vault is the ledger holder, not
            # whichever path the installer happened to sort first.
            both_home = root / "both-home"
            both_project = root / "both-work" / "proj"
            both_project.mkdir(parents=True)
            both_slug = re.sub(r"[^A-Za-z0-9]", "-", str(both_project))
            both_native = both_home / ".claude" / "projects" / both_slug / "memory"
            both_native.mkdir(parents=True)
            (both_native / memspec.MEMORY_INDEX_FILENAME).write_text("# Both\nboth native detail\n", encoding="utf-8")
            gov = root / "gov-vault"
            gov.mkdir()
            (gov / memspec.MEMORY_INDEX_FILENAME).write_text("# Gov\ngov index detail\n", encoding="utf-8")
            (gov / memspec.WORK_LEDGER_FILENAME).write_text("gov ledger detail\n", encoding="utf-8")
            other = root / "aaa-other-project"
            other.mkdir()
            (other / memspec.MEMORY_INDEX_FILENAME).write_text("# Other\nother index detail\n", encoding="utf-8")
            both_config = root / "both-config.json"
            write_config(both_config, [other, gov, both_native])
            both_result = run_synthetic(
                Path(__file__),
                {"source": "startup", "cwd": str(both_project)},
                both_config,
                environment={"HOME": os.fspath(both_home), "USERPROFILE": os.fspath(both_home)},
            )
            both_value = json.loads(both_result.stdout) if both_result.stdout.strip() else {}
            both_context = both_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "cwd vault survives being configured too; governance is the ledger holder",
                    both_result.returncode == 0
                    and "both native detail" in both_context
                    and "gov index detail" in both_context
                    and "gov ledger detail" in both_context
                    and "other index detail" not in both_context,
                )
            )

            # 2026-09-05 事故：owner 08-13 親裁的事被端回來當選項。開場要逐條列出該庫
            # 的現行裁定，帶原話、新→舊，壓縮後重注的那一場也一樣。
            decision_vault = root / "decision-vault"
            decision_vault.mkdir()
            (decision_vault / memspec.MEMORY_INDEX_FILENAME).write_text(
                "# Decisions\ndecision index detail\n", encoding="utf-8"
            )
            for index in range(14):
                (decision_vault / f"decision-{index:02d}.md").write_text(
                    f"---\nname: Decision {index}\ndescription: 2026 決策摘要 {index}\n"
                    f"{memspec.DECISION_KEY_FIELD}: rule-{index:02d}\n"
                    f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                    f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-08-{index + 1:02d}\n"
                    f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                    f"{memspec.OWNER_QUOTE_FIELD}: 只有 6s 是標準合約 {index}\n---\nbody\n",
                    encoding="utf-8",
                )
            (decision_vault / "decision-retired.md").write_text(
                "---\nname: Decision Retired\ndescription: 舊制\n"
                f"{memspec.DECISION_KEY_FIELD}: rule-retired\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.SUPERSEDED_DECISION_STATUS}\n"
                f"{memspec.SUPERSEDED_BY_FIELD}: decision-00.md\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-01-01\n---\nbody\n",
                encoding="utf-8",
            )
            decision_config = root / "decision-config.json"
            write_config(decision_config, [decision_vault])
            decision_result = run_synthetic(Path(__file__), {"source": "startup"}, decision_config)
            decision_value = json.loads(decision_result.stdout) if decision_result.stdout.strip() else {}
            decision_context = decision_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            decision_header = memspec.SESSIONSTART_DECISIONS_HEADER.format(vault=decision_vault.name)
            decision_lines = [
                line for line in decision_context.splitlines() if line.startswith("rule-")
            ]
            checks.append(
                (
                    "active decisions open the session in the owner's words, newest first, above the index",
                    decision_result.returncode == 0
                    and decision_header in decision_context
                    and decision_lines[:2] == [
                        "rule-13｜2026-08-14｜只有 6s 是標準合約 13",
                        "rule-12｜2026-08-13｜只有 6s 是標準合約 12",
                    ]
                    and "rule-retired" not in decision_context
                    and decision_context.index(decision_header)
                    < decision_context.index("decision index detail"),
                )
            )
            checks.append(
                (
                    "the list is capped and the cut is said with the command that shows the rest",
                    len(decision_lines) == memspec.SESSIONSTART_DECISIONS_MAX_LINES
                    and f'…另 2 條：python epitype/decision_lint.py "{decision_vault}"' in decision_context,
                )
            )
            compact_result = run_synthetic(Path(__file__), {"source": "compact"}, decision_config)
            compact_value = json.loads(compact_result.stdout) if compact_result.stdout.strip() else {}
            compact_context = compact_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "the session that resumes after a compaction gets the same decisions",
                    compact_result.returncode == 0
                    and decision_header in compact_context
                    and "rule-13｜2026-08-14｜只有 6s 是標準合約 13" in compact_context,
                )
            )
            plain_result = run_synthetic(Path(__file__), {"source": "startup"}, config)
            plain_value = json.loads(plain_result.stdout) if plain_result.stdout.strip() else {}
            plain_context = plain_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "a vault with no active decision card gets no block at all",
                    plain_result.returncode == 0
                    and "現行裁定" not in plain_context
                    and "index detail" in plain_context,
                )
            )
            # U53: an AI promise nobody is tracking gets one line, under the pending
            # line, and the compaction-resumed session needs it most of all.
            promise_vault = root / "promise-vault"
            promise_vault.mkdir()
            (promise_vault / memspec.MEMORY_INDEX_FILENAME).write_text(
                "# Cards\npromise index detail\n", encoding="utf-8"
            )
            (promise_vault / "plan.md").write_text(
                "---\nname: plan\ndescription: synthetic plan\n---\n- 2026-07-22 未辦（owner 自行）：SWSetup\n",
                encoding="utf-8",
            )
            promise_config = root / "promise-config.json"
            write_config(promise_config, [promise_vault])
            commitments.record(promise_vault, "sessionstart-promise", ["我等一下會補上 settle 的測試。"])
            promise_result = run_synthetic(Path(__file__), {"source": "startup"}, promise_config)
            promise_value = json.loads(promise_result.stdout) if promise_result.stdout.strip() else {}
            promise_context = promise_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            promise_line = "⏳ AI 未兌現承諾 1 條（最近：我等一下會補上 settle 的測試。…）"
            checks.append(
                (
                    "an open AI promise adds one line, after the owner's pending line",
                    promise_result.returncode == 0
                    and promise_line in promise_context
                    and promise_context.index("⏳ 殭屍待辦") < promise_context.index(promise_line)
                    and "promise index detail" in promise_context,
                )
            )
            promise_compact = run_synthetic(Path(__file__), {"source": "compact"}, promise_config)
            promise_compact_value = json.loads(promise_compact.stdout) if promise_compact.stdout.strip() else {}
            promise_compact_context = promise_compact_value.get("hookSpecificOutput", {}).get(
                "additionalContext", ""
            )
            checks.append(
                (
                    "the compaction-resumed session gets the promise line, and a vault with"
                    " no open promise gets none",
                    promise_compact.returncode == 0
                    and promise_line in promise_compact_context
                    and "AI 未兌現承諾" not in plain_context,
                )
            )

            bad_config = root / "bad-config.json"
            bad_config.write_text("{broken", encoding="utf-8")
            bad_result = run_synthetic(
                Path(__file__),
                {"source": "startup"},
                bad_config,
            )
            checks.append(
                (
                    "bad config fails open silently",
                    bad_result.returncode == 0
                    and not bad_result.stdout
                    and not bad_result.stderr,
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 15
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main():
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    try:
        event = read_event(sys.stdin)
        value = _handle(event, _STARTED_AT)
        if value is not None and not expired(_STARTED_AT):
            emit(value)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
