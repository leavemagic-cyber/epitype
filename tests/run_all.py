import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，避免繁中輸出中斷。
"""依序執行 Epitype 核心工具與轉接器的內建合成測試。"""

import os
from pathlib import Path
import subprocess


SELFTESTS = (
    Path("epitype") / "memspec.py",
    Path("epitype") / "memsearch.py",
    Path("epitype") / "card_lint.py",
    Path("epitype") / "rule_examples.py",
    Path("epitype") / "draft_ttl.py",
    Path("epitype") / "views.py",
    Path("epitype") / "core_gen.py",
    Path("epitype") / "host_sync.py",
    Path("epitype") / "decision_lint.py",
    Path("epitype") / "ledger_gate.py",
    Path("epitype") / "compact_map.py",
    Path("epitype") / "scar_census.py",
    Path("epitype") / "token_meter.py",
    Path("epitype") / "gates_report.py",
    Path("epitype") / "pending_lint.py",
    Path("epitype") / "harvest.py",
    Path("epitype") / "capture_route.py",
    Path("epitype") / "alias_batch.py",
    Path("epitype") / "dream.py",
    Path("epitype") / "compliance.py",
    Path("epitype") / "recall_quiet.py",
    Path("adapters") / "claude" / "recall_hook.py",
    Path("adapters") / "claude" / "sessionstart_hook.py",
    Path("adapters") / "claude" / "precompact_hook.py",
    Path("adapters") / "claude" / "pretooluse_gate.py",
    Path("adapters") / "claude" / "stop_gate.py",
    Path("adapters") / "codex" / "config_guard.py",
    Path("adapters") / "codex" / "hook_trust.py",
    Path("install") / "graft.py",
    Path("install") / "scar_scan.py",
    Path("exam") / "exam_runner.py",
    Path("tests") / "capture_precision.py",
    Path("tests") / "privacy_lint.py",
    Path("tests") / "package_smoke.py",
    Path("tests") / "frontmatter_consistency.py",
    Path("tests") / "recall_regression.py",
    Path("tests") / "governance_regression.py",
    Path("tests") / "recall_selection_regression.py",
    Path("tests") / "no_semantic_gate_regression.py",
    Path("tests") / "card_io_regression.py",
    Path("tests") / "views_regression.py",
    Path("tests") / "harvest_safety_regression.py",
    Path("tests") / "alias_safety_regression.py",
    Path("tests") / "route_safety_regression.py",
    Path("tests") / "capture_integration_regression.py",
    Path("tests") / "capture_admission_regression.py",
    Path("tests") / "capture_recall_regression.py",
    Path("tests") / "dream_status_regression.py",
    Path("tests") / "dream_lock_regression.py",
    Path("tests") / "crontab_regression.py",
    Path("tests") / "scheduler_regression.py",
    Path("tests") / "stop_freshness_regression.py",
    Path("tests") / "transcript_provenance_regression.py",
    Path("tests") / "source_lookup_regression.py",
    Path("tests") / "compact_map_destination_regression.py",
    Path("tests") / "hook_input_bom_regression.py",
    Path("tests") / "action_guard_regression.py",
    Path("tests") / "handoff_regression.py",
    Path("tests") / "pending_lint_regression.py",
    Path("tests") / "quote_exemption_count_regression.py",
    Path("tests") / "nightly_replacement_notice_regression.py",
    Path("tests") / "core_drift_contract_vault_regression.py",
    Path("tests") / "review_scope_regression.py",
    Path("tests") / "turn_check_regression.py",
    Path("tests") / "prefilter_regression.py",
    Path("tests") / "budget_and_quiet_rules_regression.py",
    Path("tests") / "stranger_install_regression.py",
    # 2026-09-09 U-J：git_gate_regression.py 只驗傷疤卡 trigger 的命令比對，那條路徑
    # 整條移除後一併退役（不可逆 git 動作改由宿主原生規則負責）。
)


def _emit(text, stream):
    if text:
        print(text, end="" if text.endswith("\n") else "\n", file=stream)


def _unresolved_fixture_roots(repo_root):
    """Selftest fixtures must be resolved: GitHub's Windows runner hands out an 8.3
    temp path (C:\\Users\\RUNNER~1\\...) while the product resolves every vault, so an
    unresolved fixture string never equals the product's output there (CI 2026-09-02)."""
    offenders = []
    for path in sorted(repo_root.rglob("*.py")):
        if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(repo_root).parts):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "Path(temp_dir)" in line and "Path(temp_dir).resolve()" not in line:
                offenders.append(f"{path.relative_to(repo_root).as_posix()}:{number}")
    return offenders


def _run_selftest(repo_root, relative_path, environment):
    tool = repo_root / relative_path
    if not tool.is_file():
        return None, "missing tool"
    try:
        return subprocess.run(
            [sys.executable, str(tool), "--selftest"],
            cwd=repo_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ), None
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Run every component selftest.")
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="selftests to run at once; each uses its own temp directory, so CI runs several (default 1)",
    )
    options = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    passed = 0

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=max(1, options.jobs)) as pool:
        futures = [pool.submit(_run_selftest, repo_root, relative_path, environment) for relative_path in SELFTESTS]
        for relative_path, future in zip(SELFTESTS, futures):
            name = relative_path.as_posix()
            print(f"=== {name} ===")
            result, failure = future.result()
            if result is None:
                print(f"RESULT FAIL {name}: {failure}", file=sys.stderr)
                continue
            _emit(result.stdout, sys.stdout)
            _emit(result.stderr, sys.stderr)
            if result.returncode == 0:
                passed += 1
                print(f"RESULT PASS {name}")
            else:
                print(f"RESULT FAIL {name}: exit {result.returncode}", file=sys.stderr)

    offenders = _unresolved_fixture_roots(repo_root)
    if offenders:
        print("RESULT FAIL fixture roots must use Path(temp_dir).resolve(): " + ", ".join(offenders), file=sys.stderr)
    # CI 另跑全倉隱私掃描；本機只跑 selftest 的話，v1.4.0 那種只在 CI 紅的情況會再發生。
    scan = subprocess.run(
        [sys.executable, str(repo_root / "tests" / "privacy_lint.py")],
        cwd=repo_root, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    _emit(scan.stdout, sys.stdout)
    _emit(scan.stderr, sys.stderr)
    if scan.returncode != 0:
        print("RESULT FAIL privacy scan of tracked files", file=sys.stderr)
    print(f"TOTAL PASS {passed}/{len(SELFTESTS)}")
    return 0 if passed == len(SELFTESTS) and not offenders and scan.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
