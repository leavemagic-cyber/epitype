# -*- coding: utf-8 -*-
"""把隨輪子出貨的那幾張卡放進使用者的記憶庫。

`pip install epitype && epitype install` 之後那個庫是空的：沒有一張卡，於是沒有一條規則、
沒有一次攔截可以看，而這套東西唯一說得清楚的賣點就是「規則真的擋得住」。空庫等於把證明
的責任丟回給剛裝好的人。

這裡出貨的卡沒有任何「示範用」的特例：欄位、閘門、體檢走的都是真機那一套，兩向例句由
card_lint 當場跑過。所以第一次被擋下來的那一次，就是這套規則平常的樣子。

兩條不能退讓的界線：
- 已經在庫裡的同名檔一律不覆寫。那個檔是使用者的，即使內容碰巧一樣。
- `--remove` 只刪「還帶著 starter 記號」而且「跟出貨版位元組相同」的檔。改過一個字的卡
  就是使用者的卡，只報告、不動它。
"""

import argparse
import os
from pathlib import Path
import sys

try:
    from . import memspec
except ImportError:  # 直接當腳本跑
    import memspec

_ADAPTER_DIR = Path(__file__).resolve().parents[1] / "adapters" / "claude"
# 輪子裡 `epitype/` 與 `adapters/` 是同一層的姊妹目錄，checkout 裡也是——所以這一行
# 兩種安裝方式都指得到，不必問自己是從哪裡被載進來的。
CARD_DIRECTORY = Path(__file__).resolve().parent / memspec.STARTER_DIRECTORY

WROTE = "WRITE {name}"
DRY_WRITE = "DRY-RUN write {name}"
SKIPPED = "SKIP {name}: already in the vault, left untouched"
REMOVED = "REMOVE {name}"
DRY_REMOVE = "DRY-RUN remove {name}"
KEPT_EDITED = "KEEP {name}: edited since it was installed, so it is yours now"
KEPT_FOREIGN = "KEEP {name}: no {field} marker, not ours to remove"
MISSING = "ABSENT {name}: not in this vault"
DRY_DONE = "DRY-RUN complete; no files changed."
# 裝完之後第一件該做的事，是看它擋一次。這一句擋的是 PreToolUse，指令連跑都不會跑到。
TRY_LINE = (
    "TRY: ask your agent to run `git add -A` in any repo — the call is denied "
    "before git runs. Then say `that should fix it` and watch the turn come back."
)


def shipped_cards():
    """出貨的卡，依檔名排序。目錄不在（打包漏了）就是空的，呼叫端要自己說話。"""
    if not CARD_DIRECTORY.is_dir():
        return []
    return sorted(CARD_DIRECTORY.glob("*.md"))


def _card_name(path):
    """卡片 frontmatter 的 name，讀不到就退回檔名——列表不該因為一張壞卡整個倒掉。"""
    try:
        fields, _problem = memspec.frontmatter_fields(path)
    except (OSError, UnicodeError):
        return path.stem
    return " ".join(str(fields.get(memspec.NAME_FIELD, "") or "").split()) or path.stem


def carries_marker(path):
    """這個檔還宣告自己是 starter 卡嗎。頂層鍵在不在就是答案，值不參與判斷。"""
    try:
        fields, _problem = memspec.frontmatter_fields(path)
    except (OSError, UnicodeError):
        return False
    return memspec.STARTER_FIELD in fields


def default_vault():
    """使用者設定檔指定的治理庫。EPITYPE_CONFIG 由 config_path() 自己認。"""
    import time

    if str(_ADAPTER_DIR) not in sys.path:
        sys.path.insert(0, str(_ADAPTER_DIR))
    import _hook_common as common

    config = common.load_config(time.monotonic())
    if config is None:
        raise RuntimeError(f"could not read {common.config_path()} in time")
    return common.governance_vault(config)


def _install(vault, cards, dry_run, output):
    written = []
    skipped = []
    for source in cards:
        target = vault / source.name
        if target.exists():
            skipped.append(source.name)
            print(SKIPPED.format(name=source.name), file=output)
            continue
        written.append(source.name)
        print((DRY_WRITE if dry_run else WROTE).format(name=source.name), file=output)
        if not dry_run:
            # 位元組照抄：`--remove` 之後要比對「跟出貨版一模一樣嗎」，任何換行轉換都會
            # 讓那個比對永遠不成立，於是每一張卡都變成「使用者改過的」而刪不掉。
            target.write_bytes(source.read_bytes())
    return written, skipped


def _remove(vault, cards, dry_run, output):
    removed = []
    kept = []
    for source in cards:
        target = vault / source.name
        if not target.is_file():
            print(MISSING.format(name=source.name), file=output)
            continue
        if not carries_marker(target):
            kept.append(source.name)
            print(KEPT_FOREIGN.format(name=source.name, field=memspec.STARTER_FIELD), file=output)
            continue
        try:
            same = target.read_bytes() == source.read_bytes()
        except OSError:
            same = False
        if not same:
            kept.append(source.name)
            print(KEPT_EDITED.format(name=source.name), file=output)
            continue
        removed.append(source.name)
        print((DRY_REMOVE if dry_run else REMOVED).format(name=source.name), file=output)
        if not dry_run:
            target.unlink()
    return removed, kept


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="epitype starter",
        description="Copy the shipped starter rule cards into a vault.",
    )
    parser.add_argument("--vault", help="destination vault (default: the governance vault in your config)")
    parser.add_argument("--list", action="store_true", help="list the shipped cards and where they stand")
    parser.add_argument("--dry-run", action="store_true", help="report without writing or deleting anything")
    parser.add_argument("--remove", action="store_true", help="delete the pristine starter cards this tool wrote")
    options = parser.parse_args([] if argv is None else list(argv))
    output = sys.stdout

    cards = shipped_cards()
    if not cards:
        print(f"epitype starter: no cards shipped under {CARD_DIRECTORY}", file=sys.stderr)
        return 1

    if options.vault:
        vault = Path(options.vault).expanduser().resolve()
    else:
        try:
            vault = default_vault()
        except Exception as exc:
            print(f"epitype starter: {type(exc).__name__}: {exc}", file=sys.stderr)
            print("epitype starter: pass --vault PATH, or run `epitype install` first", file=sys.stderr)
            return 2

    if options.list:
        print(f"VAULT: {vault}", file=output)
        for source in cards:
            state = "installed" if (vault / source.name).is_file() else "not installed"
            print(f"{source.name} [{state}] — {_card_name(source)}", file=output)
        return 0

    if not vault.is_dir():
        print(f"epitype starter: vault is not a directory: {vault}", file=sys.stderr)
        return 2
    print(f"VAULT: {vault}", file=output)

    if options.remove:
        removed, kept = _remove(vault, cards, options.dry_run, output)
        print(f"STARTER: removed {len(removed)}, kept {len(kept)}", file=output)
    else:
        written, skipped = _install(vault, cards, options.dry_run, output)
        print(f"STARTER: wrote {len(written)}, skipped {len(skipped)}", file=output)
        # 試跑的那一句只在真的寫進去之後才說：庫裡還沒有卡的時候叫人去試，他會看到
        # 什麼都沒發生，然後以為這套東西壞了。
        if written and not options.dry_run:
            print(TRY_LINE, file=output)
    if options.dry_run:
        print(DRY_DONE, file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
