"""Read Codex's authoritative hook inventory without starting a model turn."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


def hook_states(home):
    command = shutil.which("codex")
    if command is None:
        raise OSError("Codex CLI unavailable")
    args = [command, "app-server", "--stdio", "--disable", "memories"]
    if os.name == "nt" and Path(command).suffix.lower() == ".cmd":
        args[:0] = ["cmd.exe", "/d", "/c"]
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home / ".codex")
    child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                             env=environment)
    incoming = queue.Queue()

    def read():
        try:
            for line in child.stdout:
                incoming.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            incoming.put(None)

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + 6

    def send(value):
        child.stdin.write(json.dumps(value) + "\n")
        child.stdin.flush()

    def call(identity, method, params):
        send({"id": identity, "method": method, "params": params})
        while True:
            value = incoming.get(timeout=max(0.001, deadline - time.monotonic()))
            if value is None:
                raise OSError("Codex hook inventory closed unexpectedly")
            if value.get("id") == identity and "method" not in value:
                if "error" in value:
                    raise OSError("Codex hook inventory rejected the request")
                return value["result"]

    try:
        call(1, "initialize", {"clientInfo": {"name": "epitype-trust-check", "version": "1"}})
        send({"method": "initialized"})
        result = call(2, "hooks/list", {"cwds": [str(Path.cwd())]})
        entries = result.get("data", [])
        if len(entries) != 1 or entries[0].get("errors"):
            raise OSError("Codex hook inventory incomplete")
        return {item["key"]: item for item in entries[0]["hooks"]}
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                child.kill()
            child.wait(timeout=2)
        child.stdout.close()
