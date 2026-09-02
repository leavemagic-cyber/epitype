import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Stable Epitype hook entrypoint; rendered once per adapter by graft."""

import json
from pathlib import Path
import subprocess


ADAPTER_FILENAME = "__EPITYPE_ADAPTER_FILENAME__"


def main():
    try:
        config_path = Path.home() / ".epitype" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        repo_value = config.get("repo_root") if isinstance(config, dict) else None
        if not isinstance(repo_value, str) or not repo_value.strip():
            return 0
        repo_root = Path(repo_value).expanduser()
        if not repo_root.is_dir():
            return 0
        adapter = repo_root / "adapters" / "claude" / ADAPTER_FILENAME
        if not adapter.is_file():
            return 0
        result = subprocess.run(
            [sys.executable, str(adapter), *sys.argv[1:]],
            check=False,
        )
        return result.returncode
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
