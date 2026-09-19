# -*- coding: utf-8 -*-
"""量掛鉤真正花掉的時間。

兩個數字分開報，因為它們回答不同的問題：

- **宿主等多久**：從啟動行程到吐出判斷。這是使用者實際感受到的，但它含一段減不掉的
  Python 啟動成本，而那一段會隨機器負載浮動。
- **我們自己的程式碼多久**：在同一個行程裡計時，從匯入第一行到判斷結束。優化要看的是
  這一個。

取**最小值**不取中位數：機器上同時有別的東西在跑時，中位數量到的是負載，不是程式。
最小值是「這台機器在最好的情況下要花多久」，而優化前後比的就該是同一個條件。
"""
import json
import statistics
import subprocess
import sys
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
        "tool_input": {"command": "cat <<'EOF' > C:\\tmp\\x.txt"},
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


def _spawn_ms(script, event):
    payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
    started = time.perf_counter()
    subprocess.run([sys.executable, str(script)], input=payload,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return (time.perf_counter() - started) * 1000


def _inner_ms(script, event):
    code = _INNER % {"event": json.dumps(event, ensure_ascii=False),
                     "script": str(script), "adapters": str(ADAPTERS)}
    done = subprocess.run([sys.executable, "-c", code],
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
