import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude PreToolUse adapter: the fail-open write-content gate and the action guard.

2026-09-09 (owner, docs/FAILURE_MODES.md §34) removed the scar-card `trigger:`
interception, on the premise that irreversible actions belong to the host's own
native rules (Claude `permissions.deny`, Codex `execpolicy`). 2026-09-16 that
premise was tested and failed: Claude's Bash patterns match positionally with no
AND operator, so four of the nine hazard classes moved across and five could not be
expressed at all. The owner lifted the "cards may not carry an action condition"
half of the ruling that day, and what came back is deliberately the narrow form —
a card names literal fragments and the call is denied when *all* of them appear in
its text. No regex, no shell parsing, no intent. Semantic judgement and genuinely
irreversible actions stay with the host, exactly as §34 left them."""

from collections import namedtuple
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import memspec
from _hook_common import (
    GATE_LOG_MAX_BYTES,
    append_gate_log,
    compile_bounded_regex,
    config_path,
    declared_frontmatter,
    emit,
    expired,
    governance_vault,
    load_config,
    notice_marker_directory,
    read_event,
    resolve_vaults,
    run_synthetic,
    sequence_fields,
    with_session,
    write_config,
)


def _best_effort_audit(callback, *arguments):
    try:
        callback(*arguments)
    except Exception:
        pass


def _deny_value(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _sweep_due(root, now):
    """這一輪該不該掃。掃過就在根目錄留一個時間戳，還沒到間隔就直接跳過。

    2026-09-19 實測：每一次工具呼叫都掃一遍舊標記，光是 stat 就 2,945 次、54 ms——
    而標記只是同一場的去重，掃晚一點沒有任何壞處。"""
    stamp = root / memspec.NOTICE_SWEEP_STAMP
    try:
        if now - stamp.stat().st_mtime < memspec.NOTICE_SWEEP_INTERVAL_SECONDS:
            return False
    except OSError:
        pass
    try:
        root.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        return False
    return True


def _sweep_notice_markers(root, now, keep=None):
    """Markers are a same-session dedupe, not a record: drop the aged-out ones."""
    if not _sweep_due(root, now):
        return
    try:
        for session_directory in root.iterdir():
            if not session_directory.is_dir() or session_directory == keep:
                continue
            empty = True
            for marker in session_directory.iterdir():
                try:
                    if now - marker.stat().st_mtime > memspec.NOTICE_MARKER_TTL_SECONDS:
                        marker.unlink()
                    else:
                        empty = False
                except OSError:
                    empty = False
            if empty:
                session_directory.rmdir()
    except OSError:
        pass


def _notice_marker(session_id, text):
    """True the first time this session sees this notice; False afterwards."""
    # 用到才載入：hashlib 要 6 ms，絕大多數呼叫走不到這裡。
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    directory = notice_marker_directory(session_id)
    root = directory.parent
    try:
        _sweep_notice_markers(root, time.time(), keep=directory)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / digest).open("x", encoding="ascii") as stream:
            stream.write(digest + "\n")
    except FileExistsError:
        return False
    except OSError:
        return True
    return True


def _write_target(tool_input, cwd):
    """Absolute path this call is about to write, or None when it names none."""
    for field in memspec.WRITE_GATE_PATH_FIELDS:
        raw = tool_input.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            path = Path(raw)
            if not path.is_absolute() and isinstance(cwd, str) and cwd.strip():
                path = Path(cwd) / path
            return path.resolve()
        except (OSError, TypeError, ValueError):
            return None
    return None


def _edit_items(tool_name, tool_input):
    """(old, new, replace_all) triples this call would apply, in order."""
    if tool_name.casefold() in memspec.WRITE_GATE_MULTI_EDIT_TOOLS:
        raw_edits = tool_input.get(memspec.WRITE_GATE_EDITS_FIELD)
        edits = raw_edits if isinstance(raw_edits, list) else []
    else:
        edits = [tool_input]
    return [
        (
            edit.get(memspec.WRITE_GATE_OLD_FIELD),
            edit.get(memspec.WRITE_GATE_NEW_FIELD),
            bool(edit.get(memspec.WRITE_GATE_REPLACE_ALL_FIELD)),
        )
        for edit in edits
        if isinstance(edit, dict)
    ]


def _prospective_write(tool_name, tool_input, target):
    """(the new text this call adds, the full text the file would then hold).

    The second element is None whenever the result cannot be known exactly — an
    oversized or unreadable file, an `old_string` that is not in the current text.
    A gate that guessed the post-write text would judge a card nobody wrote; the
    new text alone is still checked against the settled rulings."""
    if tool_name.casefold() in memspec.WRITE_GATE_CONTENT_TOOLS:
        content = tool_input.get(memspec.WRITE_GATE_CONTENT_FIELD)
        if not isinstance(content, str):
            return [], None
        return [content], content

    items = _edit_items(tool_name, tool_input)
    additions = [new for _old, new, _all in items if isinstance(new, str) and new]
    if not items or any(
        not isinstance(old, str) or not isinstance(new, str) for old, new, _all in items
    ):
        return additions, None
    try:
        if target.stat().st_size > memspec.WRITE_GATE_MAX_CONTENT_BYTES:
            return additions, None
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return additions, None
    for old, new, replace_all in items:
        if not old or old not in text:
            return additions, None
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return additions, text


def _forbidden_rule_edit(decision, target, fragment, texts):
    """True when this write is the ruling itself being edited, not a re-statement.

    2026-09-06 incident: editing the very card that defines a `forbidden` pattern
    was blocked by that pattern, and only got through because the same content is
    allowed on its second attempt. Two exemptions: the target IS the card that
    carries this ruling, or the matched fragment sits inside the frontmatter's own
    `forbidden:` block. Identity is the card's path, never a `decision_key` the
    content declares — a file anywhere could claim any key and walk past the rule.
    Changing the rule is always allowed; re-stating it anywhere else is not."""
    if decision.path is not None:
        try:
            if target.resolve() == Path(decision.path).resolve():
                return True
        except OSError:
            pass
    # 測試檔裡的禁語是樣本，不是主張：一條規則的回歸測試必須寫得出那句被禁的話，否則
    # 這道閘擋掉的第一個東西就是「證明它有效」的那份測試。2026-09-19 真的發生兩次。
    try:
        if any(part.casefold() in memspec.WRITE_GATE_FIXTURE_DIRECTORIES
               for part in target.parts):
            return True
    except (AttributeError, OSError):
        pass
    for text_value in texts:
        if not isinstance(text_value, str) or not text_value:
            continue
        front_lines, closing = memspec.split_frontmatter(text_value)
        if front_lines is None or closing is None:
            continue
        block = []
        parent = None
        for raw_line in front_lines:
            indented = raw_line[:1].isspace()
            match = None if indented else memspec.TOP_LEVEL_FIELD.match(raw_line)
            if match is not None:
                key, raw_value = match.groups()
                value = memspec.strip_inline_comment(raw_value).strip()
                parent = key
                if key == memspec.FORBIDDEN_FIELD:
                    block.append(value)
                continue
            if indented and parent == memspec.FORBIDDEN_FIELD:
                block.append(raw_line)
        if fragment and any(fragment in line for line in block):
            return True
    return False


def _forbidden_write(event, config, target, additions, prospective, started_at, notices):
    """(vault, decision key, reason) for the first settled ruling this text violates.

    The decision cards, their `forbidden` patterns, and the pattern validator are
    the Stop gate's own: a ruling the model may not restate at the end of a turn is
    the same ruling it may not write into a file, and two readings of one card would
    drift. An unusable pattern is named to the model, never silently dropped."""
    import stop_gate

    for vault in stop_gate._vaults(config, event):
        if expired(started_at):
            return None
        for decision in stop_gate._decisions(vault, started_at):
            # 管「說出口的話」的裁定不套在寫檔上：白話規則擋的是對 owner 丟機器名稱，
            # 而同一個字寫進程式碼註解或英文提交訊息是正當的。
            if decision.applies_to == memspec.APPLIES_TO_SPEECH:
                continue
            for index, text in enumerate(additions):
                # Defects are collected from the first text only; the same broken
                # pattern repeated once per edit would say nothing new.
                fragment = stop_gate._forbidden_fragment(
                    decision, text, notices if index == 0 else []
                )
                if fragment is None:
                    continue
                if _forbidden_rule_edit(decision, target, fragment, (prospective, text)):
                    continue
                return (
                    vault,
                    decision.key,
                    memspec.WRITE_GATE_FORBIDDEN_REASON.format(
                        decision=stop_gate._named(decision),
                        quote=decision.advice or decision.quote,
                        fragment=fragment[: memspec.WRITE_GATE_FRAGMENT_MAX_CHARS],
                    ),
                )
    return None


def _vault_card_path(target, vaults):
    """(vault, vault-relative posix path) when the target is a card of a registered
    vault, by memsearch's own card filter: a '_'/'.' prefixed part, a non-.md name
    and the memory index are not cards, so writing them carries no card contract."""
    for vault in vaults:
        try:
            relative = target.relative_to(vault)
        except ValueError:
            continue
        parts = relative.parts
        if not parts or any(part.startswith(("_", ".")) for part in parts):
            return None
        if not parts[-1].lower().endswith(".md") or parts[-1] == memspec.MEMORY_INDEX_FILENAME:
            return None
        return vault, relative.as_posix()
    return None


def _card_review(relative, text):
    """(deny reason, advice line) for the card this write would leave on disk.

    card_lint.check_card is the single reading of the type contract — a second
    required-field table here would let one card pass the gate and fail the lint.
    It reads a path, so the prospective text is staged in the temp directory: the
    vault must not hold a card the model has not actually written yet."""
    from epitype import card_lint

    handle, name = tempfile.mkstemp(prefix="epitype-write-", suffix=".md")
    staging = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        card_type, findings = card_lint.check_card(staging, relative)
    finally:
        try:
            staging.unlink()
        except OSError:
            pass

    fails = [reason for level, _rule, reason in findings if level == card_lint.FAIL]
    warns = [reason for level, _rule, reason in findings if level == card_lint.WARN]
    advice = (
        memspec.WRITE_GATE_CARD_ADVICE.format(
            card_type=card_type, path=relative, problems="；".join(warns)
        )
        if warns
        else None
    )
    if not fails:
        return None, advice
    problems = "；".join(fails)
    examples = [
        example
        for field, example in memspec.WRITE_GATE_FIELD_EXAMPLES.items()
        if field in problems
    ]
    reason = memspec.WRITE_GATE_CARD_REASON.format(
        card_type=card_type,
        path=relative,
        problems=problems,
        example="；".join(examples[: memspec.GATE_DEFECT_MAX_LINES])
        or "見 docs/ARCHITECTURE.md §Card types and required fields",
    )
    return reason[: memspec.WRITE_GATE_REASON_MAX_CHARS], advice


def _write_marker(session_id, rule, target, text):
    """Same-session dedupe keyed by (rule, file, content digest): a model that
    cannot satisfy a ruling would otherwise be denied the same write forever."""
    # 用到才載入：hashlib 要 6 ms，絕大多數呼叫走不到這裡。
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return _notice_marker(
        session_id, f"{memspec.WRITE_GATE_LOG_KIND}\0{rule}\0{target}\0{digest}"
    )


def _append_write_block(vault, rule, subject, target, started_at, session_id=None):
    """Audit a blocked write by rule, subject, and filename only — the content is
    exactly the material a ruling is about and does not belong in the ledger."""
    append_gate_log(
        vault,
        with_session(
            {
                "kind": memspec.WRITE_GATE_LOG_KIND,
                "rule": rule,
                "filename": target.name,
                **subject,
            },
            session_id,
        ),
        started_at,
    )


_Guard = namedtuple("_Guard", "card tool substrings advice path requires unless when")


def _read_guard(path):
    """One card's action guard, a defect string, or None when the card declares none.

    Validation is strict in the fail-open direction: a card whose substrings are too
    short, too many, or missing enforces nothing and says so, because dropping the
    bad items instead would leave fewer required fragments and therefore a guard that
    matches *more* than its author wrote."""
    front = declared_frontmatter(
        path, memspec.ACTION_GUARD_TOOL_FIELD, memspec.STOP_GATE_FRONTMATTER_MAX_BYTES
    )
    if front is None:
        return None
    fields, _problem = memspec.frontmatter_text("---\n" + "\n".join(front) + "\n---\n")

    def one_line(value):
        return " ".join(str(value or "").split())

    name = one_line(fields.get(memspec.NAME_FIELD)) or path.stem
    tool = one_line(fields.get(memspec.ACTION_GUARD_TOOL_FIELD))
    if not tool:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name, field=memspec.ACTION_GUARD_TOOL_FIELD, reason="工具名是空的"
        )
    declared = sequence_fields(
        front,
        (
            memspec.ACTION_GUARD_ALL_OF_FIELD,
            memspec.ACTION_GUARD_REQUIRES_FIELD,
            memspec.ACTION_GUARD_UNLESS_FIELD,
            memspec.ACTION_GUARD_WHEN_FIELD,
        ),
    )
    substrings = declared[memspec.ACTION_GUARD_ALL_OF_FIELD]
    requires = declared[memspec.ACTION_GUARD_REQUIRES_FIELD]
    unless_items = declared[memspec.ACTION_GUARD_UNLESS_FIELD]
    when_items = declared[memspec.ACTION_GUARD_WHEN_FIELD]
    problem = None
    field_name = re.compile(memspec.ACTION_GUARD_FIELD_NAME_PATTERN + r"\Z")
    unless = []
    for item in unless_items:
        name_part, _, value_part = str(item).partition("=")
        # 逃生口寫壞就整張卡不生效。反過來（忽略壞掉的那一條）會讓守衛擋得比作者寫的更多，
        # 而擋過頭的那一方沒有人會來報案——被擋的人只會換個寫法繞過去。
        if not field_name.match(name_part.strip()) or not value_part.strip():
            problem = (
                f"{memspec.ACTION_GUARD_UNLESS_FIELD} 的「{item}」不是 欄位=值 的寫法"
            )
            break
        unless.append((name_part.strip(), value_part.strip()))
    if problem:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name, field=memspec.ACTION_GUARD_UNLESS_FIELD, reason=problem
        )
    when = []
    for item in when_items:
        name_part, _, value_part = str(item).partition("=")
        if not field_name.match(name_part.strip()) or not value_part.strip():
            problem = f"{memspec.ACTION_GUARD_WHEN_FIELD} 的「{item}」不是 欄位=值 的寫法"
            break
        values = tuple(
            piece.strip().casefold()
            for piece in value_part.split(memspec.ACTION_GUARD_WHEN_ALTERNATIVE)
            if piece.strip()
        )
        if not values:
            problem = f"{memspec.ACTION_GUARD_WHEN_FIELD} 的「{item}」沒有值"
            break
        when.append((name_part.strip(), values))
    if problem:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name, field=memspec.ACTION_GUARD_WHEN_FIELD, reason=problem
        )
    if len(when) > memspec.ACTION_GUARD_MAX_WHEN:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name,
            field=memspec.ACTION_GUARD_WHEN_FIELD,
            reason=f"條件 {len(when)} 組，超過上限 {memspec.ACTION_GUARD_MAX_WHEN}",
        )
    for item in requires:
        if not field_name.match(str(item).strip()):
            return memspec.ACTION_GUARD_DEFECT.format(
                card=name,
                field=memspec.ACTION_GUARD_REQUIRES_FIELD,
                reason=f"「{item}」不是一個欄位名",
            )
    if len(requires) > memspec.ACTION_GUARD_MAX_REQUIRES:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name,
            field=memspec.ACTION_GUARD_REQUIRES_FIELD,
            reason=f"必填欄位 {len(requires)} 個，超過上限 {memspec.ACTION_GUARD_MAX_REQUIRES}",
        )
    if not substrings and (requires or when):
        # 欄位型的守衛不需要字面片段：它問的是「少了什麼」或「哪兩個欄位配在一起」，
        # 兩者都沒有字面可以比對。
        return {
            "card": name,
            "tool": tool,
            "substrings": [],
            "advice": one_line(
                fields.get(memspec.ACTION_GUARD_ADVICE_FIELD)
                or fields.get(memspec.DESCRIPTION_FIELD)
            ),
            "requires": [str(item).strip() for item in requires],
            "unless": [list(pair) for pair in unless],
            "when": [[pair[0], list(pair[1])] for pair in when],
            # 到期日只存不判（同下）。漏了這一行的話，欄位型守衛的 valid_until 會被
            # 靜靜忽略——卡片寫了期限、閘永遠不會停。
            "expires": one_line(
                fields.get(memspec.VALID_UNTIL_FIELD) or fields.get(memspec.GRANT_EXPIRES_FIELD)
            ),
        }
    if not substrings:
        problem = (
            f"既沒有 {memspec.ACTION_GUARD_ALL_OF_FIELD} 的字面片段，"
            f"也沒有 {memspec.ACTION_GUARD_REQUIRES_FIELD} 的必填欄位"
            f"或 {memspec.ACTION_GUARD_WHEN_FIELD} 的欄位組合"
        )
    elif len(substrings) > memspec.ACTION_GUARD_MAX_SUBSTRINGS:
        problem = f"片段超過 {memspec.ACTION_GUARD_MAX_SUBSTRINGS} 個"
    elif (
        len(substrings) == 1
        and len(substrings[0]) < memspec.ACTION_GUARD_LONE_FRAGMENT_MIN_CHARS
    ):
        problem = (
            f"只有一個片段而且短於 {memspec.ACTION_GUARD_LONE_FRAGMENT_MIN_CHARS} 個字，"
            "會擋掉整類工具；請再加一個片段把條件收窄"
        )
    if problem:
        return memspec.ACTION_GUARD_DEFECT.format(
            card=name, field=memspec.ACTION_GUARD_ALL_OF_FIELD, reason=problem
        )
    advice = one_line(
        fields.get(memspec.ACTION_GUARD_ADVICE_FIELD)
        or fields.get(memspec.DESCRIPTION_FIELD)
    )
    return {
        "card": name,
        "tool": tool,
        "substrings": list(substrings),
        "advice": advice,
        "requires": [str(item).strip() for item in requires],
        "unless": [list(pair) for pair in unless],
        # 欄位組合條件在這一支也要帶上。2026-09-20 Codex 審查抓到：只有「沒有字面片段」
        # 那一支序列化了 when，於是一張同時寫了字面與欄位條件的卡，欄位那半在讀卡時就
        # 消失，結果變成「只要文字命中就擋」——比作者寫的寬。
        "when": [[pair[0], list(pair[1])] for pair in when],
        # 到期日只存不判：卡片不動也會過期，而這裡的結果會進快取。
        "expires": one_line(
            fields.get(memspec.VALID_UNTIL_FIELD) or fields.get(memspec.GRANT_EXPIRES_FIELD)
        ),
    }


def _guard_cache(vault):
    return vault / memspec.FTS_INDEX_DIRECTORY / memspec.ACTION_GUARD_CACHE_FILENAME


def warm_guard_cache(vaults, started_at):
    """Read every card's guard status once, inside the caller's deadline.

    A tool call may only re-read a bounded slice of a vault, so on a cold cache a
    guard sitting past that slice is not enforced yet — the gate under-enforces
    silently, which is the failure this whole mechanism exists to remove. SessionStart
    has budget a tool call does not, so it pays the discovery cost once per session
    and every later call reads a warm cache."""
    for vault in vaults:
        if expired(started_at):
            return
        try:
            _guards(vault, started_at, [], cap=_WARM_CAP)
        except Exception:
            continue


_WARM_CAP = 1 << 30
# 2：守衛的項目多了必填欄位、逃生口與到期日；版本不對就整份重讀。
_GUARD_CACHE_VERSION = 4


def _guards(vault, started_at, defects, cap=None):
    """Every usable guard in one vault, discovered through a manifest cache.

    Same shape as the Stop gate's decision cache and for the same reason: reading
    every card on every tool call is the cost §34 objected to, so discovery is cached
    against (mtime, size, ctime, device, inode) and only changed cards — plus a
    rotating slice of the known non-guards — are re-read, bounded per call."""
    from epitype import cardscan

    vault = Path(vault).resolve()
    try:
        scan = cardscan.scan_vault(Path(vault).resolve())
    except Exception as exc:
        # 這個庫的規則這一次一條都沒生效。安靜回空的話，外面看起來就像「這裡沒有
        # 規則」——而那正是沒有人會去查的那個答案。
        defects.append(memspec.GATE_VAULT_UNREADABLE_NOTICE.format(
            gate="動作閘", vault=Path(vault).name, reason=type(exc).__name__))
        return []
    manifest, paths = {}, {}
    for card_path, path, mtime_ns, size, ctime_ns in scan:
        if expired(started_at):
            return []
        # 走訪已經帶回這三項；再 stat 一次只為了 inode 與裝置編號，而 Windows 的
        # 目錄列表根本不給那兩項（實測回 0），等於每張卡多付一次系統呼叫換兩個零。
        manifest[card_path] = [mtime_ns, size, ctime_ns]
        paths[card_path] = path

    cache_path = _guard_cache(vault)
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        # 版本沒對上就整份重讀。以前這裡不看版本，於是舊格式的項目會缺新欄位、而清單
        # 又只在檔案變動時才重讀——新加的判斷（例如到期）對既有的卡永遠不會生效。
        if isinstance(loaded, dict) and loaded.get("version") != _GUARD_CACHE_VERSION:
            loaded = None
        old_manifest = loaded.get("manifest") if isinstance(loaded, dict) else None
        cached = loaded.get("guards") if isinstance(loaded, dict) else None
        cursor = loaded.get("cursor") if isinstance(loaded, dict) else ""
        if not isinstance(old_manifest, dict) or not isinstance(cached, dict):
            old_manifest, cached, cursor = {}, {}, ""
        if not isinstance(cursor, str):
            cursor = ""
    except (OSError, ValueError):
        old_manifest, cached, cursor = {}, {}, ""

    old_cursor = cursor
    known = {key: value for key, value in cached.items() if key in paths}
    verified = {key: old_manifest.get(key) for key in known}
    changed = [key for key in paths if key not in cached or old_manifest.get(key) != manifest[key]]
    negatives = sorted(key for key in paths if key in cached and key not in changed and not known.get(key))
    rotated = [key for key in negatives if key > cursor] + [key for key in negatives if key <= cursor]
    budget = memspec.ACTION_GUARD_MAX_CARDS_PER_VAULT if cap is None else cap
    for card_path in (changed + rotated)[:budget]:
        if expired(started_at):
            break
        try:
            known[card_path] = _read_guard(paths[card_path])
        except Exception:
            known[card_path] = None
        verified[card_path] = manifest[card_path]
        if card_path in negatives:
            cursor = card_path

    if verified != old_manifest or known != cached or cursor != old_cursor:
        staging = cache_path.with_name(f".{cache_path.name}.tmp-{os.getpid()}")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            staging.write_text(
                json.dumps(
                    {"version": _GUARD_CACHE_VERSION, "manifest": verified,
                     "guards": known, "cursor": cursor},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(staging, cache_path)
        except OSError:
            pass
        finally:
            # 寫失敗留下的暫存檔沒人會回頭清，而檔名帶 pid，每次失敗都是新的一個。
            try:
                staging.unlink()
            except OSError:
                pass

    found = []
    for card_path in sorted(known):
        entry = known[card_path]
        if isinstance(entry, str):
            defects.append(entry)
        elif isinstance(entry, dict) and (
            entry.get("substrings") or entry.get("requires") or entry.get("when")
        ):
            if memspec.card_expired(entry.get("expires")):
                # 過期的守衛不再攔人；時限型的規則要自己停下來，不能靠人記得去拔。
                continue
            found.append(
                _Guard(
                    entry.get("card", card_path),
                    entry.get("tool", ""),
                    tuple(item for item in (entry.get("substrings") or ()) if isinstance(item, str)),
                    entry.get("advice", ""),
                    vault / card_path,
                    tuple(item for item in (entry.get("requires") or ()) if isinstance(item, str)),
                    tuple(
                        (pair[0], pair[1])
                        for pair in (entry.get("unless") or ())
                        if isinstance(pair, (list, tuple)) and len(pair) == 2
                    ),
                    tuple(
                        (pair[0], tuple(str(value).casefold() for value in pair[1]))
                        for pair in (entry.get("when") or ())
                        if isinstance(pair, (list, tuple)) and len(pair) == 2
                        and isinstance(pair[1], (list, tuple)) and pair[1]
                    ),
                )
            )
    return found


def _action_text(tool_input):
    """Every acting string the call carries, joined — the haystack a guard reads.

    Nothing is parsed: a guard asks whether its fragments all appear in what this
    call actually does, which is the one question a string check can answer honestly
    about a shell command. The only fields left out are the ones that cannot act —
    `description` is prose written for the owner's permission card, and its words
    were enough to fake a hit (see ACTION_GUARD_IGNORED_FIELDS)."""
    if not isinstance(tool_input, dict):
        return ""
    parts = [
        value
        for key, value in tool_input.items()
        if isinstance(value, str) and key not in memspec.ACTION_GUARD_IGNORED_FIELDS
    ]
    return "\n".join(parts)[: memspec.ACTION_GUARD_HAYSTACK_MAX_CHARS]


def _guard_review(event, tool_name, tool_input, config, started_at, defects):
    """(deny value, ()) when a scar card's fragments all appear in this call.

    Owner 2026-09-16 lifted §34's "cards may not carry an action condition". What is
    restored is only the literal form: every fragment must be present, as plain text.
    Semantic judgement and genuinely irreversible actions remain the host's native
    rules, exactly as §34 left them."""
    # 用到才載入：hashlib 要 6 ms，絕大多數呼叫走不到這裡。
    import hashlib

    haystack = _action_text(tool_input)
    # 必填欄位型的守衛問的是「少了什麼」，所以呼叫沒有任何可比對的字串時它仍然要判——
    # 只有連 tool_input 都不是個欄位集合時，這道閘才真的沒有東西可問。
    if not haystack and not isinstance(tool_input, dict):
        return None
    # 刻意不看 `stop_hook_active`。那是 Stop 閘為了避免自己重入才讀的旗標，動作守衛
    # 沒有重入問題（它不寫東西，拒絕也不會再觸發自己）。照著讀的話，Stop 閘擋下之後
    # 的那一段續跑，工具守衛整個是關的——而那正是我被逼著換做法、最可能亂動手的時刻。
    folded_tool = tool_name.casefold()
    for vault in resolve_vaults(config, event):
        if expired(started_at):
            return None
        # 自動降級已經拿掉（判準分不出「規則太寬」與「我一直犯這條」，2026-09-17
        # 實跑：一條 6 次命中、6 次全擋下、0 漏擋的規則被關了 14 天）。那個函式現在
        # 永遠回空集合，而為了拿一個空集合，每一次工具呼叫都要多載 8.3 ms 的模組。
        demoted = frozenset()
        for guard in _guards(vault, started_at, defects):
            if not memspec.action_guard_tool_matches(guard.tool, folded_tool) or guard.card in demoted:
                continue
            if guard.substrings and not all(
                fragment in haystack for fragment in guard.substrings
            ):
                continue
            fields = tool_input if isinstance(tool_input, dict) else {}
            if any(
                str(fields.get(name, "")).strip() == value for name, value in guard.unless
            ):
                continue
            # 欄位組合型的條件：全部成立才算命中。一組不成立就整張卡跳過，不是「部分命中」。
            if guard.when and not all(
                str(fields.get(name, "") or "").strip().casefold() in values
                for name, values in guard.when
            ):
                continue
            missing = [
                name for name in guard.requires if not str(fields.get(name, "") or "").strip()
            ]
            if guard.requires and not missing:
                continue
            if missing:
                reason = memspec.ACTION_GUARD_REQUIRES_REASON.format(
                    card=guard.card,
                    tool=tool_name,
                    fields="、".join(f"「{name}」" for name in missing),
                    advice=guard.advice,
                )
            elif not guard.substrings and guard.when:
                reason = memspec.ACTION_GUARD_WHEN_REASON.format(
                    card=guard.card,
                    tool=tool_name,
                    pairs="＋".join(
                        f"{name}={str(fields.get(name, '') or '').strip()}"
                        for name, _values in guard.when
                    ),
                    advice=guard.advice,
                )
            else:
                fragments = "、".join(
                    f"「{fragment[: memspec.ACTION_GUARD_FRAGMENT_MAX_CHARS]}」"
                    for fragment in guard.substrings
                )
                reason = memspec.ACTION_GUARD_REASON.format(
                    card=guard.card, tool=tool_name, fragments=fragments, advice=guard.advice
                )
            _best_effort_audit(
                append_gate_log,
                vault,
                with_session(
                    {
                        "kind": memspec.ACTION_GUARD_LOG_KIND,
                        "rule": memspec.ACTION_GUARD_RULE,
                        "card": guard.card,
                        "tool": tool_name,
                        # 夜間重放靠它分辨「這張卡放行了」與「同一次呼叫被別張卡擋下」。
                        "digest": hashlib.sha256(
                            haystack.encode("utf-8", errors="replace")
                        ).hexdigest()[:16],
                    },
                    event.get("session_id"),
                ),
                started_at,
            )
            return _deny_value(reason[: memspec.ACTION_GUARD_REASON_MAX_CHARS])
    return None


def _write_review(event, tool_name, tool_input, config, started_at):
    """(deny value, advice lines) for a call about to write file content.

    Rule A: new content that re-states what the owner already ruled out is blocked
    against the Stop gate's decision cards. Rule B: a card written into a registered
    vault must satisfy card_lint's contract for its own type — FAIL blocks, WARN only
    advises. Anything else proceeds untouched: a non-file tool, a re-entrant hook run,
    a path outside every vault, content past the size cap, or a post-write text that
    cannot be known exactly. Bash redirections never reach this gate at all
    (docs/FAILURE_MODES.md §11)."""
    if tool_name.casefold() not in memspec.WRITE_GATE_TOOL_NAMES:
        return None, []
    if not isinstance(tool_input, dict) or event.get("stop_hook_active"):
        return None, []
    target = _write_target(tool_input, event.get("cwd"))
    if target is None or expired(started_at):
        return None, []
    additions, prospective = _prospective_write(tool_name, tool_input, target)

    def oversized(text):
        return len(text.encode("utf-8", errors="replace")) > memspec.WRITE_GATE_MAX_CONTENT_BYTES

    if any(oversized(text) for text in additions):
        return None, []
    if prospective is not None and oversized(prospective):
        prospective = None

    notices = []
    session_id = event.get("session_id")
    found = _forbidden_write(
        event, config, target, additions, prospective, started_at, notices
    )
    if found is not None:
        vault, decision_key, reason = found
        if not _write_marker(
            session_id, memspec.WRITE_GATE_FORBIDDEN_RULE, target, "\0".join(additions)
        ):
            return None, notices
        _best_effort_audit(
            _append_write_block,
            vault,
            memspec.WRITE_GATE_FORBIDDEN_RULE,
            {"decision": decision_key},
            target,
            started_at,
            session_id,
        )
        return _deny_value(reason[: memspec.WRITE_GATE_REASON_MAX_CHARS]), []

    if prospective is None or expired(started_at):
        return None, notices
    card = _vault_card_path(target, resolve_vaults(config, event))
    if card is None:
        return None, notices
    vault, relative = card
    reason, advice = _card_review(relative, prospective)
    if reason is None:
        if advice:
            notices.append(advice)
        return None, notices
    if not _write_marker(session_id, memspec.WRITE_GATE_CARD_RULE, target, prospective):
        return None, notices
    _best_effort_audit(
        _append_write_block,
        vault,
        memspec.WRITE_GATE_CARD_RULE,
        {"card_path": relative},
        target,
        started_at,
        session_id,
    )
    return _deny_value(reason), []


def _allow_context(event, notices=()):
    """Context for a call the gate lets through: the write gate's own advice and
    the rulings whose `forbidden` pattern could not be compiled, each named once
    per session — a ruling that silently stopped being enforced is the failure the
    gate exists to prevent."""
    lines = []
    session_id = event.get("session_id")
    for notice in list(dict.fromkeys(notices))[: memspec.GATE_DEFECT_MAX_LINES]:
        if _notice_marker(session_id, notice):
            lines.append(notice)
    if not lines:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(lines)}}


def _read_state_path(vault, session_id):
    session = "".join(char for char in str(session_id or "") if char.isalnum() or char in "-_")
    if not session:
        return None
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY / "reads" / (session + ".json")


def _waste_review(event, tool_name, tool_input, config, started_at):
    """(擋下的值, 提醒行)——同一場重複讀同一份內容，或整檔拉一個大檔。

    這道問的不是「這次呼叫做了什麼壞事」，而是「這次呼叫有沒有必要」。省 token 是
    owner 2026-09-18 最在意的一條，而它只在動手那一刻看得出來：事後檢討只能數浪費，
    擋不住浪費。"""
    # 用到才載入：hashlib 要 6 ms，絕大多數呼叫走不到這裡。
    import hashlib

    if tool_name.casefold() not in memspec.READ_WASTE_TOOLS:
        return None, None
    if not isinstance(tool_input, dict):
        return None, None
    raw = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    try:
        target = Path(raw)
        info = target.stat()
    except OSError:
        return None, None

    # 有界讀取不只有 offset／limit 一種寫法：PDF 給的是頁碼範圍，筆記本給的是格子。
    # 2026-09-19 這道閘上線半小時就誤擋了一次帶頁碼範圍的 PDF 讀取——擋的理由是
    # 「整檔拉進來」，而那次呼叫本來就只要六頁。
    bounds = [tool_input.get(field) for field in memspec.READ_WASTE_BOUND_FIELDS]
    bounded = any(str(value or "").strip() for value in bounds)
    if info.st_size >= memspec.READ_WASTE_BIG_FILE_BYTES and not bounded:
        return _deny_value(memspec.READ_WASTE_BIG_FILE_REASON.format(
            path=raw, size=info.st_size)), None

    # 「同一段」的身分要含全部的範圍欄位，不能只有 offset／limit。2026-09-20 Codex
    # 審查抓到：同一份 PDF 連續讀 1-3、4-6、7-9 頁，第三次會被擋，理由還錯稱這一段
    # 已經讀過——那三次讀的根本是不同頁。
    digest = hashlib.sha256(
        "|".join(str(part) for part in (
            [os.path.normcase(os.path.abspath(raw)), info.st_mtime_ns, info.st_size] + bounds
        )).encode("utf-8")
    ).hexdigest()[:16]

    vault = governance_vault(config, for_write=True)
    state_path = _read_state_path(vault, event.get("session_id"))
    if state_path is None:
        return None, None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    count = int(state.get(digest, 0)) + 1
    state[digest] = count
    if len(state) > memspec.READ_WASTE_STATE_MAX_ENTRIES:
        state = dict(list(state.items())[-memspec.READ_WASTE_STATE_MAX_ENTRIES:])
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        staging = state_path.with_name("." + state_path.name + ".tmp-%d" % os.getpid())
        staging.write_text(json.dumps(state), encoding="utf-8")
        os.replace(staging, state_path)
    except OSError:
        pass

    if count > 1 + memspec.READ_WASTE_FREE_REPEATS:
        return _deny_value(memspec.READ_WASTE_REPEAT_REASON.format(count=count, path=raw)), None
    if count > 1:
        return None, memspec.READ_WASTE_REPEAT_NOTICE.format(count=count, path=raw)
    return None, None


def _handle(event, started_at, defects=None):
    """The gate's whole decision, in two parts.

    A call about to write file content is judged against the owner's settled rulings
    and the card contract. Any call at all is judged against the scar cards' literal
    action guards (owner 2026-09-16, docs/FAILURE_MODES.md §34). Nothing here reads
    intent: what is not a content rule and not a declared literal guard is the host's
    native rules to judge, never this hook's."""
    defects = [] if defects is None else defects
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    config = load_config(started_at)
    if config is None:
        return None
    try:
        guard_value = _guard_review(
            event, tool_name, event.get("tool_input"), config, started_at, defects
        )
    except Exception:
        guard_value = None
    if guard_value is not None:
        return guard_value
    try:
        write_value, notices = _write_review(
            event, tool_name, event.get("tool_input"), config, started_at
        )
    except Exception:
        write_value, notices = None, ()
    if write_value is not None:
        return write_value
    try:
        waste_value, waste_notice = _waste_review(
            event, tool_name, event.get("tool_input"), config, started_at
        )
    except Exception:
        waste_value, waste_notice = None, None
    if waste_value is not None:
        return waste_value
    if waste_notice:
        notices = tuple(notices) + (waste_notice,)
    return _allow_context(event, notices)


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-gate-") as temp_dir:
            root = Path(temp_dir).resolve()
            vault = root / "vault"
            vault.mkdir()
            # 2026-09-09 U-J：卡片就算還寫著 trigger，這道閘也不再讀它、不再攔任何動作。
            (vault / "retired-trigger.md").write_text(
                "---\n"
                "name: synthetic-safety\n"
                "trigger:\n"
                "  tool: ^Bash$\n"
                "  input: remove target\n"
                "advice: Use the read-only alternative.\n"
                "---\n"
                "Synthetic card body.\n",
                encoding="utf-8",
            )
            config = root / "config.json"
            write_config(config, [vault])
            log_path = vault / memspec.GATE_LOG_FILENAME

            retired = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}},
                config,
            )
            checks.append((
                "a card that still declares trigger denies nothing and writes no audit row",
                retired.returncode == 0
                and not retired.stdout.strip()
                and not retired.stderr.strip()
                and not log_path.exists(),
            ))

            # 這四條原本各由一張 trigger 卡攔下；現在一律由宿主原生規則負責
            # （Claude permissions.deny／Codex execpolicy），本 hook 不表意見。
            host_rule_cases = (
                ("heredoc backslash", "python - <<'PY'\np = 'C:\\Users\\x'\nPY\n"),
                ("host process", "taskkill /IM claude.exe /F"),
                ("destructive git", "git reset --hard"),
                ("credential read", "Get-Content .env"),
            )
            host_rule_silent = True
            for _name, command in host_rule_cases:
                result = run_synthetic(
                    Path(__file__),
                    {"tool_name": "Bash", "tool_input": {"command": command}},
                    config,
                )
                host_rule_silent = host_rule_silent and result.returncode == 0 and not result.stdout.strip()
            checks.append((
                "shell command classification is gone: the four retired scars produce no decision",
                host_rule_silent and not log_path.exists(),
            ))

            broken_card = vault / "broken-trigger.md"
            broken_card.write_text(
                "---\nname: broken-trigger\ntrigger:\n  tool: [\n  input: remove\n"
                "advice: never used.\n---\n",
                encoding="utf-8",
            )
            broken = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}, "session_id": uuid.uuid4().hex},
                config,
            )
            checks.append((
                "an unparsable card is no longer a gate defect: nothing is denied, named, or audited",
                broken.returncode == 0
                and not broken.stdout.strip()
                and not log_path.exists(),
            ))

            rotate_vault = root / "rotate-vault"
            rotate_vault.mkdir()
            rotate_log = rotate_vault / memspec.GATE_LOG_FILENAME
            rotate_log.write_text("x" * (GATE_LOG_MAX_BYTES + 1024) + "\n", encoding="utf-8")
            append_gate_log(rotate_vault, {"card": "rotate-check"}, time.monotonic())
            rotated_path = rotate_vault / (memspec.GATE_LOG_FILENAME + ".1")
            rotated_rows = [
                json.loads(line)
                for line in rotate_log.read_text(encoding="utf-8").splitlines()
                if line
            ]
            checks.append(
                (
                    "a gate log past GATE_LOG_MAX_BYTES rotates to .1 before the next row lands",
                    rotated_path.is_file()
                    and rotated_path.stat().st_size > GATE_LOG_MAX_BYTES
                    and len(rotated_rows) == 1
                    and rotated_rows[0].get("card") == "rotate-check",
                )
            )

            # The shared `forbidden` pattern validator (Stop gate and rule A both
            # compile through it): a pattern that can backtrack exponentially would
            # hang the hook, and a hung hook is a bypass.
            def regex_accepted(pattern):
                try:
                    compile_bounded_regex(pattern)
                except (ValueError, re.error):
                    return False
                return True

            checks.append((
                "adjacent repetitions, bounded groups, and long patterns are accepted",
                regex_accepted(r"git\s+add\s+(?:-A|--all|\.)\s*$")
                and regex_accepted(r"rm(?:\s+-\w+)?\s+-rf\b")
                and regex_accepted(r"(?:\s+-\w+(?:\s+\S+)?)?\s*>")
                and regex_accepted("|".join(f"(?:token{index}\\s*)" for index in range(60))),
            ))
            checks.append((
                "repeated alternations, nested repetitions, and backreferences are rejected",
                not regex_accepted(r"(?:ab|cd)+$")
                and not regex_accepted(r"(?:\s+\S+)*x")
                and not regex_accepted(r"(a)\1")
                and not regex_accepted("a" * (memspec.FORBIDDEN_REGEX_MAX_CHARS + 1)),
            ))

            # Owner 2026-09-09 (§30): the narration meter is gone. A transcript
            # whose turn is full of mid-run prose must produce no context at all.
            def transcript_rows(*rows):
                path = root / f"transcript-{uuid.uuid4().hex}.jsonl"
                path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
                return os.fspath(path)

            def assistant(kind, value):
                block = {"type": "text", "text": value} if kind == "text" else {"type": "tool_use", "id": value, "name": "Bash", "input": {}}
                return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}

            prompt_row = {"type": "user", "message": {"role": "user", "content": "修一下"}}
            result_row = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]}}
            narrated = transcript_rows(
                prompt_row,
                assistant("text", "先看檔案。"),
                assistant("tool_use", "t1"),
                result_row,
                assistant("text", "那次失敗是我的路徑錯，改成 C:/… 重跑一次。"),
            )
            quiet = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "synthetic.txt"}, "transcript_path": narrated, "session_id": uuid.uuid4().hex},
                config,
            )
            checks.append((
                "prose between tool calls adds no context: the narration meter is gone",
                quiet.returncode == 0 and not quiet.stdout.strip() and not quiet.stderr.strip(),
            ))

            sweep_root = Path(tempfile.gettempdir()) / memspec.NOTICE_MARKER_DIRECTORY
            aged_session = sweep_root / ("aged-" + uuid.uuid4().hex)
            aged_session.mkdir(parents=True, exist_ok=True)
            aged_marker = aged_session / "0123456789abcdef"
            aged_marker.write_text("aged\n", encoding="ascii")
            aged_time = time.time() - memspec.NOTICE_MARKER_TTL_SECONDS - 60
            os.utime(aged_marker, (aged_time, aged_time))
            # 清掃改成有間隔的（每次呼叫都掃一遍舊標記，實測 2,945 次 stat、54 ms）。
            # 把時間戳拿掉就代表「這一輪該掃了」，掃的行為本身照舊要驗。
            try:
                (sweep_root / memspec.NOTICE_SWEEP_STAMP).unlink()
            except OSError:
                pass
            _notice_marker("sweep-" + uuid.uuid4().hex, "synthetic notice")
            checks.append(
                (
                    "aged notice markers are swept instead of accumulating",
                    not aged_marker.exists() and not aged_session.exists(),
                )
            )

            second_session = sweep_root / ("aged2-" + uuid.uuid4().hex)
            second_session.mkdir(parents=True, exist_ok=True)
            second_marker = second_session / "0123456789abcdef"
            second_marker.write_text("aged\n", encoding="ascii")
            os.utime(second_marker, (aged_time, aged_time))
            _notice_marker("sweep-" + uuid.uuid4().hex, "another notice")
            checks.append(
                (
                    "剛掃過就不再掃：標記是同一場的去重，不是每次呼叫都要付的代價",
                    second_marker.exists(),
                )
            )
            for leftover in (second_marker, second_session):
                try:
                    leftover.unlink() if leftover.is_file() else leftover.rmdir()
                except OSError:
                    pass

            miss = run_synthetic(
                Path(__file__),
                {"tool_name": "Read", "tool_input": {"path": "synthetic.txt"}},
                config,
            )
            checks.append(
                (
                    "a call that writes no file content allows silently",
                    miss.returncode == 0 and not miss.stdout and not miss.stderr,
                )
            )

            write_root = root / "write-gate"
            write_vault = write_root / "vault"
            write_vault.mkdir(parents=True)
            (write_vault / "mirror.md").write_text(
                "---\nname: 虛擬盤鏡像裁定\ndescription: 2026-08-13 虛擬盤與實盤參數一致\n"
                f"{memspec.DECISION_KEY_FIELD}: virtual-mirrors-live\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-08-13\n"
                f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 虛擬必須鏡像實盤\n"
                f"{memspec.ALIASES_FIELD}: [虛擬盤, 鏡像實盤]\n"
                f"{memspec.FORBIDDEN_FIELD}: [兩套參數]\n---\nbody\n",
                encoding="utf-8",
            )
            (write_vault / "retired.md").write_text(
                "---\nname: 舊制\ndescription: 2026-01-01 已作廢\n"
                f"{memspec.DECISION_KEY_FIELD}: retired-write-rule\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.SUPERSEDED_DECISION_STATUS}\n"
                f"{memspec.SUPERSEDED_BY_FIELD}: mirror.md\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-01-01\n"
                f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 舊制原話\n"
                f"{memspec.ALIASES_FIELD}: [退休甲, 退休乙]\n"
                f"{memspec.FORBIDDEN_FIELD}: [退休禁詞]\n---\nbody\n",
                encoding="utf-8",
            )
            write_config_path = root / "write-config.json"
            write_config(write_config_path, [write_vault])

            def write_call(tool_name, tool_input, session=None, extra=None):
                event = {
                    "session_id": session or f"write-{uuid.uuid4().hex}",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "cwd": os.fspath(write_root),
                }
                event.update(extra or {})
                # The event carries cwd, and every ancestor of a cwd is looked up
                # as a slug under home: without this the case would read the test
                # machine's own native vaults.
                result = run_synthetic(
                    Path(__file__),
                    event,
                    write_config_path,
                    environment={
                        "HOME": os.fspath(write_root),
                        "USERPROFILE": os.fspath(write_root),
                    },
                )
                value = json.loads(result.stdout) if result.stdout.strip() else {}
                return result, value.get("hookSpecificOutput", {})

            forbidden_write, forbidden_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "plan.txt"),
                    "content": "我打算讓虛擬盤用兩套參數各自最佳化。",
                },
            )
            forbidden_reason = forbidden_out.get("permissionDecisionReason", "")
            checks.append((
                "Write 的內容命中現行裁定的 forbidden 就擋，理由帶裁定鍵、日期、裁定說明與命中片段",
                forbidden_write.returncode == 0
                and forbidden_out.get("permissionDecision") == "deny"
                and "virtual-mirrors-live，2026-08-13" in forbidden_reason
                and "虛擬盤與實盤參數一致" in forbidden_reason
                and "兩套參數" in forbidden_reason,
            ))
            write_log = write_vault / memspec.GATE_LOG_FILENAME
            write_rows = [
                json.loads(line)
                for line in write_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ] if write_log.is_file() else []
            checks.append((
                "擋下的寫入以 write_block 入帳，只記規則、裁定鍵與檔名，不記內容",
                any(
                    row.get("kind") == memspec.WRITE_GATE_LOG_KIND
                    and row.get("rule") == memspec.WRITE_GATE_FORBIDDEN_RULE
                    and row.get("decision") == "virtual-mirrors-live"
                    and row.get("filename") == "plan.txt"
                    and "兩套參數" not in json.dumps(row, ensure_ascii=False)
                    for row in write_rows
                ),
            ))

            edit_target = write_root / "notes.txt"
            edit_target.write_text("原本這裡寫著舊做法。\n", encoding="utf-8")
            _edit_result, edit_out = write_call(
                "Edit",
                {
                    "file_path": os.fspath(edit_target),
                    "old_string": "舊做法",
                    "new_string": "兩套參數",
                },
            )
            _multi_result, multi_out = write_call(
                "MultiEdit",
                {
                    "file_path": os.fspath(edit_target),
                    "edits": [
                        {"old_string": "原本", "new_string": "現在"},
                        {"old_string": "舊做法", "new_string": "兩套參數"},
                    ],
                },
            )
            checks.append((
                "Edit 的 new_string 與 MultiEdit 其中一項命中 forbidden 都擋",
                edit_out.get("permissionDecision") == "deny"
                and multi_out.get("permissionDecision") == "deny",
            ))
            _stale_result, stale_out = write_call(
                "Edit",
                {
                    "file_path": os.fspath(edit_target),
                    "old_string": "這個字串不在檔案裡",
                    "new_string": "無害替代文字",
                },
            )
            checks.append((
                "Edit 找不到 old_string 時不判寫入後內容，放行",
                _stale_result.returncode == 0 and not _stale_result.stdout.strip(),
            ))

            _superseded_result, superseded_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "old.txt"),
                    "content": "退休禁詞照舊寫進來。",
                },
            )
            checks.append((
                "superseded 的決策卡不再擋寫入",
                _superseded_result.returncode == 0 and not superseded_out,
            ))

            # 2026-09-06 事故：改「定義 forbidden 的那張卡」時被自己的 forbidden 擋住。
            _own_result, own_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "mirror.md"),
                    "content": "---\nname: 虛擬盤鏡像裁定\ndescription: 2026-08-13 虛擬盤與實盤參數一致\n"
                    f"{memspec.DECISION_KEY_FIELD}: virtual-mirrors-live\n"
                    f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                    f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-08-13\n"
                    f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                    f"{memspec.OWNER_QUOTE_FIELD}: 虛擬必須鏡像實盤\n"
                    f"{memspec.ALIASES_FIELD}: [虛擬盤, 鏡像實盤]\n"
                    f"{memspec.FORBIDDEN_FIELD}: [兩套參數]\n---\n這條裁定禁的就是兩套參數。\n",
                },
            )
            checks.append((
                "改的就是定義那條 forbidden 的決策卡：命中自己的禁詞不擋",
                _own_result.returncode == 0 and own_out.get("permissionDecision") != "deny",
            ))

            _other_result, other_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "other-rule.md"),
                    "content": "---\nname: 別的裁定\ndescription: 2026-09-06 另一條裁定\n"
                    f"{memspec.DECISION_KEY_FIELD}: k-other\n"
                    f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                    f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-09-06\n"
                    f"{memspec.DECIDED_BY_FIELD}: three-way\n"
                    f"{memspec.ALIASES_FIELD}: [別甲, 別乙]\n---\n就讓虛擬盤用兩套參數各自最佳化。\n",
                },
            )
            _plain_result, plain_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "plan2.txt"),
                    "content": "之後一律改用兩套參數。",
                },
            )
            checks.append((
                "豁免只認那張卡：別的決策卡與一般檔案寫同一句仍然擋",
                other_out.get("permissionDecision") == "deny"
                and plain_out.get("permissionDecision") == "deny",
            ))

            # 2026-09-06 對抗審：宣告同一個 decision_key 就能讓任何檔案繞過規則 A。
            _forged_result, forged_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "plan3.txt"),
                    "content": "---\nname: 假冒\ndescription: 不是那張卡\n"
                    f"{memspec.DECISION_KEY_FIELD}: virtual-mirrors-live\n"
                    "---\n就讓虛擬盤用兩套參數各自最佳化。\n",
                },
            )
            checks.append((
                "vault 外的檔案自稱同一個 decision_key 不算在改那張卡，照擋",
                forged_out.get("permissionDecision") == "deny",
            ))

            _block_result, block_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "new-rule.md"),
                    "content": "---\nname: 新規則\ndescription: 2026-09-06 把裸名詞改寫成句形\n"
                    f"{memspec.DECISION_KEY_FIELD}: k-newrule\n"
                    f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                    f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-09-06\n"
                    f"{memspec.DECIDED_BY_FIELD}: three-way\n"
                    f"{memspec.ALIASES_FIELD}: [新甲, 新乙]\n"
                    f"{memspec.FORBIDDEN_FIELD}:\n"
                    "  - (建議|要不要|是否|應該).{0,12}(納入|採用|改成)兩套參數\n---\nbody\n",
                },
            )
            checks.append((
                "禁詞落在寫入內容自己的 forbidden 區塊裡＝正在改規則，放行",
                _block_result.returncode == 0 and block_out.get("permissionDecision") != "deny",
            ))

            _outside_result, outside_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(root / "outside-any-vault.md"),
                    "content": "---\nname: 沒有必填欄位的卡\n---\nbody\n",
                },
            )
            checks.append((
                "落在所有已登記 vault 之外的 .md 不做規則 B",
                _outside_result.returncode == 0 and not _outside_result.stdout.strip(),
            ))

            _card_result, card_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "new-decision.md"),
                    "content": "---\nname: 新裁定\ndescription: 2026-09-06 只寫了一半\n"
                    f"{memspec.DECISION_KEY_FIELD}: k-new\n---\nbody\n",
                },
            )
            card_reason = card_out.get("permissionDecisionReason", "")
            checks.append((
                "寫進 vault 的決策卡缺必填欄位就擋，理由列出缺哪些欄位並附可照抄的一行範例",
                card_out.get("permissionDecision") == "deny"
                and all(
                    field in card_reason
                    for field in (
                        memspec.DECISION_STATUS_FIELD,
                        memspec.CURRENT_DECISION_AT_FIELD,
                        memspec.DECIDED_BY_FIELD,
                        memspec.ALIASES_FIELD,
                    )
                )
                and memspec.WRITE_GATE_FIELD_EXAMPLES[memspec.DECISION_STATUS_FIELD] in card_reason,
            ))
            card_rows = [
                json.loads(line)
                for line in write_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            checks.append((
                "規則 B 的攔阻入帳記 card_path，不記卡片內容",
                any(
                    row.get("rule") == memspec.WRITE_GATE_CARD_RULE
                    and row.get("card_path") == "new-decision.md"
                    for row in card_rows
                ),
            ))

            _warn_result, warn_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "reference-dated.md"),
                    "content": "---\nname: reference-dated\ndescription: english only reference card\n"
                    f"{memspec.LAST_VERIFIED_AT_FIELD}: 2026-09-01\n"
                    "metadata:\n  type: reference\n---\nbody\n",
                },
            )
            checks.append((
                "WARN 級只在 additionalContext 提示，不擋寫入",
                _warn_result.returncode == 0
                and "permissionDecision" not in warn_out
                and memspec.WRITE_GATE_CARD_ADVICE[:6] in warn_out.get("additionalContext", "")
                and "reference-dated.md" in warn_out.get("additionalContext", "")
                and memspec.ALIASES_FIELD in warn_out.get("additionalContext", "")
                and "沒有中文字" not in warn_out.get("additionalContext", ""),
            ))

            _derived_result, derived_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_vault / "body-dated.md"),
                    "content": "---\nname: body-dated\ndescription: 欄位無日期、正文有\n"
                    f"{memspec.ALIASES_FIELD}: [正文日期]\n"
                    "metadata:\n  type: feedback\n---\n2026-08-15 那天的紀錄\n",
                },
            )
            checks.append((
                "正文有日期、frontmatter 沒有的卡不擋：規則 B 用的是 card_lint 同一條日期判定",
                _derived_result.returncode == 0
                and "permissionDecision" not in derived_out
                and memspec.CARD_DATE_SOURCE_BODY in derived_out.get("additionalContext", ""),
            ))

            repeat_session = "write-repeat-" + uuid.uuid4().hex
            repeat_input = {
                "file_path": os.fspath(write_root / "again.txt"),
                "content": "還是兩套參數。",
            }
            _first_result, first_out = write_call("Write", repeat_input, session=repeat_session)
            second_result, second_out = write_call("Write", repeat_input, session=repeat_session)
            checks.append((
                "同 session 同規則同檔案同內容只擋一次，AI 修不動時不會無限卡死",
                first_out.get("permissionDecision") == "deny"
                and second_result.returncode == 0
                and not second_out,
            ))

            _codex_result, codex_out = write_call(
                "write_file",
                {
                    "path": os.fspath(write_root / "codex.txt"),
                    "content": "改成兩套參數再說。",
                },
                extra={"transcript_path": os.fspath(root / "codex-synthetic.jsonl")},
            )
            checks.append((
                "Codex 形狀的檔案寫入工具走同一道閘",
                _codex_result.returncode == 0
                and codex_out.get("permissionDecision") == "deny",
            ))

            oversized_content = "兩套參數" + "填充" * 70000
            _big_result, big_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "big.txt"),
                    "content": oversized_content,
                },
            )
            checks.append((
                "超過內容上限就放行（fail-open，不在 hook deadline 內跑大字串）",
                len(oversized_content.encode("utf-8")) > memspec.WRITE_GATE_MAX_CONTENT_BYTES
                and _big_result.returncode == 0
                and not big_out,
            ))

            (write_vault / "broken-forbidden.md").write_text(
                "---\nname: 壞禁詞\ndescription: 2026-09-06 禁詞正則寫壞了\n"
                f"{memspec.DECISION_KEY_FIELD}: k-broken-forbidden\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-09-06\n"
                f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 這條的禁詞寫壞了\n"
                f"{memspec.ALIASES_FIELD}: [壞甲, 壞乙]\n"
                f"{memspec.FORBIDDEN_FIELD}: ['(a+)+$']\n---\nbody\n",
                encoding="utf-8",
            )
            _broken_result, broken_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "probe.txt"),
                    "content": "a" * 24,
                },
            )
            checks.append((
                "無法使用的 forbidden 正則被忽略而非擋下，並向模型點名",
                _broken_result.returncode == 0
                and "permissionDecision" not in broken_out
                and "k-broken-forbidden" in broken_out.get("additionalContext", ""),
            ))

            # U64: 規則 A 走 stop_gate._forbidden_fragment，引號豁免同一處修好兩邊都有。
            quoted_write_result, quoted_write_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "check_forbidden.py"),
                    "content": 'FORBIDDEN_PHRASE = "兩套參數"\nassert FORBIDDEN_PHRASE not in reply\n',
                },
            )
            checks.append((
                "forbidden 落在寫入內容的引號字串字面值內是引用／驗證腳本，不擋",
                quoted_write_result.returncode == 0
                and "permissionDecision" not in quoted_write_out,
            ))

            bare_write_result, bare_write_out = write_call(
                "Write",
                {
                    "file_path": os.fspath(write_root / "check_forbidden2.py"),
                    "content": 'FORBIDDEN_PHRASE = "兩套參數"\n# 這裡直接寫兩套參數，沒加引號。\n',
                },
            )
            checks.append((
                "同一份內容除了引號內的引用還有裸禁詞，裸的那份照擋",
                bare_write_result.returncode == 0
                and bare_write_out.get("permissionDecision") == "deny",
            ))

            missing_config = root / "missing-config.json"
            missing = run_synthetic(
                Path(__file__),
                {"tool_name": "Bash", "tool_input": {"command": "remove target"}},
                missing_config,
            )
            checks.append(
                (
                    "missing config infra failure allows silently",
                    missing.returncode == 0
                    and not missing.stdout
                    and not missing.stderr,
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 31
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _best_effort_record(event):
    """附記這次呼叫碰到的檔名。壞了就算了：這是紀錄，不是關卡，不能讓它擋住工作。"""
    try:
        from epitype import opened

        config = load_config(_STARTED_AT)
        if config is None:
            return
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict):
            return
        payload = " ".join(
            str(value)
            for name, value in tool_input.items()
            if name in memspec.OPENED_TARGET_FIELDS
            and isinstance(value, (str, int, float, list, tuple))
        )
        opened.record(
            governance_vault(config, for_write=True),
            event.get("session_id", event.get("sessionId")),
            payload,
        )
    except Exception:
        pass


def main():
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    defects = []
    try:
        event = read_event(sys.stdin)
        value = _handle(event, _STARTED_AT, defects)
        # 這一次呼叫碰到哪些檔，附記給回合閘用：說「我查過某個檔」的時候，那個檔名
        # 必須在這裡出現過。擋下的呼叫不記——那個檔根本沒被打開。
        if value is None or value.get("hookSpecificOutput", {}).get(
                "permissionDecision") != "deny":
            _best_effort_record(event)
        # A guard card that stopped being enforced is the silent failure §34 warned
        # about, so it is named on stderr rather than swallowed.
        for line in defects[: memspec.GATE_DEFECT_MAX_LINES]:
            print(line, file=sys.stderr)
        if value is not None:
            output = value.get("hookSpecificOutput", {})
            is_deny = output.get("permissionDecision") == "deny"
            if is_deny or not expired(_STARTED_AT):
                emit(value)
    except Exception as exc:
        # 這裡是最後一道：設定檔壞了、記憶庫讀不到、程式本身有 bug，全都走這一圈。
        # 仍然 fail-open（不擋住工作），但裝了卻用不了的時候一定要講一句——安靜退場
        # 跟「沒有東西要擋」在外面看起來一模一樣，而這正是本專案最不能容忍的那種壞掉。
        # 沒有設定檔則照舊安靜：那代表這個專案根本沒在用 Epitype，不是壞掉。
        try:
            if config_path().exists():
                print(memspec.GATE_DEGRADED_NOTICE.format(
                    gate="動作閘", reason=type(exc).__name__), file=sys.stderr)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
