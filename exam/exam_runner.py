import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 consoles must not break the exam entrypoint.
"""Run synthetic Epitype recall, abstention, gate, stop, lint, and supersession questions."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


_REPO_ROOT = Path(__file__).resolve().parents[1]
_EPITYPE_DIR = _REPO_ROOT / "epitype"
_ADAPTER_DIR = _REPO_ROOT / "adapters" / "claude"
for _import_root in (_REPO_ROOT, _EPITYPE_DIR, _ADAPTER_DIR):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from epitype import decision_lint, memsearch, memspec
from _hook_common import (
    clear_notice_markers,
    clear_recall_markers,
    governance_vault,
    load_config,
)


_CATEGORIES = {"recall", "abstention", "gate", "stop", "lint", "supersession"}
# 題目對卡的映射（U-P 第 3 行）：一題失敗時要回得到「這一題在管哪一條規則」。只認題目
# 層級明寫的欄位。`setup.vault_cards` 是這一題的合成庫，不是它在管的規則卡；拿它充數
# 會讓「未映射題數」永遠是 0，而那個數字正是這一項要量出來的缺口。
QUESTION_CARD_FIELDS = ("cards", "card", memspec.DECISION_KEY_FIELD)
RESULTS_VERSION = 1
RESULTS_SKIPPED = "RESULTS SKIPPED"
_SAMPLE_CORPUS = Path(__file__).with_name("sample_corpus.json")
_GATE_SCRIPT = _ADAPTER_DIR / "pretooluse_gate.py"
_STOP_SCRIPT = _ADAPTER_DIR / "stop_gate.py"
_RECALL_SCRIPT = _ADAPTER_DIR / "recall_hook.py"
# The seats recall_hook keeps for pinned lines: it cuts `[*decisions, *pinned]` to
# this many, so a ruling ranked past them never reached the turn at all.
_PINNED_WINDOW = memspec.RECALL_TOTAL_MAX_LINES


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


def _recall_card_lines(vault, root, prompt):
    """Card lines the real UserPromptSubmit hook injects, in the order it pins them.

    Which cards are pinned, and that a ruling's owner quote goes in uncut, is the
    hook's rule — read from its own output rather than re-derived here, or the exam
    would grade a second implementation. No session id is sent, so the hook's
    per-session dedupe markers are never written for an exam question."""
    stdout = _run_hook(_RECALL_SCRIPT, vault, root, {"prompt": prompt})
    if not stdout.strip():
        return []
    context = json.loads(stdout).get("hookSpecificOutput", {}).get("additionalContext", "")
    return [line for line in context.splitlines() if line.startswith("- ")]


def _judge_recall(vault, root, question_input, expect):
    if not isinstance(question_input, str) or not question_input.strip():
        raise ValueError("recall input must be a natural-language string")
    result = memsearch.recall_index(vault, question_input)
    hits = _relative_hits(vault, result)
    required = _as_text_list(expect.get("top_k_contains", []), "top_k_contains")
    excluded = _as_text_list(expect.get("top_k_excludes", []), "top_k_excludes")
    raw_pinned = expect.get("pinned_contains")
    if not required and not excluded and raw_pinned is None:
        raise ValueError("recall expect needs top_k_contains, top_k_excludes, or pinned_contains")
    missing = [item for item in required if item not in hits]
    forbidden = [item for item in excluded if item in hits]
    if missing or forbidden:
        return f"top_k={hits}; missing={missing}; forbidden={forbidden}"
    if raw_pinned is None:
        return None
    pinned = _as_text_list(raw_pinned, "pinned_contains")
    window = _recall_card_lines(vault, root, question_input)[:_PINNED_WINDOW]
    absent = [item for item in pinned if not any(item in line for line in window)]
    if absent:
        return f"pinned={window}; absent={absent}"
    return None


def _judge_abstention(vault, question_input, expect):
    """A prompt no card answers must bring back no card at all.

    Emptiness is judged on the result set, the same source `_judge_recall` reads:
    every injected card line (`- … | V1/path`) is rendered from one entry of
    `results`, so an empty result set is exactly an injection with no card line."""
    if not isinstance(question_input, str) or not question_input.strip():
        raise ValueError("abstention input must be a natural-language string")
    if expect.get("top_k_empty") is not True:
        raise ValueError("abstention expect.top_k_empty must be true")
    excluded = _as_text_list(expect.get("top_k_excludes", []), "top_k_excludes")
    result = memsearch.recall_index(vault, question_input)
    hits = _relative_hits(vault, result)
    forbidden = [item for item in excluded if item in hits]
    if hits or forbidden:
        return f"top_k={hits}; forbidden={forbidden}"
    return None


def _run_hook(script, vault, root, event):
    config = root / "hook-config.json"
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
        [sys.executable, os.fspath(script)],
        input=json.dumps(event, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_REPO_ROOT,
        env=environment,
        timeout=memspec.HOOK_TIMEOUT_SECONDS + 5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{script.name} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _run_gate(vault, root, event):
    """The write gate's verdict for one synthetic tool call.

    The gate blocks one (session, rule, file, content) exactly once and marks it in
    the shared temp marker directory, so a corpus replayed twice would see the
    second run allowed. Each question therefore runs under its own session id, and
    the markers it wrote are dropped again either way."""
    if not isinstance(event, dict):
        raise ValueError("gate input must be a tool event object")
    marked = f"exam-gate-{uuid.uuid4().hex}"
    try:
        stdout = _run_hook(_GATE_SCRIPT, vault, root, {**event, "session_id": marked})
    finally:
        clear_notice_markers(marked)
    if not stdout.strip():
        return "allow", ""
    output = json.loads(stdout).get("hookSpecificOutput", {})
    return (
        output.get("permissionDecision", "allow"),
        output.get("permissionDecisionReason", ""),
    )


def _run_stop(vault, root, question_input):
    """The turn-end gate's verdict for one synthetic assistant turn.

    A named session id is made run-unique: the gate blocks one
    (session, decision, message) once and marks it in the shared temp marker
    directory, so a reused literal id would let the first run of a corpus silence
    the second. The markers this question wrote are dropped again either way."""
    if not isinstance(question_input, dict):
        raise ValueError("stop input must be a turn-end event object")
    message = question_input.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("stop input.last_assistant_message must be text")
    active = question_input.get("stop_hook_active", False)
    if not isinstance(active, bool):
        raise ValueError("stop input.stop_hook_active must be true or false")
    session_id = question_input.get("session_id", "")
    if not isinstance(session_id, str):
        raise ValueError("stop input.session_id must be text")
    marked = f"{session_id}-{uuid.uuid4().hex}" if session_id.strip() else ""
    event = {
        "hook_event_name": "Stop",
        "session_id": marked,
        "stop_hook_active": active,
        "last_assistant_message": message,
    }
    try:
        stdout = _run_hook(_STOP_SCRIPT, vault, root, event)
    finally:
        if marked:
            clear_recall_markers(marked)
    if not stdout.strip():
        return "allow", ""
    value = json.loads(stdout)
    return value.get("decision", "allow"), value.get("reason", "")


def _judge_stop(vault, root, question_input, expect):
    expected_decision = expect.get("decision")
    expected_reason = expect.get("reason_contains")
    if expected_decision not in ("allow", "block"):
        raise ValueError("stop expect.decision must be allow or block")
    if expected_reason is not None and not isinstance(expected_reason, str):
        raise ValueError("stop expect.reason_contains must be text")
    decision, reason = _run_stop(vault, root, question_input)
    # Anything that is not a block lets the turn end, so it is graded as allow:
    # the gate answers with silence far more often than with a payload.
    if decision != "block":
        decision = "allow"
    if decision != expected_decision or (
        expected_reason is not None and expected_reason not in reason
    ):
        return f"decision={decision}; reason={reason!r}"
    return None


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


def _question_cards(question):
    """這一題對到的規則卡，題目沒寫就是空清單（＝未映射，總結會把它算出來）。"""
    found = []
    for field in QUESTION_CARD_FIELDS:
        value = question.get(field) if isinstance(question, dict) else None
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and item.strip() and item.strip() not in found:
                found.append(item.strip())
    return found


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
        root = Path(temp_dir).resolve()
        vault = root / "vault"
        vault.mkdir()
        written = _write_cards(vault, question.get("setup"))
        if category in ("recall", "abstention", "supersession"):
            memsearch.build_index(vault)
        if category == "recall":
            failure = _judge_recall(vault, root, question.get("input"), expect)
        elif category == "abstention":
            failure = _judge_abstention(vault, question.get("input"), expect)
        elif category == "gate":
            failure = _judge_gate(vault, root, question.get("input"), expect)
        elif category == "stop":
            failure = _judge_stop(vault, root, question.get("input"), expect)
        elif category == "lint":
            failure = _judge_lint(vault, written, question.get("input"), expect)
        else:
            failure = _judge_supersession(
                vault, written, question.get("input"), expect
            )
    return question_id, failure, _question_cards(question)


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
            results.append((str(question_id), "duplicate question id", []))
            continue
        seen.add(identity)
        try:
            results.append(_run_question(question))
        except Exception as exc:
            results.append(
                (str(question_id), f"{type(exc).__name__}: {exc}", _question_cards(question))
            )
    return results


def _load_corpus(path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def _emit_results(results):
    passed = 0
    unmapped = 0
    for question_id, failure, cards in results:
        if not cards:
            unmapped += 1
        suffix = f" | cards={','.join(cards)}" if cards else ""
        if failure is None:
            passed += 1
            print(f"PASS {question_id}{suffix}")
        else:
            print(f"FAIL {question_id}: {failure}{suffix}")
    print(f"SCORE {passed}/{len(results)}")
    # 沒有對到規則卡的題目要自己說出來：夢的檢討包只把失敗連回有映射的那些，未映射的
    # 那批是「查不到是哪條規則在管」的缺口，不是零缺口（U-P 第 3 行）。
    print(f"UNMAPPED {unmapped}/{len(results)}")
    return passed


def _result_exit_code(results, strict):
    return 1 if strict and any(failure is not None for _, failure, _cards in results) else 0


def _corpus_version(path):
    """規則版本＝題庫檔內容的 sha256 前 12 碼：題庫改一個字，這次的數字就換一個版本。"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def _record_results(corpus_path, results):
    """把這次的題號／通過／對到的卡／規則版本留在治理庫，夢的檢討包才有得讀。

    只在 `EPITYPE_CONFIG` 指路時寫，而且只寫那份設定的治理庫：考題會在別人的機器、CI
    與暫存庫上跑，沒有指路就不該猜一個真庫來寫（`~/.epitype/config.json` 那個預設在這
    裡刻意不採用）。寫不成只是少一份輸入，判分完全不受影響——但缺口要印出來，不能靜
    靜地少一份（CORE-10）。
    """
    if not os.environ.get(memspec.EPITYPE_CONFIG_ENV):
        return [f"{RESULTS_SKIPPED} {memspec.EPITYPE_CONFIG_ENV} 未設定"]
    payload = {
        "version": RESULTS_VERSION,
        "corpus": Path(corpus_path).name,
        "rules_version": _corpus_version(corpus_path),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {
            "total": len(results),
            "passed": sum(1 for _id, failure, _cards in results if failure is None),
            "failed": sum(1 for _id, failure, _cards in results if failure is not None),
            "unmapped": sum(1 for _id, _failure, cards in results if not cards),
        },
        "results": [
            {"id": question_id, "passed": failure is None, "cards": cards}
            for question_id, failure, cards in results
        ],
    }
    try:
        vault = governance_vault(load_config(time.monotonic()), for_write=True)
        target = Path(vault) / memspec.DREAM_DIRECTORY / memspec.EXAM_RESULTS_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
        )
        os.replace(temporary, target)
    except Exception as exc:
        return [f"{RESULTS_SKIPPED} {type(exc).__name__}: {exc}"]
    return [f"RESULTS {target}"]


def _selftest():
    checks = []
    try:
        sample = _load_corpus(_SAMPLE_CORPUS)
        sample_results = run_corpus(sample)
        by_id = {question["id"]: index for index, question in enumerate(sample["questions"])}
        checks.append(
            (
                "sample corpus passes",
                len(sample_results) == 16
                and all(failure is None for _, failure, _cards in sample_results),
            )
        )

        def graded(identifier, mutate=None):
            """One sample question re-run alone, optionally with `mutate` breaking it."""
            return run_alone(identifier, mutate)[1]

        def run_alone(identifier, mutate=None):
            corpus = {"questions": [deepcopy(sample["questions"][by_id[identifier]])]}
            if mutate is not None:
                mutate(corpus["questions"][0])
            return run_corpus(corpus)[0]

        checks.append(("a real turn-end block is graded PASS", graded("stop-zh-block") is None))
        checks.append(
            (
                "a turn the gate lets end cannot be sold as a block",
                graded(
                    "stop-zh-allow",
                    lambda question: question["expect"].update({"decision": "block"}),
                )
                is not None,
            )
        )
        checks.append(
            (
                "an abstention question that does hit a card reaches FAIL",
                graded(
                    "abstention-zh-empty",
                    lambda question: question.update({"input": "夜間批次的重試次數是多少？"}),
                )
                is not None,
            )
        )
        checks.append(
            (
                "a missing pinned ruling line reaches FAIL",
                graded(
                    "recall-zh-pinned",
                    lambda question: question["expect"].update(
                        {"pinned_contains": ["這句原話不在任何一行"]}
                    ),
                )
                is not None,
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

        # ── U-P 第 3 行：題目對卡映射 ──
        mapped_id = sample["questions"][0]["id"]
        mapped = run_alone(
            mapped_id,
            lambda question: question.update({"cards": ["decisions/one.md", "decisions/one.md"]}),
        )
        keyed = run_alone(
            mapped_id, lambda question: question.update({memspec.DECISION_KEY_FIELD: "one-key"})
        )
        checks.append((
            "a question that names its rule card carries that card out with its result",
            mapped[2] == ["decisions/one.md"]
            and keyed[2] == ["one-key"]
            and all(not cards for _id, _failure, cards in sample_results),
        ))
        checks.append((
            "the synthetic vault's own fixture cards are never sold as the question's mapping",
            not _question_cards(sample["questions"][0])
            and bool(sample["questions"][0]["setup"]["vault_cards"]),
        ))

        # ── U-P 第 3 行：機器可讀結果檔（絕不碰真庫：自己開一個暫存治理庫）──
        with tempfile.TemporaryDirectory(prefix="epitype-exam-results-") as results_dir:
            root = Path(results_dir).resolve()
            vault = root / "vault"
            vault.mkdir()
            config = root / "config.json"
            config.write_text(
                json.dumps({memspec.CONFIG_VAULTS_FIELD: [os.fspath(vault)]}, ensure_ascii=False),
                encoding="utf-8", newline="\n",
            )
            corpus_file = root / "corpus.json"
            corpus_file.write_text(
                json.dumps({"questions": [sample["questions"][by_id[mapped_id]]]}, ensure_ascii=False),
                encoding="utf-8", newline="\n",
            )
            target = vault / memspec.DREAM_DIRECTORY / memspec.EXAM_RESULTS_FILENAME
            expected_version = _corpus_version(corpus_file)
            saved = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
            try:
                os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None)
                unset_lines = _record_results(corpus_file, mapped_results := [mapped])
                unwritten = not target.exists()
                os.environ[memspec.EPITYPE_CONFIG_ENV] = os.fspath(config)
                written_lines = _record_results(corpus_file, mapped_results)
                value = json.loads(target.read_text(encoding="utf-8"))
            finally:
                if saved is None:
                    os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None)
                else:
                    os.environ[memspec.EPITYPE_CONFIG_ENV] = saved
        checks.append((
            "the results file lands in the configured governance vault, with the corpus version",
            len(written_lines) == 1
            and not written_lines[0].startswith(RESULTS_SKIPPED)
            and written_lines[0].startswith("RESULTS ")
            and value["rules_version"] == expected_version
            and len(value["rules_version"]) == 12
            and value["results"] == [
                {"id": mapped[0], "passed": mapped[1] is None, "cards": mapped[2]}
            ]
            and value["counts"]["unmapped"] == 0,
        ))
        checks.append((
            "no configured vault means no results file at all, and the gap is printed",
            unwritten
            and len(unset_lines) == 1
            and unset_lines[0].startswith(RESULTS_SKIPPED),
        ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    total = 11
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
    parser.add_argument(
        "--dry-run", action="store_true",
        help=f"只判分，不寫 {memspec.EXAM_RESULTS_FILENAME}",
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    try:
        results = run_corpus(_load_corpus(args.corpus))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    _emit_results(results)
    if not args.dry_run:
        for line in _record_results(args.corpus, results):
            print(line)
    return _result_exit_code(results, args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
