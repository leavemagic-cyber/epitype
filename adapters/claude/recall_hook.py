import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Claude UserPromptSubmit adapter for bounded, deduplicated local recall."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import memsearch, memspec
from _hook_common import (
    bounded_context,
    emit,
    expired,
    load_config,
    payload,
    read_event,
    resolve_vaults,
    run_synthetic,
    write_config,
)

# An owner grant that only lives in one session is lost the moment nobody cards
# it, and the next session asks for the same permission again. Grant-shaped
# prompts are therefore captured mechanically into the first configured vault;
# the wording is verbatim, the interpretation is left to whoever recalls it.
GRANT_PATTERN = re.compile(
    r"(?:我同意|同意過|我授權|授權你|你可以(?:操作|使用|用|直接|動|改|刪|執行|做|開|關|讀|寫)"
    r"|准你|准了|批准|允許你|不用問我|不必問我|直接(?:做|修|改|動|刪|執行)|隨你|照你"
    r"|\bI\s+(?:agree|authori[sz]e|approve|consent)\b|\byou\s+(?:may|are\s+allowed\s+to|have\s+my\s+permission)\b"
    r"|\bgo\s+ahead\b|\bpermission\s+granted\b|\bdon'?t\s+ask\s+me\b)",
    re.IGNORECASE,
)
GRANT_DIRECTORY = "grants"
GRANT_BODY_MAX_CHARS = 2000
GRANT_LOCK_SECONDS = 0.2


def _one_line(value):
    return " ".join(str(value or "").split())


def _grant_digest(prompt):
    return hashlib.sha256(_one_line(prompt).encode("utf-8")).hexdigest()[:12]


def _capture_grant(prompt, vault, event, started_at):
    if not GRANT_PATTERN.search(prompt) or expired(started_at):
        return None
    digest = _grant_digest(prompt)
    directory = vault / GRANT_DIRECTORY
    try:
        if any(directory.glob(f"grant-*-{digest}.md")):
            return None
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        name = f"grant-{stamp[:10].replace('-', '')}-{digest}"
        target = directory / f"{name}.md"
        summary = _one_line(prompt)[:120]
        body = prompt if len(prompt) <= GRANT_BODY_MAX_CHARS else prompt[:GRANT_BODY_MAX_CHARS] + "\n[truncated]"
        card = (
            "---\n"
            f"name: {name}\n"
            f"description: owner grant auto-captured {stamp[:10]}: {summary}\n"
            f"{memspec.SCOPE_FIELD}: governance-core\n"
            f"captured_at: {stamp}\n"
            f"cwd: {_one_line(event.get('cwd'))}\n"
            f"session_id: {_one_line(event.get('session_id', event.get('sessionId')))}\n"
            "---\n"
            f"{body}\n\n"
            "(auto-captured verbatim by the epitype recall hook; verify scope before acting)\n"
        )
        with memspec.file_lock(target, GRANT_LOCK_SECONDS) as locked:
            if not locked or target.exists():
                return None
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(card, encoding="utf-8")
            os.replace(temporary, target)
        # The staleness grace window would hide the new card from the very next
        # prompt; a grant must be recallable immediately, so refresh incrementally.
        if not expired(started_at):
            memsearch.build_index(vault, lock_timeout=0.0)
        return target
    except (OSError, sqlite3.Error):
        return None


def _session_component(session_id):
    component = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:128]
    return component or hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _claim_marker(session_id, block_digest):
    if not session_id:
        return True
    directory = (
        Path(tempfile.gettempdir())
        / memspec.RECALL_MARKER_DIRECTORY
        / _session_component(session_id)
    )
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / block_digest
    try:
        with marker.open("x", encoding="ascii") as stream:
            stream.write(block_digest + "\n")
    except FileExistsError:
        return False
    return True


def _handle(event, started_at):
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    config = load_config(started_at)
    if config is None:
        return None

    _capture_grant(prompt, config[memspec.CONFIG_VAULTS_FIELD][0], event, started_at)

    pieces = [memspec.UNTRUSTED_ADVISORY]
    for vault in resolve_vaults(config, event):
        if expired(started_at):
            return None
        result = memsearch.recall_index(vault, prompt)
        if expired(started_at):
            return None
        for hit in result.get("results", ())[: memspec.FTS_TOP_K]:
            pieces.append(
                "- "
                + " | ".join(
                    (
                        _one_line(hit.get("name")),
                        _one_line(hit.get("description")),
                        _one_line(hit.get("path")),
                    )
                )
            )

    context = bounded_context(
        "UserPromptSubmit",
        pieces,
        config[memspec.CONFIG_BUDGET_BYTES_FIELD],
        required_first=True,
    )
    if not context or context == memspec.UNTRUSTED_ADVISORY:
        return None
    digest = hashlib.sha256(context.encode("utf-8")).hexdigest()
    session_id = event.get("session_id", event.get("sessionId", ""))
    if not isinstance(session_id, str):
        session_id = ""
    if expired(started_at) or not _claim_marker(session_id, digest):
        return None
    return payload("UserPromptSubmit", context)


def _selftest():
    checks = []
    marker_directory = None
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-recall-") as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            short_card = vault / "b-short.md"
            short_card.write_text(
                "---\nname: Portable Recall\ndescription: Synthetic card\n---\nportable recall\n",
                encoding="utf-8",
            )
            long_card = vault / "a-long.md"
            long_card.write_text(
                "---\nname: Oversized Recall\ndescription: "
                + ("long " * 600)
                + "\n---\nportable recall\n",
                encoding="utf-8",
            )
            chinese_card = vault / "c-chinese.md"
            chinese_card.write_text(
                "---\nname: 中文喚回卡\ndescription: 真機編碼測試\n---\n中文事件測試\n",
                encoding="utf-8",
            )
            old_decision = vault / "d-old-decision.md"
            old_decision.write_text(
                "---\n"
                "name: Retired Hook Decision\n"
                "description: hookdecisionneedle historical rule\n"
                "decision_key: hook-read-contract\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.SUPERSEDED_DECISION_STATUS}\n"
                f"{memspec.SUPERSEDED_BY_FIELD}: e-current-decision.md\n"
                "---\n"
                "hookdecisionneedle old provenance\n",
                encoding="utf-8",
            )
            current_decision = vault / "e-current-decision.md"
            current_decision.write_text(
                "---\n"
                "name: Current Hook Decision\n"
                "description: hookdecisionneedle current rule\n"
                "decision_key: hook-read-contract\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                "---\n"
                "hookdecisionneedle governs injection\n",
                encoding="utf-8",
            )
            memsearch.build_index(vault)
            config = root / "config.json"
            write_config(config, [vault])
            session_id = "synthetic-" + uuid.uuid4().hex
            marker_directory = (
                Path(tempfile.gettempdir())
                / memspec.RECALL_MARKER_DIRECTORY
                / _session_component(session_id)
            )
            event = {"prompt": "how do I use portable recall from the command line", "session_id": session_id}

            first = run_synthetic(Path(__file__), event, config)
            first_value = json.loads(first.stdout) if first.stdout.strip() else {}
            context = (
                first_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            )
            checks.append(
                (
                    "hit injection",
                    first.returncode == 0
                    and memspec.UNTRUSTED_ADVISORY in context
                    and "Portable Recall" in context
                    and "Synthetic card" in context
                    and str(short_card.resolve()) in context,
                )
            )

            supersession_result = run_synthetic(
                Path(__file__),
                {"prompt": "load hookdecisionneedle"},
                config,
            )
            supersession_value = (
                json.loads(supersession_result.stdout)
                if supersession_result.stdout.strip()
                else {}
            )
            supersession_context = supersession_value.get(
                "hookSpecificOutput", {}
            ).get("additionalContext", "")
            injected_cards = [
                line for line in supersession_context.splitlines() if line.startswith("- ")
            ]
            checks.append(
                (
                    "default supersession filtering reaches injection",
                    supersession_result.returncode == 0
                    and len(injected_cards) == 1
                    and "Current Hook Decision" in supersession_context
                    and str(current_decision.resolve()) in supersession_context
                    and "Retired Hook Decision" not in supersession_context
                    and str(old_decision.resolve()) not in supersession_context,
                )
            )

            second = run_synthetic(Path(__file__), event, config)
            checks.append(
                (
                    "same-session deduplication",
                    second.returncode == 0 and not second.stdout and not second.stderr,
                )
            )

            hard_budget = 400
            write_config(config, [vault], budget=hard_budget)
            budget_event = {
                "prompt": "how can I retrieve portable recall from a local CLI",
                "session_id": "budget-" + uuid.uuid4().hex,
            }
            budget_result = run_synthetic(Path(__file__), budget_event, config)
            budget_value = json.loads(budget_result.stdout) if budget_result.stdout.strip() else {}
            budget_context = (
                budget_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            )
            checks.append(
                (
                    "hard context budget",
                    budget_result.returncode == 0
                    and bool(budget_context)
                    and len(budget_context.encode("utf-8")) <= hard_budget,
                )
            )

            write_config(config, [vault])
            cp950_environment = os.environ.copy()
            cp950_environment[memspec.EPITYPE_CONFIG_ENV] = os.fspath(config)
            cp950_environment["PYTHONDONTWRITEBYTECODE"] = "1"
            cp950_environment["PYTHONUTF8"] = "0"
            cp950_environment["PYTHONIOENCODING"] = "cp950"
            chinese_event = {"prompt": "請喚回中文真機編碼測試"}
            cp950_result = subprocess.run(
                [sys.executable, os.fspath(Path(__file__))],
                input=json.dumps(chinese_event, ensure_ascii=False).encode("utf-8"),
                capture_output=True,
                env=cp950_environment,
                timeout=10,
                check=False,
            )
            cp950_stdout = cp950_result.stdout.decode("utf-8", errors="replace")
            checks.append(
                (
                    "UTF-8 Chinese event under cp950 stdin",
                    cp950_result.returncode == 0
                    and bool(cp950_stdout.strip())
                    and "中文喚回卡" in cp950_stdout,
                )
            )

            config.write_text("{broken", encoding="utf-8")
            broken = run_synthetic(
                Path(__file__),
                {"prompt": "how do I recover portable recall with broken settings", "session_id": uuid.uuid4().hex},
                config,
            )
            checks.append(
                (
                    "bad config fail-open",
                    broken.returncode == 0 and not broken.stdout and not broken.stderr,
                )
            )

            write_config(config, [vault])
            grant_prompt = "你可以操作 chrome！我同意過，這件事以後不用再問"
            for _ in range(2):
                run_synthetic(Path(__file__), {"prompt": grant_prompt, "session_id": uuid.uuid4().hex}, config)
            grant_files = list((vault / GRANT_DIRECTORY).glob("grant-*.md"))
            grant_text = grant_files[0].read_text(encoding="utf-8") if grant_files else ""
            checks.append(
                (
                    "owner grant captured verbatim once",
                    len(grant_files) == 1
                    and grant_prompt in grant_text
                    and f"name: {grant_files[0].stem}" in grant_text
                    and not (vault / GRANT_DIRECTORY / (grant_files[0].name + ".tmp")).exists(),
                )
            )
            run_synthetic(Path(__file__), {"prompt": "請整理 chrome 分頁", "session_id": uuid.uuid4().hex}, config)
            checks.append(
                (
                    "plain request captures nothing",
                    len(list((vault / GRANT_DIRECTORY).glob("grant-*.md"))) == 1,
                )
            )
            recalled = run_synthetic(
                Path(__file__),
                {"prompt": "chrome 操作有沒有同意過", "session_id": uuid.uuid4().hex},
                config,
            )
            recalled_value = json.loads(recalled.stdout) if recalled.stdout.strip() else {}
            recalled_context = recalled_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "captured grant is recallable",
                    recalled.returncode == 0
                    and bool(grant_files)
                    and grant_files[0].stem in recalled_context,
                )
            )

            home = root / "home"
            project = root / "work" / "proj"
            project.mkdir(parents=True)
            slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
            native = home / ".claude" / "projects" / slug / "memory"
            native.mkdir(parents=True)
            (native / "native-card.md").write_text(
                "---\nname: Native Project Card\ndescription: nativeneedle lives in the cwd vault\n---\nnativeneedle\n",
                encoding="utf-8",
            )
            memsearch.build_index(native)
            empty_slug = re.sub(r"[^A-Za-z0-9]", "-", str(project.parent))
            (home / ".claude" / "projects" / empty_slug / "memory").mkdir(parents=True)
            home_environment = {"HOME": os.fspath(home), "USERPROFILE": os.fspath(home)}
            native_result = run_synthetic(
                Path(__file__),
                {"prompt": "find nativeneedle", "session_id": uuid.uuid4().hex, "cwd": str(project)},
                config,
                environment=home_environment,
            )
            native_value = json.loads(native_result.stdout) if native_result.stdout.strip() else {}
            native_context = native_value.get("hookSpecificOutput", {}).get("additionalContext", "")
            checks.append(
                (
                    "cwd-slug native vault joins recall; empty ancestor shell stays untouched",
                    native_result.returncode == 0
                    and "Native Project Card" in native_context
                    and not any((home / ".claude" / "projects" / empty_slug / "memory").iterdir()),
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        if marker_directory is not None:
            shutil.rmtree(marker_directory, ignore_errors=True)

    passed = sum(bool(ok) for _, ok in checks)
    total = 10
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main():
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    try:
        event = read_event(sys.stdin)
        value = _handle(event, _STARTED_AT)
        if value is not None and not expired(_STARTED_AT):
            emit(value)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
