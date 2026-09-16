import sys; sys.dont_write_bytecode = True
"""把卡片生成的規則與短索引，寫進宿主每一場都會載入的那個檔。

沒有這一段，Epitype 只到「產生得出規則」為止：使用者寫了卡、跑了 core-gen，代理卻
永遠讀不到那些字，因為代理讀的是 `~/.claude/CLAUDE.md` 與 `~/.codex/AGENTS.md`，不是
產生出來的中間檔。引擎有了，傳動軸沒有。

寫法刻意保守，因為那兩個檔是使用者自己的：
  - 只動兩個標記之間，其餘逐位元組不碰；
  - 標記必須剛好一對，重複／巢狀／順序顛倒一律拒絕，絕不猜使用者的意思；
  - 先備份再原子寫入；
  - 每塊有位元組上限，超過就拒絕寫——那個檔每一場都整份載入，寫爆是天天付錢。

第一次 `--apply` 會把區塊建起來（附加在檔尾，或連檔案一起建）。舊的私人標記認得，會
就地換成產品標記，不會在同一個檔裡長出第二塊一樣的內容。
"""

import argparse
from collections import namedtuple
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import core_gen, memspec

Region = namedtuple("Region", "name text present current")
Plan = namedtuple("Plan", "host path regions problems damaged")

STATE_FILENAME = "host_sync_state.json"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_REFUSED = 2


def host_path(host, home=None):
    directory, filename = memspec.HOST_SYNC_FILES[host]
    return (Path(home) if home else Path.home()) / directory / filename


def installed_hosts(home=None):
    """宿主目錄存在才算裝了那個宿主；不替使用者生出他沒有的宿主目錄。"""
    root = Path(home) if home else Path.home()
    return [
        host for host, (directory, _filename) in sorted(memspec.HOST_SYNC_FILES.items())
        if (root / directory).is_dir()
    ]


def _normalised(text):
    return str(text or "").replace("\r\n", "\n").strip("\n")


def read_host(path):
    """(原始位元組, 解碼後文字, 行尾風格)。讀不到就回 (None, None, None)。

    位元組留著，因為備份必須是原檔的位元組副本——備份若經過任何正規化，出事時就還原
    不回原狀，那份備份等於沒有。行尾風格也留著：使用者的檔是 CRLF 就要寫回 CRLF，
    我們只負責標記之間，不該順手把他整份檔案的每一行結尾都改掉。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None, None, None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"不是 UTF-8（{exc.reason}，位置 {exc.start}）") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    return raw, text.replace("\r\n", "\n"), newline


def atomic_write(path, data):
    """寫進同目錄的暫存檔、落盤、換上去；失敗不留垃圾，也不留半份檔。

    與 install/graft.py 的 `_atomic_write` 同一套：單行式 `io.open(...).write(...)` 不
    保證資料真的落盤，磁碟滿的時候截斷的內容會被 os.replace 扶正成正本。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".epitype_tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def contract_vaults(vaults):
    """宿主檔該載入哪一個庫的規則：拿著工作帳本的那一個（治理庫）。

    宿主檔是跨專案的契約，專案庫的規則卡屬於那個專案，不該被升格成全域規則。從每一個
    登記庫收會把專案規則寫進使用者的 CLAUDE.md——2026-09-17 拿副本演練時實際看到三條
    titan 專案規則要被寫進全域契約，常駐條數從 50 變 53。認不出治理庫時退回第一個，
    因為單庫使用者的那一個本來就是治理庫。
    """
    paths = [Path(vault) for vault in vaults]
    for path in paths:
        if (path / memspec.WORK_LEDGER_FILENAME).is_file():
            return [path]
    return paths[:1]


def rules_text(vaults, host):
    """這個宿主實際該載入的規則塊：共用區加它自己的宿主區。

    走 core_gen 的 `host_view`，與 `epitype core-gen` 是同一條判斷；兩邊若各寫一份，
    同一張卡會在產生時屬於一個宿主、在同步時屬於另一個。
    """
    rules, unreadable = core_gen.collect_rules(contract_vaults(vaults))
    if unreadable:
        # 讀不到一部分卡就不是「規則少幾條」，是這份規則塊不完整。寧可拒絕同步，也不要
        # 把殘缺的核心寫進宿主檔——少一條規則不會有人發現。
        raise ValueError("讀不到部分卡片：" + "；".join(unreadable[:3]))
    assembled = core_gen.assemble(rules)
    return _normalised(core_gen.host_view(assembled, host))


def index_text(vaults):
    """短索引的本文：治理庫的 MEMORY.md，去掉它的第一行標題。"""
    for vault in contract_vaults(vaults):
        path = Path(vault) / memspec.HOST_SYNC_INDEX_FILENAME
        try:
            raw = io.open(path, encoding="utf-8").read()
        except OSError:
            continue
        lines = _normalised(raw).split("\n")
        if lines and lines[0].startswith("#"):
            lines = lines[1:]
        return _normalised("\n".join(lines))
    return ""


def split_region(raw, begin, end):
    """(前段, 區塊內容, 後段)；區塊不存在回 None，標記不合法就 raise。"""
    opens, closes = raw.count(begin), raw.count(end)
    if opens == 0 and closes == 0:
        return None
    if opens != 1 or closes != 1:
        raise ValueError(f"BEGIN={opens} END={closes}")
    start, finish = raw.index(begin), raw.index(end)
    if finish < start:
        raise ValueError("順序顛倒")
    inner = raw[start + len(begin):finish]
    if begin in inner or end in inner:
        raise ValueError("巢狀")
    return raw[:start], _normalised(inner), raw[finish + len(end):]


def _locate(raw, name):
    """這一塊目前在檔裡的位置：先找產品標記，再找舊的私人標記。

    回傳 (切好的三段, 用到的標記, 是不是舊標記)。兩套標記同時存在時以產品標記為準，
    舊的那一塊由 `render` 整段移除——留著的話代理會讀到兩份規則，而且兩份還會分岔。
    """
    for legacy, markers in ((False, memspec.HOST_SYNC_MARKERS[name]),
                            (True, memspec.HOST_SYNC_LEGACY_MARKERS[name])):
        found = split_region(raw, *markers)
        if found is not None:
            return found, markers, legacy
    return None, memspec.HOST_SYNC_MARKERS[name], False


def _drop(raw, begin, end):
    """整段移除（含標記）；不存在或壞掉就原樣回傳。"""
    try:
        found = split_region(raw, begin, end)
    except ValueError:
        return raw
    if found is None:
        return raw
    head, _inner, tail = found
    return head.rstrip("\n") + ("\n\n" if head.strip() else "") + tail.lstrip("\n")


def render(raw, name, wanted):
    """把這一塊換成 `wanted`，回傳整份新內容。區塊不存在就建在檔尾。"""
    begin, end = memspec.HOST_SYNC_MARKERS[name]
    block = f"{begin}\n{wanted}\n{end}" if wanted else f"{begin}\n{end}"
    found, _markers, legacy = _locate(raw, name)
    if found is None:
        base = raw.rstrip("\n")
        return (base + "\n\n" if base else "") + block + "\n"
    head, _current, tail = found
    updated = head.rstrip("\n") + ("\n\n" if head.strip() else "") + block + "\n" + tail.lstrip("\n")
    if not legacy:
        # 產品標記在手，舊標記那一塊就該消失，不是留在旁邊各說各話。
        updated = _drop(updated, *memspec.HOST_SYNC_LEGACY_MARKERS[name])
    return updated


def plan_for(host, vaults, home=None):
    """這個宿主檔要改什麼：每塊的現況與應有內容，以及擋住寫入的問題。"""
    path = host_path(host, home)
    try:
        _raw_bytes, raw, _newline = read_host(path)
    except ValueError as exc:
        return Plan(host, path, [], [f"讀不動這個檔：{exc}；請先自行處理編碼再同步"], True)
    raw = raw or ""
    regions, problems = [], []
    damaged = False
    try:
        wanted = {
            memspec.HOST_SYNC_RULES_REGION: rules_text(vaults, host),
            memspec.HOST_SYNC_INDEX_REGION: index_text(vaults),
        }
    except Exception as exc:
        return Plan(host, path, [], [f"組不出要寫的內容：{type(exc).__name__}: {exc}"], False)

    for name, text in wanted.items():
        size = len(text.encode("utf-8"))
        if size > memspec.HOST_SYNC_REGION_CAP_BYTES:
            problems.append(
                f"{name} 有 {size} 位元組，超過每場固定成本上限 "
                f"{memspec.HOST_SYNC_REGION_CAP_BYTES}；先讓內容瘦身再同步"
            )
            continue
        # 要寫進去的內容自己含標記，寫下去就會讓這個檔永遠有兩組標記，之後每次都拒絕、
        # 只能人工手改才救得回來。寧可現在就說不。
        # 比對每一塊的標記，不只自己那一塊：索引的內容裡出現規則的標記，一樣會把這個
        # 檔弄成兩組標記。
        carried = [
            marker
            for table in (memspec.HOST_SYNC_MARKERS, memspec.HOST_SYNC_LEGACY_MARKERS)
            for markers in table.values()
            for marker in markers
            if marker in text
        ]
        if carried:
            problems.append(
                f"{name} 要寫的內容裡出現了區塊標記本身（{carried[0][:40]}…）；"
                "寫下去會讓這個檔永遠有兩組標記，請先把卡片裡的那段字改掉"
            )
            continue
        try:
            found, _markers, _legacy = _locate(raw, name)
        except ValueError as exc:
            # 數字要報「實際壞掉的那一組標記」，不然訊息會自相矛盾。
            counts = {
                label: (raw.count(begin), raw.count(end))
                for label, (begin, end) in (
                    ("產品", memspec.HOST_SYNC_MARKERS[name]),
                    ("舊版", memspec.HOST_SYNC_LEGACY_MARKERS[name]),
                )
            }
            label, (opens, closes) = max(counts.items(), key=lambda kv: sum(kv[1]))
            problems.append(memspec.HOST_SYNC_MISSING_MARKER_REASON.format(
                path=path, region=f"{name}（{label}標記）", begin=opens, end=closes
            ) + f"：{exc}")
            damaged = True  # 檔案本身壞了：整個宿主一個位元組都不要動
            continue
        inner = found[1] if found else None
        if inner is not None and not _ours(inner, text, host, name, home, legacy=_legacy):
            problems.append(
                f"{name} 區塊裡的內容不是我們寫的（可能是你自己在檔案裡引用過這對標記）；"
                "覆蓋它就是把你的字弄不見，所以這裡停手。請把那段內容移出標記之間，或改用別的字說明"
            )
            # 這一塊停手，但不連累另一塊：規則塊進不去的代價是「規則從來沒到過代理面
            # 前」，不該由索引塊的問題造成。整檔停手只留給標記本身壞掉那一種。
            continue
        regions.append(Region(name, text, found is not None, inner))
    return Plan(host, path, regions, problems, damaged)


def _state_path(home=None):
    return (Path(home) if home else Path.home()) / ".epitype" / STATE_FILENAME


def _written_before(home=None):
    """我們上次寫進每個 (宿主, 區塊) 的內容指紋。"""
    try:
        loaded = json.loads(io.open(_state_path(home), encoding="utf-8").read())
    except (OSError, ValueError):
        return {}
    written = loaded.get("written") if isinstance(loaded, dict) else None
    return written if isinstance(written, dict) else {}


def _remember(home, host, name, text):
    state = {"written": dict(_written_before(home))}
    state["written"][f"{host}/{name}"] = _fingerprint(text)
    try:
        atomic_write(_state_path(home),
                     json.dumps(state, ensure_ascii=False).encode("utf-8"))
    except OSError:
        pass


def _fingerprint(text):
    return hashlib.sha256(_normalised(text).encode("utf-8")).hexdigest()[:16]


def _ours(inner, wanted, host, name, home, legacy=False):
    """這一塊裡面的內容是不是我們寫的。

    只有認得出是自己寫的才覆蓋。使用者可能在自己的檔案裡引用過我們的標記（說明文件、
    範本、教學），那一對標記中間是他的字——照覆蓋就是資料損失，而且退出碼還是 0。
    認得出來的三種：空的、跟這次要寫的一樣、指紋等於我們上次寫進去的那份；規則塊另外
    認生成器自己的標題行，好讓既有安裝與舊標記遷移得過去。
    """
    if not inner.strip():
        return True
    if legacy and name == memspec.HOST_SYNC_RULES_REGION:
        # 舊標記是前一版同步工具寫下的，但「標記是舊的」不等於「裡面的字是我們的」：
        # 使用者把自己的段落放進舊標記之間，照樣會被整段覆蓋而且回報成功。規則塊認得
        # 出生成器的標題行，就用它判；索引塊沒有這種特徵，往下走一般的擁有權判定。
        return inner.lstrip().startswith(memspec.CORE_GEN_OUTPUT_TITLE)
    if _normalised(inner) == _normalised(wanted):
        return True
    written = _written_before(home)
    if written.get(f"{host}/{name}") == _fingerprint(inner):
        return True
    if name == memspec.HOST_SYNC_RULES_REGION:
        return inner.lstrip().startswith(memspec.CORE_GEN_OUTPUT_TITLE)
    # 指紋表整份不見時（解除安裝會連它一起刪掉），索引塊沒有任何別的辨識特徵，會被判成
    # 「不是我們寫的」而永遠拒絕同步，只能人工改檔才救得回來，理由句還把責任推給使用者。
    # 這個區塊是我們的標記圍出來的，指紋表不在就以標記為準——標記本身有成對與巢狀檢查，
    # 而「使用者自己引用過標記」那一種，會在指紋表存在時照樣擋下來。
    return not written


def _drifted(region):
    return region.current != region.text


def check(vaults, hosts=None, home=None, output=sys.stdout):
    plans = [plan_for(host, vaults, home) for host in (hosts or installed_hosts(home))]
    drift = refused = 0
    for item in plans:
        for problem in item.problems:
            print(f"REFUSE {item.host}: {problem}", file=output)
            refused += 1
        for region in item.regions:
            if not region.present:
                print(f"DRIFT  {item.host}: {region.name} 區塊還沒建立", file=output)
                drift += 1
            elif _drifted(region):
                print(f"DRIFT  {item.host}: {region.name} 與卡片不一致", file=output)
                drift += 1
    if not plans:
        print("沒有偵測到任何宿主（家目錄底下沒有 .claude／.codex）", file=output)
    elif not drift and not refused:
        print(f"OK 宿主檔與卡片一致（{'、'.join(item.host for item in plans)}）", file=output)
    return EXIT_REFUSED if refused else (EXIT_DRIFT if drift else EXIT_OK)


def apply(vaults, hosts=None, home=None, output=sys.stdout):
    plans = [plan_for(host, vaults, home) for host in (hosts or installed_hosts(home))]
    refused = written = 0
    for item in plans:
        for problem in item.problems:
            print(f"REFUSE {item.host}: {problem}", file=output)
        if item.problems:
            refused += 1
        # 檔案本身壞了（標記不成對、區塊裡是別人的字）就整個宿主停手。只有「我們要寫的
        # 內容有問題」那種才逐塊跳過——一塊超過上限不該連累另一塊寫不進去。
        if item.damaged or not item.regions:
            continue
        # 一塊有問題不該連累另一塊：索引超過上限時，規則塊照樣要進得去，不然新使用者
        # 會落到「規則從來沒到過代理面前」而且看不出原因。
        try:
            original, raw, newline = read_host(item.path)
        except ValueError as exc:
            print(f"REFUSE {item.host}: 讀不動這個檔：{exc}", file=output)
            refused += 1
            continue
        raw = raw or ""
        updated = raw
        changed = [region.name for region in item.regions if not region.present or _drifted(region)]
        for region in item.regions:
            updated = render(updated, region.name, region.text)
        if updated == raw:
            print(f"OK     {item.host}: 已經一致", file=output)
            continue
        payload = updated.replace("\n", newline or "\n").encode("utf-8")
        try:
            item.path.parent.mkdir(parents=True, exist_ok=True)
            backup = item.path.with_name(item.path.name + memspec.HOST_SYNC_BACKUP_SUFFIX)
            if original is not None and not backup.exists():
                # 備份是原檔的位元組副本，不經任何正規化——備份若被動過（例如把 CRLF
                # 換成 LF），出事時就還原不回原狀，那份備份等於沒有。
                #
                # 只在還沒有備份時寫：留的是「我們第一次動它之前」那一份。每次都覆寫的
                # 話，夢每晚自動同步一次，兩天之後備份裡就只剩我們自己寫過的內容，使用者
                # 原本的樣子再也找不回來。
                atomic_write(backup, original)
            atomic_write(item.path, payload)
        except OSError as exc:
            print(f"REFUSE {item.host}: 寫入失敗 {type(exc).__name__}: {exc}", file=output)
            refused += 1
            continue
        for region in item.regions:
            _remember(home, item.host, region.name, region.text)
        written += 1
        print(f"WROTE  {item.host}: {'、'.join(changed) or '標記整理'} → {item.path}", file=output)
    if not plans:
        print("沒有偵測到任何宿主（家目錄底下沒有 .claude／.codex）", file=output)
    return EXIT_REFUSED if refused else EXIT_OK


def remove(hosts=None, home=None, output=sys.stdout):
    """把我們寫進宿主檔的區塊整段拿掉，其餘一個位元組都不動。

    沒有這條路徑的話，解除安裝之後那兩個檔會永遠留著一段「由卡片生成、勿手改」的文
    字，而生成它的東西已經不在了——每一場都載入一份沒有主人的規則。產品寫得進使用者
    的全域指令檔，就必須拿得回來。
    """
    removed = 0
    for host in (hosts or installed_hosts(home)):
        path = host_path(host, home)
        try:
            original, raw, newline = read_host(path)
        except ValueError as exc:
            print(f"REFUSE {host}: 讀不動這個檔：{exc}", file=output)
            continue
        if original is None:
            continue
        updated = raw
        for name in memspec.HOST_SYNC_MARKERS:
            for markers in (memspec.HOST_SYNC_MARKERS[name],
                            memspec.HOST_SYNC_LEGACY_MARKERS[name]):
                updated = _drop(updated, *markers)
        if updated == raw:
            continue
        payload = updated.strip("\n")
        payload = (payload + "\n").encode("utf-8") if payload else b""
        payload = payload.replace(b"\n", (newline or "\n").encode("utf-8"))
        try:
            atomic_write(path.with_name(path.name + memspec.HOST_SYNC_BACKUP_SUFFIX), original)
            if payload.strip():
                atomic_write(path, payload)
            else:
                # 整個檔都是我們寫的，拿掉就沒東西了——那是產品自己建的孤兒檔。
                path.unlink()
                print(f"REMOVED {host}: 這個檔整份都是我們建的，已刪除 {path}", file=output)
                removed += 1
                continue
        except OSError as exc:
            print(f"REFUSE {host}: 寫入失敗 {type(exc).__name__}: {exc}", file=output)
            continue
        removed += 1
        print(f"REMOVED {host}: 區塊已拿掉，其餘內容原樣保留 → {path}", file=output)
    if not removed:
        print("宿主檔裡沒有我們寫的區塊，沒有要拿掉的東西", file=output)
    return EXIT_OK


def _selftest():
    import tempfile

    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-hostsync-") as temp_dir:
            root = Path(temp_dir).resolve()
            home = root / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".codex").mkdir(parents=True)
            vault = root / "vault"
            vault.mkdir()
            (vault / "rule-a.md").write_text(
                "---\nname: rule-a\ndescription: 2026-09-17 一條底線\nlayer: floor\n"
                "section: evidence\norder: 10\ntext: Honesty governs this contract.\n"
                "decided_by: owner-explicit\napproved_by: owner\napproved_at: 2026-09-17\n"
                "aliases: [誠實]\nmetadata:\n  type: rule\n---\nbody\n",
                encoding="utf-8",
            )
            (vault / memspec.HOST_SYNC_INDEX_FILENAME).write_text(
                "# 入口\n\n- [一張卡](a.md)\n", encoding="utf-8"
            )

            checks.append((
                "兩個宿主目錄都在，就認得兩個宿主",
                installed_hosts(home) == ["claude", "codex"],
            ))

            # 專案庫的規則不得被升格成跨專案契約。
            project = root / "project-vault"
            project.mkdir()
            (project / "rule-project-only.md").write_text(
                "---\nname: rule-project-only\ndescription: 2026-09-17 只屬於這個專案\n"
                "layer: resident\nsection: execution\norder: 10\n"
                "text: Project scoped rule that must not reach the global contract.\n"
                "decided_by: owner-explicit\napproved_by: owner\napproved_at: 2026-09-17\n"
                "aliases: [專案規則]\nmetadata:\n  type: rule\n---\nbody\n",
                encoding="utf-8",
            )
            (vault / memspec.WORK_LEDGER_FILENAME).write_text("# 帳本\n", encoding="utf-8")
            contract = rules_text([project, vault], "claude")
            checks.append((
                "多個庫時只收治理庫（拿著工作帳本的那一個）的規則",
                contract_vaults([project, vault]) == [vault]
                and "Project scoped rule" not in contract,
            ))

            code = apply([vault], home=home, output=io.StringIO())
            claude = host_path("claude", home)
            body = claude.read_text(encoding="utf-8")
            begin, end = memspec.HOST_SYNC_MARKERS[memspec.HOST_SYNC_RULES_REGION]
            checks.append((
                "第一次 apply 連檔案一起建，規則與索引各一塊",
                code == EXIT_OK and claude.is_file()
                and body.count(begin) == 1 and body.count(end) == 1
                and "Honesty governs this contract." in body
                and "[一張卡](a.md)" in body,
            ))
            checks.append((
                "索引去掉了 MEMORY.md 的標題行",
                "# 入口" not in body,
            ))
            checks.append((
                "一致之後 check 回 0",
                check([vault], home=home, output=io.StringIO()) == EXIT_OK,
            ))

            # 使用者自己寫的東西不能被動到。
            claude.write_text("我自己的筆記\n\n" + body, encoding="utf-8")
            (vault / "rule-a.md").write_text(
                (vault / "rule-a.md").read_text(encoding="utf-8").replace(
                    "Honesty governs this contract.", "Honesty governs everything here."),
                encoding="utf-8",
            )
            drifted = check([vault], home=home, output=io.StringIO())
            apply([vault], home=home, output=io.StringIO())
            after = claude.read_text(encoding="utf-8")
            checks.append((
                "卡片改了就報漂移，apply 之後只有區塊內容變，使用者自己的字原樣留著",
                drifted == EXIT_DRIFT
                and after.startswith("我自己的筆記")
                and "Honesty governs everything here." in after
                and "Honesty governs this contract." not in after,
            ))
            backup = claude.with_name(claude.name + memspec.HOST_SYNC_BACKUP_SUFFIX)
            checks.append(("寫之前先備份", backup.is_file()))

            # 舊的私人標記就地換成產品標記，不長出第二塊。
            legacy_begin, legacy_end = memspec.HOST_SYNC_LEGACY_MARKERS[
                memspec.HOST_SYNC_RULES_REGION]
            codex = host_path("codex", home)
            # 舊區塊裡放的是前一版同步工具真的會寫的東西（生成器自己的標題行開頭）。
            old_block = memspec.CORE_GEN_OUTPUT_TITLE + "\n舊內容\n"
            codex.write_text(
                f"序言\n\n{legacy_begin}\n{old_block}{legacy_end}\n\n結尾\n", encoding="utf-8")
            apply([vault], hosts=["codex"], home=home, output=io.StringIO())
            migrated = codex.read_text(encoding="utf-8")
            checks.append((
                "舊標記就地換成產品標記，內容只有一份",
                migrated.count(begin) == 1 and legacy_begin not in migrated
                and "舊內容" not in migrated
                and migrated.startswith("序言") and "結尾" in migrated,
            ))

            # 標記不成對：整個宿主不寫，也不猜。
            broken = host_path("claude", home)
            broken.write_text(f"{begin}\nA\n{end}\n{begin}\nB\n{end}\n", encoding="utf-8")
            before = broken.read_text(encoding="utf-8")
            report = io.StringIO()
            code = apply([vault], hosts=["claude"], home=home, output=report)
            checks.append((
                "標記不成對就拒絕，檔案一個位元組都不動",
                code == EXIT_REFUSED
                and broken.read_text(encoding="utf-8") == before
                and "REFUSE" in report.getvalue(),
            ))

            # 超過每場固定成本上限：拒絕寫，說清楚是哪一塊。
            big = root / "big-vault"
            big.mkdir()
            (big / memspec.HOST_SYNC_INDEX_FILENAME).write_text(
                "# 入口\n\n" + ("x" * (memspec.HOST_SYNC_REGION_CAP_BYTES + 10)),
                encoding="utf-8")
            report = io.StringIO()
            code = check([big], hosts=["codex"], home=home, output=report)
            checks.append((
                "區塊超過上限就拒絕，並指名是哪一塊",
                code == EXIT_REFUSED and "index" in report.getvalue(),
            ))

            # 第二輪審查：寫得進去就要拿得回來，而且拿回來只能動我們的區塊。
            keeper = host_path("claude", home)
            keeper.write_text("我自己的開頭\n\n中段筆記\n", encoding="utf-8")
            apply([vault], hosts=["claude"], home=home, output=io.StringIO())
            with_block = keeper.read_text(encoding="utf-8")
            remove(hosts=["claude"], home=home, output=io.StringIO())
            after_removal = keeper.read_text(encoding="utf-8")
            checks.append((
                "移除拿掉整個區塊，使用者自己的字原樣留著",
                begin in with_block and begin not in after_removal
                and "我自己的開頭" in after_removal and "中段筆記" in after_removal,
            ))

            # 舊標記之間放的是使用者自己的字時，一樣不得覆蓋。
            legacy_rules = host_path("claude", home)
            legacy_rules.write_text(
                f"前言\n\n{legacy_begin}\n我手寫在舊標記之間的內容\n{legacy_end}\n",
                encoding="utf-8")
            report = io.StringIO()
            code = apply([vault], hosts=["claude"], home=home, output=report)
            checks.append((
                "舊標記之間若不是我們寫的，一樣停手",
                code == EXIT_REFUSED
                and "我手寫在舊標記之間的內容" in legacy_rules.read_text(encoding="utf-8"),
            ))

            # 備份留的是「第一次動它之前」那一份，不會被第二次同步蓋掉。
            pristine = host_path("claude", home)
            pristine.write_text("使用者原本的樣子\n", encoding="utf-8")
            backup_file = pristine.with_name(pristine.name + memspec.HOST_SYNC_BACKUP_SUFFIX)
            if backup_file.exists():
                backup_file.unlink()
            apply([vault], hosts=["claude"], home=home, output=io.StringIO())
            (vault / "rule-a.md").write_text(
                (vault / "rule-a.md").read_text(encoding="utf-8").replace(
                    "Honesty governs everything here.", "Honesty governs it all."),
                encoding="utf-8")
            apply([vault], hosts=["claude"], home=home, output=io.StringIO())
            checks.append((
                "第二次同步不覆寫備份：留的是使用者原本的樣子",
                backup_file.read_text(encoding="utf-8") == "使用者原本的樣子\n",
            ))

            # 指紋表被刪掉（解除安裝就會刪）不得讓同步永久卡死。
            state = _state_path(home)
            apply([vault], hosts=["claude"], home=home, output=io.StringIO())
            if state.exists():
                state.unlink()
            report = io.StringIO()
            code = apply([vault], hosts=["claude"], home=home, output=report)
            checks.append((
                "指紋表不見了，同步照樣做得下去，不是永久拒絕",
                code == EXIT_OK and "REFUSE" not in report.getvalue(),
            ))

            # 以下六項來自 2026-09-17 的對抗審查。原本的 selftest 十項全綠卻一項都
            # 沒蓋到——它測的是作者預期的路徑，等於自我認證。
            def fresh(name, text, binary=False):
                target = home / ".claude" / "CLAUDE.md"
                target.write_bytes(text) if binary else target.write_text(text, encoding="utf-8")
                return target

            # 1. 標記字串出現在使用者自己寫的內容裡，不得把他的字吃掉。
            demo = fresh("demo", f"我的筆記\n\n示範：區塊長這樣\n{begin}\n我自己的心得\n{end}\n")
            report = io.StringIO()
            code = apply([vault], hosts=["claude"], home=home, output=report)
            kept = demo.read_text(encoding="utf-8")
            checks.append((
                "標記出現在使用者自己的內容裡：他的字要留著，不是被整段覆蓋",
                "我的筆記" in kept and "我自己的心得" in kept,
            ))

            # 2. CRLF 的檔：行尾不得被整份改掉，備份要是原檔的位元組副本。
            crlf_body = "我的標題\r\n\r\n第二行\r\n"
            target = fresh("crlf", crlf_body.encode("utf-8"), binary=True)
            original_bytes = target.read_bytes()
            # 備份只在第一次動這個檔時寫，所以這一案要從沒有備份的狀態開始。
            stale_backup = target.with_name(target.name + memspec.HOST_SYNC_BACKUP_SUFFIX)
            if stale_backup.exists():
                stale_backup.unlink()
            apply([vault], hosts=["claude"], home=home, output=io.StringIO())
            after_bytes = target.read_bytes()
            backup_path = target.with_name(target.name + memspec.HOST_SYNC_BACKUP_SUFFIX)
            checks.append((
                "CRLF 檔的行尾保持 CRLF，備份與原檔逐位元組相同",
                b"\r\n" in after_bytes
                and "第二行\r\n".encode("utf-8") in after_bytes
                and backup_path.read_bytes() == original_bytes,
            ))

            # 3. 非 UTF-8 的檔：明說讀不動，不是丟一個看不懂的例外。
            fresh("big5", "中文內容\n".encode("big5"), binary=True)
            report = io.StringIO()
            code = apply([vault], hosts=["claude"], home=home, output=report)
            checks.append((
                "非 UTF-8 的宿主檔：明說讀不動並拒絕，不丟例外",
                code == EXIT_REFUSED and "不是 UTF-8" in report.getvalue(),
            ))

            # 4. 要寫的內容自己含標記：現在就拒絕，不要製造一個永遠修不好的檔。
            poisoned = root / "poisoned"
            poisoned.mkdir()
            (poisoned / memspec.HOST_SYNC_INDEX_FILENAME).write_text(
                f"# 入口\n\n說明：Epitype 會寫在 {begin} 與 {end} 之間。\n", encoding="utf-8")
            report = io.StringIO()
            code = check([poisoned], hosts=["codex"], home=home, output=report)
            checks.append((
                "要寫的內容自己含標記就拒絕，理由指名是哪一塊",
                code == EXIT_REFUSED and "區塊標記本身" in report.getvalue(),
            ))

            # 5. 舊標記與新標記並存：舊的那一塊要消失，不能兩份規則各說各話。
            generated = memspec.CORE_GEN_OUTPUT_TITLE
            both = fresh(
                "both",
                f"序言\n\n{legacy_begin}\n{generated}\n舊規則\n{legacy_end}\n\n"
                f"{begin}\n{generated}\n新規則\n{end}\n",
            )
            apply([vault], hosts=["claude"], home=home, output=io.StringIO())
            merged = both.read_text(encoding="utf-8")
            checks.append((
                "新舊標記並存時，舊的那一塊整段移除",
                legacy_begin not in merged and "舊規則" not in merged
                and merged.count(begin) == 1 and "序言" in merged,
            ))

            # 6. 一塊超過上限不該連累另一塊。
            report = io.StringIO()
            fresh("cap", "使用者序言\n")
            code = apply([big], hosts=["claude"], home=home, output=report)
            capped = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            checks.append((
                "索引超過上限時，規則塊照樣寫得進去",
                "REFUSE" in report.getvalue() and begin in capped
                and "使用者序言" in capped,
            ))

            # 沒有宿主目錄的家目錄：不生出使用者沒有的宿主。
            bare = root / "bare-home"
            bare.mkdir()
            checks.append((
                "家目錄底下沒有宿主目錄就什麼都不做",
                installed_hosts(bare) == []
                and check([vault], home=bare, output=io.StringIO()) == EXIT_OK,
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 21
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main(argv=None, output=sys.stdout):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    parser = argparse.ArgumentParser(description="把卡片生成的規則與索引同步進宿主檔")
    parser.add_argument("vaults", nargs="*", type=Path,
                        help="記憶庫路徑；`--remove` 不需要（拿掉區塊不必讀卡片）")
    parser.add_argument("--apply", action="store_true", help="實際寫入（預設只比對）")
    parser.add_argument("--remove", action="store_true",
                        help="把寫進宿主檔的區塊整段拿掉，其餘內容原樣保留")
    parser.add_argument("--host", action="append", choices=sorted(memspec.HOST_SYNC_FILES),
                        help="只處理這個宿主，可重複；預設處理偵測得到的全部")
    parsed = parser.parse_args(arguments)
    try:
        if parsed.remove:
            return remove(hosts=parsed.host, output=output)
        if not parsed.vaults:
            parser.error("要比對或寫入時必須給至少一個記憶庫路徑")
        runner = apply if parsed.apply else check
        return runner(parsed.vaults, hosts=parsed.host, output=output)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
