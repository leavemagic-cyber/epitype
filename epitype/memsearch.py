import sys; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，避免繁中輸出在程式進入點就中斷。
"""Epitype 記憶卡的本機 FTS5 全文與別名搜尋器。"""

import argparse
import csv
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

try:
    from . import memspec
except ImportError:  # Direct script execution keeps the U1 CLI contract.
    import memspec


# 2026-09-01 實測事故：雙語卡別名未進索引，導致同義詞檢索三連空手；規則：
# name/description/aliases/scope 必須和本文一起成為可搜尋欄，且別名欄名只認
# memspec 正本。
_SEARCH_FRONT_FIELDS = ("name", "description", memspec.ALIASES_FIELD, memspec.SCOPE_FIELD)
_FRONT_FIELDS = _SEARCH_FRONT_FIELDS + (
    memspec.DECISION_STATUS_FIELD,
    memspec.SUPERSEDED_BY_FIELD,
)
_ALL_FIELDS = _SEARCH_FRONT_FIELDS + ("body",)
_DB_FIELDS = {
    "name": "name",
    "description": "description",
    memspec.ALIASES_FIELD: "fm_aliases",
    memspec.SCOPE_FIELD: "fm_scope",
    memspec.DECISION_STATUS_FIELD: memspec.DECISION_STATUS_FIELD,
    memspec.SUPERSEDED_BY_FIELD: memspec.SUPERSEDED_BY_FIELD,
    "body": "body",
}
_CJK_RANGE = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f"
_CJK_RUN = re.compile(f"[{_CJK_RANGE}]+")
_RECALL_PART = re.compile(f"[{_CJK_RANGE}]+|[^\\s{_CJK_RANGE}]+")
# Trigram FTS cannot match two-codepoint terms. A private-use prefix makes CJK
# bigrams indexable while the cards table and returned hit fields stay raw.
_CJK_BIGRAM_PREFIX = "\ue000"
_FTS_FORMAT_KEY = "fts_format"
_FTS_FORMAT_VERSION = "5"
_CURRENT_DECISION_GUIDANCE = "此題現行決定="


def _db_path(vault):
    return vault / memspec.FTS_DB_PATH


def _legacy_db_path(vault):
    return vault / memspec.FTS_LEGACY_DB_PATH


def _resolve_vault(vault):
    resolved = Path(vault).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"vault path does not exist: {resolved}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"vault path is not a directory: {resolved}")
    return resolved


def _read_connection(db_path):
    return sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=5.0,
    )


def _index_warnings(vault):
    current_directory = _db_path(vault).parent
    legacy_directory = _legacy_db_path(vault).parent
    if current_directory.exists() and legacy_directory.exists():
        return [
            f"Epitype used {current_directory}; legacy index directory "
            f"{legacy_directory} can be deleted after verification."
        ]
    return []


def _migrate_legacy_index(vault):
    current_directory = _db_path(vault).parent
    legacy_directory = _legacy_db_path(vault).parent
    migrated_from = None
    if legacy_directory.is_dir() and not current_directory.exists():
        try:
            os.replace(legacy_directory, current_directory)
            migrated_from = str(legacy_directory)
        except FileNotFoundError:
            # Another builder may have completed the same atomic directory move.
            pass
    return migrated_from


def _read_db_path(vault):
    current_db = _db_path(vault)
    current_directory = current_db.parent
    legacy_db = _legacy_db_path(vault)
    legacy_directory = legacy_db.parent
    if current_db.is_file():
        return current_db, False
    if current_directory.exists() or not legacy_db.is_file():
        return current_db, False
    try:
        os.replace(legacy_directory, current_directory)
    except OSError:
        # A locked or permission-blocked directory move must not turn a valid
        # legacy snapshot into silent no-index. Read it in place for this call.
        if current_db.is_file():
            return current_db, False
        if legacy_db.is_file():
            return legacy_db, True
    return current_db, False


def _no_index_result(vault):
    guidance = (
        "This directory has no Epitype index. Run "
        "`python epitype/memsearch.py build <vault>` first."
    )
    if _legacy_db_path(vault).is_file() and not _db_path(vault).exists():
        guidance += " Build will migrate and reuse the detected legacy index."
    payload = {
        "error": "no-index",
        "message": "This directory has no Epitype index.",
        "vault": str(vault),
        "guidance": guidance,
    }
    warnings = _index_warnings(vault)
    if warnings:
        payload["warnings"] = warnings
    return payload


def _scalar(raw):
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, str) else str(decoded)
        except (json.JSONDecodeError, TypeError):
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value.split(" #", 1)[0].strip()


def _alias_values(raw):
    value = raw.strip()
    if value.startswith("[") and "]" in value:
        value = value[1:value.rfind("]")]
        try:
            return [_scalar(item) for item in next(csv.reader([value], skipinitialspace=True)) if item.strip()]
        except csv.Error:
            pass
    scalar = _scalar(value)
    return [scalar] if scalar else []


def _parse_frontmatter(text):
    fields = {field: "" for field in _FRONT_FIELDS}
    aliases = []
    list_key = None
    block_key = None
    block_style = None
    block_lines = []

    def flush_block():
        nonlocal block_key, block_style, block_lines
        if block_key:
            separator = "\n" if block_style == "|" else " "
            fields[block_key] = separator.join(part for part in block_lines if part).strip()
        block_key = None
        block_style = None
        block_lines = []

    for line in text.splitlines():
        stripped = line.strip()
        is_top_level = line == line.lstrip() and ":" in line
        if is_top_level:
            flush_block()
            key, raw = line.split(":", 1)
            key = key.strip()
            list_key = None
            if key not in _FRONT_FIELDS:
                continue
            if key == memspec.ALIASES_FIELD:
                aliases.extend(_alias_values(raw))
                if not raw.strip():
                    list_key = key
            elif raw.strip() in ("|", ">"):
                block_key, block_style = key, raw.strip()
            else:
                fields[key] = _scalar(raw)
            continue
        if block_key and (line.startswith(" ") or line.startswith("\t")):
            block_lines.append(stripped)
        elif list_key == memspec.ALIASES_FIELD and stripped.startswith("-"):
            item = _scalar(stripped[1:])
            if item:
                aliases.append(item)

    flush_block()
    fields[memspec.ALIASES_FIELD] = "\n".join(dict.fromkeys(aliases))
    return fields


def _read_card(path):
    with path.open("rb") as stream:
        first = stream.readline()
        if first.startswith(b"\xef\xbb\xbf"):
            first = first[3:]
        frontmatter = b""
        has_frontmatter = first.strip() == b"---"
        if has_frontmatter:
            chunks = []
            for line in stream:
                if line.strip() == b"---":
                    break
                chunks.append(line)
            frontmatter = b"".join(chunks)
            body = stream.read(memspec.FTS_BODY_SCAN_BYTES)
        else:
            body = (first + stream.read(max(0, memspec.FTS_BODY_SCAN_BYTES - len(first))))[
                :memspec.FTS_BODY_SCAN_BYTES
            ]
    fields = _parse_frontmatter(frontmatter.decode("utf-8", errors="replace"))
    fields["body"] = body.decode("utf-8", errors="replace")
    fields["is_card"] = has_frontmatter and bool(
        fields["name"].strip() or fields["description"].strip()
    )
    return fields


def card_files(vault):
    """Public view of the card scan so other lints share one privacy filter."""
    return _markdown_files(Path(vault).resolve())


def _markdown_files(vault):
    files = []
    # Keep the recursive vault scan, but no private path segment may leak into
    # the generated index merely because only its directory starts with "_".
    for path in vault.rglob("*"):
        try:
            relative = path.relative_to(vault)
            if (
                path.is_file()
                and path.suffix.lower() == ".md"
                and path.name != memspec.MEMORY_INDEX_FILENAME
                and not any(part.startswith("_") for part in relative.parts)
            ):
                files.append(path)
        except OSError:
            continue
    return sorted(files, key=lambda item: item.relative_to(vault).as_posix().casefold())


def _ensure_schema(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    row = connection.execute(
        "SELECT value FROM search_meta WHERE key = ?", (_FTS_FORMAT_KEY,)
    ).fetchone()
    cards_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cards'"
    ).fetchone()
    columns = (
        {item[1] for item in connection.execute("PRAGMA table_info(cards)")}
        if cards_exists
        else set()
    )
    expected_columns = {
        "id",
        "card_path",
        "mtime_ns",
        "size",
        "is_card",
        "name",
        "description",
        "fm_aliases",
        "fm_scope",
        memspec.DECISION_STATUS_FIELD,
        memspec.SUPERSEDED_BY_FIELD,
        "body",
    }
    rebuild = row is None or row[0] != _FTS_FORMAT_VERSION or not expected_columns.issubset(columns)
    if rebuild:
        # The SQLite database is a generated index. A format change rebuilds it
        # from retained Markdown cards so old rows cannot keep blank metadata.
        connection.execute("DROP TABLE IF EXISTS cards_fts")
        connection.execute("DROP TABLE IF EXISTS cards")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY,
            card_path TEXT NOT NULL UNIQUE,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            is_card INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            fm_aliases TEXT NOT NULL,
            fm_scope TEXT NOT NULL,
            status TEXT NOT NULL,
            superseded_by TEXT NOT NULL,
            body TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
            card_path UNINDEXED, name, description, fm_aliases, fm_scope, body,
            tokenize='trigram'
        );
        """
    )
    return rebuild


def _cjk_bigrams(text):
    seen = set()
    for match in _CJK_RUN.finditer(text or ""):
        run = match.group(0)
        for index in range(len(run) - 1):
            term = run[index : index + 2]
            if term not in seen:
                seen.add(term)
                yield term


def _fts_document(text):
    value = text or ""
    encoded = " ".join(_CJK_BIGRAM_PREFIX + term for term in _cjk_bigrams(value))
    return value if not encoded else value + "\n" + encoded


def _replace_fts_row(connection, row_id, card_path, fields):
    connection.execute("DELETE FROM cards_fts WHERE rowid = ?", (row_id,))
    connection.execute(
        "INSERT INTO cards_fts(rowid, card_path, name, description, fm_aliases, fm_scope, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row_id,
            card_path,
            _fts_document(fields["name"]),
            _fts_document(fields["description"]),
            _fts_document(fields[memspec.ALIASES_FIELD]),
            _fts_document(fields[memspec.SCOPE_FIELD]),
            _fts_document(fields["body"]),
        ),
    )


def _stable_card(path):
    for _ in range(2):
        before = path.stat()
        fields = _read_card(path)
        after = path.stat()
        if before.st_mtime_ns == after.st_mtime_ns and before.st_size == after.st_size:
            return after, fields
    return None


def build_index(vault, lock_timeout=0.0):
    vault = _resolve_vault(vault)
    migrated_from = _migrate_legacy_index(vault)
    db_path = _db_path(vault)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 2026-09-01 實測事故：多程序同時更新同一庫會撕裂資料；規則：所有 schema
    # 與卡列異動均包在 memspec 統一鎖內。
    with memspec.file_lock(db_path, lock_timeout) as acquired:
        if not acquired:
            payload = {"status": "lock-busy", "index": str(db_path)}
            if migrated_from:
                payload["index_migrated_from"] = migrated_from
            warnings = _index_warnings(vault)
            if warnings:
                payload["warnings"] = warnings
            return payload

        files = _markdown_files(vault)
        connection = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            rebuild_fts = _ensure_schema(connection)
            known = {
                row[0]: (row[1], row[2])
                for row in connection.execute("SELECT card_path, mtime_ns, size FROM cards")
            }
            current_paths = {path.relative_to(vault).as_posix() for path in files}
            scanned = 0
            removed = 0
            with connection:
                for card_path in sorted(set(known) - current_paths):
                    row = connection.execute(
                        "SELECT id FROM cards WHERE card_path = ?", (card_path,)
                    ).fetchone()
                    if row:
                        connection.execute("DELETE FROM cards_fts WHERE rowid = ?", (row[0],))
                        connection.execute("DELETE FROM cards WHERE id = ?", (row[0],))
                        removed += 1

                # 2026-09-01 實測事故：stale 檢查反覆重掃未變卡片會放大成本；
                # 規則：全文庫增量維護，未變新的卡不得重掃本文。
                for path in files:
                    card_path = path.relative_to(vault).as_posix()
                    try:
                        stat = path.stat()
                        old = known.get(card_path)
                        if old and stat.st_mtime_ns <= old[0]:
                            continue
                        stable = _stable_card(path)
                    except OSError:
                        continue
                    if stable is None:
                        continue
                    stat, fields = stable
                    connection.execute(
                        """
                        INSERT INTO cards(
                            card_path, mtime_ns, size, is_card, name, description, fm_aliases,
                            fm_scope, status, superseded_by, body
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(card_path) DO UPDATE SET
                            mtime_ns=excluded.mtime_ns, size=excluded.size,
                            is_card=excluded.is_card, name=excluded.name,
                            description=excluded.description, fm_aliases=excluded.fm_aliases,
                            fm_scope=excluded.fm_scope, status=excluded.status,
                            superseded_by=excluded.superseded_by, body=excluded.body
                        """,
                        (
                            card_path,
                            stat.st_mtime_ns,
                            stat.st_size,
                            int(fields["is_card"]),
                            fields["name"],
                            fields["description"],
                            fields[memspec.ALIASES_FIELD],
                            fields[memspec.SCOPE_FIELD],
                            fields[memspec.DECISION_STATUS_FIELD],
                            fields[memspec.SUPERSEDED_BY_FIELD],
                            fields["body"],
                        ),
                    )
                    row_id = connection.execute(
                        "SELECT id FROM cards WHERE card_path = ?", (card_path,)
                    ).fetchone()[0]
                    if not rebuild_fts:
                        _replace_fts_row(connection, row_id, card_path, fields)
                    scanned += 1
                if rebuild_fts:
                    connection.execute("DELETE FROM cards_fts")
                    rows = connection.execute(
                        "SELECT id, card_path, name, description, fm_aliases, fm_scope, body "
                        "FROM cards ORDER BY id"
                    ).fetchall()
                    for row in rows:
                        _replace_fts_row(
                            connection,
                            row[0],
                            row[1],
                            {
                                "name": row[2],
                                "description": row[3],
                                memspec.ALIASES_FIELD: row[4],
                                memspec.SCOPE_FIELD: row[5],
                                "body": row[6],
                            },
                        )
                    connection.execute(
                        "INSERT INTO search_meta(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (_FTS_FORMAT_KEY, _FTS_FORMAT_VERSION),
                    )
            card_count = connection.execute("SELECT count(*) FROM cards").fetchone()[0]
        finally:
            connection.close()
        payload = {
            "status": "built",
            "index": str(db_path),
            "cards": card_count,
            "scanned": scanned,
            "removed": removed,
        }
        if migrated_from:
            payload["index_migrated_from"] = migrated_from
        warnings = _index_warnings(vault)
        if warnings:
            payload["warnings"] = warnings
        return payload


def _is_stale(vault, db_path):
    if not db_path.exists():
        return True
    try:
        connection = _read_connection(db_path)
        try:
            row = connection.execute(
                "SELECT value FROM search_meta WHERE key = ?", (_FTS_FORMAT_KEY,)
            ).fetchone()
        finally:
            connection.close()
        if row is None or row[0] != _FTS_FORMAT_VERSION:
            return True
    except sqlite3.Error:
        return True
    latest = None
    for path in _markdown_files(vault):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        latest = modified if latest is None else max(latest, modified)
    if latest is None:
        return False
    try:
        indexed_at = db_path.stat().st_mtime
    except OSError:
        return True
    # Rate-limit rebuilds by index age, never by card age: a card landing inside
    # the window is picked up once the window has passed instead of staying
    # invisible until some later card happens to fall outside it.
    return latest > indexed_at and time.time() - indexed_at > memspec.FTS_STALE_SECONDS


def _hit_fields(row, term):
    needle = term.casefold()
    return [
        field
        for field in _ALL_FIELDS
        if needle in (row[_DB_FIELDS[field]] or "").casefold()
    ]


def _fts_phrase(term):
    return '"' + term.replace('"', '""') + '"'


def _rows_for_term(connection, term):
    quoted = _fts_phrase(term)
    if len(term) >= 3:
        try:
            rows = connection.execute(
                """
                SELECT c.card_path, c.is_card, c.name, c.description, c.fm_aliases, c.fm_scope,
                       c.status, c.superseded_by, c.body,
                       bm25(cards_fts, 0.0, 12.0, 8.0, 12.0, 6.0, 1.0) AS relevance
                FROM cards_fts JOIN cards AS c ON c.id = cards_fts.rowid
                WHERE cards_fts MATCH ?
                """,
                (quoted,),
            ).fetchall()
            if rows:
                return rows
        except sqlite3.OperationalError:
            pass
    # trigram 不收少於三碼的 token；短中英文仍須符合「任何詞都能搜」，故只掃已受限的索引內容。
    return connection.execute(
        "SELECT card_path, is_card, name, description, fm_aliases, fm_scope, status, "
        "superseded_by, body, 0.0 AS relevance FROM cards"
    ).fetchall()


def _recall_terms(prompt):
    priority_terms = []
    priority_seen = set()
    cjk_terms = []
    for match in _RECALL_PART.finditer(prompt):
        part = match.group(0)
        if _CJK_RUN.fullmatch(part):
            cjk_terms.extend(
                part[index : index + 2] for index in range(len(part) - 1)
            )
            continue
        if len(part) < 2:
            continue
        key = part.casefold()
        if key not in priority_seen:
            priority_seen.add(key)
            priority_terms.append(part)

    terms = priority_terms[: memspec.RECALL_MAX_TERMS]
    seen = {term.casefold() for term in terms}
    head = 0
    tail = len(cjk_terms) - 1
    take_head = True
    while len(terms) < memspec.RECALL_MAX_TERMS and head <= tail:
        if take_head:
            term = cjk_terms[head]
            head += 1
        else:
            term = cjk_terms[tail]
            tail -= 1
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        take_head = not take_head
    return terms


def _recall_fts_term(term):
    if len(term) == 2 and _CJK_RUN.fullmatch(term):
        return _CJK_BIGRAM_PREFIX + term
    return term


def _rows_for_recall(connection, terms):
    if not terms:
        return []
    expression = " OR ".join(_fts_phrase(_recall_fts_term(term)) for term in terms)
    try:
        return connection.execute(
            """
            SELECT c.card_path, c.is_card, c.name, c.description, c.fm_aliases, c.fm_scope,
                   c.status, c.superseded_by, c.body,
                   bm25(cards_fts, 0.0, 12.0, 8.0, 12.0, 6.0, 1.0) AS relevance
            FROM cards_fts JOIN cards AS c ON c.id = cards_fts.rowid
            WHERE cards_fts MATCH ?
            """,
            (expression,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _recall_hits(row, terms):
    hit_fields = []
    matched_terms = set()
    for field in _ALL_FIELDS:
        value = (row[_DB_FIELDS[field]] or "").casefold()
        field_hit = False
        for term in terms:
            key = term.casefold()
            if key in value:
                field_hit = True
                matched_terms.add(key)
        if field_hit:
            hit_fields.append(field)
    return hit_fields, len(matched_terms)


def _is_superseded(row):
    return (
        (row[memspec.DECISION_STATUS_FIELD] or "").strip()
        == memspec.SUPERSEDED_DECISION_STATUS
    )


def _successor_path(vault, source_card_path, raw_target, indexed_paths):
    target = posixpath.normpath(str(raw_target or "").strip().replace("\\", "/"))
    if (
        not target
        or target in (".", "..")
        or target.startswith("../")
        or posixpath.isabs(target)
    ):
        return None

    variants = [target]
    if Path(target).suffix.casefold() != ".md":
        variants.append(target + ".md")
    parent = posixpath.dirname(source_card_path)
    relative_candidates = []
    if parent:
        relative_candidates.extend(posixpath.normpath(f"{parent}/{item}") for item in variants)
    relative_candidates.extend(variants)

    indexed = {item.casefold(): item for item in indexed_paths}
    for candidate in relative_candidates:
        matched = indexed.get(candidate.casefold())
        if matched is not None:
            return str((vault / matched).resolve())
        path = (vault / candidate).resolve()
        try:
            path.relative_to(vault)
        except ValueError:
            continue
        if path.is_file():
            return str(path)

    if "/" not in target:
        loose = []
        target_names = {
            Path(item).name.casefold() for item in variants
        } | {
            Path(item).stem.casefold() for item in variants
        }
        for card_path in indexed_paths:
            if (
                Path(card_path).name.casefold() in target_names
                or Path(card_path).stem.casefold() in target_names
            ):
                loose.append(card_path)
        if len(set(loose)) == 1:
            return str((vault / loose[0]).resolve())
    return None


def _guidance_lines(vault, excluded_links, results, indexed_paths):
    result_paths = {item["path"] for item in results}
    lines = []
    seen = set()
    for source_card_path, target in excluded_links:
        successor = _successor_path(vault, source_card_path, target, indexed_paths)
        if successor is None or successor in result_paths:
            continue
        line = _CURRENT_DECISION_GUIDANCE + successor
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def _result(vault, row, hits):
    return {
        "path": str((vault / row["card_path"]).resolve()),
        "is_card": bool(row["is_card"]),
        "name": row["name"],
        "description": row["description"],
        memspec.DECISION_STATUS_FIELD: row[memspec.DECISION_STATUS_FIELD],
        memspec.SUPERSEDED_BY_FIELD: row[memspec.SUPERSEDED_BY_FIELD],
        "hit_fields": hits,
    }


def query_index(vault, term, include_superseded=False, include_noncard=False):
    vault = _resolve_vault(vault)
    term = str(term).strip()
    if not term:
        raise ValueError("query term must not be empty")
    db_path, migration_pending = _read_db_path(vault)
    if not db_path.is_file():
        return _no_index_result(vault)
    index_updated = False
    if not migration_pending and _is_stale(vault, db_path):
        # 2026-09-01 實測事故：重建競爭若阻斷查詢會讓喚回不可用；規則：拿不到鎖
        # 即讀既有 SQLite 快照。
        index_updated = build_index(vault, lock_timeout=0.0)["status"] == "built"

    connection = _read_connection(db_path)
    connection.row_factory = sqlite3.Row
    try:
        candidates = []
        excluded_links = []
        for row in _rows_for_term(connection, term):
            hits = _hit_fields(row, term)
            if not hits:
                continue
            if not include_noncard and not row["is_card"]:
                continue
            if not include_superseded and _is_superseded(row):
                excluded_links.append(
                    (row["card_path"], row[memspec.SUPERSEDED_BY_FIELD])
                )
                continue
            front_hit = any(field in _SEARCH_FRONT_FIELDS for field in hits)
            field_rank = min(_ALL_FIELDS.index(field) for field in hits)
            candidates.append(
                (
                    (0 if front_hit else 1, field_rank, float(row["relevance"]), row["card_path"]),
                    _result(vault, row, hits),
                )
            )
        indexed_paths = (
            [item[0] for item in connection.execute("SELECT card_path FROM cards")]
            if excluded_links
            else []
        )
    finally:
        connection.close()
    candidates.sort(key=lambda item: item[0])
    results = [item[1] for item in candidates[:memspec.FTS_TOP_K]]
    payload = {
        "query": term,
        "count": len(results),
        "results": results,
        "guidance": _guidance_lines(vault, excluded_links, results, indexed_paths),
    }
    if index_updated:
        payload["index_updated"] = True
    if migration_pending:
        payload["index_migration_pending"] = True
    warnings = _index_warnings(vault)
    if warnings:
        payload["warnings"] = warnings
    return payload


def recall_index(vault, prompt, include_superseded=False, include_noncard=False, limit=None):
    """`limit` widens the returned window; callers that pin a card class need to
    see past the default top-k or a pinned card ranked below it never surfaces
    (adversarial review 2026-09-03 #1)."""
    vault = _resolve_vault(vault)
    db_path, migration_pending = _read_db_path(vault)
    if not db_path.is_file():
        return _no_index_result(vault)
    prompt = str(prompt).strip()
    terms = _recall_terms(prompt)
    if not terms:
        payload = {"query": prompt, "terms": [], "count": 0, "results": [], "guidance": []}
        if migration_pending:
            payload["index_migration_pending"] = True
        return payload
    index_updated = False
    if not migration_pending and _is_stale(vault, db_path):
        index_updated = build_index(vault, lock_timeout=0.0)["status"] == "built"

    connection = _read_connection(db_path)
    connection.row_factory = sqlite3.Row
    try:
        candidates = []
        excluded_links = []
        for row in _rows_for_recall(connection, terms):
            hits, matched_count = _recall_hits(row, terms)
            if not hits:
                continue
            if not include_noncard and not row["is_card"]:
                continue
            if not include_superseded and _is_superseded(row):
                excluded_links.append(
                    (row["card_path"], row[memspec.SUPERSEDED_BY_FIELD])
                )
                continue
            front_hit = any(field in _SEARCH_FRONT_FIELDS for field in hits)
            field_rank = min(_ALL_FIELDS.index(field) for field in hits)
            candidates.append(
                (
                    (
                        float(row["relevance"]),
                        -matched_count,
                        0 if front_hit else 1,
                        field_rank,
                        row["card_path"],
                    ),
                    _result(vault, row, hits),
                )
            )
        indexed_paths = (
            [item[0] for item in connection.execute("SELECT card_path FROM cards")]
            if excluded_links
            else []
        )
    finally:
        connection.close()
    candidates.sort(key=lambda item: item[0])
    window = memspec.FTS_TOP_K if limit is None else max(int(limit), memspec.FTS_TOP_K)
    results = [item[1] for item in candidates[:window]]
    payload = {
        "query": prompt,
        "terms": terms,
        "count": len(results),
        "results": results,
        "guidance": _guidance_lines(vault, excluded_links, results, indexed_paths),
    }
    if index_updated:
        payload["index_updated"] = True
    if migration_pending:
        payload["index_migration_pending"] = True
    warnings = _index_warnings(vault)
    if warnings:
        payload["warnings"] = warnings
    return payload


def _write_card(path, frontmatter, body):
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def _selftest():
    checks = []

    def run_cli(*arguments):
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), *map(str, arguments)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    try:
        with tempfile.TemporaryDirectory(prefix="memsearch-") as temp_dir:
            vault = Path(temp_dir).resolve()
            no_index_vault = vault / "empty-vault"
            no_index_vault.mkdir()
            no_index_before = list(no_index_vault.rglob("*"))
            no_index_query = run_cli(
                "query", "missing-index-fixture", "--vault", no_index_vault
            )
            no_index_recall = run_cli(
                "recall", "find missing index fixture", "--vault", no_index_vault
            )
            no_index_query_payload = json.loads(no_index_query.stdout)
            no_index_recall_payload = json.loads(no_index_recall.stdout)
            checks.append(
                (
                    "No-index query and recall stay read-only and explicit",
                    no_index_query.returncode == 1
                    and no_index_recall.returncode == 1
                    and no_index_query_payload.get("error") == "no-index"
                    and no_index_recall_payload.get("error") == "no-index"
                    and "guidance" in no_index_query_payload
                    and "guidance" in no_index_recall_payload
                    and "count" not in no_index_query_payload
                    and "count" not in no_index_recall_payload
                    and list(no_index_vault.rglob("*")) == no_index_before,
                )
            )

            missing_vault = vault / "path-does-not-exist"
            missing_query = run_cli(
                "query", "missing-path-fixture", "--vault", missing_vault
            )
            missing_payload = json.loads(missing_query.stdout)
            checks.append(
                (
                    "Missing vault path is an explicit error",
                    missing_query.returncode == 1
                    and missing_payload.get("error") == "FileNotFoundError"
                    and "does not exist" in missing_payload.get("message", "")
                    and not missing_vault.exists(),
                )
            )

            bilingual = vault / "bilingual.md"
            english = vault / "english.md"
            mixed = vault / "mixed.md"
            front = vault / "front.md"
            body = vault / "body.md"
            other = vault / "other.md"
            old_decision = vault / "old-decision.md"
            current_decision = vault / "current-decision.md"
            memory_index = vault / memspec.MEMORY_INDEX_FILENAME
            private_view = vault / "_VIEW.md"
            private_directory = vault / "_compact_notes"
            private_nested = private_directory / "machine-note.md"
            nonfrontmatter = vault / "plain-note.md"
            blank_metadata = vault / "blank-metadata.md"
            _write_card(
                bilingual,
                "name: 雙語路由卡\ndescription: bilingual CLI routing fixture\naliases: [routealias, relayalias]\nscope: infra",
                "這張卡記錄中文無空格檢索事故與修復。",
            )
            _write_card(
                english,
                "name: Failure Ledger\ndescription: resilient gemini recovery evidence\naliases: [ledger]\nscope: infra",
                "A deterministic English memory card.",
            )
            _write_card(
                mixed,
                "name: 星橋 Memory Bridge\ndescription: 中英 mixed lookup\nscope: governance-core",
                "Cross-language bridge content.",
            )
            _write_card(
                front,
                "name: priorityneedle precedence rule\ndescription: frontmatter ranking case\nscope: data",
                "One field occurrence.",
            )
            _write_card(
                body,
                "name: Body-only Card\ndescription: ranking control\nscope: data",
                "priorityneedle priorityneedle priorityneedle in body only.",
            )
            _write_card(
                other,
                "name: Spare Card\ntags:\n  - sparetag",
                "Unrelated control content with ordinarynostatusneedle.",
            )
            _write_card(
                old_decision,
                "\n".join(
                    (
                        "name: Retired Supersession Fixture",
                        "description: supersessionfixture historical decision",
                        "decision_key: search-contract",
                        f"{memspec.DECISION_STATUS_FIELD}: {memspec.SUPERSEDED_DECISION_STATUS}",
                        f"{memspec.SUPERSEDED_BY_FIELD}: {current_decision.name}",
                    )
                ),
                "supersessionfixture legacydirectionneedle provenance remains retained.",
            )
            _write_card(
                current_decision,
                "\n".join(
                    (
                        "name: Current Supersession Fixture",
                        "description: supersessionfixture current decision",
                        "decision_key: search-contract",
                        f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}",
                    )
                ),
                "supersessionfixture governs the present read path.",
            )
            memory_index.write_text(
                "# Memory Index\nmemoryindexonlyneedle\n",
                encoding="utf-8",
            )
            private_view.write_text(
                "# Generated View\nprivateviewonlyneedle\n",
                encoding="utf-8",
            )
            private_directory.mkdir()
            _write_card(
                private_nested,
                "name: Hidden Machine Note\ndescription: path exclusion fixture",
                "nestedprivateonlyneedle",
            )
            nonfrontmatter.write_text(
                "# Machine note\nplainnoncardneedle\n",
                encoding="utf-8",
            )
            _write_card(
                blank_metadata,
                "scope: archaeology",
                "blankmetadataneedle",
            )

            initial = build_index(vault)
            fresh_index_before = sorted(
                (
                    path.relative_to(vault).as_posix(),
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in _db_path(vault).parent.rglob("*")
                if path.is_file()
            )
            zero_query = query_index(vault, "definitelyabsentqueryfixture")
            fresh_index_after = sorted(
                (
                    path.relative_to(vault).as_posix(),
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in _db_path(vault).parent.rglob("*")
                if path.is_file()
            )
            checks.append(
                (
                    "Indexed zero-hit query keeps the established empty-result shape",
                    zero_query
                    == {
                        "query": "definitelyabsentqueryfixture",
                        "count": 0,
                        "results": [],
                        "guidance": [],
                    }
                    and fresh_index_after == fresh_index_before,
                )
            )
            connection = sqlite3.connect(str(_db_path(vault)))
            try:
                indexed_metadata = connection.execute(
                    "SELECT status, superseded_by FROM cards WHERE card_path = ?",
                    (old_decision.name,),
                ).fetchone()
                indexed_markers = dict(
                    connection.execute("SELECT card_path, is_card FROM cards")
                )
            finally:
                connection.close()
            checks.append(
                (
                    "Underscore path segment excluded",
                    private_nested.relative_to(vault).as_posix() not in indexed_markers
                    and query_index(
                        vault, "nestedprivateonlyneedle", include_noncard=True
                    )["count"]
                    == 0
                    and recall_index(
                        vault, "find nestedprivateonlyneedle", include_noncard=True
                    )["count"]
                    == 0,
                )
            )
            cli_noncard_query = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "query",
                    "plainnoncardneedle",
                    "--vault",
                    str(vault),
                    "--include-noncard",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            cli_noncard_recall = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "recall",
                    "find blankmetadataneedle",
                    "--vault",
                    str(vault),
                    "--include-noncard",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            noncard_query_results = json.loads(cli_noncard_query.stdout)["results"]
            noncard_recall_results = json.loads(cli_noncard_recall.stdout)["results"]
            checks.append(
                (
                    "Non-card default exclusion and archaeology opt-in",
                    indexed_markers[nonfrontmatter.name] == 0
                    and indexed_markers[blank_metadata.name] == 0
                    and query_index(vault, "plainnoncardneedle")["count"] == 0
                    and recall_index(vault, "find blankmetadataneedle")["count"] == 0
                    and cli_noncard_query.returncode == 0
                    and cli_noncard_recall.returncode == 0
                    and len(noncard_query_results) == 1
                    and noncard_query_results[0]["path"] == str(nonfrontmatter.resolve())
                    and noncard_query_results[0]["is_card"] is False
                    and len(noncard_recall_results) == 1
                    and noncard_recall_results[0]["path"] == str(blank_metadata.resolve())
                    and noncard_recall_results[0]["is_card"] is False,
                )
            )
            normal_query = query_index(vault, "resilient")
            normal_recall = recall_index(vault, "recover resilient evidence")
            checks.append(
                (
                    "Normal card unaffected by non-card filter",
                    indexed_markers[english.name] == 1
                    and normal_query["results"][0]["path"] == str(english.resolve())
                    and normal_query["results"][0]["is_card"] is True
                    and normal_recall["results"][0]["path"] == str(english.resolve())
                    and normal_recall["results"][0]["is_card"] is True,
                )
            )
            default_query = query_index(vault, "supersessionfixture")
            default_recall = recall_index(vault, "load supersessionfixture decision")
            checks.append(
                (
                    "Default query and recall exclude superseded",
                    indexed_metadata
                    == (
                        memspec.SUPERSEDED_DECISION_STATUS,
                        current_decision.name,
                    )
                    and {item["path"] for item in default_query["results"]}
                    == {str(current_decision.resolve())}
                    and {item["path"] for item in default_recall["results"]}
                    == {str(current_decision.resolve())}
                    and default_query["guidance"] == []
                    and default_recall["guidance"] == [],
                )
            )
            cli_query_all = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "query",
                    "supersessionfixture",
                    "--vault",
                    str(vault),
                    "--include-superseded",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            cli_recall_all = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "recall",
                    "load supersessionfixture decision",
                    "--vault",
                    str(vault),
                    "--include-superseded",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            all_query_results = json.loads(cli_query_all.stdout)["results"]
            all_recall_results = json.loads(cli_recall_all.stdout)["results"]
            expected_decisions = {
                str(old_decision.resolve()),
                str(current_decision.resolve()),
            }
            checks.append(
                (
                    "Include-superseded archaeology returns both cards",
                    cli_query_all.returncode == 0
                    and cli_recall_all.returncode == 0
                    and {item["path"] for item in all_query_results} == expected_decisions
                    and {item["path"] for item in all_recall_results} == expected_decisions
                    and json.loads(cli_query_all.stdout)["guidance"] == []
                    and json.loads(cli_recall_all.stdout)["guidance"] == []
                    and any(
                        item["path"] == str(old_decision.resolve())
                        and item[memspec.DECISION_STATUS_FIELD]
                        == memspec.SUPERSEDED_DECISION_STATUS
                        and item[memspec.SUPERSEDED_BY_FIELD] == current_decision.name
                        for item in all_query_results
                    ),
                )
            )
            expected_guidance = [
                _CURRENT_DECISION_GUIDANCE + str(current_decision.resolve())
            ]
            guidance_query = query_index(vault, "legacydirectionneedle")
            guidance_recall = recall_index(vault, "recall legacydirectionneedle")
            checks.append(
                (
                    "Superseded result points to current decision",
                    guidance_query["count"] == 0
                    and guidance_query["guidance"] == expected_guidance
                    and guidance_recall["count"] == 0
                    and guidance_recall["guidance"] == expected_guidance,
                )
            )
            ordinary_query = query_index(vault, "ordinarynostatusneedle")
            ordinary_recall = recall_index(vault, "find ordinarynostatusneedle")
            checks.append(
                (
                    "Card without status remains eligible",
                    ordinary_query["results"][0]["path"] == str(other.resolve())
                    and ordinary_recall["results"][0]["path"] == str(other.resolve()),
                )
            )
            checks.append(("English term", query_index(vault, "resilient")["results"][0]["path"] == str(english.resolve())))
            checks.append(("Chinese trigram", query_index(vault, "中文無空格")["results"][0]["path"] == str(bilingual.resolve())))
            checks.append(
                (
                    "Index and underscore views excluded",
                    query_index(vault, "memoryindexonlyneedle")["count"] == 0
                    and query_index(vault, "privateviewonlyneedle")["count"] == 0,
                )
            )
            cli_query = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "query", "routealias", "--vault", str(vault)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            cli_payload = json.loads(cli_query.stdout)
            alias_results = cli_payload["results"]
            checks.append(
                (
                    "Alias hit",
                    cli_query.returncode == 0
                    and len(alias_results) == 1
                    and alias_results[0]["path"] == str(bilingual.resolve())
                    and memspec.ALIASES_FIELD in alias_results[0]["hit_fields"],
                )
            )
            cli_recall = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "recall",
                 "how do I drive gemini from a terminal", "--vault", str(vault)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            recall_payload = json.loads(cli_recall.stdout)
            checks.append(
                (
                    "English natural-sentence recall",
                    cli_recall.returncode == 0
                    and recall_payload["results"][0]["path"] == str(english.resolve()),
                )
            )
            chinese_recall = recall_index(vault, "怎麼用星橋處理未知噪音")
            checks.append(
                (
                    "Chinese natural-sentence bigram recall",
                    chinese_recall["results"][0]["path"] == str(mixed.resolve())
                    and "星橋" in chinese_recall["terms"],
                )
            )
            long_chinese_noise = (
                "這是一段刻意放在前方而且完全不相關的中文噪音內容"
                "用來模擬使用者描述背景脈絡最後才說"
            )
            latin_tail_recall = recall_index(vault, long_chinese_noise + " gemini")
            checks.append(
                (
                    "Long Chinese prompt retains trailing Latin keyword",
                    latin_tail_recall["results"][0]["path"] == str(english.resolve())
                    and "gemini" in latin_tail_recall["terms"]
                    and len(latin_tail_recall["terms"]) == memspec.RECALL_MAX_TERMS,
                )
            )
            chinese_tail_recall = recall_index(vault, long_chinese_noise + "星橋")
            checks.append(
                (
                    "Long Chinese prompt samples trailing Chinese keyword",
                    chinese_tail_recall["results"][0]["path"] == str(mixed.resolve())
                    and "星橋" in chinese_tail_recall["terms"]
                    and len(chinese_tail_recall["terms"]) == memspec.RECALL_MAX_TERMS,
                )
            )
            checks.append(
                (
                    "All-noise recall is empty",
                    recall_index(vault, "quartz zebras frolic beyond nebula")["count"] == 0,
                )
            )
            empty_recall = recall_index(vault, "I a x \t")
            checks.append(
                ("Empty recall term set", empty_recall["count"] == 0 and empty_recall["terms"] == [])
            )
            special_recall = recall_index(vault, 'gemini (OR) "noise" + wildcard*')
            checks.append(
                (
                    "FTS-special tokens are escaped",
                    special_recall["results"][0]["path"] == str(english.resolve()),
                )
            )
            connection = sqlite3.connect(str(_db_path(vault)))
            try:
                with connection:
                    connection.executescript(
                        """
                        DROP TABLE cards_fts;
                        DROP TABLE cards;
                        CREATE TABLE cards (
                            id INTEGER PRIMARY KEY,
                            card_path TEXT NOT NULL UNIQUE,
                            mtime_ns INTEGER NOT NULL,
                            size INTEGER NOT NULL,
                            name TEXT NOT NULL,
                            description TEXT NOT NULL,
                            fm_aliases TEXT NOT NULL,
                            fm_scope TEXT NOT NULL,
                            body TEXT NOT NULL
                        );
                        CREATE VIRTUAL TABLE cards_fts USING fts5(
                            card_path UNINDEXED, name, description, fm_aliases, fm_scope, body,
                            tokenize='trigram'
                        );
                        """
                    )
                    for legacy_path in _markdown_files(vault) + [memory_index, private_view]:
                        legacy_stat = legacy_path.stat()
                        legacy_fields = _read_card(legacy_path)
                        cursor = connection.execute(
                            """
                            INSERT INTO cards(card_path, mtime_ns, size, name, description, fm_aliases, fm_scope, body)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                legacy_path.name,
                                legacy_stat.st_mtime_ns,
                                legacy_stat.st_size,
                                legacy_fields["name"],
                                legacy_fields["description"],
                                legacy_fields[memspec.ALIASES_FIELD],
                                legacy_fields[memspec.SCOPE_FIELD],
                                legacy_fields["body"],
                            ),
                        )
                        _replace_fts_row(
                            connection,
                            cursor.lastrowid,
                            legacy_path.name,
                            legacy_fields,
                        )
                    connection.execute(
                        "UPDATE search_meta SET value = ? WHERE key = ?",
                        ("4", _FTS_FORMAT_KEY),
                    )
            finally:
                connection.close()
            migrated_recall = recall_index(vault, "怎麼用星橋處理未知噪音")
            checks.append(
                (
                    "Legacy index format auto-rebuild",
                    migrated_recall["results"][0]["path"] == str(mixed.resolve())
                    and query_index(vault, "memoryindexonlyneedle")["count"] == 0
                    and query_index(vault, "privateviewonlyneedle")["count"] == 0
                    and query_index(vault, "legacydirectionneedle")["guidance"]
                    == [_CURRENT_DECISION_GUIDANCE + str(current_decision.resolve())],
                )
            )
            checks.append(("Frontmatter first", query_index(vault, "priorityneedle")["results"][0]["path"] == str(front.resolve())))

            time.sleep(0.01)
            mixed.write_text(mixed.read_text(encoding="utf-8") + "新鮮索引自動重建證據\n", encoding="utf-8")
            future = time.time() + 1.0
            os.utime(mixed, (future, future))
            old_db = time.time() - memspec.FTS_STALE_SECONDS - 2.0
            os.utime(_db_path(vault), (old_db, old_db))
            stale_payload = query_index(vault, "自動重建證據")
            stale_results = stale_payload["results"]
            checks.append(("Stale incremental rebuild", bool(stale_results) and stale_results[0]["path"] == str(mixed.resolve())))
            checks.append(
                (
                    "Stale incremental update is declared",
                    stale_payload.get("index_updated") is True,
                )
            )

            grace_vault = Path(tempfile.mkdtemp(prefix="epitype-grace-"))
            grace_old = grace_vault / "old.md"
            grace_old.write_text("---\nname: Old\ndescription: graceoldneedle\n---\n", encoding="utf-8")
            build_index(grace_vault)
            grace_db = _db_path(grace_vault)
            grace_young = grace_vault / "young.md"
            grace_young.write_text("---\nname: Young\ndescription: graceyoungneedle\n---\n", encoding="utf-8")
            grace_now = time.time()
            os.utime(grace_old, (grace_now - 2000, grace_now - 2000))
            os.utime(grace_db, (grace_now - 100, grace_now - 100))
            os.utime(grace_young, (grace_now - 90, grace_now - 90))
            inside_window = _is_stale(grace_vault, grace_db)
            os.utime(grace_db, (grace_now - 1000, grace_now - 1000))
            os.utime(grace_young, (grace_now - 990, grace_now - 990))
            checks.append((
                "Card inside grace window indexes once the index itself ages out",
                not inside_window
                and _is_stale(grace_vault, grace_db)
                and query_index(grace_vault, "graceyoungneedle")["results"][0]["path"] == str(grace_young.resolve()),
            ))
            shutil.rmtree(grace_vault, ignore_errors=True)

            other.write_text(other.read_text(encoding="utf-8") + "concurrentwriteproof\n", encoding="utf-8")
            concurrent_mtime = time.time() + 2.0
            os.utime(other, (concurrent_mtime, concurrent_mtime))
            workers = 8
            barrier = threading.Barrier(workers)
            outcomes = [None] * workers

            def concurrent_build(index):
                try:
                    barrier.wait()
                    outcomes[index] = build_index(vault)
                except (OSError, sqlite3.Error, threading.BrokenBarrierError) as exc:
                    outcomes[index] = exc

            threads = [threading.Thread(target=concurrent_build, args=(index,)) for index in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            connection = sqlite3.connect(str(_db_path(vault)))
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                card_count = connection.execute("SELECT count(*) FROM cards").fetchone()[0]
                fts_count = connection.execute("SELECT count(*) FROM cards_fts").fetchone()[0]
            finally:
                connection.close()
            concurrent_ok = (
                initial["status"] == "built"
                and all(isinstance(outcome, dict) for outcome in outcomes)
                and any(outcome["status"] == "built" for outcome in outcomes)
                and integrity == "ok"
                and card_count == 10
                and fts_count == 10
                and query_index(vault, "concurrentwriteproof")["results"][0]["path"]
                == str(other.resolve())
            )
            checks.append(("Concurrent build without tears", concurrent_ok))

        with tempfile.TemporaryDirectory(prefix="epitype-legacy-index-") as temp_dir:
            legacy_vault = Path(temp_dir).resolve()
            legacy_card = legacy_vault / "retained.md"
            _write_card(
                legacy_card,
                "name: Retained Index Card\ndescription: retainedlegacyfixture\nscope: infra",
                "Existing index rows must survive the directory migration.",
            )
            build_index(legacy_vault)
            current_directory = _db_path(legacy_vault).parent
            legacy_directory = _legacy_db_path(legacy_vault).parent
            current_directory.rename(legacy_directory)
            migration = build_index(legacy_vault)
            migrated_query = query_index(legacy_vault, "retainedlegacyfixture")
            checks.append(
                (
                    "Legacy index directory migrates through build and remains searchable",
                    migration.get("index_migrated_from") == str(legacy_directory)
                    and _db_path(legacy_vault).is_file()
                    and not legacy_directory.exists()
                    and migrated_query["results"][0]["path"]
                    == str(legacy_card.resolve()),
                )
            )

        with tempfile.TemporaryDirectory(prefix="epitype-read-migration-") as temp_dir:
            read_vault = Path(temp_dir).resolve()
            read_card = read_vault / "read.md"
            _write_card(
                read_card,
                "name: Read Migration Card\ndescription: readmigrationfixture\nscope: infra",
                "Query and recall both migrate the existing snapshot on first read.",
            )
            build_index(read_vault)
            read_current = _db_path(read_vault).parent
            read_legacy = _legacy_db_path(read_vault).parent
            os.replace(read_current, read_legacy)
            read_query = query_index(read_vault, "readmigrationfixture")
            checks.append(
                (
                    "Query migrates a legacy index on read",
                    read_query["count"] == 1
                    and read_current.is_dir()
                    and not read_legacy.exists(),
                )
            )
            os.replace(read_current, read_legacy)
            read_recall = recall_index(read_vault, "find readmigrationfixture")
            checks.append(
                (
                    "Recall migrates a legacy index on read",
                    read_recall["count"] == 1
                    and read_current.is_dir()
                    and not read_legacy.exists(),
                )
            )

        with tempfile.TemporaryDirectory(prefix="epitype-pending-migration-") as temp_dir:
            pending_vault = Path(temp_dir).resolve()
            pending_card = pending_vault / "pending.md"
            _write_card(
                pending_card,
                "name: Pending Migration Card\ndescription: pendingmigrationfixture\nscope: infra",
                "A blocked rename still reads the legacy snapshot.",
            )
            build_index(pending_vault)
            pending_current = _db_path(pending_vault).parent
            pending_legacy = _legacy_db_path(pending_vault).parent
            os.replace(pending_current, pending_legacy)
            real_replace = os.replace

            def blocked_replace(source, destination):
                if Path(source) == pending_legacy and Path(destination) == pending_current:
                    raise PermissionError("synthetic blocked index migration")
                return real_replace(source, destination)

            os.replace = blocked_replace
            try:
                pending_query = query_index(pending_vault, "pendingmigrationfixture")
                pending_recall = recall_index(
                    pending_vault, "find pendingmigrationfixture"
                )
            finally:
                os.replace = real_replace
            checks.append(
                (
                    "Blocked read migration falls back to the legacy snapshot",
                    pending_query["count"] == 1
                    and pending_recall["count"] == 1
                    and pending_query.get("index_migration_pending") is True
                    and pending_recall.get("index_migration_pending") is True
                    and pending_legacy.is_dir()
                    and not pending_current.exists(),
                )
            )

        with tempfile.TemporaryDirectory(prefix="epitype-dual-index-") as temp_dir:
            dual_vault = Path(temp_dir).resolve()
            dual_card = dual_vault / "current.md"
            _write_card(
                dual_card,
                "name: Current Index Card\ndescription: currentindexfixture\nscope: infra",
                "The current index wins when both directories exist.",
            )
            build_index(dual_vault)
            dual_legacy_directory = _legacy_db_path(dual_vault).parent
            dual_legacy_directory.mkdir()
            dual_query = query_index(dual_vault, "currentindexfixture")
            checks.append(
                (
                    "Current index wins and the legacy directory is disclosed",
                    dual_query["results"][0]["path"] == str(dual_card.resolve())
                    and any(
                        "can be deleted" in warning
                        and str(dual_legacy_directory) in warning
                        for warning in dual_query.get("warnings", ())
                    ),
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 32
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": "usage", "message": message}, ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(2)


def _parser():
    parser = _JsonArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    build = subparsers.add_parser("build")
    build.add_argument("vault")
    query = subparsers.add_parser("query")
    query.add_argument("term")
    query.add_argument("--vault", default=".")
    query.add_argument("--include-superseded", action="store_true")
    query.add_argument("--include-noncard", action="store_true")
    recall = subparsers.add_parser("recall")
    recall.add_argument("prompt")
    recall.add_argument("--vault", default=".")
    recall.add_argument("--include-superseded", action="store_true")
    recall.add_argument("--include-noncard", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.selftest:
        return _selftest()
    try:
        if args.command == "build":
            payload = build_index(args.vault)
            exit_code = 0 if payload["status"] == "built" else 3
        elif args.command == "query":
            payload = query_index(
                args.vault,
                args.term,
                include_superseded=args.include_superseded,
                include_noncard=args.include_noncard,
            )
            exit_code = 1 if payload.get("error") else 0
        elif args.command == "recall":
            payload = recall_index(
                args.vault,
                args.prompt,
                include_superseded=args.include_superseded,
                include_noncard=args.include_noncard,
            )
            exit_code = 1 if payload.get("error") else 0
        else:
            payload = {"error": "command required", "commands": ["build", "query", "recall"]}
            exit_code = 2
    except (OSError, sqlite3.Error, ValueError) as exc:
        payload = {"error": type(exc).__name__, "message": str(exc)}
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
