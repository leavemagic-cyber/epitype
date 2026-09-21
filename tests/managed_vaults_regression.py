import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""裝著卡的原生庫本來就受管，`vaults` 不是名冊（owner 2026-09-21）。

設定檔沒登記的原生庫一樣要進夜間夢、卡片檢查與視圖。整形對每一庫都跑（精簡索引是共通
規則），但保護的必須是各庫自己的手寫段：寫死的白名單只寫得出治理庫的段名，別的庫用自己
的段名就會被整段搬走——而專案庫的 MEMORY.md 是宿主每一場自動載入的送達面。
"""

import contextlib
import io
import json
import os
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epitype import capture_route, dream, memspec


def _card(path, stem):
    path.write_text(
        f"---\nname: {stem}\ndescription: 2026-09-21 synthetic\naliases:\n- {stem}\n---\nbody\n",
        encoding="utf-8",
    )


def _native_home(root):
    """假的家目錄：一個裝著卡的專案庫、一個空殼專案庫。"""
    home = root / "home"
    projects = home / ".claude" / "projects"
    held = projects / "C--projects-money" / "memory"
    held.mkdir(parents=True)
    _card(held / "plan.md", "plan")
    (projects / "C--projects-empty" / "memory").mkdir(parents=True)
    return home, held.resolve()


@contextlib.contextmanager
def _fake_home(home):
    """整段期間 Path.home() 指向假家：宿主檔同步那一節不准寫到真機的契約檔。"""
    saved = {name: os.environ.get(name) for name in ("USERPROFILE", "HOME")}
    os.environ["USERPROFILE"] = os.fspath(home)
    os.environ["HOME"] = os.fspath(home)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _index(section, stem):
    return (
        "# 短入口\n"
        "\n"
        f"## {section}\n"
        f"- [{stem}]({stem}.md)\n"
    )


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix="epitype-managed-vaults-") as temp_dir:
        root = Path(temp_dir).resolve()
        home, held = _native_home(root)

        configured = root / "configured"
        configured.mkdir()
        _card(configured / "rule.md", "rule")
        second = root / "second"
        second.mkdir()
        _card(second / "note.md", "note")

        discovered = capture_route.native_vaults(home)
        checks.append((
            "持卡的原生庫被掃到，空殼不算庫",
            discovered == [held],
        ))

        managed = capture_route.managed_vaults([os.fspath(configured), os.fspath(second)], home=home)
        checks.append((
            "沒登記但持卡的原生庫照樣受管",
            held in managed,
        ))
        checks.append((
            "登記的庫排在前面，順序與設定檔一致",
            managed[:2] == [configured, second] and len(managed) == 3,
        ))
        checks.append((
            "同一個庫登記過就不再由掃描加第二次",
            capture_route.managed_vaults([os.fspath(held)], home=home) == [held],
        ))
        checks.append((
            "設定檔列到不存在的目錄不會混進受管清單",
            capture_route.managed_vaults([os.fspath(root / "gone"), ""], home=home) == [held],
        ))

        # 設定檔說要掃哪一個家：合成設定只掃它自己的家，掃不到真機的庫。
        config_path = home / ".epitype" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({memspec.CONFIG_VAULTS_FIELD: [os.fspath(configured)]}, ensure_ascii=False),
            encoding="utf-8",
        )
        checks.append((
            "家目錄由讀到的設定檔決定（<home>/.epitype/config.json → <home>）",
            capture_route.config_home(config_path) == home.resolve(),
        ))
        checks.append((
            "dream.configured_vaults 吃同一份受管清單",
            dream.configured_vaults(config_path) == [configured, held],
        ))

        # --- 整形：每一庫都跑，保護的是各庫自己的手寫段 ---
        # 專案庫沒有工作帳本，一樣要整形（owner 2026-09-21：要保持精簡索引是共通規則）。
        project = root / "project"
        project.mkdir()
        _card(project / "kept-card.md", "kept-card")
        _card(project / "later-card.md", "later-card")
        index_path = project / memspec.MEMORY_INDEX_FILENAME
        first_text = _index("專案執行約束", "kept-card")
        index_path.write_bytes(first_text.encode("utf-8"))
        sections_path = project / memspec.DREAM_DIRECTORY / dream.INDEX_SECTIONS_FILENAME
        pruned_file = project.joinpath(*memspec.INDEX_PRUNED_SUBPATH) / "20260921.md"

        # 整場夢的家目錄指向假家：這一支測試絕不去碰真機的 CLAUDE.md／AGENTS.md。
        with _fake_home(home):
            first_code = dream.main(
                ["--today", "2026-09-21", os.fspath(project)], output=io.StringIO())
        recorded = json.loads(sections_path.read_text(encoding="utf-8")) if sections_path.is_file() else {}
        checks.append((
            "首次整形：MEMORY.md 一個位元組都沒動，也沒開 index_pruned",
            first_code == 0
            and index_path.read_bytes() == first_text.encode("utf-8")
            and not pruned_file.parent.exists(),
        ))
        checks.append((
            "首次整形：把這一庫自己的手寫段名記進 .epitype/index_sections.json",
            recorded.get("version") == dream.INDEX_SECTIONS_VERSION
            and "專案執行約束" in (recorded.get("sections") or ())
            and all(item in (recorded.get("sections") or ()) for item in memspec.INDEX_ALLOWED_SECTIONS),
        ))

        second_text = first_text + "\n## 事後長出來的段\n- [later-card](later-card.md)\n"
        index_path.write_bytes(second_text.encode("utf-8"))
        with _fake_home(home):
            second_code = dream.main(
                ["--today", "2026-09-21", os.fspath(project)], output=io.StringIO())
        after = index_path.read_text(encoding="utf-8")
        checks.append((
            "第二次：記下的段一字不動，事後新增段裡已被目錄承載的連結搬進 index_pruned",
            second_code == 0
            and after == second_text.replace("- [later-card](later-card.md)\n", "")
            and "- [later-card](later-card.md)" in pruned_file.read_text(encoding="utf-8"),
        ))

        # 清單壞掉＝當成第一次：不搬、重記。絕不反過來當成「沒有保護」。
        broken_text = after + "\n## 清單壞掉之後的段\n- [kept-card](kept-card.md)\n"
        index_path.write_bytes(broken_text.encode("utf-8"))
        pruned_before = pruned_file.read_bytes()
        sections_path.write_text("{broken", encoding="utf-8")
        with _fake_home(home):
            broken_code = dream.main(
                ["--today", "2026-09-21", os.fspath(project)], output=io.StringIO())
        try:
            rerecorded = json.loads(sections_path.read_text(encoding="utf-8"))
        except ValueError:
            rerecorded = {}
        checks.append((
            "清單讀不出來：一行都不搬、重記一份，新段也記進去",
            broken_code == 0
            and index_path.read_bytes() == broken_text.encode("utf-8")
            and pruned_file.read_bytes() == pruned_before
            and "清單壞掉之後的段" in (rerecorded.get("sections") or ()),
        ))

    passed = sum(bool(ok) for _, ok in checks)
    total = 11
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    for name, ok in checks:
        if not ok:
            print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
