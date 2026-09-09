import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，避免繁中輸出在程式進入點就中斷。
"""首次使用的大整理前兩階段：盤點與回放收割（零模型、全本機）。

線上只在當下那句 prompt 捕捉 owner 的授權／糾正／裁決，所以第一天到今天的歷史
全部沒有卡；治理層蓋在不完整的記憶上等於沒有治理。本模組把線上那套規則
（epitype.capture）回放到全部歷史 transcript 與文件，只抄 owner 原話、附
transcript file:line，不用模型也不做摘要。
"""

import argparse
import json
import os
from pathlib import Path
import re
import time

try:
    from . import capture, capture_route, card_io, memsearch, memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import capture
    import capture_route
    import card_io
    import memsearch
    import memspec


CLAUDE_TRANSCRIPT_GLOB = ("projects", "*", "*.jsonl")
CODEX_TRANSCRIPT_GLOB = ("sessions", "*", "*", "*", "rollout-*.jsonl")
MANIFEST_SUBPATH = (".epitype", "harvest_manifest.json")
DRAFT_SUBPATH = ("_drafts", "decisions")
DUPLICATES_SUBPATH = ("_drafts", "duplicates")
EVENT_DIRECTORIES = (memspec.GRANT_DIRECTORY, memspec.CORRECTION_DIRECTORY, memspec.RULING_DIRECTORY)
DOC_SUFFIXES = (".md", ".markdown", ".txt")
# 粗篩只認角色字串本身，不認 '"type":"user"' 整段：不同寫入端的分隔符空白不一，
# 認整段會讓真 transcript 過篩、合成 transcript 落篩（或反過來）。
ROLE_MARKERS = (b'"user"', b'"assistant"')
# Codex 只在開場的 session_meta 寫一次 cwd，那一行沒有角色字串；不放它過粗篩，
# 回放出來的 Codex 卡就永遠沒有來源專案（實測治理庫 40 張卡的 cwd 是空的）。
SESSION_META_TYPE = "session_meta"
SESSION_META_MARKER = b'"session_meta"'
SCAN_MARKERS = (*ROLE_MARKERS, SESSION_META_MARKER)
# 文件裡的 owner 裁定句：只挖明確標記 owner 裁定或 owner 原話引號的行，其餘留給人。
DOC_RULING_REGEX = re.compile(r"owner\s*(?:已)?裁(?:定|示|決)|owner[:：]\s*[「\"]", re.IGNORECASE)
DECISION_KEY_REGEX = re.compile(rf"^{re.escape(memspec.DECISION_KEY_FIELD)}:\s*(\S.*)$", re.MULTILINE)
# capture.write_capture's format is "description: {label} {date}: {summary}";
# stripping label+date lets a grant card and a correction card for the same
# owner sentence compare equal (the "一句兩卡" duplicate case) even though the
# label differs.
CARD_SUMMARY_REGEX = re.compile(r"^description:.*?\d{4}-\d{2}-\d{2}:\s*(.*)$", re.MULTILINE)
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FRONTMATTER_SCAN_BYTES = 8 * 1024
INDEX_LOCK_SECONDS = 0.5


def _date_argument(value):
    if not DATE_REGEX.match(value):
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    return value


def _utc_date(seconds):
    return time.strftime("%Y-%m-%d", time.gmtime(seconds))


def _utc_stamp(seconds=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def transcripts(home, parts, base):
    """Sorted transcript paths under home/base, or [] when the host is absent."""
    root = Path(home) / base
    try:
        return sorted(root.glob(os.path.join(*parts).replace(os.sep, "/")))
    except OSError:
        return []


def claude_transcripts(home):
    return transcripts(home, CLAUDE_TRANSCRIPT_GLOB, ".claude")


def codex_transcripts(home):
    return transcripts(home, CODEX_TRANSCRIPT_GLOB, ".codex")


def transcript_date(path):
    """The session's date: Codex writes it into the path, Claude only into mtime."""
    parts = path.parts
    if len(parts) >= 4 and parts[-4].isdigit() and len(parts[-4]) == 4:
        year, month, day = parts[-4], parts[-3], parts[-2]
        if month.isdigit() and day.isdigit():
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    try:
        return _utc_date(path.stat().st_mtime)
    except OSError:
        return ""


def owner_text(item):
    """One record's owner-typed text, or None when the owner did not type it.

    Sidechain (subagent) turns, tool results, and host-injected blocks are not the
    owner speaking; a card written from them would be Epitype quoting itself.
    """
    if not isinstance(item, dict) or item.get("isSidechain") is True:
        return None
    if item.get("type") == "user":
        if item.get("isMeta") or item.get("isCompactSummary"):
            return None
        message = item.get("message")
        kinds = ("text",)
    elif item.get("type") == "response_item":
        message = item.get("payload")
        if not isinstance(message, dict) or message.get("type") != "message" or message.get("role") != "user":
            return None
        kinds = ("input_text",)
    else:
        return None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in kinds and isinstance(block.get("text"), str)
        )
    else:
        return None
    text = text.strip()
    # Codex hands the host's own blocks (<user_instructions>, AGENTS.md, environment
    # context) to the model as user content; a tag-opened turn is never owner speech.
    if not text or text.startswith("<"):
        return None
    return text


def utterances(path):
    """Yield (line_number, owner text, preceding assistant text, record).

    The assistant text is the ruling question window: it resets at every user-shaped
    record, exactly as the online tail reader stops walking back at one.
    """
    pending = []
    session_cwd = ""
    try:
        stream = Path(path).open("rb")
    except OSError:
        return
    with stream:
        for number, raw in enumerate(stream, 1):
            raw = raw.rstrip(b"\r\n")
            if len(raw) > memspec.COMPACT_MAP_MAX_LINE_BYTES:
                continue  # a 2MB line is a pasted payload, not a sentence
            if not any(marker in raw for marker in SCAN_MARKERS):
                continue
            try:
                item = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue  # a truncated or half-written line ends nothing
            if not isinstance(item, dict):
                continue
            if item.get("type") == SESSION_META_TYPE:
                # Claude writes cwd on every record; Codex writes it once, here.
                meta = item.get("payload")
                if isinstance(meta, dict) and isinstance(meta.get("cwd"), str):
                    session_cwd = meta["cwd"]
                continue
            text = capture.assistant_text(item)
            if text is not None:
                if text.strip():
                    pending.append(text)
                continue
            if item.get("type") not in ("user", "response_item"):
                continue
            question = "".join(pending)
            pending = []
            owner = owner_text(item)
            if owner:
                if session_cwd and not isinstance(item.get("cwd"), str):
                    # 補在紀錄上，落點與卡上的 cwd 欄位就與線上捕捉同一個來源。
                    item["cwd"] = session_cwd
                yield number, owner, question, item


CARD_DIRECTORIES = {
    "grant": memspec.GRANT_DIRECTORY,
    "correction": memspec.CORRECTION_DIRECTORY,
    "ruling": memspec.RULING_DIRECTORY,
}
CARD_LABELS = {
    "grant": "owner grant auto-captured",
    "correction": "owner correction auto-captured",
    "ruling": "owner ruling auto-captured",
}


def candidates(text, question):
    """The card this utterance would produce, as [(kind, directory, digest, label,
    body, summary)] or []. One decision path for online and replay: the whole
    judgment is capture.classify, so a replayed history card and a live one carry
    the same standard."""
    found = capture.classify(text, question)
    if found is None:
        return []
    kind, body, summary, digest_source = found
    return [(
        kind, CARD_DIRECTORIES[kind], capture.grant_digest(digest_source),
        CARD_LABELS[kind], body, summary,
    )]


def _record_stamp(item, fallback):
    raw = item.get("timestamp")
    if isinstance(raw, str) and len(raw) >= 19 and raw[4:5] == "-" and raw[7:8] == "-" and raw[10:11] == "T":
        return raw[:19] + "Z"
    return fallback


def _record_event(item, session_id):
    cwd = item.get("cwd")
    return {
        "cwd": cwd if isinstance(cwd, str) else "",
        "session_id": item.get("sessionId") if isinstance(item.get("sessionId"), str) else session_id,
    }


def _card_fields(path):
    """(has decision_key, has aliases) for one card, read from a bounded head.

    別名語意沿用索引的 parser、欄名沿用 memspec：盤點的數字必須和索引與 lint 同義。
    """
    try:
        with Path(path).open("rb") as stream:
            head = stream.read(FRONTMATTER_SCAN_BYTES)
    except OSError:
        return False, False
    text = head.decode("utf-8", errors="replace").lstrip("﻿")
    if not text.startswith("---"):
        return False, False
    block = []
    for line in text.splitlines()[1:]:
        if line.strip() in ("---", "..."):
            break
        block.append(line)
    frontmatter = "\n".join(block)
    aliases = memsearch._parse_frontmatter(frontmatter).get(memspec.ALIASES_FIELD, "").strip()
    return bool(DECISION_KEY_REGEX.search(frontmatter)), bool(aliases)


def vault_stats(vault):
    """卡總數／事件卡／決策卡／無別名卡：完整度四數字的前三個由這裡供數。"""
    cards = memsearch.card_files(vault)
    events = decisions = unaliased = 0
    for path in cards:
        if path.parent.name in EVENT_DIRECTORIES:
            events += 1
        has_key, has_aliases = _card_fields(path)
        decisions += bool(has_key)
        unaliased += not has_aliases
    return {"cards": len(cards), "events": events, "decisions": decisions, "unaliased": unaliased}


def doc_sentences(directory):
    """Yield (path, line number, sentence) for each owner-ruling line under directory."""
    root = Path(directory)
    try:
        paths = sorted(root.rglob("*"))
    except OSError:
        return
    for path in paths:
        relative = path.relative_to(root) if path != root else Path()
        if any(part.startswith((".", "_")) for part in relative.parts):
            continue  # .git and vault-private directories are not source documents
        if path.suffix.lower() not in DOC_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            sentence = line.strip()
            if sentence and DOC_RULING_REGEX.search(sentence):
                yield path, number, sentence


def load_vaults(config_path):
    value = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("config must be an object")
    found = []
    for item in value.get(memspec.CONFIG_VAULTS_FIELD) or ():
        if isinstance(item, str) and item.strip():
            vault = Path(item).expanduser().resolve()
            if vault.is_dir() and vault not in found:
                found.append(vault)
    if not found:
        raise ValueError("config lists no existing vault")
    return found


def governance_vault(vaults):
    """Same rule as the hooks (_hook_common.governance_vault): the ledger holder,
    else the first vault. Captured cards must land where the online ones do."""
    return next(
        (vault for vault in vaults if (vault / memspec.WORK_LEDGER_FILENAME).is_file()),
        vaults[0],
    )


def _manifest_path(vault):
    return vault.joinpath(*MANIFEST_SUBPATH)


def load_manifest(vault):
    try:
        value = json.loads(_manifest_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = value.get("files") if isinstance(value, dict) else None
    return files if isinstance(files, dict) else {}


def save_manifest(vault, files):
    path = _manifest_path(vault)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "files": files}, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        pass  # a manifest that cannot be written costs a re-scan, never a wrong card


def _fingerprint(path):
    info = path.stat()
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _write_draft(vault, source, sentence, counts, dry_run):
    """文件裡的裁定句只寫草稿（_drafts 不進索引），因為那是引用不是 owner 當場的話。"""
    digest = capture.grant_digest(sentence)
    directory = vault.joinpath(*DRAFT_SUBPATH)
    name = f"harvest-{time.strftime('%Y%m%d', time.gmtime())}-{digest}"
    relative = "/".join((*DRAFT_SUBPATH, f"{name}.md"))
    if memspec.CAPTURE_REJECT_REGEX.search(sentence):
        counts["rejected"] += 1
        return
    if any(directory.glob(f"harvest-*-{digest}.md")):
        counts["duplicates"] += 1
        return
    if dry_run:
        print(f"WOULD DRAFT {relative} {source}")
        counts["drafts"] += 1
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{name}.md"
        card = (
            "---\n"
            f"name: {name}\n"
            f"description: {capture.one_line(sentence)[: memspec.GRANT_MAX_CHARS]}\n"
            f"source: {source}\n"
            f"{memspec.DECISION_STATUS_FIELD}: draft\n"
            "---\n"
            f"{sentence}\n"
        )
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(card, encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        counts["rejected"] += 1
        return
    counts["drafts"] += 1


def harvest(home, vaults, docs=(), since=None, limit=None, dry_run=False):
    """Replay the online capture rules over every transcript and document.

    落點與線上捕捉同一份規則（capture_route）：一句話屬於哪個專案，卡就進那個專案
    的記憶庫；治理庫只收 cwd 不屬於任何已登記專案庫的話，並繼續持有 manifest、
    草稿與盤點數字。
    """
    vault = governance_vault(vaults)
    counts = {key: 0 for key in ("files", "utterances", "grants", "corrections", "rulings", "duplicates", "rejected", "drafts")}
    manifest = load_manifest(vault)
    seen = dict(manifest)
    landed = {}
    routes = {}
    now = _utc_stamp()

    for path in [*claude_transcripts(home), *codex_transcripts(home)]:
        if limit is not None and counts["files"] >= limit:
            break
        if since is not None and transcript_date(path) < since:
            continue
        key = os.fspath(path)
        try:
            fingerprint = _fingerprint(path)
        except OSError:
            continue
        if manifest.get(key) == fingerprint:
            continue  # unchanged since the last harvest
        counts["files"] += 1
        fallback = _utc_stamp(fingerprint["mtime_ns"] / 1_000_000_000)
        session_id = path.stem
        for number, text, question, item in utterances(path):
            counts["utterances"] += 1
            found = candidates(text, question)
            if not found:
                continue
            source = f"{key}:{number}"
            event = _record_event(item, session_id)
            # 落點按 cwd 算一次就快取：一場對話幾百句話算的是同一個答案，而每次算都要
            # 去問檔案系統「這個庫登記了嗎」。
            if event["cwd"] not in routes:
                routes[event["cwd"]] = capture_route.capture_vault(event["cwd"], vault, home)
            target = routes[event["cwd"]]
            for kind, directory, digest, label, body, summary in found:
                if memspec.CAPTURE_REJECT_REGEX.search(body):
                    counts["rejected"] += 1
                    continue
                # 一句話一張卡：治理庫也要問，否則 2026-09-06 之前落在治理庫的同一
                # 句話會在專案庫裡再長出一張。
                if any(
                    capture.existing_capture(item_vault, directory, kind, digest) is not None
                    for item_vault in dict.fromkeys((target, vault))
                ):
                    counts["duplicates"] += 1
                    continue
                if dry_run:
                    stamp = _record_stamp(item, fallback)
                    landing = "" if target == vault else f" -> {os.fspath(target)}"
                    admitted, _template = capture.auto_admitted(capture.owner_side(body, summary))
                    where = directory if admitted else "/".join(
                        (*memspec.CAPTURE_PENDING_SUBPATH, stamp[:10].replace("-", ""))
                    )
                    print(
                        f"{'WOULD WRITE' if admitted else 'WOULD PROPOSE'} "
                        f"{where}/{kind}-{stamp[:10].replace('-', '')}-{digest}.md "
                        f"{source}{landing}"
                    )
                    if admitted:
                        counts[f"{kind}s"] += 1
                        landed[target] = landed.get(target, 0) + 1
                    else:
                        counts["drafts"] += 1
                    continue
                replay = capture.Replay(
                    stamp=_record_stamp(item, fallback),
                    fields=(("source", source), ("harvested_at", now)),
                )
                # 白名單判定在 write_capture 裡（判 owner 自己那半），線上與回放共用
                # 同一處，回放出來的卡才會和今天線上寫的卡分在同一邊。
                capture.write_capture(
                    target, directory, kind, digest, label, body,
                    event, None, summary=summary, replay=replay,
                )
                if replay.status == capture.STATUS_WRITTEN:
                    counts[f"{kind}s"] += 1
                    landed[target] = landed.get(target, 0) + 1
                elif replay.status == capture.STATUS_PENDING:
                    counts["drafts"] += 1
                elif replay.status == capture.STATUS_DUPLICATE:
                    counts["duplicates"] += 1
                else:
                    counts["rejected"] += 1
        seen[key] = fingerprint

    for directory in docs:
        for path, number, sentence in doc_sentences(directory):
            _write_draft(vault, f"{path}:{number}", sentence, counts, dry_run)

    if not dry_run:
        save_manifest(vault, seen)
        # 新卡必須立刻可喚回；索引鎖被別人拿著就把索引標舊，讓下一個讀者重建。
        # 卡可能落在好幾個專案庫，每個寫過的庫都要重建自己的索引。
        for target in landed:
            if memsearch.build_index(target, lock_timeout=INDEX_LOCK_SECONDS).get("status") != "built":
                memsearch.mark_stale(target)
    routed = {os.fspath(target): count for target, count in landed.items() if target != vault}
    return counts, vault, routed


CARD_QUESTION_PREFIX = "問（助理）："
CARD_ANSWER_PREFIX = "答（owner 逐字）："
CARD_NAME_REGEX = re.compile(r"^name:.*$", re.MULTILINE)
CARD_LABEL_REGEX = re.compile(r"^(description:\s*)owner (?:grant|correction|ruling) auto-captured", re.MULTILINE)


def card_utterance(path):
    """(owner text, assistant question) recovered from one captured card.

    A ruling card stores the pair; a grant or correction card stores only the
    owner's sentence. Re-evaluating a card can therefore only ever be as strict as
    what the card kept — a grant card cannot be re-judged as an answer to a
    question nobody wrote down.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None, None
    body = text.partition("\n---\n")[2].strip() if text.startswith("---") else text.strip()
    if not body.startswith(CARD_QUESTION_PREFIX):
        return body, None
    asked, marker, answer = body.partition("\n" + CARD_ANSWER_PREFIX)
    if not marker:
        return body, None
    return answer.strip(), asked[len(CARD_QUESTION_PREFIX):].strip()


def _held_pending(path):
    """True for an unreviewed proposal under `_drafts/captured_pending/`.

    Every card in there is one today's rules still capture — that is exactly why
    it was held — so a forward replay would silently promote the whole pile back
    into the vault and undo the admission policy.
    """
    if memspec.CAPTURE_PENDING_SUBPATH[-1] not in path.parts:
        return False
    fields, _problem = memspec.frontmatter_fields(path)
    return capture.one_line(fields.get(memspec.VERIFIED_FIELD)).casefold() != memspec.VERIFIED_TRUE


def reevaluate(directory, vault, apply=False, quarantine_drops=None):
    """Re-judge cards with today's rules; print keep/drop per card. Two directions:

    Forward (quarantine_drops is None): `directory` is a flat pile of previously
    quarantined or drafted cards. Deleting is not this tool's call, so a dropped
    card simply stays where it is; a keeper moves under vault/<kind>/.

    Reverse (quarantine_drops is a directory): `directory` is a vault, scanned
    under its own grants/corrections/rulings. A keeper stays exactly where it is;
    a card today's rules would no longer capture moves OUT to
    quarantine_drops/<kind>/, so a live vault can be swept for capture drift
    without ever deleting a card.

    Either direction, --apply gates every move; without it this only prints what
    would happen. Publication refuses an occupied destination even during a race.

    A keeper's destination can already be occupied — an older bug once wrote
    the same owner sentence under two kinds ("一句兩卡"), or (reverse mode) a
    card is being reclassified into a kind another card already holds under
    that digest. `_move_card` resolves that itself (duplicate vs "-2" rename);
    see its docstring. Forward-mode successes count as `moved`; reverse-mode
    reclassifications (kind changed, card already lived in the vault) count
    separately as `reclassified` so a live-vault sweep can tell "swept out"
    from "renamed in place" apart.
    """
    lines = []
    counts = {
        "cards": 0, "keep": 0, "drop": 0, "moved": 0, "unreadable": 0,
        "duplicates": 0, "reclassified": 0, "held": 0,
    }
    if quarantine_drops is not None:
        scan = [(kind, Path(directory) / dirname) for kind, dirname in CARD_DIRECTORIES.items()]
    else:
        scan = [(None, Path(directory))]
    for kind_hint, scan_root in scan:
        for path in sorted(scan_root.rglob("*.md")):
            counts["cards"] += 1
            was = kind_hint or next((kind for kind in CARD_DIRECTORIES if path.name.startswith(kind + "-")), "?")
            owner, asked = card_utterance(path)
            if not owner:
                counts["unreadable"] += 1
                counts["drop"] += 1
                moved = _quarantine_drop(path, was, quarantine_drops, apply, counts)
                lines.append(f"DROP  {was:<10} {path} (no owner text in card){moved}")
                continue
            found = capture.classify(owner, asked)
            if found is None:
                counts["drop"] += 1
                _ok, reason = capture.candidate_shape(owner)
                moved = _quarantine_drop(path, was, quarantine_drops, apply, counts)
                lines.append(f"DROP  {was:<10} {path} ({reason}){moved}")
                continue
            kind, body, summary, digest_source = found
            counts["keep"] += 1
            moved = ""
            if quarantine_drops is None and apply and _held_pending(path):
                # 提案區的卡本來就「今天的規則也會捕捉」——那正是它被扣住的原因，不是
                # 搬正的理由。轉正是人看過、把 verified 改 true 的動作（owner 2026-09-09 Q5「C」）。
                counts["held"] += 1
                lines.append(f"HOLD  {was:<10} {path} ({memspec.CAPTURE_PENDING_HOLD_REASON})")
                continue
            if quarantine_drops is None and apply:
                outcome, target = _move_card(path, vault, kind, capture.grant_digest(digest_source), counts)
                if outcome == "duplicate":
                    lines.append(f"DUPLICATE {path} == {target}")
                elif outcome == "renamed":
                    lines.append(f"RENAMED {path} -> {target}")
                    moved = f" -> {target}"
                elif outcome == "moved":
                    moved = f" -> {target}"
            elif quarantine_drops is not None and apply and kind != was:
                outcome, target = _move_card(
                    path, vault, kind, capture.grant_digest(digest_source), counts, counter="reclassified",
                )
                if outcome == "duplicate":
                    lines.append(f"DUPLICATE {path} == {target}")
                elif outcome in ("moved", "renamed"):
                    lines.append(f"RECLASS {path} -> {target}")
            lines.append(f"KEEP  {was:<10} -> {kind:<10} {path}{moved}")
    lines.append(
        "REEVALUATE " + " ".join(f"{key}={value}" for key, value in counts.items())
    )
    return counts, lines


def _quarantine_drop(path, kind, quarantine_drops, apply, counts):
    """Reverse mode only: with --apply, move one dropped card to
    quarantine_drops/<kind>/ and return " -> <target>"; otherwise a no-op "".
    """
    if quarantine_drops is None or not apply:
        return ""
    target_dir = Path(quarantine_drops) / kind
    target = target_dir / path.name
    try:
        moved = card_io.move(path, target)
    except OSError as exc:
        counts["failed"] = counts.get("failed", 0) + 1
        return f" FAILED {exc}"
    if not moved:
        return ""
    counts["moved"] += 1
    return f" -> {target}"


def _card_summary(path):
    """Owner-sentence summary from a capture card's `description:` field, with
    the kind label and date stripped off — so a grant card and a correction
    card written for the same owner sentence compare equal regardless of which
    kind captured it first."""
    try:
        with Path(path).open("rb") as stream:
            head = stream.read(FRONTMATTER_SCAN_BYTES)
    except OSError:
        return ""
    text = head.decode("utf-8", errors="replace").lstrip("﻿")
    match = CARD_SUMMARY_REGEX.search(text)
    return match.group(1).strip() if match else ""


def _move_card(path, vault, kind, digest, counts, counter="moved"):
    """Move one passing card under vault/<kind>/, renaming it when the kind changed.

    The filename carries the kind and the digest, and `name:` has to match the
    filename or every lint that reads the card disagrees with the index.

    The destination can already be taken by a card with the same digest — same
    owner sentence, different kind (a "一句兩卡" leftover), or a reclassify
    landing on a kind another card already claimed. Compare the two cards'
    owner-sentence summaries before touching either file:
    - same sentence -> `path` is a duplicate; it moves intact (never deleted)
      to vault/_drafts/duplicates/<kind>/, `counts["duplicates"]` gets +1, and
      the caller prints `DUPLICATE <src> == <dst>`.
    - different sentence (digest coincidence) -> the destination name gets a
      "-2" suffix so both cards keep their own file.

    Returns (outcome, target) where outcome is "moved", "renamed", "duplicate",
    "unchanged", or None on a reported failure. Conflicts preserve both cards.
    """
    stamp = path.name.split("-")[1] if path.name.count("-") >= 2 else ""
    if not (len(stamp) == 8 and stamp.isdigit()):
        stamp = time.strftime("%Y%m%d", time.gmtime())
    name = f"{kind}-{stamp}-{digest}"
    target = Path(vault) / CARD_DIRECTORIES[kind] / f"{name}.md"
    if target.resolve() == path.resolve():
        return "unchanged", target
    outcome = "moved"
    if target.exists() and target.resolve() != path.resolve():
        if _card_summary(path) == _card_summary(target):
            existing = target  # the already-landed card `path` duplicates
            duplicate_dir = Path(vault).joinpath(*DUPLICATES_SUBPATH, kind)
            destination = duplicate_dir / path.name
            try:
                card_io.move(path, destination)
            except OSError as exc:
                counts["failed"] = counts.get("failed", 0) + 1
                print(f"MOVE FAILED {path}: {exc}", file=sys.stderr)
                return None, None
            counts["duplicates"] += 1
            # Report what `path` duplicates (the existing vault card), not where
            # it physically landed — the filesystem move is a bookkeeping detail,
            # the equivalence with `existing` is what the caller's message needs.
            return "duplicate", existing
        name = f"{name}-2"
        target = target.with_name(f"{name}.md")
        outcome = "renamed"
    try:
        original = path.read_bytes()
        text = original.decode("utf-8-sig")
        text = CARD_NAME_REGEX.sub(f"name: {name}", text, count=1)
        text = CARD_LABEL_REGEX.sub(rf"\g<1>{CARD_LABELS[kind]}", text, count=1)
        payload = (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
        card_io.move(path, target, payload, expected=original)
    except (OSError, UnicodeError) as exc:
        counts["failed"] = counts.get("failed", 0) + 1
        print(f"MOVE FAILED {path}: {exc}", file=sys.stderr)
        return None, None
    counts[counter] += 1
    return outcome, target


def render_inventory(home, vaults, docs=()):
    lines = []
    for label, paths in (("claude transcripts", claude_transcripts(home)), ("codex sessions", codex_transcripts(home))):
        dates = sorted(date for date in (transcript_date(path) for path in paths) if date)
        span = f"{dates[0]}..{dates[-1]}" if dates else "none"
        lines.append(f"{label}={len(paths)} range={span}")
    for vault in vaults:
        stats = vault_stats(vault)
        lines.append(
            f"vault {os.fspath(vault)} cards={stats['cards']} events={stats['events']} "
            f"decisions={stats['decisions']} unaliased={stats['unaliased']}"
        )
    for directory in docs:
        files = set()
        sentences = 0
        for path, _number, _sentence in doc_sentences(directory):
            files.add(path)
            sentences += 1
        lines.append(f"docs {os.fspath(Path(directory))} files={len(files)} sentences={sentences}")
    lines.append("INVENTORY OK")
    return lines


def _config_path(home, explicit_home):
    """--home wins for a relocated or synthetic home; otherwise the hooks' own env."""
    if not explicit_home:
        configured = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
        if configured:
            return Path(configured).expanduser()
    return Path(home) / ".epitype" / "config.json"


def _selftest():
    import calendar
    import contextlib
    import io
    import tempfile

    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-harvest-") as temp_dir:
            root = Path(temp_dir).resolve()
            home = root / "home"
            vault = home / "vault"
            (vault / "notes").mkdir(parents=True)
            (vault / memspec.WORK_LEDGER_FILENAME).write_text("# ledger\n", encoding="utf-8")
            (vault / "notes" / "plain.md").write_text(
                "---\nname: plain\ndescription: no aliases here\n---\nbody\n", encoding="utf-8"
            )
            (vault / "notes" / "decided.md").write_text(
                f"---\nname: decided\ndescription: a decision card\n{memspec.DECISION_KEY_FIELD}: topic-a\n"
                f"{memspec.ALIASES_FIELD}: [alpha, beta]\n---\nbody\n",
                encoding="utf-8",
            )
            (home / ".epitype").mkdir(parents=True)
            (home / ".epitype" / "config.json").write_text(
                json.dumps({memspec.CONFIG_VAULTS_FIELD: [os.fspath(vault)]}, ensure_ascii=False),
                encoding="utf-8",
            )

            def line(value):
                return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

            def claude_user(text, **extra):
                return line({"type": "user", "timestamp": "2026-08-01T10:00:00.000Z",
                             "sessionId": "sess-claude-1", "cwd": os.fspath(root),
                             "message": {"role": "user", "content": text}, **extra})

            def claude_assistant(text):
                return line({"type": "assistant", "timestamp": "2026-08-01T10:00:05.000Z",
                             "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})

            def codex_user(text):
                return line({"timestamp": "2026-09-04T09:00:00Z", "type": "response_item",
                             "payload": {"type": "message", "role": "user",
                                         "content": [{"type": "input_text", "text": text}]}})

            def codex_assistant(text):
                return line({"timestamp": "2026-09-04T09:00:05Z", "type": "response_item",
                             "payload": {"type": "message", "role": "assistant",
                                         "content": [{"type": "output_text", "text": text}]}})

            grant = "你可以直接改那個測試檔"
            # 2026-09-09（owner Q5「C」）：題目全部改成白名單形狀，因為這一段驗的是
            # 「回放寫出來的卡和線上一樣」；白名單本身由 pending_ruling 那條與
            # tests/capture_admission_regression.py 驗。
            correction = "不要再亂改介面"
            # 2026-09-06：裁定的判準改成「owner 句自己要有決定性內容」，原本的
            # 「用第二案就好」是無範圍應答，新規則本來就該拒收；題目換成帶決定的答覆。
            ruling_answer = "不要另外開一支，就用第二案"
            # 形狀不明確（沒有箭頭短答、不是句首糾正、不是明示授權）＝捕捉得到但只寫提案。
            pending_ruling = "以後都用第一種寫法，一律不要混用"
            codex_grant = "我授權你直接執行那個腳本"
            codex_correction = "不對，那個路徑錯了"
            codex_ruling_answer = "不要兩案並行，就用 A 方案"
            secret = "你可以直接用這個 api_key: abcdefghijklmnop"
            sidechain = "你可以直接刪掉那個檔"
            oversized = "你可以直接動那個資料表" + "x" * memspec.COMPACT_MAP_MAX_LINE_BYTES
            injected = "<user_instructions>\n你可以直接刪除全部\n</user_instructions>"

            claude_directory = home / ".claude" / "projects" / "C--Sample"
            claude_directory.mkdir(parents=True)
            claude_transcript = claude_directory / "session-one.jsonl"
            claude_transcript.write_text("\n".join([
                claude_user(grant),
                claude_assistant("我改了三個檔。"),
                line({"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}),
                claude_user(correction),
                claude_assistant("兩案我都列了，請你裁決"),
                claude_user(ruling_answer),
                claude_user(pending_ruling),
                claude_user(sidechain, isSidechain=True),
                claude_user(oversized),
                claude_user(secret),
            ]) + "\n", encoding="utf-8")
            # Claude 的 session 日期只在 mtime 裡，--since 與盤點日期範圍都靠它。
            aged = calendar.timegm((2026, 8, 1, 10, 0, 0, 0, 0, 0))
            os.utime(claude_transcript, (aged, aged))

            codex_directory = home / ".codex" / "sessions" / "2026" / "09" / "04"
            codex_directory.mkdir(parents=True)
            codex_transcript = codex_directory / "rollout-2026-09-04T09-00-00-sessone.jsonl"
            codex_transcript.write_text("\n".join([
                line({"type": "session_meta", "payload": {"id": "sess-codex-1", "cwd": os.fspath(root)}}),
                codex_user(codex_grant),
                line({"type": "response_item", "payload": {"type": "function_call", "name": "shell",
                                                           "arguments": "{}"}}),
                line({"type": "event_msg", "payload": {"type": "agent_message", "message": "略過"}}),
                codex_assistant("兩個方案都可行，請你裁決"),
                codex_user(codex_ruling_answer),
                codex_user(codex_correction),
                codex_user(injected),
            ]) + "\n", encoding="utf-8")

            docs = root / "docs"
            (docs / "sub").mkdir(parents=True)
            (docs / "one.md").write_text(
                "前言一句。\n2026-08-01 owner 裁定：測試一律同步跑完並貼出輸出。\n不含關鍵字的一行。\n",
                encoding="utf-8",
            )
            (docs / "sub" / "two.md").write_text(
                "owner：「驗收不交給實作者」\n", encoding="utf-8"
            )
            (docs / "sub" / "skip.log").write_text("owner 裁定：這個副檔名不掃。\n", encoding="utf-8")

            def run(arguments):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = main([*arguments, "--home", os.fspath(home)])
                return code, buffer.getvalue().splitlines()

            def summary(lines, prefix="HARVEST "):
                for text in lines:
                    if text.startswith(prefix):
                        return dict(
                            (key, int(value)) for key, value in
                            (item.split("=", 1) for item in text[len(prefix):].split())
                        )
                return {}

            base = ["--docs", os.fspath(docs)]
            dry_code, dry_lines = run([*base, "--dry-run"])
            dry = summary(dry_lines)
            checks.append((
                "dry run plans every card and writes nothing",
                dry_code == 0
                and dry == {"files": 2, "utterances": 8, "grants": 2, "corrections": 2, "rulings": 2,
                            "duplicates": 0, "rejected": 1, "drafts": 3}
                and sum(1 for text in dry_lines if text.startswith("WOULD WRITE")) == 6
                and sum(1 for text in dry_lines if text.startswith("WOULD PROPOSE")) == 1
                and sum(1 for text in dry_lines if text.startswith("WOULD DRAFT")) == 2
                and not (vault / memspec.GRANT_DIRECTORY).exists()
                and not _manifest_path(vault).exists(),
            ))

            first_code, first_lines = run(base)
            first = summary(first_lines)
            checks.append((
                "first harvest writes one card per owner sentence, both hosts",
                first_code == 0 and first == dry,
            ))

            grants = sorted((vault / memspec.GRANT_DIRECTORY).glob("*.md"))
            corrections = sorted((vault / memspec.CORRECTION_DIRECTORY).glob("*.md"))
            rulings = sorted((vault / memspec.RULING_DIRECTORY).glob("*.md"))
            bodies = {path.name: path.read_text(encoding="utf-8") for path in [*grants, *corrections, *rulings]}
            checks.append((
                "card kinds and owner sentences are the captured ones",
                len(grants) == 2 and len(corrections) == 2 and len(rulings) == 2
                and any(grant in text for text in bodies.values())
                and any(codex_grant in text for text in bodies.values())
                and any(correction in text for text in bodies.values())
                and any(codex_correction in text for text in bodies.values())
                and any("請你裁決" in text and ruling_answer in text for text in bodies.values())
                and any("請你裁決" in text and codex_ruling_answer in text for text in bodies.values()),
            ))

            # owner 2026-09-09 Q5「C」：形狀不明確的那句照樣判得出 kind，但只寫提案；
            # 提案在 `_drafts/` 底下（memsearch 排除 `_` 路徑段），所以它不進索引。
            proposals = sorted(vault.joinpath(*memspec.CAPTURE_PENDING_SUBPATH).rglob("*.md"))
            proposal_text = "\n".join(path.read_text(encoding="utf-8") for path in proposals)
            checks.append((
                "a captured sentence outside the whitelist is proposed, not filed",
                len(proposals) == 1
                and proposals[0].name.startswith("ruling-")
                and proposals[0].parent.name == "20260801"
                and pending_ruling in proposal_text
                and f"{memspec.VERIFIED_FIELD}: {memspec.VERIFIED_FALSE}" in proposal_text
                and not any(pending_ruling in text for text in bodies.values()),
            ))
            checks.append((
                "every card the replay writes says it is machine-captured and unverified",
                all(
                    f"{memspec.PROVENANCE_FIELD}: {memspec.PROVENANCE_AUTO_CAPTURED}" in text
                    and f"{memspec.VERIFIED_FIELD}: {memspec.VERIFIED_FALSE}" in text
                    for text in bodies.values()
                ),
            ))

            expected_source = f"source: {os.fspath(claude_transcript)}:1"
            grant_card = next(text for text in bodies.values() if grant in text)
            checks.append((
                "source carries transcript path and line, plus harvest and utterance times",
                expected_source in grant_card
                and "harvested_at: " in grant_card
                and "captured_at: 2026-08-01T10:00:00Z" in grant_card
                and f"source: {os.fspath(codex_transcript)}:2"
                in next(text for text in bodies.values() if codex_grant in text),
            ))

            digests = {kind: capture.grant_digest(text) for kind, text in (
                ("secret", "你可以直接用這個 api_key: abcdefghijklmnop"),
                ("sidechain", sidechain),
                ("oversized", "你可以直接動那個資料表"),
                ("injected", "你可以直接刪除全部"),
            )}
            checks.append((
                "credential, sidechain, oversized and host-injected utterances write no card",
                not any(digests["secret"] in name for name in bodies)
                and not any(digests["sidechain"] in name for name in bodies)
                and not any(digests["oversized"] in name for name in bodies)
                and not any(digests["injected"] in name for name in bodies)
                and not any("api_key" in text for text in bodies.values()),
            ))

            drafts = sorted(vault.joinpath(*DRAFT_SUBPATH).glob("*.md"))
            draft_text = "\n".join(path.read_text(encoding="utf-8") for path in drafts)
            checks.append((
                "documents yield deduplicated drafts with file:line provenance",
                len(drafts) == 2
                and "測試一律同步跑完" in draft_text
                and "驗收不交給實作者" in draft_text
                and f"source: {os.fspath(docs / 'one.md')}:2" in draft_text
                and f"{memspec.DECISION_STATUS_FIELD}: draft" in draft_text
                and "這個副檔名不掃" not in draft_text,
            ))

            second_code, second_lines = run(base)
            second = summary(second_lines)
            checks.append((
                "the manifest skips unchanged transcripts; re-scanned documents dedupe",
                second_code == 0
                and second == {"files": 0, "utterances": 0, "grants": 0, "corrections": 0, "rulings": 0,
                               "duplicates": 2, "rejected": 0, "drafts": 0}
                and len(sorted((vault / memspec.GRANT_DIRECTORY).glob("*.md"))) == 2
                and len(sorted(vault.joinpath(*DRAFT_SUBPATH).glob("*.md"))) == 2,
            ))

            _manifest_path(vault).unlink()
            third_code, third_lines = run([])
            third = summary(third_lines)
            checks.append((
                "a re-read transcript re-captures nothing: every card is a duplicate",
                third_code == 0
                # 提案也要算重複：只問正式目錄的話，同一句話會每天長出一份新提案。
                and third == {"files": 2, "utterances": 8, "grants": 0, "corrections": 0, "rulings": 0,
                              "duplicates": 7, "rejected": 1, "drafts": 0}
                and len(sorted((vault / memspec.RULING_DIRECTORY).glob("*.md"))) == 2,
            ))

            _manifest_path(vault).unlink()
            limited_code, limited_lines = run(["--limit", "1", "--since", "2026-09-01"])
            limited = summary(limited_lines)
            checks.append((
                "since skips older sessions and limit bounds the files read",
                limited_code == 0 and limited["files"] == 1 and limited["utterances"] == 3,
            ))

            inventory_code, inventory_lines = run(["--inventory", "--docs", os.fspath(docs)])
            stats = vault_stats(vault)
            checks.append((
                "inventory counts transcripts, cards and document sentences without writing",
                inventory_code == 0
                and inventory_lines[-1] == "INVENTORY OK"
                and inventory_lines[0] == "claude transcripts=1 range=2026-08-01..2026-08-01"
                and "codex sessions=1 range=2026-09-04..2026-09-04" in inventory_lines[1]
                and f"cards={stats['cards']} events=6 decisions=1 unaliased=7" in inventory_lines[2]
                and inventory_lines[3].endswith("files=2 sentences=2"),
            ))

            # --reevaluate：舊判定寫下的卡用今天的規則重判；不通過的留在原地（不刪），
            # 通過的搬回 <vault>/<kind>/ 並依新 kind 改名，否則檔名與 name: 會對不上。
            quarantine = root / "quarantine"
            quarantine.mkdir()
            (quarantine / "ruling-20260801-deadbeef0001.md").write_text(
                "---\nname: ruling-20260801-deadbeef0001\n"
                "description: owner ruling auto-captured 2026-08-01: 好\n---\n"
                f"{CARD_QUESTION_PREFIX}兩案我都列了，請你裁決\n{CARD_ANSWER_PREFIX}好\n",
                encoding="utf-8",
            )
            stale_correction = "以後都用第二案，不要另外開一支"
            (quarantine / "ruling-20260801-deadbeef0002.md").write_text(
                "---\nname: ruling-20260801-deadbeef0002\n"
                f"description: owner ruling auto-captured 2026-08-01: {stale_correction}\n---\n"
                f"{stale_correction}\n",
                encoding="utf-8",
            )
            reeval_counts, reeval_lines = reevaluate(quarantine, vault, apply=True)
            moved_card = vault / memspec.RULING_DIRECTORY / f"ruling-20260801-{capture.grant_digest(stale_correction)}.md"
            checks.append((
                "reevaluate keeps the decisive card, leaves the empty answer where it is",
                reeval_counts == {"cards": 2, "keep": 1, "drop": 1, "moved": 1, "unreadable": 0,
                                  "duplicates": 0, "reclassified": 0, "held": 0}
                and reeval_lines[-1].startswith("REEVALUATE ")
                and (quarantine / "ruling-20260801-deadbeef0001.md").exists()
                and not (quarantine / "ruling-20260801-deadbeef0002.md").exists()
                and moved_card.exists()
                and f"name: {moved_card.stem}" in moved_card.read_text(encoding="utf-8"),
            ))
            for path in sorted((vault / memspec.RULING_DIRECTORY).glob("ruling-20260801-*.md")):
                path.unlink()

            # 提案區的卡「今天的規則也會捕捉」正是它被扣住的原因，所以 forward-mode
            # --apply 不得把它搬進庫：那等於用機器判定取代人核（owner 2026-09-09 Q5「C」）。
            pending_root = vault.joinpath(*memspec.CAPTURE_PENDING_SUBPATH)
            hold_counts, hold_lines = reevaluate(pending_root, vault, apply=True)
            checks.append((
                "an unverified proposal is held, never promoted by a replay",
                hold_counts["held"] == 1
                and hold_counts["moved"] == 0
                and any(text.startswith("HOLD") for text in hold_lines)
                and len(sorted(pending_root.rglob("*.md"))) == 1
                and not sorted((vault / memspec.RULING_DIRECTORY).glob("ruling-20260801-*.md")),
            ))

            # --quarantine-drops (reverse mode): scan the vault's own
            # grants/corrections/rulings instead of a quarantine pile; a card that
            # still passes stays exactly where it is, one that no longer passes
            # moves OUT to quarantine_drops/<kind>/ — apply-gated, never deleted.
            quarantine_target = root / "quarantine_drops"
            # The earlier forward-mode cleanup (two lines up) unlinks every
            # `ruling-20260801-*.md`, which also removes the original claude-dated
            # ruling card (same date prefix as its digest); one codex-dated ruling
            # (2026-09-04) remains, so the vault holds 2 grants + 2 corrections + 1
            # ruling before the stale card below is added.
            ruling_count_before = len(sorted((vault / memspec.RULING_DIRECTORY).glob("*.md")))
            stale_ruling = vault / memspec.RULING_DIRECTORY / "ruling-20260801-deadbeef0003.md"
            stale_ruling.write_text(
                "---\nname: ruling-20260801-deadbeef0003\n"
                "description: owner ruling auto-captured 2026-08-01: 好\n---\n"
                f"{CARD_QUESTION_PREFIX}兩案我都列了，請你裁決\n{CARD_ANSWER_PREFIX}好\n",
                encoding="utf-8",
            )
            dry_reeval_counts, dry_reeval_lines = reevaluate(vault, vault, apply=False, quarantine_drops=quarantine_target)
            expected_total = 4 + ruling_count_before + 1
            checks.append((
                "--quarantine-drops dry-run reports the drop but moves nothing",
                dry_reeval_counts == {"cards": expected_total, "keep": expected_total - 1, "drop": 1, "moved": 0, "unreadable": 0,
                                      "duplicates": 0, "reclassified": 0, "held": 0}
                and any(text.startswith("DROP") and "ruling-20260801-deadbeef0003.md" in text for text in dry_reeval_lines)
                and stale_ruling.exists()
                and not quarantine_target.exists(),
            ))
            apply_reeval_counts, _apply_reeval_lines = reevaluate(vault, vault, apply=True, quarantine_drops=quarantine_target)
            quarantined_card = quarantine_target / "ruling" / "ruling-20260801-deadbeef0003.md"
            checks.append((
                "--quarantine-drops apply moves the drop to <dir>/<kind>/ and leaves keeps in place",
                apply_reeval_counts == {"cards": expected_total, "keep": expected_total - 1, "drop": 1, "moved": 1, "unreadable": 0,
                                        "duplicates": 0, "reclassified": 0, "held": 0}
                and not stale_ruling.exists()
                and quarantined_card.exists()
                and len(sorted((vault / memspec.GRANT_DIRECTORY).glob("*.md"))) == 2
                and len(sorted((vault / memspec.CORRECTION_DIRECTORY).glob("*.md"))) == 2
                and len(sorted((vault / memspec.RULING_DIRECTORY).glob("*.md"))) == ruling_count_before,
            ))

            # 撞名（一句兩卡）：舊 bug 把同一句話存成兩個 kind；forward-mode --apply
            # 把一張改判為別的 kind 時，目的地已經有真卡。同句 -> 進 duplicates（不刪）。
            dup_pile = root / "dup_pile"
            dup_pile.mkdir()
            dup_source = dup_pile / "grant-20260801-placeholder002.md"
            dup_source.write_text(
                f"---\nname: grant-20260801-placeholder002\n"
                f"description: owner grant auto-captured 2026-08-01: {correction}\n---\n{correction}\n",
                encoding="utf-8",
            )
            existing_correction_card = next(
                (vault / memspec.CORRECTION_DIRECTORY).glob(f"correction-*-{capture.grant_digest(correction)}.md")
            )
            dup_reeval_counts, dup_reeval_lines = reevaluate(dup_pile, vault, apply=True)
            duplicate_landing = vault.joinpath(*DUPLICATES_SUBPATH, "correction", dup_source.name)
            checks.append((
                "reevaluate --apply files a same-sentence collision as a duplicate, not a clobber",
                dup_reeval_counts == {"cards": 1, "keep": 1, "drop": 0, "moved": 0, "unreadable": 0,
                                      "duplicates": 1, "reclassified": 0, "held": 0}
                and any(
                    text.startswith("DUPLICATE ") and os.fspath(dup_source) in text
                    and os.fspath(existing_correction_card) in text
                    for text in dup_reeval_lines
                )
                and not dup_source.exists()
                and duplicate_landing.exists()
                and existing_correction_card.exists(),
            ))

            # 撞名但不同句（digest 巧合）：目的地保留原檔，來源改名 -2 落地，兩張都留著。
            rename_pile = root / "rename_pile"
            rename_pile.mkdir()
            rename_source_sentence = "你不要再改那個顏色設定了"
            rename_digest = capture.grant_digest(rename_source_sentence)
            rename_source = rename_pile / "grant-20260801-placeholder003.md"
            rename_source.write_text(
                f"---\nname: grant-20260801-placeholder003\n"
                f"description: owner grant auto-captured 2026-08-01: {rename_source_sentence}\n---\n"
                f"{rename_source_sentence}\n",
                encoding="utf-8",
            )
            collision_target = vault / memspec.CORRECTION_DIRECTORY / f"correction-20260801-{rename_digest}.md"
            collision_target.write_text(
                f"---\nname: correction-20260801-{rename_digest}\n"
                "description: owner correction auto-captured 2026-08-01: 完全不同的另一句話\n---\n"
                "完全不同的另一句話\n",
                encoding="utf-8",
            )
            rename_reeval_counts, rename_reeval_lines = reevaluate(rename_pile, vault, apply=True)
            renamed_target = vault / memspec.CORRECTION_DIRECTORY / f"correction-20260801-{rename_digest}-2.md"
            checks.append((
                "reevaluate --apply resolves a same-digest, different-sentence collision with a -2 rename",
                rename_reeval_counts == {"cards": 1, "keep": 1, "drop": 0, "moved": 1, "unreadable": 0,
                                         "duplicates": 0, "reclassified": 0, "held": 0}
                and any(
                    text.startswith("RENAMED ") and os.fspath(rename_source) in text and os.fspath(renamed_target) in text
                    for text in rename_reeval_lines
                )
                and not rename_source.exists()
                and renamed_target.exists()
                and f"name: {renamed_target.stem}" in renamed_target.read_text(encoding="utf-8")
                and collision_target.exists(),
            ))

            # --quarantine-drops + --apply：一張 KEEP 但今天規則會改判 kind 的卡，
            # 之前只印訊息不搬；dry-run 仍不搬，--apply 才真的搬到新 kind 目錄。
            reclass_sentence = "你不要再放大那個字級了"
            reclass_digest = capture.grant_digest(reclass_sentence)
            misfiled_grant = vault / memspec.GRANT_DIRECTORY / f"grant-20260801-{reclass_digest}.md"
            misfiled_grant.write_text(
                f"---\nname: grant-20260801-{reclass_digest}\n"
                f"description: owner grant auto-captured 2026-08-01: {reclass_sentence}\n---\n{reclass_sentence}\n",
                encoding="utf-8",
            )
            reclassed_target = vault / memspec.CORRECTION_DIRECTORY / f"correction-20260801-{reclass_digest}.md"
            quarantine_reclass = root / "quarantine_drops_reclass"
            dry_reclass_counts, dry_reclass_lines = reevaluate(
                vault, vault, apply=False, quarantine_drops=quarantine_reclass
            )
            checks.append((
                "--quarantine-drops dry-run reports no reclass moves for a KEEP-but-reclassified card",
                dry_reclass_counts["reclassified"] == 0
                and misfiled_grant.exists()
                and not reclassed_target.exists()
                and not any(text.startswith("RECLASS ") for text in dry_reclass_lines),
            ))
            apply_reclass_counts, apply_reclass_lines = reevaluate(
                vault, vault, apply=True, quarantine_drops=quarantine_reclass
            )
            checks.append((
                "--quarantine-drops --apply actually moves a KEEP-but-reclassified card to its new kind directory",
                apply_reclass_counts["reclassified"] == 1
                and not misfiled_grant.exists()
                and reclassed_target.exists()
                and f"name: {reclassed_target.stem}" in reclassed_target.read_text(encoding="utf-8")
                and any(
                    text.startswith("RECLASS ") and os.fspath(misfiled_grant) in text and os.fspath(reclassed_target) in text
                    for text in apply_reclass_lines
                ),
            ))

            # 落點（U65）：一句話屬於哪個專案，回放出來的卡就進那個專案的記憶庫。
            # 自帶一組獨立 fixture，才不會動到上面每一條對治理庫張數的斷言。
            route_home = root / "route-home"
            route_vault = (root / "route-vault").resolve()
            route_vault.mkdir()
            (route_vault / memspec.WORK_LEDGER_FILENAME).write_text("# ledger\n", encoding="utf-8")
            route_project = root / "route-work" / "proj"
            route_project.mkdir(parents=True)
            route_native = (
                route_home / ".claude" / "projects"
                / capture_route.project_slug(route_project) / "memory"
            )
            route_native.mkdir(parents=True)
            (route_native / "seed.md").write_text(
                "---\nname: seed\ndescription: a registered project vault\n---\nbody\n",
                encoding="utf-8",
            )
            route_directory = route_home / ".claude" / "projects" / "C--Route"
            route_directory.mkdir(parents=True)
            route_sentence = "你可以直接改那個路由設定檔"
            (route_directory / "route-session.jsonl").write_text(
                line({"type": "user", "timestamp": "2026-08-02T10:00:00.000Z",
                      "sessionId": "sess-route-1", "cwd": os.fspath(route_project),
                      "message": {"role": "user", "content": route_sentence}}) + "\n",
                encoding="utf-8",
            )
            route_counts, route_governance, route_routed = harvest(route_home, [route_vault])
            route_cards = sorted((route_native / memspec.GRANT_DIRECTORY).glob("*.md"))
            checks.append((
                "an offline replay files the card in the cwd's project vault, not the governance vault",
                route_counts["grants"] == 1
                and len(route_cards) == 1
                and route_sentence in route_cards[0].read_text(encoding="utf-8")
                and not (route_vault / memspec.GRANT_DIRECTORY).exists()
                and route_routed == {os.fspath(route_native.resolve()): 1}
                and route_governance == route_vault,
            ))

            # Codex 的 cwd 只在開場那一行，補上之後回放的卡才有來源專案可判。
            codex_route_directory = route_home / ".codex" / "sessions" / "2026" / "09" / "05"
            codex_route_directory.mkdir(parents=True)
            codex_route_sentence = "你可以直接改那個回放設定檔"
            (codex_route_directory / "rollout-2026-09-05T09-00-00-routeone.jsonl").write_text(
                "\n".join([
                    line({"type": "session_meta",
                          "payload": {"id": "sess-codex-route", "cwd": os.fspath(route_project)}}),
                    line({"timestamp": "2026-09-05T09:00:00Z", "type": "response_item",
                          "payload": {"type": "message", "role": "user",
                                      "content": [{"type": "input_text", "text": codex_route_sentence}]}}),
                ]) + "\n",
                encoding="utf-8",
            )
            codex_counts, _codex_governance, codex_routed = harvest(route_home, [route_vault])
            codex_card = next(
                (path for path in sorted((route_native / memspec.GRANT_DIRECTORY).glob("*.md"))
                 if codex_route_sentence in path.read_text(encoding="utf-8")),
                None,
            )
            checks.append((
                "a Codex session's cwd comes from session_meta, so its card routes and records it",
                codex_counts["grants"] == 1
                and codex_card is not None
                and f"{memspec.CWD_FIELD}: {os.fspath(route_project)}" in codex_card.read_text(encoding="utf-8")
                and codex_routed == {os.fspath(route_native.resolve()): 1}
                and not (route_vault / memspec.GRANT_DIRECTORY).exists(),
            ))

            completeness = next(text for text in first_lines if text.startswith("COMPLETENESS "))
            checks.append((
                "the harvest reports the first three completeness numbers",
                completeness == "COMPLETENESS events=6 decisions=1 unaliased=7/8",
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 23
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _parser():
    parser = argparse.ArgumentParser(prog="epitype harvest", description=__doc__)
    parser.add_argument("--home", type=Path, default=None, help="home directory holding .claude/.codex/.epitype")
    parser.add_argument("--inventory", action="store_true", help="count sources only; write nothing")
    parser.add_argument("--docs", type=Path, action="append", default=[], help="directory of documents to mine for owner rulings")
    parser.add_argument("--since", type=_date_argument, default=None, help="skip sessions dated before YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=None, help="at most N transcripts this run")
    parser.add_argument("--dry-run", action="store_true", help="print what would be written")
    parser.add_argument("--reevaluate", type=Path, default=None, help="re-judge the cards under this directory with today's rules")
    parser.add_argument("--apply", action="store_true", help="with --reevaluate: move passing cards back under the vault")
    parser.add_argument(
        "--quarantine-drops", nargs="?", const="", default=None, metavar="DIR",
        help="reverse --reevaluate: treat the --reevaluate argument as a vault and re-judge its own "
             "grants/corrections/rulings; cards that no longer pass move to DIR/<kind>/ (default "
             "<vault>/_drafts/captured_dropped/<kind>/), passing cards stay put",
    )
    parser.add_argument("--selftest", action="store_true", help="run synthetic checks")
    return parser


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in arguments:
        return _selftest()
    options = _parser().parse_args(arguments)
    home = options.home if options.home is not None else Path.home()
    try:
        vaults = load_vaults(_config_path(home, options.home is not None))
    except (OSError, ValueError) as exc:
        print(f"HARVEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if options.inventory:
        for text in render_inventory(home, vaults, options.docs):
            print(text)
        return 0

    if options.reevaluate is not None:
        quarantine_drops = None
        if options.quarantine_drops is not None:
            quarantine_drops = Path(options.quarantine_drops) if options.quarantine_drops else (
                options.reevaluate / "_drafts" / "captured_dropped"
            )
        counts, lines = reevaluate(
            options.reevaluate, governance_vault(vaults), apply=options.apply, quarantine_drops=quarantine_drops
        )
        for text in lines:
            print(text)
        return 1 if counts.get("failed") else 0

    counts, vault, routed = harvest(
        home, vaults, docs=options.docs, since=options.since, limit=options.limit, dry_run=options.dry_run
    )
    print("HARVEST " + " ".join(f"{key}={value}" for key, value in counts.items()))
    # 治理庫之外還有別的庫收到卡，說出來；只印 HARVEST 那行會看不見它們。
    for target, count in sorted(routed.items(), key=lambda pair: (-pair[1], pair[0])):
        print(f"ROUTED cards={count} -> {target}")
    stats = vault_stats(vault)
    print(f"COMPLETENESS events={stats['events']} decisions={stats['decisions']} "
          f"unaliased={stats['unaliased']}/{stats['cards']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
