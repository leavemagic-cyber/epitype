import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # Keep output deterministic on cp950 consoles.
"""Keep Codex context settings below known whole-request billing cliffs."""

import argparse
from datetime import date
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib


# OpenAI rate card (checked 2026-09-10): input over 272K reprices the whole request for
# GPT-5.6 Sol/Terra and GPT-5.5; GPT-6 Astra is exempt inside Codex. A model missing here
# is unprotected, so "exempt" must stay distinguishable from "unknown".
MODEL_CLIFFS_JSON = r'''{
  "gpt-5.6-sol": {"cliff_input": 272000, "window": 240000, "auto_compact": 210000},
  "gpt-5.6-terra": {"cliff_input": 272000, "window": 240000, "auto_compact": 210000},
  "gpt-5.5": {"cliff_input": 272000, "window": 240000, "auto_compact": 210000}
}'''
MODEL_CLIFFS = json.loads(MODEL_CLIFFS_JSON)
MODEL_NO_CLIFF = frozenset({"gpt-6-astra"})
TARGETS = (
    ("model_context_window", "window"),
    ("model_auto_compact_token_limit", "auto_compact"),
)
UTF8_BOM = b"\xef\xbb\xbf"
TABLE_HEADER = re.compile(rb"^[ \t]*\[{1,2}[^\r\n]+\]{1,2}[ \t]*(?:#.*)?$")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_config(path):
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    value = tomllib.loads(text)
    if not isinstance(value, dict):
        raise ValueError("config root must be a TOML table")
    return value, raw


def _display(value):
    if value is None:
        return "<missing>"
    return str(value)


def _line_ending(data):
    match = re.search(rb"\r\n|\n|\r", data)
    return match.group(0) if match else os.linesep.encode("ascii")


def _split_ending(line):
    for ending in (b"\r\n", b"\n", b"\r"):
        if line.endswith(ending):
            return line[: -len(ending)], ending
    return line, b""


def _assignment(body, key):
    return re.match(
        rb"^([ \t]*" + re.escape(key.encode("ascii")) + rb"[ \t]*=[ \t]*)(.*)$",
        body,
    )


def _replace_value(body, key, value):
    match = _assignment(body, key)
    if match is None:
        return None
    before_comment, marker, comment = match.group(2).partition(b"#")
    value_bytes = before_comment.rstrip(b" \t")
    spacing = before_comment[len(value_bytes) :]
    suffix = marker + comment if marker else b""
    return match.group(1) + str(value).encode("ascii") + spacing + suffix


def _surgery(raw, updates, existing_keys=()):
    bom = UTF8_BOM if raw.startswith(UTF8_BOM) else b""
    body = raw[len(bom) :]
    newline = _line_ending(body)
    lines = body.splitlines(keepends=True)
    existing = set(existing_keys)
    pending = {key: value for key, value in updates.items() if key in existing}
    output = []
    root_open = True

    for line in lines:
        content, ending = _split_ending(line)
        if root_open and TABLE_HEADER.match(content):
            root_open = False
        if root_open:
            for key, value in tuple(pending.items()):
                replacement = _replace_value(content, key, value)
                if replacement is not None:
                    content = replacement
                    pending.pop(key)
                    break
        output.append(content + ending)
    if pending:
        raise ValueError("could not locate an existing root setting for line surgery")

    additions = [
        f"{key} = {value}".encode("ascii") + newline
        for key, value in updates.items()
        if key not in existing
    ]
    return bom + b"".join(additions + output)


def _backup_path(path):
    suffix = f".bak_epitype_{date.today():%Y%m%d}"
    candidate = path.with_name(path.name + suffix)
    counter = 2
    while candidate.exists():
        candidate = path.with_name(path.name + suffix + f"_{counter}")
        counter += 1
    return candidate


def _atomic_write(path, data):
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".epitype_tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _apply(path, raw, updates, existing_keys):
    updated = _surgery(raw, updates, existing_keys)
    if updated == raw:
        return None
    backup = _backup_path(path)
    shutil.copy2(path, backup)
    _atomic_write(path, updated)
    return backup


def _run_check(path, apply_changes=False, output=sys.stdout):
    config, raw = _read_config(path)
    model = config.get("model")
    print(f"model: {_display(model)}", file=output)
    cliff = MODEL_CLIFFS.get(model) if isinstance(model, str) else None
    if cliff is None:
        if model in MODEL_NO_CLIFF:
            print("此模型在 Codex 內沒有長上下文加價門檻；設定檔未修改。", file=output)
        else:
            print("無斷崖資料；設定檔未修改。", file=output)
        return 0

    print(f"cliff_input: {cliff['cliff_input']}", file=output)
    updates = {}
    for config_key, table_key in TARGETS:
        current = config.get(config_key)
        recommended = cliff[table_key]
        state = "OK" if type(current) is int and current == recommended else "CHANGE"
        print(
            f"{config_key}: current={_display(current)} recommended={recommended} {state}",
            file=output,
        )
        if state == "CHANGE":
            updates[config_key] = recommended

    if not apply_changes:
        print("check only; no file changes.", file=output)
        return 0
    if not updates:
        print("already at recommended values; no file changes.", file=output)
        return 0

    backup = _apply(path, raw, updates, {key for key in updates if key in config})
    print(f"backup: {backup}", file=output)
    print("applied: " + ", ".join(updates), file=output)
    return 0


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix=".config-guard-", dir=_REPO_ROOT) as temp_dir:
            root = Path(temp_dir).resolve()
            known = root / "known.toml"
            source = (
                b"# synthetic fixture\r\n"
                b"model = \"gpt-5.6-sol\"\r\n"
                b"model_context_window = 300000  # retain comment\r\n"
                b"model_auto_compact_token_limit=260000\r\n"
                b"\r\n[synthetic]\r\n"
                b"model_context_window = 123456\r\n"
                b"label = \"unchanged\"\r\n"
            )
            known.write_bytes(source)

            check_output = io.StringIO()
            check_code = _run_check(known, output=check_output)
            checks.append((
                "check reads model and values",
                check_code == 0
                and "current=300000 recommended=240000" in check_output.getvalue()
                and "current=260000 recommended=210000" in check_output.getvalue(),
            ))

            apply_output = io.StringIO()
            apply_code = _run_check(known, apply_changes=True, output=apply_output)
            expected = source.replace(
                b"model_context_window = 300000  # retain comment",
                b"model_context_window = 240000  # retain comment",
                1,
            ).replace(
                b"model_auto_compact_token_limit=260000",
                b"model_auto_compact_token_limit=210000",
                1,
            )
            missing_source = b'model = "gpt-5.6-sol"\n[synthetic]\nkeep = true\n'
            missing_expected = (
                b'model_context_window = 240000\n'
                b'model_auto_compact_token_limit = 210000\n'
                b'model = "gpt-5.6-sol"\n'
                b'[synthetic]\nkeep = true\n'
            )
            checks.append((
                "apply changes only target lines",
                apply_code == 0
                and known.read_bytes() == expected
                and _surgery(
                    missing_source,
                    {key: MODEL_CLIFFS["gpt-5.6-sol"][table_key] for key, table_key in TARGETS},
                ) == missing_expected,
            ))

            backups = list(root.glob("known.toml.bak_epitype_*"))
            checks.append((
                "backup exists with original bytes",
                len(backups) == 1 and backups[0].read_bytes() == source,
            ))

            unknown = root / "unknown.toml"
            unknown_source = b'model = "synthetic-unknown"\nmodel_context_window = 999999\n'
            unknown.write_bytes(unknown_source)
            unknown_output = io.StringIO()
            unknown_code = _run_check(unknown, apply_changes=True, output=unknown_output)
            checks.append((
                "unknown model never changes file",
                unknown_code == 0
                and unknown.read_bytes() == unknown_source
                and "無斷崖資料" in unknown_output.getvalue()
                and not list(root.glob("unknown.toml.bak_epitype_*")),
            ))

            exempt = root / "astra.toml"
            exempt_source = b'model = "gpt-6-astra"\nmodel_context_window = 999999\n'
            exempt.write_bytes(exempt_source)
            exempt_output = io.StringIO()
            exempt_code = _run_check(exempt, apply_changes=True, output=exempt_output)
            terra = root / "terra.toml"
            terra.write_bytes(b'model = "gpt-5.6-terra"\nmodel_context_window = 999999\n')
            terra_output = io.StringIO()
            _run_check(terra, apply_changes=False, output=terra_output)
            checks.append((
                "exempt model is told apart from unknown; a model sharing the cliff is guarded",
                exempt_code == 0
                and exempt.read_bytes() == exempt_source
                and "沒有長上下文加價門檻" in exempt_output.getvalue()
                and "無斷崖資料" not in exempt_output.getvalue()
                and "cliff_input: 272000" in terra_output.getvalue()
                and "CHANGE" in terra_output.getvalue(),
            ))

            cp950_environment = os.environ.copy()
            cp950_environment["PYTHONUTF8"] = "0"
            cp950_environment["PYTHONIOENCODING"] = "cp950"
            cp950_environment["PYTHONDONTWRITEBYTECODE"] = "1"
            cp950_result = subprocess.run(
                [sys.executable, os.fspath(Path(__file__)), "check", "--config", os.fspath(known)],
                capture_output=True,
                env=cp950_environment,
                timeout=10,
                check=False,
            )
            cp950_stdout = cp950_result.stdout.decode("utf-8", errors="replace")
            checks.append((
                "cp950 console remains UTF-8 safe",
                cp950_result.returncode == 0 and "gpt-5.6-sol" in cp950_stdout,
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 6
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="inspect or safely apply known cliff settings")
    check.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".codex" / "config.toml",
        help="config.toml path (default: ~/.codex/config.toml)",
    )
    check.add_argument("--apply", action="store_true", help="back up and update only two settings")
    return parser


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    parsed = _parser().parse_args(arguments)
    try:
        return _run_check(parsed.config.expanduser().resolve(), parsed.apply)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
