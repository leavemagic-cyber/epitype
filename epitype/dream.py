import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8；唯讀 CLI 不落 pyc。
"""Epitype 夢——離線整理審核包的確定性盤點入口。

不呼叫任何模型。「醒時」（hook）已經有的確定性檢查（別名匯出、卡片型別 lint、殭屍
待辦、承諾帳本、草稿、決策鏈、事件卡老化）在這裡各跑一次唯讀盤點，彙整成一份審核
包（Markdown 或 JSON），讓 owner 或子代理一眼看到今晚該整理什麼、該跑哪個既有指令
——這裡本身不套用任何建議，套用一律由列出的指令另外執行。
"""

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

try:
    from . import alias_batch, card_lint, commitments, decision_lint, memsearch, memspec, pending_lint
except ImportError:  # Direct script execution keeps the CLI contract.
    import alias_batch
    import card_lint
    import commitments
    import decision_lint
    import memsearch
    import memspec
    import pending_lint

EXAMPLE_LIMIT = 10
DEFAULT_EVENT_AGING_DAYS = 90
RECENT_WINDOW_DAYS = 7
DRAFT_DIRNAME = "_drafts"
_EVENT_TYPE_BY_DIR = dict(memspec.EVENT_CARD_DIRECTORIES)  # {"grants": "grant", ...}


# --------------------------------------------------------------------------- helpers


def _bounded(vaults, fn):
    """跑一個逐 vault 的函式；單一 vault 出錯只跳過那個 vault，其餘照跑。"""
    results = []
    errors = []
    for vault in vaults:
        try:
            results.append((vault, fn(vault)))
        except Exception as exc:
            errors.append(f"{vault}: {type(exc).__name__}: {exc}")
    return results, errors


def _iso_date_of(value):
    if not value or not memspec.is_iso_date(value):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- section 1


def _section_missing_aliases(vaults, today, since_date):
    results, errors = _bounded(vaults, lambda v: alias_batch._export_candidates(v, True, None))
    entries = []
    for vault, candidates in results:
        for item in candidates:
            entries.append({"vault": str(vault), "card_path": item["card_path"], "aliases_now": item["aliases_now"]})
    entries.sort(key=lambda item: (item["vault"], item["card_path"]))
    commands = [f'epitype aliases export "{vault}"' for vault in vaults] if entries else []
    return {
        "counts": {"missing_aliases": len(entries)},
        "examples": entries[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 2


def _section_card_lint(vaults, today, since_date):
    results, errors = _bounded(vaults, lambda v: card_lint.scan_vault(v, today))
    fail = warn = fail_cards = 0
    cards = []
    for vault, report in results:
        fail += report["fail"]
        warn += report["warn"]
        fail_cards += report["fail_cards"]
        for card in report["cards"]:
            cards.append({"vault": str(vault), "path": card["path"], "fail": card["fail"], "warn": card["warn"]})
    cards.sort(key=lambda item: (-item["fail"], -item["warn"], item["path"]))
    commands = [f'epitype cards "{vault}"' for vault in vaults] if (fail or warn) else []
    return {
        "counts": {"fail": fail, "warn": warn, "fail_cards": fail_cards},
        "examples": cards[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 3


def _section_pending(vaults, today, since_date):
    results, errors = _bounded(vaults, lambda v: pending_lint.scan_vault(v, today=today))
    zombie_cards = zombie_lines = oldest = 0
    entries = []
    for vault, report in results:
        zombie_cards += report["zombie_cards"]
        zombie_lines += report["zombie_lines"]
        oldest = max(oldest, report["oldest_days"])
        for card in report["cards"]:
            entries.append({"vault": str(vault), "path": card["path"], "oldest_days": card["oldest_days"]})
    entries.sort(key=lambda item: -item["oldest_days"])
    commands = [f'epitype pending "{vault}"' for vault in vaults] if zombie_lines else []
    return {
        "counts": {"zombie_cards": zombie_cards, "zombie_lines": zombie_lines, "oldest_days": oldest},
        "examples": entries[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 4


def _section_commitments(vaults, today, since_date):
    results, errors = _bounded(vaults, lambda v: commitments.open_items(v, limit=None))
    entries = []
    for vault, rows in results:
        for row in rows:
            entries.append({
                "vault": str(vault),
                "digest": row.get("digest"),
                "ts": row.get("ts"),
                "text": row.get("text"),
            })
    entries.sort(key=lambda item: str(item.get("ts")), reverse=True)
    commands = [f'epitype commitments "{vault}"' for vault in vaults] if entries else []
    return {
        "counts": {"open_commitments": len(entries)},
        "examples": entries[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 5


def _drafts_of(vault):
    root = Path(vault) / DRAFT_DIRNAME
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.md"))


def _section_drafts(vaults, today, since_date):
    results, errors = _bounded(vaults, _drafts_of)
    by_subdir = {}
    entries = []
    for vault, paths in results:
        for path in paths:
            relative = path.relative_to(vault).as_posix()
            parts = relative.split("/")
            subdir = parts[1] if len(parts) > 2 else "(root)"
            by_subdir[subdir] = by_subdir.get(subdir, 0) + 1
            entries.append({"vault": str(vault), "path": relative})
    commands = []
    for vault, paths in results:
        if paths:
            commands.append(
                f'python epitype/harvest.py --reevaluate "{Path(vault) / DRAFT_DIRNAME / "decisions"}" [--apply]'
            )
    return {
        "counts": {"total_drafts": len(entries), "by_subdir": by_subdir},
        "examples": entries[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 6


def _stop_gate_forbidden_reader():
    """A callable (path) -> bool: does this card declare a non-empty `forbidden`?

    memspec.frontmatter_fields (what decision_lint.Card.fields carries) only returns
    flat top-level scalars, so a `forbidden:` written as a YAML block list (or an
    inline `[a, b]`) reads back as "" and would show up here as a false
    "missing_forbidden". adapters/claude/stop_gate.py already has to solve exactly
    this (its own gate rules on `forbidden`), so this reuses that reader instead of
    writing a third YAML-list parser.
    """
    repo_root = Path(__file__).resolve().parents[1]
    claude_adapter_dir = repo_root / "adapters" / "claude"
    for extra in (str(repo_root), str(claude_adapter_dir)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import stop_gate
    from pretooluse_gate import _inline_items

    def has_forbidden(path):
        front_lines = stop_gate._decision_frontmatter(path)
        if front_lines is None:
            return False
        values = stop_gate._sequence_fields(front_lines, memspec.TOP_LEVEL_FIELD, _inline_items)
        return bool(values[memspec.FORBIDDEN_FIELD])

    return has_forbidden


def _section_decisions(vaults, today, since_date):
    results, errors = _bounded(vaults, decision_lint.lint_vault)
    has_forbidden_reader = _stop_gate_forbidden_reader()
    active = superseded = 0
    missing = []
    for vault, report in results:
        for card in report.cards:
            status = card.fields.get(memspec.DECISION_STATUS_FIELD, "").strip()
            if status == memspec.ACTIVE_DECISION_STATUS:
                active += 1
                has_quote = bool(card.fields.get(memspec.OWNER_QUOTE_FIELD, "").strip())
                has_forbidden = has_forbidden_reader(card.path)
                if not has_quote or not has_forbidden:
                    try:
                        relative = card.path.relative_to(vault).as_posix()
                    except ValueError:
                        relative = str(card.path)
                    missing.append({
                        "vault": str(vault),
                        "path": relative,
                        "missing_owner_quote": not has_quote,
                        "missing_forbidden": not has_forbidden,
                    })
            elif status == memspec.SUPERSEDED_DECISION_STATUS:
                superseded += 1
    missing.sort(key=lambda item: item["path"])
    commands = [f'epitype decisions "{vault}"' for vault in vaults] if missing else []
    return {
        "counts": {"active": active, "superseded": superseded, "missing_owner_or_forbidden": len(missing)},
        "examples": missing[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 7


def _event_cards_of(vault):
    found = []
    for path in memsearch.card_files(vault):
        relative = path.relative_to(vault).as_posix()
        top_dir = relative.split("/", 1)[0]
        card_type = _EVENT_TYPE_BY_DIR.get(top_dir)
        if card_type is None:
            continue
        fields, _problem = memspec.frontmatter_fields(path)
        found.append((relative, card_type, fields.get(memspec.CAPTURED_AT_FIELD, "").strip()))
    return found


def _section_event_aging(vaults, today, since_date):
    results, errors = _bounded(vaults, _event_cards_of)
    counts_by_type = {card_type: 0 for card_type in _EVENT_TYPE_BY_DIR.values()}
    aging_by_type = {card_type: 0 for card_type in _EVENT_TYPE_BY_DIR.values()}
    candidates = []
    for vault, found in results:
        for relative, card_type, captured in found:
            counts_by_type[card_type] += 1
            captured_date = _iso_date_of(captured)
            if captured_date is not None and captured_date < since_date:
                aging_by_type[card_type] += 1
                candidates.append({
                    "vault": str(vault),
                    "path": relative,
                    "type": card_type,
                    "captured_at": captured,
                })
    candidates.sort(key=lambda item: item["captured_at"])
    total_aging = sum(aging_by_type.values())
    commands = ["人工複核候選事件卡，決定是否歸檔（保留，不刪）；沒有對應的自動 CLI 指令"] if total_aging else []
    return {
        "counts": {"by_type": counts_by_type, "aging_by_type": aging_by_type, "aging_total": total_aging},
        "examples": candidates[:EXAMPLE_LIMIT],
        "commands": commands,
        "errors": errors,
    }


# --------------------------------------------------------------------------- section 8


def _git_added_dates(vault):
    """{相對 posix 路徑: 最早新增日期}；不是 git repo 或指令不可用時回傳 {}。"""
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(vault), "log", "--diff-filter=A", "--name-only", "--format=C\t%ad", "--date=short"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    added = {}
    current = None
    for raw_line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if raw_line.startswith("C\t"):
            current = raw_line.split("\t", 1)[1].strip()
            continue
        line = raw_line.strip()
        if not line or current is None:
            continue
        # git log walks newest commit first; keep the OLDEST add date per path.
        added[line] = min(added[line], current) if line in added else current
    return added


def _recent_cards_of(vault):
    git_dates = _git_added_dates(vault)
    entries = []
    for path in memsearch.card_files(vault):
        relative = path.relative_to(vault).as_posix()
        fields, _problem = memspec.frontmatter_fields(path)
        captured_date = _iso_date_of(fields.get(memspec.CAPTURED_AT_FIELD, "").strip())
        source = "captured_at" if captured_date is not None else None
        if captured_date is None and relative in git_dates:
            try:
                captured_date = date.fromisoformat(git_dates[relative])
                source = "git"
            except ValueError:
                captured_date = None
        if captured_date is None:
            try:
                captured_date = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
                source = "mtime"
            except OSError:
                continue
        entries.append((relative, captured_date, source))
    return entries


def _section_recent(vaults, today, since_date):
    results, errors = _bounded(vaults, _recent_cards_of)
    recent = []
    for vault, entries in results:
        for relative, added_date, source in entries:
            if (today - added_date).days <= RECENT_WINDOW_DAYS:
                recent.append({"vault": str(vault), "path": relative, "date": added_date.isoformat(), "source": source})
    recent.sort(key=lambda item: item["date"], reverse=True)
    return {
        "counts": {"recent_7d": len(recent)},
        "examples": recent[:EXAMPLE_LIMIT],
        "commands": [],
        "errors": errors,
    }


# --------------------------------------------------------------------------- report assembly


_SECTIONS = (
    (1, "缺別名卡", _section_missing_aliases),
    (2, "卡片型別檢查 FAIL／WARN", _section_card_lint),
    (3, "殭屍待辦", _section_pending),
    (4, "AI 未兌現承諾", _section_commitments),
    (5, "草稿待審", _section_drafts),
    (6, "裁定鏈", _section_decisions),
    (7, "事件卡老化", _section_event_aging),
    (8, "最近 7 天新增卡數", _section_recent),
)


def _next_steps(sections):
    by_id = {section["id"]: section for section in sections}

    def counts(section_id):
        section = by_id.get(section_id) or {}
        return section.get("counts") or {}

    steps = []
    card = counts(2)
    if card.get("fail", 0) > 0:
        steps.append(f"卡片型別檢查有 FAIL {card['fail']} 筆，先修 → epitype cards <vault>")
    alias = counts(1)
    if alias.get("missing_aliases", 0) > 20:
        steps.append(f"缺別名卡 {alias['missing_aliases']} 張超過門檻，跑別名批次 → epitype aliases export <vault>")
    draft = counts(5)
    if draft.get("total_drafts", 0) > 0:
        steps.append(f"草稿待審 {draft['total_drafts']} 份 → 人工審閱 _drafts/**")
    pending = counts(3)
    if pending.get("zombie_cards", 0) > 0:
        steps.append(f"殭屍待辦 {pending['zombie_lines']} 行／{pending['zombie_cards']} 卡 → epitype pending <vault>")
    commitment = counts(4)
    if commitment.get("open_commitments", 0) > 0:
        steps.append(f"未兌現承諾 {commitment['open_commitments']} 筆 → epitype commitments <vault>")
    decision = counts(6)
    if decision.get("missing_owner_or_forbidden", 0) > 0:
        steps.append(f"active 決策卡缺 owner_quote／forbidden 共 {decision['missing_owner_or_forbidden']} 張 → epitype decisions <vault>")
    event = counts(7)
    if event.get("aging_total", 0) > 0:
        steps.append(f"事件卡老化候選 {event['aging_total']} 張 → 人工複核是否歸檔（不刪）")
    if not steps:
        steps.append("目前沒有需要今晚整理的項目。")
    return steps


def build_report(vaults, today=None, since_date=None):
    today = today or datetime.now(timezone.utc).date()
    since_date = since_date or (today - timedelta(days=DEFAULT_EVENT_AGING_DAYS))
    sections = []
    for section_id, title, fn in _SECTIONS:
        try:
            data = fn(vaults, today, since_date)
            sections.append({"id": section_id, "title": title, "error": None, **data})
        except Exception as exc:
            sections.append({"id": section_id, "title": title, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "vaults": [str(vault) for vault in vaults],
        "today": today.isoformat(),
        "since": since_date.isoformat(),
        "sections": sections,
        "next_steps": _next_steps(sections),
    }


def _render_markdown(report):
    lines = [f"# Epitype Dream Pack — {report['today']}", ""]
    lines.append("Vaults:")
    for vault in report["vaults"]:
        lines.append(f"- {vault}")
    lines.append(f"事件卡老化門檻（--since）：{report['since']}")
    lines.append("")
    for section in report["sections"]:
        lines.append(f"## {section['id']}. {section['title']}")
        if section.get("error"):
            lines.append(f"（此節失敗：{section['error']}）")
            lines.append("")
            continue
        lines.append("counts: " + json.dumps(section.get("counts", {}), ensure_ascii=False))
        for error in section.get("errors") or ():
            lines.append(f"（部分 vault 略過：{error}）")
        examples = section.get("examples") or []
        if examples:
            lines.append(f"examples (前 {len(examples)} 筆):")
            for item in examples:
                lines.append(f"- {item}")
        for command in section.get("commands") or ():
            lines.append(f"建議指令：{command}")
        note = section.get("note")
        if note:
            lines.append(f"備註：{note}")
        lines.append("")
    lines.append("## 9. 夢的下一步")
    for step in report["next_steps"]:
        lines.append(f"- {step}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- selftest


def _write_card(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-dream-") as temp_dir:
            vault = Path(temp_dir).resolve() / "vault"
            vault.mkdir()
            today = date(2026, 9, 6)

            # section 1: one card with no aliases, one with plenty.
            _write_card(
                vault / "feedback" / "no_alias.md",
                "---\nname: no_alias\ndescription: 2026-06-01 synthetic feedback\n---\nbody\n",
            )
            _write_card(
                vault / "feedback" / "has_alias.md",
                "---\nname: has_alias\ndescription: 2026-06-01 synthetic feedback\naliases:\n- a\n- b\n---\nbody\n",
            )

            # section 2: a FAIL card (no frontmatter) and a clean one.
            _write_card(vault / "feedback" / "broken.md", "no frontmatter here\n")
            _write_card(
                vault / "feedback" / "clean.md",
                "---\nname: clean\ndescription: 2026-06-01 中文 synthetic\naliases:\n- x\n---\nbody\n",
            )

            # section 3: an overdue pending line.
            _write_card(
                vault / "feedback" / "plan.md",
                "---\nname: plan\ndescription: 2026-06-01 synthetic（待 owner 決策項）\naliases:\n- plan\n---\n"
                "- 2026-07-01 待辦：跑掉這行\n",
            )

            # section 4: one open commitment in the ledger.
            (vault / memspec.FTS_INDEX_DIRECTORY).mkdir(parents=True, exist_ok=True)
            ledger = vault / memspec.FTS_INDEX_DIRECTORY / memspec.COMMITMENT_LEDGER_FILENAME
            ledger.write_text(
                json.dumps({
                    "digest": "abc123456789",
                    "ts": "2026-08-01T00:00:00Z",
                    "text": "我會做 X。",
                    "status": memspec.COMMITMENT_OPEN_STATUS,
                    "session_id": "s1",
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            # section 5: two drafts in one subdirectory.
            _write_card(vault / "_drafts" / "decisions" / "d1.md", "draft one\n")
            _write_card(vault / "_drafts" / "decisions" / "d2.md", "draft two\n")

            # section 6: one active decision missing owner_quote, one clean superseded.
            _write_card(
                vault / "missing_quote.md",
                "---\ndecision_key: k1\nstatus: active\ncurrent_decision_at: 2026-06-01\n"
                "decided_by: ai-autonomous\naliases:\n- k1\n---\nbody\n",
            )
            _write_card(
                vault / "old_decision.md",
                "---\ndecision_key: k1\nstatus: superseded\nsuperseded_by: missing_quote.md\n"
                "current_decision_at: 2026-05-01\ndecided_by: ai-autonomous\naliases:\n- k1\n---\nbody\n",
            )
            # section 6 (fix): forbidden written as a YAML block list must not be
            # misread as missing — memspec.frontmatter_fields only returns flat
            # scalars, so this active card would false-positive without the
            # stop_gate sequence reader.
            _write_card(
                vault / "block_forbidden.md",
                "---\ndecision_key: k2\nstatus: active\ncurrent_decision_at: 2026-06-01\n"
                "decided_by: ai-autonomous\nowner_quote: 就這樣\nforbidden:\n  - 不要這樣做\n"
                "aliases:\n- k2\n---\nbody\n",
            )

            # section 7 + 8: one old grant (aging candidate), one fresh grant (recent).
            _write_card(
                vault / "grants" / "old_grant.md",
                "---\nname: old_grant\ndescription: synthetic\ncaptured_at: 2025-01-01\nsession_id: s1\n---\nbody\n",
            )
            _write_card(
                vault / "grants" / "new_grant.md",
                "---\nname: new_grant\ndescription: synthetic\ncaptured_at: 2026-09-05\nsession_id: s1\n---\nbody\n",
            )

            # backdate everything so the mtime fallback in section 8 doesn't
            # pick up "just written by this test" as "recent" — only
            # new_grant.md's explicit captured_at should land in the window.
            old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
            for md_path in vault.rglob("*.md"):
                os.utime(md_path, (old_ts, old_ts))

            report = build_report([vault], today=today)
            by_id = {section["id"]: section for section in report["sections"]}

            checks.append(("all 8 deterministic sections present with no error", all(
                by_id[i]["error"] is None for i in range(1, 9)
            )))
            checks.append(("section 1 counts the alias-less card only", by_id[1]["counts"]["missing_aliases"] == 1
                and by_id[1]["examples"][0]["card_path"] == "feedback/no_alias.md"))
            checks.append(("section 2 sees the frontmatter-less FAIL card", by_id[2]["counts"]["fail"] >= 1
                and any(item["path"] == "feedback/broken.md" for item in by_id[2]["examples"])))
            checks.append(("section 3 counts the overdue pending line", by_id[3]["counts"]["zombie_lines"] == 1))
            checks.append(("section 4 counts the one open commitment", by_id[4]["counts"]["open_commitments"] == 1
                and by_id[4]["examples"][0]["digest"] == "abc123456789"))
            checks.append(("section 5 counts both drafts under their subdirectory", by_id[5]["counts"]["total_drafts"] == 2
                and by_id[5]["counts"]["by_subdir"].get("decisions") == 2))
            checks.append(("section 6 flags the active card missing owner_quote and counts superseded", (
                by_id[6]["counts"]["active"] == 2
                and by_id[6]["counts"]["superseded"] == 1
                and by_id[6]["counts"]["missing_owner_or_forbidden"] == 1
                and by_id[6]["examples"][0]["path"] == "missing_quote.md"
            )))
            checks.append(("section 6 does not flag a block-list forbidden as missing", not any(
                item["path"] == "block_forbidden.md" for item in by_id[6]["examples"]
            )))
            checks.append(("section 7 flags the old grant as an aging candidate, not the new one", (
                by_id[7]["counts"]["by_type"]["grant"] == 2
                and by_id[7]["counts"]["aging_total"] == 1
                and by_id[7]["examples"][0]["path"] == "grants/old_grant.md"
            )))
            checks.append(("section 8 counts the fresh grant as recent, not the old one", (
                by_id[8]["counts"]["recent_7d"] == 1
                and by_id[8]["examples"][0]["path"] == "grants/new_grant.md"
            )))

            # --since moved before old_grant's captured_at (2025-01-01) clears it as
            # a candidate: only cards captured *before* the cutoff count as aging.
            loose_report = build_report([vault], today=today, since_date=date(2024, 1, 1))
            loose_by_id = {section["id"]: section for section in loose_report["sections"]}
            checks.append(("--since moved earlier than old_grant's date clears the aging candidate", loose_by_id[7]["counts"]["aging_total"] == 0))

            # a broken section function must not take the rest of the report down.
            global _SECTIONS
            saved_sections = _SECTIONS
            def _boom(vaults, today, since_date):
                raise RuntimeError("boom")
            try:
                _SECTIONS = tuple((sid, title, _boom if sid == 4 else fn) for sid, title, fn in _SECTIONS)
                broken_report = build_report([vault], today=today)
            finally:
                _SECTIONS = saved_sections
            broken_by_id = {section["id"]: section for section in broken_report["sections"]}
            checks.append(("a broken section fails alone; siblings still report their numbers", (
                broken_by_id[4]["error"] is not None
                and broken_by_id[1]["error"] is None
                and broken_by_id[2]["error"] is None
                and broken_by_id[1]["counts"]["missing_aliases"] == 1
            )))

            # next steps surface the pending/commitment/decision findings deterministically.
            checks.append(("next steps name the overdue pending line and the open commitment", any(
                "殭屍待辦" in step for step in report["next_steps"]
            ) and any("未兌現承諾" in step for step in report["next_steps"])))

            # --dry-run prints to the given stream and writes nothing to disk.
            import io
            out = io.StringIO()
            code = main(["--dry-run", "--today", "2026-09-06", os.fspath(vault)], output=out)
            dream_dir = vault / ".epitype"
            existing_before = set(dream_dir.glob("dream_pack_*.md")) if dream_dir.is_dir() else set()
            checks.append(("--dry-run exits 0, prints the report, writes no pack file", (
                code == 0 and "Epitype Dream Pack" in out.getvalue() and existing_before == set()
            )))

            # default --out path and --json both work and agree on the numbers.
            code = main(["--today", "2026-09-06", os.fspath(vault)], output=io.StringIO())
            default_out = vault / ".epitype" / "dream_pack_20260906.md"
            checks.append(("default run writes the dated pack under <vault>/.epitype", code == 0 and default_out.is_file()))

            custom_out = Path(temp_dir).resolve() / "custom_pack.json"
            out2 = io.StringIO()
            code = main(["--today", "2026-09-06", "--out", os.fspath(custom_out), "--json", os.fspath(vault)], output=out2)
            checks.append(("--out redirects the write target", code == 0 and custom_out.is_file()))
            parsed = json.loads(custom_out.read_text(encoding="utf-8"))
            parsed_by_id = {section["id"]: section for section in parsed["sections"]}
            checks.append(("--json output parses and matches the in-process counts", (
                parsed_by_id[6]["counts"] == by_id[6]["counts"]
                and parsed_by_id[3]["counts"] == by_id[3]["counts"]
            )))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 17
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


# --------------------------------------------------------------------------- CLI


def main(argv=None, output=sys.stdout):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vaults", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--since", type=date.fromisoformat, default=None,
                         help=f"事件卡老化門檻 ISO 日期；預設今天往前 {DEFAULT_EVENT_AGING_DAYS} 天")
    parser.add_argument("--today", type=date.fromisoformat, default=None, help="ISO date override for reproducible runs")
    parser.add_argument("--dry-run", action="store_true", help="只印到 stdout，不寫檔")
    parsed = parser.parse_args(arguments)

    vaults = []
    for raw in parsed.vaults:
        try:
            vaults.append(memsearch._resolve_vault(raw))
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    today = parsed.today or datetime.now(timezone.utc).date()
    since_date = parsed.since or (today - timedelta(days=DEFAULT_EVENT_AGING_DAYS))
    report = build_report(vaults, today=today, since_date=since_date)
    content = json.dumps(report, ensure_ascii=False, indent=1) if parsed.json else _render_markdown(report)

    if parsed.dry_run:
        print(content, file=output)
        return 0

    out_path = parsed.out.resolve() if parsed.out else (vaults[0] / ".epitype" / f"dream_pack_{today.strftime('%Y%m%d')}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"DREAM PACK {out_path}", file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
