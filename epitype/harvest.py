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
    from . import capture, memsearch, memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import capture
    import memsearch
    import memspec


CLAUDE_TRANSCRIPT_GLOB = ("projects", "*", "*.jsonl")
CODEX_TRANSCRIPT_GLOB = ("sessions", "*", "*", "*", "rollout-*.jsonl")
MANIFEST_SUBPATH = (".epitype", "harvest_manifest.json")
DRAFT_SUBPATH = ("_drafts", "decisions")
EVENT_DIRECTORIES = (memspec.GRANT_DIRECTORY, memspec.CORRECTION_DIRECTORY, memspec.RULING_DIRECTORY)
DOC_SUFFIXES = (".md", ".markdown", ".txt")
# 粗篩只認角色字串本身，不認 '"type":"user"' 整段：不同寫入端的分隔符空白不一，
# 認整段會讓真 transcript 過篩、合成 transcript 落篩（或反過來）。
ROLE_MARKERS = (b'"user"', b'"assistant"')
# 文件裡的 owner 裁定句：只挖明確標記 owner 裁定或 owner 原話引號的行，其餘留給人。
DOC_RULING_REGEX = re.compile(r"owner\s*(?:已)?裁(?:定|示|決)|owner[:：]\s*[「\"]", re.IGNORECASE)
DECISION_KEY_REGEX = re.compile(rf"^{re.escape(memspec.DECISION_KEY_FIELD)}:\s*(\S.*)$", re.MULTILINE)
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
    try:
        stream = Path(path).open("rb")
    except OSError:
        return
    with stream:
        for number, raw in enumerate(stream, 1):
            raw = raw.rstrip(b"\r\n")
            if len(raw) > memspec.COMPACT_MAP_MAX_LINE_BYTES:
                continue  # a 2MB line is a pasted payload, not a sentence
            if not any(marker in raw for marker in ROLE_MARKERS):
                continue
            try:
                item = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue  # a truncated or half-written line ends nothing
            if not isinstance(item, dict):
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
                yield number, owner, question, item


def candidates(text, question):
    """Every card this utterance would produce, as (kind, directory, digest, label,
    body, summary). One decision path for online and replay: the triggers, the
    sentence split, and the digest all come from epitype.capture."""
    found = []
    for kind, (directory, trigger, label) in capture.CAPTURE_KINDS.items():
        shaped, _reason = capture.is_owner_utterance(text, trigger)
        if not shaped:
            continue
        sentence = capture.matched_sentence(text, trigger)
        if sentence is None:
            continue
        found.append((kind, directory, capture.grant_digest(sentence), label, sentence, None))
    ruling = capture.ruling_body(text, question)
    if ruling is not None:
        body, summary = ruling
        found.append((
            "ruling", memspec.RULING_DIRECTORY, capture.grant_digest(text),
            "owner ruling auto-captured", body, summary,
        ))
    return found


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
    """Replay the online capture rules over every transcript and document."""
    vault = governance_vault(vaults)
    counts = {key: 0 for key in ("files", "utterances", "grants", "corrections", "rulings", "duplicates", "rejected", "drafts")}
    manifest = load_manifest(vault)
    seen = dict(manifest)
    written = 0
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
            source = f"{key}:{number}"
            for kind, directory, digest, label, body, summary in candidates(text, question):
                if memspec.CAPTURE_REJECT_REGEX.search(body):
                    counts["rejected"] += 1
                    continue
                if capture.existing_capture(vault, directory, kind, digest) is not None:
                    counts["duplicates"] += 1
                    continue
                if dry_run:
                    stamp = _record_stamp(item, fallback)
                    print(f"WOULD WRITE {directory}/{kind}-{stamp[:10].replace('-', '')}-{digest}.md {source}")
                    counts[f"{kind}s"] += 1
                    continue
                replay = capture.Replay(
                    stamp=_record_stamp(item, fallback),
                    fields=(("source", source), ("harvested_at", now)),
                )
                capture.write_capture(
                    vault, directory, kind, digest, label, body,
                    _record_event(item, session_id), None, summary=summary, replay=replay,
                )
                if replay.status == capture.STATUS_WRITTEN:
                    counts[f"{kind}s"] += 1
                    written += 1
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
        if written and memsearch.build_index(vault, lock_timeout=INDEX_LOCK_SECONDS).get("status") != "built":
            memsearch.mark_stale(vault)
    return counts, vault


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
            correction = "我不是說過不要亂改介面"
            ruling_answer = "用第二案就好"
            codex_grant = "我授權你直接執行那個腳本"
            codex_correction = "不是這樣，那個路徑錯了"
            codex_ruling_answer = "選 A 方案"
            secret = "你可以直接用這個 api_key: abcdef1234567890"
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
                and dry == {"files": 2, "utterances": 7, "grants": 2, "corrections": 2, "rulings": 2,
                            "duplicates": 0, "rejected": 1, "drafts": 2}
                and sum(1 for text in dry_lines if text.startswith("WOULD WRITE")) == 6
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
                ("secret", "你可以直接用這個 api_key: abcdef1234567890"),
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
                and third == {"files": 2, "utterances": 7, "grants": 0, "corrections": 0, "rulings": 0,
                              "duplicates": 6, "rejected": 1, "drafts": 0}
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

            completeness = next(text for text in first_lines if text.startswith("COMPLETENESS "))
            checks.append((
                "the harvest reports the first three completeness numbers",
                completeness == "COMPLETENESS events=6 decisions=1 unaliased=7/8",
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 11
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

    counts, vault = harvest(
        home, vaults, docs=options.docs, since=options.since, limit=options.limit, dry_run=options.dry_run
    )
    print("HARVEST " + " ".join(f"{key}={value}" for key, value in counts.items()))
    stats = vault_stats(vault)
    print(f"COMPLETENESS events={stats['events']} decisions={stats['decisions']} "
          f"unaliased={stats['unaliased']}/{stats['cards']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
