import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，避免繁中輸出在程式進入點就中斷。
"""事件卡的落點規則，與治理庫的誤置稽核／歸戶工具。

2026-09-06 實測事故：自動捕捉（線上 hook 與離線回放）一律把 owner 的授權／糾正／
裁定卡寫進治理庫，所以在某個專案的對話裡講、話裡也明講那個專案的裁定，卻被寫進
通用庫（實測治理庫 132 張事件卡有 74 張的 cwd 指向別的已登記專案庫）。落點
規則因此集中在這裡一次實作：**這場對話屬於哪個專案，卡就進那個專案的記憶庫**；cwd
不屬於任何已登記專案庫時才落治理庫。跨專案通用的長效規則仍該進治理庫，但那是人立卡
時的判斷，自動捕捉判不了，所以自動捕捉一律照 cwd 落點，並把來源專案留在卡上的
``cwd`` 欄位供事後歸戶。

同一份規則被三個呼叫端共用：Claude 的 UserPromptSubmit hook（線上捕捉）、
``epitype.harvest``（離線回放）、以及本模組的 ``--audit``／``--apply``（既有卡的誤置
盤點與歸戶）。規則若各寫一份，回放與線上就會把同一句話寫進不同的庫。
"""

import argparse
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time

try:
    from . import memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import memspec


# 宿主（Claude Code）自己按 cwd 開一個記憶目錄：<home>/.claude/projects/<slug>/memory。
NATIVE_PROJECTS_SUBPATH = (".claude", "projects")
NATIVE_MEMORY_DIRNAME = "memory"
# 宿主開在家目錄下、不綁專案的那幾個記憶庫；與專案庫一起構成「原生庫」的全部落點。
NATIVE_SINGLE_VAULT_SUBPATHS = (
    (".claude", "memory"),
    (".claude", "memories"),
    (".codex", "memories"),
)
_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9]")

# 落點與稽核的判定值：誤置、已在對的庫、卡上沒有 cwd（判不了，不搬）、卡讀不出來。
ROUTE_MISROUTED = "misrouted"
ROUTE_OK = "ok"
ROUTE_UNKNOWN = "unknown"
ROUTE_BROKEN = "broken"

# 歸戶註記寫在正文最後一行；搬過的卡要能自己說出它是從哪個庫、依什麼證據搬來的。
REHOME_NOTE = "（{stamp} 歸戶：自 {source} 移入，依卡上 {field}「{cwd}」；epitype/capture_route.py --apply）"
# 同名時的後綴上限：撞名超過這個數字代表判斷錯了，不是要再加一個檔。
MAX_RENAME_SUFFIX = 99


def _one_line(value):
    return " ".join(str(value or "").split())


def project_slug(text):
    """宿主把 cwd 的每個非英數字元換成 `-` 當目錄名；歸戶要算同一個 slug。"""
    return _SLUG_PATTERN.sub("-", str(text))


def holds_cards(vault):
    """True 表示這個原生記憶庫「已登記」：已有索引或至少一張卡。

    宿主會替每個 cwd 開空目錄，空殼不是庫；往空殼寫第一張卡等於替 owner 決定
    要在那裡開一個庫，所以未登記的一律退回治理庫（卡上仍記 cwd，可事後歸戶）。
    """
    vault = Path(vault)
    try:
        if (vault / memspec.MEMORY_INDEX_FILENAME).is_file():
            return True
        return any(
            item.suffix.lower() == ".md" and not item.name.startswith("_")
            for item in vault.iterdir()
        )
    except OSError:
        return False


def native_cwd_vaults(cwd, home=None):
    """這個 cwd 屬於哪些原生記憶庫，最相關（最深）的在前。

    cwd 自己與每一層祖先都算：對話常在專案的子目錄裡跑，卡屬於專案而不是那個子
    目錄。只收已登記的庫（holds_cards），空殼跳過，所以不會有索引被種進宿主剛開
    的空目錄。喚回端（_hook_common.resolve_vaults）與落點端讀的是同一份清單。
    """
    if not isinstance(cwd, str) or not cwd.strip():
        return []
    projects = (Path(home) if home is not None else Path.home()).joinpath(*NATIVE_PROJECTS_SUBPATH)
    try:
        start = Path(cwd)
        bases = (start, *start.parents)
    except (TypeError, ValueError):
        return []
    found = []
    for base in bases:
        texts = {str(base)}
        try:
            texts.add(str(base.resolve()))
        except OSError:
            pass
        for text in sorted(texts):
            candidate = projects / project_slug(text) / NATIVE_MEMORY_DIRNAME
            if candidate.is_dir() and holds_cards(candidate):
                resolved = candidate.resolve()
                if resolved not in found:
                    found.append(resolved)
    return found


def capture_vault(cwd, governance, home=None):
    """自動捕捉的事件卡該寫進哪個庫：專案的進專案庫，其餘進治理庫。"""
    return next(iter(native_cwd_vaults(cwd, home)), Path(governance).resolve())


def native_vaults(home=None):
    """這台機器上所有「裝著卡」的原生記憶庫，空殼不算。

    判準沿用 holds_cards：安裝器、夜間夢與落點端若各寫一套「這個目錄算不算庫」，同一
    個目錄會一邊被管、一邊不被管（2026-09-21 之前 install/graft.py 用 rglob 另判一
    套）。要掃的根與安裝器的偵測清單同一份：專案庫與各宿主的單一庫。
    """
    base = Path(home) if home is not None else Path.home()
    candidates = []
    try:
        with os.scandir(base.joinpath(*NATIVE_PROJECTS_SUBPATH)) as entries:
            candidates.extend(
                Path(entry.path) / NATIVE_MEMORY_DIRNAME
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
            )
    except OSError:
        pass
    candidates.extend(base.joinpath(*parts) for parts in NATIVE_SINGLE_VAULT_SUBPATHS)
    found = []
    seen = set()
    for candidate in candidates:
        if not candidate.is_dir() or not holds_cards(candidate):
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = os.path.normcase(os.fspath(resolved))
        if key in seen:
            continue
        seen.add(key)
        found.append(resolved)
    return sorted(found, key=lambda item: os.path.normcase(os.fspath(item)))


def managed_vaults(configured, home=None):
    """實際受管的庫：設定檔登記的（原順序、只留存在的）在前，接上掃描到的原生庫。

    owner 2026-09-21：「不應該是登記，應該是所有不管是不是專案都通用才對」。設定檔的
    `vaults` 不是名冊——裝著卡的原生庫本來就是這套東西在管的庫，沒被登記只代表沒人去
    寫那一行，不代表它的卡不該進夜間夢、卡片檢查與視圖。

    掃描只准在 CLI 與夜間這一側做。喚回與各 hook（adapters/）仍以 cwd 解析庫：實測
    373 個專案目錄的全域掃描要 0.44 秒，每一則提問都付這個代價不可接受。
    """
    found = []
    seen = set()
    for item in configured or ():
        try:
            if isinstance(item, str) and not item.strip():
                continue
            path = Path(item).expanduser()
            if not path.is_dir():
                continue
            resolved = path.resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        key = os.path.normcase(os.fspath(resolved))
        if key not in seen:
            seen.add(key)
            found.append(resolved)
    for vault in native_vaults(home):
        key = os.path.normcase(os.fspath(vault))
        if key not in seen:
            seen.add(key)
            found.append(vault)
    return found


def config_home(config_path):
    """要掃哪一台機器的家目錄：讀到的那份設定檔說了算（`<home>/.epitype/config.json`）。

    合成設定（測試、`EPITYPE_CONFIG` 指到別處）因此只掃它自己那個家，一場合成的跑
    不會把真機上的庫拉進來改。登記與掃描同源，不另立第二套家目錄推導。
    """
    path = Path(config_path).expanduser()
    try:
        path = path.resolve()
    except OSError:
        pass
    parents = path.parents
    if len(parents) > 1:
        return parents[1]
    return parents[0] if parents else path


# 宿主開在家目錄下的目錄；兩個都不在＝這個家推錯了，原生庫掃描必然一無所獲。
NATIVE_HOST_DIRNAMES = (".claude", ".codex")
CONFIG_HOME_NO_HOST_NOTICE = (
    "Epitype 提醒：由設定檔位置推得的家目錄 {home} 底下沒有 .claude 也沒有 .codex，"
    "掃不到任何原生記憶庫，只有設定檔明列的庫會受管。多半是 EPITYPE_CONFIG 指到了家"
    "目錄以外的地方。"
)


def home_has_host(home):
    """這個家目錄底下有沒有宿主目錄（.claude／.codex）。兩個都沒有＝掃不到原生庫。"""
    base = Path(home)
    return any((base / name).is_dir() for name in NATIVE_HOST_DIRNAMES)


def managed_vaults_for_config(config_path, configured, *, stream=None):
    """CLI 與夜間解析受管庫的唯一入口：由設定檔位置推家目錄、掃原生庫。

    推得的家目錄底下一個宿主目錄都沒有時，原生庫掃描必然空手而回——以前這種情形
    靜默少管，怎麼少了一個庫查不出來（owner 2026-09-21）。現在往 stderr 說一行，
    正常安裝（設定檔在 `<home>/.epitype/` 底下）不會觸發。喚回與各 hook 不走這裡：
    它們以 cwd 解析庫，每則提問付不起全域掃描。
    """
    home = config_home(config_path)
    vaults = managed_vaults(configured, home=home)
    if not home_has_host(home):
        print(CONFIG_HOME_NO_HOST_NOTICE.format(home=home), file=stream or sys.stderr)
    return vaults


def event_cards(vault):
    """庫裡的事件卡（grants/ corrections/ rulings/），路徑排序固定。"""
    vault = Path(vault)
    found = []
    for directory, _card_type in memspec.EVENT_CARD_DIRECTORIES:
        try:
            found.extend(sorted((vault / directory).glob("*.md")))
        except OSError:
            continue
    return found


def card_route(path, vault, home=None):
    """一張卡的歸戶判定：{status, cwd, target}。target 只在 misrouted 時有值。"""
    path = Path(path)
    fields, _problem = memspec.frontmatter_fields(path)
    if not fields:
        # 讀不出 frontmatter 的卡（無界線、非 UTF-8、只有正文）跳過就好：一張壞卡
        # 不該讓整庫的盤點停在半路，但也絕不能憑猜測搬走。
        return {"status": ROUTE_BROKEN, "cwd": "", "target": None}
    cwd = _one_line(fields.get(memspec.CWD_FIELD))
    if not cwd:
        # 回放 Codex 歷史的卡沒有 cwd（宿主只在 session_meta 寫一次）；判不了就不搬。
        return {"status": ROUTE_UNKNOWN, "cwd": "", "target": None}
    target = capture_vault(cwd, vault, home)
    if target == Path(vault).resolve():
        return {"status": ROUTE_OK, "cwd": cwd, "target": None}
    return {"status": ROUTE_MISROUTED, "cwd": cwd, "target": target}


def audit(vault, home=None):
    """唯讀盤點：庫裡每張事件卡按卡上 cwd 判斷它其實屬於哪個庫。"""
    vault = Path(vault).resolve()
    entries = []
    counts = {ROUTE_MISROUTED: 0, ROUTE_OK: 0, ROUTE_UNKNOWN: 0, ROUTE_BROKEN: 0}
    by_target = {}
    for path in event_cards(vault):
        route = card_route(path, vault, home)
        counts[route["status"]] += 1
        entry = {
            "card": path.relative_to(vault).as_posix(),
            "status": route["status"],
            memspec.CWD_FIELD: route["cwd"],
            "target": os.fspath(route["target"]) if route["target"] is not None else "",
        }
        if route["status"] == ROUTE_MISROUTED:
            by_target[entry["target"]] = by_target.get(entry["target"], 0) + 1
        entries.append(entry)
    return {
        "vault": os.fspath(vault),
        "cards": len(entries),
        **counts,
        "by_target": by_target,
        "entries": entries,
    }


def _unique_target(directory, stem):
    """目的地檔名；同名就加 -2、-3…（永不覆蓋、永不刪）。"""
    candidate = directory / f"{stem}.md"
    if not candidate.exists():
        return candidate, stem
    for suffix in range(2, MAX_RENAME_SUFFIX + 1):
        renamed = f"{stem}-{suffix}"
        candidate = directory / f"{renamed}.md"
        if not candidate.exists():
            return candidate, renamed
    return None, stem


def _annotated(text, source_vault, cwd, stamp, renamed_stem=None):
    """先產生完整歸戶內容，再一次發布；不修改已搬到位的卡。"""
    if renamed_stem is not None:
        text = re.sub(
            rf"^{re.escape(memspec.NAME_FIELD)}:.*$",
            f"{memspec.NAME_FIELD}: {renamed_stem}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    if not text.endswith("\n"):
        text += "\n"
    note = REHOME_NOTE.format(
        stamp=stamp, source=os.fspath(source_vault), field=memspec.CWD_FIELD, cwd=cwd
    )
    return text + note + "\n"


def apply_routes(vault, home=None, stamp=None, report=None):
    """把完整的歸戶卡發布到空檔名；碰撞時保留來源及目的地。"""
    try:
        from . import card_io
    except ImportError:
        import card_io
    vault = Path(vault).resolve()
    report = report if report is not None else audit(vault, home)
    stamp = stamp or time.strftime("%Y-%m-%d", time.gmtime())
    lines = []
    moved = renamed = failed = notes_failed = 0
    touched = set()
    for entry in report["entries"]:
        if entry["status"] != ROUTE_MISROUTED:
            continue
        source = vault / entry["card"]
        directory = Path(entry["target"]) / Path(entry["card"]).parent.name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target, stem = _unique_target(directory, source.stem)
            if target is None:
                raise OSError(f"{MAX_RENAME_SUFFIX} 個同名檔都被佔用")
            original = source.read_bytes()
            text = _annotated(
                original.decode("utf-8"), vault, entry[memspec.CWD_FIELD], stamp,
                renamed_stem=stem if stem != source.stem else None,
            )
            card_io.move(source, target, text.encode("utf-8"), expected=original)
        except (OSError, UnicodeError) as exc:
            failed += 1
            lines.append(f"FAILED {entry['card']} -> {entry['target']} {type(exc).__name__}: {exc}")
            continue
        moved += 1
        if stem != source.stem:
            renamed += 1
        touched.add(entry["target"])
        lines.append(f"MOVED {entry['card']} -> {os.fspath(target)}")
    if moved:
        # 兩邊的索引都不再反映庫裡的卡：標舊，讓下一個讀者重建；不標，搬走的卡會
        # 在來源庫的索引裡繼續被喚回，搬到的卡則要等寬限期過了才看得見。
        try:
            from . import memsearch
        except ImportError:  # Direct script execution keeps the CLI contract.
            import memsearch
        for item in (os.fspath(vault), *sorted(touched)):
            memsearch.mark_stale(item)
    lines.append(
        f"APPLIED moved={moved} renamed={renamed} failed={failed} notes_failed={notes_failed}"
    )
    return {
        "moved": moved, "renamed": renamed, "failed": failed,
        "notes_failed": notes_failed, "vaults": sorted(touched),
    }, lines


def render(report, apply_lines=None):
    """稽核清單、歸戶紀錄與統計；--json 之外的唯一輸出格式。"""
    lines = [
        f"MISROUTED {entry['card']} -> {entry['target']}"
        for entry in report["entries"] if entry["status"] == ROUTE_MISROUTED
    ]
    lines.extend(apply_lines or ())
    for target, count in sorted(report["by_target"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"BY-TARGET {count} {target}")
    lines.append(
        f"ROUTE cards={report['cards']} misrouted={report[ROUTE_MISROUTED]} ok={report[ROUTE_OK]} "
        f"unknown={report[ROUTE_UNKNOWN]} broken={report[ROUTE_BROKEN]} vault={report['vault']}"
    )
    return lines


def _card(name, cwd, sentence, kind="correction"):
    return (
        "---\n"
        f"name: {name}\n"
        f"description: owner {kind} auto-captured 2026-08-01: {sentence}\n"
        f"{memspec.SCOPE_FIELD}: governance-core\n"
        "captured_at: 2026-08-01T10:00:00Z\n"
        f"{memspec.CWD_FIELD}: {cwd}\n"
        "session_id: sess-1\n"
        "---\n"
        f"{sentence}\n"
    )


def _selftest():
    try:
        from . import capture
    except ImportError:  # Direct script execution keeps the CLI contract.
        import capture

    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-route-") as temp_dir:
            root = Path(temp_dir).resolve()
            home = root / "home"
            governance = root / "governance"
            governance.mkdir()
            (governance / memspec.MEMORY_INDEX_FILENAME).write_text("# index\n", encoding="utf-8")

            project = root / "work" / "proj"
            (project / "sub").mkdir(parents=True)
            registered = home / ".claude" / "projects" / project_slug(project) / "memory"
            registered.mkdir(parents=True)
            (registered / "project-card.md").write_text(
                "---\nname: project card\ndescription: this vault is registered\n---\nbody\n",
                encoding="utf-8",
            )
            shell_project = root / "work" / "shell"
            shell_project.mkdir(parents=True)
            shell = home / ".claude" / "projects" / project_slug(shell_project) / "memory"
            shell.mkdir(parents=True)
            outsider = root / "elsewhere"
            outsider.mkdir()

            grant = "你可以直接改那個測試檔"
            replay = capture.Replay(reindex=False)
            grant_target = capture.capture_owner_sentence(
                grant,
                capture_vault(os.fspath(project / "sub"), governance, home),
                {"cwd": os.fspath(project / "sub"), "session_id": "sess-project"},
                None,
                "grant",
                replay=replay,
            )
            checks.append((
                "a cwd inside a registered project vault files the card in that project vault",
                capture_vault(os.fspath(project), governance, home) == registered.resolve()
                and capture_vault(os.fspath(project / "sub"), governance, home) == registered.resolve()
                and grant_target is not None
                and grant_target.parent.parent == registered.resolve()
                and not (governance / memspec.GRANT_DIRECTORY).exists(),
            ))

            outside_target = capture.capture_owner_sentence(
                grant,
                capture_vault(os.fspath(outsider), governance, home),
                {"cwd": os.fspath(outsider), "session_id": "sess-outside"},
                None,
                "grant",
                replay=capture.Replay(reindex=False),
            )
            checks.append((
                "a cwd under no registered vault falls back to the governance vault",
                capture_vault(os.fspath(outsider), governance, home) == governance.resolve()
                and outside_target is not None
                and outside_target.parent.parent == governance.resolve(),
            ))

            shell_target = capture.capture_owner_sentence(
                "你可以直接刪掉那個暫存檔",
                capture_vault(os.fspath(shell_project), governance, home),
                {"cwd": os.fspath(shell_project), "session_id": "sess-shell"},
                None,
                "grant",
                replay=capture.Replay(reindex=False),
            )
            checks.append((
                "an unregistered project shell keeps the card in governance and records its cwd",
                capture_vault(os.fspath(shell_project), governance, home) == governance.resolve()
                and shell_target is not None
                and shell_target.parent.parent == governance.resolve()
                and f"{memspec.CWD_FIELD}: {os.fspath(shell_project)}"
                in shell_target.read_text(encoding="utf-8")
                and not any(shell.iterdir()),
            ))

            corrections = governance / memspec.CORRECTION_DIRECTORY
            rulings = governance / memspec.RULING_DIRECTORY
            corrections.mkdir(parents=True, exist_ok=True)
            rulings.mkdir(parents=True, exist_ok=True)
            misrouted = corrections / "correction-20260801-aaaaaaaaaaaa.md"
            misrouted.write_text(
                _card(misrouted.stem, os.fspath(project), "不要再動那個介面"), encoding="utf-8"
            )
            settled = corrections / "correction-20260801-bbbbbbbbbbbb.md"
            settled.write_text(
                _card(settled.stem, os.fspath(outsider), "不要再改那個顏色"), encoding="utf-8"
            )
            headless = rulings / "ruling-20260801-cccccccccccc.md"
            headless.write_text(
                _card(headless.stem, "", "一律先跑測試再回報", kind="ruling"), encoding="utf-8"
            )
            broken = rulings / "ruling-20260801-dddddddddddd.md"
            broken.write_text("沒有 frontmatter 的壞卡\n", encoding="utf-8")

            report = audit(governance, home)
            statuses = {entry["card"]: entry["status"] for entry in report["entries"]}
            checks.append((
                "audit names the misrouted card, its target, and leaves the settled one alone",
                statuses.get(f"{memspec.CORRECTION_DIRECTORY}/{misrouted.name}") == ROUTE_MISROUTED
                and statuses.get(f"{memspec.CORRECTION_DIRECTORY}/{settled.name}") == ROUTE_OK
                and report["by_target"] == {os.fspath(registered.resolve()): 1}
                and report[ROUTE_MISROUTED] == 1,
            ))
            checks.append((
                "a card without cwd is unknown, never a move candidate",
                statuses.get(f"{memspec.RULING_DIRECTORY}/{headless.name}") == ROUTE_UNKNOWN
                and report[ROUTE_UNKNOWN] == 1
                and all(entry["target"] == "" for entry in report["entries"]
                        if entry["status"] == ROUTE_UNKNOWN),
            ))
            checks.append((
                "a card whose frontmatter cannot be read is skipped without stopping the audit",
                statuses.get(f"{memspec.RULING_DIRECTORY}/{broken.name}") == ROUTE_BROKEN
                and report[ROUTE_BROKEN] == 1
                and report["cards"] == len(statuses) == 6
                and broken.exists(),
            ))

            audit_output = io.StringIO()
            audit_code = main(
                [os.fspath(governance), "--audit", "--home", os.fspath(home)], output=audit_output
            )
            audit_lines = audit_output.getvalue().splitlines()
            checks.append((
                "--audit prints the move list and the counts, and moves nothing",
                audit_code == 0
                and f"MISROUTED {memspec.CORRECTION_DIRECTORY}/{misrouted.name} -> "
                f"{os.fspath(registered.resolve())}" in audit_lines
                and any(text.startswith("ROUTE cards=6 misrouted=1 ok=3 unknown=1 broken=1")
                        for text in audit_lines)
                and misrouted.exists()
                and not (registered / memspec.CORRECTION_DIRECTORY).exists(),
            ))

            collision = registered / memspec.CORRECTION_DIRECTORY
            collision.mkdir(parents=True)
            occupied = collision / misrouted.name
            occupied.write_text(
                _card(occupied.stem, os.fspath(project), "另一句完全不同的話"), encoding="utf-8"
            )
            apply_output = io.StringIO()
            apply_code = main(
                [os.fspath(governance), "--apply", "--home", os.fspath(home)], output=apply_output
            )
            apply_lines = apply_output.getvalue().splitlines()
            landed = collision / f"{misrouted.stem}-2.md"
            landed_text = landed.read_text(encoding="utf-8") if landed.exists() else ""
            checks.append((
                "--apply moves the card into the project vault and never deletes the source card",
                apply_code == 0
                and not misrouted.exists()
                and landed.exists()
                and "不要再動那個介面" in landed_text
                and any(text.startswith("MOVED ") and landed.name in text for text in apply_lines)
                and any(text.startswith("APPLIED moved=1 renamed=1 failed=0 notes_failed=0")
                        for text in apply_lines),
            ))
            checks.append((
                "a name collision lands as -2 with its name field aligned, original untouched",
                f"{memspec.NAME_FIELD}: {landed.stem}" in landed_text
                and occupied.exists()
                and "另一句完全不同的話" in occupied.read_text(encoding="utf-8"),
            ))
            checks.append((
                "the moved card carries a one-line rehome note naming the source vault and cwd",
                landed_text.rstrip().splitlines()[-1].startswith("（")
                and os.fspath(governance.resolve()) in landed_text
                and os.fspath(project) in landed_text.rstrip().splitlines()[-1],
            ))
            second_report = audit(governance, home)
            checks.append((
                "after the move the governance audit reports nothing misrouted and keeps the rest",
                second_report[ROUTE_MISROUTED] == 0
                and second_report[ROUTE_OK] == 3
                and second_report[ROUTE_UNKNOWN] == 1
                and second_report[ROUTE_BROKEN] == 1
                and settled.exists()
                and headless.exists(),
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
    parser = argparse.ArgumentParser(prog="epitype capture-route", description=__doc__)
    parser.add_argument("vault", type=Path, nargs="?", help="治理庫（要盤點的庫）")
    parser.add_argument("--audit", action="store_true", help="唯讀盤點：印出建議搬遷清單與統計")
    parser.add_argument("--apply", action="store_true", help="真的把誤置的卡搬到它該在的庫")
    parser.add_argument("--home", type=Path, default=None, help="家目錄（預設 ~），原生專案庫從這裡找")
    parser.add_argument("--json", action="store_true", help="輸出完整報告（含每張卡的判定）")
    parser.add_argument("--selftest", action="store_true", help="run synthetic checks")
    return parser


def main(argv=None, output=sys.stdout):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in arguments:
        return _selftest()
    parsed = _parser().parse_args(arguments)
    if parsed.vault is None:
        print("capture_route: vault is required (or pass --selftest)", file=sys.stderr)
        return 2
    vault = parsed.vault.expanduser()
    home = parsed.home.expanduser() if parsed.home is not None else None
    if not vault.is_dir():
        print(f"ERROR NotADirectoryError: {vault}", file=sys.stderr)
        return 2
    try:
        report = audit(vault, home)
        applied, apply_lines = (
            apply_routes(vault, home, report=report) if parsed.apply else (None, None)
        )
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if parsed.json:
        payload = dict(report)
        if applied is not None:
            payload["applied"] = applied
        print(json.dumps(payload, ensure_ascii=False, indent=1), file=output)
    else:
        for text in render(report, apply_lines):
            print(text, file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
