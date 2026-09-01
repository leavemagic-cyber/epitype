import sys; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，避免繁中輸出在程式進入點就中斷。
"""Epitype 記憶卡的本機 FTS5 全文與別名搜尋器。"""

import argparse
import csv
import json
import os
from pathlib import Path
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
_FRONT_FIELDS = ("name", "description", memspec.ALIASES_FIELD, memspec.SCOPE_FIELD)
_ALL_FIELDS = _FRONT_FIELDS + ("body",)
_DB_FIELDS = {
    "name": "name",
    "description": "description",
    memspec.ALIASES_FIELD: "fm_aliases",
    memspec.SCOPE_FIELD: "fm_scope",
    "body": "body",
}


def _db_path(vault):
    return vault / memspec.FTS_DB_PATH


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
        if first.strip() == b"---":
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
    return fields


def _markdown_files(vault):
    files = []
    for path in vault.rglob("*"):
        try:
            if path.is_file() and path.suffix.lower() == ".md":
                files.append(path)
        except OSError:
            continue
    return sorted(files, key=lambda item: item.relative_to(vault).as_posix().casefold())


def _ensure_schema(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS cards (
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
        CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
            card_path UNINDEXED, name, description, fm_aliases, fm_scope, body,
            tokenize='trigram'
        );
        """
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
    vault = Path(vault).resolve()
    if not vault.is_dir():
        raise NotADirectoryError(str(vault))
    db_path = _db_path(vault)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 2026-09-01 實測事故：多程序同時更新同一庫會撕裂資料；規則：所有 schema
    # 與卡列異動均包在 memspec 統一鎖內。
    with memspec.file_lock(db_path, lock_timeout) as acquired:
        if not acquired:
            return {"status": "lock-busy", "index": str(db_path)}

        files = _markdown_files(vault)
        connection = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            _ensure_schema(connection)
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
                        INSERT INTO cards(card_path, mtime_ns, size, name, description, fm_aliases, fm_scope, body)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(card_path) DO UPDATE SET
                            mtime_ns=excluded.mtime_ns, size=excluded.size, name=excluded.name,
                            description=excluded.description, fm_aliases=excluded.fm_aliases,
                            fm_scope=excluded.fm_scope, body=excluded.body
                        """,
                        (
                            card_path,
                            stat.st_mtime_ns,
                            stat.st_size,
                            fields["name"],
                            fields["description"],
                            fields[memspec.ALIASES_FIELD],
                            fields[memspec.SCOPE_FIELD],
                            fields["body"],
                        ),
                    )
                    row_id = connection.execute(
                        "SELECT id FROM cards WHERE card_path = ?", (card_path,)
                    ).fetchone()[0]
                    connection.execute("DELETE FROM cards_fts WHERE rowid = ?", (row_id,))
                    connection.execute(
                        "INSERT INTO cards_fts(rowid, card_path, name, description, fm_aliases, fm_scope, body) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            row_id,
                            card_path,
                            fields["name"],
                            fields["description"],
                            fields[memspec.ALIASES_FIELD],
                            fields[memspec.SCOPE_FIELD],
                            fields["body"],
                        ),
                    )
                    scanned += 1
            card_count = connection.execute("SELECT count(*) FROM cards").fetchone()[0]
        finally:
            connection.close()
        return {
            "status": "built",
            "index": str(db_path),
            "cards": card_count,
            "scanned": scanned,
            "removed": removed,
        }


def _is_stale(vault, db_path):
    if not db_path.exists():
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
        return latest - db_path.stat().st_mtime > memspec.FTS_STALE_SECONDS
    except OSError:
        return True


def _hit_fields(row, term):
    needle = term.casefold()
    return [
        field
        for field in _ALL_FIELDS
        if needle in (row[_DB_FIELDS[field]] or "").casefold()
    ]


def _rows_for_term(connection, term):
    quoted = '"' + term.replace('"', '""') + '"'
    if len(term) >= 3:
        try:
            rows = connection.execute(
                """
                SELECT c.card_path, c.name, c.description, c.fm_aliases, c.fm_scope, c.body,
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
        "SELECT card_path, name, description, fm_aliases, fm_scope, body, 0.0 AS relevance FROM cards"
    ).fetchall()


def query_index(vault, term):
    vault = Path(vault).resolve()
    if not vault.is_dir():
        raise NotADirectoryError(str(vault))
    term = str(term).strip()
    if not term:
        raise ValueError("query term must not be empty")
    db_path = _db_path(vault)
    if _is_stale(vault, db_path):
        # 2026-09-01 實測事故：重建競爭若阻斷查詢會讓喚回不可用；規則：拿不到鎖
        # 即讀既有 SQLite 快照。
        build_index(vault, lock_timeout=0.0)

    connection = sqlite3.connect(str(db_path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        candidates = []
        for row in _rows_for_term(connection, term):
            hits = _hit_fields(row, term)
            if not hits:
                continue
            front_hit = any(field in _FRONT_FIELDS for field in hits)
            field_rank = min(_ALL_FIELDS.index(field) for field in hits)
            candidates.append(
                (
                    (0 if front_hit else 1, field_rank, float(row["relevance"]), row["card_path"]),
                    {
                        "path": str((vault / row["card_path"]).resolve()),
                        "name": row["name"],
                        "description": row["description"],
                        "hit_fields": hits,
                    },
                )
            )
    finally:
        connection.close()
    candidates.sort(key=lambda item: item[0])
    results = [item[1] for item in candidates[:memspec.FTS_TOP_K]]
    return {"query": term, "count": len(results), "results": results}


def _write_card(path, frontmatter, body):
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="memsearch-") as temp_dir:
            vault = Path(temp_dir)
            bilingual = vault / "bilingual.md"
            english = vault / "english.md"
            mixed = vault / "mixed.md"
            front = vault / "front.md"
            body = vault / "body.md"
            other = vault / "other.md"
            _write_card(
                bilingual,
                "name: 雙語路由卡\ndescription: bilingual CLI routing fixture\naliases: [routealias, relayalias]\nscope: infra",
                "這張卡記錄中文無空格檢索事故與修復。",
            )
            _write_card(
                english,
                "name: Failure Ledger\ndescription: resilient recovery evidence\naliases: [ledger]\nscope: infra",
                "A deterministic English memory card.",
            )
            _write_card(
                mixed,
                "name: 混合 Memory Bridge\ndescription: 中英 mixed lookup\nscope: governance-core",
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
            _write_card(other, "name: Spare Card\ntags:\n  - sparetag", "Unrelated control content.")

            initial = build_index(vault)
            checks.append(("English term", query_index(vault, "resilient")["results"][0]["path"] == str(english.resolve())))
            checks.append(("Chinese trigram", query_index(vault, "中文無空格")["results"][0]["path"] == str(bilingual.resolve())))
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
            checks.append(("Frontmatter first", query_index(vault, "priorityneedle")["results"][0]["path"] == str(front.resolve())))

            time.sleep(0.01)
            mixed.write_text(mixed.read_text(encoding="utf-8") + "新鮮索引自動重建證據\n", encoding="utf-8")
            future = time.time() + 1.0
            os.utime(mixed, (future, future))
            old_db = time.time() - memspec.FTS_STALE_SECONDS - 2.0
            os.utime(_db_path(vault), (old_db, old_db))
            stale_results = query_index(vault, "自動重建證據")["results"]
            checks.append(("Stale incremental rebuild", bool(stale_results) and stale_results[0]["path"] == str(mixed.resolve())))

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
                and card_count == 6
                and fts_count == 6
                and query_index(vault, "concurrentwriteproof")["results"][0]["path"]
                == str(other.resolve())
            )
            checks.append(("Concurrent build without tears", concurrent_ok))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 6
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
            payload = query_index(args.vault, args.term)
            exit_code = 0
        else:
            payload = {"error": "command required", "commands": ["build", "query"]}
            exit_code = 2
    except (OSError, sqlite3.Error, ValueError) as exc:
        payload = {"error": type(exc).__name__, "message": str(exc)}
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
