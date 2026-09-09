import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude SessionStart adapter for slim index and work-ledger injection."""

from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import tempfile

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import capture_route, card_lint, commitments, memsearch, memspec, pending_lint
from _hook_common import (
    bounded_context,
    emit,
    expired,
    governance_vault,
    load_config,
    native_cwd_vaults,
    payload,
    payload_fits,
    read_event,
    resolve_vaults,
    run_synthetic,
    write_config,
)


def _soft_remaining(started_at):
    """這場開場還剩多少自用預算（秒）。

    2026-09-06 事故：Codex 把 SessionStart 記成 Failed，因為整場跑超過宿主的 10 s
    才被砍掉——而 `expired()` 只在段與段之間被問到，段內沒有上限的掃描（當時的
    pending_lint）可以一路吃到宿主砍人為止。所以開場另立一個更緊的天花板，且每一段
    都拿「剩餘預算」當自己的期限，不是拿宿主的期限當自己的期限。
    """
    return memspec.SESSIONSTART_BUDGET_SECONDS - (time.monotonic() - started_at)


def _segment_budget(started_at, want):
    """這一段能拿到的秒數；剩太少就回 None＝整段省略（半段的數字是錯的數字）。"""
    remaining = _soft_remaining(started_at)
    if remaining < memspec.SESSIONSTART_SEGMENT_FLOOR_SECONDS:
        return None
    return min(want, remaining)


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


def _recent(decided_at, today):
    """近 30 天內定的？沒有日期的一律不算近期。"""
    try:
        return (today - date.fromisoformat(decided_at[:10])).days <= (
            memspec.SESSIONSTART_DECISION_RECENT_DAYS
        )
    except ValueError:
        return False


def _active_decisions(vault, started_at):
    """(current_decision_at, decision_key, 有沒有 forbidden) per active decision card.

    Read from disk rather than from the index: the index carries `status` but not
    the decision's key, and a vault whose index was never built must still open the
    session with its standing rulings. None when the hook's deadline arrives
    mid-scan — half a vault's rulings would read as the whole list.

    2026-09-06 實測：12 條各帶完整 owner 原話＝1977 bytes。原話是喚回命中那張卡時
    才需要的東西，開場只需要「有哪些現行裁定、哪天定的」。
    """
    found = []
    try:
        paths = memsearch.card_files(vault)
    except Exception:
        return None
    for path in paths:
        if _soft_remaining(started_at) <= 0:
            return None
        try:
            fields = _frontmatter_fields(path)
        except Exception:
            continue
        key = " ".join(str(fields.get(memspec.DECISION_KEY_FIELD) or "").split())
        status = " ".join(str(fields.get(memspec.DECISION_STATUS_FIELD) or "").split())
        if not key or status != memspec.ACTIVE_DECISION_STATUS:
            continue
        found.append(
            (
                " ".join(str(fields.get(memspec.CURRENT_DECISION_AT_FIELD) or "").split()),
                key,
                # 欄位在不在，不看值：區塊式清單被前置解析攤成空字串。
                memspec.FORBIDDEN_FIELD in fields,
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
    today = date.today()
    # 會擋人的裁定（有 forbidden）不論多舊都佔位、也不被上限擠掉；其餘只列近 30 天的，
    # 舊的靠喚回在命中時帶回來。
    forbidden = [row for row in rows if row[2]][: memspec.SESSIONSTART_DECISIONS_MAX_LINES]
    room = memspec.SESSIONSTART_DECISIONS_MAX_LINES - len(forbidden)
    recent = [row for row in rows if not row[2] and _recent(row[0], today)][:room]
    listed = sorted(forbidden + recent, key=lambda row: (row[0], row[1]), reverse=True)
    lines = [memspec.SESSIONSTART_DECISIONS_HEADER.format(vault=label)]
    for decided_at, key, _forbidden in listed:
        lines.append("｜".join(part for part in (key, decided_at) if part))
    dropped = len(rows) - len(listed)
    if dropped > 0:
        lines.append(memspec.SESSIONSTART_DECISION_REST_LINE.format(count=dropped, vault=vault))
    return "\n".join(lines)


def _dream_state(governance):
    """狀態檔本身，不 import epitype.dream：那條 import 每一場開場都要付 ~35 ms，
    而開場真正需要的只是「上次幾點跑完」這個數字。"""
    path = governance / memspec.DREAM_DIRECTORY / memspec.DREAM_STATE_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dream_spawn(settings, governance, started_at=None, launcher=None, source=None):
    """順路做：距上次完成超過 interval_hours 就起一個脫鉤的低優先權背景夢，hook 不等
    它跑完。nightly 交給系統排程（否則同一天會跑兩次），off 什麼都不做。
    開場預算剩不到 DREAM_SPAWN_RESERVE_SECONDS 就不起——夢晚一場沒關係，記憶注入
    掉一場才是真的損失。壓縮續場同理不起：那不是新的一場，而長回合裡壓縮幾次就起幾次
    背景程序，剛好搶走這場正在用的 CPU。"""
    if isinstance(source, str) and source == "compact":
        return False
    if settings.get(memspec.DREAM_MODE_FIELD) != memspec.DREAM_MODE_PIGGYBACK:
        return False
    if started_at is not None:
        if _soft_remaining(started_at) < memspec.DREAM_SPAWN_RESERVE_SECONDS:
            return False
    completed = _dream_state(governance).get(memspec.DREAM_STATE_COMPLETED_EPOCH_FIELD)
    hours = settings.get(
        memspec.DREAM_INTERVAL_HOURS_FIELD, memspec.DREAM_DEFAULT_INTERVAL_HOURS
    )
    if not isinstance(completed, bool) and isinstance(completed, (int, float)):
        if (time.time() - completed) < hours * 3600:
            return False
    from epitype import dream  # 只有到期的那一場付這個 import 的錢

    return dream.spawn(governance, launcher=launcher)


def _dream_notice(governance, source, settings):
    """夢跑完後的下一場開場印一行，只印一次。壓縮續場不印——那不是新的一場。
    沒有待處理項也印一行短的：不然「夢跑完但乾淨」與「夢從沒跑」長得一樣。"""
    if settings.get(memspec.DREAM_MODE_FIELD) == memspec.DREAM_MODE_OFF:
        return None
    if isinstance(source, str) and source == "compact":
        return None
    state = _dream_state(governance)
    completed = state.get(memspec.DREAM_STATE_COMPLETED_FIELD)
    if not isinstance(completed, str) or not completed:
        return None
    if state.get(memspec.DREAM_STATE_NOTIFIED_FIELD) == completed:
        return None
    headline = state.get(memspec.DREAM_STATE_HEADLINE_FIELD)
    headline = headline if isinstance(headline, dict) else {}
    numbers = {}
    for field in memspec.DREAM_HEADLINE_FIELDS:
        value = headline.get(field)
        numbers[field] = value if isinstance(value, int) and not isinstance(value, bool) else 0
    # 標記失敗就不印：印不掉的通知會每一場重複，而重複的開場行比漏一行更糟。
    if not _dream_mark_notified(governance, state, completed):
        return None
    when = state.get(memspec.DREAM_STATE_DATE_FIELD)
    when = when if isinstance(when, str) and when else completed[:10]
    pack = state.get(memspec.DREAM_STATE_PACK_FIELD)
    pack = pack if isinstance(pack, str) and pack else "-"
    # 舊格式沒有完整性證據；保留通知，但不能把未知或部分結果宣告成乾淨。
    if (state.get(memspec.DREAM_STATE_COMPLETE_FIELD) is not True
            or state.get(memspec.DREAM_STATE_ERRORS_FIELD)):
        return memspec.DREAM_NOTICE_INCOMPLETE_LINE.format(date=when, pack=pack)
    if not any(numbers.values()):
        return memspec.DREAM_NOTICE_CLEAN_LINE.format(date=when)
    return memspec.DREAM_NOTICE_LINE.format(
        date=when, pack=pack, **numbers
    )


def _dream_mark_notified(governance, state, completed):
    """標記寫成暫存檔再 os.replace：`write_text` 先截斷再寫，宿主在那一瞬間砍掉
    hook（2026-09-06 Codex 逾時事故）就留下一個 0 byte 的 dream_state.json——夢的
    完成時間、headline、pack 路徑一起消失，而且沒有任何一段會發現它消失了。"""
    path = governance / memspec.DREAM_DIRECTORY / memspec.DREAM_STATE_FILENAME
    temporary = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        value = dict(state)
        value[memspec.DREAM_STATE_NOTIFIED_FIELD] = completed
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False


def _vault_labels(vaults):
    """Directory names, falling back to the full path where a name repeats: every
    native cwd vault is called `memory`, so the short name alone can be a lie."""
    names = [vault.name for vault in vaults]
    return [
        name if names.count(name) == 1 else str(vault)
        for vault, name in zip(vaults, names)
    ]


def _claude_native_index_vaults(event, native):
    """Claude Code hands every hook a transcript_path under ~/.claude/projects/<slug>/
    and already loads that cwd slug's MEMORY.md into context, so echoing it here
    doubled up to 3 KB per session (owner 2026-09-09: skip it on Claude). Codex has
    no native index load and no .claude transcript, so its echo stays; ancestor
    vaults are not loaded natively either and stay."""
    path = event.get("transcript_path") if isinstance(event, dict) else None
    if not isinstance(path, str) or ".claude" not in path.replace("\\", "/").split("/"):
        return set()
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return set()
    projects = Path.home().joinpath(*capture_route.NATIVE_PROJECTS_SUBPATH)
    texts = {cwd}
    try:
        texts.add(str(Path(cwd).resolve()))
    except OSError:
        pass
    exact = set()
    for text in texts:
        try:
            exact.add((projects / capture_route.project_slug(text) / capture_route.NATIVE_MEMORY_DIRNAME).resolve())
        except OSError:
            continue
    return {vault for vault in native if vault in exact}


def _joined(pieces, piece=""):
    """bounded_context 之後會怎麼接，這裡就先怎麼接——量錯拼法就量錯預算。"""
    parts = [item for item in pieces if isinstance(item, str) and item]
    if piece:
        parts.append(piece)
    return "\n".join(parts)


# 截斷行的位置要先留：bounded_context 放不下某一段時，是把**已選的前面幾段丟掉**
# 直到截斷行塞得進去。索引段量到剛好滿，下一段（帳本第一行）一放不下，索引就整段
# 被彈出來——實測 2026-09-09：整本回音「裝得下」卻在輸出裡整段消失。
_SUFFIX_RESERVE = memspec.CONTEXT_TRUNCATED_SUFFIX.format(dropped=9999)


def _fits(pieces, piece, budget):
    return payload_fits(
        "SessionStart", _joined(pieces, piece) + "\n" + _SUFFIX_RESERVE, budget
    )


def _index_echo(index_path, pieces, budget):
    """沒有原生載入的宿主（Codex）拿到的短入口：裝得下就整段，裝不下才排序取樣，
    並在最後一行明說送出多少／全文多少。

    預算按 `payload_fits` 那一份真實 JSON 編碼位元組算，不是字元數、也不是固定
    「截前 3 KB」——舊碼截了不留痕跡，收件端無從得知偏好有沒有送到（2026-09-09
    Claude↔Codex 收斂第 8 條）。裝不下就一路縮，縮到連 slim 的路徑頁尾都放不下時
    整段不送：半段索引配一行沒對上的位元組數，比沒有索引更難判讀。
    """
    heading = f"## {memspec.MEMORY_INDEX_FILENAME}"
    body = index_path.read_text(encoding="utf-8").rstrip()
    total = len(body.encode("utf-8"))
    if _fits(pieces, f"{heading}\n{body}", budget):
        return f"{heading}\n{body}"

    full_path = index_path.resolve()

    def notice(sent):
        return memspec.SESSIONSTART_INDEX_TRUNCATED_LINE.format(
            filename=memspec.MEMORY_INDEX_FILENAME, sent=sent, total=total, path=full_path
        )

    used = len(_joined(pieces).encode("utf-8"))
    room = budget - used - len(
        f"{heading}\n\n{notice(total)}\n{_SUFFIX_RESERVE}".encode("utf-8")
    )
    while room > 0:
        try:
            slim = memspec.slim_index(body, room, full_path)
        except ValueError:
            return None
        piece = "\n".join((heading, slim, notice(len(slim.encode("utf-8")))))
        if _fits(pieces, piece, budget):
            return piece
        room -= max(64, room // 8)
    return None


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
        vaults = resolved  # no ledger anywhere: inject every configured vault
    else:
        vaults = [vault for vault in resolved if vault in native or vault == governance]

    source = event.get("source") if isinstance(event, dict) else None
    dream = config.get(memspec.DREAM_CONFIG_FIELD) or {}
    if _soft_remaining(started_at) > 0:
        try:
            _dream_spawn(dream, governance_vault(config, for_write=True), started_at, source=source)
        except Exception:
            pass  # 夢起不來絕不影響開場注入

    # One line, first, so the budget cannot drop it: pending items with an entry
    # and no exit are exactly what resurfaces as wrong memory later.
    seconds = _segment_budget(started_at, memspec.PENDING_LINT_HOOK_BUDGET_SECONDS)
    if seconds is not None:
        overdue = pending_lint.summary_line(vaults, time_budget=seconds)
        if overdue:
            pieces.append(overdue)

    # U53：AI 自己開的承諾（「我等一下會…」）沒有任何人在追，而 compaction 正是它蒸發
    # 的時刻——所以 source: compact 也印。這不是 owner 的待辦，帳本另放，一行帶最近一條。
    if _soft_remaining(started_at) >= memspec.SESSIONSTART_SEGMENT_FLOOR_SECONDS:
        promised = commitments.summary_line(vaults, memspec.COMMITMENT_SESSIONSTART_MAX)
        if promised:
            pieces.append(promised)

    # A card missing its type's required fields is a card the recall side will
    # hand over half-true. One line, and only when the scan finished inside its
    # own budget: half a vault's numbers are worse than no numbers.
    # 同一趟掃描也餵下面那行順手任務：掃兩次就是同一份預算付兩次。
    seconds = _segment_budget(started_at, memspec.CARD_LINT_HOOK_BUDGET_SECONDS)
    if seconds is not None:
        reports = card_lint.scan_vaults(vaults, time_budget=seconds)
        malformed = card_lint.summary_line(vaults, reports=reports)
        if malformed:
            pieces.append(malformed)

        # owner 2026-09-06 裁定：卡沒有中文別名不是給 owner 的決定題，是本場 AI 順手
        # 補的事。壓縮續場不印——那不是新的一場，翻譯任務也不該在同一場派兩次。
        if source != "compact":
            try:
                translate = card_lint.no_chinese_line(reports, governance)
            except Exception:
                translate = None
            if translate:
                pieces.append(translate)

    # 同一裁定的另一半，一場說一次：喚回的卡與現況不符就直接改，不要端回來問。
    pieces.append(memspec.CARD_SELF_CORRECT_NOTICE)

    # 夢的一行跟其他一行摘要放在一起，排在裁定之前：它是狀態，不是規則。
    if _soft_remaining(started_at) > 0:
        try:
            notice = _dream_notice(governance_vault(config, for_write=True), source, dream)
        except Exception:
            notice = None
        if notice:
            pieces.append(notice)

    # 2026-09-05 事故：owner 08-13 親裁的事被端回來當選項。A standing ruling the model
    # cannot see is a ruling it re-opens, so every session — including the one that
    # resumes after a compaction — opens with the vault's active decisions, in the
    # owner's own words, before any index.
    for vault, label in zip(vaults, _vault_labels(vaults)):
        if _soft_remaining(started_at) <= 0:
            break
        block = _decision_block(vault, label, started_at)
        if block:
            pieces.append(block)

    skip_index = _claude_native_index_vaults(event, native)
    for vault in vaults:
        if expired(started_at):
            return None
        index_path = vault / memspec.MEMORY_INDEX_FILENAME
        if index_path.is_file() and vault not in skip_index:
            echo = _index_echo(index_path, pieces, budget)
            if echo:
                pieces.append(echo)

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
                    "index and ledger injection: a short index goes whole, with no sampling footer",
                    result.returncode == 0
                    and "# Synthetic Index" in context
                    and "index detail" in context
                    and "ledger detail" in context
                    and "Full index:" not in context
                    and "未完整回音" not in context,
                )
            )

            # 裝不下時的行為才是這段程式的重點：舊碼固定截前 3 KB 又不留痕跡。
            big_vault = root / "bigvault"
            big_vault.mkdir()
            big_body = "# Big Index\n" + "\n".join(
                f"- line {number} 索引內容 padding padding" for number in range(1200)
            )
            (big_vault / memspec.MEMORY_INDEX_FILENAME).write_text(
                big_body + "\n", encoding="utf-8"
            )
            # 索引後面一定要還有段（帳本行），才測得到那個坑：bounded_context 放不下
            # 下一段時是回頭把已選的段丟掉，所以量到剛好滿的索引會整段被彈出來。
            (big_vault / memspec.WORK_LEDGER_FILENAME).write_text(
                "\n".join(f"ledger row {number}" for number in range(40)) + "\n",
                encoding="utf-8",
            )
            big_config = root / "big-config.json"
            write_config(big_config, [big_vault])
            big_result = run_synthetic(Path(__file__), {"source": "startup"}, big_config)
            big_value = json.loads(big_result.stdout) if big_result.stdout.strip() else {}
            big_context = big_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            notice_line = next(
                (line for line in big_context.splitlines() if "未完整回音" in line), ""
            )
            numbers = re.search(r"送出 (\d+)／全文 (\d+)", notice_line)
            heading = f"## {memspec.MEMORY_INDEX_FILENAME}\n"
            sampled = ""
            if numbers and heading in big_context:
                start = big_context.index(heading) + len(heading)
                sampled = big_context[start:big_context.index(notice_line)].rstrip("\n")
            checks.append(
                (
                    "oversized index is sampled, and the notice states the real bytes sent",
                    big_result.returncode == 0
                    and numbers is not None
                    and int(numbers.group(1)) == len(sampled.encode("utf-8"))
                    and int(numbers.group(2)) == len(big_body.encode("utf-8"))
                    and int(numbers.group(1)) < int(numbers.group(2))
                    and str((big_vault / memspec.MEMORY_INDEX_FILENAME).resolve()) in big_context
                    and len(big_context.encode("utf-8")) <= memspec.HOOK_DEFAULT_BUDGET_BYTES,
                )
            )
            checks.append(
                (
                    "the sampled index survives the later pieces: room is left for the cut suffix",
                    heading in big_context
                    and memspec.CONTEXT_TRUNCATED_SUFFIX.split("{")[0] in big_context
                    and notice_line in big_context,
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

            # Owner 2026-09-09: Claude Code loads the cwd slug's MEMORY.md itself, so a
            # Claude-shaped event (transcript under ~/.claude/projects) skips that echo
            # while the governance index and ledger stay. Codex-shaped events above
            # (no .claude transcript) keep the echo.
            claude_result = run_synthetic(
                Path(__file__),
                {
                    "source": "startup",
                    "cwd": str(project),
                    "transcript_path": str(home / ".claude" / "projects" / slug / "session.jsonl"),
                },
                config,
                environment={"HOME": os.fspath(home), "USERPROFILE": os.fspath(home)},
            )
            claude_value = json.loads(claude_result.stdout) if claude_result.stdout.strip() else {}
            claude_context = claude_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "Claude host skips the natively loaded cwd index, keeps governance index and ledger",
                    claude_result.returncode == 0
                    and "native index detail" not in claude_context
                    and "index detail" in claude_context
                    and "ledger detail" in claude_context,
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
                    "overdue pending line comes first, before the index",
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

            # owner 2026-09-06：沒中文別名的卡不是 WARN，是本場的順手任務，每場換人。
            translate_vault = root / "translate-vault"
            translate_vault.mkdir()
            (translate_vault / memspec.MEMORY_INDEX_FILENAME).write_text(
                "# Translate\ntranslate index detail\n", encoding="utf-8"
            )
            for letter in "abcd":
                (translate_vault / f"english-{letter}.md").write_text(
                    f"---\nname: english-{letter}\ndescription: english only card {letter}\n"
                    f"{memspec.LAST_VERIFIED_AT_FIELD}: 2026-09-01\n"
                    f"{memspec.ALIASES_FIELD}:\n  - english alias {letter}\n"
                    "metadata:\n  type: reference\n---\nbody\n",
                    encoding="utf-8",
                )
            translate_config = root / "translate-config.json"
            write_config(translate_config, [translate_vault])

            def translate_run(source="startup"):
                done = run_synthetic(Path(__file__), {"source": source}, translate_config)
                value = json.loads(done.stdout) if done.stdout.strip() else {}
                return done, value.get("hookSpecificOutput", {}).get("additionalContext", "")

            first_run, first_context = translate_run()
            second_run, second_context = translate_run()
            _compact_run, compact_translate_context = translate_run("compact")
            checks.append(
                (
                    "沒中文的卡列成一行順手任務（本場 ≤3 張），不是 WARN，也不佔卡片檢查那一行",
                    first_run.returncode == 0
                    and "🈳 順手補中文別名（本場 ≤3 張）：" in first_context
                    and all(f"english-{letter}.md" in first_context for letter in "abc")
                    and "english-d.md" not in first_context
                    and "🧾" not in first_context
                    and "translate index detail" in first_context,
                )
            )
            checks.append(
                (
                    "游標讓每場輪替：下一場從上次列到的那張之後接下去；壓縮續場不派翻譯",
                    second_run.returncode == 0
                    and "english-d.md" in second_context
                    and "english-c.md" not in second_context
                    and "🈳" not in compact_translate_context,
                )
            )
            checks.append(
                (
                    "喚回的卡與現況不符就直接修卡——這條規則每一場說一次，壓縮續場也說",
                    memspec.CARD_SELF_CORRECT_NOTICE in first_context
                    and memspec.CARD_SELF_CORRECT_NOTICE in compact_translate_context
                    and "superseded" in memspec.CARD_SELF_CORRECT_NOTICE,
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
            # 日期相對今天算，否則這份題目會隨時間自己過期（近 30 天的判斷是時間函數）。
            today = date.today()
            recent_days = [
                (today - timedelta(days=index)).isoformat() for index in range(14)
            ]
            stale_day = (
                today - timedelta(days=memspec.SESSIONSTART_DECISION_RECENT_DAYS + 10)
            ).isoformat()
            for index in range(14):
                (decision_vault / f"decision-{index:02d}.md").write_text(
                    f"---\nname: Decision {index}\ndescription: 2026 決策摘要 {index}\n"
                    f"{memspec.DECISION_KEY_FIELD}: rule-{index:02d}\n"
                    f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                    f"{memspec.CURRENT_DECISION_AT_FIELD}: {recent_days[13 - index]}\n"
                    f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                    f"{memspec.OWNER_QUOTE_FIELD}: 只有 6s 是標準合約 {index}\n---\nbody\n",
                    encoding="utf-8",
                )
            # 舊的沒 forbidden＝不列，只進「另 N 條」；舊的有 forbidden＝會擋人，照列。
            for name, key, forbidden in (
                ("decision-stale", "rule-stale", ""),
                ("decision-stale-forbidden", "rule-stale-forbidden",
                 f"{memspec.FORBIDDEN_FIELD}:\n  - 再提議改回舊制\n"),
            ):
                (decision_vault / f"{name}.md").write_text(
                    f"---\nname: {key}\ndescription: 舊決策\n"
                    f"{memspec.DECISION_KEY_FIELD}: {key}\n"
                    f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                    f"{memspec.CURRENT_DECISION_AT_FIELD}: {stale_day}\n"
                    f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                    f"{forbidden}---\nbody\n",
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
                    "active decisions open the session as key｜date, newest first, above the index",
                    decision_result.returncode == 0
                    and decision_header in decision_context
                    and decision_lines[:2] == [
                        f"rule-13｜{recent_days[0]}",
                        f"rule-12｜{recent_days[1]}",
                    ]
                    and "只有 6s 是標準合約" not in decision_context
                    and "rule-retired" not in decision_context
                    and decision_context.index(decision_header)
                    < decision_context.index("decision index detail"),
                )
            )
            checks.append(
                (
                    "old decisions are dropped unless they carry forbidden, and the cut is said",
                    len(decision_lines) == memspec.SESSIONSTART_DECISIONS_MAX_LINES
                    and f"rule-stale-forbidden｜{stale_day}" in decision_context
                    and f"rule-stale｜{stale_day}" not in decision_context
                    and memspec.SESSIONSTART_DECISION_REST_LINE.format(
                        count=4, vault=decision_vault
                    ) in decision_context,
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
                    and f"rule-13｜{recent_days[0]}" in compact_context,
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

            # --- 夢：順路做的觸發條件與開場通知 ---
            import time as _time

            dream_vault = root / "dream-vault"
            dream_vault.mkdir()
            (dream_vault / memspec.MEMORY_INDEX_FILENAME).write_text(
                "# Dream\ndream index detail\n", encoding="utf-8"
            )
            dream_config = root / "dream-config.json"
            write_config(dream_config, [dream_vault])
            dream_dir = dream_vault / memspec.DREAM_DIRECTORY
            dream_dir.mkdir()
            dream_state_file = dream_dir / memspec.DREAM_STATE_FILENAME
            lock_file = dream_dir / memspec.DREAM_LOCK_FILENAME
            launched = []

            def _fake_launcher(argv, log_path):
                launched.append(argv)
                return 99

            piggyback = {
                memspec.DREAM_MODE_FIELD: memspec.DREAM_MODE_PIGGYBACK,
                memspec.DREAM_INTERVAL_HOURS_FIELD: 24,
            }
            first = _dream_spawn(piggyback, dream_vault, launcher=_fake_launcher)
            held = _dream_spawn(piggyback, dream_vault, launcher=_fake_launcher)
            checks.append(
                (
                    "a vault that never dreamt spawns one detached dream, and the held lock stops a second",
                    first
                    and not held
                    and len(launched) == 1
                    and launched[0][-4:-1]
                    == [memspec.DREAM_SCHEDULED_FLAG, memspec.DREAM_LOCK_HELD_FLAG, "--lock-token"]
                    and lock_file.is_file(),
                )
            )

            lock_file.write_text(
                json.dumps({"pid": 0, "started": _time.time() - memspec.DREAM_LOCK_STALE_SECONDS - 60}),
                encoding="utf-8",
            )
            stale = _dream_spawn(piggyback, dream_vault, launcher=_fake_launcher)
            lock_file.unlink()
            checks.append(
                (
                    "a dead owner's stale lock is taken over rather than blocking every later dream",
                    stale and len(launched) == 2,
                )
            )

            def _write_dream_state(completed_epoch, headline, notified=None):
                value = {
                    memspec.DREAM_STATE_COMPLETED_EPOCH_FIELD: completed_epoch,
                    memspec.DREAM_STATE_COMPLETED_FIELD: "2026-09-06T03:30:00+00:00",
                    memspec.DREAM_STATE_DATE_FIELD: "2026-09-06",
                    memspec.DREAM_STATE_COMPLETE_FIELD: True,
                    memspec.DREAM_STATE_ERRORS_FIELD: {},
                    memspec.DREAM_STATE_PACK_FIELD: os.fspath(dream_dir / memspec.DREAM_PACK_FILENAME),
                    memspec.DREAM_STATE_HEADLINE_FIELD: headline,
                    memspec.DREAM_STATE_NOTIFIED_FIELD: notified,
                }
                dream_state_file.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

            full_headline = {"card_fail": 2, "missing_aliases": 7, "drafts": 1, "open_commitments": 3}
            _write_dream_state(_time.time(), full_headline)
            inside = _dream_spawn(piggyback, dream_vault, launcher=_fake_launcher)
            _write_dream_state(_time.time() - 25 * 3600, full_headline)
            outside = _dream_spawn(piggyback, dream_vault, launcher=_fake_launcher)
            lock_file.unlink()
            checks.append(
                (
                    "no dream inside the interval, one dream once the interval has passed",
                    not inside and outside and len(launched) == 3,
                )
            )

            _write_dream_state(_time.time() - 25 * 3600, full_headline)
            nightly = _dream_spawn(
                {**piggyback, memspec.DREAM_MODE_FIELD: memspec.DREAM_MODE_NIGHTLY},
                dream_vault,
                launcher=_fake_launcher,
            )
            off = _dream_spawn(
                {**piggyback, memspec.DREAM_MODE_FIELD: memspec.DREAM_MODE_OFF},
                dream_vault,
                launcher=_fake_launcher,
            )
            checks.append(
                (
                    "nightly leaves the dream to the system scheduler and off starts nothing, however overdue",
                    not nightly and not off and len(launched) == 3 and not lock_file.exists(),
                )
            )

            late = _dream_spawn(
                piggyback,
                dream_vault,
                started_at=time.monotonic() - memspec.HOOK_TIMEOUT_SECONDS + 1,
                launcher=_fake_launcher,
            )
            checks.append(
                (
                    "a session that has almost spent its budget skips the dream rather than risking the injection",
                    not late and len(launched) == 3 and not lock_file.exists(),
                )
            )

            # U56b：壓縮續場不是新的一場，長回合壓縮幾次就起幾支背景夢搶自己的 CPU。
            compact_spawn = _dream_spawn(piggyback, dream_vault, launcher=_fake_launcher, source="compact")
            resume_spawn = _dream_spawn(piggyback, dream_vault, launcher=_fake_launcher, source="resume")
            lock_file.unlink()
            checks.append(
                (
                    "壓縮續場就算夢已到期也不起背景夢，resume 之類的新開場才起",
                    not compact_spawn and resume_spawn and len(launched) == 4,
                )
            )

            _write_dream_state(_time.time(), full_headline)
            nightly_env = {memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_NIGHTLY}
            notice_result = run_synthetic(
                Path(__file__), {"source": "startup"}, dream_config, environment=nightly_env
            )
            notice_value = json.loads(notice_result.stdout) if notice_result.stdout.strip() else {}
            notice_context = notice_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            repeat_result = run_synthetic(
                Path(__file__), {"source": "startup"}, dream_config, environment=nightly_env
            )
            repeat_value = json.loads(repeat_result.stdout) if repeat_result.stdout.strip() else {}
            repeat_context = repeat_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            expected_notice = memspec.DREAM_NOTICE_LINE.format(
                date="2026-09-06",
                pack=os.fspath(dream_dir / memspec.DREAM_PACK_FILENAME),
                **full_headline,
            )
            checks.append(
                (
                    "a finished dream is announced once, with its numbers and pack path, and never again",
                    notice_result.returncode == 0
                    and expected_notice in notice_context
                    and "dream index detail" in notice_context
                    and "🌙" not in repeat_context
                    and "dream index detail" in repeat_context,
                )
            )

            _write_dream_state(_time.time(), full_headline)
            compact_notice = run_synthetic(
                Path(__file__), {"source": "compact"}, dream_config, environment=nightly_env
            )
            compact_notice_value = json.loads(compact_notice.stdout) if compact_notice.stdout.strip() else {}
            compact_notice_context = compact_notice_value.get("hookSpecificOutput", {}).get(
                "additionalContext", ""
            )
            _write_dream_state(_time.time(), {field: 0 for field in memspec.DREAM_HEADLINE_FIELDS})
            clean_notice = run_synthetic(
                Path(__file__), {"source": "startup"}, dream_config, environment=nightly_env
            )
            clean_notice_value = json.loads(clean_notice.stdout) if clean_notice.stdout.strip() else {}
            clean_notice_context = clean_notice_value.get("hookSpecificOutput", {}).get(
                "additionalContext", ""
            )
            checks.append(
                (
                    "the compaction-resumed session says nothing about the dream; a clean dream still says it ran",
                    "🌙" not in compact_notice_context
                    and memspec.DREAM_NOTICE_CLEAN_LINE.format(date="2026-09-06") in clean_notice_context,
                )
            )

            # 2026-09-06 事故：Codex 把 SessionStart 記成 Failed。宿主砍 hook 的兩個
            # 理由只有「逾時」與「stdout 不是它認得的 JSON」，所以這兩件事各釘一次，
            # 而且釘在一個大到會讓無界掃描現形的庫上（300 卡）。
            bulk_vault = root / "bulk-vault"
            bulk_vault.mkdir()
            (bulk_vault / memspec.MEMORY_INDEX_FILENAME).write_text(
                "# Bulk\nbulk index detail\n", encoding="utf-8"
            )
            for number in range(300):
                (bulk_vault / f"feedback-bulk-{number:03d}.md").write_text(
                    f"---\nname: bulk-{number:03d}\ndescription: 合成卡 {number}\n---\n"
                    "- 2026-01-01 待辦：合成殭屍待辦，沒有出口\n"
                    "本文一行。\n",
                    encoding="utf-8",
                )
            bulk_config = root / "bulk-config.json"
            write_config(bulk_config, [bulk_vault])
            bulk_started = time.monotonic()
            bulk_result = run_synthetic(
                Path(__file__),
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "selftest-codex",
                    "cwd": os.fspath(root),
                    "source": "startup",
                },
                bulk_config,
                # Codex 的事件帶 cwd，而 cwd 的每一層祖先都會去 home 底下找同名的原生
                # 庫；不改 home 的話這一項會把跑測試那台機器的真實庫拌進來。
                environment={"USERPROFILE": os.fspath(root), "HOME": os.fspath(root)},
            )
            bulk_elapsed = time.monotonic() - bulk_started
            bulk_value = json.loads(bulk_result.stdout) if bulk_result.stdout.strip() else {}
            bulk_inner = bulk_value.get("hookSpecificOutput") if isinstance(bulk_value, dict) else None
            checks.append(
                (
                    "the Codex-shaped event answers with exactly the two keys the host accepts",
                    bulk_result.returncode == 0
                    and set(bulk_value) == {"hookSpecificOutput"}
                    and isinstance(bulk_inner, dict)
                    and set(bulk_inner) == {"hookEventName", "additionalContext"}
                    and bulk_inner["hookEventName"] == "SessionStart"
                    and "bulk index detail" in bulk_inner["additionalContext"],
                )
            )
            # 上限＝開場自用預算＋子程序啟動與 import 的固定成本，仍遠低於宿主的 10 s。
            checks.append(
                (
                    "a 300-card vault still answers inside the SessionStart budget",
                    bulk_elapsed <= memspec.SESSIONSTART_BUDGET_SECONDS + 4.0,
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
    total = 31
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
            sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
