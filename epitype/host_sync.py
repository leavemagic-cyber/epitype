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
import io
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import core_gen, memspec

Region = namedtuple("Region", "name text present current")
Plan = namedtuple("Plan", "host path regions problems")

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


def rules_text(vaults, host):
    """這個宿主實際該載入的規則塊：共用區加它自己的宿主區。

    走 core_gen 的 `host_view`，與 `epitype core-gen` 是同一條判斷；兩邊若各寫一份，
    同一張卡會在產生時屬於一個宿主、在同步時屬於另一個。
    """
    rules, unreadable = core_gen.collect_rules([Path(vault) for vault in vaults])
    if unreadable:
        # 讀不到一部分卡就不是「規則少幾條」，是這份規則塊不完整。寧可拒絕同步，也不要
        # 把殘缺的核心寫進宿主檔——少一條規則不會有人發現。
        raise ValueError("讀不到部分卡片：" + "；".join(unreadable[:3]))
    assembled = core_gen.assemble(rules)
    return _normalised(core_gen.host_view(assembled, host))


def index_text(vaults):
    """短索引的本文：第一個有 MEMORY.md 的庫，去掉它的第一行標題。"""
    for vault in vaults:
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
    """這一塊目前在檔裡的位置：先找產品標記，再找舊的私人標記。"""
    for markers in (memspec.HOST_SYNC_MARKERS[name], memspec.HOST_SYNC_LEGACY_MARKERS[name]):
        found = split_region(raw, *markers)
        if found is not None:
            return found, markers
    return None, memspec.HOST_SYNC_MARKERS[name]


def render(raw, name, wanted):
    """把這一塊換成 `wanted`，回傳整份新內容。區塊不存在就建在檔尾。"""
    begin, end = memspec.HOST_SYNC_MARKERS[name]
    block = f"{begin}\n{wanted}\n{end}" if wanted else f"{begin}\n{end}"
    found, _markers = _locate(raw, name)
    if found is None:
        base = raw.rstrip("\n")
        return (base + "\n\n" if base else "") + block + "\n"
    head, _current, tail = found
    return head.rstrip("\n") + ("\n\n" if head.strip() else "") + block + "\n" + tail.lstrip("\n")


def plan_for(host, vaults, home=None):
    """這個宿主檔要改什麼：每塊的現況與應有內容，以及擋住寫入的問題。"""
    path = host_path(host, home)
    try:
        raw = io.open(path, encoding="utf-8").read().replace("\r\n", "\n")
    except OSError:
        raw = ""
    regions, problems = [], []
    wanted = {
        memspec.HOST_SYNC_RULES_REGION: rules_text(vaults, host),
        memspec.HOST_SYNC_INDEX_REGION: index_text(vaults),
    }
    for name, text in wanted.items():
        size = len(text.encode("utf-8"))
        if size > memspec.HOST_SYNC_REGION_CAP_BYTES:
            problems.append(
                f"{name} 有 {size} 位元組，超過每場固定成本上限 "
                f"{memspec.HOST_SYNC_REGION_CAP_BYTES}；先讓內容瘦身再同步"
            )
            continue
        try:
            found, _markers = _locate(raw, name)
        except ValueError as exc:
            begin, end = memspec.HOST_SYNC_MARKERS[name]
            problems.append(memspec.HOST_SYNC_MISSING_MARKER_REASON.format(
                path=path, region=name, begin=raw.count(begin), end=raw.count(end)
            ) + f"（{exc}）")
            continue
        regions.append(Region(name, text, found is not None, found[1] if found else None))
    return Plan(host, path, regions, problems)


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
        if item.problems:
            for problem in item.problems:
                print(f"REFUSE {item.host}: {problem}", file=output)
            refused += 1
            continue  # 有問題就整個宿主不寫：半份同步比沒同步更難查
        try:
            raw = io.open(item.path, encoding="utf-8").read().replace("\r\n", "\n")
        except OSError:
            raw = ""
        updated = raw
        changed = [region.name for region in item.regions if not region.present or _drifted(region)]
        for region in item.regions:
            updated = render(updated, region.name, region.text)
        if updated == raw:
            print(f"OK     {item.host}: 已經一致", file=output)
            continue
        try:
            item.path.parent.mkdir(parents=True, exist_ok=True)
            if raw:
                backup = item.path.with_name(item.path.name + memspec.HOST_SYNC_BACKUP_SUFFIX)
                io.open(backup, "w", encoding="utf-8", newline="\n").write(raw)
            staging = item.path.with_name(f".{item.path.name}.tmp-{os.getpid()}")
            io.open(staging, "w", encoding="utf-8", newline="\n").write(updated)
            os.replace(staging, item.path)
        except OSError as exc:
            print(f"REFUSE {item.host}: 寫入失敗 {type(exc).__name__}: {exc}", file=output)
            refused += 1
            continue
        written += 1
        print(f"WROTE  {item.host}: {'、'.join(changed) or '標記整理'} → {item.path}", file=output)
    if not plans:
        print("沒有偵測到任何宿主（家目錄底下沒有 .claude／.codex）", file=output)
    return EXIT_REFUSED if refused else EXIT_OK


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
            codex.write_text(
                f"序言\n\n{legacy_begin}\n舊內容\n{legacy_end}\n\n結尾\n", encoding="utf-8")
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
    total = 10
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
    parser.add_argument("vaults", nargs="+", type=Path)
    parser.add_argument("--apply", action="store_true", help="實際寫入（預設只比對）")
    parser.add_argument("--host", action="append", choices=sorted(memspec.HOST_SYNC_FILES),
                        help="只處理這個宿主，可重複；預設處理偵測得到的全部")
    parsed = parser.parse_args(arguments)
    runner = apply if parsed.apply else check
    try:
        return runner(parsed.vaults, hosts=parsed.host, output=output)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
