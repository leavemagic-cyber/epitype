# -*- coding: utf-8 -*-
"""量掛鉤真正花掉的時間。

兩個數字分開報，因為它們回答不同的問題：

- **宿主等多久**：從啟動行程到吐出判斷。這是使用者實際感受到的，但它含一段減不掉的
  Python 啟動成本，而那一段會隨機器負載浮動。
- **我們自己的程式碼多久**：在同一個行程裡計時，從匯入第一行到判斷結束。優化要看的是
  這一個。

取**最小值**不取中位數：機器上同時有別的東西在跑時，中位數量到的是負載，不是程式。
最小值是「這台機器在最好的情況下要花多久」，而優化前後比的就該是同一個條件。

量的是**記憶庫的暫存副本**：閘門每擋一次就往庫裡的紀錄檔寫一列，直接對正式庫量，
等於每跑一次就灌進十幾列假的「擋下」——2026-09-19 這支工具一天灌了 185 列，害
「這道守衛最近一天擋了幾次」的提醒與夜間報告全部失真。卡片照抄，所以量到的工作量一樣。
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADAPTERS = REPO / "adapters" / "claude"
ROUNDS = 9

CASES = (
    ("pretooluse（放行）", ADAPTERS / "pretooluse_gate.py", {
        "tool_name": "Read",
        "tool_input": {"file_path": str(REPO / "README.md")},
        "session_id": "latency-allow",
        "cwd": str(REPO),
    }),
    ("pretooluse（擋下）", ADAPTERS / "pretooluse_gate.py", {
        "tool_name": "Bash",
        "tool_input": {"command": "python - <<'PY'  print('\\\\\\\\')"},
        "session_id": "latency-deny",
        "cwd": str(REPO),
    }),
    ("stop（放行）", ADAPTERS / "stop_gate.py", {
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "好了。",
        "session_id": "latency-stop",
        "cwd": str(REPO),
    }),
)

# 在同一個行程裡量：把事件塞進 stdin，計時包住「匯入＋跑完」。
_INNER = (
    "import io, sys, time\n"
    # 宿主是用腳本路徑直接跑的，那樣 adapters 目錄會自動進 sys.path；這裡用 runpy 就要
    # 自己補上，不然量到的是一個匯入失敗的行程。
    "sys.path.insert(0, %(adapters)r)\n"
    "sys.stdin = io.StringIO(%(event)r)\n"
    "started = time.perf_counter()\n"
    "import runpy\n"
    "try:\n"
    "    runpy.run_path(%(script)r, run_name='__main__')\n"
    "except SystemExit:\n"
    "    pass\n"
    "sys.stderr.write('ELAPSED %%.3f' %% ((time.perf_counter() - started) * 1000))\n"
)


_ENV = dict(os.environ)


def _keep_cards_only(directory, names):
    return [name for name in names
            if not name.endswith('.md') and not os.path.isdir(os.path.join(directory, name))]


def _scratch_config(root):
    """把設定裡每個記憶庫抄一份到暫存處（只抄卡片），回傳指向副本的設定檔路徑。"""
    sys.path.insert(0, str(REPO))
    from epitype import memspec
    source = Path(os.environ.get(memspec.EPITYPE_CONFIG_ENV)
                  or Path.home() / '.epitype' / 'config.json')
    config = json.loads(source.read_text(encoding='utf-8'))
    copies = []
    for index, vault in enumerate(config.get(memspec.CONFIG_VAULTS_FIELD) or []):
        target = Path(root) / ('vault%d' % index)
        if Path(vault).is_dir():
            shutil.copytree(vault, target, ignore=_keep_cards_only)
        else:
            target.mkdir(parents=True)
        copies.append(str(target))
    config[memspec.CONFIG_VAULTS_FIELD] = copies
    path = Path(root) / 'config.json'
    path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    _ENV[memspec.EPITYPE_CONFIG_ENV] = str(path)
    # 光換設定檔不夠：閘門還會從 cwd 往上找本機原生的記憶庫，C 槽任何路徑最後都會
    # 找到正式治理庫，紀錄照樣寫進去。家目錄一起指到暫存處，它才找不到。
    for name in ('USERPROFILE', 'HOME'):
        _ENV[name] = str(root)
    return path


def _spawn_ms(script, event):
    payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
    started = time.perf_counter()
    subprocess.run([sys.executable, str(script)], input=payload, env=_ENV,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return (time.perf_counter() - started) * 1000


def _inner_ms(script, event):
    code = _INNER % {"event": json.dumps(event, ensure_ascii=False),
                     "script": str(script), "adapters": str(ADAPTERS)}
    done = subprocess.run([sys.executable, "-c", code], env=_ENV,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    text = done.stderr.decode("utf-8", "replace")
    marker = text.rfind("ELAPSED ")
    if marker < 0:
        return None
    try:
        return float(text[marker + 8:].split()[0])
    except (ValueError, IndexError):
        return None


def _baseline():
    samples = []
    for _ in range(ROUNDS):
        started = time.perf_counter()
        subprocess.run([sys.executable, "-c", "pass"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        samples.append((time.perf_counter() - started) * 1000)
    return min(samples)


def main(argv=None):
    with tempfile.TemporaryDirectory(prefix='epitype-latency-') as root:
        _scratch_config(root)
        return _measure()


def _measure():
    base = _baseline()
    print("空的 Python 行程（減不掉的底，取最小值）：%.0f ms" % base)
    print("%-18s %10s %10s %12s" % ("", "宿主等(最小)", "我們的碼", "中位數"))
    worst = 0.0
    for name, script, event in CASES:
        spawns = [_spawn_ms(script, event) for _ in range(ROUNDS)]
        inners = [value for value in (_inner_ms(script, event) for _ in range(ROUNDS))
                  if value is not None]
        inner = min(inners) if inners else float("nan")
        print("%-18s %8.0f ms %8.0f ms %10.0f ms"
              % (name, min(spawns), inner, statistics.median(spawns)))
        worst = max(worst, inner)
    print("我們自己的碼，最慢的一支 = %.0f ms" % worst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
