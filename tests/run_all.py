import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，避免繁中輸出中斷。
"""依序執行 Epitype 七個核心工具的內建合成測試。"""

import os
from pathlib import Path
import subprocess


TOOLS = (
    "memspec.py",
    "memsearch.py",
    "decision_lint.py",
    "ledger_gate.py",
    "compact_map.py",
    "scar_census.py",
    "token_meter.py",
)


def _emit(text, stream):
    if text:
        print(text, end="" if text.endswith("\n") else "\n", file=stream)


def main():
    repo_root = Path(__file__).resolve().parents[1]
    tool_root = repo_root / "epitype"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    passed = 0

    for name in TOOLS:
        tool = tool_root / name
        print(f"=== {name} ===")
        if not tool.is_file():
            print(f"RESULT FAIL {name}: missing tool", file=sys.stderr)
            continue
        try:
            result = subprocess.run(
                [sys.executable, str(tool), "--selftest"],
                cwd=repo_root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            print(f"RESULT FAIL {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        _emit(result.stdout, sys.stdout)
        _emit(result.stderr, sys.stderr)
        if result.returncode == 0:
            passed += 1
            print(f"RESULT PASS {name}")
        else:
            print(f"RESULT FAIL {name}: exit {result.returncode}", file=sys.stderr)

    print(f"TOTAL PASS {passed}/{len(TOOLS)}")
    return 0 if passed == len(TOOLS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
