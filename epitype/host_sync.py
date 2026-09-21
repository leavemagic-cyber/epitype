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
import re
import shutil
import tempfile

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import capture_route, core_gen, memspec

# unrecognised：這一塊現在有幾行認不出是 Epitype 上次寫的；取代前原文要先存起來。
Region = namedtuple("Region", "name text present current unrecognised", defaults=(0,))
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
    regions, problems, notices = [], [], []
    damaged = False
    try:
        wanted = {
            memspec.HOST_SYNC_RULES_REGION: rules_text(vaults, host),
            memspec.HOST_SYNC_INDEX_REGION: index_text(vaults),
        }
    except Exception as exc:
        return Plan(host, path, [], [f"組不出要寫的內容：{type(exc).__name__}: {exc}"], False)

    # 設了 core_cap_bytes 就是規則塊的上限：core-gen 超過會拒寫，而代理真正讀到的是這裡
    # 寫進去的字，這裡不守的話那個上限等於沒設。
    rules_cap = core_gen._cap_of(None, memspec.config_options())
    for name, text in wanted.items():
        size = len(text.encode("utf-8"))
        if size > memspec.HOST_SYNC_REGION_CAP_BYTES:
            problems.append(
                f"{name} 有 {size} 位元組，超過每場固定成本上限 "
                f"{memspec.HOST_SYNC_REGION_CAP_BYTES}；先讓內容瘦身再同步"
            )
            continue
        if name == memspec.HOST_SYNC_RULES_REGION and rules_cap is not None and size > rules_cap:
            problems.append(
                f"{name} 有 {size} 位元組，超過設定的 {memspec.CONFIG_CORE_CAP_BYTES_FIELD} "
                f"{rules_cap}；先讓規則瘦身或調整上限再同步"
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
        unrecognised = 0 if inner is None else _unrecognised(
            inner, text, host, name, home, legacy=_legacy
        )
        regions.append(Region(name, text, found is not None, inner, unrecognised))
    problems.extend(_budget_problems(host, path, raw, regions, home))
    return Plan(host, path, regions, problems, damaged)


def _codex_configured_budget(home=None):
    """Codex 使用者自己設的載入上限（`project_doc_max_bytes`），沒設就回 None。

    2026-09-20 Codex 審查抓到：32 KiB 是那個設定的**預設值**，不是硬上限，官方文件還
    示範改成 65536。把預設當硬上限的話，調高的人會被我們無故拒絕、調低的人則不受保護。
    """
    path = (Path(home) if home else Path.home()) / ".codex" / "config.toml"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^\s*project_doc_max_bytes\s*=\s*(\d+)", raw, re.MULTILINE)
    if match is None:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def _budget_problems(host, path, raw, regions, home=None):
    """整個檔同步後會不會超過宿主的載入上限。

    只守自己那兩塊是不夠的：宿主載入的是整個檔，使用者自己的內容加上去之後超過上限，
    被安靜丟掉的那一段可能正是規則塊——而代理看起來還是「讀了整份」。

    上限優先讀使用者自己設的值，讀不到才用官方預設；沒有公告上限的宿主不編一個出來。
    """
    budget = memspec.HOST_BUDGET_BYTES.get(host)
    if host == "codex":
        budget = _codex_configured_budget(home) or budget
    if not budget:
        return []
    updated = raw
    for region in regions:
        updated = render(updated, region.name, region.text)
    size = len(updated.encode("utf-8"))
    if size <= budget:
        return []
    return [memspec.HOST_BUDGET_NOTICE.format(
        path=path, size=size, host=host, budget=budget)]


def _state_path(home=None):
    return (Path(home) if home else Path.home()) / ".epitype" / STATE_FILENAME


def _written_before(home=None):
    """我們上次寫進每個 (宿主, 區塊) 的內容指紋。"""
    try:
        # 用 with 關掉：這個函式每次同步都跑好幾趟，控制代碼留著在 Windows 上會讓
        # 暫存目錄刪不掉（測試輸出滿是 ResourceWarning 就是這樣來的）。
        with io.open(_state_path(home), encoding="utf-8") as stream:
            loaded = json.loads(stream.read())
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


def _unrecognised(inner, wanted, host, name, home, legacy=False):
    """這一塊現在有幾行認不出是 Epitype 寫的；認得出來回 0。

    認得出來的：空的、跟這次要寫的一樣、指紋等於我們上次寫進去的那份。其餘照樣取代，
    但原文先存起來並寫明誰換的——使用者可能在標記之間放過自己的字，拒絕同步會讓規則永遠
    到不了代理面前，默默覆蓋則是資料損失；存原文＋標註兩邊都不犧牲。
    「以生成器標題行開頭」不算認得：標題後面接的可能是使用者加的字。指紋紀錄不在時
    （解除安裝過、舊標記遷移）舊的生成內容會被多存一份，那是副本，不是損失。
    """
    if not inner.strip():
        return 0
    if _normalised(inner) == _normalised(wanted):
        return 0
    if _written_before(home).get(f"{host}/{name}") == _fingerprint(inner):
        return 0
    return len(inner.strip().splitlines())


def _replaced_path(path):
    return path.with_name(path.name + memspec.HOST_SYNC_REPLACED_SUFFIX)


def _save_replaced(path, regions, actor):
    """認不出的原文附加到宿主檔旁邊的紀錄檔，寫明誰、何時換掉。寫不進去就丟 OSError。"""
    from datetime import datetime

    entries = "".join(
        memspec.HOST_SYNC_REPLACED_ENTRY.format(
            when=datetime.now().astimezone().isoformat(timespec="seconds"),
            region=region.name, actor=actor, path=path, text=region.current.strip("\n"),
        )
        for region in regions
    )
    target = _replaced_path(path)
    with io.open(target, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(entries)
    return target


def _drifted(region):
    return region.current != region.text


def _report_dropped(vaults, output):
    """哪些庫的規則卡沒有進宿主檔。不講的話，使用者會以為每個庫的規則都生效了。

    同一張卡在 Stop 閘與寫檔閘是生效的，規則卡卻只有治理庫那一份進得了宿主檔——兩邊
    作用域不一樣，而輸出只印一行 WROTE 的話，這件事沒有任何地方看得到。
    """
    chosen = {str(path) for path in contract_vaults(vaults)}
    dropped = [Path(vault) for vault in vaults if str(Path(vault)) not in chosen]
    for vault in dropped:
        cards = 0
        try:
            cards = sum(1 for _ in Path(vault).rglob("rule-*.md"))
        except OSError:
            pass
        if not cards:
            continue  # 沒有規則卡就沒有東西被丟掉，講了只是噪音
        # 記憶庫目錄常常都叫 memory，只印目錄名分不出是哪一個庫。
        print(
            f"NOTE   {vault.parent.name}/{vault.name} 有 {cards} 張規則卡沒有進宿主檔："
            "宿主檔是跨專案契約，只收治理庫；這個庫的裁定卡只要還在設定裡的庫清單上，"
            "在兩道閘就仍然生效（閘讀的是那份清單，不是宿主檔）",
            file=output,
        )


def check(vaults, hosts=None, home=None, output=sys.stdout):
    _report_dropped(vaults, output)
    plans = [plan_for(host, vaults, home) for host in (hosts or installed_hosts(home))]
    drift = refused = 0
    for item in plans:
        # 整個宿主停手時不要先講一句沒發生的事：標記壞掉那一種是一個位元組都不動的。
        if not item.damaged:
            for region in item.regions:
                if region.unrecognised:
                    print(f"{memspec.HOST_SYNC_REPLACED_PREFIX} {item.host}: "
                          + memspec.HOST_SYNC_REPLACE_PREVIEW.format(
                              region=region.name, lines=region.unrecognised,
                              saved=_replaced_path(item.path)), file=output)
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


def apply(vaults, hosts=None, home=None, output=sys.stdout, actor=memspec.HOST_SYNC_ACTOR_MANUAL):
    _report_dropped(vaults, output)
    plans = [plan_for(host, vaults, home) for host in (hosts or installed_hosts(home))]
    refused = written = 0
    for item in plans:
        for problem in item.problems:
            print(f"REFUSE {item.host}: {problem}", file=output)
        if item.problems:
            refused += 1
        # 標記壞掉就整個宿主停手（找不準區塊邊界）。「我們要寫的內容有問題」那種才逐塊
        # 跳過——一塊超過上限不該連累另一塊寫不進去。
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
        foreign = [region for region in item.regions if region.unrecognised]
        saved = None
        try:
            item.path.parent.mkdir(parents=True, exist_ok=True)
            if foreign:
                # 先存原文再寫檔：存不進去就不寫，認不出的字不能在沒有副本時被換掉。
                saved = _save_replaced(item.path, foreign, actor)
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
        for region in foreign:
            print(f"{memspec.HOST_SYNC_REPLACED_PREFIX} {item.host}: "
                  + memspec.HOST_SYNC_REPLACE_DONE.format(
                      region=region.name, lines=region.unrecognised, actor=actor, saved=saved),
                  file=output)
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
        backup = path.with_name(path.name + memspec.HOST_SYNC_BACKUP_SUFFIX)
        wrote_backup = False
        try:
            # 只在還沒有備份時寫，跟 apply 同一條規矩。照寫的話，解除安裝會把安裝當初留
            # 下的原檔副本換成「含我們區塊的那一版」——使用者手上唯一一份「Epitype 動它
            # 之前長什麼樣」就這樣沒了，而且是在解除安裝這一步沒的。
            if not backup.exists():
                atomic_write(backup, original)
                wrote_backup = True
            if payload.strip():
                atomic_write(path, payload)
            else:
                # 整個檔都是我們寫的，拿掉就沒東西了——那是產品自己建的孤兒檔。
                # 連它的備份一起收掉：那份備份裡沒有半個字是使用者的，留著只會讓人
                # 以為自己有東西被刪了。
                path.unlink()
                try:
                    backup.unlink()
                except OSError:
                    pass
                print(f"REMOVED {host}: 這個檔整份都是我們建的，已刪除 {path}", file=output)
                removed += 1
                continue
        except OSError as exc:
            print(f"REFUSE {host}: 寫入失敗 {type(exc).__name__}: {exc}", file=output)
            continue
        removed += 1
        print(f"REMOVED {host}: 區塊已拿掉，其餘內容原樣保留 → {path}", file=output)
        # 備份檔留在使用者的目錄裡，就要講。不講的話，解除安裝之後那裡多一個檔，而且
        # 「這次剛寫的、裡面含我們的區塊」跟「安裝當初留的、是你自己的原文」是兩件很不
        # 一樣的事——前者是我們留下的東西，後者是還給你的東西。
        if wrote_backup:
            print(f"        原檔副本留在 {backup}（這是拿掉區塊之前的樣子，"
                  "裡面含我們寫的區塊；確認沒問題就可以刪）", file=output)
        elif backup.exists():
            print(f"        安裝當初的原檔副本仍在 {backup}"
                  "（那是 Epitype 動它之前的樣子；確認沒問題就可以刪）", file=output)
        if _replaced_path(path).exists():
            print(f"        同步時被取代的原文存在 {_replaced_path(path)}"
                  "（每筆寫明誰、何時換掉；確認不需要就可以刪）", file=output)
    if not removed:
        print("宿主檔裡沒有我們寫的區塊，沒有要拿掉的東西", file=output)
    return EXIT_OK


def _selftest():
    import tempfile

    checks = []
    saved_config = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-hostsync-") as temp_dir:
            root = Path(temp_dir).resolve()
            # 這台機器的真設定可能設了 core_cap_bytes；自測不能被真上限左右。
            selftest_config = root / "selftest-config.json"
            selftest_config.write_text("{}", encoding="utf-8")
            os.environ[memspec.EPITYPE_CONFIG_ENV] = str(selftest_config)
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
            keeper_backup = keeper.with_name(keeper.name + memspec.HOST_SYNC_BACKUP_SUFFIX)
            if keeper_backup.exists():
                # 前面的案例留下的備份是別份檔的內容。要驗「移除不覆寫備份」就得從
                # 「這一份檔的備份」開始，不然測到的是上一個案例的殘留。
                keeper_backup.unlink()
            apply([vault], hosts=["claude"], home=home, output=io.StringIO())
            with_block = keeper.read_text(encoding="utf-8")
            remove(hosts=["claude"], home=home, output=io.StringIO())
            after_removal = keeper.read_text(encoding="utf-8")
            checks.append((
                "移除拿掉整個區塊，使用者自己的字原樣留著",
                begin in with_block and begin not in after_removal
                and "我自己的開頭" in after_removal and "中段筆記" in after_removal,
            ))
            # 解除安裝不得把安裝當初留的原檔副本換成「含我們區塊的那一版」。
            checks.append((
                "移除不覆寫備份：留的仍然是我們動它之前那一份",
                keeper_backup.is_file()
                and begin not in keeper_backup.read_text(encoding="utf-8"),
            ))
            # 留在使用者目錄裡的備份檔要講出來，而且要講清楚是哪一種。
            removal_said = io.StringIO()
            keeper.write_text("我自己的開頭\n\n中段筆記\n", encoding="utf-8")
            apply([vault], hosts=["claude"], home=home, output=io.StringIO())
            remove(hosts=["claude"], home=home, output=removal_said)
            checks.append((
                "移除會講出備份檔還在哪，不讓它變成沒人提過的殘留",
                str(keeper_backup) in removal_said.getvalue(),
            ))

            # 舊標記之間放的是使用者自己的字：照樣取代，但原文存進紀錄檔。
            legacy_rules = host_path("claude", home)
            legacy_rules.write_text(
                f"前言\n\n{legacy_begin}\n我手寫在舊標記之間的內容\n{legacy_end}\n",
                encoding="utf-8")
            report = io.StringIO()
            code = apply([vault], hosts=["claude"], home=home, output=report)
            replaced_log = _replaced_path(legacy_rules)
            checks.append((
                "舊標記之間若不是我們寫的：取代，原文留在紀錄檔",
                code == EXIT_OK
                and "我手寫在舊標記之間的內容" not in legacy_rules.read_text(encoding="utf-8")
                and replaced_log.is_file()
                and "我手寫在舊標記之間的內容" in replaced_log.read_text(encoding="utf-8"),
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

            # 1. 標記字串出現在使用者自己寫的內容裡：他的字不能消失。
            demo = fresh("demo", f"我的筆記\n\n示範：區塊長這樣\n{begin}\n我自己的心得\n{end}\n")
            report = io.StringIO()
            code = apply([vault], hosts=["claude"], home=home, output=report)
            kept = demo.read_text(encoding="utf-8")
            checks.append((
                "標記出現在使用者自己的內容裡：區塊外的字原樣留著，區塊內的字存進紀錄檔",
                "我的筆記" in kept
                and "我自己的心得" in _replaced_path(demo).read_text(encoding="utf-8"),
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

            # 7（第四輪審查）。指紋表不在時，索引塊認不出內容也照寫——取捨是對的，
            # 不然解除安裝過一次就再也同步不回來。但推錯時消失的是使用者的字，所以
            # 不准默默做：換掉幾行、原檔在不在，都要當場講。
            index_begin, index_end = memspec.HOST_SYNC_MARKERS[
                memspec.HOST_SYNC_INDEX_REGION]
            silent = fresh(
                "silent",
                f"我的開頭\n\n{index_begin}\n我寫的第一行\n我寫的第二行\n{index_end}\n",
            )
            if _state_path(home).exists():
                _state_path(home).unlink()
            silent_backup = silent.with_name(silent.name + memspec.HOST_SYNC_BACKUP_SUFFIX)
            if silent_backup.exists():
                silent_backup.unlink()
            report = io.StringIO()
            code = apply([vault], hosts=["claude"], home=home, output=report)
            said = report.getvalue()
            checks.append((
                "取代了認不出的索引內容：講出換掉幾行、誰換的、原文存在哪",
                code == EXIT_OK and "我寫的第一行" not in silent.read_text(encoding="utf-8")
                and memspec.HOST_SYNC_REPLACED_PREFIX in said and "2 行" in said
                and memspec.HOST_SYNC_ACTOR_MANUAL in said
                and str(_replaced_path(silent)) in said
                and "我寫的第二行" in _replaced_path(silent).read_text(encoding="utf-8"),
            ))
            # 同一件事在 check（唯讀預覽）也要講，不然使用者是在檔案被改之後才知道。
            fresh("silent2", f"{index_begin}\n我寫的一行\n{index_end}\n")
            if _state_path(home).exists():
                _state_path(home).unlink()
            report = io.StringIO()
            check([vault], hosts=["claude"], home=home, output=report)
            checks.append((
                "check 也先講：不是等檔案被改了才知道",
                memspec.HOST_SYNC_REPLACED_PREFIX in report.getvalue()
                and "1 行" in report.getvalue(),
            ))
            # check 只是預覽：紀錄檔不能因為 check 而多一筆。
            before_log = _replaced_path(silent).read_text(encoding="utf-8")
            checks.append((
                "check 不寫紀錄檔",
                "我寫的一行" not in before_log,
            ))
            # 指紋表在、內容不是我們的：照樣取代，紀錄寫明是誰換的。
            _remember(home, "claude", memspec.HOST_SYNC_INDEX_REGION, "別的東西")
            guarded = fresh("guarded", f"{index_begin}\n使用者自己的索引\n{index_end}\n")
            report = io.StringIO()
            code = apply([vault], hosts=["claude"], home=home, output=report, actor="測試用的取代者")
            log = _replaced_path(guarded).read_text(encoding="utf-8")
            checks.append((
                "指紋表在、內容不是我們的：取代，紀錄寫明原文與取代者",
                code == EXIT_OK
                and "使用者自己的索引" not in guarded.read_text(encoding="utf-8")
                and "使用者自己的索引" in log and "由 測試用的取代者 取代" in log,
            ))

            # 設了 core_cap_bytes：規則塊超過就拒寫，索引照寫；沒設時同一份規則照常寫得進去。
            fresh("corecap", "序言\n")
            if _state_path(home).exists():
                _state_path(home).unlink()
            selftest_config.write_text(
                json.dumps({memspec.CONFIG_CORE_CAP_BYTES_FIELD: 10}), encoding="utf-8")
            report = io.StringIO()
            code = apply([vault], hosts=["claude"], home=home, output=report)
            written = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            checks.append((
                "規則塊超過設定的 core_cap_bytes：拒寫並指名上限，索引照寫",
                code == EXIT_REFUSED and memspec.CONFIG_CORE_CAP_BYTES_FIELD in report.getvalue()
                and begin not in written and "一張卡" in written,
            ))
            selftest_config.write_text("{}", encoding="utf-8")
            code = apply([vault], hosts=["claude"], home=home, output=io.StringIO())
            checks.append((
                "上限拿掉後同一份規則寫得進去",
                code == EXIT_OK and begin in (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"),
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
    finally:
        if saved_config is None:
            os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None)
        else:
            os.environ[memspec.EPITYPE_CONFIG_ENV] = saved_config

    passed = sum(bool(ok) for _, ok in checks)
    total = 29
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _managed_vaults(config_path=None):
    """設定檔說得出來的受管庫；設定讀不到就回空清單（由呼叫端說「要給路徑」）。"""
    path = Path(config_path or memspec.config_path())
    options = memspec.config_options(path)
    configured = options.get(memspec.CONFIG_VAULTS_FIELD)
    return capture_route.managed_vaults(
        configured if isinstance(configured, list) else (),
        home=capture_route.config_home(path),
    )


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
    parser.add_argument("--actor", default=memspec.HOST_SYNC_ACTOR_MANUAL,
                        help="取代認不出的內容時，紀錄裡寫是誰做的")
    parsed = parser.parse_args(arguments)
    try:
        if parsed.remove:
            return remove(hosts=parsed.host, output=output)
        vaults = parsed.vaults
        if not vaults:
            # 沒給路徑＝「這台機器該同步的那些庫」：設定登記的加上掃描到裝著卡的原生庫
            # （owner 2026-09-21：vaults 不是名冊）。寫進宿主檔的仍只有治理庫的規則，
            # 那道界線在 contract_vaults，不因為清單變長而放寬。
            vaults = _managed_vaults()
            if not vaults:
                parser.error("要比對或寫入時必須給至少一個記憶庫路徑")
        if parsed.apply:
            return apply(vaults, hosts=parsed.host, output=output, actor=parsed.actor)
        return check(vaults, hosts=parsed.host, output=output)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
