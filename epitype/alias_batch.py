import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8；唯讀/寫入 CLI 不落 pyc。
"""離線整理批次的確定性半邊：匯出缺別名的卡片工作清單，套用審核過的別名建議。

模型那一半（讀 body_head 想別名）不在本模組，由呼叫端另派子代理產生 suggested。
這裡只做兩件事：``export`` 把缺別名（或別名 <2 個）且非事件卡的卡片列成 JSON 清單；
``apply`` 把已審核、``suggested`` 已填好的同格式檔案套回卡片——規則是「只新增」：
不刪不改既有別名或任何其他欄位，frontmatter 界線與 scalar 一律沿用
``memspec.split_frontmatter``／``memspec.parse_scalar``，別名欄名沿用
``memspec.ALIASES_FIELD``，事件卡目錄沿用 ``memspec.EVENT_CARD_DIRECTORIES`` 同源。
"""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata

try:
    from . import decision_lint, memsearch, memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import decision_lint
    import memsearch
    import memspec


BODY_HEAD_CHARS = 600
ALIAS_MAX_CHARS = 40
MIN_ALIASES = 2  # 預設門檻：aliases_now 少於這個數字才列入 export 候選

_EVENT_DIRECTORIES = frozenset(directory for directory, _card_type in memspec.EVENT_CARD_DIRECTORIES)
_KEY_LINE_REGEX = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$")
_QUOTE_STRIP_CHARS = "'\"“”‘’「」『』`"


# --------------------------------------------------------------------------- export


def _is_event_card(relative_path):
    directories = relative_path.split("/")[:-1]
    return any(directory in _EVENT_DIRECTORIES for directory in directories)


def _export_candidates(vault, only_missing, limit):
    entries = []
    for path in memsearch.card_files(vault):
        relative = path.relative_to(vault).as_posix()
        if _is_event_card(relative):
            continue
        try:
            fields = memsearch._read_card(path)
        except OSError:
            continue
        if not fields["is_card"]:
            continue
        aliases_now = [item for item in fields[memspec.ALIASES_FIELD].split("\n") if item]
        if only_missing:
            if aliases_now:
                continue
        elif len(aliases_now) >= MIN_ALIASES:
            continue
        entries.append({
            "card_path": relative,
            "name": fields["name"],
            "description": fields["description"],
            "aliases_now": aliases_now,
            "body_head": fields["body"][:BODY_HEAD_CHARS],
            "suggested": [],
        })
    entries.sort(key=lambda item: item["card_path"])
    if limit is not None:
        entries = entries[:limit]
    return entries


def _default_export_path(vault):
    return vault / ".epitype" / f"alias_work_{time.strftime('%Y%m%d')}.json"


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def cmd_export(args):
    vault = memsearch._resolve_vault(args.vault)
    entries = _export_candidates(vault, args.only_missing, args.limit)
    out_path = Path(args.out).resolve() if args.out else _default_export_path(vault)
    _write_json(out_path, entries)
    missing = sum(1 for entry in entries if not entry["aliases_now"])
    print(f"ALIAS EXPORT cards={len(entries)} missing={missing}")
    print(f"OUT {out_path}")
    return 0


# ---------------------------------------------------------------------------- apply


def _normalize_alias(value):
    """大小寫與全形/半形正規化後的比較鍵，供去重與碰撞檢查共用。"""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _clean_alias(raw):
    if not isinstance(raw, str):
        return ""
    if any(unicodedata.category(char) in ("Cc", "Zl", "Zp") for char in raw):
        return ""
    value = raw.strip().strip(_QUOTE_STRIP_CHARS).strip()
    return value


def _needs_quoting(value):
    if not value or value[0] in "'\"[]{}#&*!|>%@`-":
        return True
    if value.strip() != value:
        return True
    if ": " in value or value.endswith(":") or any(char in value for char in ",[]{}#\"'"):
        return True
    return False


def _format_alias(value):
    if _needs_quoting(value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _line_terminator(line):
    for terminator in ("\r\n", "\n"):
        if line.endswith(terminator):
            return terminator
    return ""


def _default_terminator(lines_with_ends):
    for line in lines_with_ends:
        terminator = _line_terminator(line)
        if terminator:
            return terminator
    return "\n"


def _read_card_raw(path):
    """(bom, text, lines_with_ends, closing_index) 或 None（沒有可用 frontmatter）。

    text 已剝除 BOM 位元組但保留其餘每一個位元組（含 CRLF）；
    ``"".join(lines_with_ends) == text`` 恆成立，寫回時才能只在插入點動筆。
    """
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    content = raw[3:] if bom else raw
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    front_lines, closing = memspec.split_frontmatter(text)
    if front_lines is None or closing is None:
        return None
    lines_with_ends = text.splitlines(keepends=True)
    return bom, text, lines_with_ends, closing


def _locate_aliases_block(front_body_lines):
    """既有 aliases 欄的位置與形狀。

    回傳 (kind, key_index, item_end_index)：
    - ("block", key_index, item_end_index)：區塊清單，item_end_index 是最後一個既有
      "- " 項目之後的插入點（沒有既有項目時等於 key_index + 1）。
    - ("flow", key_index, None)：單行 ``aliases: [a, b]``。
    - ("unsupported", None, None)：aliases 鍵存在但不是這兩種形狀（例如純量），
      呼叫端必須整張卡跳過，不得亂猜格式。
    - (None, None, None)：沒有 aliases 鍵。
    """
    for index, raw_line in enumerate(front_body_lines):
        stripped = raw_line.rstrip("\r\n")
        if not stripped or stripped[0].isspace():
            continue
        match = _KEY_LINE_REGEX.match(stripped)
        if match is None or match.group(1) != memspec.ALIASES_FIELD:
            continue
        value = match.group(2).strip()
        if not value:
            item_end = index + 1
            while item_end < len(front_body_lines):
                following = front_body_lines[item_end].rstrip("\r\n")
                if following[:1].isspace() and following.strip().startswith("-"):
                    item_end += 1
                    continue
                break
            return "block", index, item_end
        if value.startswith("[") and value.endswith("]"):
            return "flow", index, None
        return "unsupported", None, None
    return None, None, None


def _existing_item_indent(front_body_lines, key_index, item_end_index):
    for position in range(key_index + 1, item_end_index):
        stripped = front_body_lines[position].rstrip("\r\n")
        leading = stripped[: len(stripped) - len(stripped.lstrip())]
        if leading:
            return leading
    return "  "


def _insert_into_flow_line(raw_line, new_aliases):
    terminator = _line_terminator(raw_line)
    body = raw_line[: len(raw_line) - len(terminator)] if terminator else raw_line
    close_position = body.rfind("]")
    open_position = body.find("[")
    inner = body[open_position + 1:close_position]
    addition = ", ".join(_format_alias(alias) for alias in new_aliases)
    if inner.strip():
        separator = " " if inner.rstrip().endswith(",") else ", "
        new_body = body[:close_position] + separator + addition + body[close_position:]
    else:
        new_body = body[:close_position] + addition + body[close_position:]
    return new_body + terminator


def _insert_aliases(lines_with_ends, closing, location, new_aliases, terminator):
    kind, key_index, item_end_index = location
    lines = list(lines_with_ends)
    if kind == "flow":
        absolute = 1 + key_index
        lines[absolute] = _insert_into_flow_line(lines[absolute], new_aliases)
    elif kind == "block":
        indent = _existing_item_indent(lines_with_ends[1:closing], key_index, item_end_index)
        insert_at = 1 + item_end_index
        addition = [f"{indent}- {_format_alias(alias)}{terminator}" for alias in new_aliases]
        lines[insert_at:insert_at] = addition
    else:  # kind is None: no aliases key at all yet
        addition = [f"aliases:{terminator}"] + [
            f"  - {_format_alias(alias)}{terminator}" for alias in new_aliases
        ]
        lines[closing:closing] = addition
    return "".join(lines)


def _write_card(target, bom, new_text, *, expected):
    try:
        from . import card_io
    except ImportError:
        import card_io
    payload = new_text.encode("utf-8")
    if bom:
        payload = b"\xef\xbb\xbf" + payload
    original = (b"\xef\xbb\xbf" if bom else b"") + expected.encode("utf-8")
    card_io.replace_if_unchanged(target, payload, original)


def _decision_key_of(path):
    fields, _problem = decision_lint._parse_frontmatter(path)
    return fields.get(memspec.DECISION_KEY_FIELD, "").strip()


def _vault_decision_alias_index(vault):
    """{正規化別名: {決策卡相對路徑, ...}}，只掃決策卡既有別名（套用前的基準）。"""
    index = {}
    for path in memsearch.card_files(vault):
        if not _decision_key_of(path):
            continue
        relative = path.relative_to(vault).as_posix()
        try:
            fields = memsearch._read_card(path)
        except OSError:
            continue
        for alias in fields[memspec.ALIASES_FIELD].split("\n"):
            alias = alias.strip()
            if alias:
                index.setdefault(_normalize_alias(alias), set()).add(relative)
    return index


def _prepare_entry(vault, entry):
    """(prepared dict) 或 None（無法安全處理，已印出原因）。"""
    if not isinstance(entry, dict):
        return None
    card_path = entry.get("card_path")
    suggested = entry.get("suggested", [])
    if not isinstance(card_path, str) or not card_path or not isinstance(suggested, list):
        return None
    target = (vault / card_path).resolve()
    try:
        target.relative_to(vault)
    except ValueError:
        print(f"SKIP outside-vault {card_path}", file=sys.stderr)
        return None
    if not target.is_file():
        print(f"SKIP missing {card_path}", file=sys.stderr)
        return None
    raw = _read_card_raw(target)
    if raw is None:
        print(f"SKIP no-frontmatter {card_path}", file=sys.stderr)
        return None
    bom, text, lines_with_ends, closing = raw
    front_body_lines = lines_with_ends[1:closing]
    location = _locate_aliases_block(front_body_lines)
    if location[0] == "unsupported":
        print(f"SKIP unsupported-aliases-shape {card_path}", file=sys.stderr)
        return None
    try:
        existing_fields = memsearch._read_card(target)
    except OSError:
        return None
    existing_aliases = [item for item in existing_fields[memspec.ALIASES_FIELD].split("\n") if item]
    seen = {_normalize_alias(alias) for alias in existing_aliases}
    new_aliases = []
    for raw_alias in suggested:
        cleaned = _clean_alias(raw_alias)
        if not cleaned or len(cleaned) > ALIAS_MAX_CHARS:
            continue
        key = _normalize_alias(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        new_aliases.append(cleaned)
    return {
        "card_path": card_path,
        "target": target,
        "bom": bom,
        "original": text,
        "lines_with_ends": lines_with_ends,
        "closing": closing,
        "location": location,
        "terminator": _default_terminator(lines_with_ends),
        "new_aliases": new_aliases,
        "decision_key": _decision_key_of(target),
    }


def _collisions(vault, prepared):
    """(blocked_count, blocked)；只檢查決策卡（有 decision_key）。

    碰撞顆粒度是 (alias, cardA, cardB)：只擋撞名的那一個別名，同一張卡其餘
    suggested 別名不受影響。``blocked`` 是 {(card_path, normalized_alias_key), ...}，
    只含這次批次裡實際要寫入的卡（baseline-only 的既有決策卡不在其中，因為
    它們本來就不會被 cmd_apply 動到）。``blocked_count`` = len(blocked)，
    即被擋下的「別名 x 卡」筆數，不是碰撞配對／COLLISION 印出的行數。
    """
    baseline = _vault_decision_alias_index(vault)
    alias_to_batch_cards = {}
    for item in prepared:
        if not item["decision_key"]:
            continue
        for alias in item["new_aliases"]:
            alias_to_batch_cards.setdefault(_normalize_alias(alias), {})[item["card_path"]] = alias

    blocked = set()
    for key, owners_in_batch in alias_to_batch_cards.items():
        all_owners = set(owners_in_batch) | baseline.get(key, set())
        if len(all_owners) <= 1:
            continue
        alias_text = next(iter(owners_in_batch.values()))
        ordered = sorted(all_owners)
        for other in ordered[1:]:
            print(f"COLLISION {alias_text} {ordered[0]} {other}")
        for path in owners_in_batch:
            blocked.add((path, key))
    return len(blocked), blocked


def cmd_apply(args):
    vault = memsearch._resolve_vault(args.vault)
    try:
        entries = json.loads(Path(args.review).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ALIAS APPLY ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(entries, list):
        print("ALIAS APPLY ERROR review file must be a JSON array", file=sys.stderr)
        return 2

    prepared = [item for item in (_prepare_entry(vault, entry) for entry in entries) if item is not None]
    collisions, blocked = _collisions(vault, prepared)

    cards = added = skipped = failed = 0
    for item in prepared:
        cards += 1
        aliases_to_apply = [
            alias for alias in item["new_aliases"]
            if (item["card_path"], _normalize_alias(alias)) not in blocked
        ]
        if not aliases_to_apply:
            skipped += 1
            continue
        new_text = _insert_aliases(
            item["lines_with_ends"], item["closing"], item["location"],
            aliases_to_apply, item["terminator"],
        )
        if not args.dry_run:
            try:
                _write_card(item["target"], item["bom"], new_text, expected=item["original"])
            except OSError as exc:
                failed += 1
                print(f"ALIAS APPLY FAILED {item['card_path']}: {exc}", file=sys.stderr)
                continue
        print(f"+{len(aliases_to_apply)} {item['card_path']}")
        added += len(aliases_to_apply)

    print(f"ALIAS APPLY cards={cards} added={added} skipped={skipped} collisions={collisions} failed={failed}")
    return 1 if failed else 0


# ------------------------------------------------------------------------- selftest


def _write_fixture(vault, relative, text):
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _selftest():
    import contextlib
    import io
    import tempfile as _tempfile

    checks = []
    try:
        with _tempfile.TemporaryDirectory(prefix="epitype-aliasbatch-") as temp_dir:
            vault = Path(temp_dir).resolve()

            _write_fixture(vault, "no-alias.md", (
                "---\nname: no-alias\ndescription: 2026-09-06 沒有別名的卡\n---\n本文第一段。\n"
            ))
            _write_fixture(vault, "one-alias.md", (
                "---\nname: one-alias\ndescription: 2026-09-06 只有一個別名\n"
                "aliases:\n  - 舊別名\n---\n本文。\n"
            ))
            _write_fixture(vault, "two-alias.md", (
                "---\nname: two-alias\ndescription: 2026-09-06 已有兩個別名\n"
                "aliases:\n  - 別名一\n  - 別名二\n---\n本文。\n"
            ))
            _write_fixture(vault, "grants/grant-card.md", (
                "---\nname: grant-card\ndescription: owner grant auto-captured 2026-09-06: 同意\n"
                "captured_at: 2026-09-06T00:00:00Z\nsession_id: synthetic\n---\nbody\n"
            ))
            decision_a = _write_fixture(vault, "decision-a.md", (
                "---\nname: decision-a\ndescription: 2026-09-06 決策A\ndecision_key: k-collide\n"
                "status: active\ncurrent_decision_at: 2026-09-06\ndecided_by: owner-explicit\n"
                "owner_quote: 「就這樣」\n---\nbody\n"
            ))
            decision_b = _write_fixture(vault, "decision-b.md", (
                "---\nname: decision-b\ndescription: 2026-09-06 決策B\ndecision_key: k-collide-2\n"
                "status: active\ncurrent_decision_at: 2026-09-06\ndecided_by: owner-explicit\n"
                "owner_quote: 「也這樣」\n---\nbody\n"
            ))
            decision_c = _write_fixture(vault, "decision-c.md", (
                "---\nname: decision-c\ndescription: 2026-09-06 決策C\ndecision_key: k-lonely\n"
                "status: active\ncurrent_decision_at: 2026-09-06\ndecided_by: owner-explicit\n"
                "owner_quote: 「獨立」\n---\nbody\n"
            ))
            decision_d = _write_fixture(vault, "decision-d.md", (
                "---\nname: decision-d\ndescription: 2026-09-06 決策D 三個建議一個撞名\n"
                "decision_key: k-partial\nstatus: active\ncurrent_decision_at: 2026-09-06\n"
                "decided_by: owner-explicit\nowner_quote: 「三選二」\n---\nbody\n"
            ))
            decision_e = _write_fixture(vault, "decision-e.md", (
                "---\nname: decision-e\ndescription: 2026-09-06 決策E 已有別名當基準\n"
                "decision_key: k-partial-baseline\nstatus: active\ncurrent_decision_at: 2026-09-06\n"
                "decided_by: owner-explicit\nowner_quote: 「先卡位」\n"
                "aliases:\n  - 既有別名\n---\nbody\n"
            ))
            bom_crlf_text = (
                "﻿---\r\nname: bom-crlf\r\ndescription: 2026-09-06 BOM CRLF 卡\r\n"
                "aliases:\r\n  - 舊\r\n---\r\n本文\r\n"
            )
            bom_crlf = _write_fixture(vault, "bom-crlf.md", bom_crlf_text)
            flow_alias = _write_fixture(vault, "flow-alias.md", (
                "---\nname: flow-alias\ndescription: 2026-09-06 行內別名\n"
                "aliases: [alpha, beta]\n---\nbody\n"
            ))
            dup_test = _write_fixture(vault, "dup-test.md", (
                "---\nname: dup-test\ndescription: 2026-09-06 測試去重\n"
                "aliases:\n  - ABC\n---\nbody\n"
            ))
            long_alias_card = _write_fixture(vault, "long-alias-card.md", (
                "---\nname: long-alias-card\ndescription: 2026-09-06 測試別名過長\n---\nbody\n"
            ))
            unsupported = _write_fixture(vault, "unsupported-scalar.md", (
                "---\nname: unsupported-scalar\ndescription: 2026-09-06 別名是純量非清單\n"
                "aliases: 單一別名文字\n---\nbody\n"
            ))
            unsupported_before = unsupported.read_bytes()

            # 1) export：只列缺別名/少於兩個且非事件卡；跳過事件卡與已滿兩個別名的卡。
            entries = _export_candidates(vault, only_missing=False, limit=None)
            paths = {item["card_path"] for item in entries}
            checks.append((
                "export 只列缺別名/少於兩個且非事件卡",
                "no-alias.md" in paths
                and "one-alias.md" in paths
                and "two-alias.md" not in paths
                and "grants/grant-card.md" not in paths
                and all(len(item["aliases_now"]) < MIN_ALIASES for item in entries),
            ))
            no_alias_entry = next(item for item in entries if item["card_path"] == "no-alias.md")
            checks.append((
                "export 帶 body_head 前 600 字且 suggested 為空陣列",
                no_alias_entry["body_head"].startswith("本文第一段")
                and no_alias_entry["suggested"] == [],
            ))
            only_missing_paths = {
                item["card_path"] for item in _export_candidates(vault, only_missing=True, limit=None)
            }
            checks.append((
                "--only-missing 進一步只留完全沒有別名的卡",
                "no-alias.md" in only_missing_paths and "one-alias.md" not in only_missing_paths,
            ))

            def run_apply(review_entries, dry_run=False):
                out = io.StringIO()
                err = io.StringIO()
                args = argparse.Namespace(
                    vault=str(vault), review=str(review_path), dry_run=dry_run,
                )
                review_path.write_text(json.dumps(review_entries, ensure_ascii=False), encoding="utf-8")
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cmd_apply(args)
                return code, out.getvalue(), err.getvalue()

            review_path = vault / "review.json"

            # 2) 既有 block 別名區塊末尾追加。
            code, out, _err = run_apply([{"card_path": "one-alias.md", "suggested": ["新別名"]}])
            one_alias_text = (vault / "one-alias.md").read_text(encoding="utf-8")
            checks.append((
                "apply 追加到既有 aliases 區塊末尾，不動既有項目",
                code == 0 and "+1 one-alias.md" in out
                and "  - 舊別名\n  - 新別名\n" in one_alias_text,
            ))

            # 3) 沒有 aliases 鍵時新建區塊。
            run_apply([{"card_path": "no-alias.md", "suggested": ["建立別名"]}])
            no_alias_text = (vault / "no-alias.md").read_text(encoding="utf-8")
            checks.append((
                "apply 在沒有 aliases 鍵時，於 frontmatter 結尾前新建區塊",
                "aliases:\n  - 建立別名\n---\n" in no_alias_text,
            ))

            # 4) 去重：大小寫與全形/半形正規化後相同視為重複。
            code, out, _err = run_apply([{"card_path": "dup-test.md", "suggested": ["abc", "ＤＥＦ"]}])
            dup_text = (vault / "dup-test.md").read_text(encoding="utf-8")
            checks.append((
                "重複別名（大小寫/全半形正規化後相同）不加，未重複的照加",
                "+1 dup-test.md" in out
                and "  - ABC\n  - ＤＥＦ\n" in dup_text,
            ))

            # 5) 決策卡碰撞：只擋撞名的那一個別名（這裡兩張各自只建議這一個別名，
            # 所以整張卡沒有其他別名可套），第三張不受影響；collisions 計被擋別名數。
            code, out, _err = run_apply([
                {"card_path": "decision-a.md", "suggested": ["共用別名"]},
                {"card_path": "decision-b.md", "suggested": ["共用別名"]},
                {"card_path": "decision-c.md", "suggested": ["獨立別名"]},
            ])
            decision_a_text = decision_a.read_text(encoding="utf-8")
            decision_b_text = decision_b.read_text(encoding="utf-8")
            decision_c_text = decision_c.read_text(encoding="utf-8")
            checks.append((
                "同一別名撞兩張決策卡時只擋該別名並印 COLLISION，第三張不受影響",
                "COLLISION 共用別名 decision-a.md decision-b.md" in out
                and "共用別名" not in decision_a_text
                and "共用別名" not in decision_b_text
                and "獨立別名" in decision_c_text
                and "cards=3 added=1 skipped=2 collisions=2" in out,
            ))

            # 6) 一張卡三個建議、其中一個撞到既有決策卡的別名：只擋撞名的那一個，
            # 其餘兩個照套（+2），COLLISION 只印一行。
            code, out, _err = run_apply([{
                "card_path": "decision-d.md",
                "suggested": ["既有別名", "新A", "新B"],
            }])
            decision_d_text = decision_d.read_text(encoding="utf-8")
            checks.append((
                "卡內只有撞名的別名被擋，其餘 suggested 照套，COLLISION 只印一行",
                "+2 decision-d.md" in out
                and out.count("COLLISION") == 1
                and "COLLISION 既有別名 decision-d.md decision-e.md" in out
                and "既有別名" not in decision_d_text
                and "新A" in decision_d_text and "新B" in decision_d_text
                and "cards=1 added=2 skipped=0 collisions=1" in out,
            ))

            # 7) CRLF + BOM 卡：套用後其餘位元組不變，只在插入點加東西。
            before_bytes = bom_crlf_text.encode("utf-8")
            run_apply([{"card_path": "bom-crlf.md", "suggested": ["新別名"]}])
            after_bytes = bom_crlf.read_bytes()
            expected_bytes = (
                "﻿---\r\nname: bom-crlf\r\ndescription: 2026-09-06 BOM CRLF 卡\r\n"
                "aliases:\r\n  - 舊\r\n  - 新別名\r\n---\r\n本文\r\n"
            ).encode("utf-8")
            checks.append((
                "CRLF+BOM 卡套用後只多插入的別名行，其餘位元組與原檔逐位元組相同",
                after_bytes != before_bytes and after_bytes == expected_bytes,
            ))

            # 8) 行內 [a, b] 形式的別名可以正確追加。
            run_apply([{"card_path": "flow-alias.md", "suggested": ["gamma"]}])
            flow_text = flow_alias.read_text(encoding="utf-8")
            checks.append((
                "行內 aliases: [a, b] 追加新項目而不重寫既有項目",
                "aliases: [alpha, beta, gamma]" in flow_text,
            ))

            # 9) 別名長度上限：超過 40 字的別名不套用，同卡其他別名照常套用。
            code, out, _err = run_apply([{
                "card_path": "long-alias-card.md",
                "suggested": ["A" * 45, "有效別名"],
            }])
            long_text = long_alias_card.read_text(encoding="utf-8")
            checks.append((
                "超過 40 字的別名被丟棄，同卡其餘有效別名照套",
                "A" * 45 not in long_text and "有效別名" in long_text and "+1 long-alias-card.md" in out,
            ))

            # 10) aliases 欄不是清單/行內清單形狀時，整張卡跳過、位元組不變。
            code, out, err = run_apply([{"card_path": "unsupported-scalar.md", "suggested": ["新增別名"]}])
            checks.append((
                "aliases 是純量等不支援形狀時整張卡跳過，位元組完全不變",
                unsupported.read_bytes() == unsupported_before
                and "SKIP unsupported-aliases-shape" in err
                and "cards=0 added=0 skipped=0 collisions=0" in out,
            ))

            # 11) --dry-run 只印不寫。
            before_dry = (vault / "two-alias.md").read_bytes()
            code, out, _err = run_apply(
                [{"card_path": "two-alias.md", "suggested": ["dry別名"]}], dry_run=True
            )
            checks.append((
                "dry-run 印出結果但不寫檔",
                "+1 two-alias.md" in out and (vault / "two-alias.md").read_bytes() == before_dry,
            ))

            # 12) 套用後新別名能被索引與查詢命中。
            run_apply([{"card_path": "no-alias.md", "suggested": ["唯一索引詞"]}])
            memsearch.build_index(vault)
            result = memsearch.query_index(vault, "唯一索引詞")
            checks.append((
                "套用後 memsearch build_index + query_index 能用新別名查到該卡",
                result.get("count", 0) >= 1
                and any(item["card_path"] == "no-alias.md" for item in result.get("results", [])),
            ))

    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 14
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


# -------------------------------------------------------------------------------- CLI


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    export_parser = subparsers.add_parser("export", help="列出缺別名的卡片為 JSON 工作清單")
    export_parser.add_argument("vault")
    export_parser.add_argument("--out", help="輸出檔路徑（預設 <vault>/.epitype/alias_work_YYYYMMDD.json）")
    export_parser.add_argument("--limit", type=int, default=None)
    export_parser.add_argument("--only-missing", action="store_true", help="只列完全沒有別名的卡")

    apply_parser = subparsers.add_parser("apply", help="把審核過的 suggested 別名套回卡片")
    apply_parser.add_argument("vault")
    apply_parser.add_argument("review", help="export 產生、suggested 已審核填好的 JSON 檔")
    apply_parser.add_argument("--dry-run", action="store_true")

    return parser


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    args = _build_parser().parse_args(arguments)
    if args.selftest:
        return _selftest()
    try:
        if args.command == "export":
            return cmd_export(args)
        if args.command == "apply":
            return cmd_apply(args)
    except (OSError, ValueError) as exc:
        print(f"ALIAS BATCH ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("usage: alias_batch.py {export,apply} ... | --selftest", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
