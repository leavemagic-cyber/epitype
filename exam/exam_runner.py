import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 consoles must not break the exam entrypoint.
"""Run synthetic Epitype recall, gate, lint, and supersession questions."""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile


_REPO_ROOT = Path(__file__).resolve().parents[1]
_EPITYPE_DIR = _REPO_ROOT / "epitype"
for _import_root in (_REPO_ROOT, _EPITYPE_DIR):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from epitype import decision_lint, memsearch, memspec


_CATEGORIES = {"recall", "gate", "lint", "supersession"}
_SAMPLE_CORPUS = Path(__file__).with_name("sample_corpus.json")
_GATE_SCRIPT = _REPO_ROOT / "adapters" / "claude" / "pretooluse_gate.py"


def _as_text_list(value, field):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return value


def _write_cards(vault, setup):
    cards = setup.get("vault_cards") if isinstance(setup, dict) else None
    if not isinstance(cards, list):
        raise ValueError("setup.vault_cards must be a list")
    written = []
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError("each vault card must be an object")
        relative = card.get("path")
        content = card.get("content")
        if not isinstance(relative, str) or not relative or not isinstance(content, str):
            raise ValueError("vault cards require text path and content")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"unsafe vault card path: {relative}")
        target = (vault / relative_path).resolve()
        try:
            target.relative_to(vault.resolve())
        except ValueError as exc:
            raise ValueError(f"vault card escapes temporary vault: {relative}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(relative_path.as_posix())
    if len(written) != len(set(written)):
        raise ValueError("setup.vault_cards contains duplicate paths")
    return written


def _relative_hits(vault, result):
    hits = []
    for item in result.get("results", ()):
        path = Path(item["path"]).resolve()
        hits.append(path.relative_to(vault.resolve()).as_posix())
    return hits


def _judge_recall(vault, question_input, expect):
    if not isinstance(question_input, str) or not question_input.strip():
        raise ValueError("recall input must be a natural-language string")
    result = memsearch.recall_index(vault, question_input)
    hits = _relative_hits(vault, result)
    required = _as_text_list(expect.get("top_k_contains", []), "top_k_contains")
    excluded = _as_text_list(expect.get("top_k_excludes", []), "top_k_excludes")
    if not required and not excluded:
        raise ValueError("recall expect needs top_k_contains or top_k_excludes")
    missing = [item for item in required if item not in hits]
    forbidden = [item for item in excluded if item in hits]
    if missing or forbidden:
        return f"top_k={hits}; missing={missing}; forbidden={forbidden}"
    return None


def _run_gate(vault, root, event):
    if not isinstance(event, dict):
        raise ValueError("gate input must be a tool event object")
    config = root / "gate-config.json"
    config.write_text(
        json.dumps(
            {
                memspec.CONFIG_VAULTS_FIELD: [str(vault)],
                memspec.CONFIG_BUDGET_BYTES_FIELD: memspec.HOOK_DEFAULT_BUDGET_BYTES,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    environment = os.environ.copy()
    environment[memspec.EPITYPE_CONFIG_ENV] = os.fspath(config)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, os.fspath(_GATE_SCRIPT)],
        input=json.dumps(event, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_REPO_ROOT,
        env=environment,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gate exited {result.returncode}: {result.stderr.strip()}")
    if not result.stdout.strip():
        return "allow", ""
    payload = json.loads(result.stdout)
    output = payload.get("hookSpecificOutput", {})
    return (
        output.get("permissionDecision", "allow"),
        output.get("permissionDecisionReason", ""),
    )


def _judge_gate(vault, root, question_input, expect):
    decision, advice = _run_gate(vault, root, question_input)
    expected_decision = expect.get("decision")
    expected_advice = expect.get("advice_contains")
    if expected_decision not in ("allow", "deny"):
        raise ValueError("gate expect.decision must be allow or deny")
    if expected_advice is not None and not isinstance(expected_advice, str):
        raise ValueError("gate expect.advice_contains must be text")
    if decision != expected_decision or (
        expected_advice is not None and expected_advice not in advice
    ):
        return f"decision={decision}; advice={advice!r}"
    return None


def _check_card_input(question_input, written, field="input"):
    selected = _as_text_list(question_input, field)
    if set(selected) != set(written) or len(selected) != len(written):
        raise ValueError(f"{field} must name every setup card exactly once")


def _judge_lint(vault, written, question_input, expect):
    _check_card_input(question_input, written)
    report = decision_lint.lint_vault(vault)
    violations = [finding.reason for finding in report.failures]
    expected_exit = expect.get("exit_code")
    expected_violations = _as_text_list(expect.get("violations"), "violations")
    if expected_exit not in (0, 1):
        raise ValueError("lint expect.exit_code must be 0 or 1")
    if report.exit_code != expected_exit or violations != expected_violations:
        return f"exit_code={report.exit_code}; violations={violations}"
    return None


def _judge_supersession(vault, written, question_input, expect):
    if not isinstance(question_input, dict):
        raise ValueError("supersession input must be an object")
    _check_card_input(question_input.get("cards"), written, "input.cards")
    query = question_input.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("supersession input.query must be text")
    expected = _as_text_list(expect.get("current_only"), "current_only")
    result = memsearch.recall_index(vault, query)
    hits = _relative_hits(vault, result)
    returned_superseded = any(
        item.get(memspec.DECISION_STATUS_FIELD) == memspec.SUPERSEDED_DECISION_STATUS
        for item in result.get("results", ())
    )
    if hits != expected or returned_superseded:
        return f"results={hits}; returned_superseded={returned_superseded}"
    return None


def _run_question(question):
    if not isinstance(question, dict):
        raise ValueError("question must be an object")
    question_id = question.get("id")
    category = question.get("category")
    expect = question.get("expect")
    if not isinstance(question_id, str) or not question_id:
        raise ValueError("question id must be non-empty text")
    if category not in _CATEGORIES:
        raise ValueError(f"unsupported category: {category}")
    if not isinstance(expect, dict):
        raise ValueError("expect must be an object")

    with tempfile.TemporaryDirectory(prefix="epitype-exam-") as temp_dir:
        root = Path(temp_dir)
        vault = root / "vault"
        vault.mkdir()
        written = _write_cards(vault, question.get("setup"))
        if category == "recall":
            failure = _judge_recall(vault, question.get("input"), expect)
        elif category == "gate":
            failure = _judge_gate(vault, root, question.get("input"), expect)
        elif category == "lint":
            failure = _judge_lint(vault, written, question.get("input"), expect)
        else:
            failure = _judge_supersession(
                vault, written, question.get("input"), expect
            )
    return question_id, failure


def run_corpus(corpus):
    questions = corpus.get("questions") if isinstance(corpus, dict) else None
    if not isinstance(questions, list):
        raise ValueError("corpus must contain a questions list")
    if not questions:
        raise ValueError("corpus questions list must not be empty")
    results = []
    seen = set()
    for index, question in enumerate(questions, start=1):
        question_id = question.get("id") if isinstance(question, dict) else f"#{index}"
        identity = question_id if isinstance(question_id, str) else f"#{index}"
        if identity in seen:
            results.append((str(question_id), "duplicate question id"))
            continue
        seen.add(identity)
        try:
            results.append(_run_question(question))
        except Exception as exc:
            results.append((str(question_id), f"{type(exc).__name__}: {exc}"))
    return results


def _load_corpus(path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def _emit_results(results):
    passed = 0
    for question_id, failure in results:
        if failure is None:
            passed += 1
            print(f"PASS {question_id}")
        else:
            print(f"FAIL {question_id}: {failure}")
    print(f"SCORE {passed}/{len(results)}")
    return passed


def _result_exit_code(results, strict):
    return 1 if strict and any(failure is not None for _, failure in results) else 0


def _selftest():
    checks = []
    try:
        sample = _load_corpus(_SAMPLE_CORPUS)
        sample_results = run_corpus(sample)
        checks.append(
            (
                "sample corpus passes",
                len(sample_results) == 12
                and all(failure is None for _, failure in sample_results),
            )
        )

        broken = deepcopy(sample)
        broken["questions"][0]["expect"]["top_k_contains"] = ["missing-card.md"]
        broken_results = run_corpus(broken)
        failures = [item for item in broken_results if item[1] is not None]
        checks.append(
            (
                "deliberately broken question reaches FAIL",
                len(failures) == 1 and failures[0][0] == broken["questions"][0]["id"],
            )
        )
        checks.append(
            (
                "strict exit contract",
                _result_exit_code(broken_results, strict=True) == 1
                and _result_exit_code(sample_results, strict=True) == 0
                and _result_exit_code(broken_results, strict=False) == 0,
            )
        )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 3
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", nargs="?", default=str(_SAMPLE_CORPUS))
    parser.add_argument("--strict", action="store_true", help="exit 1 if any question fails")
    parser.add_argument("--selftest", action="store_true", help="run synthetic engine checks")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    try:
        results = run_corpus(_load_corpus(args.corpus))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    _emit_results(results)
    return _result_exit_code(results, args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
